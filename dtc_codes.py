"""Fault code names and descriptions: a friendly table of common codes, then the full OBDex list."""
import gzip
import json
from functools import lru_cache
from pathlib import Path

GENERIC_CODES_FILE = Path(__file__).parent / "data" / "dtc_generic.json"
DETAILS_FILE = Path(__file__).parent / "data" / "dtc_details.json.gz"
OEM_CODES_FILE = Path(__file__).parent / "data" / "dtc_oem.json"
PLANS_FILE = Path(__file__).parent / "data" / "repair_plans.json"
PLANS_ARABIC_FILE = Path(__file__).parent / "data" / "repair_plans_ar.json"

# The first characters of a VIN name the maker. Only makes with a code list are here, longest prefix first.
VIN_MAKERS = {
    "KNA": "Kia", "KNC": "Kia", "KND": "Kia", "KNE": "Kia", "U5Y": "Kia", "U6Y": "Kia", "3KP": "Kia",
    "JTH": "Lexus", "JTJ": "Lexus", "JT": "Toyota", "4T": "Toyota", "5T": "Toyota", "2T": "Toyota",
    "JH4": "Acura", "19U": "Acura", "JHM": "Honda", "1HG": "Honda", "2HG": "Honda", "5FN": "Honda", "5J6": "Honda",
    "JNK": "Infiniti", "JN1": "Nissan", "JN8": "Nissan", "1N4": "Nissan", "1N6": "Nissan", "3N1": "Nissan", "5N1": "Nissan",
    "1FA": "Ford", "1FM": "Ford", "1FT": "Ford", "2FM": "Ford", "3FA": "Ford", "WF0": "Ford",
    "1G1": "Chevrolet", "1GC": "Chevrolet", "1GN": "Chevrolet", "2G1": "Chevrolet", "3G1": "Chevrolet",
    "1GT": "GMC", "1GK": "GMC", "2GT": "GMC", "3GT": "GMC",
    "JM1": "Mazda", "JM3": "Mazda", "4F2": "Mazda", "JA3": "Mitsubishi", "JA4": "Mitsubishi", "ML3": "Mitsubishi",
    "JF1": "Subaru", "JF2": "Subaru", "4S3": "Subaru", "4S4": "Subaru", "JS2": "Suzuki", "JS3": "Suzuki",
    "WVW": "Volkswagen", "WVG": "Volkswagen", "1VW": "Volkswagen", "3VW": "Volkswagen", "WAU": "Audi", "WA1": "Audi",
    "WBA": "BMW", "WBS": "BMW", "WBX": "BMW", "5UX": "BMW", "5YM": "BMW",
    "WDB": "Mercedes", "WDD": "Mercedes", "WDC": "Mercedes", "4JG": "Mercedes", "W1K": "Mercedes", "SAJ": "Jaguar",
    "1J4": "Jeep", "1J8": "Jeep", "1C4": "Jeep", "1B3": "Dodge", "1D3": "Dodge", "2B3": "Dodge", "3D3": "Dodge",
    "1C3": "Chrysler", "2C3": "Chrysler",
}
_car_make = None


def make_from_vin(vin):
    """Return the maker named by a VIN, or None if it isn't one Fahis has a code list for."""
    vin = (vin or "").upper()
    return VIN_MAKERS.get(vin[:3]) or VIN_MAKERS.get(vin[:2])


def set_car_make(make):
    """Remember which maker's codes apply to the connected car."""
    global _car_make
    _car_make = make


def car_make():
    return _car_make


@lru_cache(maxsize=1)
def oem_codes():
    return json.loads(OEM_CODES_FILE.read_text(encoding="utf-8"))


def oem_meanings(code):
    """Return {make: meaning}. The same code can mean different things, so only the connected car's maker counts."""
    meanings = {make: codes[code] for make, codes in oem_codes().items() if code in codes}
    return {_car_make: meanings[_car_make]} if _car_make in meanings else meanings


def oem_meaning(code):
    """Return the meaning for the connected car's maker, or None."""
    return oem_codes().get(_car_make, {}).get(code)


