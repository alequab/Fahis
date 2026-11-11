"""Look for patterns in a connection's readings that a single value on a page doesn't show.

Everything here is a hint from general rules of thumb, not the car maker's limits, and none of it is a diagnosis.
Readings arrive a few times a second at best, so fast events can be missed.
"""
import statistics
from collections import deque

# Roughly stoichiometric air to fuel ratio, and grams per litre, for gasoline.
GASOLINE_AIR_RATIO = 14.7
GASOLINE_GRAMS_PER_LITRE = 745

WARM_COOLANT = 70          # C, from which fuel trims and sensors are judged
DRIVING_KMH = 20
LONGEST_GAP_SECONDS = 5    # a longer gap between updates isn't counted as driving time
TRIM_WARN, TRIM_BAD = 10, 25                # percent of fuel added or removed
ROUGH_IDLE_RPM = 80                         # spread of idle speed
IDLE_BELOW_RPM = 1300                       # faster than this with the car stopped is someone pressing the pedal
PEDAL_REST_MARGIN = 3                       # percent above the lowest pedal reading that still counts as released
ENGINE_SETTLED_SECONDS = 5                  # after stopping, before intake pressure is compared with the outside air
PLAUSIBLE_FUEL_USE = (2, 60)                # L/100km; outside this the estimate is wrong, so it isn't shown
HARD_ACCELERATION, HARD_BRAKING = 10, -12   # km/h gained or lost each second
COLD_AFTER_SECONDS = 900                    # an engine still cold after this long may have a thermostat stuck open
EVAP_CODES = {"P0440", "P0441", "P0442", "P0455", "P0456", "P0457"}
REFUELLED_BY = 10                           # percent of the tank
SAMPLES_NEEDED = 12
ATTENTION = ("bad", "warn")


def t(text):
    """Marks text for translation. The page translates it, so this module stays free of language."""
    return text


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def litres_per_hour(values):
    """Estimate fuel flow. The air flow method only suits gasoline, as diesels run with spare air."""
    flow = number(values.get(0x5E))
    if flow is None and values.get(0x51) == "Gasoline" and number(values.get(0x10)) is not None:
        flow = values[0x10] * 3600 / (GASOLINE_AIR_RATIO * GASOLINE_GRAMS_PER_LITRE)
    return flow


class Tally:
    """Count, average, lowest and highest of a run of numbers."""

    def __init__(self):
        self.count, self.total, self.squares, self.low, self.high = 0, 0.0, 0.0, None, None

    def add(self, value):
        self.count += 1
        self.total += value
        self.squares += value * value
        self.low = value if self.low is None else min(self.low, value)
        self.high = value if self.high is None else max(self.high, value)

    @property
    def average(self):
        return self.total / self.count if self.count else None

    @property
    def spread(self):
        return self.high - self.low if self.count else None

    @property
    def wobble(self):
        """How much the values vary around their average (standard deviation)."""
        return max(self.squares / self.count - self.average ** 2, 0) ** 0.5 if self.count else None


