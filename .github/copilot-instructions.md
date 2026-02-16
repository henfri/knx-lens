# KNX-Lens Copilot Instructions

## Project Overview

**KNX-Lens** is a suite of Python tools for monitoring and analyzing KNX bus traffic. It consists of three main components:

1. **`knx-lens-logger.py`**: Daemon that captures live KNX telegrams, decodes them using an ETS project file, and writes rotating logs to disk with gzip compression
2. **`knx-lens.py`**: Interactive TUI (Text-User-Interface) explorer using Textual framework for browsing `.knxproj` files and filtering logs
3. **`knx-lens-web.py`**: Web server wrapper making the TUI accessible via browser (using textual-serve)

All three share common utilities and configuration via `.env` file.

## Architecture & Data Flow

### Three-Tier Data Processing Pipeline

```
KNX Bus (via XKNX lib)
    ↓
knx-lens-logger.py (async telegram capture)
    ↓ [telegram_to_log_message + DPT decoding]
    ↓
knx_bus.log (rotating, daily + ZIP compression)
    ↓
knx_log_utils.py (parse + cache)
    ↓ [detect format: pipe-separated or CSV]
    ↓
knx-lens.py TUI
    ↓ [selection filters + regex + time filters]
    ↓
DataTable display
```

### Key Components & Their Responsibilities

| File | Role |
|------|------|
| `knx-lens.py` | Main TUI app (Textual App + KNXTuiLogic mixin) - 757 lines |
| `knx_tui_logic.py` | Business logic mixin (tree building, filtering, caching) - 557 lines |
| `knx_tui_screens.py` | Modal screens/widgets (FilterInputScreen, FilteredDirectoryTree) |
| `knx_project_utils.py` | ETS `.knxproj` parsing via `xknxproject` library - MD5 caching for performance |
| `knx_log_utils.py` | Log file parsing (pipe-separated + CSV), payload history tracking |
| `knx-lens-logger.py` | Async gateway monitoring (XKNX), rotating log handler with ZIP rotation |
| `setup.py` | Interactive configuration wizard (Textual TUI, gateway auto-discovery) |
| `knx-lens-web.py` | Web server entry point (textual-serve) |

### Project Data Structures

**Loaded from `.knxproj`** (via `xknxproject.XKNXProj`):
- `devices_dict`: Physical address → device info
- `ga_dict`: Group address (GA) → semantic info
- `building_structure`: Hierarchical spaces (floors, rooms, functions)

**In-memory caches** (`knx_tui_logic.py`):
- `cached_log_data`: List[Dict] up to MAX_CACHE_SIZE (50,000 lines)
- `payload_history`: Dict[GA → List[{timestamp, value, previous}]] for "live values in tree" feature
- `project_data["project"]`: Unwrapped `.knxproj` parse result (wrapper has MD5 for cache invalidation)

## Critical Development Workflows

### 1. Setup & Configuration

```bash
python setup.py        # Interactive Textual TUI to configure gateway + paths → .env file
```

Required `.env` variables:
```
KNX_GATEWAY_IP=192.168.x.x     # or "AUTO" to scan on startup
KNX_GATEWAY_PORT=3671
KNX_PROJECT_PATH=/path/to/project.knxproj
LOG_PATH=/path/to/logs/dir
KNX_LOG_FILE=/path/to/logs/knx_bus.log
KNX_PASSWORD=optional_proj_password
```

**Windows quirk**: Use forward slashes in paths to avoid escaping issues.

### 2. Running the Logger

```bash
python knx-lens-logger.py      # Starts async event loop, connects to gateway, logs telegrams
```

- Uses `xknx.XKNX` for KNX/IP communication
- Implements `ZipTimedRotatingFileHandler` for daily rotation → `.zip` compression
- Decodes telegrams to human-readable format using DPT types from project
- Logs format: `timestamp | source_PA | sink_PA | dest_GA | payload_description | raw_payload`

### 3. Running the TUI Explorer

```bash
python knx-lens.py             # Interactive explorer with mouse/keyboard support
python knx-lens.py -v          # Verbose logging to console + knx_debug.log
python knx-lens.py /path/to/log.zip  # Load specific ZIP log file
```

- **Tabs**: "Building" (spaces), "Physical Addresses" (lines), "Group Addresses" (functions), "Selection Groups", "Log View", "Files"
- **Tree selection ('a')**: Toggle nodes → update `selected_gas` set → filter log to matching GAs
- **Selection Groups ('s')**: Save selected GAs + regex rules to `named_filters.yaml` for reuse
- **Global AND filter**: Regex in "Log View" tab applied in addition to selection group filters

### 4. Running the Web Interface

```bash
python knx-lens-web.py         # Starts textual-serve on configured IP:PORT (default 8000)
```

