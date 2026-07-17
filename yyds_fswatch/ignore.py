# -*- coding: utf-8 -*-

import re
from pathlib import Path
from typing import Union, List, Tuple
from functools import lru_cache

class GitIgnoreFilter:
    def __init__(self, root_paths: List[Path], use_gitignore: bool = True, ignore_patterns: List[str] = None):
        self._roots = [p.resolve() for p in root_paths]
        # Precompute string prefixes to avoid slow try-except relative_to checks
        self._root_strs = []
        for r in self._roots:
            r_str = str(r)
            if not r_str.endswith('/') and not r_str.endswith('\\'):
                import os
                r_str += os.sep
            self._root_strs.append((r, r_str))

        self._use_gitignore = use_gitignore
        self._fast_ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".egg-info", "dist", "build"}
        
        # Global custom rules
        self._global_rules = []
        if ignore_patterns:
            for pat in ignore_patterns:
                pat = pat.strip()
                if pat and not pat.startswith('#'):
                    self._add_rule_to_list(pat, self._global_rules)

        # Map of dir_path (Path) -> list of compiled rules from that dir's .gitignore
        self._dir_rules = {}
        # Set of dir_paths we have checked for .gitignore
        self._checked_dirs = set()

        # Bind the LRU cache to this instance to avoid memory leaks
        self.is_ignored = lru_cache(maxsize=4096)(self._is_ignored_impl)

    def close(self):
        """Clean up resources and break circular references from lru_cache."""
        if hasattr(self, 'is_ignored') and self.is_ignored is not None:
            try:
                self.is_ignored.cache_clear()
            except Exception:
                pass
            self.is_ignored = None

    def _ensure_gitignore_loaded(self, dir_path: Path):
        if not self._use_gitignore:
            return
        
        # Check if dir_path is within our roots
        resolved_dir = dir_path.absolute()
        resolved_dir_str = str(resolved_dir)
        under_root = False
        root_path = None
        for root, root_str in self._root_strs:
            if resolved_dir_str.startswith(root_str) or resolved_dir_str == root_str[:-1]:
                under_root = True
                root_path = root
                break
        
        if not under_root or root_path is None:
            return

        # Find all ancestor directories up to root_path
        ancestors = []
        curr = resolved_dir
        while True:
            ancestors.append(curr)
            if curr == root_path or curr.parent == curr:
                break
            curr = curr.parent

        # Process ancestors from top (root_path) down to dir_path
        for ancestor in reversed(ancestors):
            if ancestor not in self._checked_dirs:
                self._checked_dirs.add(ancestor)
                gitignore_file = ancestor / ".gitignore"
                if gitignore_file.is_file():
                    rules = []
                    try:
                        with open(gitignore_file, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                line = line.strip()
                                if not line or line.startswith('#'):
                                    continue
                                self._add_rule_to_list(line, rules)
                    except Exception:
                        pass
                    if rules:
                        self._dir_rules[ancestor] = rules

    def _add_rule_to_list(self, pattern: str, rule_list: list):
        is_negated = False
        if pattern.startswith('!'):
            is_negated = True
            pattern = pattern[1:]

        is_dir_only = pattern.endswith('/')
        if is_dir_only:
            pattern = pattern[:-1]

        anchored = False
        if pattern.startswith('/'):
            anchored = True
            pattern = pattern[1:]
        elif '/' in pattern:
            anchored = True

        regex_parts = []
        i = 0
        n = len(pattern)
        while i < n:
            c = pattern[i]
            if c == '*':
                if i + 1 < n and pattern[i+1] == '*':
                    regex_parts.append('.*')
                    i += 2
                else:
                    regex_parts.append('[^/]*')
                    i += 1
            elif c == '?':
                regex_parts.append('[^/]')
                i += 1
            elif c in ('.', '+', '^', '$', '(', ')', '[', ']', '{', '}', '|', '\\'):
                regex_parts.append('\\' + c)
                i += 1
            else:
                regex_parts.append(c)
                i += 1

        regex_str = "".join(regex_parts)

        # Build regex matching
        suffix = '(/.*)?$'

        if anchored:
            regex_str = '^' + regex_str + suffix
        else:
            regex_str = '(^|.*/)' + regex_str + suffix

        try:
            compiled = re.compile(regex_str)
            rule_list.append((compiled, is_dir_only, is_negated))
        except re.error:
            pass

    def _is_ignored_impl(self, path: Path, is_dir: bool = False) -> bool:
        resolved_path = path.absolute()
        resolved_path_str = str(resolved_path)
        
        # Determine the relative path to whichever root it belongs to
        root_path = None
        for root, root_str in self._root_strs:
            if resolved_path_str.startswith(root_str) or resolved_path_str == root_str[:-1]:
                root_path = root
                break

        if root_path is None:
            return True

        # Fast path check for common directories
        rel_path = resolved_path.relative_to(root_path)
        for i, part in enumerate(rel_path.parts):
            if part in self._fast_ignore_dirs:
                if i < len(rel_path.parts) - 1 or is_dir:
                    return True

        # Ensure all gitgitignore files along the path are loaded
        parent_dir = resolved_path.parent if not is_dir else resolved_path
        self._ensure_gitignore_loaded(parent_dir)

        # Apply rules in order (last matching rule wins)
        
        # 1. Global rules
        ignored = False
        rel_path_str = str(rel_path).replace('\\', '/')
        for regex, is_dir_only, is_negated in self._global_rules:
            if is_dir_only and not is_dir:
                continue
            if regex.match(rel_path_str):
                ignored = not is_negated

        # 2. Local/nested rules from root down to parent_dir
        curr = parent_dir
        applicable_dirs = []
        while True:
            if curr in self._dir_rules:
                applicable_dirs.append(curr)
            if curr == root_path or curr.parent == curr:
                break
            curr = curr.parent

        for d in reversed(applicable_dirs):
            rules = self._dir_rules[d]
            try:
                d_rel_path = resolved_path.relative_to(d)
                d_rel_path_str = str(d_rel_path).replace('\\', '/')
                if d_rel_path_str == ".":
                    continue
                for regex, is_dir_only, is_negated in rules:
                    if is_dir_only and not is_dir:
                        continue
                    if regex.match(d_rel_path_str):
                        ignored = not is_negated
            except ValueError:
                continue

        return ignored
