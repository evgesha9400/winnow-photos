"""
studio/scanner.py - Universal Directory & Media Scanner for Winnow Photos.
Scans any folder on the machine, extracts directory hierarchy into Level 1 (Tier 1)
and Level 2 (Tier 2) clusters, extracts EXIF dates/cameras/GPS, and merges with existing state.
"""

import os
import re
import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

try:
    from PIL import Image, ExifTags
    Image.MAX_IMAGE_PIXELS = None
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from studio.cluster_tree import ClusterTree

logger = logging.getLogger("winnow_photos.scanner")

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.webp', '.bmp', '.tiff', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}
ALL_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _convert_gps_coord(coord, ref) -> Optional[float]:
    """Convert EXIF GPS coordinates (degrees, minutes, seconds) to decimal degrees."""
    try:
        def to_float(val):
            if isinstance(val, (int, float)):
                return float(val)
            if hasattr(val, 'numerator') and hasattr(val, 'denominator'):
                return float(val.numerator) / float(val.denominator) if val.denominator else 0.0
            if isinstance(val, tuple) and len(val) == 2:
                return float(val[0]) / float(val[1]) if val[1] else 0.0
            return float(val)

        d = to_float(coord[0])
        m = to_float(coord[1])
        s = to_float(coord[2])
        dec = d + (m / 60.0) + (s / 3600.0)
        if ref in ['S', 'W']:
            dec = -dec
        return round(dec, 6)
    except Exception:
        return None


def parse_date_from_filename(filename: str) -> Optional[str]:
    """Parse date from common camera, smartphone, and messaging filename conventions."""
    # Pattern: IMG-20170909-WA0011 or VID-20200501-WA...
    m = re.search(r'(?:IMG|VID)[-_](\d{4})(\d{2})(\d{2})', filename, re.IGNORECASE)
    if m:
        return f"{m.group(1)}:{m.group(2)}:{m.group(3)}"

    # Pattern: 20211005_150821 or 2021-10-05 or 2021.10.05
    m = re.search(r'(?:^|[^\d])(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})', filename)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"{year}:{month}:{day}"

    # Pattern: PXL_20220604_...
    m = re.search(r'PXL_(\d{4})(\d{2})(\d{2})', filename, re.IGNORECASE)
    if m:
        return f"{m.group(1)}:{m.group(2)}:{m.group(3)}"

    return None


