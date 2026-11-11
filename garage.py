"""Remember each car that connects, in one local JSON file per car. Files contain the VIN."""
import json
import os
import re
from datetime import datetime

from dtc_codes import make_from_vin
from paths import DATA_HOME

CARS_DIR = DATA_HOME / "cars"
MAX_HISTORY = 200
MAX_SESSIONS = 100
MAX_TESTS = 100
MAX_TEST_OUTPUT = 20_000
MAX_NAME = 40
MAX_TEST_HISTORY = 40
TEST_HISTORY_GAP_SECONDS = 3600  # readings closer together than this are the same visit
MAX_REFUELS = 60
REFUELLED_BY = 10  # percent of the tank
UNKNOWN_CAR = "unknown-car"
# VIN position 10 gives the model year. It repeats every 30 years, so this reads it as the most recent cycle.
YEAR_LETTERS = "ABCDEFGHJKLMNPRSTVWXY"


def now():
    return datetime.now().isoformat(timespec="seconds")


def car_key(vehicle):
    """Identify a car by VIN, falling back to its calibration IDs if the VIN isn't reported."""
    vin = next((v for v in vehicle.get("VIN", {}).values() if v), None)
    fallback = "-".join(sorted(v for v in vehicle.get("Calibration ID", {}).values() if v))
    return re.sub(r"[^A-Za-z0-9_-]", "_", vin or fallback or UNKNOWN_CAR)


def load_car(key):
    path = CARS_DIR / f"{key}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError
        return record
    except FileNotFoundError:
        return {}
    except ValueError:
        path.replace(path.with_suffix(".damaged"))  # keep it for inspection and start fresh
        return {}


def delete_car(key):
    """Remove a saved car's record. Returns whether there was one."""
    path = CARS_DIR / f"{key}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


def save_car(key, record):
    CARS_DIR.mkdir(parents=True, exist_ok=True)
    path = CARS_DIR / f"{key}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def remember_connection(vehicle, adapter, protocol):
    key = car_key(vehicle)
    if key == UNKNOWN_CAR:
        return {}  # Nothing identifies this car, so a record couldn't be matched to it later.
    record = load_car(key)
    record.setdefault("first_seen", now())
    record["last_seen"] = now()
    record["connections"] = record.get("connections", 0) + 1
    record["vehicle"] = vehicle
    record["adapter"] = adapter
    record["protocol"] = protocol
    save_car(key, record)
    return record


def remember(vehicle, section, data):
    """Save the latest result of one kind, such as `status` or `modules`, with its time."""
    if vehicle is None:
        return
    key = car_key(vehicle)
    if key == UNKNOWN_CAR:
        return
    record = load_car(key)
    record.setdefault("first_seen", now())
    record["last_seen"] = now()
    record.setdefault("vehicle", vehicle)
    record[section] = {"time": now(), **data}
    if section == "status":
        codes = data.get("codes", {})
        listed = lambda label: sorted({code for found in codes.get(label, {}).values() for code in found})
        history = record.setdefault("history", [])
        history.append({"time": now(), "light": data.get("light"), "codes": data.get("active_codes", []),
                        "stored": listed("Stored"), "pending": listed("Pending"), "permanent": listed("Permanent"),
                        # Which computers answered, and which one reported each code. A code missing from a reading only
                        # counts as gone if its computer answered that time.
                        "modules": sorted(codes.get("Stored", {})),
                        "where": {code: header for found in codes.values() for header, module_codes in found.items() for code in module_codes}})
        del history[:-MAX_HISTORY]
        remember_tests(record, data.get("test_results", []))
    save_car(key, record)


def remember_tests(record, results):
    """Keep each visit's self-test values, so a value sliding toward its limit can be seen over time."""
    values = {f"{r['module']}:{r['monitor']:02X}:{r['test']:02X}": r["value"] for r in results
              if r.get("state") in ("pass", "fail") and isinstance(r.get("value"), (int, float))}
    if not values:
        return
    log = record.setdefault("test_history", [])
    entry = {"time": now(), "values": values}
    try:
        same_visit = log and (datetime.fromisoformat(entry["time"]) - datetime.fromisoformat(log[-1]["time"])).total_seconds() < TEST_HISTORY_GAP_SECONDS
    except (ValueError, KeyError, TypeError):
        same_visit = False
    if same_visit:
        log[-1] = entry
    else:
        log.append(entry)
    del log[:-MAX_TEST_HISTORY]


def fault_followup(record):
    """Follow each fault code across the saved readings: when it first showed, what kind it is, and whether it has gone.

    A code counts as gone only after a later reading in which the computer that reported it answered without it.
    Readings saved before that was recorded can't show this, so such a code is left as unclear.
    """
    readings = [entry for entry in record.get("history", []) if isinstance(entry, dict) and entry.get("time")]
    cases = {}
    for index, entry in enumerate(readings):
        stored, pending, permanent = (set(entry.get(kind, [])) for kind in ("stored", "pending", "permanent"))
        for code in set(entry.get("codes", [])) | stored | pending | permanent:
            kind = "confirmed" if code in stored else "pending" if code in pending else "permanent" if code in permanent else "seen"
            case = cases.setdefault(code, {"code": code, "first_seen": entry["time"], "confirmed": False, "module": None})
            case.update(last_seen=entry["time"], last_index=index, kind=kind, module=entry.get("where", {}).get(code, case["module"]))
            case["confirmed"] = case["confirmed"] or kind == "confirmed"
    for case in cases.values():
        later = readings[case.pop("last_index") + 1:]
        module, kind = case.pop("module"), case.pop("kind")
        case["clean_readings"] = sum(1 for entry in later if module and module in entry.get("modules", []))
        case["status"] = kind if not later else "gone" if case["clean_readings"] else "unclear"
    return sorted(cases.values(), key=lambda case: (case["status"] in ("gone", "unclear"), case["code"]))


