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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import bridge
import car_modules
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


# pid: (name, unit, formula taking the data bytes)
LIVE_PIDS = {
    0x0C: ("Engine RPM", "rpm", lambda d: (256 * d[0] + d[1]) / 4),
    0x0D: ("Speed", "km/h", lambda d: d[0]),
    0x05: ("Coolant temp", "C", lambda d: d[0] - 40),
    0x0F: ("Intake air temp", "C", lambda d: d[0] - 40),
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
    0x46: ("Ambient temp", "C", lambda d: d[0] - 40),
    0x5C: ("Oil temp", "C", lambda d: d[0] - 40),
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
    0x3C: ("Catalyst temp B1S1", "C", lambda d: word(d) / 10 - 40),
    0x3D: ("Catalyst temp B2S1", "C", lambda d: word(d) / 10 - 40),
    0x3E: ("Catalyst temp B1S2", "C", lambda d: word(d) / 10 - 40),
    0x3F: ("Catalyst temp B2S2", "C", lambda d: word(d) / 10 - 40),
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
}
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
# Some cars stop charging for minutes at a time to save fuel, resting near battery voltage, so wait longer
# there. Below the discharging level the battery is really being drained, so the shorter wait applies.
CHARGING_RESTING_ALERT_SECONDS = 600
CHARGING_DISCHARGING_VOLTS = 12.2
CHARGING_LOW_VOLTS = 12.8
CHARGING_HIGH_VOLTS = 15.5

COMMON_MONITORS = ["Misfire", "Fuel system", "Components"]
SPARK_MONITORS = ["Catalyst", "Heated catalyst", "Evaporative system", "Secondary air",
                  "A/C refrigerant", "Oxygen sensor", "Oxygen sensor heater", "EGR"]
DIESEL_MONITORS = ["NMHC catalyst", "NOx aftertreatment", None, "Boost pressure",
                   None, "Exhaust gas sensor", "Particulate filter", "EGR"]


class OBDError(Exception):
    """An error for the user. Keeps its English text so the page can show it in either language."""

    def __init__(self, text, **values):
        self.text = text
        self.values = values
        super().__init__(t(text, **values))


class Reply(bytes):
    """A module's reply. `complete` is False if frames were lost and the reply was cut off."""

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
        payload, length = bytearray(), None
        for frame in frames[header]:
            kind = frame[0] >> 4
            if kind == 0:
                # A later single frame replaces an earlier one, such as "please wait" before the answer.
                length = frame[0] & 0x0F
                payload = bytearray(frame[1:])
            elif kind == 1 and len(frame) > 1:
                length = ((frame[0] & 0x0F) << 8) | frame[1]
                payload.extend(frame[2:])
            elif kind == 2:
                payload.extend(frame[1:])
        reply = Reply(payload[:length])
        reply.complete = length is None or len(payload) >= length
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


def decode_monitors(payload):
    """Decode a mode 01 PID 01 reply into light state, code count and self-tests."""
    a, b, c, d = payload[2:6]
    tests = []
    for bit, name in enumerate(COMMON_MONITORS):
        if b & (1 << bit):
            tests.append((name, not b & (1 << (bit + 4))))
    for bit, name in enumerate(DIESEL_MONITORS if b & 0x08 else SPARK_MONITORS):
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
        text = self.buffer.decode("ascii", "replace").replace(">", "")
        return [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]


class MeasuredTransport:
    """Passes commands to the adapter while recording how it responds."""

    def __init__(self, transport):
        self.transport = transport
        self.sent = self.replied = self.timeouts = 0
        self.total_seconds = 0.0
        self.last_reply = None

    async def send(self, command, wait=5):
        started = time.monotonic()
        self.sent += 1
        reply = await self.transport.send(command, wait)
        if reply is None:
            self.timeouts += 1
        else:
            self.replied += 1
            self.total_seconds += time.monotonic() - started
            self.last_reply = time.monotonic()
        return reply


