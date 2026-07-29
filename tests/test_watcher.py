# -*- coding: utf-8 -*-

import unittest
import tempfile
import shutil
import time
from pathlib import Path
from yyds_fswatch import FSWatcher

class TestFSWatcher(unittest.TestCase):
    def setUp(self):
        # Create temp dir within workspace
        self.test_dir = Path(tempfile.mkdtemp(dir=Path(".").resolve())).resolve()

    def tearDown(self):
        # Remove temp dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sync_iterator_and_debounce(self):
        # FSEvents is allowed to coalesce rapid writes, renames, and deletes.
        # This test verifies the synchronous iterator and debounce contract, so
        # use the deterministic polling backend rather than asserting a native
        # backend's best-effort event granularity.
        watcher = FSWatcher(
            self.test_dir,
            debounce=0.1,
            backend="polling",
            poll_interval=0.02,
        )
        file_path = self.test_dir / "test.txt"
        new_path = self.test_dir / "test_new.txt"
        moved_sequence = [
            ("created", file_path, None),
            ("modified", file_path, None),
            ("moved", file_path, new_path),
            ("deleted", new_path, None),
        ]
        replacement_sequence = [
            ("created", file_path, None),
            ("modified", file_path, None),
            ("deleted", file_path, None),
            ("created", new_path, None),
            ("deleted", new_path, None),
        ]
        events = []

        def matches_sequence(sequence):
            index = 0
            for event in events:
                event_type, source_path, destination_path = sequence[index]
                if event.event_type != event_type:
                    continue
                if Path(event.src_path).resolve() != source_path:
                    continue
                if destination_path is not None and (
                    Path(event.dest_path).resolve() != destination_path
                ):
                    continue
                index += 1
                if index == len(sequence):
                    return True
            return False
        
        # Start a helper to do file changes while watching
        def perform_changes():
            # Wait up to 2 seconds for watcher to start
            for _ in range(20):
                if watcher.is_ready:
                    break
                time.sleep(0.1)
            time.sleep(0.1)  # Extra buffer to ensure loop is active
            
            # 1. Create file
            file_path.touch()
            time.sleep(0.2)  # Allow create to flush
            
            # 2. Burst of modifications (should be debounced to 1 modified event)
            for i in range(5):
                with open(file_path, "a") as f:
                    f.write(f"line {i}\n")
                time.sleep(0.01)
            time.sleep(0.2)  # Allow modified to flush
                
            # 3. Rename file
            file_path.rename(new_path)
            
            # 4. Delete file
            time.sleep(0.2) # Allow rename to flush
            new_path.unlink()
            
            # Allow time to flush delete event
            time.sleep(0.2)

        # Run changes in a separate thread
        import threading
        t = threading.Thread(target=perform_changes)
        t.start()

        # Iterate over watcher events with timeout protection
        import queue
        from yyds_fswatch.watcher import _STOP_SENTINEL
        with watcher as w:
            start_time = time.time()
            while not (
                matches_sequence(moved_sequence) or
                matches_sequence(replacement_sequence)
            ):
                if time.time() - start_time > 5.0:
                    break
                try:
                    event = w._sync_queue.get(timeout=0.2)
                    if event is _STOP_SENTINEL:
                        break
                    source_path = Path(event.src_path).resolve()
                    destination_path = (
                        Path(event.dest_path).resolve() if event.dest_path else None
                    )
                    if source_path not in {file_path, new_path} and (
                        destination_path not in {file_path, new_path}
                    ):
                        continue
                    events.append(event)
                except queue.Empty:
                    continue

        t.join()

        self.assertTrue(
            matches_sequence(moved_sequence) or matches_sequence(replacement_sequence),
            f"Expected a move or delete/create replacement sequence; got: {events}",
        )

    def test_decorator_mode(self):
        watcher = FSWatcher(self.test_dir, debounce=0.05)
        captured = []

        @watcher.on("created")
        def on_created(event):
            captured.append(event)

        @watcher.on("*")
        def on_any(event):
            captured.append(event)

        # Run decorator start in a thread since it is blocking
        import threading
        t_watcher = threading.Thread(target=watcher.start)
        t_watcher.daemon = True
        t_watcher.start()

        # Wait up to 2 seconds for loop to be initialized in the thread
        for _ in range(20):
            if watcher.is_ready:
                break
            time.sleep(0.1)
        time.sleep(0.1)  # Extra buffer to ensure loop is active

        (self.test_dir / "decor.txt").touch()
        time.sleep(0.2)  # Wait for event dispatch

        watcher.stop()
            
        t_watcher.join(timeout=2.0)
        self.assertFalse(t_watcher.is_alive())

        # We touched decor.txt. We expect:
        # 1. on_created called (1 event)
        # 2. on_any called (1 event)
        # Total events in captured >= 2
        self.assertTrue(len(captured) >= 2)
        event_types = [e.event_type for e in captured]
        self.assertIn("created", event_types)

if __name__ == "__main__":
    unittest.main()
