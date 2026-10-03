"""
studio/scanner.py - Universal Directory & Media Scanner for Photo Curation Studio.
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
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = logging.getLogger("curation_studio.scanner")

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

    # 2. Exiftool fallback for videos or missing dates
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

    # 3. Filename heuristic fallback
    if meta['date'] is None:
        fn_date = parse_date_from_filename(file_path.name)
        if fn_date:
            meta['date'] = fn_date
            meta['session_date'] = fn_date

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

    # Index existing items by filename to preserve human review decisions
    prev_items = existing_state.get('items', {}) if existing_state else {}
    prev_tier1 = set(existing_state.get('tier1_clusters', [])) if existing_state else set()
    prev_tier2 = existing_state.get('tier2_clusters', {}) if existing_state else {}

    discovered_tier1: set[str] = set(prev_tier1)
    discovered_tier2: Dict[str, set[str]] = {k: set(v) for k, v in prev_tier2.items()}

    # Always ensure standard system clusters exist
    discovered_tier1.add("Review & Unassigned")
    discovered_tier1.add("Trash / To Delete")

    items: Dict[str, Dict[str, Any]] = {}
    found_filenames: set[str] = set()

    for entry in root_path.rglob('*'):
        if entry.is_file() and entry.suffix.lower() in ALL_MEDIA_EXTENSIONS:
            filename = entry.name
            if filename.startswith('.'):
                continue

            found_filenames.add(filename)
            rel_path = entry.relative_to(root_path)
            folder_parts = rel_path.parts[:-1]

            # 1. Determine Level 1 (Tier 1) and Level 2 (Tier 2) from directory depth
            if len(folder_parts) == 0:
                # File is in the root directory
                detected_t1 = "Review & Unassigned"
                detected_t2 = None
                default_t1_reviewed = False
                default_t2_reviewed = False
            elif len(folder_parts) == 1:
                # File is in a Level 1 folder (e.g., "Travel & Holidays/photo.jpg")
                p0 = folder_parts[0].strip()
                if p0.lower() in ('trash', 'to delete', 'deleted', 'garbage'):
                    detected_t1 = "Trash / To Delete"
                else:
                    detected_t1 = p0
                detected_t2 = None
                default_t1_reviewed = (detected_t1 != "Review & Unassigned")
                default_t2_reviewed = False
            else:
                # File is in a Level 2 folder (e.g., "Travel & Holidays/Costa Brava/photo.jpg")
                p0 = folder_parts[0].strip()
                p1 = folder_parts[1].strip()
                if p0.lower() in ('trash', 'to delete', 'deleted', 'garbage'):
                    detected_t1 = "Trash / To Delete"
                    detected_t2 = None
                else:
                    detected_t1 = p0
                    detected_t2 = p1
                default_t1_reviewed = True
                default_t2_reviewed = bool(detected_t2)

            # Register discovered clusters
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
                # Ensure essential fields
                if 'tier1_cluster' not in item_dict or not item_dict['tier1_cluster']:
                    item_dict['tier1_cluster'] = detected_t1
                if 'tier2_cluster' not in item_dict:
                    item_dict['tier2_cluster'] = detected_t2
                items[filename] = item_dict
            else:
                # New item or full rescan: extract metadata
                meta = extract_media_metadata(entry)
                item_dict = {
                    'filename': filename,
                    'disk_path': str(entry),
                    'date': meta['date'],
                    'session_date': meta['session_date'],
                    'camera': meta['camera'],
                    'gps': meta['gps'],
                    'taker_perspective': 'unknown',
                    'setting': None,
                    'location_or_event': detected_t2 if detected_t2 else (detected_t1 if detected_t1 != 'Review & Unassigned' else None),
                    'visual_description': f"Photo from {detected_t1}" + (f" - {detected_t2}" if detected_t2 else ""),
                    'tier1_cluster': detected_t1,
                    'tier2_cluster': detected_t2,
                    'is_video': meta['is_video'],
                    'status': 'confirmed' if default_t1_reviewed else 'unassigned',
                    'tier1_reviewed': default_t1_reviewed,
                    'tier2_reviewed': default_t2_reviewed
                }
                # If prev existed, preserve manual overrides
                if prev:
                    for field in ['taker_perspective', 'setting', 'location_or_event', 'visual_description', 'tier1_cluster', 'tier2_cluster', 'tier1_reviewed', 'tier2_reviewed', 'status']:
                        if field in prev:
                            item_dict[field] = prev[field]
                items[filename] = item_dict

    # Convert sets to sorted lists for JSON serialization
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
        'tier1_clusters': sorted_tier1,
        'tier2_clusters': sorted_tier2,
        'items': items,
        'last_scanned': datetime.now().isoformat()
    }
    return state
