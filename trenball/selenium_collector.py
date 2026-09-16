"""Collect BC.Game Crash results from Chrome WebSocket performance logs."""

from __future__ import annotations

import base64
from collections import deque
import json
import queue
import re
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any


ID_KEYS = {"gameid", "game_id", "roundid", "round_id"}
MULTIPLIER_KEYS = {"multiplier", "odds", "crash", "crashpoint", "crash_point", "bust", "payout"}
HASH_KEYS = {"hash", "gamehash", "game_hash"}
TIME_KEYS = {"timestamp", "time", "createdat", "created_at", "endtime", "end_time"}

# Keep enough identities to cover several days of rounds without allowing the
# deduplication cache to grow for the lifetime of the application.
SEEN_ROUND_LIMIT = 20_000


@dataclass(frozen=True)
class CrashRound:
    game_id: str | None
    multiplier: float
    hash: str | None = None
    timestamp: int | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        if self.game_id:
            return ("game", self.game_id)
        if self.hash:
            return ("hash", self.hash)
        return ("event", self.multiplier, self.timestamp)


def _number(value: Any) -> float | None:
    try:
        number = float(re.sub(r"[xX,]", "", value.strip())) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number < 1_000_000 else None


def _timestamp(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return round(number * 1000 if number < 10_000_000_000 else number)


def parse_payloads(payload: str) -> list[CrashRound]:
    text = payload.strip()
    if not text:
        return []
    text = re.sub(r"^\d+\s*(?=[\[{])", "", text)
    try:
        decoded = json.loads(text)
        if isinstance(decoded, str) and decoded.lstrip().startswith(("[", "{")):
            decoded = json.loads(decoded)
    except (ValueError, TypeError):
        return []

    found: list[CrashRound] = []

    def visit(node: Any, inherited: dict[str, Any] | None = None) -> None:
        inherited = dict(inherited or {})
        if isinstance(node, list):
            for item in node:
                visit(item, inherited)
            return
        if not isinstance(node, dict):
            return
        preferred = crash = None
        for key, value in node.items():
            lower = key.lower()
            if lower in ID_KEYS and isinstance(value, (str, int, float)):
                inherited["game_id"] = str(value)
            elif lower in MULTIPLIER_KEYS:
                number = _number(value)
                if lower in {"odds", "multiplier"}:
                    preferred = number
                elif lower == "crash":
                    crash = number
                elif preferred is None:
                    preferred = number
            elif lower in HASH_KEYS and isinstance(value, str) and value.strip():
                inherited["hash"] = value.strip()
            elif lower in TIME_KEYS:
                inherited["timestamp"] = _timestamp(value) or inherited.get("timestamp")
        multiplier = preferred if preferred is not None else crash
        if multiplier is not None:
            found.append(CrashRound(multiplier=multiplier, **inherited))
        for value in node.values():
            if isinstance(value, (dict, list)):
                visit(value, inherited)

    visit(decoded)
    return list({item.identity + (item.multiplier,): item for item in found}.values())


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _protobuf_fields(data: bytes) -> dict[int, int | bytes]:
    fields: dict[int, int | bytes] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            size, offset = _varint(data, offset)
            value, offset = data[offset : offset + size], offset + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, offset = data[offset : offset + size], offset + size
        else:
            raise ValueError("unsupported protobuf wire type")
        if offset > len(data):
            raise ValueError("truncated protobuf field")
        fields[field] = value
    return fields


def decode_bc_binary(payload: str) -> CrashRound | None:
    try:
        data = base64.b64decode(payload)
        if len(data) < 6 or data[:2] != b"\x04\x02":
            return None
        path_end = 3 + data[2]
        path = data[3:path_end].decode()
        event_size = data[path_end]
        event_start = path_end + 1
        event = data[event_start : event_start + event_size].decode()
        if path != "/g/cm" or event != "st":
            return None
        fields = _protobuf_fields(data[event_start + event_size :])
        game_id, hundredths = fields.get(1), fields.get(6)
        if not isinstance(game_id, int) or not isinstance(hundredths, int):
            return None
        hash_value = fields.get(7)
        return CrashRound(
            game_id=str(game_id), multiplier=hundredths / 100,
            hash=hash_value.decode(errors="replace") if isinstance(hash_value, bytes) else None,
            timestamp=round(time.time() * 1000),
        )
    except (ValueError, IndexError, UnicodeError):
        return None


class SeleniumCrashCollector:
    """Background-friendly collector. Events are tuples: (kind, payload)."""

    def __init__(self, url: str, events: queue.Queue, stop_event: threading.Event, headless: bool = False):
        self.url, self.events, self.stop_event, self.headless = url, events, stop_event, headless
        self.seen: set[tuple[Any, ...]] = set()
        self.seen_order: deque[tuple[Any, ...]] = deque()

    def _remember(self, identity: tuple[Any, ...]) -> bool:
        """Return False for a duplicate and retain only recent identities."""
        if identity in self.seen:
            return False
        self.seen.add(identity)
        self.seen_order.append(identity)
        if len(self.seen_order) > SEEN_ROUND_LIMIT:
            self.seen.remove(self.seen_order.popleft())
        return True

    def run(self) -> None:
        while not self.stop_event.is_set():
            driver = None
            try:
                driver = self._browser()
                self.events.put(("status", "Opening Crash page; log in if requested..."))
                driver.get(self.url)
                self.events.put(("status", "Connected; waiting for Crash rounds..."))
                last_frame = time.monotonic()
                while not self.stop_event.wait(0.25):
                    logs = driver.get_log("performance")
                    for entry in logs:
                        frame = self._frame(entry)
                        if frame is None:
                            continue
                        last_frame = time.monotonic()
                        for result in self._rounds(*frame):
                            if not self._remember(result.identity):
                                continue
                            self.events.put(("round", result))
                    if time.monotonic() - last_frame > 120:
                        raise RuntimeError("no WebSocket activity for 120 seconds")
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.events.put(("status", f"Collector reconnecting: {exc}"))
                    self.stop_event.wait(5)
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
        self.events.put(("stopped", None))

    def _browser(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        for argument in (
            "--window-size=1920,1080",
            "--log-level=3",
            "--disable-background-networking",
            "--disk-cache-size=67108864",
            "--media-cache-size=33554432",
        ):
            options.add_argument(argument)
        options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "WARNING"})
        return webdriver.Chrome(options=options)

    @staticmethod
    def _frame(entry: dict[str, Any]) -> tuple[int, str] | None:
        try:
            event = json.loads(entry["message"])["message"]
            if event["method"] != "Network.webSocketFrameReceived":
                return None
            response = event["params"]["response"]
            return int(response["opcode"]), response["payloadData"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _rounds(opcode: int, payload: str) -> list[CrashRound]:
        if opcode != 2:
            return parse_payloads(payload)
        rounds: list[CrashRound] = []
        binary = decode_bc_binary(payload)
        if binary:
            rounds.append(binary)
        try:
            compressed = base64.b64decode(payload)
            candidates = [compressed]
            for decoder in (zlib.decompress, lambda b: zlib.decompress(b, -zlib.MAX_WBITS)):
                try:
                    candidates.append(decoder(compressed))
                except zlib.error:
                    pass
            for candidate in candidates:
                try:
                    rounds.extend(parse_payloads(candidate.decode()))
                except UnicodeDecodeError:
                    pass
        except ValueError:
            pass
        return rounds
