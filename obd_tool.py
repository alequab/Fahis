"""Read fault codes and live data from a car through a Bluetooth LE ELM327 adapter.

Only the `clear` command changes anything in the car, and it asks first.
"""
# Wrapped in t(), the text in this file is shown in the user's language; see i18n.py.
import argparse
import asyncio
import contextlib
import csv
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import bridge
import car_analysis
import car_copy
import car_explorer
import car_modules
import car_systems
import garage
import i18n
from demo import DemoTransport
from dtc_codes import (car_make, dtc_details, dtc_name, lookup_dtc, make_from_vin, oem_meaning, oem_meanings, repair_plans,
                       set_car_make)
from i18n import t
from paths import DATA_HOME

ADAPTER_NAME_HINTS = ("OBD", "ELM")
SERVICE_PREFIX = "0000fff0"
LOG_DIR = DATA_HOME / "logs"
REPORT_DIR = DATA_HOME / "reports"

# CAN with 11-bit headers. Replies are parsed as "7E8 06 41 ..." so others are refused.
SUPPORTED_PROTOCOLS = {"6": "ISO 15765-4 CAN 11-bit 500 kbps", "8": "ISO 15765-4 CAN 11-bit 250 kbps"}

CAN_LINE = re.compile(r"^[0-9A-F]{3}( [0-9A-F]{2})+$")

# 0 means closed/open loop doesn't apply right now: an engine that's off, or a diesel at any speed.
FUEL_STATUS = {0: "Not active", 1: "Open loop (cold)", 2: "Closed loop", 4: "Open loop (load)",
               8: "Open loop (fault)", 16: "Closed loop (fault)"}
OBD_STANDARDS = {1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD and OBD-II", 4: "OBD-I", 5: "Not OBD compliant",
                 6: "EOBD", 7: "EOBD and OBD-II", 8: "EOBD and OBD", 9: "EOBD, OBD and OBD-II",
                 10: "JOBD", 11: "JOBD and OBD-II", 12: "JOBD and EOBD", 13: "JOBD, EOBD and OBD-II",
                 17: "EMD", 18: "EMD+", 19: "HD OBD-C", 20: "HD OBD", 21: "WWH OBD", 23: "HD EOBD-I",
                 24: "HD EOBD-I N", 25: "HD EOBD-II", 26: "HD EOBD-II N", 28: "OBDBr-1", 29: "OBDBr-2",
                 30: "KOBD (Korea)", 31: "IOBD I (India)", 32: "IOBD II (India)", 33: "HD EOBD-IV"}
FUEL_TYPES = {1: "Gasoline", 2: "Methanol", 3: "Ethanol", 4: "Diesel", 5: "LPG", 6: "CNG", 7: "Propane",
              8: "Electric", 9: "Bifuel gasoline", 17: "Hybrid gasoline", 18: "Hybrid ethanol", 19: "Hybrid diesel"}


def percent(d):
    return d[0] * 100 / 255


def trim(d):
    return (d[0] - 128) * 100 / 128


def word(d):
    return 256 * d[0] + d[1]


def o2_sensors(d):
    found = [f"B{bank + 1}S{sensor + 1}" for bank in range(2) for sensor in range(4)
             if d[0] & (1 << (bank * 4 + sensor))]
    return ", ".join(found) or "None"


def catalyst_temp(d):
    """A tested diesel sends a fixed value just under 0 C with the engine hot, so that isn't a real temperature."""
    celsius = word(d) / 10 - 40
    return "Not reported" if celsius <= 0 else celsius


def temperature(d):
    """00 and FF are the ends of the range, which sensors and modules use when they have no reading."""
    return "Not reported" if d[0] in (0x00, 0xFF) else d[0] - 40


# pid: (name, unit, formula taking the data bytes)
LIVE_PIDS = {
    0x0C: ("Engine RPM", "rpm", lambda d: (256 * d[0] + d[1]) / 4),
    0x0D: ("Speed", "km/h", lambda d: d[0]),
    0x05: ("Coolant temp", "C", temperature),
    0x0F: ("Intake air temp", "C", temperature),
    0x04: ("Engine load", "%", lambda d: d[0] * 100 / 255),
    0x11: ("Throttle", "%", lambda d: d[0] * 100 / 255),
    0x0E: ("Ignition timing", "deg", lambda d: d[0] / 2 - 64),
    0x0B: ("Intake pressure", "kPa", lambda d: d[0]),
    0x10: ("Air flow", "g/s", lambda d: (256 * d[0] + d[1]) / 100),
    0x06: ("Short fuel trim", "%", lambda d: (d[0] - 128) * 100 / 128),
    0x07: ("Long fuel trim", "%", lambda d: (d[0] - 128) * 100 / 128),
    0x1F: ("Run time", "s", lambda d: 256 * d[0] + d[1]),
    0x2F: ("Fuel level", "%", lambda d: d[0] * 100 / 255),
    0x33: ("Barometric pressure", "kPa", lambda d: d[0]),
    0x42: ("Module voltage", "V", lambda d: (256 * d[0] + d[1]) / 1000),
    0x46: ("Ambient temp", "C", temperature),
    # A tested diesel always sends 00 here with a warm engine, which means no sensor rather than -40.
    0x5C: ("Oil temp", "C", lambda d: "Not reported" if d[0] == 0 else d[0] - 40),
    0x5E: ("Fuel rate", "L/h", lambda d: (256 * d[0] + d[1]) / 20),
    0x03: ("Fuel system", "", lambda d: FUEL_STATUS.get(d[0], f"Code {d[0]}")),
    0x0A: ("Fuel pressure", "kPa", lambda d: d[0] * 3),
    0x13: ("O2 sensors fitted", "", o2_sensors),
    0x1C: ("OBD standard", "", lambda d: OBD_STANDARDS.get(d[0], f"Code {d[0]}")),
    0x21: ("Distance with light on", "km", word),
    0x23: ("Fuel rail pressure", "kPa", lambda d: word(d) * 10),
    0x2C: ("Commanded EGR", "%", percent),
    # On a tested car FF came almost only with no EGR commanded, so it means no figure rather than +99%.
    0x2D: ("EGR error", "%", lambda d: "Not reported" if d[0] == 0xFF else trim(d)),
    0x2E: ("Commanded evap purge", "%", percent),
    0x30: ("Warm-ups since clear", "", lambda d: "255+" if d[0] == 255 else d[0]),
    0x31: ("Distance since clear", "km", lambda d: "65535+" if word(d) == 65535 else word(d)),
    0x32: ("Evap vapour pressure", "Pa", lambda d: int.from_bytes(d[:2], "big", signed=True) / 4),
    0x3C: ("Catalyst temp B1S1", "C", catalyst_temp),
    0x3D: ("Catalyst temp B2S1", "C", catalyst_temp),
    0x3E: ("Catalyst temp B1S2", "C", catalyst_temp),
    0x3F: ("Catalyst temp B2S2", "C", catalyst_temp),
    0x43: ("Absolute load", "%", lambda d: word(d) * 100 / 255),
    0x44: ("Commanded lambda", "", lambda d: word(d) / 32768),
    0x45: ("Relative throttle", "%", percent),
    0x47: ("Absolute throttle B", "%", percent),
    0x48: ("Absolute throttle C", "%", percent),
    0x49: ("Accelerator pedal D", "%", percent),
    0x4A: ("Accelerator pedal E", "%", percent),
    0x4B: ("Accelerator pedal F", "%", percent),
    0x4C: ("Commanded throttle", "%", percent),
    0x4D: ("Time with light on", "min", word),
    0x4E: ("Time since clear", "min", word),
    0x51: ("Fuel type", "", lambda d: FUEL_TYPES.get(d[0], f"Code {d[0]}")),
    0x52: ("Ethanol content", "%", percent),
    0x55: ("Short O2 trim B1", "%", trim),
    0x56: ("Long O2 trim B1", "%", trim),
    0x57: ("Short O2 trim B2", "%", trim),
    0x58: ("Long O2 trim B2", "%", trim),
    0x59: ("Fuel rail absolute pressure", "kPa", lambda d: word(d) * 10),
    0x5D: ("Fuel injection timing", "deg", lambda d: word(d) / 128 - 210),
    0x61: ("Driver demand torque", "%", lambda d: d[0] - 125),
    0x62: ("Actual engine torque", "%", lambda d: d[0] - 125),
    0x63: ("Engine reference torque", "Nm", word),
    0x08: ("Short fuel trim B2", "%", trim),
    0x09: ("Long fuel trim B2", "%", trim),
    0x22: ("Fuel rail pressure (relative)", "kPa", lambda d: word(d) * 0.079),
    0x5A: ("Relative pedal", "%", percent),
    0x5B: ("Hybrid battery charge", "%", percent),
}
for _base in (0x24, 0x34):  # Cars use one range or the other for wideband oxygen sensors.
    for _index in range(8):
        LIVE_PIDS[_base + _index] = (f"Wideband lambda {_index + 1}", "", lambda d: word(d) / 32768)
for _index, _pid in enumerate(range(0x14, 0x1C)):
    LIVE_PIDS[_pid] = (f"O2 voltage B{_index // 4 + 1}S{_index % 4 + 1}", "V", lambda d: d[0] / 200)

# Read on every update; everything else takes turns so the fast values stay responsive.
FAST_PIDS = (0x0C, 0x0D, 0x11, 0x04)
SLOW_PER_STEP = 2
# When the car accepts several values in one request, far more of the slow ones fit in each update.
GROUP_SLOW_PER_STEP = 8
MAX_PIDS_PER_REQUEST = 6
# Some adapters only manage a shorter list, so try these sizes before giving up on grouping.
GROUP_SIZES = (MAX_PIDS_PER_REQUEST, 3, 2)
# Room in one CAN frame for grouped values, each taking its PID byte plus its data. A reply that fits
# comes back about one Bluetooth interval sooner, measured at 240 ms against 300 ms on a real car.
SINGLE_FRAME_GROUP_BYTES = 6
GROUP_MISSES_ALLOWED = 3
GROUP_RETRY_SECONDS = 30
# Replies a value's usual module can miss before another module's answer is used instead.
SOURCE_MISSES_ALLOWED = 3
# Reads of a value before trusting how many modules answer it, and the largest reply in one CAN frame.
READS_BEFORE_COUNTING = 3
SINGLE_FRAME_DATA_BYTES = 5
# A value older than this is marked as old. Slow values normally come round about every 20 seconds.
FAST_STALE_SECONDS = 5
SLOW_STALE_SECONDS = 30
# PIDs that list other PIDs rather than holding a measurement.
INDEX_PIDS = set(range(0x00, 0x100, 0x20))
# Self-tests for this drive. The community definition reads these flags the opposite way round.
THIS_DRIVE_PID = 0x41
# How often a long session rereads the check engine light and fault codes.
REFRESH_SECONDS = 60
# Alternators on many modern cars dip for short periods, so only a lasting problem gets an alert.
CHARGING_ALERT_SECONDS = 60
DRIVE_SAVE_SECONDS = 30
# Some cars stop charging for minutes at a time to save fuel, resting near battery voltage, so wait longer
# there. Below the discharging level the battery is really being drained, so the shorter wait applies.
CHARGING_RESTING_ALERT_SECONDS = 600
CHARGING_DISCHARGING_VOLTS = 12.2
CHARGING_LOW_VOLTS = 12.8
CHARGING_HIGH_VOLTS = 15.5

COMMON_MONITORS = ["Misfire", "Fuel system", "Components"]
SPARK_MONITORS = ["Catalyst", "Heated catalyst", "Evaporative system", "Secondary air",
                  "A/C refrigerant", "Oxygen sensor", "Oxygen sensor heater", "EGR or VVT"]
DIESEL_MONITORS = ["NMHC catalyst", "NOx aftertreatment", None, "Boost pressure",
                   None, "Exhaust gas sensor", "Particulate filter", "EGR or VVT"]


class OBDError(Exception):
    """An error for the user. Keeps its English text so the page can show it in either language."""

    def __init__(self, text, **values):
        self.text = text
        self.values = values
        super().__init__(t(text, **values))


class Reply(bytes):
    """A module's reply. `complete` is False if frames were lost, repeated or out of order, so the rest was dropped."""

    complete = True


def parse_can(lines):
    """Rebuild each module's reply from CAN lines, joining multi-frame messages."""
    frames = {}
    for line in lines:
        line = line.strip().upper()
        if CAN_LINE.match(line):
            header, *data = line.split()
            frames.setdefault(header, []).append(bytes(int(b, 16) for b in data))

    replies = {}
    for header in sorted(frames):
        payload, length, next_number, broken = bytearray(), None, None, False
        for frame in frames[header]:
            kind = frame[0] >> 4
            if kind == 2 and (next_number is None or (length is not None and len(payload) >= length)):
                continue  # Not part of a message in progress, so it changes nothing.
            if kind == 0:
                # A new message replaces an earlier one, such as "please wait" before the answer.
                length, next_number, broken = frame[0] & 0x0F, None, False
                payload = bytearray(frame[1:])
            elif kind == 1 and len(frame) > 1:
                length, next_number, broken = ((frame[0] & 0x0F) << 8) | frame[1], 1, False
                payload = bytearray(frame[2:])
            elif kind == 2:
                # Frames are numbered, so a lost or repeated one must not shift the bytes after it into the wrong place.
                if frame[0] & 0x0F == next_number:
                    payload.extend(frame[1:])
                    next_number = (next_number + 1) % 16
                else:
                    broken = True
                    next_number = -1
        if length is None:
            continue  # only flow control or stray frames, so this module sent no message
        reply = Reply(payload[:length])
        reply.complete = not broken and length > 0 and len(payload) >= length
        replies[header] = reply
    return replies


def decode_dtcs(payload):
    """Decode a mode 03/07/0A reply: response byte, count byte, then 2 bytes per code."""
    codes = []
    body = payload[2:]
    for i in range(0, len(body) - 1, 2):
        a, b = body[i], body[i + 1]
        if a or b:
            codes.append(dtc_name(a, b))
    return codes


