# -*- coding: utf-8 -*-
"""
Level 3: Sync Iterator Mode Example
This script demonstrates how to monitor a directory using synchronous standard iterators.
"""

import os
import time
import threading
from yyds_fswatch import FSWatcher

def do_file_changes(watch_dir):
    time.sleep(1.0)
    file_path = os.path.join(watch_dir, "sync_test.txt")
    
    print("\n--- Performing file changes ---")
    print("[1] Creating file...")
    with open(file_path, "w") as f:
        f.write("test")
    time.sleep(0.5)

    print("[2] Deleting file...")
    os.remove(file_path)
    time.sleep(0.5)
    print("--- Completed file changes ---\n")

def main():
    watch_dir = "./watch_test_sync"
    os.makedirs(watch_dir, exist_ok=True)
    
    # Run changes in a helper thread
    t = threading.Thread(target=do_file_changes, args=(watch_dir,))
    t.start()
    
    print(f"Starting sync iterator watcher on: {watch_dir}")
    
    # Iterate synchronously over events
    with FSWatcher(watch_dir, debounce=0.05) as w:
        for event in w:
            print(f"🔄 [Sync Iter Event] {event.event_type.upper()}: {event.src_path}")
            # Exit loop once file deletion is caught
            if event.event_type == "deleted" and "sync_test.txt" in event.src_path:
                break
                
    t.join()
    try:
        os.rmdir(watch_dir)
    except OSError:
        pass
    print("Watcher finished.")

if __name__ == "__main__":
    main()
