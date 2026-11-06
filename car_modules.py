"""Read other modules: find which ones answer, read their codes, and read profile values."""
import json
import re
import urllib.error
from functools import lru_cache
import urllib.request
from datetime import date
from pathlib import Path

from dtc_codes import dtc_name
from i18n import t
from paths import DATA_HOME

PROFILE_DIR = Path(__file__).parent / "profiles"
DOWNLOAD_DIR = DATA_HOME / "profiles" / "downloaded"
CATALOG_FILE = PROFILE_DIR / "catalog.json"
STANDARD_PIDS_FILE = Path(__file__).parent / "data" / "standard-pids.json"
OBDB_API = "https://api.github.com/repos/OBDb/{repo}/contents/signalsets/v3"
OBDB_RAW = "https://raw.githubusercontent.com/OBDb/{repo}/HEAD/signalsets/v3/{file}"

# Only read requests may be sent to modules: read codes (18, 19) and read values (21, 22).
READ_ONLY_SERVICES = {"18", "19", "21", "22"}

# Command settings this tool doesn't handle. din/dout would open a special diagnostic session,
# which goes beyond plain reading. The others need addressing or connection setup it doesn't do.
UNSUPPORTED_SETTINGS = {"din", "dout", "eax", "tst", "pri", "proto", "baud", "init"}

# Stop asking a module after this many requests in a row get no reply.
MAX_SILENT = 3

# Mask AF leaves out "test not completed" entries. Asking for everything (FF) got a reply of 359
# bytes from a real engine module, which came back cut off after the first frame.
UDS_READ_CODES = "1902AF"
KWP_READ_CODES = "1800FF00"
FIRST_FAULT_REQUEST, LATEST_FAULT_REQUEST = "190B", "190D"
# Stop asking a computer about itself after this many silent replies in a row.
ECU_SILENT_LIMIT = 2
# Standard identity values a computer can report about itself.
ECU_DETAILS = (("22F187", "Part number"), ("22F189", "Software version"), ("22F191", "Hardware number"), ("22F195", "Supplier software version"),
               ("22F197", "System name"), ("22F199", "Last programmed"), ("22F190", "Stored VIN"), ("22F100", "Maker's description"))
SEVERITY_REQUEST = "1908E0AF"
SEVERITIES = ((0x80, "Check immediately"), (0x40, "Check at next stop"), (0x20, "Needs maintenance"))

# Why a module refused a request, from its reply 7F <service> <reason>.
REFUSALS = {
    0x10: "General refusal", 0x11: "Request type not supported", 0x12: "Request option not supported",
    0x13: "Request not understood", 0x14: "Reply would be too long", 0x21: "Busy", 0x22: "Conditions not right, such as engine state",
    0x24: "Requests in the wrong order", 0x25: "A connected part didn't answer", 0x26: "A fault prevents this request",
    0x31: "Value not available", 0x33: "Locked by the maker's security", 0x34: "Locked by the maker's security", 0x37: "Asked again too soon",
    0x78: "Still working", 0x7E: "Not available in normal mode", 0x7F: "Not available in normal mode",
    0x81: "Engine speed too high", 0x82: "Engine speed too low", 0x83: "Needs the engine off", 0x84: "Needs the engine running",
    0x88: "Vehicle speed too high", 0x89: "Vehicle speed too low", 0x8F: "Needs the brake pressed", 0x90: "Needs the shifter in park",
    0x92: "Voltage too high", 0x93: "Voltage too low", 0x94: "Temporarily unavailable",
}
# Reasons that mean ask again, with the seconds to wait first.
TRY_AGAIN = {0x21: 0.2, 0x78: 1, 0x94: 1}

# What the other status bits of a UDS fault record mean. See ISO 15765-3 for the bits.
STATUS_DETAILS = ((0x80, "Asking for the warning light"), (0x02, "Failed on this drive"),
                  (0x40, "Test not finished on this drive"), (0x10, "Test not finished since codes were cleared"))