def extract_media_metadata(file_path: Path) -> Dict[str, Any]:
    """
    Extract date, camera make/model, GPS coordinates, and media type.
    Uses PIL for fast in-process extraction with exiftool/mtime fallback.
    """
    ext = file_path.suffix.lower()
    is_video = ext in VIDEO_EXTENSIONS
    meta: Dict[str, Any] = {
        'date': None,
        'session_date': None,
        'camera': 'Unknown',
        'gps': None,
        'is_video': is_video
    }

    # 1. Try PIL for images
    if not is_video and HAS_PIL:
        try:
            with Image.open(file_path) as img:
                exif_data = img._getexif() or {}
                named_exif = {}
                for k, v in exif_data.items():
                    tag = ExifTags.TAGS.get(k, k)
                    named_exif[tag] = v

                # Date extraction
                date_str = named_exif.get('DateTimeOriginal') or named_exif.get('DateTimeDigitized') or named_exif.get('DateTime')
                if date_str and isinstance(date_str, str):
                    date_parts = date_str.split(' ')
                    if date_parts:
                        meta['date'] = date_parts[0]
                        meta['session_date'] = date_parts[0]

                # Camera extraction
                make = str(named_exif.get('Make') or '').strip()
                model = str(named_exif.get('Model') or '').strip()
                if model:
                    if make and make.lower() not in model.lower():
                        meta['camera'] = f"{make} {model}".strip()
                    else:
                        meta['camera'] = model
                elif make:
                    meta['camera'] = make

                # GPS extraction
                gps_info = named_exif.get('GPSInfo')
                if gps_info and isinstance(gps_info, dict):
                    lat = gps_info.get(2)
                    lat_ref = gps_info.get(1)
                    lon = gps_info.get(4)
                    lon_ref = gps_info.get(3)
                    if lat and lat_ref and lon and lon_ref:
                        dec_lat = _convert_gps_coord(lat, lat_ref)
                        dec_lon = _convert_gps_coord(lon, lon_ref)
                        if dec_lat is not None and dec_lon is not None:
                            meta['gps'] = {
                                'latitude': dec_lat,
                                'longitude': dec_lon,
                                'formatted': f"{abs(dec_lat):.3f}° {'N' if dec_lat >= 0 else 'S'}, {abs(dec_lon):.3f}° {'E' if dec_lon >= 0 else 'W'}"
                            }
        except Exception as e:
            logger.debug(f"PIL extraction error on {file_path.name}: {e}")

    # 2. Filename heuristic fallback
    if meta['date'] is None:
        fn_date = parse_date_from_filename(file_path.name)
        if fn_date:
            meta['date'] = fn_date
            meta['session_date'] = fn_date

    # 3. Exiftool fallback for videos or missing dates
    if (meta['date'] is None or is_video) and shutil.which('exiftool'):
        try:
            res = subprocess.run(
                ['exiftool', '-json', '-CreateDate', '-DateTimeOriginal', '-Make', '-Model', '-GPSLatitude', '-GPSLongitude', str(file_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3
            )
            if res.returncode == 0 and res.stdout:
                parsed = json.loads(res.stdout)
                if parsed and isinstance(parsed, list):
                    item = parsed[0]
                    c_date = item.get('CreateDate') or item.get('DateTimeOriginal')
                    if c_date and isinstance(c_date, str):
                        parts = c_date.split(' ')
                        if parts:
                            meta['date'] = parts[0]
                            meta['session_date'] = parts[0]

                    if meta['camera'] == 'Unknown':
                        make = item.get('Make', '')
                        model = item.get('Model', '')
                        if model:
                            meta['camera'] = f"{make} {model}".strip() if make and make.lower() not in model.lower() else model

                    if not meta['gps']:
                        lat = item.get('GPSLatitude')
                        lon = item.get('GPSLongitude')
                        if lat is not None and lon is not None:
                            try:
                                meta['gps'] = {
                                    'latitude': float(lat),
                                    'longitude': float(lon),
                                    'formatted': f"{float(lat):.3f}, {float(lon):.3f}"
                                }
                            except Exception:
                                pass
        except Exception as e:
            logger.debug(f"Exiftool fallback error on {file_path.name}: {e}")

    # 4. Filesystem mtime fallback
    if meta['date'] is None:
        try:
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            dt_str = mtime.strftime('%Y:%m:%d')
            meta['date'] = dt_str
            meta['session_date'] = dt_str
        except Exception:
            meta['date'] = 'Undated'
            meta['session_date'] = 'Undated'

    if not meta['camera'] or meta['camera'] == 'Unknown':
        if 'WA' in file_path.name:
            meta['camera'] = 'Unknown / WhatsApp'
        else:
            meta['camera'] = 'Unknown'

    return meta


def scan_directory(
    target_dir: str | Path,
    existing_state: Optional[Dict[str, Any]] = None,
    rescan: bool = False
) -> Dict[str, Any]:
    """
    Recursively scans target_dir, parses directory hierarchy into Level 1 and Level 2 clusters,
    extracts metadata, and merges with existing state preserving prior triage decisions.
    """
    root_path = Path(target_dir).resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"Path does not exist or is not a directory: {root_path}")

    # Initialize cluster tree
    if existing_state and 'cluster_tree' in existing_state:
        tree = ClusterTree.from_dict(existing_state['cluster_tree'])
    elif existing_state and 'tier1_clusters' in existing_state:
        tree = ClusterTree.from_legacy_tiers(
            existing_state.get('tier1_clusters', []),
            existing_state.get('tier2_clusters', {})
        )
    else:
        tree = ClusterTree()

    discovered_tier1: set[str] = set(tree.get_children_names([]))
    discovered_tier2: Dict[str, set[str]] = {t1: set(tree.get_children_names([t1])) for t1 in discovered_tier1}

    # Always ensure standard system clusters exist
    discovered_tier1.add("Review & Unassigned")
    discovered_tier1.add("Trash / To Delete")

    items: Dict[str, Dict[str, Any]] = {}
    found_filenames: set[str] = set()
    prev_items = existing_state.get('items', {}) if existing_state else {}

    for entry in root_path.rglob('*'):
        if entry.is_file() and entry.suffix.lower() in ALL_MEDIA_EXTENSIONS:
            filename = entry.name
            if filename.startswith('.'):
                continue

            found_filenames.add(filename)
            rel_path = entry.relative_to(root_path)
            raw_folder_parts = rel_path.parts[:-1]

            # 1. Clean folder parts and handle system mappings
            clean_parts: List[str] = []
            for idx, p in enumerate(raw_folder_parts):
                clean_p = p.strip()
                if idx == 0 and clean_p.lower() in ('trash', 'to delete', 'deleted', 'garbage'):
                    clean_p = "Trash / To Delete"
                if clean_p:
                    clean_parts.append(clean_p)

            # Determine cluster path and review depth
            if not clean_parts:
                cluster_path = ["Review & Unassigned"]
                reviewed_depth = 0
                default_t1_reviewed = False
                default_t2_reviewed = False
            else:
                cluster_path = clean_parts
                reviewed_depth = len(cluster_path)
                default_t1_reviewed = (cluster_path[0] != "Review & Unassigned")
                default_t2_reviewed = (len(cluster_path) >= 2)

            detected_t1 = cluster_path[0] if cluster_path else "Review & Unassigned"
            detected_t2 = cluster_path[1] if len(cluster_path) > 1 else None

            # Register in tree
            tree.add_path(cluster_path)
            discovered_tier1.add(detected_t1)
            if detected_t1 not in discovered_tier2:
                discovered_tier2[detected_t1] = set()
            if detected_t2:
                discovered_tier2[detected_t1].add(detected_t2)

            # 2. Check if item was already triaged in existing state
            prev = prev_items.get(filename)
            if prev and not rescan:
                # Retain human curation decisions
                item_dict = dict(prev)
                item_dict['disk_path'] = str(entry)
                if 'cluster_path' not in item_dict or not item_dict['cluster_path']:
                    t1 = item_dict.get('tier1_cluster') or detected_t1
                    t2 = item_dict.get('tier2_cluster') or detected_t2
                    item_dict['cluster_path'] = [t1, t2] if t2 else [t1]
                if 'reviewed_depth' not in item_dict:
                    if item_dict.get('tier2_reviewed'):
                        item_dict['reviewed_depth'] = 2
                    elif item_dict.get('tier1_reviewed'):
                        item_dict['reviewed_depth'] = 1
                    else:
                        item_dict['reviewed_depth'] = 0

                item_dict['tier1_cluster'] = item_dict['cluster_path'][0] if item_dict['cluster_path'] else detected_t1
                item_dict['tier2_cluster'] = item_dict['cluster_path'][1] if len(item_dict['cluster_path']) > 1 else None
                tree.add_path(item_dict['cluster_path'])
                items[filename] = item_dict
            else:
                # New item or full rescan: extract metadata
                meta = extract_media_metadata(entry)
                loc_or_event = " / ".join(cluster_path[1:]) if len(cluster_path) > 1 else (cluster_path[0] if cluster_path[0] != 'Review & Unassigned' else None)
                item_dict = {
                    'filename': filename,
                    'disk_path': str(entry),
                    'date': meta['date'],
                    'session_date': meta['session_date'],
                    'camera': meta['camera'],
                    'gps': meta['gps'],
                    'taker_perspective': 'unknown',
                    'setting': None,
                    'location_or_event': loc_or_event,
                    'visual_description': f"Photo from {' / '.join(cluster_path)}",
                    'cluster_path': cluster_path,
                    'reviewed_depth': reviewed_depth,
                    'tier1_cluster': detected_t1,
                    'tier2_cluster': detected_t2,
                    'is_video': meta['is_video'],
                    'status': 'confirmed' if default_t1_reviewed else 'unassigned',
                    'tier1_reviewed': default_t1_reviewed,
                    'tier2_reviewed': default_t2_reviewed
                }
                # If prev existed, preserve manual overrides
                if prev:
                    for field in ['taker_perspective', 'setting', 'location_or_event', 'visual_description', 'cluster_path', 'reviewed_depth', 'tier1_cluster', 'tier2_cluster', 'tier1_reviewed', 'tier2_reviewed', 'status', 'need_review']:
                        if field in prev:
                            item_dict[field] = prev[field]
                    if prev.get('camera') and prev['camera'] != 'Unknown' and item_dict['camera'] == 'Unknown':
                        item_dict['camera'] = prev['camera']
                    if prev.get('date') and prev['date'] != 'Undated' and item_dict['date'] == 'Undated':
                        item_dict['date'] = prev['date']
                        item_dict['session_date'] = prev.get('session_date', prev['date'])
                    if prev.get('gps') and not item_dict.get('gps'):
                        item_dict['gps'] = prev['gps']
                tree.add_path(item_dict['cluster_path'])
                items[filename] = item_dict

    # Convert sets to sorted lists for legacy JSON compatibility
    sorted_tier1 = sorted(
        list(discovered_tier1),
        key=lambda c: (c == 'Review & Unassigned', c == 'Trash / To Delete', c)
    )
    sorted_tier2: Dict[str, List[str]] = {}
    for c in sorted_tier1:
        sub_set = discovered_tier2.get(c, set())
        sorted_tier2[c] = sorted(list(sub_set))

    state = {
        'root_dir': str(root_path),
        'cluster_tree': tree.to_dict(),
        'tier1_clusters': sorted_tier1,
        'tier2_clusters': sorted_tier2,
        'items': items,
        'last_scanned': datetime.now().isoformat()
    }
    return state
