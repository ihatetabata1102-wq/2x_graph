"""Windows keep-awake + workstation lock detection for screen capture."""

from __future__ import annotations

import ctypes
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
DESKTOP_SWITCHDESKTOP = 0x0100


def is_windows() -> bool:
    return sys.platform.startswith("win")


def prevent_sleep(enabled: bool) -> None:
    """
    While tracking, ask Windows to keep the system and display awake.
    Does NOT block manual Win+L lock — capture still cannot work while locked.
    """
    if not is_windows():
        return
    kernel32 = ctypes.windll.kernel32
    if enabled:
        kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    else:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def is_workstation_locked() -> bool:
    """
    True when the Windows session is on the lock / secure desktop.
    Screen capture of the user desktop is not available in that state.
    """
    if not is_windows():
        return False
    user32 = ctypes.windll.user32
    desktop = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if desktop:
        user32.CloseDesktop(desktop)
        return False
    return True


def looks_like_blank_capture(image_bgr) -> bool:
    """
    Locked / black captures are nearly all dark. Used as a fallback signal
    when lock detection is inconclusive.
    """
    try:
        import numpy as np
    except ImportError:
        return False
    if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
        return True
    # Mean luminance across BGR
    mean = float(np.mean(image_bgr))
    return mean < 8.0