class DriveStats:
    """Collects what one connection's readings show over time."""

    def __init__(self):
        self.last_update = None
        self.seen_at = {}
        self.ranges = {}
        self.seconds = {"total": 0.0, "running": 0.0, "idle": 0.0, "moving": 0.0}
        self.distance_km = self.fuel_litres = 0.0
        self.hard_accelerations = self.hard_brakings = 0
        self.speed_before = None  # (time, km/h)
        self.pushing = 0          # 1 while a hard acceleration is under way, -1 for braking
        self.trim_idle, self.trim_driving = Tally(), Tally()
        self.idle_rpm = deque(maxlen=600)
        self.front_o2, self.rear_o2 = Tally(), Tally()
        self.running_volts = Tally()
        self.engine_off_gap = Tally()  # intake pressure minus barometric pressure with the engine stopped
        self.fuel_at_start = None
        self.pedal_rest = None      # the lowest pedal reading seen, which is the pedal released
        self.stopped_since = None   # when the engine was first seen stopped

    def update(self, values, read_at, now):
        """Take in the latest values. `read_at` says when each was read, so a held value isn't counted twice."""
        get = lambda pid: number(values.get(pid))
        rpm, speed, coolant = get(0x0C), get(0x0D), get(0x05)
        running = rpm is not None and rpm > 0
        warm = coolant is not None and coolant >= WARM_COOLANT
        closed = str(values.get(0x03, "")).startswith("Closed loop")
        if self.fuel_at_start is None:
            self.fuel_at_start = get(0x2F)
        pedal = get(0x49) if get(0x49) is not None else get(0x11)
        if pedal is not None:
            self.pedal_rest = pedal if self.pedal_rest is None else min(self.pedal_rest, pedal)
        released = pedal is None or pedal <= self.pedal_rest + PEDAL_REST_MARGIN
        if rpm == 0:
            self.stopped_since = now if self.stopped_since is None else self.stopped_since
        else:
            self.stopped_since = None

        if self.last_update is not None:
            gap = min(now - self.last_update, LONGEST_GAP_SECONDS)
            self.seconds["total"] += gap
            if running:
                self.seconds["running"] += gap
                self.seconds["moving" if speed else "idle"] += gap
                if speed:
                    self.distance_km += speed * gap / 3600
                flow = litres_per_hour(values)
                if flow is not None:
                    self.fuel_litres += flow * gap / 3600
        self.last_update = now

        fresh = {pid for pid in values if read_at.get(pid) != self.seen_at.get(pid) and get(pid) is not None}
        self.seen_at.update({pid: read_at.get(pid) for pid in fresh})
        for pid in fresh:
            self.ranges.setdefault(pid, Tally()).add(values[pid])

        if 0x0D in fresh:
            if self.speed_before is not None and 0.2 <= now - self.speed_before[0] <= 3:
                change = (speed - self.speed_before[1]) / (now - self.speed_before[0])
                push = 1 if change >= HARD_ACCELERATION else -1 if change <= HARD_BRAKING else 0
                if push and push != self.pushing:
                    self.hard_accelerations += push == 1
                    self.hard_brakings += push == -1
                self.pushing = push
            self.speed_before = (now, speed)
        if running and warm and closed and {0x06, 0x07} & fresh and get(0x06) is not None and get(0x07) is not None:
            total = values[0x06] + values[0x07]
            if not speed:
                self.trim_idle.add(total)
            elif speed >= DRIVING_KMH:
                self.trim_driving.add(total)
        if running and warm and not speed and released and rpm < IDLE_BELOW_RPM and 0x0C in fresh:
            self.idle_rpm.append(rpm)
        if running and warm and closed:
            if 0x14 in fresh:
                self.front_o2.add(values[0x14])
            if 0x15 in fresh:
                self.rear_o2.add(values[0x15])
        if running and 0x42 in fresh:
            self.running_volts.add(values[0x42])
        settled = self.stopped_since is not None and now - self.stopped_since >= ENGINE_SETTLED_SECONDS
        if settled and 0x0B in fresh and (read_at.get(0x0B) or now) > self.stopped_since and get(0x33) is not None:
            self.engine_off_gap.add(values[0x0B] - values[0x33])

    def averages(self):
        """The few figures worth comparing between visits, rounded. None where there wasn't enough to go on."""
        enough = lambda tally: round(tally.average, 1) if tally.count >= SAMPLES_NEEDED else None
        return {"trim_idle": enough(self.trim_idle), "trim_driving": enough(self.trim_driving),
                "idle_rpm": round(statistics.fmean(self.idle_rpm)) if len(self.idle_rpm) >= SAMPLES_NEEDED else None,
                "volts_high": round(self.running_volts.high, 1) if self.running_volts.count >= SAMPLES_NEEDED else None}


def finding(key, title, state, text, system, **values):
    return {"id": key, "title": title, "state": state, "text": text, "system": system, "values": values}


def check_mixture(stats):
    idle = stats.trim_idle.average if stats.trim_idle.count >= SAMPLES_NEEDED else None
    driving = stats.trim_driving.average if stats.trim_driving.count >= SAMPLES_NEEDED else None
    known = [value for value in (idle, driving) if value is not None]
    title = t("Fuel mixture")
    if not known:
        return finding("mixture", title, "unknown", t("Needs a warm engine running for a minute or two."), "fuel_air")
    worst = max(known, key=abs)
    state = "bad" if abs(worst) >= TRIM_BAD else "warn" if abs(worst) >= TRIM_WARN else "good"
    shown = lambda value: "-" if value is None else f"{value:+.1f}"
    if state == "good" and len(known) == 1:
        return finding("mixture", title, state, t("The engine adjusts its fuel by {value}%, inside the usual 10% either way."), "fuel_air", value=shown(worst))
    if state == "good":
        return finding("mixture", title, state, t("The engine adjusts its fuel by {idle}% at idle and {driving}% while driving, inside the usual 10% either way."),
                       "fuel_air", idle=shown(idle), driving=shown(driving))
    if worst < 0:
        text = t("The engine is removing {value}% of its fuel to keep the mixture right. A leaking injector, high fuel pressure or a blocked air filter can cause this.")
    elif idle is not None and driving is not None and idle >= TRIM_WARN > driving:
        text = t("The engine is adding {value}% fuel at idle but not while driving. That pattern often comes from an air leak after the air flow sensor.")
    else:
        text = t("The engine is adding {value}% fuel to keep the mixture right. Low fuel pressure, a dirty air flow sensor or an air leak can cause this.")
    return finding("mixture", title, state, text, "fuel_air", value=f"{abs(worst):.0f}")


