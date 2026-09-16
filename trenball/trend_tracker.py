"""Compare two decoded trenball grids and extract newly appeared beads."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .trend_decoder import DecodeResult, category_of_bead


@dataclass
class DiffResult:
    new_beads: list[str]
    kind: str  # "same" | "appended" | "shifted" | "first" | "none"
    detail: str
    previous_signature: str
    current_signature: str


@dataclass
class TrendHistory:
    beads: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def append_new(self, new_beads: list[str], kind: str, detail: str) -> None:
        if not new_beads:
            return
        stamp = datetime.now().isoformat(timespec="seconds")
        self.beads.extend(new_beads)
        self.events.append(
            {
                "time": stamp,
                "new": new_beads,
                "kind": kind,
                "detail": detail,
            }
        )

    def save(self, path: Path) -> None:
        payload = {"beads": self.beads, "events": self.events}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> TrendHistory:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(beads=list(data.get("beads", [])), events=list(data.get("events", [])))


def _longest_suffix_prefix_overlap(prev: list[str], curr: list[str]) -> int:
    """
    Length of longest suffix of prev that matches a prefix of curr.
    Used when the visible road scrolled left.
    """
    max_k = min(len(prev), len(curr))
    for k in range(max_k, 0, -1):
        if prev[-k:] == curr[:k]:
            return k
    return 0


def streak_runs(beads: list[str]) -> list[tuple[str, int]]:
    """Collapse beads into (WIN|LOSE, length) runs — the win/loss series."""
    runs: list[tuple[str, int]] = []
    for bead in beads:
        category = category_of_bead(bead)
        if category == "EMPTY":
            continue
        if runs and runs[-1][0] == category:
            runs[-1] = (category, runs[-1][1] + 1)
        else:
            runs.append((category, 1))
    return runs


def align_by_streaks(prev: list[str], curr: list[str]) -> list[str] | None:
    """
    Align two frames on their win/loss streak series instead of exact beads.

    When a column scrolls out of the captured area the road is re-read, so the
    literal bead order can shift even though the underlying streaks are intact.
    The oldest visible streak may be partially cut off, and the newest streak
    may have grown, so those two are compared loosely.

    Returns the newly added beads, or None when no trustworthy alignment fits.
    """
    prev_runs = streak_runs(prev)
    curr_runs = streak_runs(curr)
    if len(prev_runs) < 2 or len(curr_runs) < 2:
        return None

    for offset in range(len(prev_runs)):
        matched = len(prev_runs) - offset
        if matched < 2 or matched > len(curr_runs):
            continue

        ok = True
        for index in range(matched):
            prev_category, prev_length = prev_runs[offset + index]
            curr_category, curr_length = curr_runs[index]
            if prev_category != curr_category:
                ok = False
                break
            if index == 0 and curr_length > prev_length:
                ok = False
                break
            if index == matched - 1 and curr_length < prev_length:
                ok = False
                break
            if 0 < index < matched - 1 and curr_length != prev_length:
                ok = False
                break
        if not ok:
            continue

        consumed = sum(length for _, length in curr_runs[: matched - 1])
        consumed += prev_runs[-1][1]
        if consumed < 4 or consumed > len(curr):
            continue
        return curr[consumed:]

    return None


UNALIGNED_DETAIL = "Could not align road sequences"

# Fewest trailing beads that may anchor a re-sync; below this the pattern is
# too common to identify one spot in the road.
MIN_ANCHOR_BEADS = 4

# Same idea at streak level: run lengths carry more signal than bare beads,
# but a couple of runs still repeat too often to be trusted on their own.
MIN_ANCHOR_RUNS = 3


def _last_index_of(haystack: list[str], needle: list[str]) -> int:
    """Index of the last contiguous occurrence of needle in haystack, or -1."""
    if not needle or len(needle) > len(haystack):
        return -1
    for start in range(len(haystack) - len(needle), -1, -1):
        if haystack[start : start + len(needle)] == needle:
            return start
    return -1


def recover_new_beads(prev: list[str], curr: list[str]) -> tuple[list[str], str] | None:
    """
    Re-sync two frames that normal diffing could not reconcile.

    Anchors the longest possible tail of the tracked series inside the current
    frame, then treats whatever follows that anchor as the rounds that were
    missed. Falls back to the win/loss streak series when bead-level anchoring
    fails, which happens when the road is re-read after columns scroll away.

    Returns (new_beads, explanation), or None when nothing can be anchored.
    """
    if not curr:
        return None
    if not prev:
        return list(curr), f"no prior series; adopted {len(curr)} bead(s)"

    # Bead-level anchor: longest tail of prev found inside curr. Short tails
    # such as "OG" repeat all over the road, so demand a distinctive run.
    shortest_anchor = min(MIN_ANCHOR_BEADS, len(prev))
    for size in range(min(len(prev), len(curr)), shortest_anchor - 1, -1):
        tail = prev[-size:]
        at = _last_index_of(curr, tail)
        if at >= 0:
            return curr[at + size :], f"anchored on last {size} bead(s)"

    # Streak-level anchor: tolerate re-reads that reshuffle bead order.
    streak_new = align_by_streaks(prev, curr)
    if streak_new is not None:
        return streak_new, "anchored on win/loss streaks"

    prev_runs = streak_runs(prev)
    curr_runs = streak_runs(curr)
    fewest_runs = min(MIN_ANCHOR_RUNS, len(prev_runs))
    for size in range(min(len(prev_runs), len(curr_runs)), fewest_runs - 1, -1):
        tail = prev_runs[-size:]
        for start in range(len(curr_runs) - size, -1, -1):
            window = curr_runs[start : start + size]
            if [c for c, _ in window] != [c for c, _ in tail]:
                continue
            # Oldest anchored streak may be clipped, newest may have grown.
            if any(w[1] != t[1] for w, t in zip(window[1:-1], tail[1:-1])):
                continue
            if window[0][1] > tail[0][1] or window[-1][1] < tail[-1][1]:
                continue
            consumed = sum(length for _, length in curr_runs[:start])
            consumed += sum(length for _, length in window[:-1])
            consumed += tail[-1][1]
            if consumed <= len(curr):
                return curr[consumed:], f"anchored on {size} streak(s)"

    return None


def max_rounds_in(seconds: float, min_seconds_per_round: float, floor: int) -> int:
    """
    Most rounds that can have finished in `seconds`.

    Rounds take real time, so a stretch of unobserved road can only hide so
    many of them. Anything beyond that is a misread rather than missed history.
    `floor` keeps a small allowance for clock and capture jitter on short gaps.
    """
    if min_seconds_per_round <= 0:
        return floor
    return max(floor, int(math.ceil(max(0.0, seconds) / min_seconds_per_round)) + 1)


def diff_grids(previous: DecodeResult | None, current: DecodeResult) -> DiffResult:
    """
    Diff using Big Road chronological sequences (not naive column flatten).

    That way a dragon bead in a mixed column (e.g. 5+1 lose under a new win
    root) stays with its root streak instead of being read as a later opposite.
    """
    curr_seq = current.flat_sequence()
    curr_sig = "".join(curr_seq)

    if previous is None:
        return DiffResult(
            new_beads=list(curr_seq),
            kind="first",
            detail=f"Initial snapshot: {len(curr_seq)} beads",
            previous_signature="",
            current_signature=curr_sig,
        )

    prev_seq = previous.flat_sequence()
    prev_sig = "".join(prev_seq)

    if prev_seq == curr_seq:
        return DiffResult(
            new_beads=[],
            kind="none",
            detail="No change",
            previous_signature=prev_sig,
            current_signature=curr_sig,
        )

    # Normal growth: previous sequence is a prefix of current
    if len(curr_seq) >= len(prev_seq) and curr_seq[: len(prev_seq)] == prev_seq:
        new = curr_seq[len(prev_seq) :]
        return DiffResult(
            new_beads=new,
            kind="appended" if new else "none",
            detail=f"Road grew (+{len(new)} bead(s))" if new else "No change",
            previous_signature=prev_sig,
            current_signature=curr_sig,
        )

    # Scrolled / truncated left edge: overlap suffix/prefix, take remainder
    overlap = _longest_suffix_prefix_overlap(prev_seq, curr_seq)
    if overlap > 0:
        new = curr_seq[overlap:]
        return DiffResult(
            new_beads=new,
            kind="shifted",
            detail=f"View scrolled; overlap {overlap}, +{len(new)} new bead(s)",
            previous_signature=prev_sig,
            current_signature=curr_sig,
        )

    # Fallback: if current is shorter but still a suffix of previous, no new beads
    if prev_seq[-len(curr_seq) :] == curr_seq:
        return DiffResult(
            new_beads=[],
            kind="shifted",
            detail="View scrolled; no new beads in frame",
            previous_signature=prev_sig,
            current_signature=curr_sig,
        )

    # A column leaving the crop makes the road re-read, so exact bead order can
    # shift while the win/loss streaks stay intact. Align on those instead.
    streak_new = align_by_streaks(prev_seq, curr_seq)
    if streak_new is not None:
        return DiffResult(
            new_beads=streak_new,
            kind="shifted",
            detail=f"Aligned on win/loss streaks; +{len(streak_new)} new bead(s)",
            previous_signature=prev_sig,
            current_signature=curr_sig,
        )

    return DiffResult(
        new_beads=[],
        kind="none",
        detail=UNALIGNED_DETAIL,
        previous_signature=prev_sig,
        current_signature=curr_sig,
    )


def summarize_streaks(beads: list[str]) -> str:
    if not beads:
        return "(empty)"
    parts: list[str] = []
    current_cat = category_of_bead(beads[0])
    run = [beads[0]]
    for bead in beads[1:]:
        cat = category_of_bead(bead)
        if cat == current_cat:
            run.append(bead)
        else:
            parts.append(f"{current_cat}x{len(run)}({''.join(run)})")
            current_cat = cat
            run = [bead]
    parts.append(f"{current_cat}x{len(run)}({''.join(run)})")
    return " -> ".join(parts)