CAR_SILENT = "The car did not answer. Check the ignition is ON and the adapter is seated."
PID_RANGE_ATTEMPTS = 4


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

        if not await self.request(0x01, 0x00, wait=25):
            raise OBDError(CAR_SILENT)

        number = "".join(await self.transport.send("ATDPN", 2) or []).lstrip("A")
        if number not in SUPPORTED_PROTOCOLS:
            raise OBDError("This tool handles 11-bit CAN only; the car uses protocol '{number}'.", number=number)
        self.protocol = SUPPORTED_PROTOCOLS[number]

    async def request(self, mode, pid=None, wait=5, frame=None):
        """Return {module header: payload}, retrying once if a reply is lost or stale."""
        command = f"{mode:02X}" + ("" if pid is None else f"{pid:02X}") + ("" if frame is None else f"{frame:02X}")
        expected = bytes([mode + 0x40] + ([] if pid is None else [pid]))
        for _ in range(2):
            lines = await self.transport.send(command, wait)
            if lines is None:
                continue
            good = {h: p for h, p in parse_can(lines).items() if p.startswith(expected)}
            if good:
                return good
            if any("NO DATA" in line for line in lines):
                return {}
        return {}

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
            if not known <= set(replies):
                await asyncio.sleep(0.2)
        return dict(sorted(replies.items()))

    async def module_request(self, hdr, rax, request, fc=False, wait=2):
        """Send a read request to one module. Returns its reply or refusal, or None if silent."""
        if request[:2] not in car_modules.READ_ONLY_SERVICES:
            raise OBDError("Refusing to send '{request}': only read requests are allowed.", request=request)
        for command in (f"ATSH{hdr}", f"ATCRA{rax}"):
            await self.transport.send(command, 2)
        if fc:
            for command in (f"ATFCSH{hdr}", "ATFCSD300000", "ATFCSM1"):
                await self.transport.send(command, 2)
        lines = await self.transport.send(request, wait)
        if fc:
            await self.transport.send("ATFCSM0", 2)
        reply = parse_can(lines or []).get(rax)
        service = int(request[:2], 16)
        if not reply or (reply[0] != service + 0x40 and reply[:2] != bytes([0x7F, service])):
            return None
        return reply

    async def restore_addressing(self):
        """Go back to asking all modules, so standard requests work again."""
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
            good = {h: p for h, p in parse_can(lines or []).items() if p.startswith(bytes([0x41, pid]))}
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
        for header, payload in sorted(parse_can(lines or []).items()):
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
        else:
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
    code = next((c for p in replies.values() for c in decode_dtcs(bytes(2) + p[3:5])), None)
    if code is None:
        return None
    supported = set()
    for payload in (await elm.request(0x02, 0x00, frame=0)).values():
        supported |= bits_to_pids(payload[3:7], 0x00)
    values = []
    for pid in (p for p in LIVE_PIDS if p in supported):
        for payload in (await elm.request(0x02, pid, frame=0)).values():
            name, unit, formula = LIVE_PIDS[pid]
            values.append({"name": name, "unit": unit, "text": format_value(formula(payload[3:]))})
            break
    return {"code": code, "values": values}


async def read_identity(elm):
    """Read what identifies this car: VIN, calibration IDs and module names."""
    vehicle = {}
    for pid, label in ((0x02, "VIN"), (0x04, "Calibration ID"), (0x06, "Calibration check"), (0x0A, "Module name")):
        replies = await elm.request_all(0x09, pid)  # a module missed here would be forgotten for the session
        if pid == 0x06:
            vehicle[label] = {h: p[3:].hex().upper() for h, p in replies.items()}
        else:
            vehicle[label] = {h: decode_text(p) for h, p in replies.items()}
    set_car_make(make_from_vin(next(iter(vehicle["VIN"].values()), None)))
    return vehicle


async def read_this_drive(elm):
    """Read which self-tests have finished this drive. In this data a set bit means not finished."""
    for payload in (await elm.request(0x01, THIS_DRIVE_PID)).values():
        if len(payload) >= 6:
            return decode_monitors(payload)["tests"]
    return None


