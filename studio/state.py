"""
studio/state.py - State management, persistence, undo/redo, and payload computation.
"""

import os
import json
import logging
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from studio.scanner import scan_directory

logger = logging.getLogger("curation_studio.state")
LOCK = threading.RLock()

CONFIG_DIR = Path.home() / ".photo_curation_studio"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'recent_libraries': [], 'last_opened': None}


def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save config: {e}")


class LibraryManager:
    def __init__(self, initial_folder: Optional[str | Path] = None):
        self.active_folder: Optional[Path] = None
        self.state_file: Optional[Path] = None
        self.state: Dict[str, Any] = {
            'root_dir': '',
            'tier1_clusters': ['Review & Unassigned', 'Trash / To Delete'],
            'tier2_clusters': {'Review & Unassigned': [], 'Trash / To Delete': []},
            'items': {}
        }
        self.undo_stack: List[Dict[str, Any]] = []
        self.file_cache: Dict[str, str] = {}

        if initial_folder:
            self.open_folder(initial_folder)
        else:
            cfg = _load_config()
            last = cfg.get('last_opened')
            if last and Path(last).exists():
                self.open_folder(last)

    def open_folder(self, folder_path: str | Path, rescan: bool = False) -> Dict[str, Any]:
        """Open a folder on the PC. Scans if no state file exists or rescan requested."""
        p = Path(folder_path).resolve()
        if not p.exists() or not p.is_dir():
            raise ValueError(f"Directory not found: {p}")

        with LOCK:
            self.active_folder = p
            # Check for existing state file in folder
            self.state_file = p / ".curation_state.json"
            alt_state_file = p / "curation_state.json"

            existing_state = None
            target_state_file = self.state_file

            if not rescan:
                if self.state_file.exists():
                    try:
                        with open(self.state_file, 'r', encoding='utf-8') as f:
                            existing_state = json.load(f)
                    except Exception as e:
                        logger.warning(f"Error reading {self.state_file}: {e}")
                elif alt_state_file.exists():
                    try:
                        with open(alt_state_file, 'r', encoding='utf-8') as f:
                            existing_state = json.load(f)
                            target_state_file = alt_state_file
                    except Exception as e:
                        logger.warning(f"Error reading {alt_state_file}: {e}")

            if existing_state and not rescan:
                self.state = existing_state
                self.state_file = target_state_file
                # Refresh disk paths in case folder was moved
                for fn, it in self.state.get('items', {}).items():
                    if 'disk_path' not in it or not Path(it['disk_path']).exists():
                        candidate = list(p.rglob(fn))
                        if candidate:
                            it['disk_path'] = str(candidate[0])
            else:
                # Scan folder to extract hierarchy into Level 1 and Level 2
                self.state = scan_directory(p, existing_state=existing_state, rescan=rescan)
                self.save_state()

            self._refresh_file_cache()

            # Record in recent libraries config
            cfg = _load_config()
            recents = cfg.get('recent_libraries', [])
            p_str = str(p)
            if p_str in recents:
                recents.remove(p_str)
            recents.insert(0, p_str)
            cfg['recent_libraries'] = recents[:10]
            cfg['last_opened'] = p_str
            _save_config(cfg)

            return self.compute_payload()

    def _refresh_file_cache(self):
        """Map filename -> disk_path for quick lookup."""
        self.file_cache.clear()
        for fn, it in self.state.get('items', {}).items():
            dp = it.get('disk_path')
            if dp and os.path.exists(dp):
                self.file_cache[fn] = dp
        # Also cache directly from disk if any items missing
        if self.active_folder and self.active_folder.exists():
            for root, _, files in os.walk(self.active_folder):
                for f in files:
                    if not f.startswith('.') and f not in self.file_cache:
                        self.file_cache[f] = os.path.join(root, f)

    def save_state(self):
        if not self.state_file:
            return
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save state to {self.state_file}: {e}")

    def compute_payload(self) -> Dict[str, Any]:
        with LOCK:
            counts = {}
            tier1_reviewed_counts = {}
            tier2_reviewed_counts = {}
            for c in self.state.get('tier1_clusters', []):
                counts[c] = 0
                tier1_reviewed_counts[c] = 0
                tier2_reviewed_counts[c] = 0

            items_list = list(self.state.get('items', {}).values())
            for it in items_list:
                c = it.get('tier1_cluster', 'Review & Unassigned')
                counts[c] = counts.get(c, 0) + 1
                if it.get('tier1_reviewed'):
                    tier1_reviewed_counts[c] = tier1_reviewed_counts.get(c, 0) + 1
                if it.get('tier2_reviewed'):
                    tier2_reviewed_counts[c] = tier2_reviewed_counts.get(c, 0) + 1

            # Build session groups
            sessions_map = {}
            for it in items_list:
                s_date = it.get('session_date') or 'Undated'
                t1 = it.get('tier1_cluster', 'Review & Unassigned')
                t2 = it.get('tier2_cluster')
                key = f"{s_date}___{t1}"
                if key not in sessions_map:
                    sessions_map[key] = {
                        'session_key': key,
                        'session_date': s_date,
                        'tier1_cluster': t1,
                        'tier2_cluster': t2,
                        'items': [],
                        'sample_description': it.get('visual_description', '')[:90]
                    }
                sessions_map[key]['items'].append(it)

            for s in sessions_map.values():
                items = s['items']
                s['tier1_reviewed'] = all(it.get('tier1_reviewed', False) for it in items)
                s['tier2_reviewed'] = all(it.get('tier2_reviewed', False) for it in items)

                # Unique subclusters
                subclusters = []
                for it in items:
                    t2 = it.get('tier2_cluster')
                    if t2 and t2 not in subclusters:
                        subclusters.append(t2)
                if len(subclusters) == 1:
                    s['tier2_cluster'] = subclusters[0]
                elif len(subclusters) > 1:
                    s['tier2_cluster'] = "Mixed (" + ", ".join(subclusters[:2]) + ")"
                else:
                    s['tier2_cluster'] = None

                # Aggregate common metadata tags
                locs = []
                for it in items:
                    loc = (it.get('location_or_event') or '').strip()
                    if loc and loc.lower() not in ('none', 'unknown', 'null', '') and loc not in locs:
                        locs.append(loc)
                s['locations'] = locs

                settings = []
                for it in items:
                    st = (it.get('setting') or '').strip()
                    if st and st.lower() not in ('none', 'unknown', 'null', '') and st not in settings:
                        settings.append(st)
                s['settings'] = settings

                perspectives = []
                for it in items:
                    tp = (it.get('taker_perspective') or '').strip()
                    if tp and tp.lower() not in ('none', 'unknown', 'null', '') and tp not in perspectives:
                        perspectives.append(tp)
                s['perspectives'] = perspectives

                cameras = []
                for it in items:
                    cam = (it.get('camera') or '').strip()
                    if cam and cam.lower() not in ('none', 'unknown', 'null', '', 'unknown / whatsapp', 'unknown') and cam not in cameras:
                        cameras.append(cam)
                s['cameras'] = cameras

            sorted_sessions = sorted(
                sessions_map.values(),
                key=lambda s: (s['session_date'] == 'Undated', s['session_date'], -len(s['items'])),
                reverse=True
            )

            total_tier1_reviewed = sum(1 for it in items_list if it.get('tier1_reviewed'))
            total_tier2_reviewed = sum(1 for it in items_list if it.get('tier2_reviewed'))

            cfg = _load_config()

            return {
                'active_folder': str(self.active_folder) if self.active_folder else '',
                'recent_libraries': cfg.get('recent_libraries', []),
                'tier1_clusters': self.state.get('tier1_clusters', []),
                'tier2_clusters': self.state.get('tier2_clusters', {}),
                'counts': counts,
                'tier1_reviewed_counts': tier1_reviewed_counts,
                'tier2_reviewed_counts': tier2_reviewed_counts,
                'total_tier1_reviewed': total_tier1_reviewed,
                'total_tier2_reviewed': total_tier2_reviewed,
                'total_items': len(items_list),
                'items': self.state.get('items', {}),
                'sessions': sorted_sessions
            }
