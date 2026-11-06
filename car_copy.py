"""Save how a car answered, then run the app from that file with no car or adapter.

A copy only holds what the car reported through read requests. It is not the ECU's program, and it holds the VIN,
so it stays on this computer.
"""
import asyncio
import json
import os
import re
from collections import deque
from datetime import datetime

from demo import frames
from paths import DATA_HOME

COPY_DIR = DATA_HOME / "car_copies"
COPY_FORMAT = "fahis-car-copy"
COPY_VERSION = 1
COPY_NAME = re.compile(r"car_\d{8}_\d{6}(_\d+)?\.json")
MAX_COPY_BYTES = 30 * 1024 * 1024
# Replies kept for each request. Live values change, so several are kept to play back in turn.
REPLIES_KEPT = 300
DEFAULT_HEADER = "7DF"
KEPT_ADAPTER_COMMANDS = ("ATZ", "ATDPN", "AT@1", "ATRV")
# Requests that change the car's memory. They are never kept, and a copy never pretends to carry them out.
CHANGING_SERVICES = ("04", "14", "2E", "2F", "31", "34", "3B", "3D")
REPLAY_DELAY_SECONDS = 0.04


def reply_key(header, command):
    """File replies by the module asked and the request. A count digit on a one-value request changes nothing."""
    command = command.upper()
    if re.fullmatch(r"01[0-9A-F]{3}", command):
        command = command[:4]
    return f"{header}|{command}"


class CarRecorder:
    """Keeps the car's replies during a connection, ready to be saved as a copy."""

    def __init__(self):
        self.header = DEFAULT_HEADER
        self.replies = {}

    def record(self, command, lines):
        command = command.upper()
        if command == "ATZ":
            self.header = DEFAULT_HEADER
        if command.startswith("ATSH"):
            self.header = command[4:]
            return
        if lines is None or command.startswith(CHANGING_SERVICES):
            return
        if command.startswith("AT") and command not in KEPT_ADAPTER_COMMANDS:
            return
        self.replies.setdefault(reply_key(self.header, command), deque(maxlen=REPLIES_KEPT)).append(list(lines))

    def as_data(self):
        return {key: list(kept) for key, kept in self.replies.items()}


class CopyTransport:
    """Answers like a real adapter, from a saved copy. It has no way to reach a car."""

    def __init__(self, copy):
        self.replies = copy["replies"]
        self.turns = {}
        self.header = DEFAULT_HEADER

    def next_reply(self, key):
        kept = self.replies.get(key)
        if not kept:
            return None
        turn = self.turns.get(key, 0)
        self.turns[key] = turn + 1
        return kept[turn % len(kept)]

    def one_value(self, pid):
        """Return {module: value bytes} for one value, from its single-frame replies."""
        found = {}
        for line in self.next_reply(reply_key(self.header, f"01{pid:02X}")) or []:
            parts = line.split()
            if len(parts) < 2 or not re.fullmatch(r"[0-9A-Fa-f]{3}", parts[0]) or not all(re.fullmatch(r"[0-9A-Fa-f]{2}", b) for b in parts[1:]):
                continue
            data = bytes.fromhex("".join(parts[1:]))
            if data[0] >> 4 == 0 and data[1:3] == bytes([0x41, pid]):
                found[parts[0]] = data[3:1 + data[0]]
        return found

    def grouped_reply(self, command):
        """Build a reply to a list of values the car was never asked for in this order, from its answers one at a time."""
        modules = {}
        for pid in bytes.fromhex(command[2:]):
            for header, data in self.one_value(pid).items():
                modules.setdefault(header, [0x41]).extend([pid, *data])
        return [line for header, payload in sorted(modules.items()) for line in frames(header, payload)]

    async def send(self, command, wait=5):
        await asyncio.sleep(REPLAY_DELAY_SECONDS)
        command = command.upper()
        if command == "ATZ":
            self.header = DEFAULT_HEADER
        if command.startswith("ATSH"):
            self.header = command[4:]
            return ["OK"]
        if command.startswith(CHANGING_SERVICES):
            return ["NO DATA"]
        kept = self.next_reply(reply_key(self.header, command))
        if kept is not None:
            return kept
        if command.startswith("AT"):
            return ["OK"]
        if re.fullmatch(r"01([0-9A-F]{2}){2,}", command):
            return self.grouped_reply(command) or ["NO DATA"]
        return ["NO DATA"]


def copy_folder(demo=False):
    """Demo pages get shared and screenshotted, so they keep their own copies apart from real ones."""
    return COPY_DIR / "demo" if demo else COPY_DIR


def save_copy(folder, recorder, name, vin):
    """Write a copy and return its file name."""
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stem, number = f"car_{now:%Y%m%d_%H%M%S}", 0
    path = folder / f"{stem}.json"
    while path.exists():
        number += 1
        path = folder / f"{stem}_{number}.json"
    data = {"format": COPY_FORMAT, "version": COPY_VERSION, "saved": now.isoformat(timespec="seconds"), "name": name, "vin": vin,
            "replies": recorder.as_data()}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)
    return path.name


def load_copy(folder, file_name):
    """Read a copy. Raises ValueError if the name or its contents aren't a car copy."""
    if not COPY_NAME.fullmatch(str(file_name)):
        raise ValueError("That isn't a car copy.")
    path = folder / file_name
    try:
        if path.stat().st_size > MAX_COPY_BYTES:
            raise ValueError("That isn't a car copy.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("That isn't a car copy.") from None
    replies = data.get("replies") if isinstance(data, dict) else None
    valid = (isinstance(replies, dict) and data.get("format") == COPY_FORMAT and data.get("version") == COPY_VERSION and
             all(isinstance(kept, list) and all(isinstance(lines, list) and all(isinstance(line, str) for line in lines) for lines in kept)
                 for kept in replies.values()))
    if not valid:
        raise ValueError("That isn't a car copy.")
    data["file"] = file_name
    return data


def list_copies(folder):
    """Describe the saved copies, newest first, leaving out any file that isn't one."""
    copies = []
    for path in folder.glob("car_*.json") if folder.is_dir() else []:
        try:
            data = load_copy(folder, path.name)
        except ValueError:
            continue
        copies.append({"file": path.name, "name": data.get("name"), "saved": data.get("saved"), "vin": data.get("vin"), "bytes": path.stat().st_size})
    return sorted(copies, key=lambda copy: copy["file"], reverse=True)
