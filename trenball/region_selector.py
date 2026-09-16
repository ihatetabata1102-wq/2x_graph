"""Fullscreen drag-to-select screen region (multi-monitor + DPI-safe)."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass

from .dpi import monitor_label_for_point, primary_scale_vs_mss, virtual_desktop


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int

    def as_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> Region | None:
        if not data:
            return None
        return cls(
            left=int(data["left"]),
            top=int(data["top"]),
            width=int(data["width"]),
            height=int(data["height"]),
        )

    def scaled(self, scale: float) -> Region:
        if abs(scale - 1.0) < 1e-6:
            return self
        return Region(
            left=int(round(self.left * scale)),
            top=int(round(self.top * scale)),
            width=max(1, int(round(self.width * scale))),
            height=max(1, int(round(self.height * scale))),
        )

    def monitor_hint(self) -> str:
        return monitor_label_for_point(self.left + self.width // 2, self.top + self.height // 2)


def select_region(parent: tk.Misc) -> Region | None:
    """
    Dim overlay across the FULL virtual desktop (all monitors).
    Drag a rectangle on whichever screen shows the trenball strip.
    """
    desk = virtual_desktop()
    # Geometry that spans every monitor (not -fullscreen, which is one display only)
    geom = f"{desk['width']}x{desk['height']}+{desk['left']}+{desk['top']}"

    overlay = tk.Toplevel(parent)
    overlay.overrideredirect(True)
    overlay.geometry(geom)
    overlay.attributes("-alpha", 0.35)
    overlay.attributes("-topmost", True)
    overlay.configure(bg="black")
    overlay.focus_force()
    overlay.update_idletasks()

    canvas = tk.Canvas(overlay, cursor="cross", bg="black", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    state: dict = {
        "x0": 0,
        "y0": 0,
        "cx0": 0,
        "cy0": 0,
        "rect": None,
        "result": None,
    }

    def on_press(event: tk.Event) -> None:
        # Absolute virtual-desktop coords (works for left OR right monitor)
        state["x0"], state["y0"] = int(event.x_root), int(event.y_root)
        state["cx0"], state["cy0"] = int(event.x), int(event.y)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00ff88", width=2
        )

    def on_drag(event: tk.Event) -> None:
        if state["rect"] is None:
            return
        canvas.coords(state["rect"], state["cx0"], state["cy0"], event.x, event.y)

    def on_release(event: tk.Event) -> None:
        x0, y0 = state["x0"], state["y0"]
        x1, y1 = int(event.x_root), int(event.y_root)
        left, top = min(x0, x1), min(y0, y1)
        width, height = abs(x1 - x0), abs(y1 - y0)
        if width >= 20 and height >= 10:
            # Scale only vs primary size mismatch — never virtual/primary mix.
            scale = primary_scale_vs_mss(parent.winfo_screenwidth())
            region = Region(left, top, width, height).scaled(scale)
            state["result"] = region
        overlay.destroy()

    def on_escape(_event: tk.Event) -> None:
        state["result"] = None
        overlay.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_escape)
    canvas.bind("<Escape>", on_escape)

    hint = tk.Label(
        overlay,
        text="Multi-monitor: drag on the screen that shows the trenball  ·  Esc to cancel",
        fg="white",
        bg="black",
        font=("Segoe UI", 14),
    )
    # Place hint near top of the primary-ish area (left half if side-by-side)
    hint.place(x=max(20, desk["width"] // 4 - 200), y=24)

    overlay.grab_set()
    overlay.lift()
    parent.wait_window(overlay)
    return state["result"]