def check_idle(stats):
    title = t("Idle steadiness")
    if len(stats.idle_rpm) < SAMPLES_NEEDED * 2:
        return finding("idle", title, "unknown", t("Needs a warm engine idling with the car stopped for about half a minute."), "engine")
    spread, middle = statistics.pstdev(stats.idle_rpm), statistics.fmean(stats.idle_rpm)
    if spread >= ROUGH_IDLE_RPM:
        return finding("idle", title, "warn", t("Idle speed wanders by about {spread} rpm around {rpm} rpm. Misfires, an air leak or a dirty throttle body can cause a rough idle, and so can the air conditioning switching on and off."),
                       "engine", spread=round(spread), rpm=round(middle))
    return finding("idle", title, "good", t("Idle is steady at about {rpm} rpm."), "engine", rpm=round(middle))


def check_temperature(stats, values):
    coolant = stats.ranges.get(0x05)
    title = t("Engine temperature")
    if coolant is None:
        return None
    run_time = number(values.get(0x1F)) or stats.seconds["running"]
    if coolant.high >= 115:
        return finding("temperature", title, "bad", t("Coolant reached {value} C, which is too hot. If the car's own gauge agrees, stop when it's safe and let the engine cool."),
                       "engine", value=round(coolant.high))
    if coolant.high >= 105:
        return finding("temperature", title, "warn", t("Coolant reached {value} C, which is on the high side. Check the coolant level with the engine cold, and that the fan runs."),
                       "engine", value=round(coolant.high))
    if coolant.high < WARM_COOLANT:
        if run_time >= COLD_AFTER_SECONDS and stats.seconds["moving"] >= 300:
            return finding("temperature", title, "warn", t("After {minutes} minutes the coolant has only reached {value} C. A thermostat stuck open is the usual cause, and it costs fuel."),
                           "engine", minutes=round(run_time / 60), value=round(coolant.high))
        return finding("temperature", title, "unknown", t("The engine is still warming up."), "engine")
    return finding("temperature", title, "good", t("The engine reached its normal temperature and stayed between {low} and {high} C once warm."),
                   "engine", low=round(max(coolant.low, WARM_COOLANT)), high=round(coolant.high))


def check_front_sensor(stats):
    title = t("Front oxygen sensor")
    if 0x14 not in stats.ranges:
        return None
    sensor = stats.front_o2
    if sensor.count < SAMPLES_NEEDED:
        return finding("front_o2", title, "unknown", t("Needs a warm engine running for a few minutes."), "exhaust")
    if sensor.spread < 0.2:
        return finding("front_o2", title, "warn", t("The sensor stayed between {low} and {high} V, where a healthy one keeps swinging. It may be slow or stuck."),
                       "exhaust", low=f"{sensor.low:.2f}", high=f"{sensor.high:.2f}")
    return finding("front_o2", title, "good", t("The sensor swings between {low} and {high} V, as it should."), "exhaust", low=f"{sensor.low:.2f}", high=f"{sensor.high:.2f}")


def check_rear_sensor(stats):
    """Only a reading outside the sensor's range is judged. Comparing it with the front sensor to judge the catalyst
    was tried, and at these reading speeds it couldn't tell a real car's passing catalyst from a worn one."""
    title = t("Rear oxygen sensor")
    if 0x15 not in stats.ranges:
        return None
    rear = stats.rear_o2
    if rear.count < SAMPLES_NEEDED * 3:
        return finding("rear_o2", title, "unknown", t("Needs a warm engine running for a few minutes."), "exhaust")
    if rear.average > 1.05:
        return finding("rear_o2", title, "warn", t("The rear sensor averages {value} V, above the 0 to 1 V a normal sensor gives. The sensor or its wiring is worth checking, and its self-test result is on the Fault codes page."),
                       "exhaust", value=f"{rear.average:.2f}")
    return finding("rear_o2", title, "good", t("The rear sensor reads between {low} and {high} V, inside a normal sensor's range. The car's own self-test judges the catalyst."),
                   "exhaust", low=f"{rear.low:.2f}", high=f"{rear.high:.2f}")