- Auto-detects local IP (with fallback to 127.0.0.1)
- Can be overridden via `WEBSERVER_IP` + `WEBSERVER_PORT` in `.env`
- Multi-user capable; browser tab runs remote Textual session

### 5. Docker Deployment

```bash
docker compose up               # Starts logger + web containers
```

- Logger writes logs to `${DOCKERCONFIG}/knx-lens/logs` (mounted host path)
- Web container mounts same logs as read-only
- `.env` file shared between both services

## Project-Specific Patterns & Conventions

### Log Format Detection (knx_log_utils.py)

```python
def detect_log_format(first_lines: List[str]) -> Optional[str]:
    # Returns "pipe_separated" or "csv" based on heuristics
```

Both formats supported:
- **Pipe-separated** (default from logger): `timestamp | source_PA | dest_PA | group_addr | decode | raw`
- **CSV** (legacy): semicolon-delimited, column indices differ

### Tree Data Structure

All three trees (`building_tree_data`, `pa_tree_data`, `ga_tree_data`) follow pattern:

```python
TreeData = Dict[str, Any]
{
    "children": {...},        # Nested dict for subtree
    "com_objects": [...],     # Group addresses at this node
    "last_value": "...",      # Latest payload for live display
    "node_id": "unique_key"   # Stable identifier
}
```

Built by functions in `knx_project_utils.py`:
- `build_ga_tree_data()`: Hierarchical GA structure
- `build_pa_tree_data()`: Line/device topology
- `build_building_tree_data()`: Spaces (floors → rooms → functions)

### Project Caching Strategy

```python
# knx_project_utils.py
cache_path = knxproj_path + ".cache.json"
cache_data = {
    "md5": get_md5_hash(knxproj_path),
    "project": parsed_dict
}
```

Cache invalidated if file MD5 changes. Critical for performance: full parse takes seconds on large projects.

### Selection & Filtering

**In `knx_tui_logic.py`:**
- `selected_gas: Set[str]` - GAs selected via tree nodes
- `regex_filter: Optional[re.Pattern]` - User-entered regex from "Filter Tree" action
- `active_named_filters: Set[str]` - Enabled selection groups from YAML
- `active_named_regex_rules: List[re.Pattern]` - Regex rules from selection groups

**Filtering precedence** (in `_filter_log_data()`):
1. If any selection group enabled → must match ≥1 group's GAs or regex rules (OR logic)
2. If regex_filter set → all results must also match (AND logic)
3. Time filter applied after GA filtering

### Named Filters File Format

`named_filters.yaml`:
```yaml
My Selection Group:
  gas: ["1/1/1", "1/1/2"]
  rules:
    - rule_name: "Floor 1"
      pattern: ".*Floor 1.*"  # Matches GA text or device names
```

Loaded/saved by `_load_filters()` and `_save_filter_to_yaml()` in `knx_tui_logic.py`.

## Common Pitfalls & Important Notes

1. **Windows path handling**: Always use forward slashes in `.env` paths. Backslashes cause escaping issues with ZIP detection logic.

2. **MD5 cache invalidation**: If `.knxproj` structure changes but file size/mtime don't, the cache won't update. MD5 is used to avoid this.

3. **Project wrapper unpacking**: Some functions expect `project_data["project"]` (unwrapped), others expect full wrapper. Check function signatures in `knx_log_utils.py` line ~25.

4. **Textual event binding**: Key actions defined in `BINDINGS` list in `knx-lens.py`. Custom modal screens dismiss with `self.dismiss(result)`, triggering app's result callback.

5. **Log rotation timing**: `ZipTimedRotatingFileHandler` rotates at **midnight UTC**, not local time. Set container `TZ` env var if needed.

6. **Gateway scanner limitations**: Linux only (via XKNX). Windows/macOS require manual gateway IP entry. Setup falls back gracefully.

7. **Remaining CO ordering**: In `build_pa_tree_data()` and `build_building_tree_data()`, remaining communication objects (not in channels) must be sorted by CO number **before** passing to `add_com_objects_to_node()`. Converting `set` to `list` without sorting causes unpredictable display order. Always sort by CO number: `sorted(rem_ids, key=lambda x: com_objects_dict.get(x, {}).get('number', 0))`.

## Testing & Debugging

- **Debug log**: `knx_app_debug.log` in log directory (only logger creates this)
- **Verbose TUI**: `python knx-lens.py -v` enables DEBUG logging to console + file
- **Log format errors**: Check `knx_log_utils.detect_log_format()` if parsing fails
- **Project parse errors**: Verify `.knxproj` path + password in `.env`; check `.cache.json` for stale cache

## Key Dependencies

- `xknx` (KNX/IP communication, gateway scanning)
- `xknxproject` (parse `.knxproj` files)
- `textual` (TUI framework)
- `textual-serve` (web wrapper)
- `python-dotenv` (config management)
- `PyYAML` (named filters persistence)