# Request IDs to try. Replies come from the ID plus 8, so only low digits 0-7 can be requests.
SCAN_IDS = [i for i in range(0x700, 0x7E8) if i & 0x0F <= 7]

# What the Picanto profile's signals suggest each module does. Other cars may differ.
LIKELY_MODULES = {
    "770": "Body control (doors, belts, lights)",
    "7B3": "Climate control",
    "7C6": "Instrument cluster",
    "7D1": "ABS/stability control (wheel speeds)",
    "7D4": "Power steering",
    "7E0": "Engine",
    "7E1": "Transmission",
}

UNITS = {
    "kilometersPerHour": "km/h", "celsius": "C", "liters": "L", "volts": "V", "kilometers": "km",
    "miles": "mi", "degrees": "deg", "percent": "%", "amps": "A", "rpm": "rpm", "kilopascal": "kPa",
    "milliseconds": "ms", "minutes": "min", "fahrenheit": "F", "newtonMeters": "Nm",
    "metersPerSecondSquared": "m/s2", "ampereHours": "Ah", "bars": "bar", "millivolts": "mV",
    "seconds": "s", "millimeters": "mm", "kiloohms": "kOhm", "psi": "psi", "kilowatts": "kW",
    "meters": "m", "hours": "h", "milliamps": "mA", "hertz": "Hz", "watts": "W", "kilowattHours": "kWh",
    "kilowattHoursPer100Kilometers": "kWh/100km", "kilowattHoursPer100Miles": "kWh/100mi", "milliohms": "mOhm",
    "radians": "rad", "litersPerHour": "L/h", "centimeters": "cm", "gravity": "g", "gramsPerSecond": "g/s",
    "kilogramsPerHour": "kg/h", "gramsPerLiter": "g/L", "gallons": "gal", "milligramsPerStroke": "mg/stroke",
    "ohms": "Ohm", "megaohms": "MOhm", "metersPerSecond": "m/s", "milesPerHour": "mph", "inches": "in",
    "poundFoot": "lb-ft",
}
TWO_STATE_UNITS = {"noyes": ("No", "Yes"), "yesno": ("Yes", "No"), "offon": ("Off", "On"), "onoff": ("On", "Off")}


def catalog():
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def search_catalog(term=""):
    """Return catalog cars whose name contains every word in the search."""
    words = term.lower().replace("-", " ").split()
    return [name for name in catalog() if all(word in name.lower() for word in words)]


def profile_key(name):
    return name.strip().lower()


def local_profile_files(name):
    """Return the profile files already on this laptop, as {file name: path}."""
    key = profile_key(name)
    bundled = PROFILE_DIR / f"{key}.json"
    if key != "catalog" and bundled.exists():
        return {"default.json": bundled}
    folder = DOWNLOAD_DIR / key
    if folder.is_dir():
        return {p.name: p for p in folder.glob("*.json") if p.name != "source.json"}
    return {}


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "fahis"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download_profile(name):
    """Download a catalog car's profile from OBDb. Only names in the catalog can be requested."""
    repo = next((car for car in catalog() if car.lower() == profile_key(name)), None)
    if repo is None:
        raise ValueError(t("'{name}' isn't in the catalog. Search for your car with: .\\obd profiles {search}",
                           name=name, search=name.split("-")[0]))
    try:
        files = [f["name"] for f in fetch_json(OBDB_API.format(repo=repo))
                 if re.fullmatch(r"default\.json|\d{4}(-\d{4})?\.json", f["name"])]
    except urllib.error.URLError:
        files = ["default.json"]  # the listing can be rate limited; the raw file usually isn't

    downloaded = {}
    for file in files:
        profile = fetch_json(OBDB_RAW.format(repo=repo, file=file))
        if not isinstance(profile.get("commands"), list):
            raise ValueError(t("{repo}/{file} doesn't look like a car profile.", repo=repo, file=file))
        downloaded[file] = profile
    folder = DOWNLOAD_DIR / profile_key(repo)
    folder.mkdir(parents=True, exist_ok=True)
    for file, profile in downloaded.items():
        (folder / file).write_text(json.dumps(profile), encoding="utf-8")
    (folder / "source.json").write_text(json.dumps({
        "source": f"https://github.com/OBDb/{repo}", "authors": "OBDb editors and contributors",
        "licence": "CC BY-SA 4.0", "retrieved": date.today().isoformat()}, indent=2), encoding="utf-8")
    return sorted(downloaded)


