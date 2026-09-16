"""Quick checks for Big Road / 5+1 dragon reading."""

from __future__ import annotations

from trenball.trend_decoder import EMPTY, DecodeResult, extract_road_sequence
from trenball.trend_tracker import diff_grids, summarize_streaks


def _col(*cells: str, rows: int = 5) -> list[str]:
    out = list(cells)
    while len(out) < rows:
        out.append(EMPTY)
    return out[:rows]


def test_lose6_win4_mixed_column() -> None:
    """
    Classic 6 = 5+1 dragon under a new win root:

      O G
      O G
      O G
      O G
      O O   <- bottom-right O belongs to LOSEx6, not after WINx4
    """
    columns = [
        _col("O", "O", "O", "O", "O"),
        _col("G", "G", "G", "G", "O"),
    ]
    seq = extract_road_sequence(columns, rows=5)
    assert seq == list("OOOOOOGGGG"), seq
    assert summarize_streaks(seq) == "LOSEx6(OOOOOO) -> WINx4(GGGG)"


def test_lose6_win1() -> None:
    columns = [
        _col("O", "O", "O", "O", "O"),
        _col("G", EMPTY, EMPTY, EMPTY, "O"),
    ]
    seq = extract_road_sequence(columns, rows=5)
    assert seq == list("OOOOOOG"), seq
    assert summarize_streaks(seq) == "LOSEx6(OOOOOO) -> WINx1(G)"


def test_four_plus_one_streak() -> None:
    """
    5 = 4+1: bottom cell occupied by earlier dragon, so 5th same-color goes right.
      G G
      G
      G
      G
      O
    """
    columns = [
        _col("G", "G", "G", "G", "O"),
        _col(EMPTY, EMPTY, EMPTY, "G", EMPTY),
    ]
    # Root at col0 is WIN — but O at bottom is a prior lose dragon from further left.
    # Build with an explicit lose root first:
    columns = [
        _col("O", "O", "O", "O", "O"),
        _col("G", "G", "G", "G", "O"),
        _col(EMPTY, EMPTY, EMPTY, "G", EMPTY),  # 5th G dragon at row 3 (4+1)
    ]
    seq = extract_road_sequence(columns, rows=5)
    assert seq == list("OOOOOOGGGGG"), seq
    assert summarize_streaks(seq) == "LOSEx6(OOOOOO) -> WINx5(GGGGG)"


def test_chop_after_lose6() -> None:
    """LOSEx6 then chop: W1 L1 W2 L1 W3 L1 W4."""
    # L6 dragon under W1, then clean chop columns
    columns = [
        _col("O", "O", "O", "O", "O"),
        _col("G", EMPTY, EMPTY, EMPTY, "O"),  # W1 + dragon O
        _col("O"),
        _col("G", "G"),
        _col("O"),
        _col("G", "G", "G"),
        _col("O"),
        _col("G", "G", "G", "G"),
    ]
    seq = extract_road_sequence(columns, rows=5)
    expected = "OOOOOOG" + "O" + "GG" + "O" + "GGG" + "O" + "GGGG"
    assert "".join(seq) == expected, "".join(seq)
    assert summarize_streaks(seq) == (
        "LOSEx6(OOOOOO) -> WINx1(G) -> LOSEx1(O) -> WINx2(GG) -> "
        "LOSEx1(O) -> WINx3(GGG) -> LOSEx1(O) -> WINx4(GGGG)"
    )


def test_diff_uses_road_order() -> None:
    prev = DecodeResult(
        columns=[_col("O", "O", "O", "O", "O"), _col("G", EMPTY, EMPTY, EMPTY, "O")],
        rows=5,
        cols=2,
    )
    curr = DecodeResult(
        columns=[
            _col("O", "O", "O", "O", "O"),
            _col("G", EMPTY, EMPTY, EMPTY, "O"),
            _col("O"),
        ],
        rows=5,
        cols=3,
    )
    diff = diff_grids(prev, curr)
    assert diff.new_beads == ["O"], diff


if __name__ == "__main__":
    test_lose6_win4_mixed_column()
    test_lose6_win1()
    test_four_plus_one_streak()
    test_chop_after_lose6()
    test_diff_uses_road_order()
    print("all road-sequence tests passed")
    print(summarize_streaks(extract_road_sequence([
        _col("O", "O", "O", "O", "O"),
        _col("G", EMPTY, EMPTY, EMPTY, "O"),
        _col("O"),
        _col("G", "G"),
        _col("O"),
        _col("G", "G", "G"),
        _col("O"),
        _col("G", "G", "G", "G"),
    ])))
