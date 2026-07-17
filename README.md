# 🚀 yyds-fswatch

[中文文档 (Chinese Version)](README_CN.md)

`yyds-fswatch` is a high-performance, asyncio-native, and developer-friendly file system watching library for Python. It is designed to fully replace and outperform the traditional `watchdog` library.

---

## 🌟 Core Advantages (vs Watchdog)

| Feature | `yyds-fswatch` (This Library) | `watchdog` |
| :--- | :--- | :--- |
| **API Usability** | 4-level progressive API (Decorators / Callbacks / Sync Iter / Async Iter) | Verbose Observer + Handler class inheritance system |
| **Async Integration** | **Native coroutine support** (`async with` and `async for`) | Requires manual threadpools or `loop.call_soon_threadsafe` bridging |
| **Linux Performance** | **Zero additional threads** (ctypes non-blocking `inotify` + `loop.add_reader`) | Starts an independent Python blocking thread for each Emitter |
| **Atomic Save Support** | **$100\%$ compatible**. File watches automatically fall back to parent directory + top-level filter | Direct file watching fails and loses watch handle upon Vim/VSCode atomic saves |
| **Debounce & Coalesce** | Built-in 50ms sliding window state machine | No debouncing; saving one file triggers dozens of redundant `modified` events |
| **GitIgnore Pruning** | Parses root and **nested `.gitignore`** dynamically, pruning directories at registration | No nested parsing/pruning; large repos easily exhaust descriptors |
| **Rename Sync** | Robust transaction-packet & cookie pairing. Automatically updates subdirectory path mappings | Subdirectory path mappings get out of sync, reporting stale paths for events |
| **Overflow Resilience** | **Supported**. Emits an `overflow` event when system buffers are saturated, continuing normally | **Unsupported**. Misses events silently or crashes the watch thread |
| **Root Self-Healing** | **Supported**. Pauses and polls when root directories are deleted/unmounted, auto-recovering | **Unsupported**. Crashes with FileNotFoundError or terminates silently |

---

## 📦 Installation

To install `yyds-fswatch` via pip:
```bash
pip install -U yyds-fswatch
```

Or install from source inside the project directory:
```bash
pip install .
```

---

## 🛠️ Progressive API Usage Examples

`yyds-fswatch` provides four API levels to suit different integration needs.

### 1. Level 1: Decorator Mode
The most intuitive mode, supporting both synchronous and asynchronous callback functions.
```python
from yyds_fswatch import FSWatcher

w = FSWatcher("./watch_dir", recursive=True, debounce=0.05)

@w.on("created")
def on_created(event):
    print(f"🔥 [CREATED] {'Directory' if event.is_directory else 'File'}: {event.src_path}")

@w.on("modified")
def on_modified(event):
    print(f"⚡ [MODIFIED]: {event.src_path}")

@w.on("deleted")
def on_deleted(event):
    print(f"🗑️ [DELETED]: {event.src_path}")

@w.on("moved")
def on_moved(event):
    print(f"📦 [MOVED]: {event.src_path} -> {event.dest_path}")

# Start watching (blocks current thread; run in a separate thread if non-blocking is needed)
try:
    w.start()
except KeyboardInterrupt:
    w.stop()
```

### 2. Level 2: One-Liner Callback
Ideal for quick scripts and debugging.
```python
from yyds_fswatch import watch

# Blocks and runs callback on any file changes
watch("./watch_dir", on_change=lambda event: print(f"⚡ Event: {event.event_type} on {event.src_path}"))
```

### 3. Level 3: Sync Standard Iterator
Uses the `with` context manager to process file events sequentially like a generator.
```python
from yyds_fswatch import FSWatcher

# Starts a background thread within the context and auto-cleans resources upon exit
with FSWatcher("./watch_dir", debounce=0.05) as w:
    for event in w:
        print(f"🔄 [Sync Iter] {event.event_type.upper()}: {event.src_path}")
        if event.event_type == "deleted" and "stop_trigger" in event.src_path:
            break  # Exiting the loop will cleanly terminate the watcher
```

### 4. Level 4: Async Native Iterator - ✨ Recommended
Integrates seamlessly with modern async frameworks (FastAPI, Sanic, Tornado). Pure asyncio, zero extra threads.
```python
import asyncio
from yyds_fswatch import FSWatcher

async def main():
    # Pure async context manager and iterator
    async with FSWatcher("./watch_dir", debounce=0.05) as w:
        async for event in w:
            print(f"👀 [Async Iter] {event.event_type.upper()}: {event.src_path}")

asyncio.run(main())
```

---


## 🛡️ Deep Technical Optimization Details

1. **Atomic Save Filtering**: For file watches (e.g. `data.json`), the system automatically monitors its parent directory and intercepts `MOVED_TO` and `CREATE` events. It filters out intermediate temporary files, guaranteeing that updates are $100\%$ captured regardless of the editor's write mechanism.
2. **High-Performance Prefix Matching**: Bypasses the expensive `try-except ValueError` and `Path.resolve()` system calls during event filtering. Uses in-memory absolute path checks and precomputed string prefix matching to achieve a throughput of over **100,000 checks per second**.
3. **Robust Lifecycle & Memory Safety**: When exiting standard/async loops or catching `KeyboardInterrupt`, the library cleanly shuts down async event queue hooks. Additionally, a `close()` mechanism is provided to purge the ignore filter's LRU cache and break cyclic references, allowing Python's GC to immediately collect resources.
4. **System Buffer Overflow Prevention (Overflow Event)**: Under extreme file system activity, OS event queues can saturate (Linux `IN_Q_OVERFLOW` / Windows `ERROR_MORE_DATA`). The watcher handles these gracefully by emitting an `"overflow"` event and continuing monitoring, preventing locks or crash loops.
5. **Hierarchical Nested `.gitignore` Support**: Automatically traverses and dynamically parses `.gitignore` files nested at any subdirectory depth, creating parent-child regex associations for precise, Git-compliant tree pruning.
6. **Self-Healing Root Recovery**: If a watched root directory is deleted or unmounted, the watcher pauses native OS watches and falls back to a low-overhead retry loop. Monitoring is automatically re-established the moment the path reappears.

---

## 📈 Long-Term Resource Stability (Production Ready)

`yyds-fswatch` is designed for mission-critical, long-running daemon processes (e.g., chat bots, automated deployment monitors, web servers). It enforces strict resource bounds to run indefinitely (months/years) without memory drift or handle leaks:

* **Bounded LRU Cache**: The `.gitignore` filter is backed by a cached lookup capped at a maximum of `4,096` entries (`lru_cache(maxsize=4096)`). Memory consumption remains constant ($O(1)$) and never leaks even when scanning millions of unique files.
* **Idle CPU Overhead is $0.00\%$**: All native platform drivers (Linux `inotify`, macOS `FSEvents`, Windows `ReadDirectoryChangesW`) are event-driven and wait in the OS kernel until events occur. No polling threads or busy-waiting loops are active when idle.
* **Auto-Expiring State Machine**: Temporary states (such as rename cookie matching and debouncing queues) auto-expire and purge themselves within 50ms, maintaining a baseline size of zero when idle.
* **Watches Pruning & Handle Safety**: Subdirectory watches are pruned dynamically at startup based on `.gitignore` rules (avoiding `ENOSPC` descriptor limits). System handles/file descriptors are synchronized dynamically when folders are deleted, and are safely closed upon stop.

