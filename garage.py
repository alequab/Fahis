"""Remember each car that connects, in one local JSON file per car. Files contain the VIN."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

CARS_DIR = Path(__file__).parent / "cars"
MAX_HISTORY = 200


def now():
    return datetime.now().isoformat(timespec="seconds")


def car_key(vehicle):
    """Identify a car by VIN, falling back to its calibration IDs if the VIN isn't reported."""
    vin = next((v for v in vehicle.get("VIN", {}).values() if v), None)
    fallback = "-".join(sorted(v for v in vehicle.get("Calibration ID", {}).values() if v))
    return re.sub(r"[^A-Za-z0-9_-]", "_", vin or fallback or "unknown-car")


def load_car(key):
    path = CARS_DIR / f"{key}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        path.replace(path.with_suffix(".damaged"))  # keep it for inspection and start fresh
        return {}


def save_car(key, record):
    CARS_DIR.mkdir(exist_ok=True)
    path = CARS_DIR / f"{key}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def remember_connection(vehicle, adapter, protocol):
    key = car_key(vehicle)
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
    record = load_car(key)
    record.setdefault("first_seen", now())
    record["last_seen"] = now()
    record.setdefault("vehicle", vehicle)
    record[section] = {"time": now(), **data}
    if section == "status":
        history = record.setdefault("history", [])
        history.append({"time": now(), "light": data.get("light"), "codes": data.get("active_codes", [])})
        del history[:-MAX_HISTORY]
    save_car(key, record)


def summaries():
    """Return a short description of every saved car, most recently seen first."""
    cars = []
    if not CARS_DIR.is_dir():
        return cars
    for path in CARS_DIR.glob("*.json"):
        record = load_car(path.stem)
        vehicle = record.get("vehicle", {})
        status = record.get("status", {})
        cars.append({
            "key": path.stem,
            "vin": next((v for v in vehicle.get("VIN", {}).values() if v), None),
            "modules": [v for v in vehicle.get("Module name", {}).values() if v],
            "first_seen": record.get("first_seen", ""),
            "last_seen": record.get("last_seen", ""),
            "connections": record.get("connections", 0),
            "light": status.get("light"),
            "codes": status.get("active_codes"),
        })
    return sorted(cars, key=lambda car: car["last_seen"], reverse=True)
