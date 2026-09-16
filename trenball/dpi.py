"""Windows DPI awareness so Tk selection coords match mss pixels."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

_ENABLED = False


def enable_dpi_awareness() -> None:
    """Call once before creating any Tk window."""
    global _ENABLED
    if _ENABLED or not sys.platform.startswith("win"):
        return
    try:
        # Per-monitor DPI aware (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _ENABLED = True
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        _ENABLED = True
    except Exception:
        pass


def virtual_desktop() -> dict[str, int]:
    """
    Full virtual desktop spanning all monitors (mss monitors[0]).
    Coordinates are absolute and may start at negative left/top.
    """
    import mss

    with mss.MSS() as sct:
        m = sct.monitors[0]
        return {
            "left": int(m["left"]),
            "top": int(m["top"]),
            "width": int(m["width"]),
            "height": int(m["height"]),
        }


def list_monitors() -> list[dict[str, Any]]:
    """Physical monitors only (excludes the virtual all-screens entry)."""
    import mss

    with mss.MSS() as sct:
        out = []
        for index, m in enumerate(sct.monitors[1:], start=1):
            out.append(
                {
                    "index": index,
                    "left": int(m["left"]),
                    "top": int(m["top"]),
                    "width": int(m["width"]),
                    "height": int(m["height"]),
                    "is_primary": bool(m.get("is_primary", index == 1)),
                    "name": str(m.get("name", f"Monitor {index}")),
                }
            )
        return out


def monitor_label_for_point(x: int, y: int) -> str:
    for m in list_monitors():
        if (
            m["left"] <= x < m["left"] + m["width"]
            and m["top"] <= y < m["top"] + m["height"]
        ):
            kind = "primary" if m["is_primary"] else "secondary"
            return f"Monitor {m['index']} ({kind})"
    return "Unknown monitor"


def primary_scale_vs_mss(tk_screen_width: int) -> float:
    """
    Scale using the PRIMARY monitor only — never virtual desktop width.
    (Virtual/primary mix was doubling coords on dual-screen setups.)
    """
    if tk_screen_width <= 0:
        return 1.0
    try:
        import mss

        with mss.MSS() as sct:
            primary = next(
                (m for m in sct.monitors[1:] if m.get("is_primary")),
                sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0],
            )
            phys_w = int(primary["width"])
        if phys_w <= 0:
            return 1.0
        scale = phys_w / float(tk_screen_width)
        if 0.9 <= scale <= 1.1:
            return 1.0
        return scale
    except Exception:
        return 1.0


# Back-compat alias (old name was misleading on multi-monitor)
def screen_scale_vs_mss(tk_screen_width: int) -> float:
    return primary_scale_vs_mss(tk_screen_width)
