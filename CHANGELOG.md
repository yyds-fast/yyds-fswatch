# Changelog

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