def set_tank(key, litres):
    """Keep the size of a saved car's fuel tank, so fill-ups can be shown in litres. Nothing removes it but 0."""
    record = load_car(key)
    if not record:
        return False
    if litres:
        record["tank_litres"] = litres
    else:
        record.pop("tank_litres", None)
    save_car(key, record)
    return True


def save_session(vehicle, session):
    """Save or update one connection to a car, so a lost connection still leaves its record."""
    key = car_key(vehicle)
    if key == UNKNOWN_CAR:
        return
    record = load_car(key)
    record.setdefault("first_seen", now())
    record["last_seen"] = now()
    record.setdefault("vehicle", vehicle)
    sessions = [item for item in record.get("sessions", []) if item.get("id") != session["id"]]
    if len(sessions) == len(record.get("sessions", [])) and sessions:
        # A new connection. A fuel level well above where the last one ended means the car was filled up in between.
        last = sessions[-1]
        before = last.get("fuel_end") if last.get("fuel_end") is not None else last.get("fuel")
        after = session.get("fuel")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and after - before >= REFUELLED_BY:
            refuels = record.setdefault("refuels", [])
            refuels.append({"time": session.get("started", now()), "before": round(before, 1), "after": round(after, 1)})
            del refuels[:-MAX_REFUELS]
    sessions.append(session)
    record["sessions"] = sessions[-MAX_SESSIONS:]
    save_car(key, record)


def add_test(vehicle, action, status, output):
    """Keep the result of a command run on this car."""
    key = car_key(vehicle)
    if key == UNKNOWN_CAR:
        return
    record = load_car(key)
    record.setdefault("first_seen", now())
    record["last_seen"] = now()
    record.setdefault("vehicle", vehicle)
    tests = record.setdefault("tests", [])
    tests.append({"time": now(), "action": action, "status": status, "output": output[-MAX_TEST_OUTPUT:]})
    del tests[:-MAX_TESTS]
    save_car(key, record)


def set_name(key, name):
    """Give a saved car a name of the user's choosing. An empty name removes it."""
    record = load_car(key)
    if not record:
        return False
    name = " ".join(str(name).split())[:MAX_NAME]
    if name:
        record["name"] = name
    else:
        record.pop("name", None)
    save_car(key, record)
    return True


def model_year(vin):
    """Read the model year from a VIN, or None if it can't be read."""
    if not vin or len(vin) != 17:
        return None
    mark = vin[9].upper()
    if mark in YEAR_LETTERS:
        return 2010 + YEAR_LETTERS.index(mark)
    return 2000 + int(mark) if mark in "123456789" else None


def fault_pattern(code, readings, present):
    """Tell a fault that comes and goes from one that stays, using the readings saved for this car, oldest first."""
    seen = [code in reading for reading in readings]
    first = seen.index(True) if True in seen else None
    if first is not None and not all(seen[first:len(seen) - seen[::-1].index(True)]):
        return "returns"
    if not present:
        return "gone"
    return "stays" if sum(seen) >= 2 else "new"


def fault_history(record):
    """Summarise each fault code this car has had, with the ones still present first."""
    faults = {}
    for entry in record.get("history", []):
        for code in entry.get("codes", []):
            fault = faults.setdefault(code, {"code": code, "first_seen": entry["time"], "last_seen": entry["time"], "times": 0})
            fault["last_seen"] = entry["time"]
            fault["times"] += 1
    present = set(record.get("status", {}).get("active_codes", []))
    for code in present:
        faults.setdefault(code, {"code": code, "first_seen": record["status"].get("time", ""),
                                 "last_seen": record["status"].get("time", ""), "times": 1})
    readings = [set(entry.get("codes", [])) for entry in record.get("history", [])]
    for fault in faults.values():
        fault["present"] = fault["code"] in present
        fault["pattern"] = fault_pattern(fault["code"], readings, fault["present"])
    return sorted(faults.values(), key=lambda f: (not f["present"], f["code"]))


def timeline(record):
    """Put connections, command results and earlier readings together, newest first."""
    items = [{"kind": "connection", "time": item["started"], **item} for item in record.get("sessions", [])]
    items += [{"kind": "command", **item} for item in record.get("tests", [])]
    items += [{"kind": "reading", **item} for item in record.get("history", [])]
    return sorted(items, key=lambda item: item["time"], reverse=True)


def summaries():
    """Return a short description of every saved car, most recently seen first."""
    cars = []
    if not CARS_DIR.is_dir():
        return cars
    for path in CARS_DIR.glob("*.json"):
        record = load_car(path.stem)
        vehicle = record.get("vehicle", {})
        status = record.get("status", {})
        vin = next((v for v in vehicle.get("VIN", {}).values() if v), None)
        cars.append({
            "key": path.stem,
            "vin": vin,
            "name": record.get("name"),
            "make": make_from_vin(vin),
            "year": model_year(vin),
            "past": sum(len(record.get(kind, [])) for kind in ("tests", "sessions", "history")),
            "modules": [v for v in vehicle.get("Module name", {}).values() if v],
            "first_seen": record.get("first_seen", ""),
            "last_seen": record.get("last_seen", ""),
            "connections": record.get("connections", 0),
            "light": status.get("light"),
            "codes": status.get("active_codes"),
        })
    return sorted(cars, key=lambda car: car["last_seen"], reverse=True)
