"""Telegram delivery and alert-rule evaluation."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class AlertRule:
    name: str = "Alert"
    enabled: bool = False
    expected_value: int = 0
    tolerance: int = 3
    unlimited: bool = True
    start_step: int = 1
    end_step: int = 100

    @classmethod
    def from_dict(cls, value: object) -> "AlertRule":
        data = value if isinstance(value, dict) else {}
        return cls(
            name=str(data.get("name", "Alert")).strip() or "Alert",
            enabled=bool(data.get("enabled", False)),
            expected_value=int(data.get("expected_value", 0)),
            tolerance=max(0, int(data.get("tolerance", 3))),
            unlimited=bool(data.get("unlimited", True)),
            start_step=max(1, int(data.get("start_step", 1))),
            end_step=max(1, int(data.get("end_step", 100))),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "expected_value": self.expected_value,
            "tolerance": self.tolerance,
            "unlimited": self.unlimited,
            "start_step": self.start_step,
            "end_step": self.end_step,
        }

    def is_active_at(self, step: int) -> bool:
        return self.enabled and (self.unlimited or self.start_step <= step <= self.end_step)

    def matches(self, step: int, current_value: int) -> bool:
        return self.is_active_at(step) and abs(current_value - self.expected_value) <= self.tolerance


def points_since_last_10x(event_types: list[str]) -> int:
    """Count consecutive recorded points since the most recent 10x event."""
    count = 0
    for event_type in reversed(event_types):
        if event_type == "10x":
            break
        if event_type != "start":
            count += 1
    return count


def count_10x_in_recent_points(event_types: list[str], point_count: int) -> tuple[int, int]:
    """Return (10x count, available points) for the latest requested window."""
    points = [event for event in event_types if event != "start"][-point_count:]
    return sum(event == "10x" for event in points), len(points)


def send_telegram_message(token: str, chat_id: str, message: str, timeout: float = 10.0) -> None:
    """Send a Telegram Bot API message; raises a useful error on failure."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram rejected the message"))
