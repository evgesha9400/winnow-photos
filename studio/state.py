"""
studio/state.py - State management, persistence, undo/redo, and payload computation.
Supports arbitrary N-level cluster trees and cascading operations.
"""

import os
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from studio.scanner import scan_directory
from studio.cluster_tree import ClusterTree, ClusterNode

logger = logging.getLogger("curation_studio.state")
LOCK = threading.RLock()

CONFIG_DIR = Path.home() / ".photo_curation_studio"
CONFIG_FILE = CONFIG_DIR / "config.json"


def atomic_write_json(file_path: str | Path, data: Any) -> None:
    """
    Atomically serialize data as JSON to file_path.
    Writes to a temporary file in the same parent directory, flushes,
    runs os.fsync, and replaces the target file using os.replace.
    """
    p = Path(file_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=p.parent,
        prefix=f".{p.name}.",
        suffix=".tmp"
    )
    try:
        with open(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, p)
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


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
        atomic_write_json(CONFIG_FILE, cfg)
    except Exception as e:
        logger.warning(f"Could not save config: {e}")


class LibraryManager:
    def __init__(self, initial_folder: Optional[str | Path] = None, force_rescan: bool = False):
        self.active_folder: Optional[Path] = None
        self.state_file: Optional[Path] = None
        self.cluster_tree: ClusterTree = ClusterTree()
        self.state: Dict[str, Any] = {
            'root_dir': '',
            'cluster_tree': self.cluster_tree.to_dict(),
            'tier1_clusters': ['Review & Unassigned', 'Trash / To Delete'],
            'tier2_clusters': {'Review & Unassigned': [], 'Trash / To Delete': []},
            'items': {}
        }
        self.undo_stack: List[Dict[str, Any]] = []
        self.file_cache: Dict[str, str] = {}

        if initial_folder:
            self.open_folder(initial_folder, rescan=force_rescan)
        else:
            cfg = _load_config()
            last = cfg.get('last_opened')
            if last and Path(last).exists():
                self.open_folder(last, rescan=force_rescan)

    @property
    def root(self) -> Optional[Path]:
        """Alias for active_folder."""
        return self.active_folder

    def _ensure_cluster_tree(self):
        """Build and synchronize ClusterTree from state and upgrade items if needed."""
        if 'cluster_tree' in self.state and self.state['cluster_tree']:
            self.cluster_tree = ClusterTree.from_dict(self.state['cluster_tree'])
        elif 'tier1_clusters' in self.state:
            self.cluster_tree = ClusterTree.from_legacy_tiers(
                self.state.get('tier1_clusters', []),
                self.state.get('tier2_clusters', {})
            )
        else:
            self.cluster_tree = ClusterTree()

        # Upgrade all items to have cluster_path and reviewed_depth
        for fn, it in self.state.get('items', {}).items():
            if 'cluster_path' not in it or not it['cluster_path']:
                t1 = it.get('tier1_cluster') or 'Review & Unassigned'
                t2 = it.get('tier2_cluster')
                it['cluster_path'] = [t1, t2] if t2 else [t1]

            if 'reviewed_depth' not in it:
                if it.get('tier2_reviewed'):
                    it['reviewed_depth'] = 2
                elif it.get('tier1_reviewed'):
                    it['reviewed_depth'] = 1
                else:
                    it['reviewed_depth'] = 0

            # Backwards compatibility fields
            cp = it['cluster_path']
            it['tier1_cluster'] = cp[0] if cp else 'Review & Unassigned'
            it['tier2_cluster'] = cp[1] if len(cp) > 1 else None
            it['tier1_reviewed'] = (it['reviewed_depth'] >= 1)
            it['tier2_reviewed'] = (it['reviewed_depth'] >= 2)

            self.cluster_tree.add_path(cp)

    def open_folder(self, folder_path: str | Path, rescan: bool = False) -> Dict[str, Any]:
        """Open a folder on the PC. Scans if no state file exists or rescan requested."""
        p = Path(folder_path).resolve()
        if not p.exists() or not p.is_dir():
            raise ValueError(f"Directory not found: {p}")

        with LOCK:
            state_file = p / ".curation_state.json"
            alt_state_file = p / "curation_state.json"

            existing_state = None
            target_state_file = state_file

            # Always attempt to load existing curation state first to preserve human decisions
            if state_file.exists():
                try:
                    with open(state_file, 'r', encoding='utf-8') as f:
                        existing_state = json.load(f)
                except Exception as e:
                    logger.warning(f"Error reading {state_file}: {e}")
            elif alt_state_file.exists():
                try:
                    with open(alt_state_file, 'r', encoding='utf-8') as f:
                        existing_state = json.load(f)
                        target_state_file = alt_state_file
                except Exception as e:
                    logger.warning(f"Error reading {alt_state_file}: {e}")

            if existing_state and not rescan:
                new_state = existing_state
                # Refresh disk paths if folder moved
                for fn, it in new_state.get('items', {}).items():
                    if 'disk_path' not in it or not Path(it['disk_path']).exists():
                        candidate = list(p.rglob(fn))
                        if candidate:
                            it['disk_path'] = str(candidate[0])
            else:
                new_state = scan_directory(p, existing_state=existing_state, rescan=rescan)

            # Update instance pointers only after folder is validated and scan/load succeeds
            self.active_folder = p
            self.state_file = target_state_file
            self.state = new_state
            self._ensure_cluster_tree()

            if not existing_state or rescan:
                self.save_state()

            self._refresh_file_cache()

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
        if self.active_folder and self.active_folder.exists():
            for root, _, files in os.walk(self.active_folder):
                for f in files:
                    if not f.startswith('.') and f not in self.file_cache:
                        self.file_cache[f] = os.path.join(root, f)

    def save_state(self):
        if not self.state_file:
            return
        with LOCK:
            # Keep cluster_tree in sync before saving
            self.state['cluster_tree'] = self.cluster_tree.to_dict()

            # Sync legacy tier1 and tier2
            t1_names = self.cluster_tree.get_children_names([])
            sorted_t1 = sorted(
                t1_names,
                key=lambda c: (c == 'Review & Unassigned', c == 'Trash / To Delete', c)
            )
            self.state['tier1_clusters'] = sorted_t1
            self.state['tier2_clusters'] = {
                t1: self.cluster_tree.get_children_names([t1]) for t1 in sorted_t1
            }

            try:
                atomic_write_json(self.state_file, self.state)
            except Exception as e:
                logger.error(f"Failed to save state to {self.state_file}: {e}")
                raise

    # --- CLUSTER TREE OPERATIONS ---

    def create_cluster(self, parent_path: List[str], name: str) -> Optional[List[str]]:
        """Create a cluster under parent_path. Returns full path of new cluster."""
        with LOCK:
            node = self.cluster_tree.create_cluster(parent_path, name)
            if node:
                self.save_state()
                return node.get_path()
        return None

    def rename_cluster(self, target_path: List[str], new_name: str) -> Optional[List[str]]:
        """Rename cluster at target_path and cascadingly update all item paths."""
        with LOCK:
            node = self.cluster_tree.find_node(target_path)
            if not node or node == self.cluster_tree.root:
                return None

            canonical_old_path = node.get_path()
            ok = self.cluster_tree.rename_cluster(canonical_old_path, new_name)
            if not ok:
                return None

            canonical_new_path = node.get_path()
            prefix_len = len(canonical_old_path)
            old_prefix_lower = [p.strip().lower() for p in canonical_old_path]

            for fn, it in self.state.get('items', {}).items():
                cp = it.get('cluster_path', [])
                if len(cp) >= prefix_len and [p.strip().lower() for p in cp[:prefix_len]] == old_prefix_lower:
                    it['cluster_path'] = canonical_new_path + cp[prefix_len:]
                    # Legacy sync
                    it['tier1_cluster'] = it['cluster_path'][0] if it['cluster_path'] else 'Review & Unassigned'
                    it['tier2_cluster'] = it['cluster_path'][1] if len(it['cluster_path']) > 1 else None

            self.save_state()
            return canonical_new_path

    def delete_cluster(self, target_path: List[str]) -> int:
        """
        Delete cluster at target_path and all its descendants.
        Resets all member items in the subtree to Review & Unassigned.
        Returns count of reset items.
        """
        with LOCK:
            node = self.cluster_tree.find_node(target_path)
            if not node or node == self.cluster_tree.root:
                return 0

            canonical_target_path = node.get_path()
            ok = self.cluster_tree.delete_cluster(canonical_target_path)
            if not ok:
                return 0

            prefix_len = len(canonical_target_path)
            target_prefix_lower = [p.strip().lower() for p in canonical_target_path]
            reset_count = 0

            for fn, it in self.state.get('items', {}).items():
                cp = it.get('cluster_path', [])
                if len(cp) >= prefix_len and [p.strip().lower() for p in cp[:prefix_len]] == target_prefix_lower:
                    it['cluster_path'] = ['Review & Unassigned']
                    it['reviewed_depth'] = 0
                    it['status'] = 'unassigned'
                    it['need_review'] = True
                    it['tier1_cluster'] = 'Review & Unassigned'
                    it['tier2_cluster'] = None
                    it['tier1_reviewed'] = False
                    it['tier2_reviewed'] = False
                    reset_count += 1

            self.save_state()
            return reset_count

    def move_cluster(self, source_path: List[str], new_parent_path: List[str]) -> Optional[List[str]]:
        """Move cluster subtree from source_path under new_parent_path."""
        with LOCK:
            node = self.cluster_tree.find_node(source_path)
            if not node or node == self.cluster_tree.root:
                return None

            canonical_source_path = node.get_path()
            ok = self.cluster_tree.move_cluster(canonical_source_path, new_parent_path)
            if not ok:
                return None

            canonical_new_path = node.get_path()
            prefix_len = len(canonical_source_path)
            source_prefix_lower = [p.strip().lower() for p in canonical_source_path]

            for fn, it in self.state.get('items', {}).items():
                cp = it.get('cluster_path', [])
                if len(cp) >= prefix_len and [p.strip().lower() for p in cp[:prefix_len]] == source_prefix_lower:
                    it['cluster_path'] = canonical_new_path + cp[prefix_len:]
                    it['tier1_cluster'] = it['cluster_path'][0] if it['cluster_path'] else 'Review & Unassigned'
                    it['tier2_cluster'] = it['cluster_path'][1] if len(it['cluster_path']) > 1 else None

            self.save_state()
            return canonical_new_path

    reparent_cluster = move_cluster

    # --- ITEM LEVEL OPERATIONS ---

    def move_items(self, filenames: List[str], target_path: List[str]) -> int:
        """Move items to target_path and mark as reviewed at target level."""
        with LOCK:
            self.cluster_tree.add_path(target_path)
            depth = len(target_path)
            count = 0
            for fn in filenames:
                if fn in self.state.get('items', {}):
                    it = self.state['items'][fn]
                    it['cluster_path'] = list(target_path)
                    it['reviewed_depth'] = depth
                    it['tier1_cluster'] = target_path[0] if target_path else 'Review & Unassigned'
                    it['tier2_cluster'] = target_path[1] if len(target_path) > 1 else None
                    it['tier1_reviewed'] = (depth >= 1)
                    it['tier2_reviewed'] = (depth >= 2)
                    it['status'] = 'confirmed'
                    it['need_review'] = False
                    count += 1
            self.save_state()
            return count

    def confirm_item(self, filename: str, depth: int = 1) -> bool:
        """Confirm a single item up to depth bounded by cluster_path length."""
        with LOCK:
            it = self.state.get('items', {}).get(filename)
            if not it:
                return False
            cp = it.get("cluster_path", [])
            it['reviewed_depth'] = max(1, min(depth, len(cp))) if cp else 0
            it['tier1_reviewed'] = (it['reviewed_depth'] >= 1)
            it['tier2_reviewed'] = (it['reviewed_depth'] >= 2)
            it['status'] = 'confirmed'
            it['need_review'] = False
            self.save_state()
            return True

    def confirm_items(self, filenames: List[str], depth: int = 1) -> int:
        """Confirm items up to the specified depth."""
        with LOCK:
            count = 0
            for fn in filenames:
                if fn in self.state.get('items', {}):
                    it = self.state['items'][fn]
                    cp = it.get("cluster_path", [])
                    effective_depth = max(1, min(depth, len(cp))) if cp else 0
                    it['reviewed_depth'] = max(it.get('reviewed_depth', 0), effective_depth)
                    it['tier1_reviewed'] = (it['reviewed_depth'] >= 1)
                    it['tier2_reviewed'] = (it['reviewed_depth'] >= 2)
                    it['status'] = 'confirmed'
                    it['need_review'] = False
                    count += 1
            self.save_state()
            return count

    def delete_items(self, filenames: List[str]) -> int:
        """Move items to Trash / To Delete."""
        with LOCK:
            count = 0
            for fn in filenames:
                if fn in self.state.get('items', {}):
                    it = self.state['items'][fn]
                    it['cluster_path'] = ['Trash / To Delete']
                    it['reviewed_depth'] = 1
                    it['tier1_cluster'] = 'Trash / To Delete'
                    it['tier2_cluster'] = None
                    it['tier1_reviewed'] = True
                    it['tier2_reviewed'] = False
                    it['status'] = 'trash'
                    count += 1
            self.save_state()
            return count

    def restore_items(self, snapshots: List[Dict[str, Any]]) -> int:
        """Restore items from action snapshots for universal undo."""
        with LOCK:
            restored = 0
            for snap in snapshots:
                fn = snap.get('filename')
                if fn and fn in self.state.get('items', {}):
                    it = self.state['items'][fn]
                    cp = snap.get('cluster_path')
                    if cp:
                        it['cluster_path'] = list(cp)
                        self.cluster_tree.add_path(cp)
                    else:
                        t1 = snap.get('tier1_cluster') or 'Review & Unassigned'
                        t2 = snap.get('tier2_cluster')
                        it['cluster_path'] = [t1, t2] if t2 else [t1]

                    it['reviewed_depth'] = snap.get('reviewed_depth', 1 if snap.get('tier1_reviewed') else 0)
                    it['tier1_cluster'] = it['cluster_path'][0] if it['cluster_path'] else 'Review & Unassigned'
                    it['tier2_cluster'] = it['cluster_path'][1] if len(it['cluster_path']) > 1 else None
                    it['tier1_reviewed'] = snap.get('tier1_reviewed', it['reviewed_depth'] >= 1)
                    it['tier2_reviewed'] = snap.get('tier2_reviewed', it['reviewed_depth'] >= 2)
                    it['status'] = snap.get('status', 'unconfirmed')
                    restored += 1
            self.save_state()
            return restored

    # --- PAYLOAD COMPUTATION ---

    def compute_payload(self) -> Dict[str, Any]:
        """Compute full frontend payload including tree, node stats, sessions, and items."""
        with LOCK:
            all_paths = self.cluster_tree.get_all_paths()
            items_list = list(self.state.get('items', {}).values())

            # 1. Compute stats for every cluster path
            # path_key -> { total: int, reviewed: int, direct: int }
            node_stats: Dict[str, Dict[str, int]] = {}
            for p in all_paths:
                key = " / ".join(p)
                node_stats[key] = {'total': 0, 'reviewed': 0, 'direct': 0}

            for it in items_list:
                cp = it.get('cluster_path', ['Review & Unassigned'])
                rev_depth = it.get('reviewed_depth', 0)

                # Direct match
                direct_key = " / ".join(cp)
                if direct_key in node_stats:
                    node_stats[direct_key]['direct'] += 1

                # Subtree matches for prefixes
                for i in range(1, len(cp) + 1):
                    prefix = cp[:i]
                    prefix_key = " / ".join(prefix)
                    if prefix_key in node_stats:
                        node_stats[prefix_key]['total'] += 1
                        if rev_depth >= i:
                            node_stats[prefix_key]['reviewed'] += 1

            # 2. Legacy tier1 and tier2 stats for compatibility
            tier1_names = self.cluster_tree.get_children_names([])
            counts: Dict[str, int] = {}
            tier1_reviewed_counts: Dict[str, int] = {}
            tier2_reviewed_counts: Dict[str, int] = {}
            for t1 in tier1_names:
                stats = node_stats.get(t1, {'total': 0, 'reviewed': 0})
                counts[t1] = stats['total']
                tier1_reviewed_counts[t1] = stats['reviewed']

                # Count items in this t1 with depth >= 2
                t2_rev = 0
                for it in items_list:
                    cp = it.get('cluster_path', [])
                    if cp and cp[0] == t1 and it.get('reviewed_depth', 0) >= 2:
                        t2_rev += 1
                tier2_reviewed_counts[t1] = t2_rev

            # 3. Build sessions
            sessions_map = {}
            for it in items_list:
                s_date = it.get('session_date') or 'Undated'
                cp = it.get('cluster_path', ['Review & Unassigned'])
                cp_str = " / ".join(cp)
                key = f"{s_date}___{cp_str}"

                if key not in sessions_map:
                    sessions_map[key] = {
                        'session_key': key,
                        'session_date': s_date,
                        'cluster_path': cp,
                        'cluster_path_str': cp_str,
                        'tier1_cluster': cp[0] if cp else 'Review & Unassigned',
                        'tier2_cluster': cp[1] if len(cp) > 1 else None,
                        'items': [],
                        'sample_description': it.get('visual_description', '')[:90]
                    }
                sessions_map[key]['items'].append(it)

            for s in sessions_map.values():
                items = s['items']
                s['tier1_reviewed'] = all(it.get('reviewed_depth', 0) >= 1 for it in items)
                s['tier2_reviewed'] = all(it.get('reviewed_depth', 0) >= 2 for it in items)
                s['fully_reviewed'] = all(it.get('reviewed_depth', 0) >= len(it.get('cluster_path', [])) for it in items)

                # Subclusters summary
                subclusters = []
                for it in items:
                    cp = it.get('cluster_path', [])
                    if len(cp) > 1:
                        sub = " / ".join(cp[1:])
                        if sub not in subclusters:
                            subclusters.append(sub)
                if len(subclusters) == 1:
                    s['tier2_cluster'] = subclusters[0]
                elif len(subclusters) > 1:
                    s['tier2_cluster'] = "Mixed (" + ", ".join(subclusters[:2]) + ")"
                else:
                    s['tier2_cluster'] = None

                # Common metadata tags
                locs = []
                settings = []
                perspectives = []
                cameras = []
                for it in items:
                    loc = (it.get('location_or_event') or '').strip()
                    if loc and loc.lower() not in ('none', 'unknown', 'null', '') and loc not in locs:
                        locs.append(loc)
                    st = (it.get('setting') or '').strip()
                    if st and st.lower() not in ('none', 'unknown', 'null', '') and st not in settings:
                        settings.append(st)
                    tp = (it.get('taker_perspective') or '').strip()
                    if tp and tp.lower() not in ('none', 'unknown', 'null', '') and tp not in perspectives:
                        perspectives.append(tp)
                    cam = (it.get('camera') or '').strip()
                    if cam and cam.lower() not in ('none', 'unknown', 'null', '', 'unknown / whatsapp', 'unknown') and cam not in cameras:
                        cameras.append(cam)

                gps_tags = []
                for it in items:
                    gps = it.get('gps')
                    if gps and isinstance(gps, dict) and gps.get('formatted'):
                        gf = str(gps['formatted']).strip()
                        if gf and gf not in gps_tags:
                            gps_tags.append(gf)

                s['locations'] = locs
                s['settings'] = settings
                s['perspectives'] = perspectives
                s['cameras'] = cameras
                s['gps_tags'] = gps_tags

            sorted_sessions = sorted(
                sessions_map.values(),
                key=lambda s: (s['session_date'] == 'Undated', s['session_date'], -len(s['items'])),
                reverse=True
            )

            total_tier1_reviewed = sum(1 for it in items_list if it.get('reviewed_depth', 0) >= 1)
            total_tier2_reviewed = sum(1 for it in items_list if it.get('reviewed_depth', 0) >= 2)
            total_fully_reviewed = sum(1 for it in items_list if it.get('reviewed_depth', 0) >= len(it.get('cluster_path', [])))

            cfg = _load_config()

            return {
                'active_folder': str(self.active_folder) if self.active_folder else '',
                'recent_libraries': cfg.get('recent_libraries', []),
                'cluster_tree': self.cluster_tree.to_dict(),
                'node_stats': node_stats,
                'tier1_clusters': self.state.get('tier1_clusters', []),
                'tier2_clusters': self.state.get('tier2_clusters', {}),
                'counts': counts,
                'tier1_reviewed_counts': tier1_reviewed_counts,
                'tier2_reviewed_counts': tier2_reviewed_counts,
                'total_tier1_reviewed': total_tier1_reviewed,
                'total_tier2_reviewed': total_tier2_reviewed,
                'total_fully_reviewed': total_fully_reviewed,
                'total_items': len(items_list),
                'items': self.state.get('items', {}),
                'sessions': sorted_sessions
            }
