# -*- coding: utf-8 -*-
"""
Level 4: Async Iterator Mode Example
This script demonstrates how to monitor a directory using asyncio native context manager.
"""

import os
import asyncio
from yyds_fswatch import FSWatcher

async def do_file_operations(watch_dir):
    await asyncio.sleep(1.0)
    file_path = os.path.join(watch_dir, "async_test.txt")
    
    print("\n--- [Background Task] Performing file operations ---")
    
    print("[1] Creating file...")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("hello")
    await asyncio.sleep(0.5)

    print("[2] Modifying file...")
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(" world")
    await asyncio.sleep(0.5)

    print("[3] Renaming file...")
    new_path = os.path.join(watch_dir, "async_test_renamed.txt")
    os.rename(file_path, new_path)
    await asyncio.sleep(0.5)

    print("[4] Deleting file...")
    os.remove(new_path)
    await asyncio.sleep(0.5)
    
    print("--- [Background Task] Completed ---\n")

async def watch_loop(watch_dir):
    # Monitor with a 0.05s debounce
    async with FSWatcher(watch_dir, recursive=True, debounce=0.05) as w:
        async for event in w:
            print(f"👀 [Async Event] {event.event_type.upper()}: {event.src_path}")
            if event.event_type == "moved":
                print(f"                -> to: {event.dest_path}")

async def main():
    watch_dir = "./watch_test_async"
    os.makedirs(watch_dir, exist_ok=True)
    
    print(f"Starting async watcher on: {watch_dir}")
    
    # Run the watcher loop and background changes concurrently
    watcher_task = asyncio.create_task(watch_loop(watch_dir))
    operations_task = asyncio.create_task(do_file_operations(watch_dir))
    
    # Wait for the file operations to complete, then cancel watcher
    await operations_task
    await asyncio.sleep(0.5) # Allow final events to process
    watcher_task.cancel()
    
    # Clean up directory
    try:
        os.rmdir(watch_dir)
    except OSError:
        pass

if __name__ == "__main__":
    asyncio.run(main())
