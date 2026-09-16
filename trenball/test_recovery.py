"""Checks that an unreconcilable frame still re-syncs once it proves stable."""

import cv2
import numpy as np

from trenball.trend_tracker import UNALIGNED_DETAIL, diff_grids, recover_new_beads
from trenball.trend_decoder import EMPTY, DecodeResult, decode_trend_grid


def grid(columns: list[list[str]], rows: int = 5) -> DecodeResult:
    padded = [list(col) + [EMPTY] * (rows - len(col)) for col in columns]
    return DecodeResult(columns=padded, rows=rows, cols=len(padded))


def test_anchor_on_bead_tail() -> None:
    got = recover_new_beads(list("OOOOOOGGGG"), list("OOOOOOGGGGOOGGG"))
    assert got is not None
    assert got[0] == list("OOGGG")


def test_anchor_after_columns_scrolled_away() -> None:
    got = recover_new_beads(list("GGGGGOOOGGO"), list("OOOGGOGGG"))
    assert got is not None
    assert got[0] == list("GGG")


def test_unrelated_series_is_not_invented() -> None:
    assert recover_new_beads(list("GGGGG"), list("OOOOO")) is None


def test_empty_baseline_adopts_whole_frame() -> None:
    got = recover_new_beads([], list("GOG"))
    assert got is not None
    assert got[0] == list("GOG")


BEAD_BGR = {"G": (110, 220, 90), "O": (60, 150, 240), "Y": (60, 230, 240)}


def render(columns: list[str], pitch: int = 25, rows: int = 5) -> np.ndarray:
    """Paint a bead road the way the site draws it, for decoder round-trips."""
    canvas = np.full((rows * pitch, len(columns) * pitch, 3), 30, dtype=np.uint8)
    for c, column in enumerate(columns):
        for r, bead in enumerate(column):
            center = (int((c + 0.5) * pitch), int((r + 0.5) * pitch))
            cv2.circle(canvas, center, pitch // 3, BEAD_BGR[bead], -1)
    return canvas


def test_decoder_keeps_rows_contiguous_when_bottom_rows_are_empty() -> None:
    # The crop gets trimmed to the beads, so the unused 5th row disappears.
    # Row spacing must come from the beads, not from the trimmed height.
    columns = ["OO", "G", "OOOO", "G", "O"]
    decoded = decode_trend_grid(render(columns), rows=5)
    assert decoded.cols == len(columns)
    for column, expected in zip(decoded.columns, columns):
        filled = [bead for bead in column if bead != EMPTY]
        assert filled == list(expected)
        assert column[: len(expected)] == list(expected), "rows must start at the top"


def test_decoder_does_not_invent_columns() -> None:
    columns = ["OG", "G", "O", "YG", "O", "G", "OO"]
    decoded = decode_trend_grid(render(columns), rows=5)
    beads = sum(1 for col in decoded.columns for b in col if b != EMPTY)
    assert decoded.cols == len(columns)
    assert beads == sum(len(c) for c in columns)


def test_unaligned_frames_report_the_shared_detail() -> None:
    previous = grid([list("GGGGG")])
    current = grid([list("OOOOO")])
    assert diff_grids(previous, current).detail == UNALIGNED_DETAIL


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
