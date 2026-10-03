# Photo Curation Studio

Two-tier universal media curation studio and library manager.
Designed for rapid sorting, burst deduplication, and hierarchical classification of local photo and video archives.

---

## Quick Start

### 1. Launch with Any Folder

```bash
# Run against any folder on your computer
python3 main.py /path/to/my/photos

# Or run with custom port
python3 main.py ~/Pictures --port 8766

# Or launch studio and choose a folder from the web UI
python3 main.py
```

Open your browser to `http://localhost:8766`.

---

## Key Features

### 📁 Automatic Folder & Level Extraction
When opened on any folder:
- **Depth 0 (Root files):** Placed in `Review & Unassigned` for inbox triage.
- **Depth 1 (`Folder/file`):** Extracted as **Level 1 (Top-Level Cluster)**.
- **Depth 2 (`Folder/Subfolder/file`):** Extracted as **Level 2 (Subcluster)**.
- **EXIF Extraction:** Automatically detects capture dates, camera models, and GPS coordinates.

### ⚡ Two-Tier Curation Workflow
1. **Pass 1 (Top-Level Clusters):** High-level categorization (e.g. `Travel & Holidays`, `Outings, Dates & Events`, `Glamour`, `Lingerie & Boudoir`).
2. **Pass 2 (Subclusters):** Deep categorization within each parent cluster (e.g. `Travel & Holidays` -> `Costa Brava`, `Edinburgh`, `Dubai`).

### 🎯 Three Work Views
- **Sessions View:** Group photos taken on the same date/session. See shared metadata tags (`📍 Location`, `🏷️ Setting`, `👁️ Perspective`, `📷 Camera`) and sort entire bursts in one click.
- **Grid View:** High-density gallery view. Use Shift-Select to select contiguous ranges of photos for bulk reassignment.
- **Focus Mode:** Single-photo deep review with rapid keyboard shortcuts.

### ⌨️ Keyboard Shortcuts (Focus Mode)
| Key | Action |
|---|---|
| `1` - `9` | Assign directly to cluster / subcluster `1` through `9` |
| `Space` | Confirm current cluster assignment and advance |
| `→` or `J` | Next photo |
| `←` or `K` | Previous photo |
| `D` or `Backspace` | Move to Trash |
| `Cmd+Z` / `Ctrl+Z` | Universal Undo |
| `F` | Toggle Focus Mode |

---

## Project Structure

```
photo-curation-studio/
├── main.py                     # CLI entrypoint
├── studio/
│   ├── scanner.py              # Directory hierarchy & EXIF parser
│   ├── state.py                # LibraryManager & state persistence
│   ├── server.py               # HTTP server & REST API
│   └── static/
│       └── index.html          # Single-page web studio interface
└── tests/
    └── test_scanner.py         # Unit tests
```

---

## Testing

Run unit tests with standard Python `unittest`:

```bash
python3 -m unittest tests/test_scanner.py
```