@lru_cache(maxsize=1)
def _plans():
    return json.loads(PLANS_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _plans_arabic():
    return json.loads(PLANS_ARABIC_FILE.read_text(encoding="utf-8"))


def repair_plans(code, lang="en"):
    """Return the repair plans for a code. A maker's own plan is left out when another maker's car is connected."""
    words = _plans_arabic() if lang == "ar" else {}
    return [{**plan, "fixes": [{**fix, "title": words.get(fix["title"], fix["title"]),
                                "steps": [words.get(step, step) for step in fix["steps"]]} for fix in plan["fixes"]]}
            for plan in _plans().get(code, []) if not (plan["make"] and _car_make and plan["make"] != _car_make)]


def dtc_name(a, b):
    """Turn two code bytes into a name like P0171."""
    return f"{'PCBU'[a >> 6]}{(a >> 4) & 3}{a & 0x0F:X}{b:02X}"


@lru_cache(maxsize=1)
def generic_codes():
    return json.loads(GENERIC_CODES_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def all_details():
    return json.loads(gzip.decompress(DETAILS_FILE.read_bytes()))


def dtc_details(code):
    """Return the causes, symptoms and description for a generic code, in English, or None."""
    return all_details().get(code)


def lookup_dtc(code, lang="en"):
    """Return a description for a generic code, or None. Arabic falls back to English."""
    if lang == "ar" and code in DTC_DESCRIPTIONS_AR:
        return DTC_DESCRIPTIONS_AR[code]
    return DTC_DESCRIPTIONS.get(code) or generic_codes().get(code)


DTC_DESCRIPTIONS_AR = {
    "P0100": "مشكلة في دائرة حساس تدفق الهواء (MAF)",
    "P0101": "قراءة حساس تدفق الهواء (MAF) خارج النطاق المتوقع",
    "P0102": "إشارة حساس تدفق الهواء (MAF) منخفضة جدًا",
    "P0103": "إشارة حساس تدفق الهواء (MAF) مرتفعة جدًا",
    "P0106": "قراءة حساس ضغط السحب (MAP) خارج النطاق المتوقع",
    "P0107": "إشارة حساس ضغط السحب (MAP) منخفضة جدًا",
    "P0108": "إشارة حساس ضغط السحب (MAP) مرتفعة جدًا",
    "P0110": "مشكلة في دائرة حساس حرارة هواء السحب",
    "P0112": "إشارة حساس حرارة هواء السحب منخفضة جدًا",
    "P0113": "إشارة حساس حرارة هواء السحب مرتفعة جدًا",
    "P0115": "مشكلة في دائرة حساس حرارة سائل التبريد",
    "P0116": "قراءة حساس حرارة سائل التبريد خارج النطاق المتوقع",
    "P0117": "إشارة حساس حرارة سائل التبريد منخفضة جدًا",
    "P0118": "إشارة حساس حرارة سائل التبريد مرتفعة جدًا",
    "P0120": "مشكلة في دائرة حساس موضع الخانق",
    "P0121": "قراءة حساس موضع الخانق خارج النطاق المتوقع",
    "P0122": "إشارة حساس موضع الخانق منخفضة جدًا",
    "P0123": "إشارة حساس موضع الخانق مرتفعة جدًا",
    "P0125": "يستغرق المحرك وقتًا طويلًا ليسخن بما يكفي للتحكم بالوقود",
    "P0128": "سائل التبريد أبرد من حرارة عمل الثرموستات (غالبًا ثرموستات عالق مفتوحًا)",
    "P0130": "مشكلة في دائرة حساس الأكسجين (البنك 1، الحساس 1)",
    "P0131": "جهد حساس الأكسجين منخفض (البنك 1، الحساس 1)",
    "P0132": "جهد حساس الأكسجين مرتفع (البنك 1، الحساس 1)",
    "P0133": "استجابة حساس الأكسجين بطيئة (البنك 1، الحساس 1)",
    "P0134": "لا نشاط في حساس الأكسجين (البنك 1، الحساس 1)",
    "P0135": "مشكلة في سخان حساس الأكسجين (البنك 1، الحساس 1)",
    "P0136": "مشكلة في دائرة حساس الأكسجين (البنك 1، الحساس 2)",
    "P0137": "جهد حساس الأكسجين منخفض (البنك 1، الحساس 2)",
    "P0138": "جهد حساس الأكسجين مرتفع (البنك 1، الحساس 2)",
    "P0140": "لا نشاط في حساس الأكسجين (البنك 1، الحساس 2)",
    "P0141": "مشكلة في سخان حساس الأكسجين (البنك 1، الحساس 2)",
    "P0171": "خليط الوقود فقير جدًا (البنك 1)",
    "P0172": "خليط الوقود غني جدًا (البنك 1)",
    "P0174": "خليط الوقود فقير جدًا (البنك 2)",
    "P0175": "خليط الوقود غني جدًا (البنك 2)",
    "P0201": "مشكلة في دائرة البخاخ (الأسطوانة 1)",
    "P0202": "مشكلة في دائرة البخاخ (الأسطوانة 2)",
    "P0203": "مشكلة في دائرة البخاخ (الأسطوانة 3)",
    "P0204": "مشكلة في دائرة البخاخ (الأسطوانة 4)",
    "P0217": "ارتفاع حرارة المحرك",
    "P0230": "مشكلة في دائرة مضخة الوقود",
    "P0300": "اختلال إشعال عشوائي أو في عدة أسطوانات",
    "P0301": "اختلال إشعال في الأسطوانة 1",
    "P0302": "اختلال إشعال في الأسطوانة 2",
    "P0303": "اختلال إشعال في الأسطوانة 3",
    "P0304": "اختلال إشعال في الأسطوانة 4",
    "P0305": "اختلال إشعال في الأسطوانة 5",
    "P0306": "اختلال إشعال في الأسطوانة 6",
    "P0325": "مشكلة في دائرة حساس الطرق (البنك 1)",
    "P0335": "مشكلة في دائرة حساس موضع عمود المرفق",
    "P0340": "مشكلة في دائرة حساس موضع عمود الكامات",
    "P0351": "مشكلة في دائرة ملف الإشعال (الملف A)",
    "P0352": "مشكلة في دائرة ملف الإشعال (الملف B)",
    "P0353": "مشكلة في دائرة ملف الإشعال (الملف C)",
    "P0354": "مشكلة في دائرة ملف الإشعال (الملف D)",
    "P0400": "مشكلة في تدفق إعادة تدوير العادم (EGR)",
    "P0401": "تدفق إعادة تدوير العادم (EGR) منخفض جدًا",
    "P0402": "تدفق إعادة تدوير العادم (EGR) مرتفع جدًا",
    "P0420": "كفاءة المحفّز أقل من الحد المطلوب (البنك 1)",
    "P0430": "كفاءة المحفّز أقل من الحد المطلوب (البنك 2)",
    "P0440": "مشكلة في نظام التحكم بتبخر الوقود",
    "P0442": "تسرب صغير في نظام تبخر الوقود",
    "P0446": "مشكلة في التحكم بفتحة تهوية نظام تبخر الوقود",
    "P0455": "تسرب كبير في نظام تبخر الوقود (افحص غطاء الوقود)",
    "P0456": "تسرب صغير جدًا في نظام تبخر الوقود",
    "P0500": "مشكلة في حساس سرعة السيارة",
    "P0505": "مشكلة في نظام التحكم بالسرعة الخاملة",
    "P0506": "السرعة الخاملة أقل من المتوقع",
    "P0507": "السرعة الخاملة أعلى من المتوقع",
    "P0562": "جهد النظام منخفض جدًا",
    "P0563": "جهد النظام مرتفع جدًا",
    "P0600": "مشكلة في وصلة الاتصال التسلسلي",
    "P0606": "مشكلة في معالج وحدة التحكم بالمحرك",
    "P0700": "عطل في نظام التحكم بناقل الحركة (اقرأ وحدة ناقل الحركة للتفاصيل)",
    "P0705": "مشكلة في دائرة حساس نطاق ناقل الحركة",
    "P0715": "مشكلة في دائرة حساس سرعة دخل ناقل الحركة",
    "P0720": "مشكلة في دائرة حساس سرعة خرج ناقل الحركة",
    "U0100": "فقدان الاتصال بوحدة التحكم بالمحرك",
    "U0101": "فقدان الاتصال بوحدة التحكم بناقل الحركة",
    "U0121": "فقدان الاتصال بوحدة ABS",
    "U0155": "فقدان الاتصال بلوحة العدادات",
}


DTC_DESCRIPTIONS = {
    "P0100": "Air flow (MAF) sensor circuit problem",
    "P0101": "Air flow (MAF) sensor reading out of expected range",
    "P0102": "Air flow (MAF) sensor signal too low",
    "P0103": "Air flow (MAF) sensor signal too high",
    "P0106": "Intake pressure (MAP) sensor reading out of expected range",
    "P0107": "Intake pressure (MAP) sensor signal too low",
    "P0108": "Intake pressure (MAP) sensor signal too high",
    "P0110": "Intake air temperature sensor circuit problem",
    "P0112": "Intake air temperature sensor signal too low",
    "P0113": "Intake air temperature sensor signal too high",
    "P0115": "Coolant temperature sensor circuit problem",
    "P0116": "Coolant temperature sensor reading out of expected range",
    "P0117": "Coolant temperature sensor signal too low",
    "P0118": "Coolant temperature sensor signal too high",
    "P0120": "Throttle position sensor circuit problem",
    "P0121": "Throttle position sensor reading out of expected range",
    "P0122": "Throttle position sensor signal too low",
    "P0123": "Throttle position sensor signal too high",
    "P0125": "Engine takes too long to warm up for fuel control",
    "P0128": "Coolant stays below thermostat temperature (often a stuck-open thermostat)",
    "P0130": "Oxygen sensor circuit problem (bank 1, sensor 1)",
    "P0131": "Oxygen sensor low voltage (bank 1, sensor 1)",
    "P0132": "Oxygen sensor high voltage (bank 1, sensor 1)",
    "P0133": "Oxygen sensor slow response (bank 1, sensor 1)",
    "P0134": "Oxygen sensor no activity (bank 1, sensor 1)",
    "P0135": "Oxygen sensor heater problem (bank 1, sensor 1)",
    "P0136": "Oxygen sensor circuit problem (bank 1, sensor 2)",
    "P0137": "Oxygen sensor low voltage (bank 1, sensor 2)",
    "P0138": "Oxygen sensor high voltage (bank 1, sensor 2)",
    "P0140": "Oxygen sensor no activity (bank 1, sensor 2)",
    "P0141": "Oxygen sensor heater problem (bank 1, sensor 2)",
    "P0171": "Fuel mixture too lean (bank 1)",
    "P0172": "Fuel mixture too rich (bank 1)",
    "P0174": "Fuel mixture too lean (bank 2)",
    "P0175": "Fuel mixture too rich (bank 2)",
    "P0201": "Injector circuit problem (cylinder 1)",
    "P0202": "Injector circuit problem (cylinder 2)",
    "P0203": "Injector circuit problem (cylinder 3)",
    "P0204": "Injector circuit problem (cylinder 4)",
    "P0217": "Engine overheating",
    "P0230": "Fuel pump circuit problem",
    "P0300": "Random or multiple cylinder misfire",
    "P0301": "Misfire in cylinder 1",
    "P0302": "Misfire in cylinder 2",
    "P0303": "Misfire in cylinder 3",
    "P0304": "Misfire in cylinder 4",
    "P0305": "Misfire in cylinder 5",
    "P0306": "Misfire in cylinder 6",
    "P0325": "Knock sensor circuit problem (bank 1)",
    "P0335": "Crankshaft position sensor circuit problem",
    "P0340": "Camshaft position sensor circuit problem",
    "P0351": "Ignition coil circuit problem (coil A)",
    "P0352": "Ignition coil circuit problem (coil B)",
    "P0353": "Ignition coil circuit problem (coil C)",
    "P0354": "Ignition coil circuit problem (coil D)",
    "P0400": "Exhaust gas recirculation (EGR) flow problem",
    "P0401": "Exhaust gas recirculation (EGR) flow too low",
    "P0402": "Exhaust gas recirculation (EGR) flow too high",
    "P0420": "Catalytic converter efficiency below threshold (bank 1)",
    "P0430": "Catalytic converter efficiency below threshold (bank 2)",
    "P0440": "Evaporative emission system problem",
    "P0442": "Small leak in evaporative emission system",
    "P0446": "Evaporative emission vent control problem",
    "P0455": "Large leak in evaporative emission system (check the fuel cap)",
    "P0456": "Very small leak in evaporative emission system",
    "P0500": "Vehicle speed sensor problem",
    "P0505": "Idle control system problem",
    "P0506": "Idle speed lower than expected",
    "P0507": "Idle speed higher than expected",
    "P0562": "System voltage too low",
    "P0563": "System voltage too high",
    "P0600": "Serial communication link problem",
    "P0606": "Engine control module processor problem",
    "P0700": "Transmission control system fault (read the transmission module for details)",
    "P0705": "Transmission range sensor circuit problem",
    "P0715": "Transmission input speed sensor circuit problem",
    "P0720": "Transmission output speed sensor circuit problem",
    "U0100": "Lost communication with engine control module",
    "U0101": "Lost communication with transmission control module",
    "U0121": "Lost communication with ABS control module",
    "U0155": "Lost communication with instrument cluster",
}