def pick_profile_file(files, year):
    """Choose the narrowest year-range file containing the year, otherwise default.json."""
    matches = []
    for file in files:
        found = re.fullmatch(r"(\d{4})(?:-(\d{4}))?\.json", file)
        if found and year is not None:
            start, end = int(found.group(1)), int(found.group(2) or found.group(1))
            if start <= year <= end:
                matches.append((end - start, file))
    if matches:
        return min(matches)[1]
    return "default.json" if "default.json" in files else None


def load_profile(name, year=None):
    files = local_profile_files(name)
    if not files:
        raise ValueError(t("No profile '{name}' on this laptop. Search for your car with: .\\obd profiles", name=name))
    file = pick_profile_file(files, year)
    if file is None:
        raise ValueError(t("Profile '{name}' has nothing for {year}. It has: {files}",
                           name=name, year=year, files=", ".join(sorted(files))))
    return json.loads(files[file].read_text(encoding="utf-8"))


def applies_to_year(rule, year):
    """Apply OBDb's year filter. Without a known year, every command is tried."""
    if not rule or year is None:
        return True
    start, end = rule.get("from"), rule.get("to")
    if start is not None and end is not None:
        if start < end:
            if start <= year <= end:
                return True
        elif year >= start or year <= end:
            return True
    elif end is not None:
        if year <= end:
            return True
    elif start is not None:
        if year >= start:
            return True
    return year in rule.get("years", [])


def usable_command(command, year=None):
    """Return (hdr, rax, service, argument) if this tool can safely send it, otherwise None."""
    if UNSUPPORTED_SETTINGS & set(command) or not applies_to_year(command.get("filter"), year):
        return None
    hdr = command.get("hdr", "")
    if not re.fullmatch(r"[0-7][0-9A-Fa-f]{2}", hdr) or hdr == "7DF":
        return None
    rax = command.get("rax") or f"{int(hdr, 16) + 8:03X}"
    cmd = command.get("cmd")
    if not re.fullmatch(r"[0-7][0-9A-Fa-f]{2}", rax) or not isinstance(cmd, dict) or len(cmd) != 1:
        return None
    (service, argument), = cmd.items()
    if service not in READ_ONLY_SERVICES or not re.fullmatch(r"([0-9A-Fa-f]{2})+", str(argument)):
        return None
    return hdr, rax, service, str(argument)


