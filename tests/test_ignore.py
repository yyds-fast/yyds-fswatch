# -*- coding: utf-8 -*-

import os
import threading
import unittest
import tempfile
from pathlib import Path
from yyds_fswatch.ignore import GitIgnoreFilter

class TestGitIgnoreFilter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(".").resolve()

    def test_basic_matching(self):
        # We simulate GitIgnoreFilter with root self.temp_dir
        # We pass patterns directly
        filt = GitIgnoreFilter(
            root_paths=[self.temp_dir],
            use_gitignore=False,
            ignore_patterns=[
                "*.log",
                "build/",
                "/dist",
                "!important.log"
            ]
        )

        # 1. *.log file anywhere should be ignored
        self.assertTrue(filt.is_ignored(self.temp_dir / "error.log"))
        self.assertTrue(filt.is_ignored(self.temp_dir / "src" / "debug.log"))

        # 2. important.log should NOT be ignored (negation)
        self.assertFalse(filt.is_ignored(self.temp_dir / "important.log"))
        self.assertFalse(filt.is_ignored(self.temp_dir / "src" / "important.log"))

        # 3. build/ directory only should be ignored
        self.assertTrue(filt.is_ignored(self.temp_dir / "build", is_dir=True))
        self.assertTrue(filt.is_ignored(self.temp_dir / "src" / "build", is_dir=True))
        
        # 4. build file should NOT be ignored because build/ is directory-only
        self.assertFalse(filt.is_ignored(self.temp_dir / "build", is_dir=False))
        self.assertTrue(filt.is_ignored(self.temp_dir / "build" / "artifact.bin", is_dir=False))

        # 5. /dist should only be ignored at root
        self.assertTrue(filt.is_ignored(self.temp_dir / "dist"))
        self.assertFalse(filt.is_ignored(self.temp_dir / "src" / "dist"))

    def test_fast_ignore_dirs(self):
        filt = GitIgnoreFilter(root_paths=[self.temp_dir], use_gitignore=False)
        self.assertTrue(filt.is_ignored(self.temp_dir / "node_modules" / "express" / "index.js"))
        self.assertTrue(filt.is_ignored(self.temp_dir / "src" / ".git" / "config"))
        self.assertTrue(filt.is_ignored(self.temp_dir / "__pycache__" / "watcher.cpython-310.pyc"))

    def test_lru_cache(self):
        filt = GitIgnoreFilter(root_paths=[self.temp_dir], use_gitignore=False, ignore_patterns=["*.tmp"])
        path = self.temp_dir / "test.tmp"
        
        # First call, uncached
        self.assertTrue(filt.is_ignored(path))
        # Second call, cached
        self.assertTrue(filt.is_ignored(path))
        
        # Verify hit info exists in cache
        info = filt.is_ignored.cache_info()
        self.assertEqual(info.hits, 1)
        self.assertEqual(info.misses, 1)

    def test_checked_gitignore_ancestors_are_not_restatted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            filt = GitIgnoreFilter(
                root_paths=[root],
                use_gitignore=True,
                ignore_common_dirs=False,
            )
            original_stat = Path.stat
            stat_calls = []

            def counted_stat(path, *args, **kwargs):
                if path.name == ".gitignore":
                    stat_calls.append(path)
                return original_stat(path, *args, **kwargs)

            Path.stat = counted_stat
            try:
                self.assertFalse(filt.is_ignored(root / "nested" / "first.txt"))
                first_call_count = len(stat_calls)
                self.assertFalse(filt.is_ignored(root / "nested" / "second.txt"))
            finally:
                Path.stat = original_stat
                filt.close()

            self.assertGreater(first_call_count, 0)
            self.assertEqual(len(stat_calls), first_call_count)

    def test_dynamic_gitignore_invalidation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            filt = GitIgnoreFilter(
                root_paths=[root],
                use_gitignore=True,
                ignore_common_dirs=False,
            )
            target = root / "dynamic.log"
            self.assertFalse(filt.is_ignored(target))

            gitignore = root / ".gitignore"
            gitignore.write_text("*.log\n", encoding="utf-8")
            filt.notify_event("created", gitignore, False)
            self.assertTrue(filt.is_ignored(target))

            gitignore.write_text("*.tmp\n", encoding="utf-8")
            filt.notify_event("modified", gitignore, False)
            self.assertFalse(filt.is_ignored(target))
            filt.close()

    def test_common_directory_filter_can_be_disabled(self):
        enabled = GitIgnoreFilter([self.temp_dir], use_gitignore=False)
        disabled = GitIgnoreFilter(
            [self.temp_dir],
            use_gitignore=False,
            ignore_common_dirs=False,
        )
        artifact = self.temp_dir / "dist" / "artifact.whl"

        self.assertTrue(enabled.is_ignored(artifact))
        self.assertFalse(disabled.is_ignored(artifact))

    def test_root_directory_not_ignored_by_dot_star_pattern(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            filt = GitIgnoreFilter(
                root_paths=[root],
                use_gitignore=False,
                ignore_patterns=[".*", "*"],
                ignore_common_dirs=False,
            )
            try:
                self.assertFalse(filt.is_ignored(root, is_dir=True))
                self.assertTrue(filt.is_ignored(root / ".hidden", is_dir=False))
                self.assertTrue(filt.is_ignored(root / "normal.txt", is_dir=False))
            finally:
                filt.close()

    def test_character_class_glob_patterns(self):
        filt = GitIgnoreFilter(
            root_paths=[self.temp_dir],
            use_gitignore=False,
            ignore_patterns=["*.py[cod]", "[!a-z]*.txt"],
            ignore_common_dirs=False,
        )
        try:
            self.assertTrue(filt.is_ignored(self.temp_dir / "mod.pyc"))
            self.assertTrue(filt.is_ignored(self.temp_dir / "pkg" / "mod.pyo"))
            self.assertTrue(filt.is_ignored(self.temp_dir / "mod.pyd"))
            self.assertFalse(filt.is_ignored(self.temp_dir / "mod.py"))
            self.assertTrue(filt.is_ignored(self.temp_dir / "1note.txt"))
            self.assertFalse(filt.is_ignored(self.temp_dir / "anote.txt"))
        finally:
            filt.close()

    def test_explicit_ignore_patterns_override_gitignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            filt = GitIgnoreFilter(
                root_paths=[root],
                use_gitignore=True,
                ignore_patterns=["!important.log"],
                ignore_common_dirs=False,
            )
            try:
                self.assertTrue(filt.is_ignored(root / "other.log"))
                self.assertFalse(filt.is_ignored(root / "important.log"))
            finally:
                filt.close()

    def test_symlink_inside_root_keeps_root_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir).resolve()
            root = base / "root"
            outside = base / "outside.txt"
            root.mkdir()
            outside.write_text("x", encoding="utf-8")
            link = root / "link.txt"
            ignored_link = root / "ignored.log"
            try:
                os.symlink(outside, link)
                os.symlink(outside, ignored_link)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            filt = GitIgnoreFilter(
                root_paths=[root],
                use_gitignore=False,
                ignore_patterns=["*.log"],
                ignore_common_dirs=False,
            )
            try:
                self.assertFalse(filt.is_ignored(link, is_dir=False))
                self.assertTrue(filt.is_ignored(ignored_link, is_dir=False))
            finally:
                filt.close()

    def test_concurrent_lookup_and_invalidation_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            gitignore = root / ".gitignore"
            gitignore.write_text("*.log\n", encoding="utf-8")
            filt = GitIgnoreFilter(
                root_paths=[root],
                use_gitignore=True,
                ignore_common_dirs=False,
            )
            errors = []
            stop = threading.Event()

            def reader():
                try:
                    i = 0
                    while not stop.is_set():
                        filt.is_ignored(root / f"file_{i % 32}.log")
                        filt.is_ignored(root / f"file_{i % 32}.txt")
                        i += 1
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                for i in range(100):
                    gitignore.write_text("*.tmp\n" if i % 2 else "*.log\n", encoding="utf-8")
                    filt.notify_event("modified", gitignore, False)
                    if i % 10 == 0:
                        filt.refresh()
            finally:
                stop.set()
                thread.join(timeout=2.0)
                filt.close()

            self.assertEqual(errors, [])

if __name__ == "__main__":
    unittest.main()
