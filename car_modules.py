"""Read other modules: find which ones answer, read their codes, and read profile values."""
import json
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from dtc_codes import dtc_name
from i18n import t

PROFILE_DIR = Path(__file__).parent / "profiles"
DOWNLOAD_DIR = PROFILE_DIR / "downloaded"
CATALOG_FILE = PROFILE_DIR / "catalog.json"
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


def decode_uds_codes(payload):
    """Decode a 59 02 reply: mask byte, then 3-byte code and status byte for each fault.

    Modules can repeat a code, so each is listed once with its most serious state. A code with
    none of the active, confirmed or pending bits only failed at some point since the last clear.
    """
    worst = {}
    order = ("history", "pending", "stored", "active")
    body = payload[3:]
    for i in range(0, len(body) - 3, 4):
        status = body[i + 3]
        if body[i] or body[i + 1]:
            state = "active" if status & 0x01 else "stored" if status & 0x08 else "pending" if status & 0x04 else "history"
            code = f"{dtc_name(body[i], body[i + 1])}-{body[i + 2]:02X}"
            if order.index(state) >= order.index(worst.get(code, "history")):
                worst[code] = state
    return [{"code": code, "state": state} for code, state in worst.items()]


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
