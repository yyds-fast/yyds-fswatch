# 🚀 yyds-fswatch

[中文文档 (Chinese Version)](https://github.com/yyds/yyds-fswatch/blob/main/README_CN.md)

`yyds-fswatch` is a lightweight, asyncio-friendly, and developer-friendly file system watching library for Python. It offers a compact alternative to observer/handler-style APIs while keeping platform fallbacks explicit and observable.

---

## 🌟 Core Advantages (vs Watchdog)

| Feature | `yyds-fswatch` (This Library) | `watchdog` |
| :--- | :--- | :--- |
| **API Usability** | 4-level progressive API (Decorators / Callbacks / Sync Iter / Async Iter) | Verbose Observer + Handler class inheritance system |
| **Async Integration** | **Native coroutine support** (`async with` and `async for`) | Requires manual threadpools or `loop.call_soon_threadsafe` bridging |
| **Linux Performance** | **Zero additional threads** (ctypes non-blocking `inotify` + `loop.add_reader`) | Starts an independent Python blocking thread for each Emitter |
| **Atomic Save Support** | File watches monitor the parent directory and normalize inbound replacement moves to target-file events | Behavior depends on the observer and handler configuration |
| **Debounce & Coalesce** | Built-in 50ms sliding window state machine | No debouncing; saving one file triggers dozens of redundant `modified` events |
| **GitIgnore Pruning** | Dynamically reloads nested `.gitignore` files and supports the commonly used glob subset | Requires application-level filtering |
| **Rename Sync** | Uses Linux cookies, Windows packets, FSEvents heuristics, and inode pairing in Polling | Backend-dependent |
| **Overflow Resilience** | Emits `overflow`; Linux rebuilds its recursive watch tree after kernel overflow | Backend-dependent |
| **Root Self-Healing** | Restarts the backend when watched roots disappear or reappear | Usually requires observer recreation |

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
# Blocks until Ctrl+C or until another thread calls w.stop().
w.start()
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
Integrates with modern async frameworks (FastAPI, Sanic, Tornado). Linux uses the caller's asyncio loop directly; Windows/macOS native backends use an OS-waiting helper thread.
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

### Lifecycle, backpressure, and backend controls

```python
import threading
from yyds_fswatch import FSWatcher

w = FSWatcher(
    "./watch_dir",
    max_queue_size=4096,   # 0 opts into an unbounded queue
    backend="auto",       # auto | native | polling
    recovery_interval=2.0,
)
thread = threading.Thread(target=w.start)
thread.start()
w.wait_until_ready(timeout=5)

# ...later
w.stop()                  # public, idempotent, and thread-safe
thread.join()
```

Use `target_type="file"` when a watched file does not exist yet. Set `ignore_common_dirs=False` when changes inside `dist`, `build`, `node_modules`, and other common generated directories must be reported.

---


## 🛡️ Deep Technical Optimization Details

1. **Atomic Save Normalization**: For single-file watches (e.g. `data.json`), the system monitors the parent directory. A temporary file moved onto an existing target is emitted as `modified` on the target path.
2. **Cached Path Filtering**: Absolute-root prefix checks and a bounded 4,096-entry decision cache reduce repeated filtering work. No unverified throughput number is claimed; benchmark on the intended directory tree and platform.
3. **Lifecycle & Memory Controls**: Context managers and the public `stop()` method release backend resources. User-space queues are bounded to 4,096 events by default and emit `overflow` when consumers fall behind.
4. **System Buffer Overflow Handling**: OS queue saturation emits an `overflow` event. Linux also recreates its inotify instance and recursive watch tree so missed directory registrations do not remain permanent.
5. **Hierarchical Nested `.gitignore` Support**: Nested files are invalidated when they change. The built-in parser supports common `*`, `?`, `**`, anchoring, directory, and negation patterns; it is intentionally documented as a practical subset rather than byte-for-byte Git equivalence.
6. **Self-Healing Root Recovery**: If a watched root directory is deleted or unmounted, the watcher pauses native OS watches and falls back to a low-overhead retry loop. Monitoring is automatically re-established the moment the path reappears.

---

## 📈 Resource and Long-Running Process Controls

`yyds-fswatch` includes controls useful for long-running processes. Production adoption should still be validated on every target OS and file-system type:

* **Bounded queues and cache**: Event queues and the `.gitignore` decision cache default to `4,096` entries. Backend descriptor maps naturally scale with the number of watched directories.
* **Low idle overhead**: Native drivers wait on OS facilities. A lightweight lifecycle heartbeat supports prompt cross-thread shutdown, a configurable root-health probe runs every two seconds by default, and the Polling backend scans at its configured interval.
* **Auto-Expiring State Machine**: Temporary states (such as rename cookie matching and debouncing queues) auto-expire and purge themselves within 50ms, maintaining a baseline size of zero when idle.
* **Watches Pruning & Handle Safety**: Subdirectory watches are pruned dynamically at startup based on `.gitignore` rules (avoiding `ENOSPC` descriptor limits). System handles/file descriptors are synchronized dynamically when folders are deleted, and are safely closed upon stop.
