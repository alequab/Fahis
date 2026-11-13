"""Map what a car's computers will share, for learning how they're organised. Read-only throughout.

A car computer has no folders. It has an address, and inside it numbered data items that can be asked for one at a
time. This finds which addresses answer, then which data items each one gives, using only read requests (services
19, 21 and 22) in the normal session. It never switches session, unlocks anything or writes, so nothing can change.
"""
import json
import time

import car_modules
from i18n import t

# Asked of every address to see if a computer is there. Any answer counts, even a refusal: only silence means nothing.
PRESENCE_REQUESTS = ("22F190", "1902AF", "2101")
# Number ranges a computer may keep its own data items in, asked only in a deep map. Kia uses BCxx on its body computer.
DEEP_RANGES = ((0xBC00, 0xBD00), (0x0100, 0x0200), (0xB000, 0xB100))
DEEP_SILENT_LIMIT = 40  # silent replies in a row before moving on from a range
STANDARD_ITEMS = range(0xF180, 0xF1A0)


def known_items(make, year=None):
    """Data items the community car profiles on this laptop read for this maker. Returns {request address: {request: names}}."""
    found = {}
    if not make:
        return found
    for name in car_modules.catalog():
        if not name.lower().startswith(make.lower()):
            continue
        for path in car_modules.local_profile_files(name).values():
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for command in profile.get("commands", []):
                usable = car_modules.usable_command(command, year)
                if not usable:
                    continue
                hdr, _, service, argument = usable
                names = found.setdefault(hdr.upper(), {}).setdefault(service + argument.upper(), [])
                for signal in command.get("signals", []):
                    if signal.get("name") and signal["name"] not in names:
                        names.append(signal["name"])
    return found


def describe(reply, request):
    """Turn a reply into (state, text, raw hex). States: answered, refused, empty."""
    if reply[:1] == b"\x7f":
        return "refused", car_modules.refusal_reason(reply) or "Reason not known", reply.hex(" ").upper()
    echo = len(request) // 2  # the positive reply repeats the request after its first byte
    data = reply[echo:]
    if not data:
        return "empty", "", ""
    # Zeros and other binary values are real data, such as a door switch that reads 00 when closed, so only text
    # is shown as words and everything else as the bytes themselves.
    date = car_modules.readable_date(data) if request[:2] == "22" and int(request[2:], 16) in car_modules.IDENTITY_DATES else None
    text = date or car_modules.readable_detail(data) or data.hex(" ").upper()
    return "answered", text, data.hex(" ").upper()


async def ask(elm, header, request):
    """Ask one module for one data item. Returns (state, text, raw) or None if it stayed silent."""
    request_id = f"{int(header, 16) - 8:03X}"
    reply = await elm.module_request(request_id, header, request, keep_address=True)
    if reply is None:
        return None
    if not getattr(reply, "complete", True):
        reply = await elm.module_request(request_id, header, request, fc=True, keep_address=True) or reply
    return describe(reply, request)


async def find_modules(elm, on_progress=None):
    """Ask every possible address whether a computer is there. Returns [{header, request, how}] for the ones that answered."""
    found = []
    try:
        for index, number in enumerate(car_modules.SCAN_IDS):
            if on_progress:
                on_progress(index + 1, len(car_modules.SCAN_IDS))
            header = f"{number + 8:03X}"
            for request in PRESENCE_REQUESTS:
                result = await ask(elm, header, request)
                if result is not None:
                    found.append({"header": header, "request": f"{number:03X}", "how": request, "first": result[0]})
                    break
    finally:
        await elm.restore_addressing()
    return found


async def map_module(elm, header, known, deep=False, on_progress=None):
    """Ask one module for every data item worth trying. Returns a list of items with what each gave."""
    plan = [(request, names) for request, names in known.get(f"{int(header, 16) - 8:03X}", {}).items()]
    plan += [(f"22{item:04X}", [car_modules.IDENTITY_ITEMS.get(item, "")]) for item in STANDARD_ITEMS if f"22{item:04X}" not in dict(plan)]
    items = []
    try:
        for index, (request, names) in enumerate(plan):
            if on_progress:
                on_progress(index + 1, len(plan))
            result = await ask(elm, header, request)
            items.append(item_row(request, names, result))
        if deep and any(item["state"] == "answered" for item in items):
            for start, end in DEEP_RANGES:
                silent = 0
                for number in range(start, end):
                    request = f"22{number:04X}"
                    if any(item["request"] == request for item in items):
                        continue
                    result = await ask(elm, header, request)
                    if result is None:
                        silent += 1
                        if silent >= DEEP_SILENT_LIMIT:
                            break
                        continue
                    silent = 0
                    if result[0] != "refused" or result[1] not in ("Value not available", "Request type not supported"):
                        items.append(item_row(request, [], result))  # only items that exist are worth listing
    finally:
        await elm.restore_addressing()
    return items


def item_row(request, names, result):
    state, text, raw = result if result else ("silent", "", "")
    return {"request": request, "names": [name for name in names if name], "state": state, "text": text, "raw": raw}


async def explore(elm, make=None, year=None, deep=False, say=print):
    """Find the car's computers and map what each will share. Returns {time, modules}."""
    say(t("Step 1 of 2: asking each of {count} possible addresses whether a computer is there ...", count=len(car_modules.SCAN_IDS)))
    modules = await find_modules(elm, on_progress=lambda done, total: None)
    say(t("Found {count} computer(s): {list}", count=len(modules), list=", ".join(module["header"] for module in modules) or "-"))
    known = known_items(make, year)
    for module in modules:
        say(t("Step 2 of 2: reading what computer {header} will share ...", header=module["header"]))
        module["name"] = car_modules.LIKELY_MODULES.get(module["request"], "Unknown module")
        module["items"] = await map_module(elm, module["header"], known, deep)
        answered = sum(item["state"] == "answered" for item in module["items"])
        say(t("  {answered} answered, {refused} refused, {silent} silent", answered=answered,
              refused=sum(item["state"] == "refused" for item in module["items"]),
              silent=sum(item["state"] == "silent" for item in module["items"])))
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "deep": deep, "modules": modules}


async def read_again(elm, last):
    """Read again every item that answered before, and mark which changed. Returns a new map."""
    modules = []
    for module in last.get("modules", []):
        before = {item["request"]: item for item in module.get("items", []) if item["state"] == "answered"}
        items = []
        try:
            for request, item in before.items():
                result = await ask(elm, module["header"], request)
                row = item_row(request, item["names"], result)
                row["changed"] = row["raw"] != item["raw"]
                row["before"] = item["raw"]
                items.append(row)
        finally:
            await elm.restore_addressing()
        modules.append({**module, "items": items})
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "deep": last.get("deep", False), "modules": modules, "compared": last.get("time")}


def report(found, lang=None):
    """The map as text, for keeping or sharing."""
    lines = [t("ECU explorer map", lang), found.get("time", "").replace("T", " "), ""]
    for module in found.get("modules", []):
        lines.append(f"{module['header']} ({t('asked at', lang)} {module['request']}) {t(module.get('name', ''), lang)}")
        for item in module.get("items", []):
            if item["state"] == "silent":
                continue
            label = f"{item['request']}  " + (", ".join(item["names"][:3]) if item["names"] else "")
            value = item["text"] if item["state"] != "refused" else t(item["text"], lang)
            changed = f"  [{t('changed', lang)}: {item['before']} -> {item['raw']}]" if item.get("changed") else ""
            lines.append(f"  {label.strip()}: {t(item['state'], lang)}: {value}{changed}  ({item['raw']})")
        lines.append("")
    return "\n".join(lines)
