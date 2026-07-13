# yyds-fswatch

A high-performance, asyncio-native, and developer-friendly file watching library for Python.

## Features

- **🏆 Multi-Paradigm API**: Supports decorators, callbacks, synchronous iterators, and asynchronous iterators out of the box.
- **⚡ Asyncio Native**: Zero thread overhead on Linux (using raw non-blocking inotify + `add_reader`).
- **🛡️ Intelligent Debouncing**: Built-in 50ms sliding window debounce to prevent "event storm" during file saves.
- **🔍 Built-in .gitignore Filtering**: Automatically loads ignore rules and clips directory tree walks, preventing watch descriptor exhaustion.
- **🚀 Zero Core Dependencies**: Standard library and ctypes only.

## Installation

```bash
pip install .
```

## Quick Start

### 1. Decorator Mode (Level 1)
```python
from yyds_fswatch import FSWatcher

w = FSWatcher('.')

@w.on('modified')
def on_modified(event):
    print(f"Modified: {event.src_path}")

w.start()
```

### 2. Async Iteration (Level 4)
```python
import asyncio
from yyds_fswatch import FSWatcher

async def main():
    async with FSWatcher('.') as w:
        async for event in w:
            print(f"{event.event_type}: {event.src_path}")

asyncio.run(main())
```