def check_charging(stats):
    volts = stats.running_volts
    title = t("Charging")
    if 0x42 not in stats.ranges:
        return None
    if volts.count < SAMPLES_NEEDED:
        return finding("charging", title, "unknown", t("Needs the engine running for a minute."), "electrical")
    if volts.high > 15.5:
        return finding("charging", title, "bad", t("Voltage reached {value} V with the engine running, which is too high and can damage the battery."), "electrical", value=f"{volts.high:.1f}")
    if volts.high < 13.0 and stats.seconds["running"] >= 600:
        return finding("charging", title, "warn", t("In {minutes} minutes of running, voltage never rose above {value} V. The alternator may not be charging."),
                       "electrical", minutes=round(stats.seconds["running"] / 60), value=f"{volts.high:.1f}")
    return finding("charging", title, "good", t("Voltage ranged from {low} to {high} V with the engine running. Many cars rest between charging spells to save fuel."),
                   "electrical", low=f"{volts.low:.1f}", high=f"{volts.high:.1f}")


def check_pressure_sensor(stats):
    gap = stats.engine_off_gap
    if not gap.count:
        return None
    title = t("Intake pressure sensor")
    if abs(gap.average) > 8:
        return finding("map", title, "warn", t("With the engine stopped, intake pressure should match the outside air, but it differs by {value} kPa. One of the two sensors may be off."),
                       "fuel_air", value=round(abs(gap.average)))
    return finding("map", title, "good", t("With the engine stopped, intake pressure matches the outside air, as it should."), "fuel_air")


def check_pending(snapshot):
    codes = snapshot["codes"]
    stored = {code for found in codes.get("Stored", {}).values() for code in found}
    pending = sorted({code for found in codes.get("Pending", {}).values() for code in found} - stored)
    if not pending:
        return None
    return finding("pending", t("Faults seen once"), "warn", t("{codes}: the car has seen this once. If it sees it again on another drive, it turns the warning light on. If it doesn't, the car drops it by itself."),
                   "status", codes=", ".join(pending))


def check_fuel_cap(stats, values, snapshot, previous):
    codes = {code for label in ("Stored", "Pending") for found in snapshot["codes"].get(label, {}).values() for code in found}
    leak = sorted(codes & EVAP_CODES)
    if not leak:
        return None
    now = number(values.get(0x2F)) if stats.fuel_at_start is None else stats.fuel_at_start
    earlier = [level for level in previous.get("fuel", []) if number(level) is not None]
    title = t("Fuel cap")
    if now is not None and earlier and now - min(earlier) >= REFUELLED_BY:
        return finding("fuel_cap", title, "warn", t("{codes} appeared and fuel was added recently ({before}% to {now}%). A fuel cap left loose after filling up is the commonest cause. Tighten it until it clicks. The code can take a few drives to go."),
                       "exhaust", codes=", ".join(leak), before=round(min(earlier)), now=round(now))
    return finding("fuel_cap", title, "warn", t("{codes} means fuel vapour is escaping. Check first that the fuel cap is tight and its seal isn't cracked, as that's the commonest cause."),
                   "exhaust", codes=", ".join(leak))


def near_limit(result):
    """Whether a passing self-test sits within a tenth of a limit that is really set."""
    value, low, high = result["value"], result["low"], result["high"]
    has_low, has_high = not result.get("open_low", low == 0), not result.get("open_high", False)
    if has_low and has_high and high > low:
        return min(value - low, high - value) < 0.1 * (high - low)
    if has_high and high > 0:
        return value > 0.9 * high
    if has_low and low > 0:
        return value < 1.1 * low
    return False


