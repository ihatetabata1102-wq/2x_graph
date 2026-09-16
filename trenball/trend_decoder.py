"""Decode BC.Game-style crash trenball grid from a cropped screenshot."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Labels used throughout the app
GREEN = "G"  # typically ~2x–9.99x
ORANGE = "O"  # typically <2x
YELLOW = "Y"  # typically >=10x
EMPTY = "."

# Huge multipliers (100x+) trigger a celebration animation — a cat trailing a
# rainbow — that flies across the road for several seconds. Its rainbow is
# painted in the same orange/yellow/green as the beads, so a covered frame still
# decodes into a plausible-looking road: beads under the cat vanish, beads under
# the rainbow change colour, and the holes left behind reorder the whole
# chronological read. Nothing in the decoded output marks such a frame as
# damaged, so it has to be recognised from the picture instead.
#
# Both limits sit in the gap measured over ~8.5k recorded captures: covered
# frames start at 0.85% foreign colour and 1.9% pale pixels, while the busiest
# untouched frame reaches 0.15% and 0.73%.
OBSCURED_FOREIGN_RATIO = 0.004
OBSCURED_PALE_RATIO = 0.012


def category_of_bead(bead: str) -> str:
    """Road category: yellow rides with green (win side)."""
    if bead in (GREEN, YELLOW):
        return "WIN"
    if bead == ORANGE:
        return "LOSE"
    return "EMPTY"


def same_road_color(a: str, b: str) -> bool:
    """Win/lose sides only — G and Y are the same road color."""
    ca, cb = category_of_bead(a), category_of_bead(b)
    return ca != "EMPTY" and ca == cb


@dataclass
class DecodeResult:
    """columns[col][row] from top→bottom; each cell is G/O/Y or empty."""

    columns: list[list[str]]
    rows: int
    cols: int

    def non_empty_columns(self) -> list[list[str]]:
        out: list[list[str]] = []
        for col in self.columns:
            beads = [c for c in col if c != EMPTY]
            if beads:
                out.append(beads)
        return out

    def flat_sequence(self) -> list[str]:
        """Chronological beads using Big Road roots + dragon tails (5+1, 4+1, …)."""
        return extract_road_sequence(self.columns, self.rows)

    def signature(self) -> str:
        """Stable signature of chronological road sequence."""
        return "".join(self.flat_sequence())

    def grid_signature(self) -> str:
        parts = []
        for col in self.columns:
            parts.append("".join(cell if cell != EMPTY else "." for cell in col))
        return "|".join(parts)


def extract_road_sequence(columns: list[list[str]], rows: int = 5) -> list[str]:
    """
    Read a Big-Road / trenball grid in true chronological order.

    Rules:
    - A streak (root) starts at row 0 of a column when that cell is not already
      part of a previous streak's dragon tail.
    - Same road-color continues down the column.
    - If it cannot go down (bottom or occupied / other color), it continues
      right on the same row (dragon). That is how 6 = 5+1, 5 = 4+1, 6 = 4+2
      appear when a lower cell is already taken by another root's dragon.
    - A physical column may show both WIN and LOSE beads; they belong to
      different roots and must not be read as one mixed streak.
    """
    if not columns:
        return []

    cols = len(columns)
    grid = []
    for c in range(cols):
        col = list(columns[c])
        if len(col) < rows:
            col = col + [EMPTY] * (rows - len(col))
        else:
            col = col[:rows]
        grid.append(col)

    visited: set[tuple[int, int]] = set()
    sequence: list[str] = []

    def cell(c: int, r: int) -> str:
        if c < 0 or r < 0 or c >= cols or r >= rows:
            return EMPTY
        return grid[c][r]

    def walk_streak(c: int, r: int) -> None:
        color_bead = cell(c, r)
        while True:
            cur = cell(c, r)
            if cur == EMPTY or not same_road_color(cur, color_bead):
                break
            if (c, r) in visited:
                break
            visited.add((c, r))
            sequence.append(cur)

            down = cell(c, r + 1)
            if (
                r + 1 < rows
                and down != EMPTY
                and same_road_color(down, color_bead)
                and (c, r + 1) not in visited
            ):
                r += 1
                continue

            right = cell(c + 1, r)
            if (
                c + 1 < cols
                and right != EMPTY
                and same_road_color(right, color_bead)
                and (c + 1, r) not in visited
            ):
                c += 1
                continue

            break

    # Dragon tails whose root column already scrolled out of the crop. They
    # belong to a streak that started before the visible window, so they must
    # be read before any root in the leftmost column.
    for r in range(1, rows):
        bead = cell(0, r)
        if bead == EMPTY or (0, r) in visited:
            continue
        if same_road_color(cell(0, r - 1), bead):
            continue
        walk_streak(0, r)

    for start_c in range(cols):
        bead0 = cell(start_c, 0)
        if bead0 == EMPTY or (start_c, 0) in visited:
            continue
        walk_streak(start_c, 0)

    for c in range(cols):
        for r in range(rows):
            if (c, r) in visited:
                continue
            cur = cell(c, r)
            if cur == EMPTY:
                continue
            visited.add((c, r))
            sequence.append(cur)

    return sequence


def _classify_bgr(bgr: np.ndarray) -> str:
    """Classify a small patch by dominant HSV hue."""
    if bgr.size == 0:
        return EMPTY
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    mask = (s > 50) & (v > 60)
    if not np.any(mask):
        return EMPTY

    h_med = float(np.median(h[mask]))
    s_med = float(np.median(s[mask]))
    v_med = float(np.median(v[mask]))

    # Orange / red
    if (h_med <= 22 or h_med >= 155) and s_med > 60:
        return ORANGE
    # Yellow / gold
    if 18 < h_med <= 40 and v_med > 90:
        return YELLOW
    # Green
    if 35 < h_med <= 100:
        return GREEN

    b, g, r = [float(x) for x in np.mean(bgr.reshape(-1, 3), axis=0)]
    if g > r * 1.1 and g > b * 1.1:
        return GREEN
    if r > g * 1.05 and r > b:
        if g > b * 1.15 and g > 70:
            return YELLOW
        return ORANGE
    return EMPTY


def _color_masks(hsv: np.ndarray) -> dict[str, np.ndarray]:
    """HSV masks for trenball bead colors (OpenCV H: 0–179)."""
    # Orange / coral
    orange1 = cv2.inRange(hsv, (0, 70, 70), (22, 255, 255))
    orange2 = cv2.inRange(hsv, (155, 70, 70), (179, 255, 255))
    orange = cv2.bitwise_or(orange1, orange2)
    # Yellow / gold (moon)
    yellow = cv2.inRange(hsv, (18, 60, 100), (40, 255, 255))
    # Green
    green = cv2.inRange(hsv, (36, 50, 60), (100, 255, 255))
    # Avoid counting yellow pixels as orange
    orange = cv2.bitwise_and(orange, cv2.bitwise_not(yellow))
    return {ORANGE: orange, YELLOW: yellow, GREEN: green}


def obscuring_ratios(image_bgr: np.ndarray) -> tuple[float, float]:
    """
    How much of the frame is not road: (foreign colour, pale) as area shares.

    The road only ever contains dark grey cells plus orange, yellow and green
    beads. Vivid pixels of any other hue — the blue and purple rainbow stripes,
    another window — are foreign. Large washed-out areas are the cat itself, a
    popup, or a page that has not finished painting.
    """
    if image_bgr is None or image_bgr.size == 0:
        return 0.0, 0.0

    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    masks = _color_masks(hsv)
    palette = masks[GREEN] | masks[ORANGE] | masks[YELLOW]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    vivid = (saturation > 70) & (value > 70)
    foreign = int(np.count_nonzero(vivid & (palette == 0)))
    pale = int(np.count_nonzero((saturation < 60) & (value > 150)))

    total = float(image_bgr.shape[0] * image_bgr.shape[1])
    return foreign / total, pale / total


def frame_is_obscured(image_bgr: np.ndarray) -> bool:
    """True when something is drawn over the road, so it must not be decoded."""
    foreign, pale = obscuring_ratios(image_bgr)
    return foreign >= OBSCURED_FOREIGN_RATIO or pale >= OBSCURED_PALE_RATIO


def describe_obstruction(image_bgr: np.ndarray) -> str:
    """Short reason for the status panel."""
    foreign, pale = obscuring_ratios(image_bgr)
    if foreign >= OBSCURED_FOREIGN_RATIO and pale >= OBSCURED_PALE_RATIO:
        return f"animation over the road ({foreign:.1%} foreign, {pale:.1%} pale)"
    if foreign >= OBSCURED_FOREIGN_RATIO:
        return f"colours that are not beads ({foreign:.1%} of the frame)"
    return f"road hidden behind something pale ({pale:.1%} of the frame)"


def _find_bead_blobs(image_bgr: np.ndarray) -> list[tuple[float, float, str]]:
    """Return list of (cx, cy, label) for detected bead dots."""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    h_img, w_img = image_bgr.shape[:2]
    min_area = max(8.0, (h_img / 5.0) * (h_img / 5.0) * 0.08)
    max_area = max(min_area * 2, (h_img / 5.0) * (h_img / 5.0) * 3.5)

    blobs: list[tuple[float, float, str]] = []
    for label, mask in _color_masks(hsv).items():
        # Clean noise
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area or area > max_area:
                continue
            m = cv2.moments(cnt)
            if m["m00"] <= 1e-6:
                continue
            cx = float(m["m10"] / m["m00"])
            cy = float(m["m01"] / m["m00"])
            # Confirm color on a small patch around centroid
            x0 = max(0, int(cx) - 2)
            x1 = min(w_img, int(cx) + 3)
            y0 = max(0, int(cy) - 2)
            y1 = min(h_img, int(cy) + 3)
            confirmed = _classify_bgr(image_bgr[y0:y1, x0:x1])
            if confirmed == EMPTY:
                confirmed = label
            blobs.append((cx, cy, confirmed))
    return blobs


def _cluster_columns(xs: list[float], cell_w: float) -> list[int]:
    """Assign each x to a column index using greedy left-to-right clustering."""
    if not xs:
        return []
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    col_of = [-1] * len(xs)
    centers: list[float] = []
    thresh = max(3.0, cell_w * 0.55)
    for i in order:
        x = xs[i]
        if not centers or abs(x - centers[-1]) > thresh:
            centers.append(x)
            col_of[i] = len(centers) - 1
        else:
            # assign to nearest existing center among recent ones
            best = min(range(len(centers)), key=lambda c: abs(x - centers[c]))
            if abs(x - centers[best]) <= thresh:
                col_of[i] = best
                n = sum(1 for c in col_of if c == best)
                centers[best] = (centers[best] * (n - 1) + x) / n
            else:
                centers.append(x)
                col_of[i] = len(centers) - 1
    return col_of


def _row_pitch_and_origin(ys: list[float], fallback_pitch: float) -> tuple[float, float]:
    """
    Measure the vertical spacing between bead rows from the beads themselves.

    The crop is trimmed to the beads, so its height only covers the rows that
    are actually occupied. Dividing that height by the configured row count
    therefore understates the spacing and pushes lower beads into rows below
    where they belong, so the spacing is read off the bead positions instead.
    """
    if not ys:
        return fallback_pitch, 0.0

    centers: list[float] = []
    for y in sorted(ys):
        if not centers or y - centers[-1] > fallback_pitch * 0.5:
            centers.append(y)
        else:
            centers[-1] = (centers[-1] + y) / 2.0

    origin = centers[0]
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    if not gaps:
        return fallback_pitch, origin

    smallest = min(gaps)
    # A column may skip rows, so treat the tightest gap as one row step and
    # fold the wider gaps back onto that step before averaging.
    steps = [g / max(1.0, round(g / smallest)) for g in gaps]
    return float(np.median(steps)), origin


def _decode_via_blobs(image_bgr: np.ndarray, rows: int = 5) -> DecodeResult | None:
    blobs = _find_bead_blobs(image_bgr)
    if len(blobs) < 2:
        return None

    h, w = image_bgr.shape[:2]
    cell_h, row_origin = _row_pitch_and_origin(
        [b[1] for b in blobs], fallback_pitch=h / float(rows)
    )
    # Estimate cell width from median horizontal gap between sorted unique-ish x
    xs = sorted(b[0] for b in blobs)
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > 1.0]
    # Gaps within same column are tiny; column gaps are ~cell_w
    col_gaps = [g for g in gaps if g > cell_h * 0.35]
    if col_gaps:
        cell_w = float(np.median(col_gaps))
    else:
        cell_w = cell_h

    col_ids = _cluster_columns([b[0] for b in blobs], cell_w)
    n_cols = max(col_ids) + 1 if col_ids else 0
    if n_cols <= 0:
        return None

    grid: list[list[str]] = [[EMPTY] * rows for _ in range(n_cols)]
    # For each blob, snap to nearest row by y
    for (cx, cy, label), col in zip(blobs, col_ids):
        if col < 0:
            continue
        row = int(round((cy - row_origin) / cell_h))
        row = max(0, min(rows - 1, row))
        # Prefer keeping a bead if cell empty; if conflict, keep stronger (non-empty already)
        if grid[col][row] == EMPTY:
            grid[col][row] = label
        elif category_of_bead(grid[col][row]) != category_of_bead(label):
            # conflict: choose by closer vertical center of that row
            row_center = (row + 0.5) * cell_h
            # leave existing; rare
            _ = row_center

    # Drop leading/trailing fully empty columns
    while grid and all(c == EMPTY for c in grid[0]):
        grid.pop(0)
    while grid and all(c == EMPTY for c in grid[-1]):
        grid.pop()

    if not grid:
        return None
    return DecodeResult(columns=grid, rows=rows, cols=len(grid))


def _decode_via_grid(image_bgr: np.ndarray, rows: int = 5) -> DecodeResult:
    """Fallback: split crop into a regular rows×cols sampling grid."""
    if image_bgr.size == 0:
        return DecodeResult(columns=[], rows=rows, cols=0)

    h, w = image_bgr.shape[:2]
    cell_h = max(4, h // rows)
    # Prefer square cells from height; fit as many columns as width allows
    cell_w = cell_h
    cols = max(1, int(round(w / cell_w)))
    cell_w = max(4, w / cols)

    grid: list[list[str]] = []
    for c in range(cols):
        column: list[str] = []
        for r in range(rows):
            y0 = int(r * cell_h + cell_h * 0.22)
            y1 = int(min(h, (r + 1) * cell_h - cell_h * 0.22))
            x0 = int(c * cell_w + cell_w * 0.22)
            x1 = int(min(w, (c + 1) * cell_w - cell_w * 0.22))
            if y1 <= y0 or x1 <= x0:
                column.append(EMPTY)
                continue
            column.append(_classify_bgr(image_bgr[y0:y1, x0:x1]))
        grid.append(column)

    while grid and all(cell == EMPTY for cell in grid[-1]):
        grid.pop()
    while grid and all(cell == EMPTY for cell in grid[0]):
        grid.pop(0)

    return DecodeResult(columns=grid, rows=rows, cols=len(grid))


def trim_grid_margins(image_bgr: np.ndarray) -> np.ndarray:
    """Crop away dark empty margins so the 5-row grid fills the frame."""
    if image_bgr.size == 0:
        return image_bgr
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    masks = _color_masks(hsv)
    combined = masks[GREEN] | masks[ORANGE] | masks[YELLOW]
    ys, xs = np.where(combined > 0)
    if len(xs) < 5:
        return image_bgr
    pad = max(2, int(round(image_bgr.shape[0] * 0.04)))
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(image_bgr.shape[1], int(xs.max()) + pad + 1)
    y0 = max(0, int(ys.min()) - pad)
    y1 = min(image_bgr.shape[0], int(ys.max()) + pad + 1)
    # Keep nearly full height if beads already span most rows
    if (y1 - y0) < image_bgr.shape[0] * 0.7:
        # still ok — tight crop helps
        pass
    return image_bgr[y0:y1, x0:x1].copy()


def decode_trend_grid(image_bgr: np.ndarray, rows: int = 5) -> DecodeResult:
    """
    Decode trenball beads. Prefers color-blob detection (more tolerant of
    slightly loose crops / non-square cells), falls back to regular grid sampling.
    """
    if image_bgr is None or image_bgr.size == 0:
        return DecodeResult(columns=[], rows=rows, cols=0)

    trimmed = trim_grid_margins(image_bgr)
    blob_result = _decode_via_blobs(trimmed, rows=rows)
    if blob_result is not None and blob_result.cols > 0:
        return blob_result

    # Only when no beads could be located directly: the fixed sampling grid
    # reads cells at assumed positions, so it double-counts wide beads and
    # invents columns whenever the crop is not an exact multiple of the cell.
    return _decode_via_grid(trimmed, rows=rows)
