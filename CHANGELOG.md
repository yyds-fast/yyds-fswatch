# Changelog

## 0.2.9

- Match gitignore patterns case-insensitively on case-insensitive filesystems (Windows NTFS) and correlate NTFS 8.3 short-name `FILE_ACTION_RENAMED_OLD_NAME` records (`TARGET~1.TMP` / `STAGIN~1`) back to ignored staging files so directory-mode atomic replacements reliably emit `modified` on the target path.
- Always wait on `[io_event, stop_win_event]` via `WaitForMultipleObjects` and issue `CancelIoEx` during `WindowsWatcher.stop()` (since `ReadDirectoryChangesW` returns `TRUE` even when `STATUS_PENDING`), restoring sub-millisecond Windows shutdown.

## 0.2.8

- Switch Windows `ReadDirectoryChangesW` to `OVERLAPPED` I/O and wait until the initial kernel notification buffer is armed before `start()`/`astart()` returns, eliminating missed early file events right after watcher startup.
- Filter incidental root startup events (such as macOS `FSEvents` directory notifications) in decorator-stop and synthetic debounce regression tests.

## 0.2.7

- Fix nested directory-only gitignore globs (`temp*/`), Git whitelist patterns (`["*", "!*/", "!*.py"]`), and backslash-escaped literals (`\#`, `\!`, `\ `) in `GitIgnoreFilter`.
- Avoid redundant `lru_cache` invalidation on plain directory deletions, add `is_ignored_canonical` to skip duplicate `realpath`/`lstat` syscalls, and replace `Path.relative_to()` subtree loops with normalized string-prefix checks (~15x faster).
- Reconcile `inotify` and Windows directory watches dynamically when `.gitignore` rules change at runtime, open Linux `inotify` descriptors atomically with `O_NONBLOCK | O_CLOEXEC` (`inotify_init1`), switch Linux event buffering to `bytearray` with bounded per-tick reads, and emit `overflow` when runtime `inotify_add_watch` hits `ENOSPC`/`EMFILE`.
- Fix macOS `CFArrayCreate` `kCFTypeArrayCallBacks` retention, `CFRunLoop` retain/release thread safety, and `stop()`-before-`CFRunLoopRun` shutdown races; switch Windows `ctypes.WinDLL(..., use_last_error=True)` error handling and root inode health checks; offload blocking native `astart()`/`aclose()` calls from the caller's asyncio loop.
- Include `(st_mtime_ns, st_size)` in `PollingWatcher` file move identity to prevent `ext4` immediate inode reuse on `unlink + create` from misreporting unrelated files as `moved`.
- Emit `created` events for entries created alongside newly appeared/recreated watched roots, flush pending subtree events before directory `moved`, preserve separate events on file-to-directory replacements within debounce windows, guard cross-mode re-entry, and halt iteration/callbacks immediately on `stop()`.

## 0.2.6

- Normalize plain `created` atomic-replacement events for already-observed files in directory-watch mode to `modified` (fixing macOS `FSEvents` directory atomic saves).
- Collapse intermediate Windows `FILE_ACTION_REMOVED` records when immediately followed by `RENAMED_OLD_NAME` + `RENAMED_NEW_NAME` on the same target file within one packet.
- Pin CI Python 3.9 to `ubuntu-24.04` and filter incidental root events in cross-platform symlink tests.

## 0.2.5

- Prevent `GitIgnoreFilter` global patterns such as `.*` from ignoring watched root directories, add `[...]` character-class glob support, make `ignore_patterns` take precedence over `.gitignore`, and guard filter lookups/invalidations with a reentrant lock.
- Avoid recursing into parent subdirectories for single-file and mixed file/directory watches across native and polling backends.
- Cancel pending debounce timers when events flush early, preserve symlinks inside watched roots, re-arm backpressure overflow notifications, fix `start()`/`stop()` races, and track observed files for atomic replacements in directory mode.
- Handle Linux `IN_MOVE_SELF` root recovery, rapid nested directory creation races, and ignored-to-visible directory renames.
- Eliminate artificial 100ms startup/shutdown delays and spurious directory `modified` events in the Polling backend, and pre-filter ignored events in Windows and macOS native worker threads while preserving compound `FSEvents` removals and typed rename matching.

## 0.2.4

- Run the first Polling backend reconciliation immediately, avoiding missed
  short-lived changes while a newly started Windows event loop is scheduling.

## 0.2.3

- Cancel Windows synchronous directory-change reads during shutdown so native
  watcher threads do not wait for the close timeout.

## 0.2.2

- Canonicalize event and ignore-rule paths so macOS `/var` and `/private/var`
  aliases remain in the same watch scope.
- Treat a create event for an already watched file as a modification, matching
  atomic replacement semantics on native backends.
- Make cross-platform watcher tests ignore incidental parent-directory events
  emitted by FSEvents.

## 0.2.1

- Recover Linux root watches when a directory is removed and recreated between recovery probes.
- Keep Polling startup and shutdown responsive to the caller's asyncio event loop.
- Avoid redundant same-loop event scheduling and repeated `.gitignore` rule-file stats.
- Emit Polling create/delete/modify events in a deterministic hierarchy-aware order.
- Add cross-platform CI coverage and release-package validation.

## 0.2.0

- Add public, thread-safe `stop()` and `wait_until_ready()` lifecycle APIs.
- Bound user-space event queues by default and emit `overflow` on backpressure.
- Support explicit missing-file targets and correct mixed file/directory scopes.
- Normalize atomic replacement events to the watched target path.
- Fix Windows/macOS native backend imports and the Windows event-loop spin.
- Remove stale Linux watches after directories move outside the watched root.
- Rebuild the Linux inotify tree after kernel queue overflow.
- Dynamically invalidate nested ignore rules and make common-directory ignores configurable.
- Detect moves in the Polling backend and offload periodic scans from the caller's asyncio loop.
- Add regression coverage, package checks, and release gates.
