"""
Guards against the 100x celebration animation corrupting the tracked series.

A win above 100x makes the site fly a cat trailing a rainbow across the road.
The rainbow is painted in the same orange/yellow/green as the beads, so a frame
caught mid-animation still decodes into a road that looks ordinary while beads
underneath are deleted, recoloured, and reordered. The animation outlasts a
single poll, so two consecutive frames can be damaged in exactly the same way —
which used to look like proof that the screen had genuinely moved on, and the
tracker would re-anchor and append old beads again as if they were new rounds.

These tests pin down the two defences: such frames are recognised and dropped,
and a re-sync can never append more rounds than the clock allows.
"""

import cv2
import numpy as np

from trenball.trend_decoder import (
    decode_trend_grid,
    describe_obstruction,
    frame_is_obscured,
    obscuring_ratios,
)
from trenball.trend_tracker import max_rounds_in

BEAD_BGR = {"G": (110, 220, 90), "O": (60, 150, 240), "Y": (60, 230, 240)}

# Nyan-cat rainbow, top stripe first (BGR).
RAINBOW_BGR = [
    (60, 60, 240),  # red
    (60, 150, 240),  # orange
    (60, 230, 240),  # yellow
    (110, 220, 90),  # green
    (240, 150, 60),  # blue
    (200, 60, 150),  # purple
]

ROAD = ["OO", "GGGGG", "OOO", "GG", "O", "G", "OOO", "G", "OOOOO", "GG", "Y", "OO"]


def render(columns: list[str], pitch: int = 25, rows: int = 5) -> np.ndarray:
    """Paint a bead road the way the site draws it."""
    canvas = np.full((rows * pitch, len(columns) * pitch, 3), 30, dtype=np.uint8)
    for c, column in enumerate(columns):
        for r, bead in enumerate(column):
            center = (int((c + 0.5) * pitch), int((r + 0.5) * pitch))
            cv2.circle(canvas, center, pitch // 3, BEAD_BGR[bead], -1)
    return canvas


def add_celebration(image: np.ndarray, nose_x: int, pitch: int = 25) -> np.ndarray:
    """Fly the cat across the road, rainbow trailing to the left."""
    frame = image.copy()
    h = frame.shape[0]
    body = pitch * 2
    top = h // 2 - body // 2

    stripe_h = max(1, body // len(RAINBOW_BGR))
    for index, colour in enumerate(RAINBOW_BGR):
        y0 = top + index * stripe_h
        cv2.rectangle(frame, (0, y0), (nose_x - body // 2, y0 + stripe_h - 1), colour, -1)

    # Pale grey cat with a dark outline, like the sprite the site uses.
    cv2.circle(frame, (nose_x, top + body // 2), body // 2, (205, 205, 200), -1)
    cv2.circle(frame, (nose_x, top + body // 2), body // 2, (40, 40, 40), 2)
    return frame


def test_clean_road_is_not_obscured() -> None:
    foreign, pale = obscuring_ratios(render(ROAD))
    assert not frame_is_obscured(render(ROAD)), f"foreign={foreign} pale={pale}"


def test_celebration_animation_is_detected() -> None:
    for nose_x in (60, 120, 200, 260):
        frame = add_celebration(render(ROAD), nose_x=nose_x)
        assert frame_is_obscured(frame), f"missed the animation at x={nose_x}"
        assert describe_obstruction(frame)


def test_animation_really_does_corrupt_the_decoded_road() -> None:
    """The reason the frame must be dropped rather than decoded and diffed."""
    clean = decode_trend_grid(render(ROAD), rows=5).flat_sequence()
    covered = decode_trend_grid(add_celebration(render(ROAD), nose_x=120), rows=5)
    assert covered.flat_sequence() != clean, "expected the overlay to damage the read"


def test_damaged_frame_can_repeat_and_still_stay_untrusted() -> None:
    """
    Two identical damaged frames used to authorise a re-sync.

    The animation is slow enough that consecutive polls can decode to the same
    wrong road, so sameness must not be read as proof the screen moved on.
    """
    first = add_celebration(render(ROAD), nose_x=120)
    second = add_celebration(render(ROAD), nose_x=120)
    assert (
        decode_trend_grid(first, rows=5).signature()
        == decode_trend_grid(second, rows=5).signature()
    )
    assert frame_is_obscured(first) and frame_is_obscured(second)


def test_a_blank_or_foreign_window_is_obscured() -> None:
    assert frame_is_obscured(np.full((112, 651, 3), 245, dtype=np.uint8))


def test_rounds_are_capped_by_elapsed_time() -> None:
    # The recorded failure: nine beads recovered across a single 5s poll.
    assert max_rounds_in(5.0, min_seconds_per_round=4.0, floor=3) == 3
    # A genuinely long gap may hand back proportionally more.
    assert max_rounds_in(60.0, min_seconds_per_round=4.0, floor=3) == 16
    # Short gaps keep a jitter allowance rather than dropping to zero.
    assert max_rounds_in(0.0, min_seconds_per_round=4.0, floor=3) == 3


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
