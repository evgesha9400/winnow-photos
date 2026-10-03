# Winnow Photos - Architecture and Governance

## Purpose

Winnow Photos is a high-speed, keyboard-driven, two-tier media categorization and library curation system.
It runs locally on any folder on the machine with zero external service dependencies.

## Key Capabilities

1. **Two-Tier Organization:**
   - **Pass 1 (Top-Level Clusters):** High-level categories (e.g. Travel & Holidays, Outings & Events, Portraits).
   - **Pass 2 (Subclusters):** Granular subcategories within each parent cluster.
2. **Directory & Level Extraction:**
   - Scans any local directory recursively.
   - Extracts Level 1 folders into Top-Level Clusters.
   - Extracts Level 2 folders into Subclusters.
   - Root media files are placed into `Review & Unassigned`.
3. **Session & Burst Detection:**
   - Groups photos by date and cluster into cohesive session cards.
   - Computes shared metadata tags (locations, settings, cameras, perspectives) to assist naming.
4. **Three Work Views:**
   - **Sessions View:** Batch-curate clusters of related photos simultaneously.
   - **Grid View:** High-density visual inspection across all filtered photos.
   - **Focus Mode:** Single-photo deep review with rapid keyboard shortcuts (`1-9` to assign, `Space` to confirm, `J`/`K` or `ArrowRight`/`ArrowLeft` to navigate).
5. **Universal Undo:**
   - `Cmd+Z` / `Ctrl+Z` reverses any single or batch action.
6. **Folder Switcher & Ingest:**
   - Select any folder on the PC directly via CLI or web UI.
   - Remembers recent libraries in `~/.winnow_photos/config.json`.
7. **Physical Disk Synchronization:**
   - Optionally moves organized files into `<root>/<Tier1>/<Tier2>/<file>` with full JSON rollback manifests.

## Repository Layout

```
winnow-photos/
├── main.py                     # CLI entrypoint (winnow-photos [folder] [--port 8766])
├── studio/
│   ├── __init__.py             # Package marker
│   ├── cluster_tree.py         # N-level recursive cluster tree model
│   ├── scanner.py              # Recursive media scanner, EXIF parser, directory hierarchy extractor
│   ├── state.py                # LibraryManager, state persistence, undo stack, payload computation
│   ├── server.py               # HTTP server, REST API routes, /thumb/ thumbnail pipeline
│   └── static/
│       └── index.html          # Single-page studio interface
├── tests/
│   ├── test_cli.py             # CLI entrypoint tests
│   ├── test_cluster_tree.py    # Cluster tree model tests
│   ├── test_scanner.py         # Hierarchy and EXIF extraction tests
│   ├── test_server.py          # API endpoints, security, and thumbnail tests
│   └── test_state.py           # State management and atomic write tests
├── .gitignore
├── AGENTS.md
└── README.md
```

## State Invariants

- **State File:** Stored at `<target_directory>/.curation_state.json`.
- **Zero Data Loss:** Scanning never deletes media files. Rescanning merges with previous state to preserve human review decisions.
- **Trash Quarantine:** Deleted files are routed to `Trash / To Delete` virtually. Physical deletion moves files to `~/.Trash` only when explicitly applied by the user.