def decode_signal(fmt, data):
    """Decode one value using OBDb's scaling rules. Returns None if absent or marked as no data."""
    start, length = fmt.get("bix", 0), fmt["len"]
    if start + length > len(data) * 8:
        return None
    if "map" in fmt:
        raw = 0
        for i in range(start, start + length):
            raw = (raw << 1) | ((data[i // 8] >> (7 - i % 8)) & 1)
        entry = fmt["map"].get(str(raw))
        if entry is None:
            return None
        return entry.get("description") or entry.get("value") if isinstance(entry, dict) else str(entry)
    if fmt.get("blsb") and length > 8:
        first = start // 8
        count = (length + 7) // 8
        data = data[:first] + data[first:first + count][::-1] + data[first + count:]
    raw = 0
    for i in range(start, start + length):
        raw = (raw << 1) | ((data[i // 8] >> (7 - i % 8)) & 1)
    if fmt.get("sign") and raw & (1 << (length - 1)):
        raw -= 1 << length

    value = raw * fmt.get("mul", 1) / fmt.get("div", 1) + fmt.get("add", 0)
    low, high = fmt.get("min", 0), fmt.get("max", 0)
    if high > low:
        value = max(low, min(value, high))
    if "nullmin" in fmt and value <= fmt["nullmin"]:
        return None
    if "nullmax" in fmt and value >= fmt["nullmax"]:
        return None
    return value


def format_signal(fmt, value):
    if isinstance(value, str):
        return value
    unit = fmt.get("unit", "")
    if unit in TWO_STATE_UNITS:
        return t(TWO_STATE_UNITS[unit][1 if value else 0])
    if unit == "hex":
        return f"{int(value):X}"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    label = t(UNITS.get(unit, ""))
    return f"{text} {label}".strip()


@lru_cache(maxsize=1)
def standard_pids():
    """Return OBDb's definitions of the standard mode 01 values, by PID number."""
    commands = json.loads(STANDARD_PIDS_FILE.read_text(encoding="utf-8"))["commands"]
    return {int(command["cmd"]["01"], 16): command for command in commands if "01" in command["cmd"]}


def decode_standard(pid, data):
    """Decode a standard value this tool has no tested decoder for. Returns a list of name and text.

    Many values start with flags saying which sensors exist. If the car flags none of them, what
    follows isn't real data: one recorded reply would otherwise decode to a 3,647 C turbo.
    """
    command = standard_pids().get(pid)
    if command is None:
        return []
    flags = [s for s in command["signals"] if s.get("hidden") and s["id"].endswith("_SUP")]
    if flags and not any(decode_signal(s["fmt"], data) for s in flags):
        return []
    results = []
    for signal in command["signals"]:
        if signal.get("hidden"):
            continue
        value = decode_signal(signal["fmt"], data)
        if value is not None:
            results.append({"name": signal["name"], "text": format_signal(signal["fmt"], value), "path": signal.get("path", "")})
    return results


def decode_uds_codes(payload):
    """Decode a 59 02 reply: mask byte, then 3-byte code and status byte for each fault.

    Modules can repeat a code, so each is listed once with its most serious state. A code with
    none of the active, confirmed or pending bits only failed at some point since the last clear.
    """
    worst, details = {}, {}
    order = ("history", "pending", "stored", "active")
    body = payload[3:]
    for i in range(0, len(body) - 3, 4):
        status = body[i + 3]
        if any(body[i:i + 3]):
            if not status & 0xAF:
                continue  # It reports tests that haven't finished, not a fault that happened.
            state = "active" if status & 0x01 else "stored" if status & 0x08 else "pending" if status & 0x04 else "history"
            code = f"{dtc_name(body[i], body[i + 1])}-{body[i + 2]:02X}"
            if order.index(state) >= order.index(worst.get(code, "history")):
                worst[code] = state
            found = details.setdefault(code, [])
            found += [text for bit, text in STATUS_DETAILS if status & bit and text not in found]
    return [{"code": code, "state": state, "details": details[code]} for code, state in worst.items()]


def refusal_reason(reply):
    """Name the reason in a 7F refusal, or None if this isn't one."""
    if not reply or reply[0] != 0x7F or len(reply) < 3:
        return None
    return REFUSALS.get(reply[2], "Reason not known")


def decode_severity(payload):
    """Decode a 59 08 reply into {code: how urgent}: availability byte, then severity, unit, 3-byte code and status."""
    found = {}
    if not payload or payload[:2] != bytes([0x59, 0x08]):
        return found
    body = payload[3:]
    for i in range(0, len(body) - 5, 6):
        label = next((text for bit, text in SEVERITIES if body[i] & bit), None)
        if label:
            found[f"{dtc_name(body[i + 2], body[i + 3])}-{body[i + 4]:02X}"] = label
    return found


def decode_one_record(reply, subfunction=None):
    """Decode a 59 reply holding a single fault, such as the first or latest. None if there's no such fault."""
    if subfunction is not None and (not reply or len(reply) < 2 or reply[1] != subfunction):
        return None
    if reply and reply[0] == 0x59 and len(reply) >= 7 and any(reply[3:6]):
        return f"{dtc_name(reply[3], reply[4])}-{reply[5]:02X}"
    return None


def decode_kwp_codes(payload):
    """Decode a 58 reply: count byte, then 2-byte code and status byte for each fault."""
    codes = []
    body = payload[2:]
    for i in range(0, len(body) - 2, 3):
        if body[i] or body[i + 1]:
            state = "active" if body[i + 2] & 0x40 else "stored"
            codes.append({"code": dtc_name(body[i], body[i + 1]), "state": state})
    return codes


async def scan_modules(elm, ids=SCAN_IDS, on_progress=None):
    """Ask each possible module for its codes. A refusal still shows that the module exists."""
    found = []
    try:
        for index, number in enumerate(ids):
            if on_progress:
                on_progress(index + 1, len(ids))
            hdr, rax = f"{number:03X}", f"{number + 8:03X}"
            reply = await elm.module_request(hdr, rax, UDS_READ_CODES, wait=1)
            if reply is None:
                continue
            if not getattr(reply, "complete", True):
                # A long list needs the adapter to tell the module to carry on, which isn't automatic for every address.
                reply = await elm.module_request(hdr, rax, UDS_READ_CODES, fc=True, wait=2) or reply
            module = {"hdr": hdr, "reply_id": rax, "name": LIKELY_MODULES.get(hdr, "Unknown module"),
                      "codes": [], "read_codes": True, "complete": getattr(reply, "complete", True)}
            if reply[0] == 0x59:
                module["codes"] = decode_uds_codes(reply)
            else:
                older = await elm.module_request(hdr, rax, KWP_READ_CODES, wait=1)
                if older is not None and older[0] == 0x58:
                    module["codes"] = decode_kwp_codes(older)
                else:
                    module["read_codes"] = False
                    module["refusal"] = refusal_reason(reply)
            found.append(module)
    finally:
        await elm.restore_addressing()
    return found


def format_scan(modules, descriptions):
    lines = [t("Modules that answered: {count}", count=len(modules)), ""]
    for module in modules:
        lines.append(f"{module['hdr']}  {t(module['name'])}")
        if not module["read_codes"]:
            lines.append("  " + t("Answered, but refused to share fault codes with these commands"))
            if module.get("refusal"):
                lines.append("  " + t("Reason: {reason}", reason=t(module["refusal"])))
        elif not module.get("complete", True):
            lines.append("  " + t("The reply was cut off, so this list may be missing codes. Run the codes command to check."))
        elif not module["codes"]:
            lines.append("  " + t("No fault codes"))
        for fault in module["codes"]:
            lines.append(f"  {fault['code']}  {t(fault['state'])}  {descriptions(fault['code'][:5])}")
    if not modules:
        lines.append(t("None. The car may block these requests, or the adapter may not support them."))
    lines.append("")
    if any(fault["state"] == "history" for module in modules for fault in module["codes"]):
        lines.append(t("\"history\" means the code failed at some point since codes were last cleared, and isn't active or confirmed now."))
    lines += [t("Module names are likely roles based on a Kia Picanto profile, not confirmed for your car."),
              t("Modules can also exist without answering here, so this list may be incomplete.")]
    return "\n".join(lines)


def readable_date(data):
    """Read a date stored as digits, such as 20 26 01 15 or 26 01 15. None if it isn't one."""
    digits = bytes(data).hex()
    if len(digits) == 6:
        digits = "20" + digits
    if len(digits) == 8 and digits.isdigit() and 1 <= int(digits[4:6]) <= 12 and 1 <= int(digits[6:8]) <= 31:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def readable_detail(data):
    """Show a reported value as text when it is text, and as hex when it isn't. Only filler bytes means nothing is stored."""
    text = bytes(data).rstrip(b"\x00\xff ").decode("ascii", "replace")
    if not text:
        return None  # A real transmission computer filled its part number with FF.
    if all(32 <= ord(character) < 127 for character in text):
        return text
    return bytes(data).hex(" ").upper()


async def read_ecu_details(elm, headers):
    """Read what each computer reports about itself. Returns {reply header: [{label, text}]}."""
    details = {}
    try:
        for header in headers:
            found = details.setdefault(header, [])
            request_id = f"{int(header, 16) - 8:03X}"
            silent = 0
            for request, label in ECU_DETAILS:
                reply = await elm.module_request(request_id, header, request, keep_address=True)
                if reply is None:
                    # A real transmission computer answered two of these, skipped one, and could still have answered more.
                    silent += 1
                    if silent >= ECU_SILENT_LIMIT:
                        break
                    continue
                silent = 0
                if not getattr(reply, "complete", True):
                    # A long reply needs the adapter to tell the module to carry on, which isn't automatic for every address.
                    reply = await elm.module_request(request_id, header, request, fc=True, keep_address=True) or reply
                if reply.startswith(b"\x62" + bytes.fromhex(request[2:])) and getattr(reply, "complete", True):
                    text = readable_date(reply[3:]) if request == "22F199" else None
                    text = text or readable_detail(reply[3:])
                    if text:
                        found.append({"label": label, "text": text})
    finally:
        if headers:
            await elm.restore_addressing()
    return details


async def read_fault_order(elm, records):
    """Add the first and latest fault to each module record that has faults, if the module can say."""
    try:
        for record in records:
            if not record["codes"]:
                continue
            for key, request in (("first", FIRST_FAULT_REQUEST), ("latest", LATEST_FAULT_REQUEST)):
                reply = await elm.module_request(record["hdr"], record["reply_id"], request, keep_address=True)
                record[key] = decode_one_record(reply, int(request[2:], 16))
            severity = decode_severity(await elm.module_request(record["hdr"], record["reply_id"], SEVERITY_REQUEST, keep_address=True))
            for item in record["codes"]:
                if item["code"] in severity:
                    item["details"] = [severity[item["code"]], *item.get("details", [])]
    finally:
        if any(record["codes"] for record in records):
            await elm.restore_addressing()


async def read_profile(elm, profile, year=None, on_progress=None):
    """Read each usable profile command. Returns (values, commands answered, commands tried)."""
    values = []
    answered = 0
    usable = [(command, usable_command(command, year)) for command in profile["commands"]]
    usable = [(command, sendable) for command, sendable in usable if sendable]
    silent = {}
    try:
        for index, (command, (hdr, rax, service, argument)) in enumerate(usable):
            if on_progress:
                on_progress(index + 1, len(usable))
            if silent.get(hdr, 0) >= MAX_SILENT:
                continue
            reply = await elm.module_request(hdr, rax, service + argument, fc=command.get("fcm1", False), wait=2)
            silent[hdr] = silent.get(hdr, 0) + 1 if reply is None else 0
            expected = bytes([int(service, 16) + 0x40]) + bytes.fromhex(argument)
            if reply is None or not reply.startswith(expected):
                continue
            answered += 1
            data = reply[len(expected):]
            for signal in command["signals"]:
                if not applies_to_year(signal.get("filter"), year):
                    continue
                value = decode_signal(signal["fmt"], data)
                if value is not None:
                    values.append({"module": hdr, "group": signal.get("path", "Other"),
                                   "name": signal["name"], "text": format_signal(signal["fmt"], value)})
    finally:
        await elm.restore_addressing()
    return values, answered, len(usable)


def format_profile(values, answered, total):
    lines = [t("Profile requests answered: {answered} of {total} usable", answered=answered, total=total), "",
             t("These decoding rules come from community data that may not match your car's exact year."),
             t("Treat any value that looks wrong as unreliable, not as a fault in the car."),
             t("Value names in this report come from the community data and are in English."), ""]
    for group in sorted({v["group"] for v in values}):
        lines.append(group.upper())
        for value in (v for v in values if v["group"] == group):
            module = t("module {header}", header=value["module"])
            lines.append(f"  {value['name']:68} {value['text']:>14}  [{module}]")
        lines.append("")
    if not values:
        lines.append(t("No values were decoded."))
    return "\n".join(lines)
