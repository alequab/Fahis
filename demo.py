"""A simulated adapter and car, so the dashboard can be seen without any hardware."""
import asyncio
import math
import time

DEMO_VIN = "DEMO0000000000000"


def live_bytes(pid, now):
    """Return the simulated data bytes for a mode 01 PID, or None if the demo car lacks it."""
    wave = math.sin(now / 4)
    rpm = int((1800 + 900 * wave) * 4)
    temperature = lambda celsius: [int(celsius + 40)]
    percent = lambda value: [int(value * 255 / 100)]
    table = {
        0x01: [0x81, 0x07, 0xE5, 0x04],  # light on, one code, evaporative test not finished
        0x03: [0x02, 0x00],
        0x04: percent(35 + 15 * wave),
        0x05: temperature(88 + 2 * math.sin(now / 30)),
        0x06: [int(128 + 4 * math.sin(now / 3))],
        0x07: [125],
        0x0B: [int(45 + 20 * wave)],
        0x0C: [rpm >> 8, rpm & 0xFF],
        0x0D: [int(55 + 25 * math.sin(now / 7))],
        0x0E: [int(2 * (14 + 6 * wave) + 128)],
        0x0F: temperature(34),
        0x10: [0x03, int(120 + 80 * wave)],
        0x11: percent(22 + 12 * wave),
        0x13: [0x03],
        0x14: [int(90 + 70 * math.sin(now * 2)), 0x80],
        0x15: [140, 0x80],
        0x1C: [6],
        0x1F: [(int(now) % 3000) >> 8, (int(now) % 3000) & 0xFF],
        0x2F: percent(62),
        0x31: [0x04, 0xD2],
        0x33: [100],
        0x41: [0x00, 0x07, 0xE5, 0x04],
        0x42: [0x36, 0x6E],
        0x46: temperature(24),
        0x51: [1],
    }
    return table.get(pid)


SUPPORTED = sorted(live_bytes(pid, 0) and pid for pid in range(1, 0x60) if live_bytes(pid, 0)) + [0x20, 0x40]


def supported_bytes(base):
    bits = 0
    for pid in SUPPORTED:
        if base < pid <= base + 32:
            bits |= 1 << (base + 32 - pid)
    return list(bits.to_bytes(4, "big"))


# Detailed self-tests: one inside its limits, one outside them, and one that hasn't run.
DEMO_TESTS = {
    "0600": [0x46, 0x00, 0xC0, 0x00, 0x00, 0x01],
    "0620": [0x46, 0x20, 0x80, 0x00, 0x00, 0x00],
    "0601": [0x46, 0x01, 0x85, 0x0B, 0x02, 0xBC, 0x00, 0x64, 0x03, 0xE8],
    "0602": [0x46, 0x02, 0x85, 0x0B, 0x04, 0xB0, 0x00, 0x64, 0x03, 0xE8],
    "0621": [0x46, 0x21, 0xA9, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
}


# What the simulated engine computer reports about itself.
DEMO_ECU = {"22F187": b"DEMO-PART-1", "22F189": b"DEMO-SW-2.0", "22F191": b"DEMO-HW-1.0"}


def frames(header, payload):
    """Format a reply as CAN lines, splitting long ones as a real car does."""
    hexed = lambda data: " ".join(f"{b:02X}" for b in data)
    if len(payload) <= 7:
        return [f"{header} {hexed([len(payload)] + payload)}"]
    lines = [f"{header} {hexed([0x10, len(payload)] + payload[:6])}"]
    rest, number = payload[6:], 1
    while rest:
        lines.append(f"{header} {hexed([0x20 | number] + rest[:7])}")
        rest, number = rest[7:], (number + 1) % 16
    return lines


class DemoTransport:
    """Answers the same commands as a real adapter, with simulated values."""

    async def send(self, command, wait=5):
        await asyncio.sleep(0.03)
        command = command.upper()
        if command.startswith("AT"):
            return {"ATZ": ["ELM327 demo"], "ATDPN": ["A6"], "AT@1": ["Simulated adapter"]}.get(
                command, [f"{14.1 + 0.1 * math.sin(time.time()):.1f}V"] if command == "ATRV" else ["OK"])
        reply = self.reply(command)
        return reply if reply else ["NO DATA"]

    def reply(self, command):
        if command.startswith("01") and len(command) == 5:
            command = command[:4]  # the last digit only says how many replies to expect
        if command.startswith("01") and len(command) > 5:
            payload = [0x41]
            for pid in bytes.fromhex(command[2:]):
                data = live_bytes(pid, time.time())
                payload += [pid] + data if data else []
            return frames("7E8", payload) if len(payload) > 1 else None
        if command.startswith("01") and len(command) == 4:
            pid = int(command[2:], 16)
            if pid in (0x00, 0x20, 0x40):
                return frames("7E8", [0x41, pid] + supported_bytes(pid))
            data = live_bytes(pid, time.time())
            if data is None:
                return None
            lines = frames("7E8", [0x41, pid] + data)
            if pid == 0x01:
                lines += frames("7E9", [0x41, 0x01, 0x00, 0x04, 0x00, 0x00])
            return lines
        if command == "03":
            return frames("7E8", [0x43, 0x01, 0x01, 0x71]) + frames("7E9", [0x43, 0x00])
        if command in ("07", "0A"):
            response = int(command, 16) + 0x40
            return frames("7E8", [response, 0x00]) + frames("7E9", [response, 0x00])
        if command == "1902AF":
            return frames("7E8", [0x59, 0x02, 0xAF, 0x01, 0x71, 0x00, 0x09, 0x04, 0x55, 0x00, 0x20])
        if command == "0904":
            return frames("7E8", [0x49, 0x04, 0x01] + list(b"DEMO-CAL-0001".ljust(16, b"\x00")))
        if command == "0906":
            return frames("7E8", [0x49, 0x06, 0x01, 0x12, 0x34, 0xAB, 0xCD])
        if command in DEMO_ECU:
            return frames("7E8", [0x62, 0xF1, int(command[4:], 16)] + list(DEMO_ECU[command]))
        if command == "190B":
            return frames("7E8", [0x59, 0x0B, 0xAF, 0x04, 0x55, 0x00, 0x20])
        if command == "190D":
            return frames("7E8", [0x59, 0x0D, 0xAF, 0x01, 0x71, 0x00, 0x09])
        if command in DEMO_TESTS:
            return frames("7E8", DEMO_TESTS[command])
        if command == "0902":
            return frames("7E8", [0x49, 0x02, 0x01] + list(DEMO_VIN.encode()))
        if command == "090A":
            return (frames("7E8", [0x49, 0x0A, 0x01] + list(b"ECM-Demo")) +
                    frames("7E9", [0x49, 0x0A, 0x01] + list(b"TCM-Demo")))
        if command == "020200":
            return frames("7E8", [0x42, 0x02, 0x00, 0x01, 0x71])
        if command == "020000":
            return frames("7E8", [0x42, 0x00, 0x00, 0x08, 0x10, 0x00, 0x00])
        if command == "020500":
            return frames("7E8", [0x42, 0x05, 0x00, 0x7D])
        if command == "020C00":
            return frames("7E8", [0x42, 0x0C, 0x00, 0x0A, 0x00])
        return None
