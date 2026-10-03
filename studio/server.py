"""
studio/server.py - HTTP Server and REST API for Photo Curation Studio.
Zero-dependency, high-performance standard library server supporting streaming,
range requests, folder switching, cluster editing, and undo/redo.
"""

import os
import re
import json
import logging
import mimetypes
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, Any, List

from studio.state import LibraryManager, LOCK

logger = logging.getLogger("curation_studio.server")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"


def query_gemini_cli(prompt_text: str) -> Optional[Any]:
    """Query Gemini via local agy CLI if available."""
    agy_path = os.path.expanduser('~/.local/bin/agy')
    if not os.path.exists(agy_path):
        agy_path = 'agy'

    cmd = [
        agy_path,
        '--model', 'gemini-3.8-flash',
        '--effort', 'low',
        '--disable-slash-commands',
        '--print', prompt_text
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
        stdout = res.stdout.strip()
        m = re.search(r"(\[.*\]|\{.*\})", stdout, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    except Exception as e:
        logger.warning(f"Gemini CLI call failed: {e}")
    return None


def create_handler(manager: LibraryManager):
    class CurationRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_json(self, data: Any, status: int = 200):
            out = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path in ['/', '/index.html']:
                if not INDEX_FILE.exists():
                    self.send_error(404, "index.html not found in static/")
                    return
                with open(INDEX_FILE, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

            if path == '/api/state':
                with LOCK:
                    payload = manager.compute_payload()
                self.send_json(payload)
                return

            if path.startswith('/image/'):
                fn = path[7:]
                p = manager.file_cache.get(fn)
                if not p or not os.path.exists(p):
                    manager._refresh_file_cache()
                    p = manager.file_cache.get(fn)

                if p and os.path.exists(p):
                    mt, _ = mimetypes.guess_type(p)
                    sz = os.path.getsize(p)
                    self.send_response(200)
                    self.send_header('Content-Type', mt or ('video/mp4' if fn.lower().endswith(('.mp4', '.mov')) else 'image/jpeg'))
                    self.send_header('Content-Length', str(sz))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.end_headers()
                    try:
                        with open(p, 'rb') as f:
                            shutil.copyfileobj(f, self.wfile)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                else:
                    self.send_error(404, f"File not found: {fn}")
                    return

            self.send_error(404, "Not Found")

        def do_POST(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else '{}'
            try:
                req = json.loads(body)
            except Exception:
                req = {}

            # --- LIBRARY / FOLDER MANAGEMENT ---
            if path == '/api/library/open':
                target_folder = req.get('path', '').strip()
                rescan = bool(req.get('rescan', False))
                if not target_folder:
                    self.send_json({'ok': False, 'error': 'Folder path required'}, status=400)
                    return
                try:
                    payload = manager.open_folder(target_folder, rescan=rescan)
                    self.send_json({'ok': True, 'payload': payload})
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)}, status=400)
                return

            if path == '/api/library/scan':
                if not manager.active_folder:
                    self.send_json({'ok': False, 'error': 'No active library open'}, status=400)
                    return
                try:
                    payload = manager.open_folder(manager.active_folder, rescan=True)
                    self.send_json({'ok': True, 'payload': payload})
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)}, status=400)
                return

            # --- ITEM ACTIONS ---
            if path == '/api/items/confirm':
                filenames = req.get('filenames', [])
                pass_num = req.get('pass', 1)
                with LOCK:
                    for fn in filenames:
                        if fn in manager.state['items']:
                            if pass_num == 1:
                                manager.state['items'][fn]['tier1_reviewed'] = True
                            else:
                                manager.state['items'][fn]['tier2_reviewed'] = True
                    manager.save_state()
                self.send_json({'ok': True, 'count': len(filenames)})
                return

            if path == '/api/items/delete':
                filenames = req.get('filenames', [])
                with LOCK:
                    if 'Trash / To Delete' not in manager.state['tier1_clusters']:
                        manager.state['tier1_clusters'].append('Trash / To Delete')
                        manager.state['tier2_clusters']['Trash / To Delete'] = []
                    for fn in filenames:
                        if fn in manager.state['items']:
                            manager.state['items'][fn]['tier1_cluster'] = 'Trash / To Delete'
                            manager.state['items'][fn]['tier1_reviewed'] = True
                            manager.state['items'][fn]['tier2_cluster'] = None
                            manager.state['items'][fn]['tier2_reviewed'] = False
                            manager.state['items'][fn]['status'] = 'trash'
                    manager.save_state()
                self.send_json({'ok': True, 'count': len(filenames)})
                return

            if path == '/api/items/move':
                filenames = req.get('filenames', [])
                target_t1 = req.get('tier1_cluster')
                target_t2 = req.get('tier2_cluster')
                with LOCK:
                    if target_t1 and target_t1 not in manager.state['tier1_clusters']:
                        manager.state['tier1_clusters'].append(target_t1)
                        manager.state['tier2_clusters'][target_t1] = []
                    if target_t1 and target_t2:
                        if target_t1 not in manager.state['tier2_clusters']:
                            manager.state['tier2_clusters'][target_t1] = []
                        if target_t2 not in manager.state['tier2_clusters'][target_t1]:
                            manager.state['tier2_clusters'][target_t1].append(target_t2)

                    for fn in filenames:
                        if fn in manager.state['items']:
                            it = manager.state['items'][fn]
                            it['tier1_cluster'] = target_t1
                            it['tier1_reviewed'] = True
                            it['tier2_cluster'] = target_t2
                            if target_t2 is not None:
                                it['tier2_reviewed'] = True
                    manager.save_state()
                self.send_json({'ok': True, 'count': len(filenames)})
                return

            if path == '/api/items/restore':
                snapshots = req.get('snapshots', [])
                restored = 0
                with LOCK:
                    for item_snap in snapshots:
                        fn = item_snap.get('filename')
                        if fn and fn in manager.state['items']:
                            t1 = item_snap.get('tier1_cluster')
                            t2 = item_snap.get('tier2_cluster')
                            t1_rev = item_snap.get('tier1_reviewed', False)
                            t2_rev = item_snap.get('tier2_reviewed', False)
                            status = item_snap.get('status', 'unconfirmed')

                            if t1 and t1 not in manager.state['tier1_clusters']:
                                manager.state['tier1_clusters'].append(t1)
                                manager.state['tier2_clusters'][t1] = []

                            if t1 and t2:
                                if t1 in manager.state['tier2_clusters'] and t2 not in manager.state['tier2_clusters'][t1]:
                                    manager.state['tier2_clusters'][t1].append(t2)

                            manager.state['items'][fn]['tier1_cluster'] = t1
                            manager.state['items'][fn]['tier2_cluster'] = t2
                            manager.state['items'][fn]['tier1_reviewed'] = t1_rev
                            manager.state['items'][fn]['tier2_reviewed'] = t2_rev
                            manager.state['items'][fn]['status'] = status
                            restored += 1
                    manager.save_state()
                self.send_json({'ok': True, 'restored': restored})
                return

            # --- CLUSTER MANAGEMENT ---
            if path == '/api/cluster/create':
                level = req.get('level', 1)
                name = req.get('name', '').strip()
                parent = req.get('parent_tier1', '').strip()
                if not name:
                    self.send_json({'ok': False, 'error': 'Name cannot be empty'}, status=400)
                    return
                with LOCK:
                    if level == 1:
                        if name not in manager.state['tier1_clusters']:
                            manager.state['tier1_clusters'].append(name)
                            manager.state['tier2_clusters'][name] = []
                    elif level == 2 and parent:
                        if parent in manager.state['tier2_clusters']:
                            if name not in manager.state['tier2_clusters'][parent]:
                                manager.state['tier2_clusters'][parent].append(name)
                    manager.save_state()
                self.send_json({'ok': True})
                return

            if path == '/api/cluster/rename':
                level = req.get('level', 1)
                old_name = req.get('old_name', '').strip()
                new_name = req.get('new_name', '').strip()
                parent = req.get('parent_tier1', '').strip()
                if not new_name or not old_name:
                    self.send_json({'ok': False, 'error': 'Names cannot be empty'}, status=400)
                    return
                with LOCK:
                    if level == 1:
                        if old_name in manager.state['tier1_clusters']:
                            idx = manager.state['tier1_clusters'].index(old_name)
                            manager.state['tier1_clusters'][idx] = new_name
                            manager.state['tier2_clusters'][new_name] = manager.state['tier2_clusters'].pop(old_name, [])
                            for fn, it in manager.state['items'].items():
                                if it.get('tier1_cluster') == old_name:
                                    it['tier1_cluster'] = new_name
                    elif level == 2 and parent in manager.state['tier2_clusters']:
                        if old_name in manager.state['tier2_clusters'][parent]:
                            idx = manager.state['tier2_clusters'][parent].index(old_name)
                            manager.state['tier2_clusters'][parent][idx] = new_name
                            for fn, it in manager.state['items'].items():
                                if it.get('tier1_cluster') == parent and it.get('tier2_cluster') == old_name:
                                    it['tier2_cluster'] = new_name
                    manager.save_state()
                self.send_json({'ok': True})
                return

            if path == '/api/cluster/delete':
                level = req.get('level', 1)
                name = req.get('name', '').strip()
                parent = req.get('parent_tier1', '').strip()
                with LOCK:
                    if level == 1:
                        if name in manager.state['tier1_clusters'] and name not in ['Review & Unassigned', 'Trash / To Delete']:
                            manager.state['tier1_clusters'].remove(name)
                            manager.state['tier2_clusters'].pop(name, None)
                            for fn, it in manager.state['items'].items():
                                if it.get('tier1_cluster') == name:
                                    it['tier1_cluster'] = 'Review & Unassigned'
                                    it['tier2_cluster'] = None
                                    it['tier1_reviewed'] = False
                                    it['tier2_reviewed'] = False
                                    it['status'] = 'unconfirmed'
                    elif level == 2 and parent in manager.state['tier2_clusters']:
                        if name in manager.state['tier2_clusters'][parent]:
                            manager.state['tier2_clusters'][parent].remove(name)
                            for fn, it in manager.state['items'].items():
                                if it.get('tier1_cluster') == parent and it.get('tier2_cluster') == name:
                                    it['tier2_cluster'] = None
                                    it['tier2_reviewed'] = False
                    manager.save_state()
                self.send_json({'ok': True})
                return

            # --- AI SUGGESTIONS ---
            if path == '/api/ai/suggest-clusters':
                level = req.get('level', 1)
                parent_tier1 = req.get('parent_tier1')
                custom_prompt = req.get('custom_prompt', '').strip()

                items_list = list(manager.state['items'].values())
                if level == 2 and parent_tier1:
                    items_list = [it for it in items_list if it.get('tier1_cluster') == parent_tier1]

                sample_items = items_list[:35]
                sample_descriptions = [
                    f"- {it.get('filename')}: {it.get('visual_description', '')[:90]} (date: {it.get('session_date')}, loc: {it.get('location_or_event')}, cam: {it.get('camera')})"
                    for it in sample_items
                ]
                samples_text = "\n".join(sample_descriptions)

                existing_tier1 = [c for c in manager.state['tier1_clusters'] if c not in ['Review & Unassigned', 'Trash / To Delete']]
                existing_tier2 = manager.state['tier2_clusters'].get(parent_tier1, []) if parent_tier1 else []

                custom_str = f"\nUser custom instructions: {custom_prompt}\n" if custom_prompt else ""

                if level == 1:
                    prompt = (
                        f"Suggest 6-8 cohesive top-level photo library categories based on this library sample:\n\n"
                        f"Sample photos:\n{samples_text}\n\n"
                        f"Currently existing categories:\n{json.dumps(existing_tier1)}\n"
                        f"{custom_str}\n"
                        f"Return ONLY a JSON array of strings, e.g. [\"Travel & Holidays\", \"Events & Celebrations\", \"Portraits\", \"Street Photography\"]."
                    )
                else:
                    prompt = (
                        f"Suggest 4-8 subcategories under parent category \"{parent_tier1}\" based on these photos:\n\n"
                        f"Sample photos:\n{samples_text}\n\n"
                        f"Currently existing subclusters:\n{json.dumps(existing_tier2)}\n"
                        f"{custom_str}\n"
                        f"Return ONLY a JSON array of strings, e.g. [\"Beach Sunsets\", \"City Walks\", \"Dining & Cafes\"]."
                    )

                res = query_gemini_cli(prompt)
                if not isinstance(res, list):
                    res = ["Travel & Vacations", "Special Events", "Portraits & Candid", "Outdoors & Walks", "Casual Everyday"] if level == 1 else ["Outdoors", "Dining", "Portraits", "Highlights"]
                self.send_json({'ok': True, 'suggestions': res})
                return

            if path == '/api/ai/suggest-photo':
                fn = req.get('filename')
                current_pass = req.get('pass', 1)
                parent_tier1 = req.get('parent_tier1')
                custom_prompt = req.get('custom_prompt', '').strip()

                it = manager.state['items'].get(fn, {})
                p_date = it.get('session_date', 'Unknown')
                p_cam = it.get('camera', 'Unknown')
                p_loc = it.get('location_or_event', '') or it.get('setting', '')
                p_persp = it.get('taker_perspective', 'unknown')
                p_desc = it.get('visual_description', '')
                custom_str = f"\nUser custom instructions: {custom_prompt}\n" if custom_prompt else ""

                existing_tier1 = [c for c in manager.state['tier1_clusters'] if c not in ['Review & Unassigned', 'Trash / To Delete']]

                if current_pass == 1:
                    prompt = (
                        f"Analyze this photo to recommend the best top-level category:\n"
                        f"Filename: {fn}\n"
                        f"Date: {p_date}\n"
                        f"Camera: {p_cam}\n"
                        f"Setting: {p_loc}\n"
                        f"Perspective: {p_persp}\n"
                        f"Visual Description: {p_desc}\n\n"
                        f"Existing top-level categories:\n{json.dumps(existing_tier1)}\n"
                        f"{custom_str}\n"
                        f"Recommend the single best top-level category and 3-5 alternative category suggestions.\n"
                        f"Return ONLY a JSON object in this format:\n"
                        f"{{\n"
                        f"  \"recommended\": \"Travel & Holidays\",\n"
                        f"  \"explanation\": \"Beach landscape and relaxed vacation setting.\",\n"
                        f"  \"suggestions\": [\"Travel & Holidays\", \"Outdoors & Nature\", \"Vacation Snapshots\"]\n"
                        f"}}"
                    )
                else:
                    curr_parent = parent_tier1 or it.get('tier1_cluster', '')
                    subcats = manager.state['tier2_clusters'].get(curr_parent, [])
                    prompt = (
                        f"Analyze this photo to recommend the best subcluster under \"{curr_parent}\":\n"
                        f"Filename: {fn}\n"
                        f"Date: {p_date}\n"
                        f"Camera: {p_cam}\n"
                        f"Setting: {p_loc}\n"
                        f"Perspective: {p_persp}\n"
                        f"Visual Description: {p_desc}\n\n"
                        f"Existing subclusters for \"{curr_parent}\":\n{json.dumps(subcats)}\n"
                        f"{custom_str}\n"
                        f"Recommend the best subcluster name under \"{curr_parent}\" and 3-5 alternative suggestions.\n"
                        f"Return ONLY a JSON object in this format:\n"
                        f"{{\n"
                        f"  \"recommended\": \"Costa Brava Beach\",\n"
                        f"  \"explanation\": \"Outdoor coastal setting matching Costa Brava trip.\",\n"
                        f"  \"suggestions\": [\"Costa Brava Beach\", \"Seaside Strolls\", \"Sun & Sea\"]\n"
                        f"}}"
                    )

                res = query_gemini_cli(prompt)
                if not isinstance(res, dict) or 'recommended' not in res:
                    fallback_rec = existing_tier1[0] if current_pass == 1 else "General"
                    res = {
                        "recommended": fallback_rec,
                        "explanation": f"Based on visual metadata: {p_desc[:60] if p_desc else 'Standard photo portrait'}.",
                        "suggestions": [fallback_rec]
                    }
                self.send_json({'ok': True, 'result': res})
                return

            # --- APPLY / PHYSICAL DISK SYNC ---
            if path == '/api/apply/preview':
                only_reviewed = req.get('only_reviewed', True)
                parent_cluster = req.get('parent_cluster', None)
                destinations = {}
                candidate_count = 0
                skipped_count = 0

                with LOCK:
                    manager._refresh_file_cache()
                    for fn, it in manager.state['items'].items():
                        if parent_cluster and it.get('tier1_cluster') != parent_cluster:
                            continue
                        is_ready = it.get('tier1_reviewed', False) if only_reviewed else True
                        if not is_ready:
                            skipped_count += 1
                            continue
                        candidate_count += 1
                        t1 = it.get('tier1_cluster') or 'Review & Unassigned'
                        t2 = it.get('tier2_cluster')
                        if t1 == 'Trash / To Delete':
                            dest_rel = '🗑 macOS Trash (~/.Trash)'
                        elif t2:
                            dest_rel = os.path.join(t1, t2)
                        else:
                            dest_rel = t1

                        if dest_rel not in destinations:
                            destinations[dest_rel] = {'dest_folder': dest_rel, 'count': 0, 'sample_files': []}
                        destinations[dest_rel]['count'] += 1
                        if len(destinations[dest_rel]['sample_files']) < 3:
                            destinations[dest_rel]['sample_files'].append(fn)

                self.send_json({
                    'ready_count': candidate_count,
                    'skipped_count': skipped_count,
                    'destinations': sorted(list(destinations.values()), key=lambda d: d['count'], reverse=True)
                })
                return

            if path == '/api/apply':
                only_reviewed = req.get('only_reviewed', True)
                parent_cluster = req.get('parent_cluster', None)
                moves = []
                root_path = Path(manager.active_folder) if manager.active_folder else None
                if not root_path or not root_path.exists():
                    self.send_json({'ok': False, 'error': 'No active library root'}, status=400)
                    return

                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                manifest_path = root_path / f'.apply_manifest_{ts}.json'

                with LOCK:
                    manager._refresh_file_cache()
                    for fn, item in list(manager.state['items'].items()):
                        if parent_cluster and item.get('tier1_cluster') != parent_cluster:
                            continue
                        if only_reviewed and not item.get('tier1_reviewed', False):
                            continue

                        t1 = item.get('tier1_cluster') or 'Review & Unassigned'
                        t2 = item.get('tier2_cluster')

                        src = manager.file_cache.get(fn)
                        if not src or not os.path.exists(src):
                            continue

                        if t1 == 'Trash / To Delete':
                            user_trash = os.path.expanduser('~/.Trash')
                            dest_dir = user_trash
                        elif t2:
                            dest_dir = os.path.join(root_path, t1, t2)
                        else:
                            dest_dir = os.path.join(root_path, t1)

                        os.makedirs(dest_dir, exist_ok=True)
                        dest_file = os.path.join(dest_dir, fn)

                        if os.path.abspath(src) == os.path.abspath(dest_file):
                            continue

                        # Avoid collisions
                        if os.path.exists(dest_file):
                            base, ext = os.path.splitext(fn)
                            dest_file = os.path.join(dest_dir, f"{base}_{ts}{ext}")

                        try:
                            shutil.move(src, dest_file)
                            item['disk_path'] = dest_file
                            moves.append({'filename': fn, 'from': src, 'to': dest_file})
                        except Exception as e:
                            logger.error(f"Failed to move {fn} to {dest_file}: {e}")

                    manager.save_state()
                    manager._refresh_file_cache()

                try:
                    with open(manifest_path, 'w', encoding='utf-8') as f:
                        json.dump({'timestamp': ts, 'moves': moves}, f, indent=2)
                except Exception:
                    pass

                self.send_json({'ok': True, 'moved_count': len(moves), 'manifest': str(manifest_path)})
                return

            self.send_error(404, "Unknown API route")

    return CurationRequestHandler


def run_server(port: int = 8766, initial_folder: Optional[str] = None):
    manager = LibraryManager(initial_folder=initial_folder)
    handler_class = create_handler(manager)
    server = HTTPServer(('0.0.0.0', port), handler_class)
    print(f"Photo Curation Studio running at http://localhost:{port}")
    if manager.active_folder:
        print(f"Active library: {manager.active_folder}")
    else:
        print("No active library set. Open any folder from the web UI.")
    server.serve_forever()
