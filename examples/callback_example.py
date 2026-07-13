# -*- coding: utf-8 -*-
"""
Level 2: Callback One-Liner Mode Example
This script demonstrates how to monitor a directory using a single-line function callback.
"""

import os
from yyds_fswatch import watch

def handle_change(event):
    print(f"⚡ [Callback Event] {event.event_type.upper()}: {event.src_path}")

def main():
    watch_dir = "./watch_test_callback"
    os.makedirs(watch_dir, exist_ok=True)
    
    print(f"Starting callback watcher on directory: {watch_dir}")
    print("Press Ctrl+C to stop.")
    
    # Block and watch in one line
    watch(watch_dir, on_change=handle_change, recursive=True, debounce=0.05)

if __name__ == "__main__":
    main()