def describe_dtc(code, lang=None):
    lang = lang or i18n.current_language()
    description = lookup_dtc(code, lang)
    if description:
        return description
    meaning = oem_meaning(code)
    if meaning:
        return f"{meaning} ({car_make()})"
    system = t({"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}[code[0]], lang)
    if code[:2] in ("P1", "B1", "B2", "C1", "C2", "U1", "U2") or code[:3] in ("P30", "P31", "P32", "P33"):
        return t("{system}: manufacturer-specific code, look it up for your car's make", lang, system=system)
    return t("{system}: generic code not in this tool's table, look it up online", lang, system=system)


def decode_monitors(payload, diesel=None):
    """Decode a mode 01 PID 01 reply into light state, code count and self-tests.

    Only PID 01 says whether the engine is diesel, so other replies in this shape need to be told.
    """
    a, b, c, d = payload[2:6]
    diesel = bool(b & 0x08) if diesel is None else diesel
    tests = []
    for bit, name in enumerate(COMMON_MONITORS):
        if b & (1 << bit):
            tests.append((name, not b & (1 << (bit + 4))))
    for bit, name in enumerate(DIESEL_MONITORS if diesel else SPARK_MONITORS):
        if name and c & (1 << bit):
            tests.append((name, not d & (1 << bit)))
    return {"mil": bool(a & 0x80), "count": a & 0x7F, "tests": tests}


def split_group(payload, sizes, wanted):
    """Split a reply to several values into {pid: data}. Anything unexpected returns nothing rather than a guess."""
    found, position = {}, 1
    if not payload or payload[0] != 0x41 or not getattr(payload, "complete", True):
        return {}
    while position < len(payload):
        pid = payload[position]
        end = position + 1 + sizes.get(pid, len(payload))
        if pid not in wanted or end > len(payload):
            return {}
        found[pid] = bytes(payload[position + 1:end])
        position = end
    return found


def format_value(value):
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def decode_text(payload):
    """Decode a mode 09 reply: response byte, PID byte, item count, then ASCII."""
    return "".join(chr(b) for b in payload[3:] if 32 < b < 127)


def bits_to_pids(data, base):
    bits = int.from_bytes(data[:4], "big")
    return {base + i + 1 for i in range(32) if bits & (1 << (31 - i))}


class BleTransport:
    """Sends a text command over BLE and collects the reply up to the '>' prompt."""

    def __init__(self, client, write_char, notify_char):
        self.client = client
        self.write_char = write_char
        self.notify_char = notify_char
        self.with_response = "write-without-response" not in write_char.properties
        self.buffer = bytearray()
        self.prompt = asyncio.Event()

    def on_data(self, _, data):
        self.buffer.extend(data)
        if b">" in data:
            self.prompt.set()

    async def send(self, command, wait=5):
        """Return the reply lines, or None if the prompt never came."""
        self.buffer.clear()
        self.prompt.clear()
        await self.client.write_gatt_char(self.write_char, (command + "\r").encode(), response=self.with_response)
        try:
            await asyncio.wait_for(self.prompt.wait(), wait)
        except asyncio.TimeoutError:
            return None
        text = self.buffer.decode("ascii", "replace").replace(">", "").replace("\x00", "")  # some adapters send stray NULs
        return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


ADAPTER_SILENT = "The adapter stopped answering. Connecting again. If this repeats, unplug the adapter and plug it back in."
ADAPTER_SILENT_LIMIT = 3
ACTIVITY_KEPT = 400
# Replies that identify the car stay out of the live log, since people share screenshots of it.
IDENTITY_COMMANDS = ("0902", "0904", "0906", "22F190", "22F18C")
TROUBLE_REPLY = re.compile(r"NO DATA|ERROR|UNABLE|STOPPED|BUFFER FULL|BUS BUSY|LV RESET|LP ALERT|ERR\d\d|^\?$")
# What the adapter says when the fault is in the connection to the car, rather than the car being quiet.
BUS_PROBLEM_REPLY = re.compile(r"BUS BUSY|BUS ERROR|CAN ERROR|DATA ERROR|FB ERROR|RX ERROR|ERR\d\d|LP ALERT|UNABLE TO CONNECT")
ADAPTER_RESET_REPLY = "LV RESET"
BUS_PROBLEM = "The adapter reported '{problem}' on its connection to the car. Check it's firmly in the port, with the ignition ON, then try again."
ADAPTER_RESTARTED = "The adapter restarted after a voltage drop, which is normal while the engine cranks. Connecting again."
SLOW_REPLY_MS = 400
TROUBLE_SHARE_PERCENT = 10


class Activity:
    """The recent commands, replies and connection changes, for the live log page."""

    def __init__(self):
        self.entries = deque(maxlen=ACTIVITY_KEPT)
        self.next = 1
        self.sent = self.lost = self.no_data = 0

    def add(self, **entry):
        self.entries.append({"n": self.next, "time": datetime.now().strftime("%H:%M:%S.%f")[:-3], **entry})
        self.next += 1

    def event(self, text, tone="", **values):
        self.add(kind="event", text=text, values=values, tone=tone)

    def command(self, command, reply, milliseconds):
        self.sent += 1
        trouble = reply is not None and any(TROUBLE_REPLY.search(line) for line in reply)
        self.lost += reply is None
        self.no_data += trouble
        shown = [] if reply is None or command in IDENTITY_COMMANDS else reply
        self.add(kind="command", command=command, reply=shown, ms=milliseconds, hidden=reply is not None and command in IDENTITY_COMMANDS,
                 state="lost" if reply is None else "trouble" if trouble else "ok")

    def view(self, after, lang=None):
        """Return entries newer than `after`, in the page's language."""
        entries = []
        for entry in self.entries:
            if entry["n"] > after:
                entries.append({**entry, "text": t(entry["text"], lang, **entry["values"])} if entry["kind"] == "event" else entry)
        return {"entries": entries, "review": self.review(lang)}

    def review(self, lang=None):
        """Look over the recent traffic and say what stands out."""
        commands = [e for e in self.entries if e["kind"] == "command"]
        answered = [e["ms"] for e in commands if e["state"] != "lost"]
        lost = sum(e["state"] == "lost" for e in commands)
        trouble = sum(e["state"] == "trouble" for e in commands)
        modules = sorted({line[:3] for e in commands for line in e["reply"] if CAN_LINE.match(line)})
        average = round(sum(answered) / len(answered)) if answered else None
        findings = []
        if lost:
            findings.append({"text": t("{count} recent requests got no reply from the adapter.", lang, count=lost), "tone": "bad"})
        if commands and trouble * 100 / len(commands) >= TROUBLE_SHARE_PERCENT:
            findings.append({"text": t("The car didn't answer {percent}% of recent requests.", lang,
                                       percent=round(trouble * 100 / len(commands))), "tone": "bad"})
        if average is not None and average >= SLOW_REPLY_MS:
            findings.append({"text": t("Replies are slower than usual, at {ms} ms.", lang, ms=average), "tone": "bad"})
        if not findings:
            text = "Recent traffic looks normal." if commands else "Nothing has been sent yet."
            findings.append({"text": t(text, lang), "tone": "good" if commands else ""})
        return {"findings": findings, "rows": [
            {"label": t("Commands sent", lang), "text": str(self.sent), "tone": ""},
            {"label": t("Replies lost", lang), "text": str(self.lost), "tone": "bad" if self.lost else "good"},
            {"label": t("No data answers", lang), "text": str(self.no_data), "tone": ""},
            {"label": t("Average reply time", lang), "text": "-" if average is None else t("{ms} ms", lang, ms=average), "tone": ""},
            {"label": t("Modules answering", lang), "text": ", ".join(modules) or "-", "tone": ""}]}


class MeasuredTransport:
    """Passes commands to the adapter while recording how it responds."""

    def __init__(self, transport):
        self.transport = transport
        self.activity = None
        self.sent = self.replied = self.timeouts = 0
        self.silent_in_a_row = 0
        self.total_seconds = 0.0
        self.last_reply = None
        self.recorder = car_copy.CarRecorder()
        self.last_problem = None

    async def send(self, command, wait=5):
        started = time.monotonic()
        self.sent += 1
        reply = await self.transport.send(command, wait)
        self.recorder.record(command, reply)
        for line in reply or []:
            found = BUS_PROBLEM_REPLY.search(line)
            self.last_problem = found.group() if found else self.last_problem
            if ADAPTER_RESET_REPLY in line and command.upper() != "ATZ":
                raise OBDError(ADAPTER_RESTARTED)  # It lost its settings, so they must be sent again.
        if self.activity is not None:
            self.activity.command(command, reply, round((time.monotonic() - started) * 1000))
        if reply is None:
            self.timeouts += 1
            self.silent_in_a_row += 1
            if self.silent_in_a_row >= ADAPTER_SILENT_LIMIT:
                raise OBDError(ADAPTER_SILENT)  # Waiting out every remaining request could take minutes.
        else:
            self.silent_in_a_row = 0
            self.replied += 1
            self.total_seconds += time.monotonic() - started
            self.last_reply = time.monotonic()
        return reply


CAR_SILENT = "The car did not answer. Check the ignition is ON and the adapter is seated."
PID_RANGE_ATTEMPTS = 4
MODULE_BUSY_ATTEMPTS = 3
# A module that needs longer replies 7F <service> 78, then has up to 5 seconds (ISO 15765-3), plus 20% tolerance, before
# it must answer or say so again. One real engine module repeated this for 30 seconds working out its calibration check.
RESPONSE_PENDING = 0x78
BUSY_SECONDS = 6


class ELM:
    def __init__(self, transport):
        self.transport = MeasuredTransport(transport)
        self.version = self.protocol = ""
        self.description = None
        self.device_name = self.device_address = None
        self.vehicle = None
        self.demo = False
        self.can_group = None  # None until tried on this car
        self.group_misses = 0
        self.group_size = MAX_PIDS_PER_REQUEST
        self.group_off_at = None  # when grouping stopped after it had been working
        self.sources = {}  # the module each value is read from, so it doesn't jump between modules
        self.source_misses = {}
        self.sizes = {}  # bytes each value used when this car answered it alone
        self.use_counts = True
        self.reply_counts = {}
        self.reads_seen = {}
        self.addressed = None
        self.busy = {}  # when each module last said it needs more time
        self.heard = {}  # when each module last answered anything
        self.last_scan = self.identity_items = None  # results of the scan and identity commands, for the page
        self.explorer = None  # the latest explorer map, and the one before it for comparing
        self.calibration = {}  # filled in when asked, or whenever a slow module finally answers

    def parsed(self, lines):
        """Rebuild replies from CAN lines, keeping track of modules that say they need more time."""
        replies = parse_can(lines or [])
        for header, reply in replies.items():
            self.heard[header] = time.monotonic()
            if len(reply) >= 3 and reply[0] == 0x7F and reply[2] == RESPONSE_PENDING:
                self.busy[header] = time.monotonic()
                continue
            self.busy.pop(header, None)
            if reply[:2] == bytes([0x49, 0x06]) and reply.complete:
                self.calibration[header] = reply[3:].hex().upper()
        return replies

    def busy_modules(self):
        now = time.monotonic()
        return {header for header, seen in self.busy.items() if now - seen <= BUSY_SECONDS}

    async def close(self):
        """Ask the adapter to stop talking to the car before letting go. Letting go ends it anyway if this gets no answer."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.transport.send("ATPC", 1), 2)

    async def setup(self):
        reply = await self.transport.send("ATZ", 5)
        if not reply or not any("ELM" in line for line in reply):
            raise OBDError("Adapter did not identify itself (reply: {reply})", reply=reply)
        self.version = next(line for line in reply if "ELM" in line)
        for command in ("ATE0", "ATL0", "ATS1", "ATH1", "ATSP0"):
            await self.transport.send(command, 2)
        description = await self.transport.send("AT@1", 2)
        if description and description[0] not in ("?", "OK", "NO DATA"):
            self.description = description[0]

        self.transport.last_problem = None
        if not await self.request(0x01, 0x00, wait=25):
            if self.transport.last_problem:
                raise OBDError(BUS_PROBLEM, problem=self.transport.last_problem)
            raise OBDError(CAR_SILENT)

        number = "".join(await self.transport.send("ATDPN", 2) or []).lstrip("A")
        if number in ("", "0"):
            # Some adapters don't say, but only a car on 11-bit CAN could have given the reply just read.
            self.protocol = "ISO 15765-4 CAN 11-bit"
            return
        if number not in SUPPORTED_PROTOCOLS:
            raise OBDError("This tool handles 11-bit CAN only; the car uses protocol '{number}'.", number=number)
        self.protocol = SUPPORTED_PROTOCOLS[number]

    async def request(self, mode, pid=None, wait=5, frame=None):
        """Return {module header: payload}, retrying once if a reply is lost or stale."""
        command = f"{mode:02X}" + ("" if pid is None else f"{pid:02X}") + ("" if frame is None else f"{frame:02X}")
        expected = bytes([mode + 0x40] + ([] if pid is None else [pid]))
        best = {}
        for _ in range(2):
            lines = await self.transport.send(command, wait)
            if lines is None:
                continue
            good = {h: p for h, p in self.parsed(lines).items() if p.startswith(expected)}
            best.update({h: p for h, p in good.items() if h not in best or not best[h].complete})
            if good and all(p.complete for p in best.values()):
                return best
            if not good and any("NO DATA" in line for line in lines):
                return best
        return best

    async def request_all(self, mode, pid=None, attempts=3, max_attempts=6):
        """Ask several times and combine the replies, since a module occasionally misses a request.

        Keeps asking, up to a limit, until every module this car is known to have has answered.
        """
        known = set((self.vehicle or {}).get("Module name", {}))
        replies = {}
        for attempt in range(max_attempts):
            replies.update(await self.request(mode, pid))
            if attempt + 1 >= attempts and known <= set(replies):
                break
            missing = known - set(replies)
            if missing:
                await asyncio.sleep(1 if missing & self.busy_modules() else 0.2)
        return dict(sorted(replies.items()))

    async def module_request(self, hdr, rax, request, fc=False, wait=2, keep_address=False):
        """Send a read request to one module. Returns its reply or refusal, or None if silent.

        keep_address skips setting the address again when the last request went to the same module, which saves two
        adapter commands. Only use it in a run of requests that ends with restore_addressing.
        """
        if request[:2] not in car_modules.READ_ONLY_SERVICES:
            raise OBDError("Refusing to send '{request}': only read requests are allowed.", request=request)
        if not (keep_address and self.addressed == (hdr, rax)):
            for command in (f"ATSH{hdr}", f"ATCRA{rax}"):
                await self.transport.send(command, 2)
        self.addressed = (hdr, rax)
        if fc:
            for command in (f"ATFCSH{hdr}", "ATFCSD300000", "ATFCSM1"):
                await self.transport.send(command, 2)
        service = int(request[:2], 16)
        reply = None
        for attempt in range(MODULE_BUSY_ATTEMPTS):
            lines = await self.transport.send(request, wait)
            reply = self.parsed(lines).get(rax)
            if not reply or len(reply) < 3 or reply[:2] != bytes([0x7F, service]) or reply[2] not in car_modules.TRY_AGAIN:
                break
            if attempt + 1 < MODULE_BUSY_ATTEMPTS:
                await asyncio.sleep(car_modules.TRY_AGAIN[reply[2]])
        if fc:
            await self.transport.send("ATFCSM0", 2)
        if not reply or (reply[0] != service + 0x40 and reply[:2] != bytes([0x7F, service])):
            return None
        return reply

    async def restore_addressing(self):
        """Go back to asking all modules, so standard requests work again."""
        self.addressed = None
        for command in ("ATCRA", "ATFCSM0", "ATSH7DF"):
            await self.transport.send(command, 2)

    async def voltage(self):
        reply = await self.transport.send("ATRV", 2)
        return reply[0] if reply else "unknown"

    async def supported_pids(self):
        """Walk the 'supported PIDs' ranges and return the set across all modules."""
        supported = set()
        base = 0x00
        while base <= 0xE0:
            # A car can miss a request, and a missed first range would hide every live value.
            for _ in range(PID_RANGE_ATTEMPTS):
                replies = await self.request(0x01, base)
                if replies:
                    break
            if not replies:
                break
            for payload in replies.values():
                supported |= bits_to_pids(payload[2:6], base)
            if base + 0x20 not in supported:
                break
            base += 0x20
        return supported

    async def counted_request(self, pid):
        """Ask for one value, saying how many replies to expect once that's known, which skips the adapter's wait."""
        count = self.reply_counts.get(pid)
        if self.use_counts and count:
            lines = await self.transport.send(f"01{pid:02X}{count:X}", 5)
            good = {h: p for h, p in self.parsed(lines).items() if p.startswith(bytes([0x41, pid]))}
            if good:
                return good
            if lines and any("?" in line for line in lines):
                self.use_counts = False  # this adapter doesn't understand it
            self.reply_counts.pop(pid, None)
        replies = await self.request(0x01, pid)
        if replies and self.use_counts and all(len(p) - 2 <= SINGLE_FRAME_DATA_BYTES for p in replies.values()):
            reads, most = self.reads_seen.get(pid, (0, 0))
            self.reads_seen[pid] = (reads + 1, max(most, len(replies)))
            if reads + 1 >= READS_BEFORE_COUNTING:
                self.reply_counts[pid] = self.reads_seen[pid][1]
        return replies

    def from_usual_module(self, pid, answers):
        """Pick a value from {module: value}, or None if its usual module missed this reply.

        Modules can report the same value differently, so it must not jump to another one after a miss.
        The lowest address wins, which is the engine module on the cars seen so far.
        """
        if not answers:
            return None
        source, lowest = self.sources.get(pid), min(answers)
        if source is not None and source not in answers and lowest > source:
            if source in self.busy_modules():
                return None  # It said it would be back, so keep its last reading.
            self.source_misses[pid] = self.source_misses.get(pid, 0) + 1
            if self.source_misses[pid] < SOURCE_MISSES_ALLOWED:
                return None
        self.sources[pid] = lowest if source is None or source not in answers or lowest < source else source
        self.source_misses[pid] = 0
        return answers[self.sources[pid]]

    async def read_pid(self, pid):
        """Read a live value from its usual module, or None."""
        replies = await self.counted_request(pid)
        answers = {}
        for header, payload in replies.items():
            try:
                answers[header] = LIVE_PIDS[pid][2](payload[2:])
            except IndexError:
                continue
            self.sizes.setdefault(pid, len(payload) - 2)
        return self.from_usual_module(pid, answers)

    async def read_group(self, pids):
        """Ask for several values in one request. Returns the ones that came back clearly."""
        wanted = set(pids)
        lines = await self.transport.send("01" + "".join(f"{pid:02X}" for pid in pids), 2)
        answers = {}
        for header, payload in sorted(self.parsed(lines).items()):
            for pid, data in split_group(payload, self.sizes, wanted).items():
                try:
                    answers.setdefault(pid, {})[header] = LIVE_PIDS[pid][2](data)
                except IndexError:
                    continue
        # A value whose usual module missed this reply stays None, so it keeps its last reading without asking again.
        found = {pid: self.from_usual_module(pid, modules) for pid, modules in answers.items()}
        if len(answers) >= 2:  # any module answering the whole list proves grouping works
            self.can_group, self.group_misses = True, 0
        elif self.can_group is None:
            # One lost reply doesn't prove anything, so only a clear refusal or the shortest list ends the trial.
            shorter = next((size for size in GROUP_SIZES if size < self.group_size), None)
            if shorter is None or any("?" in line for line in lines or []):
                self.can_group = False
            else:
                self.group_size = shorter
        elif not self.busy_modules():
            self.group_misses += 1
            if self.group_misses >= GROUP_MISSES_ALLOWED:
                self.can_group, self.group_off_at = False, time.monotonic()
        return found

    def next_group(self, pids):
        """Choose the next values to ask for together. Adapters limited to short lists get single-frame replies."""
        if self.group_size >= MAX_PIDS_PER_REQUEST:
            return pids[:self.group_size]
        chunk, room = [], SINGLE_FRAME_GROUP_BYTES
        for pid in sorted(pids, key=lambda p: self.sizes[p], reverse=True):
            if len(chunk) < self.group_size and 1 + self.sizes[pid] <= room:
                chunk.append(pid)
                room -= 1 + self.sizes[pid]
        return chunk or pids[:1]

    async def read_pids(self, pids):
        """Read several values the fastest way this car allows, and anything missed one at a time."""
        values = {}
        known = [pid for pid in pids if pid in self.sizes]
        # A car that accepted groups before may only have gone quiet for a moment, so try once more now and then.
        if self.can_group is False and self.group_off_at is not None and time.monotonic() - self.group_off_at >= GROUP_RETRY_SECONDS:
            self.can_group, self.group_misses = True, GROUP_MISSES_ALLOWED - 1
        while self.can_group is not False and len(known) >= 2:
            chunk = self.next_group(known)
            known = [pid for pid in known if pid not in chunk]
            if len(chunk) >= 2:
                values.update(await self.read_group(chunk))
        for pid in pids:
            if pid not in values:
                values[pid] = await self.read_pid(pid)
        return {pid: values[pid] for pid in pids}


def monitor_lines(status, header, indent="  "):
    light = t("ON") if status["mil"] else t("off")
    lines = [t("Module {header}: check engine light {light}, {count} code(s)",
               header=header, light=light, count=status["count"])]
    lines += [f"{indent}{t(name):22} {t('finished') if done else t('NOT finished')}" for name, done in status["tests"]]
    return lines


async def run_info(elm, _):
    print(f"{t('Adapter')}: {elm.version}")
    print(f"{t('Protocol')}: {elm.protocol}")
    print(f"{t('Voltage at adapter (approximate)')}: {await elm.voltage()}")
    for header, payload in (await elm.request_all(0x01, 0x01)).items():
        print()
        print("\n".join(monitor_lines(decode_monitors(payload), header)))
    supported = await elm.supported_pids()
    print(f"\n{t('Live values available')}: {', '.join(t(LIVE_PIDS[p][0]) for p in LIVE_PIDS if p in supported)}")
    print(f"{t('All supported PIDs')}: {' '.join(f'{p:02X}' for p in sorted(supported))}")


CODE_MODES = (("Stored", 0x03), ("Pending", 0x07), ("Permanent", 0x0A))


async def read_codes(elm):
    """Return {label: {module: [codes]}} for stored, pending and permanent codes."""
    result = {}
    for label, mode in CODE_MODES:
        result[label] = {h: decode_dtcs(p) for h, p in (await elm.request_all(mode)).items()}
    return result


def known_modules(vehicle):
    return sorted((vehicle or {}).get("Module name", {}))


async def read_module_records(elm, vehicle=None):
    """Read the fault records kept inside each known module, which can include faults no longer active."""
    ids = [int(header, 16) - 8 for header in known_modules(vehicle or elm.vehicle) if re.fullmatch(r"7E[89A-F]", header)]
    records = await car_modules.scan_modules(elm, ids=ids) if ids else []
    await car_modules.read_fault_order(elm, records)
    return records


# Mode 06 units from SAE J1979: unit ID: (signed, scale, unit, offset)
TEST_UNITS = {
    0x01: (False, 1, ""), 0x02: (False, 0.1, ""), 0x03: (False, 0.01, ""), 0x04: (False, 0.001, ""),
    0x05: (False, 0.0000305, ""), 0x06: (False, 0.000305, ""), 0x07: (False, 0.25, "rpm"), 0x08: (False, 0.01, "km/h"),
    0x09: (False, 1, "km/h"), 0x0A: (False, 0.122, "mV"), 0x0B: (False, 0.001, "V"), 0x0C: (False, 0.01, "V"),
    0x0D: (False, 0.00390625, "mA"), 0x0E: (False, 0.001, "A"), 0x0F: (False, 0.01, "A"), 0x10: (False, 1, "ms"),
    0x11: (False, 100, "ms"), 0x12: (False, 1, "s"), 0x13: (False, 1, "mOhm"), 0x14: (False, 1, "Ohm"),
    0x15: (False, 1, "kOhm"), 0x16: (False, 0.1, "C", -40), 0x17: (False, 0.01, "kPa"), 0x18: (False, 0.0117, "kPa"),
    0x19: (False, 0.079, "kPa"), 0x1A: (False, 1, "kPa"), 0x1B: (False, 10, "kPa"), 0x1C: (False, 0.01, "deg"),
    0x1D: (False, 0.5, "deg"), 0x1E: (False, 0.0000305, ""), 0x1F: (False, 0.05, ""), 0x20: (False, 0.00390625, ""),
    0x21: (False, 1, "mHz"), 0x22: (False, 1, "Hz"), 0x23: (False, 1, "kHz"), 0x24: (False, 1, ""), 0x25: (False, 1, "km"),
    0x26: (False, 0.1, "mV/ms"), 0x27: (False, 0.01, "g/s"), 0x28: (False, 1, "g/s"), 0x29: (False, 0.25, "Pa/s"),
    0x2A: (False, 0.001, "kg/h"), 0x2B: (False, 1, ""), 0x2C: (False, 0.01, "g/cyl"), 0x2D: (False, 0.01, "mg/stroke"),
    0x2E: (False, 1, ""), 0x2F: (False, 0.01, "%"), 0x30: (False, 0.001526, "%"), 0x31: (False, 0.001, "L"),
    0x32: (False, 0.0000305, "in"), 0x33: (False, 0.00024414, ""), 0x34: (False, 1, "min"), 0x35: (False, 10, "ms"),
    0x36: (False, 0.01, "g"), 0x37: (False, 0.1, "g"), 0x38: (False, 1, "g"), 0x39: (False, 0.01, "%", -327.68),
    0x3A: (False, 0.001, "g"), 0x3B: (False, 0.0001, "g"), 0x3C: (False, 0.1, "us"), 0x3D: (False, 0.01, "mA"),
    0x3E: (False, 0.00006103516, "mm2"), 0x3F: (False, 0.01, "L"), 0x40: (False, 1, "ppm"), 0x41: (False, 0.01, "uA"),
    0x81: (True, 1, ""), 0x82: (True, 0.1, ""), 0x83: (True, 0.01, ""), 0x84: (True, 0.001, ""), 0x85: (True, 0.0000305, ""),
    0x86: (True, 0.000305, ""), 0x87: (True, 1, "ppm"), 0x8A: (True, 0.122, "mV"), 0x8B: (True, 0.001, "V"),
    0x8C: (True, 0.01, "V"), 0x8D: (True, 0.00390625, "mA"), 0x8E: (True, 0.001, "A"), 0x90: (True, 1, "ms"),
    0x96: (True, 0.1, "C"), 0x99: (True, 0.1, "kPa"), 0x9C: (True, 0.01, "deg"), 0x9D: (True, 0.5, "deg"),
    0xA8: (True, 1, "g/s"), 0xA9: (True, 0.25, "Pa/s"), 0xAD: (True, 0.01, "mg/stroke"), 0xAE: (True, 0.1, "mg/stroke"),
    0xAF: (True, 0.01, "%"), 0xB0: (True, 0.003052, "%"), 0xB1: (True, 2, "mV/s"), 0xFC: (True, 0.01, "kPa"),
    0xFD: (True, 0.001, "kPa"), 0xFE: (True, 0.25, "Pa"),
}
# What the standard test numbers mean for oxygen sensor monitors. Higher numbers are defined by each maker.
O2_TEST_NAMES = {0x01: "Rich to lean threshold", 0x02: "Lean to rich threshold", 0x03: "Low voltage for switch time", 0x04: "High voltage for switch time",
                 0x05: "Rich to lean switch time", 0x06: "Lean to rich switch time", 0x07: "Lowest sensor voltage", 0x08: "Highest sensor voltage",
                 0x09: "Time between switches", 0x0A: "Sensor period"}
TEST_RESULT_BYTES = 9
MAX_MONITORS = 40


def monitor_name(monitor, lang=None):
    """Name a mode 06 monitor from its standard ID."""
    if 0x01 <= monitor <= 0x10:
        return t("Oxygen sensor B{bank}S{sensor}", lang, bank=(monitor - 1) // 4 + 1, sensor=(monitor - 1) % 4 + 1)
    if 0x41 <= monitor <= 0x50:
        return t("Oxygen sensor heater B{bank}S{sensor}", lang, bank=(monitor - 0x41) // 4 + 1, sensor=(monitor - 0x41) % 4 + 1)
    if 0xA2 <= monitor <= 0xAD:
        return t("Misfire, cylinder {number}", lang, number=monitor - 0xA1)
    single = {0x39: "Evaporative leak, 0.150 inch", 0x3A: "Evaporative leak, 0.090 inch", 0x3B: "Evaporative leak, 0.040 inch",
              0x3C: "Evaporative leak, 0.020 inch", 0x3D: "Evaporative purge flow", 0xA1: "Misfire, all cylinders"}
    if monitor in single:
        return t(single[monitor], lang)
    banks = ((0x21, 0x24, "Catalyst, bank {bank}"), (0x31, 0x34, "EGR, bank {bank}"), (0x35, 0x38, "Variable valve timing, bank {bank}"),
             (0x61, 0x64, "Heated catalyst, bank {bank}"), (0x71, 0x74, "Secondary air, bank {bank}"), (0x81, 0x84, "Fuel system, bank {bank}"),
             (0x85, 0x88, "Boost pressure, bank {bank}"), (0x90, 0x91, "NOx adsorber, bank {bank}"), (0x98, 0x99, "NOx catalyst, bank {bank}"),
             (0xB2, 0xB3, "Particulate filter, bank {bank}"))
    for first, last, text in banks:
        if first <= monitor <= last:
            return t(text, lang, bank=monitor - first + 1)
    return t("Monitor {number}", lang, number=f"{monitor:02X}")


def scaled_test_value(unit_id, data):
    """Turn a 2-byte mode 06 value into its real number, or None if its unit isn't in the standard list."""
    unit = TEST_UNITS.get(unit_id)
    if unit is None:
        return None
    value = int.from_bytes(data, "big", signed=unit[0]) * unit[1]
    return value + unit[3] if len(unit) > 3 else value


def decode_test_results(header, payload):
    """Split a mode 06 reply into results. Each is a monitor, a test, a unit, then value, lowest and highest allowed."""
    if not getattr(payload, "complete", True):
        return []
    results = []
    body = payload[1:]
    for start in range(0, len(body) - TEST_RESULT_BYTES + 1, TEST_RESULT_BYTES):
        monitor, test, unit_id, *rest = body[start:start + TEST_RESULT_BYTES]
        data = bytes(rest)
        value, low, high = (scaled_test_value(unit_id, data[i:i + 2]) for i in (0, 2, 4))
        if value is None:
            state = "unknown"
        elif not any(data):
            state = "not_run"  # A test that hasn't run yet reports nothing but zeros.
        else:
            state = "pass" if low <= value <= high else "fail"
        results.append({"module": header, "monitor": monitor, "test": test, "value": value, "low": low, "high": high,
                        "unit": TEST_UNITS.get(unit_id, (False, 1, ""))[2], "state": state,
                        # A limit of all zeros or all ones is the end of the range, so it isn't really a limit.
                        "open_low": not any(data[2:4]), "open_high": data[4:6] == b"\xff\xff"})
    return results


async def read_test_results(elm):
    """Read the car's detailed self-test results, if it offers them."""
    supported, base = set(), 0x00
    while base <= 0xE0:
        replies = await elm.request(0x06, base)
        if not replies:
            break
        for payload in replies.values():
            supported |= bits_to_pids(payload[2:6], base)
        if base + 0x20 not in supported:
            break
        base += 0x20
    results = []
    for monitor in sorted(supported - set(range(0, 0x100, 0x20)))[:MAX_MONITORS]:
        for header, payload in (await elm.request(0x06, monitor)).items():
            results += decode_test_results(header, payload)
    return results


def test_number(value):
    return "-" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def page_test_results(results, lang=None):
    """Group self-test results by monitor for the page, with any outside their limits first."""
    groups = {}
    for result in results or []:
        group = groups.setdefault(result["monitor"], {"monitor": f"{result['monitor']:02X}", "name": monitor_name(result["monitor"], lang), "tests": []})
        unit = t(result["unit"], lang) if result["unit"] else ""
        known_name = O2_TEST_NAMES.get(result["test"]) if 0x01 <= result["monitor"] <= 0x10 else None
        group["tests"].append({"module": result["module"], "test": t(known_name, lang) if known_name else t("Test {number}", lang, number=f"{result['test']:02X}"),
                               "value": f"{test_number(result['value'])} {unit}".strip(),
                               "limits": f"{test_number(result['low'])} - {test_number(result['high'])}", "state": result["state"]})
    return sorted(groups.values(), key=lambda g: (not any(test["state"] == "fail" for test in g["tests"]), g["monitor"]))


def page_module_records(records, lang=None):
    return [{"module": record["hdr"], "name": t(record["name"], lang), "read_codes": record["read_codes"], "complete": record["complete"],
             "refusal": t(record["refusal"], lang) if record.get("refusal") else None,
             "first": record.get("first"), "latest": record.get("latest"),
             "codes": [{"code": item["code"], "short": item["code"][:5], "state": item["state"], "system": car_systems.code_system(item["code"]),
                        "details": [t(detail, lang) for detail in item.get("details", [])], "description": describe_dtc(item["code"][:5], lang)}
                       for item in record["codes"]]}
            for record in records or []]


def code_lines(codes, indent="", known=()):
    lines = []
    for label, modules in codes.items():
        found = [(h, c) for h, module_codes in modules.items() for c in module_codes]
        lines.append(indent + t("{label}: {count} code(s), {modules} module(s) answered",
                                label=t(label), count=len(found), modules=len(modules)))
        lines += [f"{indent}  {code}  {describe_dtc(code)}  [{t('module {header}', header=header)}]"
                  for header, code in found]
        # Not answering doesn't mean no faults, so say so. Some cars never answer the permanent request.
        if modules:
            lines += [indent + "  " + t("Module {header} didn't answer this time, so its codes weren't read. Try again.", header=header)
                      for header in known if header not in modules]
    return lines


def print_codes(codes, known=()):
    print("\n".join(code_lines(codes, known=known)))


async def run_codes(elm, _):
    codes = await read_codes(elm)
    print_codes(codes, known_modules(elm.vehicle))
    garage.remember(elm.vehicle, "codes", {"codes": codes})
    print("\n" + t("This covers engine and emissions codes; ABS, airbag and body faults usually need other tools."))


async def read_freeze_frame(elm):
    """Return what the car saved when a stored code was set, or None if nothing was saved."""
    replies = await elm.request(0x02, 0x02, frame=0)
    module, code = next(((h, c) for h, p in replies.items() for c in decode_dtcs(bytes(2) + p[3:5])), (None, None))
    if code is None:
        return None
    supported, base = set(), 0x00
    while base <= 0x40:
        payload = (await elm.request(0x02, base, frame=0)).get(module)
        if not payload:
            break
        supported |= bits_to_pids(payload[3:7], base)
        if base + 0x20 not in supported:
            break
        base += 0x20
    values = []
    for pid in (p for p in LIVE_PIDS if p in supported):
        payload = (await elm.request(0x02, pid, frame=0)).get(module)  # only this module's values describe its fault
        if not payload:
            continue
        name, unit, formula = LIVE_PIDS[pid]
        try:
            value = formula(payload[3:])
        except IndexError:
            continue
        values.append({"name": name, "unit": "" if value == "Not reported" else unit, "text": format_value(value)})
    return {"code": code, "values": values}


async def read_identity(elm):
    """Read what identifies this car: VIN, calibration IDs and module names."""
    vehicle = {}
    for pid, label in ((0x02, "VIN"), (0x04, "Calibration ID"), (0x0A, "Module name")):
        replies = await elm.request_all(0x09, pid)  # a module missed here would be forgotten for the session
        vehicle[label] = {h: decode_text(p) for h, p in replies.items()}
    vehicle["Calibration check"] = elm.calibration
    if not vehicle["VIN"]:
        # Some cars only give the VIN when the engine module is asked directly.
        reply = await elm.module_request("7E0", "7E8", "22F190")
        await elm.restore_addressing()
        if reply and reply.startswith(bytes.fromhex("62F190")) and reply.complete:
            vehicle["VIN"] = {"7E8": "".join(chr(b) for b in reply[3:] if 32 < b < 127)}
    set_car_make(make_from_vin(next(iter(vehicle["VIN"].values()), None)))
    return vehicle


async def read_calibration_check(elm):
    """Ask for the calibration check, once everything else has been read.

    A module can go quiet for about 30 seconds working this out, answering nothing else, and asking again doesn't
    help. So it comes last, and ELM.parsed picks up the answer whenever it arrives.
    """
    for _ in range(3):
        await elm.request(0x09, 0x06)
        if elm.busy_modules():
            break


async def read_this_drive(elm):
    """Read which self-tests have finished this drive. In this data a set bit means not finished."""
    status = await elm.request(0x01, 0x01)
    diesel = any(len(payload) >= 4 and payload[3] & 0x08 for payload in status.values())
    for payload in (await elm.request(0x01, THIS_DRIVE_PID)).values():
        if len(payload) >= 6:
            return decode_monitors(payload, diesel)["tests"]
    return None


async def read_snapshot(elm):
    """Read everything the car offers through standard OBD-II, once."""
    vehicle = elm.vehicle or await read_identity(elm)
    monitors = {h: decode_monitors(p) for h, p in (await elm.request_all(0x01, 0x01)).items() if len(p) >= 6}
    codes = await read_codes(elm)
    has_stored = any(codes["Stored"].values())
    supported = await elm.supported_pids()
    if not supported:
        raise OBDError(CAR_SILENT)  # Connect again rather than stay connected with nothing to read.

    values, raw, more = {}, {}, []
    this_drive = None
    for pid in sorted(supported - INDEX_PIDS - {0x01}):
        if pid in LIVE_PIDS:
            values[pid] = await elm.read_pid(pid)
        elif pid == THIS_DRIVE_PID:
            this_drive = await read_this_drive(elm)
        else:
            replies = await elm.request(0x01, pid)
            payload = next(iter(replies.values()), None)
            decoded = car_modules.decode_standard(pid, payload[2:]) if payload else []
            if decoded:
                more += decoded
            else:
                raw[pid] = payload[2:].hex(" ").upper() if payload else None
    snapshot = {
        "module_records": await read_module_records(elm, vehicle),
        "test_results": await read_test_results(elm),
        "ecu_details": await car_modules.read_ecu_details(elm, [h for h in known_modules(vehicle) if re.fullmatch(r"7E[89A-F]", h)]),
        "this_drive": this_drive,
        "more": more,
        "adapter": elm.version,
        "protocol": elm.protocol,
        "voltage": await elm.voltage(),
        "vehicle": vehicle,
        "monitors": monitors,
        "codes": codes,
        "freeze": await read_freeze_frame(elm) if has_stored else None,
        "supported": supported,
        "values": values,
        "raw": raw,
    }
    if not elm.calibration:
        await read_calibration_check(elm)
    return snapshot


def shown_value(value):
    return "-" if value is None else t(format_value(value))


def format_report(snapshot):
    lines = [t("Car report, {time}", time=f"{datetime.now():%Y-%m-%d %H:%M}"), "",
             f"{t('Adapter')}: {snapshot['adapter']}", f"{t('Protocol')}: {snapshot['protocol']}",
             f"{t('Voltage at adapter (approximate)')}: {snapshot['voltage']}", "", t("VEHICLE")]
    for label, modules in snapshot["vehicle"].items():
        for header, text in modules.items():
            lines.append(f"  {t(label):18} {text}  [{t('module {header}', header=header)}]")
        if not modules:
            lines.append(f"  {t(label):18} {t('not reported')}")

    lines += ["", t("STATUS")]
    for header, status in snapshot["monitors"].items():
        lines += ["  " + line for line in monitor_lines(status, header)]

    lines += ["", t("FAULT CODES")] + code_lines(snapshot["codes"], "  ", known_modules(snapshot["vehicle"]))

    if snapshot["freeze"]:
        freeze = snapshot["freeze"]
        lines += ["", t("FREEZE FRAME for {code} ({description})", code=freeze["code"],
                        description=describe_dtc(freeze["code"]))]
        lines += [f"  {t(v['name']):24} {t(v['text']):>10} {t(v['unit'])}" for v in freeze["values"]]

    lines += ["", t("ALL VALUES")]
    for pid, value in snapshot["values"].items():
        name, unit, _ = LIVE_PIDS[pid]
        lines.append(f"  {pid:02X}  {t(name):24} {shown_value(value):>20} {t(unit)}")
    if snapshot["this_drive"]:
        lines += ["", t("SELF-TESTS THIS DRIVE")]
        lines += [f"  {t(name):22} {t('finished') if done else t('NOT finished')}" for name, done in snapshot["this_drive"]]
    if snapshot["more"]:
        lines += ["", t("MORE STANDARD VALUES (from community definitions, read once)")]
        lines += [f"  {item['name']:60} {item['text']:>16}" for item in snapshot["more"]]
    if snapshot["raw"]:
        lines += ["", t("OTHER DATA (not decoded by this tool, raw bytes)")]
        lines += [f"  {pid:02X}  {text or t('no reply')}" for pid, text in snapshot["raw"].items()]
    lines += ["", t("Standard OBD-II only: ABS, airbag and body modules usually need other tools.")]
    return "\n".join(lines)


EARLIER_VISITS = 6


def earlier_visits(record):
    """Pick out what earlier visits to a car showed, for comparing with this one."""
    sessions = [visit for visit in record.get("sessions", []) if isinstance(visit, dict)][-EARLIER_VISITS:]
    fuel = [visit.get("fuel") for visit in sessions]
    match = re.match(r"[\d.]+", str(record.get("status", {}).get("values", {}).get("Fuel level", "")))
    if match:
        fuel.append(float(match.group()))  # visits saved before fuel was kept per visit
    return {"sessions": sessions, "fuel": [level for level in fuel if number(level) is not None],
            "followup": garage.fault_followup(record), "test_history": record.get("test_history", []),
            "ecu_history": record.get("ecu_history", []), "scan": record.get("modules"), "explorer": record.get("explorer")}


def remember_snapshot(elm, snapshot):
    """Save the latest full reading to this car's record on this laptop."""
    if elm.demo:
        return
    codes = snapshot["codes"]
    active = sorted({c for label in ("Stored", "Pending") for found in codes[label].values() for c in found})
    garage.remember(elm.vehicle or snapshot["vehicle"], "status", {
        "light": any(status["mil"] for status in snapshot["monitors"].values()),
        "active_codes": active,
        "adapter_voltage": snapshot["voltage"],
        "codes": codes,
        "monitors": snapshot["monitors"],
        "freeze": snapshot["freeze"],
        "module_records": snapshot.get("module_records", []),
        "test_results": snapshot.get("test_results", []),
        "since_clear": {"km": number(snapshot["values"].get(0x31)), "warm_ups": number(snapshot["values"].get(0x30))},
        "ecu_ids": ecu_fingerprint(snapshot),
        "supported_pids": sorted(f"{pid:02X}" for pid in snapshot["supported"]),
        "values": {LIVE_PIDS[pid][0]: f"{'-' if value is None else format_value(value)} {LIVE_PIDS[pid][1]}".strip()
                   for pid, value in snapshot["values"].items()},
    })


async def run_report(elm, _):
    print(t("Reading everything, this takes about 20 seconds ..."), flush=True)
    snapshot = await read_snapshot(elm)
    remember_snapshot(elm, snapshot)
    report = format_report(snapshot)
    print(report)
    save_report("report", report, elm.demo)


class LiveReader:
    """Keeps the latest values, reading the fast ones every step and the rest in turn."""

    def __init__(self, elm, pids, values=None, voltage="unknown"):
        self.elm = elm
        self.pids = list(pids)
        self.fast = [p for p in FAST_PIDS if p in self.pids]
        self.slow = [p for p in self.pids if p not in self.fast]
        self.values = dict(values or {})
        self.voltage = voltage
        self.next_slow = 0
        self.interval = 0
        self.updates = 0  # lets the page skip redrawing until there's something new
        now = time.monotonic()
        self.read_at = {pid: now for pid in self.values}
        rpm = number(self.values.get(0x0C))
        self.engine_running = None if rpm is None else rpm > 0
        self.engine_changed_at = float("-inf")

    def store(self, pid, value):
        if value is None and self.values.get(pid) is not None:
            return  # A missed reply keeps the last value, and age_note marks it if it gets old.
        self.values[pid] = value
        self.read_at[pid] = time.monotonic()
        rpm = number(value) if pid == 0x0C else None
        if rpm is not None:
            if self.engine_running is not None and self.engine_running != (rpm > 0):
                self.engine_changed_at = self.read_at[pid]
            self.engine_running = rpm > 0

    def age_note(self, pid, lang=None):
        """Say why a shown value may no longer be true, or return None if it's current."""
        read_at = self.read_at.get(pid)
        if read_at is None or self.values.get(pid) is None:
            return None
        if read_at < self.engine_changed_at:
            return t("Read before the engine started" if self.engine_running else "Read before the engine stopped", lang)
        limit = FAST_STALE_SECONDS + self.interval if pid in self.fast else SLOW_STALE_SECONDS
        age = time.monotonic() - read_at
        return t("{seconds} seconds ago", lang, seconds=int(age)) if age > limit else None

    async def fill(self):
        for pid in self.pids:
            self.store(pid, await self.elm.read_pid(pid))
        self.voltage = await self.elm.voltage()

    async def step(self):
        batch = list(self.fast)
        wrapped = False
        if not self.elm.can_group:
            slow_count = SLOW_PER_STEP
        else:
            # Short lists take more requests, so they carry one slow value to keep the fast ones quick.
            slow_count = GROUP_SLOW_PER_STEP if self.elm.group_size >= MAX_PIDS_PER_REQUEST else 1
        for _ in range(min(slow_count, len(self.slow))):
            batch.append(self.slow[self.next_slow])
            self.next_slow = (self.next_slow + 1) % len(self.slow)
            wrapped = wrapped or self.next_slow == 0
        for pid, value in (await self.elm.read_pids(batch)).items():
            self.store(pid, value)
        self.updates += 1
        if wrapped:
            self.voltage = await self.elm.voltage()


async def poll(elm, args, pids, writer=None, log_file=None):
    interactive = sys.stdout.isatty()
    reader = LiveReader(elm, pids)
    await reader.fill()
    count = 0
    while args.samples is None or count < args.samples:
        if count:
            await reader.step()
        values = reader.values
        if interactive:
            sys.stdout.write("\x1b[H\x1b[J")
        print(datetime.now().strftime("%H:%M:%S") + ("  " + t("logging to {name}", name=log_file.name) if log_file else ""))
        for pid, value in values.items():
            name, unit, _ = LIVE_PIDS[pid]
            print(f"  {t(name):22} {shown_value(value):>8} {t(unit)}")
        print(f"  {t('Voltage at adapter (approximate)'):22} {t(reader.voltage):>8}")
        if interactive:
            print("\n" + t("Press Ctrl+C to stop."))
        if writer:
            writer.writerow([datetime.now().isoformat(timespec="milliseconds")]
                            + ["" if values[p] is None else format_value(values[p]) for p in pids])
            log_file.flush()
        count += 1
        await asyncio.sleep(args.interval)


async def live_pids(elm):
    supported = await elm.supported_pids()
    pids = [p for p in LIVE_PIDS if p in supported]
    if not pids:
        raise OBDError("The car reported no live values this tool can read.")
    return pids


async def run_live(elm, args):
    os.system("")  # turns on ANSI screen clearing in older Windows consoles
    await poll(elm, args, await live_pids(elm))


def column_title(pid, lang=None):
    name, unit, _ = LIVE_PIDS[pid]
    return f"{t(name, lang)} ({t(unit, lang)})" if unit else t(name, lang)


async def run_log(elm, args):
    os.system("")
    pids = await live_pids(elm)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"drive_{datetime.now():%Y%m%d_%H%M%S}.csv"
    # utf-8-sig lets Excel read Arabic headers correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as log_file:
        writer = csv.writer(log_file)
        writer.writerow([t("time")] + [column_title(p) for p in pids])
        try:
            await poll(elm, args, pids, writer, log_file)
        finally:
            print(t("Saved log: {path}", path=path))


def dashboard_state(snapshot, reader, demo=False, lang=None, cars=()):
    """Build what the page shows, with every label and name in the requested language."""
    freeze = snapshot["freeze"]
    return {
        "connected": True,
        "demo": demo,
        "cars": list(cars),
        "time": datetime.now().strftime("%H:%M:%S"),
        "reading": reader.updates,
        "module_records": page_module_records(snapshot.get("module_records"), lang),
        "test_results": page_test_results(snapshot.get("test_results"), lang),
        "adapter": snapshot["adapter"],
        "protocol": snapshot["protocol"],
        "voltage": t(reader.voltage, lang),
        "known_modules": known_modules(snapshot["vehicle"]),
        "vehicle": {t(label, lang): modules for label, modules in snapshot["vehicle"].items()},
        "monitors": {h: {**status, "tests": [[t(name, lang), done] for name, done in status["tests"]]}
                     for h, status in snapshot["monitors"].items()},
        "codes": {t(label, lang): {h: [{"code": c, "description": describe_dtc(c, lang), "system": car_systems.code_system(c)}
                                       for c in module_codes]
                                   for h, module_codes in modules.items()}
                  for label, modules in snapshot["codes"].items()},
        "freeze": freeze and {
            "code": freeze["code"], "description": describe_dtc(freeze["code"], lang), "system": car_systems.code_system(freeze["code"]),
            "recheck": recheck_hint(freeze, lang),
            "values": [{"name": t(v["name"], lang), "unit": t(v["unit"], lang), "text": t(v["text"], lang)}
                       for v in freeze["values"]]},
        "systems": page_systems(snapshot, reader, lang),
        "ecus": page_ecus(snapshot, getattr(getattr(reader, "elm", None), "busy_modules", set)(), lang, None,
                          getattr(getattr(reader, "elm", None), "heard", None)),
        "values": [*[{"pid": f"{pid:02X}", "name": t(LIVE_PIDS[pid][0], lang), "system": car_systems.value_system(pid), "tone": value_tone(pid, value),
                    "unit": "" if value == "Not reported" else t(LIVE_PIDS[pid][1], lang),
                    "value": value if isinstance(value, (int, float)) else None,
                    "text": "-" if value is None else t(format_value(value), lang),
                    "note": reader.age_note(pid, lang)}
                   for pid, value in reader.values.items()], *page_fuel_use(reader.values, lang)],
        "this_drive": [[t(name, lang), done] for name, done in snapshot["this_drive"] or []],
        "more": [{**item, "system": car_systems.more_system(item.get("path", ""), item["name"])} for item in snapshot["more"]],
        "raw": [{"pid": f"{pid:02X}", "text": text or t("no reply", lang)} for pid, text in snapshot["raw"].items()],
    }


MOVING_KMH = 5


def fuel_use(values):
    """Estimate fuel use as (number, unit), or None. The air flow method only suits gasoline, as diesels run with spare air."""
    litres_per_hour = car_analysis.litres_per_hour(values)
    if litres_per_hour is None:
        return None
    speed = number(values.get(0x0D))
    return (litres_per_hour / speed * 100, "L/100km") if speed is not None and speed > MOVING_KMH else (litres_per_hour, "L/h")


def value_tone(pid, value):
    """Say whether a reading deserves attention. These are cautious road car limits, not a diagnosis."""
    reading = number(value)
    if reading is None:
        return ""
    if pid == 0x05:
        return "bad" if reading >= 115 else "warn" if reading >= 105 else ""
    if pid == 0x42:
        return "bad" if reading < 11.5 or reading > 15.5 else ""
    if pid == 0x2F:
        return "warn" if reading < 10 else ""
    return ""


def recheck_hint(freeze, lang=None):
    """Say how to drive to recheck a fault, from the conditions saved when it was set. None if nothing useful was saved."""
    found = {v["name"]: v["text"] for v in freeze["values"]}
    parts = []
    for name, text in (("Engine RPM", "engine near {value} rpm"), ("Engine load", "load near {value}%"), ("Coolant temp", "coolant near {value} C")):
        try:
            parts.append(t(text, lang, value=round(float(found[name]))))
        except (KeyError, ValueError):
            continue
    return t("To recheck this fault after a repair, drive safely in similar conditions: {conditions}.", lang,
             conditions=t(", ", lang).join(parts)) if parts else None


ECU_ROLES = {"7E8": ("Engine computer", "engine"), "7E9": ("Transmission computer", "transmission")}


RATING_AFTER_REPLIES = 50


def adapter_rating(elm):
    """Judge the adapter from how it has behaved on this connection. Returns (tone, text, values), or None if it's too early."""
    stats = elm.transport
    if stats.replied < RATING_AFTER_REPLIES:
        return None
    milliseconds = round(stats.total_seconds / stats.replied * 1000)
    lost = stats.timeouts / stats.sent
    if lost > 0.02:
        return "bad", "Unreliable: {percent}% of requests got no reply. Check it's pushed in firmly. If that doesn't help, another adapter may read better.", \
            {"percent": round(lost * 100)}
    if elm.can_group and elm.group_size >= MAX_PIDS_PER_REQUEST:
        return "good", "Fast: answers several values at once, in about {ms} ms.", {"ms": milliseconds}
    if elm.can_group:
        return "", "Average: answers only {count} values at a time, in about {ms} ms. A better adapter would update readings more often.", \
            {"count": elm.group_size, "ms": milliseconds}
    return "bad", "Slow: answers one value at a time, in about {ms} ms, so readings update slowly.", {"ms": milliseconds}


def translated_findings(found, lang=None):
    """Fill in and translate findings for the page. A few values are words themselves, so they're translated too."""
    shown = []
    for item in found:
        values = {key: t(value, lang) if key in ("what", "light") else value for key, value in item["values"].items()}
        if "names" in item:
            values["names"] = values["items"] = t(", ", lang).join(t(name, lang) for name in item["names"])
        if "since" in item:
            values["since"] = t(item["since"], lang, **item["values"])
        shown.append({"id": item["id"], "title": t(item["title"], lang), "state": item["state"], "system": item["system"],
                      "text": t(item["text"], lang, **values)})
    return shown


def car_check(snapshot, values, previous=None):
    vin = next(iter(snapshot["vehicle"].get("VIN", {}).values()), None)
    found = car_analysis.used_car_check(snapshot, values, garage.model_year(vin))
    changed = sorted({change["label"] for changes in ecu_changes(snapshot, previous).values() for change in changes})
    if changed:
        found.insert(0, {**car_analysis.finding("software_changed", "Computer software changed", "warn",
                                                "Since an earlier visit, a computer reports different software: {items}. That follows an official update or a reflash, which can include tuning. Ask what was done.",
                                                "status"), "names": changed})
    return found


def page_car_check(snapshot, values, lang=None, previous=None):
    found = car_check(snapshot, values, previous)
    return {"findings": translated_findings(found, lang),
            "bad": sum(item["state"] == "bad" for item in found), "warn": sum(item["state"] == "warn" for item in found)}


def car_check_report(snapshot, values, lang=None):
    """The used-car check as text, for keeping or showing to a mechanic."""
    labels = {"bad": "Check this", "warn": "Worth a look", "good": "Looks fine", "unknown": "Not enough data yet"}
    vin = next(iter(snapshot["vehicle"].get("VIN", {}).values()), None)
    lines = [t("Used-car check", lang), datetime.now().strftime("%Y-%m-%d %H:%M"), f"{t('VIN', lang)}: {vin or t('unknown', lang)}", ""]
    for item in translated_findings(car_check(snapshot, values), lang):
        lines += [f"[{t(labels[item['state']], lang)}] {item['title']}", f"  {item['text']}", ""]
    lines.append(t("This check reads only the computers that answer, usually the engine and transmission. It can't see crash damage, mileage or wear, so it doesn't replace a mechanic's inspection.", lang))
    return "\n".join(lines)


def page_analysis(stats, values, snapshot, previous, lang=None):
    """Describe what this connection's readings show over time, for the Analysis page."""
    found = car_analysis.findings(stats, values, snapshot, previous, lambda monitor: monitor_name(monitor, lang))
    shown = lambda number_, unit: "-" if number_ is None else f"{number_:.0f}" if unit in ("", "rpm", "km/h") else f"{number_:.1f}"
    ranges = []
    for pid, tally in stats.ranges.items():
        if pid in LIVE_PIDS and tally.count >= 2:
            ranges.append({"name": t(LIVE_PIDS[pid][0], lang), "unit": t(LIVE_PIDS[pid][1], lang), "system": car_systems.value_system(pid),
                           "low": format_value(float(tally.low)), "average": format_value(float(tally.average)), "high": format_value(float(tally.high))})
    return {
        "findings": translated_findings(found, lang),
        "attention": sum(item["state"] in car_analysis.ATTENTION for item in found),
        "drive": [{"label": t(label, lang), "text": f"{shown(number_, unit)} {t(unit, lang) if number_ is not None else ''}".strip()}
                  for label, number_, unit in car_analysis.drive_summary(stats)],
        "ranges": ranges,
    }


# What each computer says about its software. A change between visits means new software: an update, or a reflash.
FINGERPRINT_ITEMS = ("Calibration ID", "Calibration check", "Software version", "Hardware number", "Part number")
VIN_REGIONS = (("ABCDEFGH", "Africa"), ("JKLMNPR", "Asia"), ("STUVWXYZ", "Europe"), ("12345", "North America"), ("67", "Oceania"), ("89", "South America"))
VIN_COUNTRIES = {"J": "Japan", "K": "South Korea", "L": "China", "M": "India", "W": "Germany", "V": "France or Spain", "S": "United Kingdom",
                 "Z": "Italy", "Y": "Sweden or Finland", "T": "Czechia or Hungary", "1": "United States", "4": "United States",
                 "5": "United States", "2": "Canada", "3": "Mexico", "9": "Brazil", "6": "Australia"}


def ecu_fingerprint(snapshot):
    """Return {reply header: {item: text}} for the software details each computer reported."""
    found = {}
    for label in ("Calibration ID", "Calibration check"):
        for header, text in snapshot["vehicle"].get(label, {}).items():
            if text:
                found.setdefault(header, {})[label] = text
    for header, items in (snapshot.get("ecu_details") or {}).items():
        for item in items:
            if item["label"] in FINGERPRINT_ITEMS:
                found.setdefault(header, {})[item["label"]] = item["text"]
    return found


def ecu_changes(snapshot, previous):
    """Compare each computer's software details with the last visit. Returns {header: [{label, before, now, since}]}."""
    log = (previous or {}).get("ecu_history") or []
    if not log:
        return {}
    last, since = log[-1].get("ids", {}), log[-1].get("time", "")[:10]
    changes = {}
    for header, items in ecu_fingerprint(snapshot).items():
        for label, now_ in items.items():
            before = last.get(header, {}).get(label)
            if before and before != now_:
                changes.setdefault(header, []).append({"label": label, "before": before, "now": now_, "since": since})
    return changes


def car_identity(snapshot, values, elm=None):
    """Describe the car from its VIN and what it reports, as (label, text) pairs. The VIN itself is shown elsewhere."""
    vin = next(iter(snapshot["vehicle"].get("VIN", {}).values()), None) or ""
    rows = []
    # Only a VIN of the right shape says where the car was built. The letters I, O and Q are never used.
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin.upper()):
        if make_from_vin(vin):
            rows.append(("Maker", make_from_vin(vin)))
        year = garage.model_year(vin)
        if year:
            rows.append(("Model year", str(year)))
        country = VIN_COUNTRIES.get(vin[:1].upper())
        region = next((name for letters, name in VIN_REGIONS if vin[:1].upper() in letters), None)
        if country or region:
            rows.append(("Built in", country or region))
    for pid, label in ((0x51, "Fuel type"), (0x1C, "OBD standard")):
        if values.get(pid) not in (None, ""):
            rows.append((label, str(values[pid])))
    if elm is not None:
        rows += [("Protocol", elm.protocol or "unknown"), ("Adapter", elm.version or "unknown")]
    computers = sorted({*known_modules(snapshot["vehicle"]), *snapshot["monitors"]})
    rows.append(("Computers answering", ", ".join(computers) or "-"))
    return rows


def ecu_report(snapshot, values, previous=None, elm=None, scan=None, items=None, lang=None):
    """Everything the ECU page shows, as text for keeping, copying or sending to a mechanic."""
    lines = [t("ECU report", lang), datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    vin = next(iter(snapshot["vehicle"].get("VIN", {}).values()), None)
    lines.append(f"{t('VIN', lang)}: {vin or t('unknown', lang)}")
    lines += [f"{t(label, lang)}: {t(text, lang)}" for label, text in car_identity(snapshot, values, elm)]
    changes = ecu_changes(snapshot, previous)
    for ecu in page_ecus(snapshot, (), lang, previous):
        lines += ["", f"{ecu['role']} ({ecu['header']})"]
        lines += [f"  {row['label']}: {row['text']}" for row in ecu["rows"]]
        if ecu["faults"]:
            lines.append(f"  {t('Fault codes', lang)}: {', '.join(ecu['faults'])}")
        lines += [f"  {t(text, lang)}" for text in ecu["changes"]]
    for header, found in (items or {}).items():
        if found:
            lines += ["", f"{t('All identity data read', lang)} ({header})"]
            lines += [f"  {item['item']} {t(item['label'], lang, item=item['item'])}: {item['text']}" for item in found]
    if scan and scan.get("modules"):
        lines += ["", f"{t('Other computers found by the last scan', lang)} ({scan.get('time', '')[:16].replace('T', ' ')})"]
        for module in scan["modules"]:
            codes = ", ".join(item["code"] for item in module.get("codes", [])) or t("No fault codes", lang)
            lines.append(f"  {module['hdr']} {t(module.get('name', ''), lang)}: {codes if module.get('read_codes', True) else t('Answered, but refused to share fault codes with these commands', lang)}")
    if not changes:
        lines += ["", t("No software changes found since the last visit.", lang) if (previous or {}).get("ecu_history") else
                  t("Software changes can be spotted from the next visit on.", lang)]
    return "\n".join(lines)


EXPLORER_STATES = ("answered", "empty", "refused", "silent")


def page_explorer(found, lang=None):
    """The latest explorer map for the page, with each item's names and refusal reasons translated."""
    if not found:
        return None
    modules = []
    for module in found.get("modules", []):
        items = [{"request": item["request"], "service": item["request"][:2], "names": ", ".join(t(name, lang) for name in item["names"][:3]),
                  "state": item["state"], "text": t(item["text"], lang) if item["state"] == "refused" else item["text"], "raw": item["raw"],
                  "changed": item.get("changed", False), "before": item.get("before", "")}
                 for item in module.get("items", []) if item["state"] != "silent"]
        counts = {state: sum(item["state"] == state for item in module.get("items", [])) for state in EXPLORER_STATES}
        modules.append({"header": module["header"], "request": module["request"], "name": t(module.get("name", "Unknown module"), lang),
                        "how": module.get("how", ""), "items": items, "counts": counts})
    return {"time": found.get("time", ""), "deep": found.get("deep", False), "compared": found.get("compared"), "modules": modules}


def page_ecu_tools(snapshot, values, previous, elm, scan, items, lang=None):
    identity = [{"label": t(label, lang), "text": t(text, lang)} for label, text in car_identity(snapshot, values, elm)]
    shown_scan = None
    if scan and scan.get("modules") is not None:
        shown_scan = {"time": scan.get("time", ""), "modules": [
            {"header": module["hdr"], "name": t(module.get("name", "Unknown module"), lang), "codes": [item["code"] for item in module.get("codes", [])],
             "refused": not module.get("read_codes", True), "refusal": t(module["refusal"], lang) if module.get("refusal") else None}
            for module in scan["modules"]]}
    shown_items = {header: [{"item": item["item"], "label": t(item["label"], lang, item=item["item"]), "text": item["text"]} for item in found]
                   for header, found in (items or {}).items()}
    return {"identity": identity, "scan": shown_scan, "items": shown_items}


def page_ecus(snapshot, busy=(), lang=None, previous=None, heard=None):
    """Describe each computer that has answered on this car, from what it reports about itself. Nothing here writes to it."""
    vehicle = snapshot["vehicle"]
    ecus = []
    changes = ecu_changes(snapshot, previous)
    for header in sorted({*known_modules(vehicle), *snapshot["monitors"]}):
        role, icon = ECU_ROLES.get(header, ("Computer {header}", "ecu"))
        rows = []
        for label, field in (("Module name", "Module name"), ("Calibration ID", "Calibration ID"), ("Calibration check", "Calibration check")):
            text = vehicle.get(field, {}).get(header)
            if text:
                rows.append({"label": t(label, lang), "text": text})
        rows += [{"label": t(item["label"], lang), "text": item["text"]} for item in (snapshot.get("ecu_details") or {}).get(header, [])]
        faults = sorted({code for modules in snapshot["codes"].values() for code in modules.get(header, [])})
        car_vin = next(iter(vehicle.get("VIN", {}).values()), None)
        stored_vin = next((item["text"] for item in (snapshot.get("ecu_details") or {}).get(header, []) if item["label"] == "Stored VIN"), None)
        warning = t("This computer stores a different VIN from the car's. It may have been replaced, which isn't always a problem.", lang) \
            if car_vin and stored_vin and stored_vin != car_vin else None
        state = "busy" if header in busy else "answering" if header in snapshot["monitors"] else "silent"
        changed = [t("{item} changed since {date}: it was {before}, and now it's {now}. New software means an update or a reflash, so ask what was done.", lang,
                     item=t(change["label"], lang), date=change["since"], before=change["before"], now=change["now"])
                   for change in changes.get(header, [])]
        quiet = round(time.monotonic() - heard[header]) if heard and header in heard else None
        ecus.append({"header": header, "role": t(role, lang, header=header), "icon": icon, "state": state, "rows": rows, "faults": faults, "warning": warning,
                     "changes": changed, "quiet": quiet})
    return ecus


def page_fuel_use(values, lang=None):
    use = fuel_use(values)
    return [] if use is None else [{"pid": "FUEL", "name": t("Fuel use (estimate)", lang), "system": "fuel_air", "tone": "", "unit": t(use[1], lang),
                                    "value": use[0], "text": f"{use[0]:.1f}", "note": None}]


def page_systems(snapshot, reader, lang=None):
    """Sort everything known about the car into systems, for the Car systems page."""
    systems = car_systems.empty_systems(lang)

    def module_row(system, header, label):
        names = snapshot["vehicle"].get("Module name", {})
        if header in names:
            systems[system]["values"].append({"name": t(label, lang), "text": f"{header} {names[header]}".strip(), "note": None})

    module_row("engine", "7E8", "Engine module")
    module_row("transmission", "7E9", "Transmission module")
    for pid, value in reader.values.items():
        unit = "" if value == "Not reported" else t(LIVE_PIDS[pid][1], lang)
        systems[car_systems.value_system(pid)]["values"].append({
            "name": t(LIVE_PIDS[pid][0], lang), "text": f"{'-' if value is None else t(format_value(value), lang)} {unit}".strip(),
            "note": reader.age_note(pid, lang)})
    systems["electrical"]["values"].append({"name": t("Voltage at adapter (approximate)", lang), "text": t(reader.voltage, lang), "note": None})
    for item in page_fuel_use(reader.values, lang):
        systems["fuel_air"]["values"].append({"name": item["name"], "text": f"{item['text']} {item['unit']}", "note": None})
    for item in snapshot["more"]:
        systems[car_systems.more_system(item.get("path", ""), item["name"])]["values"].append(
            {"name": item["name"], "text": item["text"], "note": None})

    faults = {}

    def add_fault(code, state, module, now):
        fault = faults.setdefault(code, {"code": code, "description": describe_dtc(code, lang), "states": [], "modules": [], "now": False})
        if state not in fault["states"]:
            fault["states"].append(state)
        if module not in fault["modules"]:
            fault["modules"].append(module)
        fault["now"] = fault["now"] or now

    for label, modules in snapshot["codes"].items():
        for header, module_codes in modules.items():
            for code in module_codes:
                add_fault(code, t(label, lang), header, True)
    states = {"active": "Active", "stored": "Stored", "pending": "Pending", "history": "Past"}
    for record in snapshot.get("module_records") or []:
        for item in record["codes"]:
            add_fault(item["code"][:5], t(states.get(item["state"], item["state"]), lang), record.get("reply_id", record["hdr"]), item["state"] != "history")
    for code, fault in faults.items():
        systems[car_systems.code_system(code)]["faults"].append(fault)

    for header, status in snapshot["monitors"].items():
        for name, done in status["tests"]:
            systems[car_systems.readiness_system(name)]["checks"].append({"name": t(name, lang), "done": done, "module": header})
    for group in page_test_results(snapshot.get("test_results"), lang):
        systems[car_systems.test_group_system(int(group["monitor"], 16))]["results"].append(group)
    return list(systems.values())


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def fix_estimate(fix, lang=None):
    """Describe a repair's estimated cost and time, leaving out whatever isn't known."""
    parts = []
    if fix["parts_max"]:
        parts.append(t("Parts: ${low} to ${high}, US estimate", lang, low=f"{fix['parts_min']:g}", high=f"{fix['parts_max']:g}"))
    if fix["labor_hours"]:
        parts.append(t("Labor: about {hours} hours", lang, hours=f"{fix['labor_hours']:g}"))
    return " | ".join(parts)


def page_plans(code, lang=None):
    return [{"make": plan["make"], "source": plan["source"],
             "fixes": [{"title": fix["title"], "difficulty": t(fix["difficulty"], lang), "estimate": fix_estimate(fix, lang),
                        "steps": fix["steps"]} for fix in plan["fixes"]]}
            for plan in repair_plans(code, lang or i18n.current_language())]


def charging_out_of_range(values):
    """True if the engine is running and the voltage suggests a charging problem."""
    volts, rpm = number(values.get(0x42)), number(values.get(0x0C))
    return volts is not None and rpm is not None and rpm > 0 and not CHARGING_LOW_VOLTS <= volts <= CHARGING_HIGH_VOLTS


def charging_problem(values):
    """Say which kind of out-of-range voltage this is, since they don't wait equally long before an alert."""
    if not charging_out_of_range(values):
        return None
    return "resting" if CHARGING_DISCHARGING_VOLTS <= number(values.get(0x42)) < CHARGING_LOW_VOLTS else "fault"


def car_alerts(snapshot, values, lang=None, charging_lasting=True):
    """Warnings worth interrupting for. These are guides, not a diagnosis."""
    alerts = []
    if any(status["mil"] for status in snapshot["monitors"].values()):
        alerts.append(t("The check engine light is on. See the Fault codes page.", lang))
    coolant, volts, rpm = number(values.get(0x05)), number(values.get(0x42)), number(values.get(0x0C))
    if coolant is not None and coolant >= 110:
        alerts.append(t("Coolant is at {value} C, which may mean overheating. If the car's temperature gauge agrees, stop safely and let the engine cool.", lang, value=round(coolant)))
    if volts is not None and rpm is not None:
        if charging_out_of_range(values) and charging_lasting:
            if volts < CHARGING_LOW_VOLTS:
                alerts.append(t("Voltage is {value} V with the engine running. The alternator may not be charging.", lang, value=f"{volts:.1f}"))
            else:
                alerts.append(t("Voltage is {value} V with the engine running. The charging system may be overcharging.", lang, value=f"{volts:.1f}"))
        elif rpm == 0 and volts < 11.8:
            alerts.append(t("Voltage is {value} V with the engine off. The battery is low, so start the engine or turn off the ignition soon.", lang, value=f"{volts:.1f}"))
    return alerts


class Job:
    """One command run from the web app, with its text output."""

    MAX_OUTPUT = 200_000

    def __init__(self, number, action, params, lang):
        self.number = number
        self.action = action
        self.params = params
        self.lang = lang
        self.status = "queued"
        self.output = ""
        self.started = datetime.now().strftime("%H:%M:%S")

    def write(self, text):
        self.output = (self.output + text)[-self.MAX_OUTPUT:]

    def flush(self):
        pass

    def view(self):
        return {"id": self.number, "action": self.action, "params": self.params, "status": self.status,
                "output": self.output, "started": self.started, "lang": self.lang}


# What the web app can run on the car. `live` and `log` are terminal commands, since they never end.
WEB_ACTIONS = ("battery", "starttest", "info", "codes", "report", "scan", "ecuread", "explore", "exploredeep", "exploreagain", "profile", "clear")
# Readings kept from before and after something new goes wrong, so the moment itself can be looked at later.
MOMENT_SECONDS = 30
MAX_MOMENTS_SHOWN = 10
MAX_JOBS_KEPT = 20
MAX_JOBS_WAITING = 5


class DashboardApp:
    """Serves the app straight away, then keeps trying to reach the car until it answers."""

    RETRY_SECONDS = 5

    def __init__(self, args):
        self.args = args
        self.message = ("Looking for the adapter ...", {})
        self.snapshot = self.reader = self.elm = None
        # Demo pages get shared and screenshotted, so they never list real saved cars.
        self.cars = [] if args.demo else garage.summaries()
        self.jobs = []
        self.waiting = []
        self.tasks = set()
        self.next_job = 1
        self.link = None  # the adapter currently connected, even while it's still reading the car
        self.adapter = None  # the adapter that last answered, while the car itself isn't answering
        self.connected_at = None
        self.connections = 0
        self.recording = None
        self.last_summary = None
        self.charging_since = None
        self.charging_kind = None
        self.activity = Activity()
        self.drive = None
        self.drive_saved_at = 0.0
        self.link_state = "on"  # "stopping" and "off" follow the Disconnect button
        self.stay_disconnected = False
        self.connect_again = asyncio.Event()
        self.connecting = None
        self.copy = None  # the saved car copy being run instead of a car
        self.stats = car_analysis.DriveStats()
        self.previous = {}  # what earlier visits to this car showed, for comparing
        self.recent = deque()  # (clock, time, values) for the last half minute
        self.moment = None     # set while the half minute after a new problem is being collected
        self.known_trouble = None
        self.moments = []      # what was saved, for the Analysis page
        self.scan = None            # the last module scan for this car
        self.identity_items = None  # every identity item read on request
        self.explorer = None        # the latest explorer map for this car
        self.was_disconnected = False  # whether Disconnect had been pressed before a copy was opened
        self.skip_wait = False

    def save_car_copy(self, lang=None):
        """Save what this car has answered. Returns (file name, None), or (None, why it can't)."""
        if self.copy:
            return None, t("This is already a car copy.", lang)
        if self.snapshot is None or self.elm is None:
            return None, t("Connect to a car and wait for it to finish reading first.", lang)
        vin = next(iter(self.snapshot["vehicle"].get("VIN", {}).values()), None)
        name = None
        if not self.args.demo:
            with contextlib.suppress(OSError, ValueError):
                name = garage.load_car(garage.car_key(self.snapshot["vehicle"])).get("name")
        year = garage.model_year(vin)
        name = name or " ".join(str(part) for part in (make_from_vin(vin), year) if part) or None
        try:
            return car_copy.save_copy(car_copy.copy_folder(self.args.demo), self.elm.transport.recorder, name, vin), None
        except OSError as error:
            return None, t("Could not save the car copy: {error}", lang, error=error)

    def switch_connection(self):
        """Let go of whatever is connected, and connect again straight away to the car or the copy."""
        self.skip_wait = True
        self.reconnect()
        if self.connecting is not None:
            self.connecting.cancel()

    def open_car_copy(self, file_name, lang=None):
        """Run the app from a saved copy. Returns why it can't, or None."""
        if self.job_changing_car():
            return t("Fault codes are being cleared. Wait for that to finish, then open the copy.", lang)
        try:
            copy = car_copy.load_copy(car_copy.copy_folder(self.args.demo), file_name)
        except ValueError as error:
            return t(str(error), lang)
        if not self.copy:
            self.was_disconnected = self.stay_disconnected or self.link_state != "on"
        self.copy = copy
        self.switch_connection()
        return None

    def close_car_copy(self):
        if not self.copy:
            return
        self.copy = None
        if self.was_disconnected:
            # The driver was told the adapter is safe to unplug, so closing a copy mustn't start looking for it again.
            self.stay_disconnected = True
            self.link_state = "stopping"
            if self.connecting is not None:
                self.connecting.cancel()
        else:
            self.switch_connection()

    def delete_car_copy(self, file_name, lang=None):
        """Delete a saved copy. Returns why it can't, or None."""
        if self.copy and self.copy.get("file") == file_name:
            return t("Close this car copy before deleting it.", lang)
        if not car_copy.delete_copy(car_copy.copy_folder(self.args.demo), file_name):
            return t("That isn't a car copy.", lang)
        return None

    def job_changing_car(self):
        """A clear must finish once it has started, or the driver can't tell whether it happened."""
        return any(job.action == "clear" and job.status in ("queued", "running") for job in self.jobs)

    def disconnect(self, lang=None):
        """Stop using the car and the adapter, and stay that way until asked. Returns why it can't, or None."""
        if self.job_changing_car():
            return t("Fault codes are being cleared. Wait for that to finish, then disconnect.", lang)
        if self.link_state == "off":
            return None
        self.copy = None
        self.stay_disconnected = True
        self.link_state = "stopping"
        if self.connecting is not None:
            self.connecting.cancel()
        return None

    def reconnect(self):
        self.stay_disconnected = False
        self.link_state = "on"
        self.message = ("Looking for the adapter ...", {})
        self.connect_again.set()

    def mark_disconnected(self):
        self.snapshot = self.reader = self.link = self.adapter = self.elm = None
        self.message = ("Disconnected. It's safe to unplug the adapter.", {})
        self.link_state = "off"
        self.activity.event("Disconnected. It's safe to unplug the adapter.", "good")

    def refresh_followup(self, elm, snapshot):
        """Bring the fault follow-up up to date with the reading just saved. Earlier visits stay as they were at the start."""
        if elm.demo or not self.previous:
            return
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.previous["followup"] = garage.fault_followup(garage.load_car(garage.car_key(elm.vehicle or snapshot["vehicle"])))

    def trouble_now(self):
        """The fault codes and analysis findings that currently need attention, as names that stay the same over time."""
        codes = {code for label in ("Stored", "Pending") for found in self.snapshot["codes"].get(label, {}).values() for code in found}
        found = car_analysis.findings(self.stats, self.reader.values, self.snapshot, self.previous, monitor_name)
        return codes | {item["title"] for item in found if item["state"] in car_analysis.ATTENTION and not item["id"].startswith("followup")}

    def watch_moment(self, elm):
        """Keep the last half minute of readings, and save them with the next half minute when something new goes wrong."""
        now = time.monotonic()
        self.recent.append((now, datetime.now().isoformat(timespec="milliseconds"), dict(self.reader.values)))
        trouble = self.trouble_now()
        if self.known_trouble is None:
            self.known_trouble = trouble  # what was already wrong when the connection began isn't news
        new = trouble - self.known_trouble
        self.known_trouble |= trouble
        if new and self.moment is None:
            self.moment = {"reason": sorted(new), "until": now + MOMENT_SECONDS}
        if self.moment is None:
            while self.recent and now - self.recent[0][0] > MOMENT_SECONDS:
                self.recent.popleft()
        elif now >= self.moment["until"]:
            self.save_moment(elm)

    def save_moment(self, elm):
        moment, self.moment = self.moment, None
        if moment is None or not self.recent or self.args.demo or elm.demo:
            return
        pids = [pid for pid in self.reader.values if pid in LIVE_PIDS]
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"moment_{datetime.now():%Y%m%d_%H%M%S}.csv"
            with path.open("x", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow([t("time")] + [column_title(pid) for pid in pids])
                for _, stamp, values in self.recent:
                    writer.writerow([stamp] + ["" if values.get(pid) is None else format_value(values[pid]) for pid in pids])
        except OSError as error:
            self.activity.event("Could not save this car's info: {error}", "bad", error=error)
            return
        self.moments.append({"time": garage.now(), "reason": moment["reason"], "file": str(path)})
        del self.moments[:-MAX_MOMENTS_SHOWN]
        self.activity.event("Saved the readings from around a new problem: {path}", "good", path=str(path))

    def start_drive(self, elm, snapshot):
        self.drive = {"id": garage.now(), "started": garage.now(), "ended": garage.now(), "seconds": 0, "adapter": elm.version,
                      "protocol": elm.protocol, "top_speed": None, "top_rpm": None, "top_coolant": None,
                      "low_volts": None, "high_volts": None, "updates": 0}
        self.drive_began = time.monotonic()
        self.save_drive(elm, snapshot)

    def track_drive(self, elm, snapshot):
        """Keep the highs and lows of this connection, and save them now and then in case it ends suddenly."""
        drive, values = self.drive, self.reader.values
        if drive is None:
            return
        drive["updates"] += 1
        for field, pid in (("top_speed", 0x0D), ("top_rpm", 0x0C), ("top_coolant", 0x05), ("high_volts", 0x42)):
            value = number(values.get(pid))
            if value is not None:
                drive[field] = value if drive[field] is None else max(value, drive[field])
        volts = number(values.get(0x42))
        if volts is not None:
            drive["low_volts"] = volts if drive["low_volts"] is None else min(volts, drive["low_volts"])
        if time.monotonic() - self.drive_saved_at >= DRIVE_SAVE_SECONDS:
            self.save_drive(elm, snapshot)

    def save_drive(self, elm, snapshot):
        if self.drive is None or elm.demo or self.args.demo:
            return
        codes = snapshot["codes"]
        self.drive.update(ended=garage.now(), seconds=round(time.monotonic() - self.drive_began),
                          light=any(status["mil"] for status in snapshot["monitors"].values()),
                          codes=sorted({c for label in ("Stored", "Pending") for found in codes[label].values() for c in found}),
                          method=elm.can_group, averages=self.stats.averages(), fuel=self.stats.fuel_at_start, fuel_end=self.stats.fuel_now,
                          distance=round(self.stats.distance_km, 1), fuel_used=round(self.stats.fuel_litres, 2),
                          checks=[item["title"] for item in car_analysis.findings(self.stats, self.reader.values if self.reader else {}, snapshot,
                                                                                    self.previous, monitor_name)
                                  if item["state"] in car_analysis.ATTENTION])
        self.drive_saved_at = time.monotonic()
        try:
            garage.save_session(elm.vehicle or snapshot["vehicle"], self.drive)
            self.cars = garage.summaries()
        except OSError as error:
            print(t("Could not save this car's info: {error}", error=error), flush=True)

    def track_charging(self):
        kind = charging_problem(self.reader.values)
        if kind != self.charging_kind:
            self.charging_kind = kind
            self.charging_since = time.monotonic() if kind else None

    def charging_lasting(self):
        wait = CHARGING_RESTING_ALERT_SECONDS if self.charging_kind == "resting" else CHARGING_ALERT_SECONDS
        return self.charging_since is not None and time.monotonic() - self.charging_since >= wait

    async def refresh_parts(self, elm):
        """Reread the light and fault codes a part at a time, so live values keep updating between parts.

        A module missing from one reread keeps its last known answer rather than looking clear.
        Each part is saved as soon as it's read, so a command run between parts is never overwritten.
        """
        snapshot = self.snapshot
        before = json.dumps([snapshot["monitors"], snapshot["codes"]], sort_keys=True, default=str)
        for header, payload in (await elm.request_all(0x01, 0x01, attempts=1)).items():
            if len(payload) >= 6:
                snapshot["monitors"][header] = decode_monitors(payload)
        yield
        for label, mode in CODE_MODES:
            replies = await elm.request_all(mode, attempts=1)
            snapshot["codes"].setdefault(label, {}).update({h: decode_dtcs(p) for h, p in replies.items()})
            yield
        if snapshot is not self.snapshot:
            return  # A command such as clear read everything again meanwhile, so this older reading mustn't be saved over it.
        if 0x41 in snapshot["supported"]:
            snapshot["this_drive"] = await read_this_drive(elm) or snapshot["this_drive"]
        if json.dumps([snapshot["monitors"], snapshot["codes"]], sort_keys=True, default=str) != before:
            stored = {code for found in snapshot["codes"]["Stored"].values() for code in found}
            if not stored:
                snapshot["freeze"] = None
            elif not snapshot["freeze"] or snapshot["freeze"]["code"] not in stored:
                snapshot["freeze"] = await read_freeze_frame(elm)
            remember_snapshot(elm, snapshot)
            self.refresh_followup(elm, snapshot)

    async def refresh_status(self, elm):
        async for _ in self.refresh_parts(elm):
            pass

    def device_status(self, lang=None):
        """Describe the adapter itself as rows of label, text and tone for the page."""
        row = lambda label, text, tone="": {"label": t(label, lang), "text": str(text), "tone": tone}

        def identity(adapter):
            rows = [row("Name", adapter.device_name or t("unknown", lang))]
            if adapter.device_address:
                rows.append(row("Bluetooth address", adapter.device_address))
            rows.append(row("Reported version", adapter.version))
            if adapter.description:
                rows.append(row("Reported description", adapter.description))
            return rows

        elm = self.link
        if self.copy:
            return [row("Status", t("Running from a saved car copy", lang), "good")]
        if self.link_state == "off":
            return [row("Status", t("Disconnected. It's safe to unplug the adapter.", lang), "good")]
        if elm is None:
            if self.adapter and self.adapter.version:
                rows = [row("Status", t("Adapter connected, waiting for the car", lang), "bad"), *identity(self.adapter)]
            else:
                rows = [row("Status", t("Looking for the adapter ...", lang), "bad")]
            if self.connections:
                rows.append(row("Times connected", self.connections))
            return rows
        stats = elm.transport
        since = time.monotonic() - self.connected_at
        rows = [row("Status", t("Connected", lang), "good"), *identity(elm)]
        rating = adapter_rating(elm)
        if rating:
            rows.append(row("Adapter rating", t(rating[1], lang, **rating[2]), rating[0]))
        rows += [row("Protocol", elm.protocol or t("unknown", lang)),
                 row("Voltage at adapter (approximate)", t(self.reader.voltage, lang) if self.reader else "-"),
                 row("Connected for", f"{int(since // 60)}:{int(since % 60):02d}"),
                 row("Times connected", self.connections)]
        if elm.can_group is not None:
            rows.append(row("Reading method", t("Several values per request" if elm.can_group else "One value per request", lang)))
        if stats.replied:
            rows.append(row("Average reply time", t("{ms} ms", lang, ms=round(stats.total_seconds / stats.replied * 1000))))
            quiet = time.monotonic() - stats.last_reply
            rows.append(row("Last reply", t("{seconds} seconds ago", lang, seconds=round(quiet)),
                            "good" if quiet < 10 else "bad"))
        rows += [row("Commands sent", stats.sent),
                 row("Replies lost", stats.timeouts, "bad" if stats.timeouts else "good")]
        return rows

    def new_job(self, action, params, lang):
        job = Job(self.next_job, action, params, lang)
        self.next_job += 1
        self.jobs.append(job)
        del self.jobs[:-MAX_JOBS_KEPT]
        return job

    def submit(self, action, params, lang):
        """Queue a command for the car. Returns (job, None), or (None, why it was refused)."""
        if action not in WEB_ACTIONS:
            return None, t("That command isn't available here.", lang)
        if self.elm is None:
            return None, t("Not connected to a car", lang)
        if self.copy and action == "clear":
            return None, t("A car copy has no car to clear. Connect to the car to do this.", lang)
        if len(self.waiting) >= MAX_JOBS_WAITING:
            return None, t("Too many commands are waiting. Try again when they finish.", lang)
        if action == "profile":
            name = str(params.get("name", ""))
            if name.lower() not in {car.lower() for car in car_modules.catalog()}:
                return None, t("'{name}' isn't in the catalog. Search for your car with: .\\obd profiles {search}",
                               lang, name=name, search=name.split("-")[0])
            year = params.get("year")
            if year not in (None, "") and not (str(year).isdigit() and 1980 <= int(year) <= 2100):
                return None, t("Enter a model year between 1980 and 2100.", lang)
            params = {"name": name, "year": int(year) if year not in (None, "") else None}
        elif action == "clear":
            if params.get("answer") != "CLEAR":
                return None, t("Clearing needs you to type CLEAR.", lang)
            params = {"answer": "CLEAR"}
        else:
            params = {}
        job = self.new_job(action, params, lang)
        self.waiting.append(job)
        return job, None

    async def run_job(self, elm, job):
        previous = i18n.current_language()
        i18n.set_language(job.lang)
        job.status = "running"
        finished = None
        try:
            with contextlib.redirect_stdout(job):
                if job.action == "profile" and not car_modules.local_profile_files(job.params["name"]):
                    print(t("Downloading profile {name} from OBDb ...", name=job.params["name"]), flush=True)
                    try:
                        await asyncio.to_thread(car_modules.download_profile, job.params["name"])
                    except OSError as error:  # no internet is no reason to drop the car
                        raise ValueError(t("Error: {error}", error=error)) from None
                handler = {"battery": run_battery, "starttest": run_start_test, "ecuread": run_identity_items, "explore": run_explore,
                           "exploredeep": run_explore_deep, "exploreagain": run_explore_again, "info": run_info, "codes": run_codes, "report": run_report,
                           "scan": run_scan, "profile": run_profile, "clear": run_clear}[job.action]
                await handler(elm, SimpleNamespace(**job.params))
            job.status = finished = "done"
            if job.action == "scan" and elm.last_scan is not None:
                self.scan = {"time": garage.now(), "modules": elm.last_scan}
            if job.action == "ecuread" and elm.identity_items is not None:
                self.identity_items = elm.identity_items
            if job.action.startswith("explore") and elm.explorer is not None:
                self.explorer = elm.explorer
            if job.action == "clear":
                self.snapshot = await read_snapshot(elm)
                for pid, value in self.snapshot["values"].items():
                    self.reader.store(pid, value)
            elif job.action == "battery":
                await elm.setup()  # the battery check resets the adapter
        except (OBDError, ValueError) as error:
            # Once the command itself has finished, trouble reading the car again doesn't undo it.
            job.status = finished or "failed"
            job.write(f"{error}\n")
            if finished or (isinstance(error, OBDError) and error.text in (ADAPTER_RESTARTED, ADAPTER_SILENT)):
                raise  # the adapter or car needs connecting again
        except asyncio.CancelledError:
            job.status = finished or "failed"
            job.write(t("Stopped because you disconnected.", job.lang) + "\n")
            raise
        except Exception as error:
            job.status = finished or "failed"
            job.write(t("Error: {error}", error=error) + "\n")
            raise  # the connection is probably gone, so reconnect
        finally:
            i18n.set_language(previous)
            if not self.args.demo and not elm.demo:
                try:
                    garage.add_test(elm.vehicle or {}, job.action, job.status, job.output)
                except OSError as error:
                    job.write(t("Could not save this car's info: {error}", job.lang, error=error) + "\n")
            if not self.args.demo:
                self.cars = garage.summaries()

    async def run_waiting_jobs(self, elm):
        while self.waiting:
            await self.run_job(elm, self.waiting.pop(0))

    async def run_download(self, job):
        job.status = "running"
        try:
            files = await asyncio.to_thread(car_modules.download_profile, job.params["name"])
            job.write(t("Downloaded {name}: {files}", job.lang, name=job.params["name"], files=", ".join(files)) + "\n")
            job.write(t("Data from OBDb contributors (https://github.com/OBDb), licensed CC BY-SA 4.0.", job.lang) + "\n")
            job.status = "done"
        except Exception as error:
            job.write(t("Error: {error}", job.lang, error=error) + "\n")
            job.status = "failed"

    def fail_waiting_jobs(self):
        for job in self.waiting:
            job.status = "failed"
            job.write(t("Not connected to a car", job.lang) + "\n")
        self.waiting.clear()

    async def api(self, method, path, query, body):
        """Answer the web app's requests. Returns (HTTP status, data)."""
        lang = i18n.normalize(query.get("lang") or body.get("lang"))
        if method == "GET" and path == "/api/catalog":
            return 200, [{"name": name, "local": bool(car_modules.local_profile_files(name))}
                         for name in car_modules.catalog()]
        if method == "GET" and path == "/api/jobs":
            return 200, [job.view() for job in reversed(self.jobs)]
        if method == "POST" and path == "/api/run":
            params = body.get("params") if isinstance(body.get("params"), dict) else {}
            job, refusal = self.submit(body.get("action"), params, lang)
            return (400, {"error": refusal}) if refusal else (200, job.view())
        if method == "GET" and path == "/api/copies":
            return 200, car_copy.list_copies(car_copy.copy_folder(self.args.demo))
        if method == "POST" and path == "/api/copy/save":
            name, refusal = self.save_car_copy(lang)
            return (409, {"error": refusal}) if refusal else (200, {"file": name, "folder": str(car_copy.copy_folder(self.args.demo))})
        if method == "POST" and path == "/api/copy/open":
            refusal = self.open_car_copy(body.get("file"), lang)
            return (409, {"error": refusal}) if refusal else (200, {"file": body.get("file")})
        if method == "POST" and path == "/api/copy/close":
            self.close_car_copy()
            return 200, {}
        if method == "POST" and path == "/api/copy/delete":
            refusal = self.delete_car_copy(body.get("file"), lang)
            return (409, {"error": refusal}) if refusal else (200, {})
        if method == "POST" and path == "/api/check/save":
            if self.snapshot is None or self.reader is None:
                return 409, {"error": t("Not connected to a car", lang)}
            if self.args.demo:
                return 409, {"error": t("Reports aren't saved in demo mode.", lang)}
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            report = REPORT_DIR / f"check_{datetime.now():%Y%m%d_%H%M%S}.txt"
            report.write_text(car_check_report(self.snapshot, self.reader.values, lang) + "\n", encoding="utf-8")
            return 200, {"file": str(report)}
        if method == "GET" and path == "/api/explorer/report":
            if not self.explorer:
                return 409, {"error": t("Map the car's computers first, then read again to compare.", lang)}
            return 200, {"text": car_explorer.report(self.explorer, lang)}
        if method == "GET" and path == "/api/ecu/report":
            if self.snapshot is None or self.reader is None:
                return 409, {"error": t("Not connected to a car", lang)}
            return 200, {"text": ecu_report(self.snapshot, self.reader.values, self.previous, self.elm, self.scan, self.identity_items, lang)}
        if method == "POST" and path == "/api/ecu/save":
            if self.snapshot is None or self.reader is None:
                return 409, {"error": t("Not connected to a car", lang)}
            if self.args.demo:
                return 409, {"error": t("Reports aren't saved in demo mode.", lang)}
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            report = REPORT_DIR / f"ecu_{datetime.now():%Y%m%d_%H%M%S}.txt"
            report.write_text(ecu_report(self.snapshot, self.reader.values, self.previous, self.elm, self.scan, self.identity_items, lang) + "\n", encoding="utf-8")
            return 200, {"file": str(report)}
        if method == "POST" and path == "/api/car/tank":
            key = str(body.get("key", ""))
            litres = body.get("litres")
            valid = litres in (None, "", 0) or (isinstance(litres, (int, float)) and not isinstance(litres, bool) and 10 <= litres <= 300)
            if self.args.demo or not valid or not re.fullmatch(r"[\w-]+", key) or not garage.set_tank(key, litres or None):
                return 400, {"error": t("Enter the tank size in litres, between 10 and 300.", lang)}
            return 200, {}
        if method == "POST" and path == "/api/car/delete":
            key = str(body.get("key", ""))
            if self.args.demo or not re.fullmatch(r"[\w-]+", key) or not garage.delete_car(key):
                return 404, {"error": t("Not found", lang)}
            self.cars = garage.summaries()
            return 200, {}
        if method == "POST" and path == "/api/disconnect":
            refusal = self.disconnect(lang)
            return (409, {"error": refusal}) if refusal else (200, {"link": self.link_state})
        if method == "POST" and path == "/api/connect":
            self.reconnect()
            return 200, {"link": self.link_state}
        if method == "POST" and path == "/api/download":
            name = next((car for car in car_modules.catalog() if car.lower() == str(body.get("name")).lower()), None)
            if name is None:
                return 400, {"error": t("That car isn't in the catalog.", lang)}
            job = self.new_job("download", {"name": name}, lang)
            task = asyncio.create_task(self.run_download(job))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return 200, job.view()
        if method == "POST" and path == "/api/record":
            if not body.get("on"):
                self.stop_recording()
                return 200, self.recording_status(lang)
            refusal = self.start_recording(lang)
            return (400, {"error": refusal}) if refusal else (200, self.recording_status(lang))
        if method == "GET" and path == "/api/dtc":
            code = str(query.get("code", "")).strip().upper()
            if not re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", code):
                return 400, {"error": t("Enter a code like P0420.", lang)}
            details = dtc_details(code) or {}
            return 200, {"code": code, "title": describe_dtc(code, lang), "found": bool(details), **details,
                         "oem": [{"make": make, "meaning": meaning} for make, meaning in sorted(oem_meanings(code).items())],
                         "plans": page_plans(code, lang)}
        if self.args.demo and path in ("/api/reports", "/api/report"):
            # Demo pages get shared and screenshotted, and real reports can contain a VIN.
            return (200, []) if path == "/api/reports" else (404, {"error": t("Not found", lang)})
        if method == "GET" and path == "/api/reports":
            files = sorted(REPORT_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True) if REPORT_DIR.is_dir() else []
            return 200, [{"name": p.name, "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                         for p in files[:100]]
        if method == "GET" and path == "/api/report":
            name = query.get("name", "")
            path_on_disk = REPORT_DIR / name
            if not re.fullmatch(r"[\w-]+\.txt", name) or not path_on_disk.is_file():
                return 404, {"error": t("Not found", lang)}
            return 200, {"name": name, "text": path_on_disk.read_text(encoding="utf-8")}
        if method == "GET" and path == "/api/car":
            key = query.get("key", "")
            record = garage.load_car(key) if re.fullmatch(r"[\w-]+", key) and not self.args.demo else {}
            if not record:
                return 404, {"error": t("Not found", lang)}
            faults = [{**fault, "description": describe_dtc(fault["code"], lang), "system": car_systems.code_system(fault["code"])}
                      for fault in garage.fault_history(record)]
            return 200, {**record, "faults": faults, "timeline": garage.timeline(record)}
        if method == "POST" and path == "/api/car/name":
            key = str(body.get("key", ""))
            if self.args.demo or not re.fullmatch(r"[\w-]+", key) or not garage.set_name(key, body.get("name", "")):
                return 404, {"error": t("Not found", lang)}
            self.cars = garage.summaries()
            return 200, {"ok": True}
        if method == "GET" and path == "/api/activity":
            try:
                after = int(query.get("after", 0))
            except (TypeError, ValueError):
                after = 0
            return 200, self.activity.view(after, lang)
        return 404, {"error": t("Not found", lang)}

    def state(self, lang=None):
        if self.snapshot is None:
            text, values = self.message
            return {"connected": False, "message": t(text, lang, **values), "cars": self.cars,
                    "device": self.device_status(lang), "link": self.link_state, "copy": self.copy_status(), "can_save_copy": False}
        state = dashboard_state(self.snapshot, self.reader, self.args.demo and not self.copy, lang, self.cars)
        state["device"] = self.device_status(lang)
        state["alerts"] = car_alerts(self.snapshot, self.reader.values, lang, self.charging_lasting())
        state["alerts"] += [t("Module {header} says it's busy, so its readings are paused. They usually return within a minute.", lang, header=header)
                            for header in sorted(self.elm.busy_modules())] if self.elm else []
        state["recording"] = self.recording_status(lang)
        state["link"] = self.link_state
        state["copy"] = self.copy_status()
        state["can_save_copy"] = not self.copy and self.elm is not None
        state["analysis"] = page_analysis(self.stats, self.reader.values, self.snapshot, self.previous, lang)
        state["analysis"]["moments"] = [{"time": moment["time"], "file": moment["file"], "reason": t(", ", lang).join(t(name, lang) for name in moment["reason"])}
                                        for moment in reversed(self.moments)]
        state["car_check"] = page_car_check(self.snapshot, self.reader.values, lang, self.previous)
        state["ecus"] = page_ecus(self.snapshot, self.elm.busy_modules() if self.elm else (), lang, self.previous, self.elm.heard if self.elm else None)
        state["explorer"] = page_explorer(self.explorer, lang)
        state["ecu_tools"] = page_ecu_tools(self.snapshot, self.reader.values, self.previous, self.elm, self.scan, self.identity_items, lang)
        state["session"] = self.connections  # changes with every new connection, so the page can drop the last car's graphs
        return state

    def copy_status(self):
        return self.copy and {"name": self.copy.get("name"), "saved": self.copy.get("saved")}

    def start_recording(self, lang=None):
        """Start saving live values to a CSV file. Returns why it can't start, or None."""
        if self.reader is None or self.elm is None:
            return t("Not connected to a car", lang)
        if self.recording:
            return t("Recording is already running.", lang)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stem, number_tried = f"drive_{datetime.now():%Y%m%d_%H%M%S}", 0
        while True:  # never write over a recording made earlier in the same second
            path = LOG_DIR / (f"{stem}.csv" if not number_tried else f"{stem}_{number_tried}.csv")
            try:
                log_file = path.open("x", newline="", encoding="utf-8-sig")
                break
            except FileExistsError:
                number_tried += 1
        writer = csv.writer(log_file)
        pids = list(self.reader.values)
        writer.writerow([t("time", lang)] + [column_title(p, lang) for p in pids])
        self.recording = {"path": path, "file": log_file, "writer": writer, "pids": pids, "rows": 0,
                          "started": time.monotonic(), "highest": {}, "speed_total": 0.0, "speed_rows": 0,
                          # Readings taken before recording began aren't measurements from the recording.
                          "written": dict(self.reader.read_at)}
        self.last_summary = None
        return None

    def record_row(self):
        recording = self.recording
        if not recording or self.reader is None:
            return
        values, read_at, written = self.reader.values, self.reader.read_at, recording["written"]
        # A blank cell means no new reading came, so a value held from earlier isn't mistaken for a measurement.
        cells = ["" if values.get(p) is None or read_at.get(p) == written.get(p) else format_value(values[p]) for p in recording["pids"]]
        written.update({p: read_at.get(p) for p in recording["pids"]})
        recording["writer"].writerow([datetime.now().isoformat(timespec="milliseconds")] + cells)
        recording["file"].flush()
        recording["rows"] += 1
        for pid in (0x0C, 0x0D, 0x05):
            value = number(values.get(pid))
            if value is not None:
                recording["highest"][pid] = max(value, recording["highest"].get(pid, value))
        speed = number(values.get(0x0D))
        if speed is not None:
            recording["speed_total"] += speed
            recording["speed_rows"] += 1

    def stop_recording(self):
        """Close the file and keep a summary of what was recorded."""
        recording = self.recording
        if not recording:
            return
        recording["file"].close()
        seconds = time.monotonic() - recording["started"]
        self.last_summary = {
            "file": str(recording["path"]), "rows": recording["rows"], "seconds": seconds,
            "top_speed": recording["highest"].get(0x0D), "top_rpm": recording["highest"].get(0x0C),
            "top_coolant": recording["highest"].get(0x05),
            "average_speed": recording["speed_total"] / recording["speed_rows"] if recording["speed_rows"] else None}
        self.recording = None

    def recording_status(self, lang=None):
        clock = lambda seconds: f"{int(seconds // 60)}:{int(seconds % 60):02d}"
        if self.recording:
            return {"on": True, "text": t("Recording: {rows} rows, {time}", lang, rows=self.recording["rows"],
                                          time=clock(time.monotonic() - self.recording["started"]))}
        summary = self.last_summary
        if not summary:
            return {"on": False, "lines": []}
        shown = lambda value, unit: "-" if value is None else f"{round(value)} {t(unit, lang)}"
        return {"on": False, "lines": [
            t("Saved log: {path}", lang, path=summary["file"]),
            f"{t('Time recorded', lang)}: {clock(summary['seconds'])}",
            f"{t('Rows recorded', lang)}: {summary['rows']}",
            f"{t('Top speed', lang)}: {shown(summary['top_speed'], 'km/h')}",
            f"{t('Average speed', lang)}: {shown(summary['average_speed'], 'km/h')}",
            f"{t('Highest engine RPM', lang)}: {shown(summary['top_rpm'], 'rpm')}",
            f"{t('Highest coolant temp', lang)}: {shown(summary['top_coolant'], 'C')}"]}

    async def session(self, elm, _):
        self.link = elm
        self.connected_at = time.monotonic()
        self.connections += 1
        self.reader = None
        self.charging_since = self.charging_kind = None
        self.message = ("Connected. Reading everything from the car, this takes about 20 seconds ...", {})
        elm.transport.activity = self.activity
        snapshot = await read_snapshot(elm)
        self.stats = car_analysis.DriveStats()
        self.previous = {}
        if not self.args.demo and not elm.demo:
            with contextlib.suppress(OSError, ValueError, AttributeError):
                self.previous = earlier_visits(garage.load_car(garage.car_key(elm.vehicle or snapshot["vehicle"])))
        if not self.args.demo:
            remember_snapshot(elm, snapshot)
            self.cars = garage.summaries()
            self.refresh_followup(elm, snapshot)
        self.recent.clear()
        self.moment, self.known_trouble = None, None
        self.scan, self.identity_items = self.previous.get("scan"), None
        self.explorer = self.previous.get("explorer")
        elm.explorer = self.explorer
        self.reader = LiveReader(elm, snapshot["values"], snapshot["values"], snapshot["voltage"])
        self.reader.interval = self.args.interval
        self.snapshot = snapshot
        self.elm = elm
        self.start_drive(elm, snapshot)
        self.activity.event("Connected to the car, reading live values.", "good")
        count = 0
        last_refresh = time.monotonic()
        refreshing = None
        try:
            while self.args.samples is None or count < self.args.samples:
                await self.run_waiting_jobs(elm)
                await self.reader.step()
                self.stats.update(self.reader.values, self.reader.read_at, time.monotonic())
                self.watch_moment(elm)
                self.track_charging()
                self.record_row()
                self.track_drive(elm, self.snapshot)
                if refreshing is None and time.monotonic() - last_refresh >= REFRESH_SECONDS:
                    refreshing = self.refresh_parts(elm)
                if refreshing is not None:
                    try:
                        await refreshing.__anext__()
                    except StopAsyncIteration:
                        refreshing = None
                        last_refresh = time.monotonic()
                count += 1
                await asyncio.sleep(self.args.interval)
        finally:
            self.save_moment(elm)  # a connection lost mid-way still keeps what led up to the problem
            self.save_drive(elm, self.snapshot)
            self.drive = None
            self.stop_recording()
            self.elm = self.link = None
            set_car_make(None)
            self.fail_waiting_jobs()

    def adapter_found(self, elm):
        self.adapter = elm
        elm.transport.activity = self.activity
        self.activity.event("Adapter found, talking to it now.")

    async def connect(self):
        if self.copy:
            elm = ELM(car_copy.CopyTransport(self.copy))
            elm.demo = True  # keeps it out of the saved cars, as it isn't a new visit to the car
            elm.device_name = t("Saved car copy")
            elm.transport.activity = self.activity
            await elm.setup()
            await self.session(elm, self.args)
        elif self.args.demo:
            elm = ELM(DemoTransport())
            elm.demo = True
            elm.device_name = "Demo"
            elm.transport.activity = self.activity
            await elm.setup()
            try:
                await self.session(elm, self.args)
            except asyncio.CancelledError:
                await elm.close()
                raise
        else:
            await connect_and_run(self.args, self.session, self.adapter_found)

    async def run(self):
        while True:
            if self.stay_disconnected:
                self.mark_disconnected()
                self.connect_again.clear()
                await self.connect_again.wait()
                continue
            self.skip_wait = False
            self.connecting = asyncio.create_task(self.connect())
            try:
                await self.connecting
                return
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # the app itself is closing
                self.snapshot = self.reader = self.link = self.elm = None
                self.fail_waiting_jobs()
                continue  # only the connection was stopped, by the Disconnect button or a car copy
            except Exception as error:
                self.snapshot = self.reader = self.link = self.elm = None
                self.fail_waiting_jobs()  # or a queued command could run on the next car to connect
                # Only a silent car proves the adapter is still there; any other failure may mean it's gone.
                if not (isinstance(error, OBDError) and error.text in (CAR_SILENT, BUS_PROBLEM, ADAPTER_RESTARTED)):
                    self.adapter = None
                if isinstance(error, OBDError):
                    self.message = (error.text, error.values)
                else:
                    self.message = (str(error), {})
                self.activity.event(*self.message[:1], "bad", **self.message[1])
                print(t("Not connected: {error}", error=error), flush=True)
            finally:
                self.connecting = None
            if not self.stay_disconnected:
                await self.retry_wait()

    async def retry_wait(self):
        """Wait before trying again, but stop waiting at once if the Disconnect button is pressed."""
        waited = 0.0
        while waited < self.RETRY_SECONDS and not self.stay_disconnected and not self.skip_wait:
            await asyncio.sleep(0.2)
            waited += 0.2


def run_backend(args):
    """Run for the desktop window. Stdout carries the pipe's replies, so stray prints go to stderr."""
    replies = sys.stdout.buffer
    sys.stdout = sys.stderr
    asyncio.run(bridge.serve(DashboardApp(args), sys.stdin.buffer, replies))


def battery_advice(volts, running):
    """Explain a battery voltage. Engine-off bands assume a rested 12 V lead-acid battery."""
    if running:
        if volts < CHARGING_LOW_VOLTS:
            return t("Low for a running engine: the alternator may not be charging.")
        if volts > 15.0:
            return t("High for a running engine: the charging system may be overcharging.")
        return t("Normal for a running engine: the battery is being charged.")
    if volts >= 12.6:
        return t("About full. Starting should be fine if the battery is healthy.")
    if volts >= 12.4:
        return t("About 75% charged. It will probably start, but charge it soon.")
    if volts >= 12.2:
        return t("About 50% charged. Starting may be weak, so charge it first.")
    if volts >= 12.0:
        return t("About 25% charged. It may not start, so charge it first.")
    return t("Very low. It probably won't start, so charge it or use a jump starter.")


START_TEST_SECONDS = 20
CRANK_GOOD_VOLTS, CRANK_WEAK_VOLTS = 9.6, 9.0
CRANK_SEEN_DROP = 0.7  # a dip smaller than this isn't a start


async def adapter_volts(elm):
    match = re.search(r"\d+(\.\d+)?", await elm.voltage())
    return float(match.group()) if match else None


async def run_start_test(elm, _):
    """Watch the voltage while the engine is started. The lowest point shows how well the battery copes with the starter."""
    rpm = await elm.read_pid(0x0C)
    if rpm is None or rpm > 0:
        raise OBDError("The start test needs the engine off with the ignition ON.")
    resting = [volts for volts in [await adapter_volts(elm) for _ in range(4)] if volts is not None]
    if not resting:
        raise OBDError("The adapter did not report a voltage.")
    rest = sum(resting) / len(resting)
    # Adapters often read high or low. The car's own reading, taken at the same moment, corrects for that.
    car = number(await elm.read_pid(0x42))
    offset = rest - car if car is not None and abs(rest - car) <= 2.5 else 0.0
    print(t("Resting voltage: {volts} V", volts=f"{rest - offset:.1f}"))
    print(t("Start the engine now. Watching for {seconds} seconds ...", seconds=START_TEST_SECONDS), flush=True)
    lowest, readings, began = rest, 0, time.monotonic()
    while time.monotonic() - began < START_TEST_SECONDS:
        try:
            volts = await adapter_volts(elm)
        except OBDError as error:
            if error.text == ADAPTER_RESTARTED:
                print(t("The adapter lost power during the start, so the voltage fell very low at the port. That points to a weak battery or a poor connection."))
            raise
        if volts is not None:
            readings += 1
            lowest = min(lowest, volts)
    after = await adapter_volts(elm)
    rpm = await elm.read_pid(0x0C)
    if rest - lowest < CRANK_SEEN_DROP:
        if rpm:
            print(t("The engine started, but no voltage dip was caught. The adapter reads voltage too slowly to be sure, so try once more."))
        else:
            print(t("No start was seen. Run the test again and start the engine when asked."))
        return
    dip = lowest - offset
    print(t("Lowest voltage while starting: {volts} V, from {count} readings", volts=f"{dip:.1f}", count=readings))
    if after is not None and rpm:
        print(t("Voltage with the engine running: {volts} V", volts=f"{after - offset:.1f}"))
    if dip >= CRANK_GOOD_VOLTS:
        print(t("The battery held up well while starting."))
    elif dip >= CRANK_WEAK_VOLTS:
        print(t("The battery dipped lower than a healthy one usually does. It's worth testing before cold weather."))
    else:
        print(t("The battery dropped very low while starting. It's probably weak, so have it tested."))
    print(t("The adapter takes a reading only every few tenths of a second, so the true lowest point may be lower still."))


async def run_battery(elm, _):
    """Read voltage without setup(), so it still works when the car's computer won't answer."""
    await elm.transport.send("ATZ", 5)
    for command in ("ATE0", "ATL0", "ATS1", "ATH1", "ATSP0"):
        await elm.transport.send(command, 2)

    readings = []
    for _ in range(5):
        match = re.search(r"\d+(\.\d+)?", await elm.voltage())
        if match:
            readings.append(float(match.group()))
        await asyncio.sleep(1)
    if not readings:
        raise OBDError("The adapter did not report a voltage.")

    module_volts, rpm = None, None
    if await elm.request(0x01, 0x00, wait=25):
        module_volts = await elm.read_pid(0x42)
        rpm = await elm.read_pid(0x0C)
    average = sum(readings) / len(readings)
    running = bool(rpm)

    print(t("Voltage at adapter: {average} V (lowest {low}, highest {high})",
            average=f"{average:.1f}", low=f"{min(readings):.1f}", high=f"{max(readings):.1f}"))
    if module_volts is None:
        print(t("Voltage at car's module: not available"))
    else:
        print(t("Voltage at car's module: {volts} V", volts=f"{module_volts:.1f}"))
    if rpm is None:
        print(t("Engine: unknown, car did not answer"))
    else:
        print(t("Engine: running") if running else t("Engine: off"))
    # On a tested car the adapter read about 1 V higher than the car's own module, across several drives.
    print("\n" + battery_advice(average if module_volts is None else module_volts, running))
    if module_volts is not None:
        print(t("This advice uses the car's module reading, which is usually more accurate than the adapter's."))
    else:
        print(t("This advice uses only the adapter's reading, which can be about 1 V too high, so the battery may be lower than it says."))
    if not running:
        print(t("Ignition-on loads and recent charging can shift this reading."))
        print(t("This doesn't test starting power: a weak battery can read normal and still fail to crank."))


MINIMUM_VOLTS = 11.8


async def check_battery(elm):
    """Long reads drain the battery, so refuse to start them when it's already low."""
    volts = await elm.read_pid(0x42)
    if volts is None:
        match = re.search(r"\d+(\.\d+)?", await elm.voltage())
        volts = float(match.group()) if match else None
    if volts is not None and volts < MINIMUM_VOLTS:
        raise OBDError("Battery is at {volts} V. Charge it or start the engine before this long read.",
                       volts=f"{volts:.1f}")


def show_progress(done, total):
    print("\r  " + t("{done} of {total} ...", done=done, total=total), end="" if done < total else "\n", flush=True)


def save_report(prefix, text, demo=False):
    if demo:
        return
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    print("\n" + t("Saved report: {path}", path=path))


async def run_scan(elm, _):
    await check_battery(elm)
    print(t("Asking each possible module, this takes about a minute ..."), flush=True)
    modules = await car_modules.scan_modules(elm, on_progress=show_progress)
    elm.last_scan = modules
    garage.remember(elm.vehicle, "modules", {"modules": modules})
    report = car_modules.format_scan(modules, describe_dtc)
    print(report)
    save_report("modules", report, elm.demo)


async def run_identity_items(elm, _):
    """Read every standard identity item each computer will share. It changes nothing and needs no unlocking."""
    headers = [header for header in known_modules(elm.vehicle) if re.fullmatch(r"7E[89A-F]", header)] or ["7E8"]
    print(t("Asking each computer for every identity item it will share, about a minute each ...", ), flush=True)
    found = await car_modules.read_identity_items(elm, headers, on_progress=show_progress)
    elm.identity_items = found
    lines = []
    for header, items in found.items():
        lines += ["", t("Computer {header}", header=header)]
        lines += [f"  {item['item']}  {t(item['label'], item=item['item'])}: {item['text']}" for item in items] or ["  " + t("It shared no identity items.")]
    report = "\n".join(lines).strip()
    print("\n" + report)
    save_report("ecu-data", report, elm.demo)


def vehicle_make_and_year(elm):
    vin = next(iter((elm.vehicle or {}).get("VIN", {}).values()), None)
    return make_from_vin(vin), garage.model_year(vin)


async def run_explore(elm, _, deep=False):
    """Find which computers answer and what each will share. It only reads, so nothing on the car can change."""
    make, year = vehicle_make_and_year(elm)
    found = await car_explorer.explore(elm, make, year, deep, say=lambda text: print(text, flush=True))
    elm.explorer = found
    if not elm.demo:
        garage.remember_explorer(elm.vehicle, found)
    report = car_explorer.report(found)
    print("\n" + report)
    save_report("explorer", report, elm.demo)


async def run_explore_deep(elm, args):
    print(t("A deep map also tries the maker's own number ranges, which takes several minutes for each computer that answers."), flush=True)
    await run_explore(elm, args, deep=True)


async def run_explore_again(elm, _):
    """Read again every item that answered last time, and show which changed, such as after opening a door."""
    last = getattr(elm, "explorer", None)
    if not last or not last.get("modules"):
        raise OBDError("Map the car's computers first, then read again to compare.")
    found = await car_explorer.read_again(elm, last)
    changed = [(module["header"], item) for module in found["modules"] for item in module["items"] if item.get("changed")]
    print(t("{count} item(s) changed since the last read.", count=len(changed)))
    for header, item in changed:
        print(f"  {header} {item['request']}: {item['before']} -> {item['raw']}")
    elm.explorer = {**last, "time": found["time"], "modules": [
        {**module, "items": [next((new for new in fresh["items"] if new["request"] == item["request"]), item) for item in module["items"]]}
        for module, fresh in zip(last["modules"], found["modules"])]}
    if not elm.demo:
        garage.remember_explorer(elm.vehicle, elm.explorer)


async def run_profile(elm, args):
    await check_battery(elm)
    profile = car_modules.load_profile(args.name, args.year)
    print(t("Reading profile {name}, this can take a few minutes ...", name=args.name), flush=True)
    values, answered, tried = await car_modules.read_profile(elm, profile, args.year, show_progress)
    garage.remember(elm.vehicle, "profile", {"name": args.name, "year": args.year, "values": values})
    report = car_modules.format_profile(values, answered, tried)
    print(report)
    save_report("profile", report, elm.demo)


def run_profiles(args):
    """Search the car catalog or download a profile. This doesn't need the adapter."""
    if args.get:
        files = car_modules.download_profile(args.get)
        print(t("Downloaded {name}: {files}", name=args.get, files=", ".join(files)))
        print(t("Data from OBDb contributors (https://github.com/OBDb), licensed CC BY-SA 4.0."))
        return
    matches = car_modules.search_catalog(" ".join(args.search))
    for name in matches:
        print(name + ("  " + t("(on this laptop)") if car_modules.local_profile_files(name) else ""))
    print("\n" + t("{count} of {total} profiles. Read one with: .\\obd profile --name NAME --year YEAR",
                   count=len(matches), total=len(car_modules.catalog())))


def run_cars():
    """List the cars saved on this laptop. This doesn't need the adapter."""
    cars = garage.summaries()
    if not cars:
        print(t("No cars saved yet. A car is saved the first time it connects."))
        return
    print(t("Saved cars: {count}, in {folder}", count=len(cars), folder=garage.CARS_DIR))
    for car in cars:
        print(f"\n{car['vin'] or t('VIN not reported')}")
        print(f"  {t('First seen')}: {car['first_seen']}")
        print(f"  {t('Last seen')}: {car['last_seen']}")
        print(f"  {t('Connections')}: {car['connections']}")
        if car["modules"]:
            print(f"  {t('Modules')}: {', '.join(car['modules'])}")
        if car["light"] is not None:
            print(f"  {t('Check engine light')}: {t('ON') if car['light'] else t('off')}")
            print(f"  {t('Stored and pending codes')}: {', '.join(car['codes']) or t('none')}")


async def run_clear(elm, args):
    rpm = await elm.read_pid(0x0C)
    if rpm is None or rpm > 0:
        raise OBDError("Not clearing: the engine must be off with the ignition ON.")
    codes = await read_codes(elm)
    print_codes(codes)
    print("\n" + t("Clearing erases these codes and their saved fault data, and resets the emissions self-tests, which then need driving to finish. It does not fix the fault. Permanent codes stay until the car's own tests pass."))
    # The web app asks for the typed confirmation before it queues this command.
    answer = getattr(args, "answer", None)
    if answer is None:
        answer = await asyncio.to_thread(input, t("Type CLEAR to continue, anything else to cancel: "))
    if answer.strip() != "CLEAR":
        print(t("Cancelled, nothing was changed."))
        return
    if await elm.request(0x04):
        print(t("Codes cleared."))
    else:
        raise OBDError("The car did not confirm the clear. Run `codes` to check.")


async def connect_and_run(args, handler, on_adapter=None):
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakBluetoothNotAvailableError

    print(t("Looking for the adapter ..."), flush=True)
    try:
        if args.address:
            device = await BleakScanner.find_device_by_address(args.address, timeout=20)
        else:
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: any(hint in (d.name or ad.local_name or "").upper() for hint in ADAPTER_NAME_HINTS),
                timeout=20)
    except BleakBluetoothNotAvailableError:
        raise OBDError("Bluetooth is turned off or unavailable. Turn it on in Windows settings and try again.") from None
    if device is None:
        raise OBDError("Adapter not found. Check it is plugged in, the ignition is ON, and no phone is connected to it.")
    async with BleakClient(device, timeout=30) as client:
        write_char = notify_char = None
        for service in client.services:
            if service.uuid.startswith(SERVICE_PREFIX):
                for char in service.characteristics:
                    if "notify" in char.properties and notify_char is None:
                        notify_char = char
                    if {"write", "write-without-response"} & set(char.properties) and write_char is None:
                        write_char = char
        if write_char is None or notify_char is None:
            raise OBDError("This adapter does not have the expected BLE serial service (FFF0).")

        transport = BleTransport(client, write_char, notify_char)
        await client.start_notify(notify_char, transport.on_data)
        elm = ELM(transport)
        elm.device_name = device.name
        elm.device_address = device.address
        if on_adapter:
            on_adapter(elm)
        if handler is not run_battery:
            await elm.setup()
            elm.vehicle = await read_identity(elm)
            try:
                # Keep modules seen on earlier visits, so one that's silent today still gets reported.
                saved = garage.load_car(garage.car_key(elm.vehicle)).get("vehicle", {}).get("Module name", {})
                elm.vehicle["Module name"] = {**saved, **elm.vehicle.get("Module name", {})}
                garage.remember_connection(elm.vehicle, elm.version, elm.protocol)
            except OSError as error:
                print(t("Could not save this car's info: {error}", error=error))
        try:
            await handler(elm, args)
        except asyncio.CancelledError:
            await elm.close()
            raise


def language_from_arguments(arguments):
    """Find --lang before the parser is built, so its help text can be translated."""
    for index, argument in enumerate(arguments):
        if argument == "--lang" and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith("--lang="):
            return argument.split("=", 1)[1]
    return None


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    chosen = language_from_arguments(sys.argv[1:])
    i18n.set_language(chosen or i18n.saved_language())
    if chosen in i18n.LANGUAGES:
        i18n.save_language(chosen)

    parser = argparse.ArgumentParser(description=t("Read fault codes and live data from a car through a Bluetooth LE ELM327 adapter. Only the clear command changes anything in the car, and it asks first."))
    parser.add_argument("--address", help=t("adapter's Bluetooth address (default: find one named OBD or ELM)"))
    parser.add_argument("--lang", choices=i18n.LANGUAGES, help=t("language: en or ar, remembered for next time"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help=t("adapter, voltage, check engine light, self-tests")).set_defaults(handler=run_info)
    commands.add_parser("battery", help=t("check battery voltage")).set_defaults(handler=run_battery)
    commands.add_parser("starttest", help=t("watch battery voltage while you start the engine")).set_defaults(handler=run_start_test)
    commands.add_parser("codes", help=t("read fault codes")).set_defaults(handler=run_codes)
    commands.add_parser("ecuread", help=t("read every identity item each computer will share")).set_defaults(handler=run_identity_items)
    commands.add_parser("explore", help=t("map which computers answer and what each will share")).set_defaults(handler=run_explore)
    commands.add_parser("exploredeep", help=t("map the computers, also trying the maker's own number ranges")).set_defaults(handler=run_explore_deep)
    commands.add_parser("exploreagain", help=t("read the mapped items again and show which changed")).set_defaults(handler=run_explore_again)
    commands.add_parser("report", help=t("read everything once and save it to a file")).set_defaults(handler=run_report)
    commands.add_parser("scan", help=t("find other modules and read their fault codes")).set_defaults(handler=run_scan)
    commands.add_parser("cars", help=t("list cars saved on this laptop")).set_defaults(handler=None)
    profile_command = commands.add_parser("profile", help=t("read extra values using a community car profile"))
    profile_command.add_argument("--name", required=True, help=t("car from `profiles`, such as kia-picanto"))
    profile_command.add_argument("--year", type=int, help=t("model year, to skip commands for other years"))
    profile_command.set_defaults(handler=run_profile)
    profiles_command = commands.add_parser("profiles", help=t("search the car catalog, no adapter needed"))
    profiles_command.add_argument("search", nargs="*", help=t("words to search for, such as toyota camry"))
    profiles_command.add_argument("--get", metavar="NAME", help=t("download a car's profile now"))
    profiles_command.set_defaults(handler=None)
    commands.add_parser("clear", help=t("clear fault codes (asks first)")).set_defaults(handler=run_clear)
    for name, handler, text in (("live", run_live, t("live values in this window")),
                                ("log", run_log, t("live values, saved to CSV")),
                                ("app", None, t("backend for the desktop app, started by npm start"))):
        command = commands.add_parser(name, help=text)
        command.add_argument("--interval", type=float, default=0.02, help=t("seconds between readings"))
        command.add_argument("--samples", type=int, help=t("stop after this many readings"))
        command.set_defaults(handler=handler)
        if name == "app":
            command.add_argument("--demo", action="store_true", help=t("show simulated data, no adapter or car needed"))
    args = parser.parse_args()

    try:
        if args.command == "profiles":
            run_profiles(args)
            return 0
        if args.command == "cars":
            run_cars()
            return 0
        if args.command == "app":
            run_backend(args)
            return 0
        if args.command == "profile":
            # Fetch and check the profile first, so a mistake doesn't use up battery connecting.
            if not car_modules.local_profile_files(args.name):
                print(t("Downloading profile {name} from OBDb ...", name=args.name), flush=True)
                car_modules.download_profile(args.name)
            car_modules.load_profile(args.name, args.year)
        asyncio.run(connect_and_run(args, args.handler))
    except KeyboardInterrupt:
        print("\n" + t("Stopped."))
    except Exception as error:
        print(t("Error: {error}", error=error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
