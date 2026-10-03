#!/usr/bin/env python3
"""
main.py - Entry point for Photo Curation Studio.
Run with any folder on your machine:
    python3 main.py /path/to/photos
    python3 main.py ~/Pictures --port 8766
    python3 main.py --scan /path/to/photos
"""

import sys
import argparse
from pathlib import Path
from studio.server import run_server


def main():
    parser = argparse.ArgumentParser(
        description="Photo Curation Studio - Universal Media Curation and Library Manager"
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="Path to photo/media folder on your computer to curate."
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

    args = parser.parse_args()

    target_folder = None
    if args.folder:
        target_folder = str(Path(args.folder).expanduser().resolve())
        if not Path(target_folder).exists():
            print(f"Error: Directory '{target_folder}' does not exist.", file=sys.stderr)
            sys.exit(1)

    run_server(port=args.port, initial_folder=target_folder, force_rescan=args.scan)


if __name__ == "__main__":
    main()
