# -*- coding: utf-8 -*-
"""
Level 1: Decorator Mode Example
This script demonstrates how to monitor a directory using decorators.
"""

import os
import time
from yyds_fswatch import FSWatcher

def main():
    watch_dir = "./watch_test"
    os.makedirs(watch_dir, exist_ok=True)
    
    print(f"Starting watcher on directory: {watch_dir}")
    print("Create/Modify/Delete files in this directory to see events.")
    
    w = FSWatcher(watch_dir, recursive=True, debounce=0.05)

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

    @w.on("*")
    def on_any_event(event):
        # This will be called for any event type
        pass

    try:
        w.start()
    except KeyboardInterrupt:
        print("\nStopping watcher...")

if __name__ == "__main__":
    main()