async def read_snapshot(elm):
    """Read everything the car offers through standard OBD-II, once."""
    vehicle = elm.vehicle or await read_identity(elm)
    monitors = {h: decode_monitors(p) for h, p in (await elm.request_all(0x01, 0x01)).items()}
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
    return {
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
    LOG_DIR.mkdir(exist_ok=True)
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
        "adapter": snapshot["adapter"],
        "protocol": snapshot["protocol"],
        "voltage": t(reader.voltage, lang),
        "known_modules": known_modules(snapshot["vehicle"]),
        "vehicle": {t(label, lang): modules for label, modules in snapshot["vehicle"].items()},
        "monitors": {h: {**status, "tests": [[t(name, lang), done] for name, done in status["tests"]]}
                     for h, status in snapshot["monitors"].items()},
        "codes": {t(label, lang): {h: [{"code": c, "description": describe_dtc(c, lang)} for c in module_codes]
                                   for h, module_codes in modules.items()}
                  for label, modules in snapshot["codes"].items()},
        "freeze": freeze and {
            "code": freeze["code"], "description": describe_dtc(freeze["code"], lang),
            "values": [{"name": t(v["name"], lang), "unit": t(v["unit"], lang), "text": t(v["text"], lang)}
                       for v in freeze["values"]]},
        "values": [{"pid": f"{pid:02X}", "name": t(LIVE_PIDS[pid][0], lang), "unit": t(LIVE_PIDS[pid][1], lang),
                    "value": value if isinstance(value, (int, float)) else None,
                    "text": "-" if value is None else t(format_value(value), lang),
                    "note": reader.age_note(pid, lang)}
                   for pid, value in reader.values.items()],
        "this_drive": [[t(name, lang), done] for name, done in snapshot["this_drive"] or []],
        "more": snapshot["more"],
        "raw": [{"pid": f"{pid:02X}", "text": text or t("no reply", lang)} for pid, text in snapshot["raw"].items()],
    }


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
WEB_ACTIONS = ("battery", "info", "codes", "report", "scan", "profile", "clear")
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
            snapshot["monitors"][header] = decode_monitors(payload)
        yield
        for label, mode in CODE_MODES:
            replies = await elm.request_all(mode, attempts=1)
            snapshot["codes"].setdefault(label, {}).update({h: decode_dtcs(p) for h, p in replies.items()})
            yield
        if 0x41 in snapshot["supported"]:
            snapshot["this_drive"] = await read_this_drive(elm) or snapshot["this_drive"]
        if json.dumps([snapshot["monitors"], snapshot["codes"]], sort_keys=True, default=str) != before:
            if any(snapshot["codes"]["Stored"].values()) and not snapshot["freeze"]:
                snapshot["freeze"] = await read_freeze_frame(elm)
            remember_snapshot(elm, snapshot)

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
        try:
            with contextlib.redirect_stdout(job):
                if job.action == "profile" and not car_modules.local_profile_files(job.params["name"]):
                    print(t("Downloading profile {name} from OBDb ...", name=job.params["name"]), flush=True)
                    await asyncio.to_thread(car_modules.download_profile, job.params["name"])
                handler = {"battery": run_battery, "info": run_info, "codes": run_codes, "report": run_report,
                           "scan": run_scan, "profile": run_profile, "clear": run_clear}[job.action]
                await handler(elm, SimpleNamespace(**job.params))
            job.status = "done"
            if job.action == "clear":
                self.snapshot = await read_snapshot(elm)
                for pid, value in self.snapshot["values"].items():
                    self.reader.store(pid, value)
            elif job.action == "battery":
                await elm.setup()  # the battery check resets the adapter
        except (OBDError, ValueError) as error:
            job.status = "failed"
            job.write(f"{error}\n")
        except Exception as error:
            job.status = "failed"
            job.write(t("Error: {error}", error=error) + "\n")
            raise  # the connection is probably gone, so reconnect
        finally:
            i18n.set_language(previous)
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
            return 200, record
        return 404, {"error": t("Not found", lang)}

    def state(self, lang=None):
        if self.snapshot is None:
            text, values = self.message
            return {"connected": False, "message": t(text, lang, **values), "cars": self.cars,
                    "device": self.device_status(lang)}
        state = dashboard_state(self.snapshot, self.reader, self.args.demo, lang, self.cars)
        state["device"] = self.device_status(lang)
        state["alerts"] = car_alerts(self.snapshot, self.reader.values, lang, self.charging_lasting())
        state["recording"] = self.recording_status(lang)
        return state

    def start_recording(self, lang=None):
        """Start saving live values to a CSV file. Returns why it can't start, or None."""
        if self.reader is None or self.elm is None:
            return t("Not connected to a car", lang)
        if self.recording:
            return t("Recording is already running.", lang)
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"drive_{datetime.now():%Y%m%d_%H%M%S}.csv"
        log_file = path.open("w", newline="", encoding="utf-8-sig")
        writer = csv.writer(log_file)
        pids = list(self.reader.values)
        writer.writerow([t("time", lang)] + [column_title(p, lang) for p in pids])
        self.recording = {"path": path, "file": log_file, "writer": writer, "pids": pids, "rows": 0,
                          "started": time.monotonic(), "highest": {}, "speed_total": 0.0, "speed_rows": 0}
        self.last_summary = None
        return None

    def record_row(self):
        recording = self.recording
        if not recording or self.reader is None:
            return
        values = self.reader.values
        recording["writer"].writerow([datetime.now().isoformat(timespec="milliseconds")]
                                     + ["" if values.get(p) is None else format_value(values[p]) for p in recording["pids"]])
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
        snapshot = await read_snapshot(elm)
        if not self.args.demo:
            remember_snapshot(elm, snapshot)
            self.cars = garage.summaries()
        self.reader = LiveReader(elm, snapshot["values"], snapshot["values"], snapshot["voltage"])
        self.reader.interval = self.args.interval
        self.snapshot = snapshot
        self.elm = elm
        count = 0
        last_refresh = time.monotonic()
        refreshing = None
        try:
            while self.args.samples is None or count < self.args.samples:
                await self.run_waiting_jobs(elm)
                await self.reader.step()
                self.track_charging()
                self.record_row()
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
            self.stop_recording()
            self.elm = self.link = None
            set_car_make(None)
            self.fail_waiting_jobs()

    def adapter_found(self, elm):
        self.adapter = elm

    async def connect(self):
        if self.args.demo:
            elm = ELM(DemoTransport())
            elm.demo = True
            elm.device_name = "Demo"
            await elm.setup()
            await self.session(elm, self.args)
        else:
            await connect_and_run(self.args, self.session, self.adapter_found)

    async def run(self):
        while True:
            try:
                await self.connect()
                return
            except Exception as error:
                self.snapshot = self.reader = self.link = None
                # Only a silent car proves the adapter is still there; any other failure may mean it's gone.
                if not (isinstance(error, OBDError) and error.text == CAR_SILENT):
                    self.adapter = None
                if isinstance(error, OBDError):
                    self.message = (error.text, error.values)
                else:
                    self.message = (str(error), {})
                print(t("Not connected: {error}", error=error), flush=True)
            await asyncio.sleep(self.RETRY_SECONDS)


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
    garage.remember(elm.vehicle, "modules", {"modules": modules})
    report = car_modules.format_scan(modules, describe_dtc)
    print(report)
    save_report("modules", report, elm.demo)


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
    print("\n" + t("Clearing erases these codes and their saved fault data, and resets the emissions self-tests, which then need driving to finish. It does not fix the fault."))
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
        await handler(elm, args)


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
    commands.add_parser("codes", help=t("read fault codes")).set_defaults(handler=run_codes)
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
