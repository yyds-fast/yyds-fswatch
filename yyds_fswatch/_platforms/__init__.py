# -*- coding: utf-8 -*-

import sys
import warnings

def get_platform_watcher_class(backend: str = "auto"):
    if backend == "polling":
        from ._polling import PollingWatcher
        return PollingWatcher

    import_error = None
    if sys.platform.startswith("linux"):
        try:
            from ._linux import LinuxWatcher
            return LinuxWatcher
        except Exception as exc:
            import_error = exc
    elif sys.platform == "win32":
        try:
            from ._windows import WindowsWatcher
            return WindowsWatcher
        except Exception as exc:
            import_error = exc
    elif sys.platform == "darwin":
        try:
            from ._darwin import DarwinWatcher
            return DarwinWatcher
        except Exception as exc:
            import_error = exc

    if backend == "native":
        if import_error is not None:
            raise RuntimeError("native file-system backend could not be loaded") from import_error
        raise RuntimeError(f"no native file-system backend for platform {sys.platform!r}")

    if import_error is not None:
        warnings.warn(
            "Native file-system backend could not be imported; falling back "
            f"to polling: {import_error}",
            RuntimeWarning,
            stacklevel=2,
        )

    from ._polling import PollingWatcher
    return PollingWatcher
