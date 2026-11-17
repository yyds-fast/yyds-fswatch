# -*- coding: utf-8 -*-

import os
import re
import stat
import threading
from functools import lru_cache
from pathlib import Path
from typing import List


def canonicalize_watch_path(path: Path, roots_set) -> Path:
    """Resolve parent directories without dereferencing in-scope symlinks."""
    path = Path(path)
    try:
        if path.is_symlink():
            try:
                resolved = path.resolve()
                if resolved in roots_set:
                    return resolved
            except (OSError, RuntimeError):
                pass
            return path.parent.resolve() / path.name
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.normpath(os.path.abspath(str(path))))


class GitIgnoreFilter:
    """A bounded, dynamically invalidated gitignore-style filter.

    The parser intentionally implements the commonly used gitignore glob
    subset. It is not intended to replace Git's parser for every escaping edge
    case, but watcher events keep nested rule files and cached decisions fresh.
    """

    def __init__(self, root_paths: List[Path], use_gitignore: bool = True,
                 ignore_patterns: List[str] = None,
                 ignore_common_dirs: bool = True):
        self._lock = threading.RLock()
        self._roots = [p.resolve() for p in root_paths]
        self._roots_set = frozenset(self._roots)
        self._root_strs = []
        for root in self._roots:
            root_str = os.path.normcase(str(root))
            if not root_str.endswith(os.sep):
                root_str += os.sep
            self._root_strs.append((root, root_str))
        # Prefer the most specific root when roots overlap.
        self._root_strs.sort(key=lambda item: len(item[1]), reverse=True)

        self._use_gitignore = use_gitignore
        self._ignore_common_dirs = ignore_common_dirs
        self._fast_ignore_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            ".egg-info", "dist", "build",
        }

        self._global_rules = []
        if ignore_patterns:
            for pattern in ignore_patterns:
                pattern = pattern.strip()
                if pattern and not pattern.startswith("#"):
                    self._add_rule_to_list(pattern, self._global_rules)

        self._dir_rules = {}
        self._checked_dirs = set()

        # Binding a bounded cache per instance keeps repeated hot-path checks
        # fast. Acquiring self._lock around the cache wrapper prevents a stale
        # lookup from populating the cache concurrently with notify_event/refresh.
        # close() clears the cache and drops the closure to break reference cycles.
        cached_lookup = lru_cache(maxsize=4096)(self._is_ignored_impl)
        lock = self._lock

        def is_ignored(path: Path, is_dir: bool = False) -> bool:
            with lock:
                return cached_lookup(path, is_dir)

        is_ignored.cache_info = cached_lookup.cache_info
        is_ignored.cache_clear = cached_lookup.cache_clear
        self.is_ignored = is_ignored

    def _canonicalize_path(self, path: Path) -> Path:
        return canonicalize_watch_path(path, self._roots_set)

    def close(self):
        """Clear caches and break the bound-method reference cycle."""
        with self._lock:
            if getattr(self, "is_ignored", None) is not None:
                self.is_ignored.cache_clear()
                self.is_ignored = None
            self._dir_rules.clear()
            self._checked_dirs.clear()

    def refresh(self):
        """Forget loaded rule files and cached decisions, reloading lazily."""
        with self._lock:
            self._dir_rules.clear()
            self._checked_dirs.clear()
            if self.is_ignored is not None:
                self.is_ignored.cache_clear()

    def notify_event(self, event_type: str, path: Path, is_dir: bool,
                     dest_path: Path = None):
        """Invalidate rule state affected by a file-system event."""
        with self._lock:
            changed = False
            candidates = [Path(path)]
            if dest_path is not None:
                candidates.append(Path(dest_path))

            for candidate in candidates:
                if candidate.name == ".gitignore":
                    self._invalidate_directory(candidate.parent)
                    changed = True

            if is_dir and event_type in {"deleted", "moved"}:
                self._purge_subtree(Path(path))
                if dest_path is not None:
                    self._purge_subtree(Path(dest_path))
                changed = True

            if changed and self.is_ignored is not None:
                self.is_ignored.cache_clear()

    def _invalidate_directory(self, directory: Path):
        directory = directory.resolve()
        self._checked_dirs.discard(directory)
        self._dir_rules.pop(directory, None)

    def _purge_subtree(self, path: Path):
        path = path.resolve()
        directories = set(self._checked_dirs) | set(self._dir_rules)
        for directory in directories:
            try:
                directory.relative_to(path)
            except ValueError:
                continue
            self._checked_dirs.discard(directory)
            self._dir_rules.pop(directory, None)

    def _find_root(self, path: Path):
        path_str = os.path.normcase(str(path))
        for root, root_str in self._root_strs:
            if path_str.startswith(root_str) or path_str == root_str[:-1]:
                return root
        return None

    def _ensure_gitignore_loaded(self, dir_path: Path, root_path: Path = None):
        if not self._use_gitignore:
            return

        resolved_dir = dir_path if root_path is not None else dir_path.resolve()
        if root_path is None:
            root_path = self._find_root(resolved_dir)
            if root_path is None:
                return

        ancestors = []
        current = resolved_dir
        while True:
            ancestors.append(current)
            if current == root_path or current.parent == current:
                break
            current = current.parent

        for ancestor in reversed(ancestors):
            if ancestor in self._checked_dirs:
                # File-system events explicitly invalidate changed rule files.
                # Avoid a stat of every ancestor for every distinct path in a
                # large tree; that otherwise turns filtering into O(depth)
                # extra filesystem calls per entry.
                continue

            gitignore_file = ancestor / ".gitignore"
            try:
                gitignore_stat = gitignore_file.stat()
            except OSError:
                gitignore_stat = None

            self._checked_dirs.add(ancestor)
            rules = []
            if gitignore_stat is not None and stat.S_ISREG(gitignore_stat.st_mode):
                try:
                    with open(gitignore_file, "r", encoding="utf-8", errors="ignore") as file:
                        for line in file:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            self._add_rule_to_list(line, rules)
                except OSError:
                    rules = []

            if rules:
                self._dir_rules[ancestor] = rules
            else:
                self._dir_rules.pop(ancestor, None)

    def _add_rule_to_list(self, pattern: str, rule_list: list):
        is_negated = pattern.startswith("!")
        if is_negated:
            pattern = pattern[1:]

        is_dir_only = pattern.endswith("/")
        if is_dir_only:
            pattern = pattern[:-1]

        anchored = pattern.startswith("/") or "/" in pattern
        if pattern.startswith("/"):
            pattern = pattern[1:]
        if not pattern:
            return

        regex_parts = []
        index = 0
        while index < len(pattern):
            char = pattern[index]
            if char == "*":
                if index + 1 < len(pattern) and pattern[index + 1] == "*":
                    if index + 2 < len(pattern) and pattern[index + 2] == "/":
                        # **/ also matches zero directories.
                        regex_parts.append("(?:.*/)?")
                        index += 3
                    else:
                        regex_parts.append(".*")
                        index += 2
                else:
                    regex_parts.append("[^/]*")
                    index += 1
            elif char == "?":
                regex_parts.append("[^/]")
                index += 1
            elif char == "[":
                search_start = index + 1
                if search_start < len(pattern) and pattern[search_start] in ("!", "^"):
                    search_start += 1
                if search_start < len(pattern) and pattern[search_start] == "]":
                    search_start += 1
                close_index = pattern.find("]", search_start)
                if close_index != -1 and "/" not in pattern[index + 1 : close_index]:
                    inner = pattern[index + 1 : close_index]
                    if inner.startswith("!"):
                        inner = "^" + inner[1:]
                    regex_parts.append(f"(?!/)[{inner}]")
                    index = close_index + 1
                else:
                    regex_parts.append(re.escape(char))
                    index += 1
            else:
                regex_parts.append(re.escape(char))
                index += 1

        regex_body = "".join(regex_parts)
        suffix = "(?P<descendant>/.*)?$"
        if anchored:
            regex_text = "^" + regex_body + suffix
        else:
            regex_text = "(^|.*/)" + regex_body + suffix

        try:
            rule_list.append((re.compile(regex_text), is_dir_only, is_negated))
        except re.error:
            return

    @staticmethod
    def _rule_matches(regex, is_dir_only: bool, path: str, is_dir: bool) -> bool:
        match = regex.match(path)
        if match is None:
            return False
        if is_dir_only and not is_dir and not match.group("descendant"):
            # A directory-only rule ignores descendants, but not a regular
            # file that merely has the same basename as the directory rule.
            return False
        return True

    def _is_ignored_impl(self, path: Path, is_dir: bool = False) -> bool:
        with self._lock:
            # Roots are resolved at construction time. Canonicalize parent
            # directories so macOS aliases such as /var and /private/var remain
            # in the same namespace while preserving symlinks inside the root.
            resolved_path = self._canonicalize_path(path)
            root_path = self._find_root(resolved_path)
            if root_path is None:
                return True

            rel_path = resolved_path.relative_to(root_path)
            if not rel_path.parts:
                # A watched root directory itself must never be ignored.
                return False

            if self._ignore_common_dirs:
                for index, part in enumerate(rel_path.parts):
                    if part in self._fast_ignore_dirs or part.endswith(".egg-info"):
                        if index < len(rel_path.parts) - 1 or is_dir:
                            return True

            parent_dir = resolved_path if is_dir else resolved_path.parent
            self._ensure_gitignore_loaded(parent_dir, root_path=root_path)

            ignored = False
            current = parent_dir
            applicable_dirs = []
            while True:
                if current in self._dir_rules:
                    applicable_dirs.append(current)
                if current == root_path or current.parent == current:
                    break
                current = current.parent

            for directory in reversed(applicable_dirs):
                try:
                    relative = resolved_path.relative_to(directory)
                except ValueError:
                    continue
                relative_str = str(relative).replace("\\", "/")
                if relative_str == ".":
                    continue
                for regex, is_dir_only, is_negated in self._dir_rules[directory]:
                    if self._rule_matches(regex, is_dir_only, relative_str, is_dir):
                        ignored = not is_negated

            rel_path_str = str(rel_path).replace("\\", "/")
            for regex, is_dir_only, is_negated in self._global_rules:
                if self._rule_matches(regex, is_dir_only, rel_path_str, is_dir):
                    ignored = not is_negated

            return ignored
