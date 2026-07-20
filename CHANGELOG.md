# Changelog

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