def check_self_tests(snapshot, monitor_name):
    results = [result for result in snapshot.get("test_results") or [] if result.get("value") is not None]
    if not results:
        return []
    named = lambda found: ", ".join(dict.fromkeys(monitor_name(result["monitor"]) for result in found[:6]))
    failed = [result for result in results if result["state"] == "fail"]
    close = [result for result in results if result["state"] == "pass" and near_limit(result)]
    title = t("Self-test margins")
    if failed:
        found = [finding("margins", title, "bad", t("Outside the maker's limits: {names}. See Detailed self-test results on the Fault codes page."), "status", names=named(failed))]
    elif close:
        found = [finding("margins", title, "warn", t("Still passing, but within a tenth of a limit: {names}. These can be the first to fail."), "status", names=named(close))]
    else:
        passed = sum(result["state"] == "pass" for result in results)
        found = [finding("margins", title, "good", t("All {count} self-tests that have run are well inside the maker's limits."), "status", count=passed)]
    misfires = [result for result in results if 0xA2 <= result["monitor"] <= 0xAD and result["state"] == "pass" and result["value"] > 0]
    if misfires:
        found.append(finding("misfires", t("Misfire counts"), "warn", t("The car has counted misfires, though still within its limit: {names}. Spark plugs and coils are the usual suspects."),
                             "engine", names=named(misfires)))
    return found


def check_trend(stats, previous):
    earlier = [visit.get("averages") for visit in previous.get("sessions", []) if isinstance(visit.get("averages"), dict)]
    title = t("Compared with earlier visits")
    now = stats.averages()
    moved = []
    for key, limit, label in (("trim_idle", 8, t("fuel adjustment at idle")), ("trim_driving", 8, t("fuel adjustment while driving")),
                              ("idle_rpm", 150, t("idle speed"))):
        before = [visit[key] for visit in earlier if number(visit.get(key)) is not None]
        if now[key] is not None and len(before) >= 2 and abs(now[key] - statistics.median(before)) >= limit:
            moved.append((label, statistics.median(before), now[key]))
    if moved:
        label, before, after = moved[0]
        return finding("trend", title, "warn", t("The {what} has moved from {before} to {now} since earlier visits. A change like this is worth watching even with no fault code."),
                       "status", what=label, before=f"{before:g}", now=f"{after:g}")
    comparable = sum(any(number(visit.get(key)) is not None for key in ("trim_idle", "trim_driving", "idle_rpm")) for visit in earlier)
    if comparable < 2 or all(now[key] is None for key in ("trim_idle", "trim_driving", "idle_rpm")):
        return finding("trend", title, "unknown", t("Fahis compares each visit with earlier ones, and needs a few visits of a few minutes each to start."), "status")
    return finding("trend", title, "good", t("Fuel adjustment and idle speed are close to your last {count} visits."), "status", count=comparable)


def findings(stats, values, snapshot, previous, monitor_name):
    """Every check that applies to this car, with the ones needing attention first."""
    found = [check_fuel_cap(stats, values, snapshot, previous), check_pending(snapshot), check_mixture(stats), check_idle(stats),
             check_temperature(stats, values), check_front_sensor(stats), check_rear_sensor(stats), check_charging(stats),
             check_pressure_sensor(stats), *check_self_tests(snapshot, monitor_name), check_trend(stats, previous)]
    order = {"bad": 0, "warn": 1, "good": 2, "unknown": 3}
    return sorted((item for item in found if item), key=lambda item: order[item["state"]])


def drive_summary(stats):
    """Figures for this connection as (label, number or None, unit). Distance and fuel are estimates."""
    moving_hours = stats.seconds["moving"] / 3600
    use = stats.fuel_litres / stats.distance_km * 100 if stats.distance_km >= 0.5 and stats.fuel_litres else None
    low, high = PLAUSIBLE_FUEL_USE
    believable = use is None or low <= use <= high or stats.distance_km < 3  # short trips are mostly idling
    use = use if use is not None and low <= use <= high else None
    speed, rpm = stats.ranges.get(0x0D), stats.ranges.get(0x0C)
    return [
        (t("Engine running"), stats.seconds["running"] / 60, "min"),
        (t("Stopped with engine running"), stats.seconds["idle"] / 60, "min"),
        (t("Distance (estimate)"), stats.distance_km, "km"),
        (t("Average speed while moving"), stats.distance_km / moving_hours if moving_hours > 0.01 else None, "km/h"),
        (t("Top speed"), speed.high if speed else None, "km/h"),
        (t("Highest engine RPM"), rpm.high if rpm else None, "rpm"),
        (t("Fuel used (estimate)"), stats.fuel_litres if stats.fuel_litres and believable else None, "L"),
        (t("Average fuel use (estimate)"), use, "L/100km"),
        (t("Hard accelerations"), stats.hard_accelerations, ""),
        (t("Hard brakings"), stats.hard_brakings, ""),
    ]
