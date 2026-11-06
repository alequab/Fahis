"""Sort what the car reports into systems such as engine and transmission, so people can find things by what they are."""
from i18n import t

# id, name, what it covers. Shown on the page in this order.
SYSTEMS = (
    ("engine", "Engine", "Speed, load, temperatures, ignition and misfires"),
    ("transmission", "Transmission", "Gearbox module, gears and gearbox faults"),
    ("fuel_air", "Fuel and air", "Fuel level and pressure, fuel trims, throttle and air flow"),
    ("exhaust", "Exhaust and emissions", "Oxygen sensors, catalyst, EGR and fuel vapour"),
    ("electrical", "Battery and charging", "Voltage, charging and hybrid battery"),
    ("driving", "Driving", "Vehicle speed and accelerator pedal"),
    ("brakes", "Brakes and chassis", "ABS, stability control, steering and suspension"),
    ("body", "Body and comfort", "Airbags, doors, lights and climate"),
    ("network", "Module network", "How the car's modules talk to each other"),
    ("status", "Car status", "OBD standard, distance and time since codes were cleared"),
)
SYSTEM_IDS = tuple(system[0] for system in SYSTEMS)
# Standard OBD says little about these, so the page points to where more can be found.
NEEDS_PROFILE = {"transmission", "brakes", "body"}

_VALUE_GROUPS = {
    "engine": (0x04, 0x05, 0x0C, 0x0E, 0x1F, 0x43, 0x5C, 0x61, 0x62, 0x63),
    "fuel_air": (0x03, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x22, 0x0F, 0x10, 0x11, 0x23, 0x2F, 0x33, 0x44, 0x45, 0x46, 0x47, 0x48,
                 0x4C, 0x51, 0x52, 0x59, 0x5D, 0x5E),
    "exhaust": (0x13, *range(0x14, 0x1C), *range(0x24, 0x2C), *range(0x34, 0x3C), 0x2C, 0x2D, 0x2E, 0x32, 0x3C, 0x3D, 0x3E, 0x3F, 0x55, 0x56, 0x57, 0x58),
    "electrical": (0x42, 0x5B),
    "driving": (0x0D, 0x49, 0x4A, 0x4B, 0x5A),
    "status": (0x1C, 0x21, 0x30, 0x31, 0x4D, 0x4E),
}
VALUE_SYSTEMS = {pid: system for system, pids in _VALUE_GROUPS.items() for pid in pids}

# Community value definitions, by the first part of their path.
_PATH_SYSTEMS = {"Battery": "electrical", "Climate": "body", "Clocks": "engine", "Control": "fuel_air", "DTCs": "status",
                 "Emissions": "exhaust", "Engine": "engine", "Fuel": "fuel_air", "Movement": "driving", "OBD": "status",
                 "Trips": "status"}

# Fault codes by their second and third characters, from the SAE ranges. Maker-specific P1 and P3 codes aren't
# covered by these ranges, so they stay under Engine rather than guessing.
_CODE_SYSTEMS = {
    "P00": "fuel_air", "P01": "fuel_air", "P02": "fuel_air", "P03": "engine", "P04": "exhaust", "P05": "engine",
    "P06": "engine", "P07": "transmission", "P08": "transmission", "P09": "transmission", "P0A": "electrical",
    "P0B": "electrical", "P0C": "electrical", "P20": "exhaust", "P21": "fuel_air", "P22": "fuel_air", "P23": "engine",
    "P24": "exhaust", "P25": "engine", "P26": "engine", "P27": "transmission", "P28": "transmission", "P29": "fuel_air",
}
# Low voltage and charging codes sit inside the ranges above but belong with the battery.
_ELECTRICAL_CODES = {"P0560", "P0561", "P0562", "P0563", "P0620", "P0621", "P0622", "P0623", "P0624", "P0625"}
_LETTER_SYSTEMS = {"C": "brakes", "B": "body", "U": "network"}

_MONITOR_SYSTEMS = {"Misfire": "engine", "Fuel system": "fuel_air", "Boost pressure": "fuel_air", "Components": "status",
                    "A/C refrigerant": "body"}


def value_system(pid):
    return VALUE_SYSTEMS.get(pid, "status")


def more_system(path, name):
    """Choose a system for a community-defined value."""
    lowered = name.lower()
    if "transmission" in lowered or "gear" in lowered:
        return "transmission"
    if path.startswith("Control.Pedals"):
        return "driving"
    return _PATH_SYSTEMS.get(path.split(".")[0], "status")


def code_system(code):
    letter = code[:1].upper()
    if letter in _LETTER_SYSTEMS:
        return _LETTER_SYSTEMS[letter]
    if code[:5].upper() in _ELECTRICAL_CODES:
        return "electrical"
    return _CODE_SYSTEMS.get(code[:3].upper(), "engine")


def readiness_system(name):
    """Choose a system for an emissions self-test, by its English name."""
    return _MONITOR_SYSTEMS.get(name, "exhaust")


def test_group_system(monitor):
    """Choose a system for a detailed self-test, by its standard monitor number."""
    if 0x35 <= monitor <= 0x38 or 0xA1 <= monitor <= 0xAD:
        return "engine"  # valve timing and misfire
    if 0x81 <= monitor <= 0x88:
        return "fuel_air"  # fuel system and boost pressure
    known = (0x01 <= monitor <= 0x10, 0x21 <= monitor <= 0x24, 0x31 <= monitor <= 0x34, 0x39 <= monitor <= 0x3D,
             0x41 <= monitor <= 0x50, 0x61 <= monitor <= 0x64, 0x71 <= monitor <= 0x74, 0x90 <= monitor <= 0x99,
             0xB2 <= monitor <= 0xB3)
    return "exhaust" if any(known) else "status"


def empty_systems(lang=None):
    """Return every system with nothing in it yet, keyed by id in page order."""
    return {system_id: {"id": system_id, "name": t(name, lang), "what": t(what, lang), "needs_profile": system_id in NEEDS_PROFILE,
                        "values": [], "faults": [], "checks": [], "results": []}
            for system_id, name, what in SYSTEMS}
