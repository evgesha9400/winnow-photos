# Winnow Photos

Fast, keyboard-driven media curation and burst triage for local photo archives.
Designed for rapid sorting, burst deduplication, and hierarchical classification with zero cloud lock-in.

---

## Quick Start

### 1. Launch from Any Directory

```bash
# Curate the current working directory
winnow-photos

# Curate any specific folder
winnow-photos /path/to/my/photos

# Automatically open in your default browser
winnow-photos --open

# Run on a custom port
winnow-photos ~/Pictures --port 8766

# Force a clean rescan
winnow-photos ~/Pictures --scan
```

Open your browser to `http://127.0.0.1:8766`.

---

## Key Features

### Recursive Folder and Level Extraction
When opened on any folder:
- **Root Files:** Placed in `Review & Unassigned` for inbox triage.
- **Arbitrary Depth Subfolders:** Extracted directly into an N-level recursive cluster tree (`ClusterTree`).
- **EXIF Extraction:** Automatically detects capture dates, camera models, and GPS coordinates.
- **Fast Thumbnails:** High-performance `/thumb/<filename>` route generates and caches 400x400 JPEG thumbnails on disk to keep scrolling responsive.

### Arbitrary Depth Cluster Hierarchy
- Organize photos into any number of nested subclusters (`Root > Vacation > 2024 > Italy > Rome`).
- Move, rename, or delete clusters with recursive cascading updates across items and disk directories.
- Reparenting cycle checks prevent nodes from moving into their own subtrees.
- Sibling collision prevention rejects duplicate folder names.
- Atomic state persistence guarantees zero data corruption on write interruptions.

### Three Work Views
- **Sessions View:** Group photos taken on the same date or burst session. View shared metadata tags (`Location`, `Setting`, `Perspective`, `Camera`) and triage entire bursts in one click.
- **Grid View:** High-density gallery view. Use Shift-Select to select ranges of photos for bulk reassignment.
- **Focus Mode:** Single-photo deep review with rapid keyboard shortcuts.

### Keyboard Shortcuts (Focus Mode)
| Key | Action |
|---|---|
| `1` to `9` | Assign directly to visible cluster or subcluster `1` through `9` |
| `C` or `Space` | Confirm current cluster assignment and advance to next photo |
| `→` or `J` | Next photo |
| `←` or `K` | Previous photo |
| `D` or `Backspace` | Move to Trash |
| `A` | Request Gemini AI cluster suggestions |
| `Cmd+Z` / `Ctrl+Z` | Universal Undo |
| `S`, `G`, `F` | Switch view mode (Sessions, Grid, Focus) |

All keyboard hotkeys are guarded: typing inside text inputs, search boxes, or modals never triggers shortcuts.

---

## Project Structure

```
winnow-photos/
├── main.py                     # CLI entrypoint (winnow-photos)
├── studio/
│   ├── cluster_tree.py         # N-level recursive ClusterTree and ClusterNode model
│   ├── scanner.py              # Directory hierarchy & EXIF parser
│   ├── state.py                # LibraryManager & atomic state persistence
│   ├── server.py               # HTTP server, REST API, & /thumb/ pipeline
│   └── static/
│       └── index.html          # Single-page web studio interface
└── tests/
    ├── test_cli.py             # CLI entrypoint and argument tests
    ├── test_cluster_tree.py    # Recursive tree, cycle, and rename tests
    ├── test_scanner.py         # Directory scan and rescan preservation tests
    ├── test_server.py          # API endpoints, security, and thumbnail tests
    └── test_state.py           # Atomic JSON and state management tests
```

---

## Testing

Run unit tests with standard Python `unittest`:

```bash
python3 -m unittest discover tests -v
```
