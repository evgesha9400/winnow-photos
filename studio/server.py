"""
studio/server.py - HTTP Server and REST API for Winnow Photos.
Zero-dependency, high-performance standard library server supporting streaming,
range requests, folder switching, cluster editing, and undo/redo.
"""

import os
import sys
import re
import json
import logging
import mimetypes
import shutil
import subprocess
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, Any, List

from studio.state import LibraryManager, LOCK, atomic_write_json

logger = logging.getLogger("winnow_photos.server")

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

            if path.startswith('/thumb/'):
                fn = path[7:]
                p = manager.file_cache.get(fn)
                if not p or not os.path.exists(p):
                    manager._refresh_file_cache()
                    p = manager.file_cache.get(fn)

                if not p or not os.path.exists(p):
                    self.send_error(404, f"File not found: {fn}")
                    return

                root_dir = manager.root or Path(p).parent
                thumb_dir = Path(root_dir) / ".curation_thumbs"
                thumb_dir.mkdir(parents=True, exist_ok=True)

                sha1 = hashlib.sha1(fn.encode('utf-8')).hexdigest()
                thumb_path = thumb_dir / f"{sha1}.jpg"

                if not thumb_path.exists():
                    try:
                        from PIL import Image, ImageOps
                        with Image.open(p) as img:
                            img = ImageOps.exif_transpose(img)
                            img = img.convert('RGB')
                            img.thumbnail((400, 400))
                            temp_fd, temp_thumb = tempfile.mkstemp(
                                dir=thumb_dir,
                                prefix=f".{sha1}.",
                                suffix=".tmp"
                            )
                            try:
                                with open(temp_fd, 'wb') as tf:
                                    img.save(tf, format='JPEG', quality=85)
                                os.replace(temp_thumb, thumb_path)
                            except Exception:
                                if os.path.exists(temp_thumb):
                                    try:
                                        os.remove(temp_thumb)
                                    except OSError:
                                        pass
                                raise
                    except Exception as e:
                        logger.warning(f"Could not generate thumbnail for {fn}: {e}")
                        self.send_error(500, f"Could not generate thumbnail: {e}")
                        return

                sz = os.path.getsize(thumb_path)
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(sz))
                self.send_header('Cache-Control', 'public, max-age=604800')
                self.end_headers()
                try:
                    with open(thumb_path, 'rb') as f:
                        shutil.copyfileobj(f, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass
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

            if path == '/api/recents/clear':
                recents = manager.clear_recent_libraries()
                self.send_json({'ok': True, 'recent_libraries': recents})
                return

            if path == '/api/recents/remove':
                target_path = req.get('path', '').strip()
                recents = manager.remove_recent_library(target_path)
                self.send_json({'ok': True, 'recent_libraries': recents})
                return

            # --- ITEM ACTIONS ---
            if path == '/api/items/confirm':
                filenames = req.get('filenames', [])
                depth = req.get('depth')
                if depth is None:
                    depth = req.get('pass', 1)
                count = manager.confirm_items(filenames, depth=int(depth))
                self.send_json({'ok': True, 'count': count})
                return

            if path == '/api/items/delete':
                filenames = req.get('filenames', [])
                count = manager.delete_items(filenames)
                self.send_json({'ok': True, 'count': count})
                return

            if path == '/api/items/move':
                filenames = req.get('filenames', [])
                target_path = req.get('target_path')
                if target_path is None:
                    t1 = req.get('tier1_cluster')
                    t2 = req.get('tier2_cluster')
                    if t1 and t2:
                        target_path = [t1, t2]
                    elif t1:
                        target_path = [t1]
                    else:
                        target_path = ['Review & Unassigned']
                count = manager.move_items(filenames, target_path)
                self.send_json({'ok': True, 'count': count})
                return

            if path == '/api/items/restore':
                snapshots = req.get('snapshots', [])
                restored = manager.restore_items(snapshots)
                self.send_json({'ok': True, 'restored': restored})
                return

            # --- CLUSTER MANAGEMENT (ARBITRARY N-LEVEL) ---
            if path == '/api/cluster/create':
                parent_path = req.get('parent_path')
                name = req.get('name', '').strip()
                if parent_path is None:
                    level = req.get('level', 1)
                    p = req.get('parent_tier1', '').strip()
                    parent_path = [p] if level == 2 and p else []

                if not name:
                    self.send_json({'ok': False, 'error': 'Name cannot be empty'}, status=400)
                    return

                new_path = manager.create_cluster(parent_path, name)
                if new_path:
                    self.send_json({'ok': True, 'cluster_path': new_path})
                else:
                    self.send_json({'ok': False, 'error': 'Failed to create cluster'}, status=400)
                return

            if path == '/api/cluster/rename':
                target_path = req.get('target_path')
                new_name = req.get('new_name', '').strip()
                if target_path is None:
                    level = req.get('level', 1)
                    old_name = req.get('old_name', '').strip()
                    p = req.get('parent_tier1', '').strip()
                    target_path = [p, old_name] if level == 2 and p else [old_name]

                if not target_path or not new_name:
                    self.send_json({'ok': False, 'error': 'Names cannot be empty'}, status=400)
                    return

                new_path = manager.rename_cluster(target_path, new_name)
                if new_path:
                    self.send_json({'ok': True, 'new_path': new_path})
                else:
                    self.send_json({'ok': False, 'error': 'Failed to rename cluster'}, status=400)
                return

            if path == '/api/cluster/delete':
                target_path = req.get('target_path')
                if target_path is None:
                    level = req.get('level', 1)
                    name = req.get('name', '').strip()
                    p = req.get('parent_tier1', '').strip()
                    target_path = [p, name] if level == 2 and p else [name]

                if not target_path:
                    self.send_json({'ok': False, 'error': 'Missing target_path'}, status=400)
                    return

                reset_count = manager.delete_cluster(target_path)
                self.send_json({'ok': True, 'reset_count': reset_count})
                return

            if path == '/api/cluster/move':
                source_path = req.get('source_path', [])
                new_parent_path = req.get('new_parent_path', [])
                if not source_path:
                    self.send_json({'ok': False, 'error': 'Missing source_path'}, status=400)
                    return

                new_path = manager.move_cluster(source_path, new_parent_path)
                if new_path:
                    self.send_json({'ok': True, 'new_path': new_path})
                else:
                    self.send_json({'ok': False, 'error': 'Failed to move cluster'}, status=400)
                return

            # --- AI SUGGESTIONS ---
            if path in ['/api/gemini/suggest', '/api/ai/suggest']:
                req_type = req.get('type', 'top_level')
                if req_type in ['top_level', 'subcluster']:
                    parent_path = req.get('parent_path')
                    parent_tier1 = req.get('parent_tier1', '')
                    if parent_path is None:
                        parent_path = [parent_tier1] if req_type == 'subcluster' and parent_tier1 else []
                    custom_prompt = req.get('custom_prompt', '').strip()

                    items_list = list(manager.state['items'].values())
                    if parent_path:
                        items_list = [
                            it for it in items_list
                            if it.get('cluster_path', [])[:len(parent_path)] == parent_path
                        ]

                    sample_items = items_list[:35]
                    sample_descriptions = [
                        f"- {it.get('filename')}: {it.get('visual_description', '')[:90]} (date: {it.get('session_date')}, loc: {it.get('location_or_event')}, cam: {it.get('camera')})"
                        for it in sample_items
                    ]
                    samples_text = "\n".join(sample_descriptions)

                    node = manager.cluster_tree.find_node(parent_path or [])
                    existing_clusters = [c.name for c in (node.children if node else []) if c.name not in ['Review & Unassigned', 'Trash / To Delete']]
                    custom_str = f"\nUser custom instructions: {custom_prompt}\n" if custom_prompt else ""

                    if not parent_path:
                        prompt = (
                            f"Suggest 6-8 cohesive top-level photo library categories based on this library sample:\n\n"
                            f"Sample photos:\n{samples_text}\n\n"
                            f"Currently existing categories:\n{json.dumps(existing_clusters)}\n"
                            f"{custom_str}\n"
                            f"Return ONLY a JSON array of strings, e.g. [\"Travel & Holidays\", \"Events & Celebrations\", \"Portraits\", \"Street Photography\"]."
                        )
                    else:
                        parent_str = " / ".join(parent_path)
                        prompt = (
                            f"Suggest 4-8 subcategories under parent category \"{parent_str}\" based on these photos:\n\n"
                            f"Sample photos:\n{samples_text}\n\n"
                            f"Currently existing subclusters:\n{json.dumps(existing_clusters)}\n"
                            f"{custom_str}\n"
                            f"Return ONLY a JSON array of strings, e.g. [\"Beach Sunsets\", \"City Walks\", \"Dining & Cafes\"]."
                        )

                    res = query_gemini_cli(prompt)
                    if not isinstance(res, list):
                        res = ["Travel & Vacations", "Special Events", "Portraits & Candid", "Outdoors & Walks", "Casual Everyday"] if not parent_path else ["Outdoors", "Dining", "Portraits", "Highlights"]
                    self.send_json({'ok': True, 'suggestions': res})
                    return

                elif req_type == 'photo':
                    fn = req.get('filename')
                    parent_path = req.get('parent_path')
                    parent_tier1 = req.get('parent_tier1')
                    if parent_path is None:
                        parent_path = [parent_tier1] if parent_tier1 else []
                    custom_prompt = req.get('custom_prompt', '').strip()

                    it = manager.state['items'].get(fn, {})
                    p_date = it.get('session_date', 'Unknown')
                    p_cam = it.get('camera', 'Unknown')
                    p_loc = it.get('location_or_event', '') or it.get('setting', '')
                    p_persp = it.get('taker_perspective', 'unknown')
                    p_desc = it.get('visual_description', '')
                    custom_str = f"\nUser custom instructions: {custom_prompt}\n" if custom_prompt else ""

                    node = manager.cluster_tree.find_node(parent_path or [])
                    existing_clusters = [c.name for c in (node.children if node else []) if c.name not in ['Review & Unassigned', 'Trash / To Delete']]

                    if not parent_path:
                        prompt = (
                            f"Analyze this photo to recommend the best top-level category:\n"
                            f"Filename: {fn}\n"
                            f"Date: {p_date}\n"
                            f"Camera: {p_cam}\n"
                            f"Setting: {p_loc}\n"
                            f"Perspective: {p_persp}\n"
                            f"Visual Description: {p_desc}\n\n"
                            f"Existing top-level categories:\n{json.dumps(existing_clusters)}\n"
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
                        parent_str = " / ".join(parent_path)
                        prompt = (
                            f"Analyze this photo to recommend the best subcluster under \"{parent_str}\":\n"
                            f"Filename: {fn}\n"
                            f"Date: {p_date}\n"
                            f"Camera: {p_cam}\n"
                            f"Setting: {p_loc}\n"
                            f"Perspective: {p_persp}\n"
                            f"Visual Description: {p_desc}\n\n"
                            f"Existing subclusters for \"{parent_str}\":\n{json.dumps(existing_clusters)}\n"
                            f"{custom_str}\n"
                            f"Recommend the best subcluster name under \"{parent_str}\" and 3-5 alternative suggestions.\n"
                            f"Return ONLY a JSON object in this format:\n"
                            f"{{\n"
                            f"  \"recommended\": \"Costa Brava Beach\",\n"
                            f"  \"explanation\": \"Outdoor coastal setting matching Costa Brava trip.\",\n"
                            f"  \"suggestions\": [\"Costa Brava Beach\", \"Seaside Strolls\", \"Sun & Sea\"]\n"
                            f"}}"
                        )

                    res = query_gemini_cli(prompt)
                    if not isinstance(res, dict) or 'recommended' not in res:
                        fallback_rec = existing_clusters[0] if existing_clusters else ("Travel & Holidays" if not parent_path else "General")
                        res = {
                            "recommended": fallback_rec,
                            "explanation": f"Based on visual metadata: {p_desc[:60] if p_desc else 'Standard photo portrait'}.",
                            "suggestions": [fallback_rec]
                        }
                    self.send_json({'ok': True, 'result': res})
                    return

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
                        cp = it.get('cluster_path')
                        if not cp:
                            t1 = it.get('tier1_cluster') or 'Review & Unassigned'
                            t2 = it.get('tier2_cluster')
                            cp = [t1, t2] if t2 else [t1]

                        if cp == ['Trash / To Delete']:
                            dest_rel = '🗑 macOS Trash (~/.Trash)'
                        else:
                            dest_rel = os.path.join(*cp)

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
                root_path = Path(manager.root) if manager.root else None
                if not root_path or not root_path.exists():
                    self.send_json({'ok': False, 'error': 'No active library root'}, status=400)
                    return

                real_root = os.path.realpath(str(root_path))
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                manifest_path = root_path / f'.apply_manifest_{ts}.json'

                with LOCK:
                    manager._refresh_file_cache()
                    for fn, item in list(manager.state.get('items', {}).items()):
                        if parent_cluster and item.get('tier1_cluster') != parent_cluster:
                            continue
                        if only_reviewed and not item.get('tier1_reviewed', False):
                            continue

                        cp = item.get('cluster_path')
                        if not cp:
                            t1 = item.get('tier1_cluster') or 'Review & Unassigned'
                            t2 = item.get('tier2_cluster')
                            cp = [t1, t2] if t2 else [t1]

                        src = manager.file_cache.get(fn)
                        if not src or not os.path.exists(src):
                            continue

                        if cp == ['Trash / To Delete']:
                            user_trash = os.path.expanduser('~/.Trash')
                            os.makedirs(user_trash, exist_ok=True)
                            dest_dir = user_trash
                        else:
                            # Sanitize cluster components (reject empty, .., ., or absolute paths)
                            valid = True
                            clean_cp = []
                            for comp in cp:
                                c = comp.strip()
                                if not c or c in ('.', '..') or '/' in c or '\\' in c or os.path.isabs(c):
                                    valid = False
                                    break
                                clean_cp.append(c)

                            if not valid or not clean_cp:
                                logger.warning(f"Skipping invalid or unsafe cluster components for {fn}: {cp}")
                                continue

                            dest_dir = os.path.join(str(root_path), *clean_cp)
                            os.makedirs(dest_dir, exist_ok=True)

                            # Verify destination directory is strictly inside manager.root
                            real_dest_dir = os.path.realpath(dest_dir)
                            try:
                                rel = os.path.relpath(real_dest_dir, real_root)
                                if rel == '..' or rel.startswith('..' + os.sep) or os.path.isabs(rel):
                                    logger.warning(f"Security: Destination {dest_dir} escapes root {real_root}")
                                    continue
                            except ValueError:
                                continue

                        dest_file = os.path.join(dest_dir, fn)
                        if os.path.abspath(src) == os.path.abspath(dest_file):
                            continue

                        # Collision resolution loop: ensure fallback filename is actually unused
                        if os.path.exists(dest_file):
                            base, ext = os.path.splitext(fn)
                            candidate = os.path.join(dest_dir, f"{base}_{ts}{ext}")
                            counter = 1
                            while os.path.exists(candidate):
                                candidate = os.path.join(dest_dir, f"{base}_{ts}_{counter}{ext}")
                                counter += 1
                            dest_file = candidate

                        try:
                            shutil.move(src, dest_file)
                            item['disk_path'] = dest_file
                            moves.append({'filename': fn, 'from': src, 'to': dest_file})
                        except Exception as e:
                            logger.error(f"Failed to move {fn} to {dest_file}: {e}")

                    manager.save_state()
                    manager._refresh_file_cache()

                # Fail safely if rollback manifest write fails
                try:
                    atomic_write_json(manifest_path, {'timestamp': ts, 'moves': moves})
                except Exception as e:
                    logger.error(f"Failed to write rollback manifest to {manifest_path}: {e}")
                    self.send_json({
                        'ok': False,
                        'error': f"Failed to write rollback manifest: {e}",
                        'moved_count': len(moves),
                        'moved': len(moves)
                    }, status=500)
                    return

                self.send_json({
                    'ok': True,
                    'moved_count': len(moves),
                    'moved': len(moves),
                    'manifest': str(manifest_path)
                })
                return

            self.send_error(404, "Unknown API route")

    return CurationRequestHandler


def run_server(port: int = 8766, initial_folder: Optional[str] = None, host: str = '127.0.0.1', force_rescan: bool = False, open_browser: bool = False):
    manager = LibraryManager(initial_folder=initial_folder, force_rescan=force_rescan)
    handler_class = create_handler(manager)
    try:
        server = HTTPServer((host, port), handler_class)
    except OSError as e:
        if e.errno == 48:
            print(f"Error: Port {port} is already in use. Specify a different port using --port <number>.", file=sys.stderr)
            sys.exit(1)
        raise
    url = f"http://{host}:{port}"
    print(f"Winnow Photos running at {url}")
    if manager.active_folder:
        print(f"Active library: {manager.active_folder}")
    else:
        print("No active library set. Open any folder from the web UI.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    server.serve_forever()
