#!/usr/bin/env python3
"""
main.py - Entry point for Winnow Photos.
Run with any folder on your machine:
    winnow-photos                     # curates current directory
    winnow-photos /path/to/photos
    winnow-photos ~/Pictures --port 8766
    winnow-photos --scan
"""

import sys
import argparse
from pathlib import Path

# Ensure repository root is on sys.path even when executed via symlink
REPO_DIR = Path(__file__).resolve().parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from studio.server import run_server


def main():
    parser = argparse.ArgumentParser(
        description="Winnow Photos - Fast Keyboard-Driven Media Curation and Library Manager"
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Path to photo/media folder to curate (default: current directory)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Port to run the web studio server on (default: 8766)."
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Force a clean rescan and re-extract folder hierarchy and EXIF."
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the web studio interface in your default browser."
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Start with a flat library where all photos are in Review & Unassigned with no imported folder clusters."
    )

    args = parser.parse_args()

    target_folder = str(Path(args.folder).expanduser().resolve())
    if not Path(target_folder).exists():
        print(f"Error: Directory '{target_folder}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not Path(target_folder).is_dir():
        print(f"Error: Path '{target_folder}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    run_server(port=args.port, initial_folder=target_folder, force_rescan=args.scan, open_browser=args.open, flat=args.flat)


if __name__ == "__main__":
    main()
