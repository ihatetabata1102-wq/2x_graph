import queue
import threading

from trenball import selenium_collector
from trenball.selenium_collector import SeleniumCrashCollector


def test_seen_round_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(selenium_collector, "SEEN_ROUND_LIMIT", 3)
    collector = SeleniumCrashCollector("https://example.com", queue.Queue(), threading.Event())

    assert collector._remember(("game", "1"))
    assert not collector._remember(("game", "1"))
    assert collector._remember(("game", "2"))
    assert collector._remember(("game", "3"))
    assert collector._remember(("game", "4"))

    assert len(collector.seen) == 3
    assert ("game", "1") not in collector.seen
    assert collector._remember(("game", "1"))
