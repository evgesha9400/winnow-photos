"""
tests/test_state.py - Unit tests for LibraryManager with arbitrary N-level cluster tree and cascading actions.
"""

import tempfile
import unittest
from pathlib import Path
from studio.state import LibraryManager


class TestLibraryManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Create sample files
        (self.root / "img1.jpg").touch()
        t_dir = self.root / "Travel" / "Spain" / "Costa Brava"
        t_dir.mkdir(parents=True)
        (t_dir / "beach.jpg").touch()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_open_folder_and_tree(self):
        mgr = LibraryManager(self.root)
        self.assertEqual(mgr.state['root_dir'], str(self.root.resolve()))
        self.assertIn("img1.jpg", mgr.state['items'])
        self.assertIn("beach.jpg", mgr.state['items'])

        # beach.jpg should have 3 levels
        it = mgr.state['items']["beach.jpg"]
        self.assertEqual(it['cluster_path'], ["Travel", "Spain", "Costa Brava"])
        self.assertEqual(it['reviewed_depth'], 3)

        payload = mgr.compute_payload()
        self.assertIn("cluster_tree", payload)
        self.assertIn("Travel / Spain / Costa Brava", payload['node_stats'])
        self.assertEqual(payload['node_stats']["Travel / Spain / Costa Brava"]['total'], 1)

    def test_create_and_rename_cascading(self):
        mgr = LibraryManager(self.root)
        # Create child under Costa Brava
        new_path = mgr.create_cluster(["Travel", "Spain", "Costa Brava"], "Tossa")
        self.assertEqual(new_path, ["Travel", "Spain", "Costa Brava", "Tossa"])

        # Move beach.jpg into Tossa
        mgr.move_items(["beach.jpg"], ["Travel", "Spain", "Costa Brava", "Tossa"])
        self.assertEqual(mgr.state['items']["beach.jpg"]['cluster_path'], ["Travel", "Spain", "Costa Brava", "Tossa"])

        # Rename Spain -> Espana
        renamed = mgr.rename_cluster(["Travel", "Spain"], "Espana")
        self.assertEqual(renamed, ["Travel", "Espana"])

        # beach.jpg must have updated path automatically!
        it = mgr.state['items']["beach.jpg"]
        self.assertEqual(it['cluster_path'], ["Travel", "Espana", "Costa Brava", "Tossa"])

    def test_delete_cluster_cascading(self):
        mgr = LibraryManager(self.root)
        # Delete Travel
        reset_count = mgr.delete_cluster(["Travel"])
        self.assertEqual(reset_count, 1)

        # beach.jpg must be reset to Review & Unassigned
        it = mgr.state['items']["beach.jpg"]
        self.assertEqual(it['cluster_path'], ["Review & Unassigned"])
        self.assertEqual(it['reviewed_depth'], 0)
        self.assertEqual(it['status'], "unassigned")
        self.assertTrue(it.get('need_review'))

    def test_move_cluster(self):
        mgr = LibraryManager(self.root)
        mgr.create_cluster([], "Holidays")
        moved = mgr.move_cluster(["Travel", "Spain"], ["Holidays"])
        self.assertEqual(moved, ["Holidays", "Spain"])

        it = mgr.state['items']["beach.jpg"]
        self.assertEqual(it['cluster_path'], ["Holidays", "Spain", "Costa Brava"])

    def test_atomic_write_json(self):
        from studio.state import atomic_write_json
        import json
        target_file = self.root / "subdir" / "test_data.json"
        data = {"hello": "world", "num": 42}
        atomic_write_json(target_file, data)

        self.assertTrue(target_file.exists())
        with open(target_file, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        self.assertEqual(loaded, data)

        # Verify replacement
        new_data = {"updated": True}
        atomic_write_json(target_file, new_data)
        with open(target_file, 'r', encoding='utf-8') as f:
            loaded2 = json.load(f)
        self.assertEqual(loaded2, new_data)

    def test_confirm_items_bounded_depth(self):
        mgr = LibraryManager(self.root)
        # img1.jpg is at ["Review & Unassigned"] (len 1)
        # Attempt to confirm at depth 5
        mgr.confirm_items(["img1.jpg"], depth=5)
        it = mgr.state['items']["img1.jpg"]
        # Reviewed depth must not exceed path length (1)
        self.assertEqual(it['reviewed_depth'], 1)
        self.assertEqual(it['status'], 'confirmed')
        self.assertFalse(it.get('need_review', True))

        # Single item confirm_item method
        mgr.confirm_item("beach.jpg", depth=2)
        it_beach = mgr.state['items']["beach.jpg"]
        self.assertEqual(it_beach['reviewed_depth'], 2)

    def test_open_folder_rescan_preserves_state(self):
        mgr = LibraryManager(self.root)
        it = mgr.state['items']["img1.jpg"]
        it['visual_description'] = "Custom user description"
        it['status'] = "confirmed"
        it['reviewed_depth'] = 1
        mgr.save_state()

        # Rescan the directory
        rescan_mgr = LibraryManager()
        payload = rescan_mgr.open_folder(self.root, rescan=True)
        rescanned_it = rescan_mgr.state['items']["img1.jpg"]

        self.assertEqual(rescanned_it['visual_description'], "Custom user description")
        self.assertEqual(rescanned_it['status'], "confirmed")
        self.assertEqual(rescanned_it['reviewed_depth'], 1)

    def test_open_folder_invalid_path(self):
        mgr = LibraryManager()
        fake_path = self.root / "does_not_exist_xyz"
        with self.assertRaises(ValueError):
            mgr.open_folder(fake_path)
        self.assertIsNone(mgr.active_folder)
        self.assertIsNone(mgr.state_file)

    def test_canonical_path_matching_case_insensitive(self):
        mgr = LibraryManager(self.root)
        # Tree has ["Travel", "Spain", "Costa Brava"]
        # Rename using lower-case path ["travel", "spain"]
        new_path = mgr.rename_cluster(["travel", "spain"], "Espana")
        self.assertEqual(new_path, ["Travel", "Espana"])

        it = mgr.state['items']["beach.jpg"]
        self.assertEqual(it['cluster_path'], ["Travel", "Espana", "Costa Brava"])


if __name__ == '__main__':
    unittest.main()
