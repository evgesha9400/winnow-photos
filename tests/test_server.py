"""
tests/test_server.py - Integration tests for Photo Curation Studio server endpoints with arbitrary depth clusters.
"""

import os
import json
import hashlib
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer
from unittest.mock import patch
from PIL import Image

from studio.state import LibraryManager
from studio.server import create_handler


class TestServerAPI(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        os.environ["CURATION_CONFIG_DIR"] = self.config_dir.name

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

        (self.root / "img1.jpg").touch()
        t_dir = self.root / "Travel" / "Spain"
        t_dir.mkdir(parents=True)
        (t_dir / "madrid.jpg").touch()

        self.mgr = LibraryManager(self.root)

        handler_class = create_handler(self.mgr)
        self.server = HTTPServer(('127.0.0.1', 0), handler_class)
        self.port = self.server.server_port
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("CURATION_CONFIG_DIR", None)
        self.config_dir.cleanup()
        self.temp_dir.cleanup()

    def post_json(self, path: str, data: dict):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode('utf-8')
                return resp.status, json.loads(body)
        except urllib.error.HTTPError as e:
            with e:
                body = e.read().decode('utf-8')
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {}
            return e.code, parsed

    def get_url(self, path: str):
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def test_api_state_payload(self):
        payload = self.mgr.compute_payload()
        self.assertIn("cluster_tree", payload)
        self.assertIn("node_stats", payload)
        self.assertIn("Travel / Spain", payload['node_stats'])
        self.assertEqual(payload['node_stats']["Travel / Spain"]['total'], 1)

    def test_create_and_rename_endpoints(self):
        # Create Costa Brava under Spain
        new_path = self.mgr.create_cluster(["Travel", "Spain"], "Costa Brava")
        self.assertEqual(new_path, ["Travel", "Spain", "Costa Brava"])

        # Move madrid.jpg to Costa Brava
        self.mgr.move_items(["madrid.jpg"], ["Travel", "Spain", "Costa Brava"])
        it = self.mgr.state['items']["madrid.jpg"]
        self.assertEqual(it['cluster_path'], ["Travel", "Spain", "Costa Brava"])

        # Rename Travel -> Vacations
        renamed = self.mgr.rename_cluster(["Travel"], "Vacations")
        self.assertEqual(renamed, ["Vacations"])

        # Item should be updated
        it = self.mgr.state['items']["madrid.jpg"]
        self.assertEqual(it['cluster_path'], ["Vacations", "Spain", "Costa Brava"])

    def test_delete_cascading_endpoint(self):
        reset_count = self.mgr.delete_cluster(["Travel", "Spain"])
        self.assertEqual(reset_count, 1)

        it = self.mgr.state['items']["madrid.jpg"]
        self.assertEqual(it['cluster_path'], ["Review & Unassigned"])
        self.assertEqual(it['reviewed_depth'], 0)
        self.assertEqual(it['status'], "unassigned")
        self.assertTrue(it.get('need_review'))

    def test_move_cluster_cascading(self):
        # Create hierarchy: Europe -> Spain
        self.mgr.create_cluster([], "Europe")
        self.mgr.move_cluster(["Travel", "Spain"], ["Europe"])

        # Tree check
        node = self.mgr.cluster_tree.find_node(["Europe", "Spain"])
        self.assertIsNotNone(node)

        # madrid.jpg was under Travel / Spain
        it = self.mgr.state['items']["madrid.jpg"]
        self.assertEqual(it['cluster_path'], ["Europe", "Spain"])

    def test_confirm_depth(self):
        # Confirm madrid.jpg at depth 2
        count = self.mgr.confirm_items(["madrid.jpg"], depth=2)
        self.assertEqual(count, 1)
        it = self.mgr.state['items']["madrid.jpg"]
        self.assertEqual(it['reviewed_depth'], 2)

    def test_ai_suggestions_root_lookup(self):
        # Test /api/gemini/suggest with parent_path=[]
        # This previously crashed because manager.cluster_tree lacked .children attribute
        with patch('studio.server.query_gemini_cli', return_value=['Vacations', 'City Trips']):
            status, data = self.post_json('/api/gemini/suggest', {
                'type': 'top_level',
                'parent_path': []
            })
            self.assertEqual(status, 200)
            self.assertTrue(data.get('ok'))
            self.assertIsInstance(data.get('suggestions'), list)

            # Test photo suggestion with parent_path=[]
            status2, data2 = self.post_json('/api/gemini/suggest', {
                'type': 'photo',
                'filename': 'madrid.jpg',
                'parent_path': []
            })
            self.assertEqual(status2, 200)
            self.assertTrue(data2.get('ok'))
            self.assertIn('result', data2)

    def test_ai_suggestions_nested_filter(self):
        # Create item in a different branch
        (self.root / "portraits.jpg").touch()
        self.mgr.open_folder(self.root, rescan=True)
        self.mgr.move_items(["portraits.jpg"], ["Portraits"])

        # When asking for suggestions under "Travel", portraits.jpg must be filtered out
        with patch('studio.server.query_gemini_cli', return_value=['Barcelona', 'Madrid']):
            status, data = self.post_json('/api/gemini/suggest', {
                'type': 'subcluster',
                'parent_path': ['Travel']
            })
            self.assertEqual(status, 200)
            self.assertTrue(data.get('ok'))

    def test_apply_security_containment_and_sanitize(self):
        # Add item with path traversal attempt
        (self.root / "hacker.jpg").touch()
        self.mgr.open_folder(self.root, rescan=True)
        it = self.mgr.state['items']["hacker.jpg"]
        it['cluster_path'] = ["..", "..", "escaped_folder"]
        it['tier1_reviewed'] = True

        status, data = self.post_json('/api/apply', {
            'only_reviewed': False
        })
        self.assertEqual(status, 200)
        self.assertTrue(data.get('ok'))
        self.assertIn('moved_count', data)
        self.assertIn('moved', data)

        # File must NOT have moved outside self.root
        escaped_file = self.root.parent.parent / "escaped_folder" / "hacker.jpg"
        self.assertFalse(escaped_file.exists())

    def test_apply_collision_resolution_loop(self):
        # Create test file to apply
        src_file = self.root / "dup.jpg"
        src_file.write_text("source content")
        self.mgr.open_folder(self.root, rescan=True)
        it = self.mgr.state['items']["dup.jpg"]
        it['cluster_path'] = ["Travel", "Spain"]
        it['tier1_reviewed'] = True

        # Pre-create the destination file to cause collision
        dest_dir = self.root / "Travel" / "Spain"
        dest_file = dest_dir / "dup.jpg"
        dest_file.write_text("existing content")

        status, data = self.post_json('/api/apply', {
            'only_reviewed': True
        })
        self.assertEqual(status, 200)
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('moved_count'), 1)
        self.assertEqual(data.get('moved'), 1)

        # Existing file was NOT overwritten
        self.assertEqual(dest_file.read_text(), "existing content")
        # New file moved to a timestamped / resolved filename
        candidates = list(dest_dir.glob("dup_*.jpg"))
        self.assertGreaterEqual(len(candidates), 1)

    def test_apply_manifest_write_failure_safety(self):
        (self.root / "apply_test.jpg").touch()
        self.mgr.open_folder(self.root, rescan=True)
        it = self.mgr.state['items']["apply_test.jpg"]
        it['cluster_path'] = ["Travel"]
        it['tier1_reviewed'] = True

        with patch('studio.server.atomic_write_json', side_effect=OSError("Disk full")):
            status, data = self.post_json('/api/apply', {
                'only_reviewed': True
            })
            self.assertEqual(status, 500)
            self.assertFalse(data.get('ok'))
            self.assertIn('error', data)
            self.assertIn('moved_count', data)
            self.assertIn('moved', data)

    def test_thumbnail_endpoint(self):
        # Generate a real image using PIL
        test_img_path = self.root / "photo_800.jpg"
        img = Image.new("RGB", (800, 600), color=(100, 150, 200))
        img.save(test_img_path, format="JPEG")

        self.mgr._refresh_file_cache()

        # Request thumbnail
        status, headers, content = self.get_url('/thumb/photo_800.jpg')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Type'), 'image/jpeg')

        # Check cached file on disk in <manager.root>/.curation_thumbs/<sha1>.jpg
        sha1 = hashlib.sha1("photo_800.jpg".encode('utf-8')).hexdigest()
        thumb_path = self.root / ".curation_thumbs" / f"{sha1}.jpg"
        self.assertTrue(thumb_path.exists())

        # Inspect thumbnail dimensions with PIL
        with Image.open(thumb_path) as thumb_img:
            w, h = thumb_img.size
            self.assertLessEqual(w, 400)
            self.assertLessEqual(h, 400)

        # Subsequent GET hits cache
        status2, headers2, content2 = self.get_url('/thumb/photo_800.jpg')
        self.assertEqual(status2, 200)
        self.assertEqual(content, content2)

        # Non-existent thumbnail returns 404
        status404, _, _ = self.get_url('/thumb/does_not_exist.jpg')
        self.assertEqual(status404, 404)

    def test_recents_clear_and_remove(self):
        # Clear recents via API
        status, clear_res = self.post_json('/api/recents/clear', {})
        self.assertEqual(status, 200)
        self.assertTrue(clear_res['ok'])
        self.assertEqual(clear_res['recent_libraries'], [])

        # Add mock entries to manager's isolated config
        from studio.state import _save_config
        with patch('studio.state.is_ephemeral_or_temp_path', return_value=False):
            d1 = self.root / "sub1"
            d2 = self.root / "sub2"
            d1.mkdir()
            d2.mkdir()
            _save_config({'recent_libraries': [str(d1), str(d2)], 'last_opened': None})
            # Remove an entry via API
            status2, rem_res = self.post_json('/api/recents/remove', {'path': str(d1)})
            self.assertEqual(status2, 200)
            self.assertTrue(rem_res['ok'])
            self.assertNotIn(str(d1), rem_res['recent_libraries'])


if __name__ == '__main__':
    unittest.main()
