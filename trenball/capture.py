"""Screenshot a selected screen region (supports multi-monitor absolute coords)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import mss
import numpy as np
from PIL import Image

from .region_selector import Region


def grab_region(region: Region) -> np.ndarray:
    """
    Return BGR uint8 image of the region (OpenCV-friendly).

    `region.left/top` are absolute virtual-desktop coordinates, so a crop on
    the second monitor (e.g. left=1920+) works the same as on the primary.
    """
    monitor = {
        "left": int(region.left),
        "top": int(region.top),
        "width": max(1, int(region.width)),
        "height": max(1, int(region.height)),
    }
    with mss.MSS() as sct:
        shot = sct.grab(monitor)
    # mss is BGRA
    bgra = np.asarray(shot, dtype=np.uint8)
    return bgra[:, :, :3].copy()  # BGR


def grab_region_pil(region: Region) -> Image.Image:
    bgr = grab_region(region)
    rgb = bgr[:, :, ::-1]
    return Image.fromarray(rgb)


def save_capture(image_bgr: np.ndarray, directory: Path, prefix: str = "capture") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"{prefix}_{stamp}.png"
    Image.fromarray(image_bgr[:, :, ::-1]).save(path)
    return path