"""Offline tests using replies recorded from the car, so no adapter is needed."""
import asyncio
import html
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bridge
import car_modules
import demo
import dtc_codes
import garage
import i18n
import obd_tool
from obd_tool import ELM, LIVE_PIDS, OBDError, decode_dtcs, decode_monitors, describe_dtc, parse_can


ROOT = Path(__file__).parent


class Sequence(list):
    """Canned replies for one command, one per attempt."""


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.sent = []

    async def send(self, command, wait=5):
        self.sent.append(command)
        reply = self.replies.get(command, ["NO DATA"])
        return reply.pop(0) if isinstance(reply, Sequence) else reply


def car_replies(**overrides):
    replies = {
        "ATZ": ["ATZ", "ELM327 v2.1"],
        "ATDPN": ["A6"],
        "0100": ["SEARCHING...", "7E9 06 41 00 98 18 80 13", "7E8 06 41 00 BE 3F B8 13"],
    }
    replies.update(overrides)
    return replies


class ParseTests(unittest.TestCase):
    def test_two_modules_single_frame(self):
        replies = parse_can(["7E9 03 41 05 78", "7E8 03 41 05 78"])
        self.assertEqual(list(replies), ["7E8", "7E9"])
        self.assertEqual(replies["7E8"], bytes([0x41, 0x05, 0x78]))

    def test_multi_frame_is_joined_and_trimmed(self):
        replies = parse_can(["7E8 10 0A 43 04 01 33 04 20", "7E8 21 03 00 01 71 00 00 00"])
        self.assertEqual(decode_dtcs(replies["7E8"]), ["P0133", "P0420", "P0300", "P0171"])

    def test_noise_is_ignored(self):
        self.assertEqual(parse_can(["SEARCHING...", "NO DATA", "CAN ERROR"]), {})


class DecodeTests(unittest.TestCase):
    def test_no_codes(self):
        self.assertEqual(decode_dtcs(bytes([0x43, 0x00])), [])

    def test_code_letters(self):
        payload = bytes([0x43, 0x04, 0x01, 0x71, 0x41, 0x23, 0x80, 0x45, 0xC1, 0x00])
        self.assertEqual(decode_dtcs(payload), ["P0171", "C0123", "B0045", "U0100"])

    def test_descriptions(self):
        self.assertIn("lean", describe_dtc("P0171"))
        self.assertIn("manufacturer-specific", describe_dtc("P1234"))
        self.assertIn("Shift Solenoid", describe_dtc("P0999"))

    def test_monitors_recorded_from_car(self):
        status = decode_monitors(bytes.fromhex("410100 07E500".replace(" ", "")))
        self.assertFalse(status["mil"])
        self.assertEqual(status["count"], 0)
        self.assertEqual(dict(status["tests"])["Catalyst"], True)
        self.assertEqual(len(status["tests"]), 8)

    def test_monitors_light_on_and_unfinished(self):
        status = decode_monitors(bytes.fromhex("410182 17E504"))
        self.assertTrue(status["mil"])
        self.assertEqual(status["count"], 2)
        tests = dict(status["tests"])
        self.assertFalse(tests["Misfire"])
        self.assertFalse(tests["Evaporative system"])

    def test_live_formulas_recorded_from_car(self):
        self.assertEqual(LIVE_PIDS[0x05][2](bytes([0x78])), 80)
        self.assertEqual(LIVE_PIDS[0x0C][2](bytes([0x1A, 0xF8])), 1726)


class ElmTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_ok(self):
        elm = ELM(FakeTransport(car_replies()))
        await elm.setup()
        self.assertEqual(elm.version, "ELM327 v2.1")
        self.assertIn("500 kbps", elm.protocol)

    async def test_setup_refuses_other_protocols(self):
        with self.assertRaises(OBDError):
            await ELM(FakeTransport(car_replies(ATDPN=["A3"]))).setup()

    async def test_setup_reports_silent_car(self):
        with self.assertRaises(OBDError):
            await ELM(FakeTransport(car_replies(**{"0100": ["UNABLE TO CONNECT"]}))).setup()

    async def test_lost_reply_is_retried(self):
        transport = FakeTransport({"0105": Sequence([None, ["7E8 03 41 05 78"]])})
        self.assertEqual(await ELM(transport).read_pid(0x05), 80)

    async def test_stale_reply_for_other_pid_is_rejected(self):
        transport = FakeTransport({"0105": Sequence([["7E8 04 41 0C 00 00"], ["7E8 03 41 05 78"]])})
        self.assertEqual(await ELM(transport).read_pid(0x05), 80)

    async def test_supported_pids_recorded_from_car(self):
        transport = FakeTransport({
            "0100": ["7E8 06 41 00 BE 3F B8 13"],
            "0120": ["7E8 06 41 20 00 00 00 00"],
        })
        supported = await ELM(transport).supported_pids()
        for pid in (0x01, 0x05, 0x0C, 0x0D, 0x1F, 0x20):
            self.assertIn(pid, supported)
        self.assertNotIn(0x02, supported)


class EverythingTests(unittest.IsolatedAsyncioTestCase):
    def test_new_formulas(self):
        self.assertEqual(LIVE_PIDS[0x03][2](bytes([2, 0])), "Closed loop")
        self.assertEqual(LIVE_PIDS[0x03][2](bytes([0, 0])), "Not active")
        self.assertIn("KOBD", LIVE_PIDS[0x1C][2](bytes([30])))
        self.assertEqual(LIVE_PIDS[0x31][2](bytes([0xFF, 0xFF])), "65535+")
        self.assertEqual(LIVE_PIDS[0x31][2](bytes([0x01, 0x00])), 256)
        self.assertEqual(LIVE_PIDS[0x13][2](bytes([0x03])), "B1S1, B1S2")
        self.assertEqual(LIVE_PIDS[0x14][0], "O2 voltage B1S1")
        self.assertEqual(LIVE_PIDS[0x14][2](bytes([0x64, 0xFF])), 0.5)
        self.assertEqual(LIVE_PIDS[0x1B][0], "O2 voltage B2S4")
        self.assertEqual(LIVE_PIDS[0x32][2](bytes([0xFF, 0xFC])), -1)
        self.assertEqual(LIVE_PIDS[0x3C][2](bytes([0x12, 0xC0])), 440)

    def test_decode_vin(self):
        lines = ["7E8 10 14 49 02 01 31 47 31", "7E8 21 4A 43 35 34 34 34 52", "7E8 22 37 32 35 32 33 36 37"]
        self.assertEqual(obd_tool.decode_text(parse_can(lines)["7E8"]), "1G1JC5444R7252367")

    async def snapshot(self, **extra):
        return await obd_tool.read_snapshot(ELM(FakeTransport({
            "0100": ["7E8 06 41 00 80 00 00 01"],
            "0101": ["7E8 06 41 01 00 04 00 00"],
            "0120": ["7E8 06 41 20 00 00 00 40"],
            "01": [],
            "011F": ["7E8 04 41 1F 00 2A"],
            "ATRV": ["12.6V"],
            **extra,
        })))

    async def test_snapshot_decodes_known_pids(self):
        snapshot = await self.snapshot(**{"0100": ["7E8 06 41 00 00 00 00 02"]})
        self.assertEqual(snapshot["values"], {0x1F: 42})
        self.assertEqual(snapshot["raw"], {})
        self.assertIsNone(snapshot["freeze"])

    async def test_undecoded_pid_shown_raw(self):
        with mock.patch.object(car_modules, "decode_standard", return_value=[]):
            snapshot = await self.snapshot(**{
                "0100": ["7E8 06 41 00 40 00 00 00"],
                "0102": ["7E8 04 41 02 12 34"],
            })
        self.assertEqual(snapshot["raw"], {0x02: "12 34"})
        self.assertIn("02  12 34", obd_tool.format_report(snapshot))

    async def test_freeze_frame_read_when_code_stored(self):
        snapshot = await self.snapshot(**{
            "03": ["7E8 04 43 01 01 71"],
            "020200": ["7E8 05 42 02 00 01 71"],
            "020000": ["7E8 07 42 00 00 08 00 00 00"],
            "020500": ["7E8 04 42 05 00 78"],
        })
        self.assertEqual(snapshot["freeze"]["code"], "P0171")
        self.assertEqual(snapshot["freeze"]["values"], [{"name": "Coolant temp", "unit": "C", "text": "80"}])

    async def test_reader_reads_fast_every_step_and_rotates_slow(self):
        transport = FakeTransport({"ATRV": ["12.6V"]})
        reader = obd_tool.LiveReader(ELM(transport), [0x0C, 0x05, 0x0F, 0x10])
        for _ in range(2):
            await reader.step()
        sent = [c for c in transport.sent if c != "ATRV"]
        self.assertEqual(sent.count("010C"), 2)
        for pid in ("0105", "010F", "0110"):
            self.assertIn(pid, sent)

    def test_values_are_marked_only_once_they_are_old(self):
        reader = obd_tool.LiveReader(ELM(FakeTransport({})), [0x0C, 0x05], {0x0C: 800.0, 0x05: 90})
        self.assertIsNone(reader.age_note(0x0C))
        self.assertIsNone(reader.age_note(0x05))
        reader.read_at[0x05] -= obd_tool.FAST_STALE_SECONDS + 1  # normal for a slow value
        self.assertIsNone(reader.age_note(0x05))
        reader.read_at[0x05] -= obd_tool.SLOW_STALE_SECONDS
        self.assertIn("seconds ago", reader.age_note(0x05))
        reader.read_at[0x0C] -= obd_tool.FAST_STALE_SECONDS + 1
        self.assertIn("seconds ago", reader.age_note(0x0C))
        self.assertIn("قبل", reader.age_note(0x0C, "ar"))

    async def test_values_from_before_the_engine_stopped_are_marked(self):
        transport = FakeTransport({"010C": ["7E8 04 41 0C 00 00"], "ATRV": ["13.9V"]})
        pids = [0x0C, 0x05, 0x0F, 0x10]
        reader = obd_tool.LiveReader(ELM(transport), pids, {0x0C: 800.0, 0x05: 90, 0x0F: 58, 0x10: 6.75})
        for pid in pids:
            reader.read_at[pid] -= 1
        await reader.step()  # reads RPM, then only two of the three slow values
        self.assertFalse(reader.engine_running)
        self.assertIsNone(reader.age_note(0x0C))
        self.assertEqual(reader.age_note(0x10), "Read before the engine stopped")
        reader.store(0x10, 0.0)  # the mark goes once the value is read again
        self.assertIsNone(reader.age_note(0x10))
        reader.read_at[0x10] -= 1
        reader.store(0x0C, 800.0)
        self.assertEqual(reader.age_note(0x10), "Read before the engine started")

    async def test_page_state_carries_the_mark(self):
        elm = ELM(FakeTransport({}))
        reader = obd_tool.LiveReader(elm, [0x05], {0x05: 90})
        reader.read_at[0x05] -= obd_tool.SLOW_STALE_SECONDS + 1
        snapshot = {"freeze": None, "adapter": "ELM327", "protocol": "CAN", "vehicle": {}, "monitors": {}, "codes": {},
                    "this_drive": [], "more": [], "raw": {}}
        value = obd_tool.dashboard_state(snapshot, reader, lang="en")["values"][0]
        self.assertIn("seconds ago", value["note"])


class BatteryTests(unittest.IsolatedAsyncioTestCase):
    def test_advice_bands(self):
        self.assertIn("full", obd_tool.battery_advice(12.7, False))
        self.assertIn("75%", obd_tool.battery_advice(12.4, False))
        self.assertIn("50%", obd_tool.battery_advice(12.2, False))
        self.assertIn("25%", obd_tool.battery_advice(12.0, False))
        self.assertIn("Very low", obd_tool.battery_advice(11.5, False))
        self.assertIn("Normal", obd_tool.battery_advice(14.2, True))
        self.assertIn("not be charging", obd_tool.battery_advice(12.5, True))

    async def test_works_when_car_does_not_answer(self):
        transport = FakeTransport({"ATZ": ["ELM327 v2.1"], "ATRV": ["11.9V"], "0100": ["UNABLE TO CONNECT"]})
        with mock.patch("asyncio.sleep", mock.AsyncMock()), mock.patch("builtins.print") as printed:
            await obd_tool.run_battery(ELM(transport), None)
        output = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("11.9 V", output)
        self.assertIn("Very low", output)


class AddressedTransport(FakeTransport):
    """Answers by module: replies are keyed like '7D1:1902AF'."""

    def __init__(self, replies):
        super().__init__(replies)
        self.hdr = "7DF"

    async def send(self, command, wait=5):
        self.sent.append(command)
        if command.startswith("ATSH"):
            self.hdr = command[4:]
            return ["OK"]
        if command.startswith("AT"):
            return ["OK"]
        return self.replies.get(f"{self.hdr}:{command}", ["NO DATA"])


def profile_command(service, argument, hdr):
    return next(c for c in car_modules.load_profile("kia-picanto")["commands"]
                if c["hdr"] == hdr and c["cmd"].get(service) == argument)


def decoded(command, data):
    return {s["id"]: car_modules.decode_signal(s["fmt"], data) for s in command["signals"]}


class ProfileDecodingTests(unittest.TestCase):
    """Expected values and recordings come from OBDb's Kia-Picanto test cases (CC BY-SA 4.0)."""

    def test_body_module_recording(self):
        reply = parse_can(["778 10 0B 62 BC 03 FF EE 20", "778 21 63 0A 66 00 00 AA AA"])["778"]
        values = decoded(profile_command("22", "BC03", "770"), reply[3:])
        self.assertEqual(values["PICANTO_BELT_DRIV"], 1)
        self.assertEqual(values["PICANTO_BELT_PASS"], 1)
        self.assertEqual(values["PICANTO_HANDBRAKE"], 1)
        self.assertEqual(values["PICANTO_DOOR_FL"], 0)
        self.assertEqual(values["PICANTO_TRUNK"], 0)

    def test_door_open_recording(self):
        reply = parse_can(["778 10 0B 62 BC 03 FF EE 20", "778 21 63 7A 60 00 00 AA AA"])["778"]
        values = decoded(profile_command("22", "BC03", "770"), reply[3:])
        self.assertEqual(values["PICANTO_DOOR_FL"], 1)
        self.assertEqual(values["PICANTO_DOOR_FR"], 1)

    def test_cluster_recording(self):
        reply = parse_can(["7CE 10 0F 62 B0 02 E0 00 00", "7CE 21 00 37 B2 00 26 69 00", "7CE 22 00 00 AA AA AA AA AA"])["7CE"]
        values = decoded(profile_command("22", "B002", "7C6"), reply[3:])
        self.assertAlmostEqual(values["PICANTO_CLUSTER_VOLTAGE"], 13.90625, places=4)
        self.assertEqual(values["PICANTO_FLI_VOL"], 27.5)
        self.assertEqual(values["PICANTO_ODO_KM"], 9833)

    def test_wheel_speed_recording(self):
        reply = parse_can(["7D9 10 11 62 40 00 FF 03 00", "7D9 21 00 0C E1 0C 0C 0C 0B", "7D9 22 F0 FC 00 00 AA AA AA"])["7D9"]
        values = decoded(profile_command("22", "4000", "7D1"), reply[3:])
        self.assertEqual(values["PICANTO_TIRE_FL_VSS_V3"], 12)
        self.assertEqual(values["PICANTO_TIRE_RR_VSS_V3"], 11)

    def test_every_profile_signal_has_a_valid_read_only_command(self):
        for command in car_modules.load_profile("kia-picanto")["commands"]:
            (service, _), = command["cmd"].items()
            self.assertIn(service, car_modules.READ_ONLY_SERVICES)
            for signal in command["signals"]:
                self.assertIn("len", signal["fmt"])

    def test_scaling_rules(self):
        self.assertEqual(car_modules.decode_signal({"bix": 0, "len": 16, "blsb": True, "max": 65535}, bytes([0x34, 0x12])), 0x1234)
        self.assertEqual(car_modules.decode_signal({"bix": 0, "len": 8, "sign": True, "min": -128, "max": 127}, bytes([0xFF])), -1)
        self.assertIsNone(car_modules.decode_signal({"bix": 0, "len": 8, "max": 255, "nullmax": 255}, bytes([0xFF])))
        self.assertIsNone(car_modules.decode_signal({"bix": 8, "len": 8, "max": 255}, bytes([0xFF])))

    def test_missing_profile_points_to_search(self):
        with self.assertRaisesRegex(ValueError, "obd profiles"):
            car_modules.load_profile("not-a-car")

    def test_map_and_two_state_values(self):
        fmt = {"bix": 0, "len": 8, "map": {"1": {"description": "2.7 - 3 V", "value": "27_3V"}}}
        self.assertEqual(car_modules.decode_signal(fmt, bytes([1])), "2.7 - 3 V")
        self.assertIsNone(car_modules.decode_signal(fmt, bytes([9])))
        self.assertEqual(car_modules.format_signal(fmt, "2.7 - 3 V"), "2.7 - 3 V")
        self.assertEqual(car_modules.format_signal({"unit": "yesno"}, 0), "Yes")
        self.assertEqual(car_modules.format_signal({"unit": "offon"}, 1), "On")
        self.assertEqual(car_modules.format_signal({"unit": "hex"}, 255), "FF")
        self.assertEqual(car_modules.format_signal({"unit": "psi"}, 32.5), "32.5 psi")


class CatalogTests(unittest.TestCase):
    def test_search_all_words_any_case(self):
        self.assertIn("Kia-Picanto", car_modules.search_catalog("kia PICANTO"))
        self.assertTrue(all("toyota" in n.lower() for n in car_modules.search_catalog("toyota")))
        self.assertGreater(len(car_modules.catalog()), 800)
        self.assertEqual(car_modules.search_catalog("zzz-not-a-car"), [])

    def test_download_only_accepts_catalog_names(self):
        for bad in ("../../secrets", "not-a-car", "Kia-Picanto/../x"):
            with self.assertRaisesRegex(ValueError, "catalog"):
                car_modules.download_profile(bad)

    def test_catalog_file_is_not_a_profile(self):
        self.assertEqual(car_modules.local_profile_files("catalog"), {})

    def test_year_file_choice(self):
        files = ["default.json", "2012-2020.json", "2015-2016.json", "2024.json"]
        self.assertEqual(car_modules.pick_profile_file(files, 2015), "2015-2016.json")
        self.assertEqual(car_modules.pick_profile_file(files, 2018), "2012-2020.json")
        self.assertEqual(car_modules.pick_profile_file(files, 2024), "2024.json")
        self.assertEqual(car_modules.pick_profile_file(files, 2030), "default.json")
        self.assertEqual(car_modules.pick_profile_file(files, None), "default.json")
        self.assertIsNone(car_modules.pick_profile_file(["2024.json"], 2010))

    def test_year_filter(self):
        self.assertTrue(car_modules.applies_to_year(None, 2017))
        self.assertTrue(car_modules.applies_to_year({"to": 2012}, None))
        self.assertTrue(car_modules.applies_to_year({"from": 2015}, 2017))
        self.assertFalse(car_modules.applies_to_year({"from": 2018}, 2017))
        self.assertFalse(car_modules.applies_to_year({"from": 2012, "to": 2014}, 2017))
        self.assertTrue(car_modules.applies_to_year({"to": 2012, "from": 2025}, 2010))
        self.assertTrue(car_modules.applies_to_year({"from": 2030, "years": [2017]}, 2017))

    def test_usable_commands(self):
        good = {"hdr": "7D1", "rax": "7D9", "cmd": {"22": "4000"}}
        self.assertEqual(car_modules.usable_command(good), ("7D1", "7D9", "22", "4000"))
        self.assertEqual(car_modules.usable_command({"hdr": "7D1", "cmd": {"21": "01"}})[1], "7D9")
        bad = [
            {**good, "din": "03"},                              # opens a special session
            {**good, "eax": "12"},
            {**good, "cmd": {"2E": "F190"}},                    # write
            {**good, "cmd": {"14": "FFFFFF"}},                  # clear codes
            {**good, "cmd": {"22": "40;00"}},                   # could inject another command
            {**good, "hdr": "18DA"},                            # 29-bit
            {**good, "hdr": "7DF"},
            {**good, "filter": {"from": 2020}},
        ]
        for command in bad:
            self.assertIsNone(car_modules.usable_command(command, 2017), command)


class FaultCodeTableTests(unittest.TestCase):
    def test_full_table_is_accurate_for_known_codes(self):
        table = dtc_codes.generic_codes()
        self.assertGreater(len(table), 9000)
        self.assertIn("Catalyst", table["P0420"])
        self.assertIn("Misfire", table["P0300"])
        self.assertIn("Lean", table["P0171"])
        self.assertIn("Lost Communication", table["U0100"])

    def test_friendly_table_wins_then_full_table(self):
        self.assertEqual(describe_dtc("P0171"), "Fuel mixture too lean (bank 1)")
        self.assertIn("Wheel Speed", describe_dtc("C0035"))
        self.assertIn("generic", describe_dtc("B0FFF"))


class ModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_are_refused_before_anything_is_sent(self):
        transport = AddressedTransport({})
        elm = ELM(transport)
        for request in ("14FFFFFF", "04", "2EF190", "27 01", "3101", "1001", "1101", "3400"):
            with self.assertRaises(OBDError):
                await elm.module_request("7D1", "7D9", request)
        self.assertEqual(transport.sent, [])

    async def test_scan_finds_modules_codes_and_refusals(self):
        transport = AddressedTransport({
            "7D1:1902AF": ["7D9 07 59 02 FF C1 23 01 09"],
            "7E0:1902AF": ["7E8 03 59 02 FF"],
            "770:1902AF": ["778 03 7F 19 11"],
            "770:1800FF00": ["778 03 7F 18 11"],
            "7C6:1902AF": ["7CE 03 7F 19 11"],
            "7C6:1800FF00": ["7CE 05 58 01 C1 55 60"],
        })
        modules = await car_modules.scan_modules(ELM(transport))
        by_hdr = {m["hdr"]: m for m in modules}
        self.assertEqual(sorted(by_hdr), ["770", "7C6", "7D1", "7E0"])
        self.assertEqual(by_hdr["7D1"]["codes"], [{"code": "U0123-01", "state": "active"}])
        self.assertFalse(by_hdr["770"]["read_codes"])
        self.assertEqual(by_hdr["7C6"]["codes"], [{"code": "U0155", "state": "active"}])
        self.assertEqual(by_hdr["7E0"]["codes"], [])
        self.assertEqual(transport.sent[-1], "ATSH7DF")
        report = car_modules.format_scan(modules, obd_tool.describe_dtc)
        self.assertIn("U0123-01", report)
        self.assertIn("refused", report)

    async def test_read_profile_uses_flow_control_and_restores(self):
        profile = {"commands": [profile_command("22", "B002", "7C6"), profile_command("22", "4000", "7D1")]}
        transport = AddressedTransport({
            "7C6:22B002": ["7CE 10 0F 62 B0 02 E0 00 00", "7CE 21 00 37 B2 00 26 69 00", "7CE 22 00 00 AA AA AA AA AA"],
        })
        values, answered, tried = await car_modules.read_profile(ELM(transport), profile)
        self.assertEqual(answered, 1)
        self.assertEqual(tried, 2)
        self.assertIn("ATFCSM1", transport.sent)
        self.assertIn({"module": "7C6", "group": "Fuel", "name": "Fuel level, volume", "text": "27.5 L"},
                      [{**v, "group": "Fuel"} for v in values])
        self.assertEqual(transport.sent[-1], "ATSH7DF")

    async def test_silent_module_is_skipped_after_three_tries(self):
        commands = [{"hdr": "7D1", "rax": "7D9", "cmd": {"22": f"40{i:02X}"}, "signals": []} for i in range(10)]
        transport = AddressedTransport({})
        await car_modules.read_profile(ELM(transport), {"commands": commands})
        self.assertEqual(sum(1 for c in transport.sent if c.startswith("2240")), car_modules.MAX_SILENT)

    async def test_low_battery_blocks_long_reads(self):
        transport = AddressedTransport({"ATRV": ["11.4V"]})
        transport.send = FakeTransport({"ATRV": ["11.4V"]}).send
        with self.assertRaisesRegex(OBDError, "11.4"):
            await obd_tool.run_scan(ELM(transport), None)

    def test_repeated_single_frame_keeps_final_answer(self):
        reply = parse_can(["7D9 03 7F 22 78", "7D9 05 62 40 00 0C 0B"])["7D9"]
        self.assertEqual(reply, bytes.fromhex("6240000C0B"))


class RecordedScanTests(unittest.TestCase):
    """Replies recorded from a real car's engine module."""

    LINES = ["7E8 10 47 59 02 FF 04 55 00", "7E8 21 60 D1 12 00 20 D1 12", "7E8 22 00 20 04 55 00 60 AA"]

    def test_history_codes_are_labelled_and_not_repeated(self):
        reply = parse_can(self.LINES)["7E8"][:19]
        codes = car_modules.decode_uds_codes(reply)
        self.assertEqual(codes, [{"code": "P0455-00", "state": "history"}, {"code": "U1112-00", "state": "history"}])
        report = car_modules.format_scan([{"hdr": "7E0", "name": "Engine", "read_codes": True, "codes": codes}],
                                         obd_tool.describe_dtc)
        self.assertIn("history", report)
        self.assertIn("since codes were last cleared", report)

    def test_most_serious_state_wins(self):
        payload = bytes.fromhex("5902FF" "04550020" "04550009" "01710004" "03000008")
        states = {c["code"]: c["state"] for c in car_modules.decode_uds_codes(payload)}
        self.assertEqual(states, {"P0455-00": "active", "P0171-00": "pending", "P0300-00": "stored"})


class BatteryVoltageTests(unittest.IsolatedAsyncioTestCase):
    async def test_module_voltage_is_preferred_over_adapter(self):
        transport = FakeTransport({"ATZ": ["ELM327"], "ATRV": ["15.2V"], "0100": ["7E8 06 41 00 80 00 00 00"],
                                   "0142": ["7E8 04 41 42 37 6E"], "010C": ["7E8 04 41 0C 0D 48"]})
        with mock.patch("asyncio.sleep", mock.AsyncMock()), mock.patch("builtins.print") as printed:
            await obd_tool.run_battery(ELM(transport), None)
        output = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("Normal for a running engine", output)
        self.assertNotIn("overcharging", output)

    async def test_low_module_voltage_blocks_long_reads(self):
        transport = FakeTransport({"ATRV": ["12.4V"], "0142": ["7E8 04 41 42 2C 88"]})
        with self.assertRaisesRegex(OBDError, "11.4"):
            await obd_tool.check_battery(ELM(transport))


class SavedCarsTests(unittest.IsolatedAsyncioTestCase):
    VEHICLE = {"VIN": {"7E8": "TESTVIN0000000001"}, "Calibration ID": {"7E8": "CAL-1"}, "Module name": {"7E8": "ECM"}}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(garage, "CARS_DIR", Path(folder.name) / "cars")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_connection_is_remembered_and_counted(self):
        garage.remember_connection(self.VEHICLE, "ELM327", "CAN")
        record = garage.remember_connection(self.VEHICLE, "ELM327", "CAN")
        self.assertEqual(record["connections"], 2)
        self.assertEqual(garage.summaries()[0]["vin"], "TESTVIN0000000001")
        self.assertEqual(garage.summaries()[0]["modules"], ["ECM"])

    def test_two_cars_get_separate_records(self):
        garage.remember_connection(self.VEHICLE, "ELM327", "CAN")
        garage.remember_connection({"VIN": {"7E8": "TESTVIN0000000002"}}, "ELM327", "CAN")
        self.assertEqual(len(garage.summaries()), 2)

    def test_key_handles_missing_vin_and_unsafe_text(self):
        self.assertEqual(garage.car_key({"VIN": {}, "Calibration ID": {"7E8": "AB/../C"}}), "AB____C")
        self.assertEqual(garage.car_key({}), "unknown-car")
        self.assertEqual(garage.car_key({"VIN": {"7E8": "../../evil"}}), "______evil")

    def test_history_is_capped(self):
        with mock.patch.object(garage, "MAX_HISTORY", 3):
            for _ in range(5):
                garage.remember(self.VEHICLE, "status", {"light": False, "active_codes": []})
        self.assertEqual(len(garage.load_car(garage.car_key(self.VEHICLE))["history"]), 3)

    def test_damaged_file_is_kept_and_replaced(self):
        garage.CARS_DIR.mkdir()
        (garage.CARS_DIR / "TESTVIN0000000001.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(garage.remember_connection(self.VEHICLE, "ELM327", "CAN")["connections"], 1)
        self.assertTrue((garage.CARS_DIR / "TESTVIN0000000001.damaged").exists())

    async def test_snapshot_is_saved_with_codes_and_values(self):
        elm = ELM(demo.DemoTransport())
        await elm.setup()
        elm.vehicle = self.VEHICLE
        obd_tool.remember_snapshot(elm, await obd_tool.read_snapshot(elm))
        record = garage.load_car(garage.car_key(self.VEHICLE))
        self.assertEqual(record["status"]["active_codes"], ["P0171"])
        self.assertTrue(record["status"]["light"])
        self.assertIn("Coolant temp", record["status"]["values"])
        self.assertEqual(len(record["history"]), 1)

    def test_demo_never_lists_real_cars(self):
        garage.remember_connection(self.VEHICLE, "ELM327", "CAN")
        args = SimpleNamespace(demo=True, port=0, no_browser=True, samples=1, interval=0)
        self.assertEqual(obd_tool.DashboardApp(args).state()["cars"], [])
        args.demo = False
        self.assertEqual(len(obd_tool.DashboardApp(args).state()["cars"]), 1)

    async def test_demo_session_saves_nothing(self):
        args = SimpleNamespace(demo=True, port=0, no_browser=True, samples=1, interval=0)
        with mock.patch("builtins.print"):
            await obd_tool.DashboardApp(args).run()
        self.assertEqual(garage.summaries(), [])


class LocalizationTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_translatable_string_has_arabic(self):
        missing = set()
        for name in ("obd_tool.py", "car_modules.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            found = re.findall(r'\b(?:t|OBDError)\(\s*"((?:[^"\\]|\\.)+)"', source)
            found += re.findall(r"\bt\(\s*'([^']+)'", source)
            found = [text.encode().decode("unicode_escape").encode("latin-1").decode() for text in found]
            missing |= {text for text in found if text not in i18n.ARABIC}
        self.assertEqual(missing, set())

    def test_page_lists_only_real_commands(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        listed = set(re.findall(r'\.\\\\obd (\w+)', page)) | set(re.findall(r'<code dir="ltr">\.\\obd (\w+)', page))
        real = set(re.findall(r'add_parser\("(\w+)"', (ROOT / "obd_tool.py").read_text(encoding="utf-8")))
        real |= {"live", "log"}
        self.assertEqual(listed - real, set())
        for action in obd_tool.WEB_ACTIONS:
            self.assertIn(f'id: "{action}"', page)

    def test_every_page_string_has_arabic(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        found = {html.unescape(text) for text in re.findall(r'data-i18n(?:-placeholder)?="([^"]+)"', page)}
        found |= {text for text in re.findall(r'\b(?:title|what|needs): "([^"]+)"', page)}
        found |= set(re.findall(r'\b(?:overview|live|faults|commands|cars|saved|lookup|reports|queued|running|done|failed|high|medium|low|mil|emissions_relevant|drive_cycle_required|limp_mode_possible): "([^"]+)"', page))
        found |= {text.replace("\\\\", "\\") for text in re.findall(r'\btr\("((?:[^"\\]|\\.)+)"', page)}
        for pair in re.findall(r'\btr\(\w+(?:\.\w+)? \? "([^"]+)" : "([^"]+)"\)', page):
            found |= set(pair)
        self.assertGreater(len(found), 20)
        self.assertEqual({text for text in found if text not in i18n.ARABIC}, set())

    def test_every_live_name_monitor_and_value_has_arabic(self):
        names = [v[0] for v in LIVE_PIDS.values()] + [v[1] for v in LIVE_PIDS.values() if v[1] and v[1] != "%"]
        names += obd_tool.COMMON_MONITORS + obd_tool.SPARK_MONITORS + [m for m in obd_tool.DIESEL_MONITORS if m]
        names += list(obd_tool.FUEL_STATUS.values()) + list(obd_tool.FUEL_TYPES.values())
        names += list(car_modules.LIKELY_MODULES.values()) + ["Unknown module", "active", "stored", "pending", "history"]
        names += ["Stored", "Pending", "Permanent", "VIN", "Calibration ID", "Calibration check", "Module name"]
        self.assertEqual({n for n in names if n not in i18n.ARABIC}, set())

    def test_placeholders_match_between_languages(self):
        for english, arabic in i18n.ARABIC.items():
            self.assertEqual(set(re.findall(r"{(\w+)}", english)), set(re.findall(r"{(\w+)}", arabic)), english)

    def test_language_switch_and_unknown_language(self):
        self.assertEqual(i18n.t("Speed"), "Speed")
        self.assertEqual(i18n.t("Speed", "ar"), "السرعة")
        self.assertEqual(i18n.t("Speed", "fr"), "Speed")
        i18n.set_language("ar")
        self.assertEqual(i18n.t("Speed"), "السرعة")
        self.assertEqual(i18n.t("Connected, updated {time}", time="10:00"), "متصل، آخر تحديث 10:00")
        self.assertEqual(i18n.t("No translation here"), "No translation here")

    def test_fault_codes_in_both_languages(self):
        self.assertEqual(obd_tool.describe_dtc("P0171", "en"), "Fuel mixture too lean (bank 1)")
        self.assertIn("خليط الوقود", obd_tool.describe_dtc("P0171", "ar"))
        self.assertIn("Wheel Speed", obd_tool.describe_dtc("C0035", "ar"))
        self.assertIn("الشركة المصنّعة", obd_tool.describe_dtc("U1112", "ar"))
        self.assertEqual(set(dtc_codes.DTC_DESCRIPTIONS), set(dtc_codes.DTC_DESCRIPTIONS_AR))

    def test_language_choice_is_saved(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(i18n, "SETTINGS_FILE", Path(folder) / "s.json"):
            self.assertEqual(i18n.saved_language(), "en")
            i18n.save_language("ar")
            self.assertEqual(i18n.saved_language(), "ar")
            i18n.SETTINGS_FILE.write_text("not json", encoding="utf-8")
            self.assertEqual(i18n.saved_language(), "en")

    def test_language_argument_forms(self):
        self.assertEqual(obd_tool.language_from_arguments(["--lang", "ar", "cars"]), "ar")
        self.assertEqual(obd_tool.language_from_arguments(["--lang=en", "cars"]), "en")
        self.assertIsNone(obd_tool.language_from_arguments(["cars"]))

    def test_report_in_arabic(self):
        async def report():
            elm = ELM(demo.DemoTransport())
            await elm.setup()
            return obd_tool.format_report(await obd_tool.read_snapshot(elm))

        i18n.set_language("ar")
        text = asyncio.run(report())
        for expected in ("تقرير السيارة", "خليط الوقود", "حرارة سائل التبريد", "لمبة فحص المحرك"):
            self.assertIn(expected, text)


class MissedReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_asking_until_known_modules_answer(self):
        replies = [["7E8 02 43 00"]] * 4 + [["7E8 02 43 00", "7E9 04 43 01 07 41"]] + [["7E8 02 43 00"]]
        transport = FakeTransport({"03": Sequence(replies)})
        elm = ELM(transport)
        elm.vehicle = {"Module name": {"7E8": "ECM", "7E9": "TCM"}}
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            result = await elm.request_all(0x03)
        self.assertEqual(set(result), {"7E8", "7E9"})
        self.assertEqual(obd_tool.decode_dtcs(result["7E9"]), ["P0741"])
        self.assertEqual(transport.sent.count("03"), 5)

    def test_silent_module_is_reported_not_hidden(self):
        codes = {"Stored": {"7E8": []}, "Permanent": {}}
        lines = obd_tool.code_lines(codes, known=["7E8", "7E9"])
        self.assertEqual(sum("7E9 didn't answer" in line for line in lines), 1)
        self.assertFalse(any("7E8 didn't answer" in line for line in lines))

    async def test_stops_at_the_limit_when_a_module_never_answers(self):
        transport = FakeTransport({"03": ["7E8 02 43 00"]})
        elm = ELM(transport)
        elm.vehicle = {"Module name": {"7E8": "ECM", "7E9": "TCM"}}
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            result = await elm.request_all(0x03)
        self.assertEqual(set(result), {"7E8"})
        self.assertEqual(transport.sent.count("03"), 6)

    async def test_module_that_misses_a_request_is_still_read(self):
        transport = FakeTransport({"03": Sequence([
            ["7E8 02 43 00"],
            ["7E8 02 43 00", "7E9 04 43 01 07 00"],
            ["7E8 02 43 00"],
        ]), "07": ["NO DATA"], "0A": ["NO DATA"]})
        codes = await obd_tool.read_codes(ELM(transport))
        self.assertEqual(codes["Stored"], {"7E8": [], "7E9": ["P0700"]})


class DashboardLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_and_server_follow_requested_language(self):
        elm = ELM(demo.DemoTransport())
        await elm.setup()
        snapshot = await obd_tool.read_snapshot(elm)
        reader = obd_tool.LiveReader(elm, snapshot["values"], snapshot["values"], snapshot["voltage"])

        english = obd_tool.dashboard_state(snapshot, reader, lang="en")
        arabic = obd_tool.dashboard_state(snapshot, reader, lang="ar")
        self.assertIn("Stored", english["codes"])
        self.assertIn("مخزّنة", arabic["codes"])
        self.assertIn("السرعة", [v["name"] for v in arabic["values"]])
        self.assertIn("خليط", arabic["freeze"]["description"])


class RecordedDieselTests(unittest.TestCase):
    """Replies recorded from a real diesel car."""

    def test_cut_off_reply_is_detected(self):
        reply = parse_can(["7E8 11 67 59 02 FF 25 04 00"])["7E8"]
        self.assertFalse(reply.complete)
        self.assertEqual(car_modules.decode_uds_codes(reply), [])
        report = car_modules.format_scan([{"hdr": "7E0", "name": "Engine", "read_codes": True, "codes": [],
                                           "complete": False}], obd_tool.describe_dtc)
        self.assertIn("cut off", report)
        self.assertNotIn("No fault codes", report)

    def test_complete_reply_with_confirmed_codes(self):
        reply = parse_can(["7E8 10 0B 59 02 FF 20 03 00", "7E8 21 EC 07 00 00 AF 55 55"])["7E8"]
        self.assertTrue(reply.complete)
        self.assertEqual(car_modules.decode_uds_codes(reply),
                         [{"code": "P2003-00", "state": "stored"}, {"code": "P0700-00", "state": "active"}])

    def test_diesel_values(self):
        self.assertEqual(LIVE_PIDS[0x59][2](bytes.fromhex("0B68")), 29200)
        self.assertAlmostEqual(LIVE_PIDS[0x5D][2](bytes.fromhex("6981")), 1.0, places=0)
        self.assertEqual(LIVE_PIDS[0x61][2](bytes.fromhex("77")), -6)
        self.assertEqual(LIVE_PIDS[0x62][2](bytes.fromhex("80")), 3)
        self.assertEqual(LIVE_PIDS[0x63][2](bytes.fromhex("01B8")), 440)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        return obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))

    async def test_state_and_translations(self):
        reply = await bridge.answer(self.app(), '{"id": 7, "kind": "state", "payload": {"lang": "ar"}}')
        self.assertEqual((reply["id"], reply["status"]), (7, 200))
        self.assertFalse(reply["data"]["connected"])
        self.assertIn("البحث", reply["data"]["message"])
        reply = await bridge.answer(self.app(), '{"id": 8, "kind": "i18n", "payload": {"lang": "ar"}}')
        self.assertEqual(reply["data"]["text"]["Speed"], "السرعة")
        reply = await bridge.answer(self.app(), '{"id": 9, "kind": "i18n", "payload": {"lang": "xx"}}')
        self.assertEqual(reply["data"]["text"], {})

    async def test_api_requests_reach_the_app(self):
        line = json.dumps({"id": 1, "kind": "api", "payload": {"method": "GET", "path": "/api/catalog"}})
        reply = await bridge.answer(self.app(), line)
        self.assertEqual(reply["status"], 200)
        self.assertGreater(len(reply["data"]), 800)
        line = json.dumps({"id": 2, "kind": "api", "payload": {"method": "POST", "path": "/api/run",
                                                               "body": {"action": "exec"}}})
        self.assertEqual((await bridge.answer(self.app(), line))["status"], 400)

    async def test_bad_requests_are_refused(self):
        for line in ("not json", "[1, 2]", "{}", '{"id": 1}', '{"id": 1, "kind": "state", "payload": 5}'):
            self.assertEqual((await bridge.answer(self.app(), line))["status"], 400)
        reply = await bridge.answer(self.app(), '{"id": 1, "kind": "shell"}')
        self.assertEqual(reply["status"], 404)

    async def test_serve_answers_and_stops_when_pipe_closes(self):
        replies = io.BytesIO()
        requests = io.BytesIO(b'{"id": 1, "kind": "i18n", "payload": {"lang": "en"}}\n')
        app = self.app()
        app.RETRY_SECONDS = 0
        await asyncio.wait_for(bridge.serve(app, requests, replies), 20)
        self.assertEqual(json.loads(replies.getvalue())["id"], 1)

    def test_readme_opens_with_basmala_then_arabic_then_english(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        basmala = readme.index("بسم الله الرحمن الرحيم")
        salawat = readme.index("والصلاة والسلام على نبينا محمد")
        arabic = readme.index("## العربية")
        english = readme.index("## English")
        self.assertLess(readme.index("<picture>"), 60)
        self.assertLess(basmala, readme.index("</picture>"))
        self.assertLess(salawat, readme.index("</picture>"))
        self.assertLess(readme.index("</picture>"), readme.index("electron/icon.svg"))
        self.assertTrue((ROOT / "docs" / "opening-dark.png").exists())
        self.assertTrue((ROOT / "docs" / "opening-light.png").exists())
        self.assertEqual(readme.count("<h1>"), 0)
        self.assertLess(basmala, salawat)
        self.assertLess(salawat, arabic)
        self.assertLess(arabic, english)

    def test_icon_file_has_every_windows_size(self):
        data = (ROOT / "electron" / "icon.ico").read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")
        count = int.from_bytes(data[4:6], "little")
        sizes = {data[6 + 16 * i] or 256 for i in range(count)}
        self.assertEqual(sizes, {16, 24, 32, 48, 64, 128, 256})
        for i in range(count):
            offset = int.from_bytes(data[6 + 16 * i + 12:6 + 16 * i + 16], "little")
            self.assertEqual(data[offset:offset + 4], b"\x89PNG")

    def test_page_only_loads_its_own_content(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("fetch(", page)
        main_file = (ROOT / "electron" / "main.js").read_text(encoding="utf-8")
        self.assertIn("contextIsolation: true", main_file)
        self.assertIn("nodeIntegration: false", main_file)
        self.assertIn("sandbox: true", main_file)
        self.assertNotIn("createServer", main_file)


class SpyTransport(demo.DemoTransport):
    """The demo car, recording every command it receives."""

    def __init__(self):
        self.sent = []

    async def send(self, command, wait=5):
        self.sent.append(command)
        return await super().send(command, wait)


class WebJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.args = SimpleNamespace(demo=True, port=0, no_browser=True, samples=1, interval=0)
        self.app = obd_tool.DashboardApp(self.args)
        self.transport = SpyTransport()
        self.elm = ELM(self.transport)
        self.elm.demo = True
        await self.elm.setup()
        snapshot = await obd_tool.read_snapshot(self.elm)
        self.app.snapshot = snapshot
        self.app.reader = obd_tool.LiveReader(self.elm, snapshot["values"], snapshot["values"], snapshot["voltage"])
        self.app.elm = self.elm

    async def run_one(self, action, params=None, lang="en"):
        job, refusal = self.app.submit(action, params or {}, lang)
        self.assertIsNone(refusal)
        await self.app.run_waiting_jobs(self.elm)
        return job

    async def test_command_runs_and_shows_result(self):
        job = await self.run_one("codes")
        self.assertEqual(job.status, "done")
        self.assertIn("P0171", job.output)
        self.assertEqual(self.app.jobs[-1], job)

    async def test_result_uses_requested_language_and_restores(self):
        job = await self.run_one("codes", lang="ar")
        self.assertIn("خليط الوقود", job.output)
        self.assertEqual(i18n.current_language(), "en")

    async def test_refused_when_not_connected(self):
        self.app.elm = None
        job, refusal = self.app.submit("codes", {}, "en")
        self.assertIsNone(job)
        self.assertIn("Not connected", refusal)

    async def test_unknown_command_refused(self):
        for action in ("exec", "live", None, "../clear"):
            job, refusal = self.app.submit(action, {}, "en")
            self.assertIsNone(job)
            self.assertTrue(refusal)

    async def test_clear_needs_typed_confirmation(self):
        for params in ({}, {"answer": "clear"}, {"answer": "yes"}):
            job, refusal = self.app.submit("clear", params, "en")
            self.assertIsNone(job)
            self.assertIn("CLEAR", refusal)
        self.assertNotIn("04", self.transport.sent)

    async def test_clear_refused_with_engine_running(self):
        job = await self.run_one("clear", {"answer": "CLEAR"})
        self.assertEqual(job.status, "failed")
        self.assertIn("engine must be off", job.output)
        self.assertNotIn("04", self.transport.sent)

    async def test_profile_name_must_be_in_catalog(self):
        for name in ("../../secrets", "not-a-car", ""):
            job, refusal = self.app.submit("profile", {"name": name}, "en")
            self.assertIsNone(job)
            self.assertTrue(refusal)

    async def test_profile_year_is_validated(self):
        for year in ("abc", "1900", "-1"):
            job, refusal = self.app.submit("profile", {"name": "Kia-Picanto", "year": year}, "en")
            self.assertIsNone(job)
            self.assertIn("year", refusal)

    async def test_profile_runs_on_page(self):
        job = await self.run_one("profile", {"name": "Kia-Picanto", "year": "2017"})
        self.assertEqual(job.status, "done")
        self.assertIn("Profile requests answered", job.output)
        self.assertEqual(self.transport.sent[-1], "ATSH7DF")

    async def test_scan_restores_standard_requests_and_saves_nothing_in_demo(self):
        job = await self.run_one("scan")
        self.assertEqual(job.status, "done")
        self.assertEqual(self.transport.sent[-1], "ATSH7DF")

    async def test_waiting_jobs_fail_when_connection_ends(self):
        job, _ = self.app.submit("codes", {}, "en")
        self.app.fail_waiting_jobs()
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.app.waiting, [])

    async def test_queue_is_limited(self):
        for _ in range(obd_tool.MAX_JOBS_WAITING):
            self.app.submit("codes", {}, "en")
        job, refusal = self.app.submit("codes", {}, "en")
        self.assertIsNone(job)
        self.assertTrue(refusal)

    async def test_api_catalog_and_report_paths(self):
        code, cars = await self.app.api("GET", "/api/catalog", {}, {})
        self.assertEqual(code, 200)
        self.assertGreater(len(cars), 800)
        self.assertTrue(next(c for c in cars if c["name"] == "Kia-Picanto")["local"])
        for name in ("../LICENSE", "..\\LICENSE", "a/b.txt", "x.json", ""):
            code, _ = await self.app.api("GET", "/api/report", {"name": name}, {})
            self.assertEqual(code, 404)
        code, _ = await self.app.api("GET", "/api/car", {"key": "../settings"}, {})
        self.assertEqual(code, 404)

    async def test_demo_hides_and_saves_no_reports(self):
        self.assertEqual(await self.app.api("GET", "/api/reports", {}, {}), (200, []))
        with mock.patch.object(obd_tool, "REPORT_DIR", ROOT / "does-not-exist"):
            job = await self.run_one("report")
            self.assertEqual(job.status, "done")
            self.assertFalse(obd_tool.REPORT_DIR.exists())

    async def test_download_only_accepts_catalog_names(self):
        code, data = await self.app.api("POST", "/api/download", {}, {"name": "../../secrets"})
        self.assertEqual(code, 400)
        self.assertIn("error", data)


class StandardValueTests(unittest.TestCase):
    """Replies recorded from a real diesel car."""

    def names(self, pid, hexed):
        return {item["name"]: item["text"] for item in car_modules.decode_standard(pid, bytes.fromhex(hexed))}

    def test_values_match_tested_decoders(self):
        self.assertEqual(self.names(0x0C, "0D48")["Engine RPM"], "850 rpm")
        self.assertEqual(self.names(0x05, "78")["Engine coolant temperature"], "80 C")

    def test_flagged_as_unsupported_is_hidden(self):
        self.assertEqual(self.names(0x75, "005A01900624"), {})

    def test_unknown_pid_gives_nothing(self):
        self.assertEqual(car_modules.decode_standard(0xFE, b"\x00"), [])

    def test_tested_decoders_stay_first(self):
        # OBDb's definition of this PID gave -210 on a reply that really means about 1.
        self.assertAlmostEqual(LIVE_PIDS[0x5D][2](bytes.fromhex("6981")), 1.0, places=0)
        self.assertIn(0x5D, LIVE_PIDS)


class FaultDetailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))

    def test_details_match_known_codes(self):
        details = dtc_codes.dtc_details("P0741")
        self.assertIn("converter clutch", details["description"])
        self.assertEqual(details["causes"][0]["likelihood"], "high")
        self.assertTrue(details["flags"]["mil"])
        self.assertIn("Catalyst", dtc_codes.generic_codes()["P0420"])
        self.assertIsNone(dtc_codes.dtc_details("P1234"))

    async def test_lookup_api(self):
        code, data = await self.app.api("GET", "/api/dtc", {"code": " p0420 ", "lang": "ar"}, {})
        self.assertEqual((code, data["code"], data["found"]), (200, "P0420", True))
        self.assertIn("المحفّز", data["title"])
        code, data = await self.app.api("GET", "/api/dtc", {"code": "U1112"}, {})
        self.assertEqual((code, data["found"]), (200, False))
        self.assertIn("manufacturer", data["title"])

    async def test_lookup_refuses_bad_input(self):
        for bad in ("", "P04", "X0420", "P04200", "../x", "P9420"):
            code, data = await self.app.api("GET", "/api/dtc", {"code": bad}, {})
            self.assertEqual(code, 400, bad)
            self.assertIn("P0420", data["error"])


class LongSessionTests(unittest.IsolatedAsyncioTestCase):
    """Problems found in a real logged session."""

    async def asyncSetUp(self):
        self.app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))
        self.transport = FakeTransport({"0101": ["7E8 06 41 01 00 07 E5 00", "7E9 06 41 01 00 04 00 00"],
                                        "03": ["7E8 02 43 00", "7E9 02 43 00"], "07": ["7E8 02 47 00"], "0A": ["7E8 02 4A 00"],
                                        "ATZ": ["ELM327"]})
        self.elm = ELM(self.transport)
        self.elm.demo = True
        self.app.snapshot = {"monitors": {"7E8": {"mil": False, "count": 0, "tests": []}}, "supported": set(), "this_drive": None,
                             "codes": {"Stored": {"7E8": [], "7E9": []}, "Pending": {}, "Permanent": {}}, "freeze": None,
                             "voltage": "14V", "vehicle": {}, "values": {}, "raw": {}, "more": []}

    async def test_new_fault_is_noticed_during_a_session(self):
        self.transport.replies["0101"] = ["7E8 06 41 01 81 07 E5 00"]
        self.transport.replies["03"] = ["7E8 04 43 01 01 71", "7E9 02 43 00"]
        self.transport.replies["020200"] = ["7E8 05 42 02 00 01 71"]
        await self.app.refresh_status(self.elm)
        self.assertTrue(self.app.snapshot["monitors"]["7E8"]["mil"])
        self.assertEqual(self.app.snapshot["codes"]["Stored"]["7E8"], ["P0171"])
        self.assertEqual(self.app.snapshot["freeze"]["code"], "P0171")
        self.assertIn("check engine", obd_tool.car_alerts(self.app.snapshot, {}, "en")[0])

    async def test_silent_module_keeps_its_last_answer(self):
        self.app.snapshot["codes"]["Stored"]["7E9"] = ["P0741"]
        self.transport.replies["03"] = ["7E8 02 43 00"]
        await self.app.refresh_status(self.elm)
        self.assertEqual(self.app.snapshot["codes"]["Stored"]["7E9"], ["P0741"])

    async def test_module_missed_once_is_still_found(self):
        self.transport.replies["090A"] = Sequence([["7E8 05 49 0A 01 45 43"], ["7E8 05 49 0A 01 45 43", "7E9 05 49 0A 01 54 43"],
                                                   ["7E8 05 49 0A 01 45 43"]])
        vehicle = await obd_tool.read_identity(ELM(self.transport))
        self.assertEqual(obd_tool.known_modules(vehicle), ["7E8", "7E9"])

    def test_short_voltage_dip_gives_no_alert(self):
        values = {0x42: 12.0, 0x0C: 900.0}
        self.app.reader = SimpleNamespace(values=values, voltage="12V")
        self.app.track_charging()
        self.assertEqual(obd_tool.car_alerts({"monitors": {}}, values, "en", charging_lasting=False), [])
        self.app.charging_since = obd_tool.time.monotonic() - 61
        self.assertLess(self.app.charging_since + obd_tool.CHARGING_ALERT_SECONDS, obd_tool.time.monotonic())
        self.assertIn("alternator", obd_tool.car_alerts({"monitors": {}}, values, "en", charging_lasting=True)[0])
        self.app.reader.values = {0x42: 14.1, 0x0C: 900.0}
        self.app.track_charging()
        self.assertIsNone(self.app.charging_since)

    def test_real_dip_from_the_log_is_not_a_fault(self):
        self.assertFalse(obd_tool.charging_out_of_range({0x42: 13.0, 0x0C: 900.0}))
        self.assertNotIn("not be charging", obd_tool.battery_advice(13.0, True))

    async def test_this_drive_follows_the_standard_meaning(self):
        self.transport.replies["0141"] = ["7E8 06 41 41 00 07 21 01"]
        tests = dict(await obd_tool.read_this_drive(ELM(self.transport)))
        self.assertTrue(tests["Misfire"])
        self.assertFalse(tests["Catalyst"])
        self.assertTrue(tests["Oxygen sensor"])


class AlertTests(unittest.TestCase):
    QUIET = {"monitors": {"7E8": {"mil": False}}}

    def alerts(self, snapshot=None, **values):
        pids = {"coolant": 0x05, "volts": 0x42, "rpm": 0x0C}
        return obd_tool.car_alerts(snapshot or self.QUIET, {pids[k]: v for k, v in values.items()}, "en")

    def test_normal_readings_are_quiet(self):
        self.assertEqual(self.alerts(coolant=90, volts=14.1, rpm=850), [])
        self.assertEqual(self.alerts(coolant=90, volts=12.4, rpm=0), [])
        self.assertEqual(self.alerts(), [])

    def test_each_warning(self):
        self.assertIn("overheating", self.alerts(coolant=112)[0])
        self.assertIn("alternator", self.alerts(volts=12.5, rpm=800)[0])
        self.assertIn("overcharging", self.alerts(volts=15.9, rpm=800)[0])
        self.assertIn("battery is low", self.alerts(volts=11.4, rpm=0)[0])
        self.assertIn("check engine", self.alerts({"monitors": {"7E8": {"mil": True}}})[0])

    def test_invalid_values_are_ignored(self):
        self.assertEqual(self.alerts(coolant="255+", volts=None, rpm=None), [])

    def test_arabic(self):
        self.assertIn("الدينامو", obd_tool.car_alerts(self.QUIET, {0x42: 12.5, 0x0C: 800}, "ar")[0])


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(obd_tool, "LOG_DIR", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))
        self.elm = ELM(demo.DemoTransport())
        self.elm.demo = True
        await self.elm.setup()
        values = {0x0C: 2000.0, 0x0D: 50, 0x05: 90}
        self.app.reader = obd_tool.LiveReader(self.elm, values, values, "14.0V")
        self.app.elm = self.elm

    def test_records_and_summarizes(self):
        self.assertIsNone(self.app.start_recording("en"))
        self.app.record_row()
        self.app.reader.values.update({0x0D: 70, 0x0C: 3000.0})
        self.app.record_row()
        self.assertEqual(self.app.recording_status("en")["on"], True)
        self.assertIn("2 rows", self.app.recording_status("en")["text"])
        path = self.app.recording["path"]
        self.app.stop_recording()
        rows = path.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(len(rows), 3)
        lines = "\n".join(self.app.recording_status("en")["lines"])
        self.assertIn("Top speed: 70 km/h", lines)
        self.assertIn("Average speed: 60 km/h", lines)
        self.assertIn("Highest engine RPM: 3000 rpm", lines)

    def test_column_titles(self):
        self.assertEqual(obd_tool.column_title(0x0C, "en"), "Engine RPM (rpm)")
        self.assertEqual(obd_tool.column_title(0x03, "en"), "Fuel system")

    def test_cannot_record_twice_or_without_a_car(self):
        self.assertIsNone(self.app.start_recording("en"))
        self.assertIn("already", self.app.start_recording("en"))
        self.app.stop_recording()
        self.app.elm = None
        self.assertIn("Not connected", self.app.start_recording("en"))

    async def test_connection_loss_closes_the_file(self):
        self.assertIsNone(self.app.start_recording("en"))
        recording = self.app.recording
        self.app.stop_recording()
        self.assertTrue(recording["file"].closed)
        self.app.stop_recording()  # safe to call again

    async def test_api(self):
        code, data = await self.app.api("POST", "/api/record", {}, {"on": True, "lang": "en"})
        self.assertEqual((code, data["on"]), (200, True))
        code, data = await self.app.api("POST", "/api/record", {}, {"on": True, "lang": "en"})
        self.assertEqual(code, 400)
        code, data = await self.app.api("POST", "/api/record", {}, {"on": False, "lang": "en"})
        self.assertEqual((code, data["on"]), (200, False))


class BluetoothOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_message_when_bluetooth_is_off(self):
        from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason
        error = BleakBluetoothNotAvailableError("off", BleakBluetoothNotAvailableReason.POWERED_OFF)
        args = SimpleNamespace(address=None)
        with mock.patch("bleak.BleakScanner.find_device_by_filter", mock.AsyncMock(side_effect=error)), \
                mock.patch("builtins.print"), self.assertRaisesRegex(OBDError, "Bluetooth is turned off"):
            await obd_tool.connect_and_run(args, obd_tool.run_info)


class AdapterStatusTests(unittest.IsolatedAsyncioTestCase):
    def rows(self, app, lang="en"):
        return {row["label"]: row for row in app.device_status(lang)}

    def test_looking_for_adapter(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))
        rows = self.rows(app)
        self.assertEqual(rows["Status"]["tone"], "bad")
        self.assertIn("Looking", rows["Status"]["text"])
        self.assertEqual(app.state("en")["device"], app.device_status("en"))

    async def test_connected_adapter_details_and_counts(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))
        transport = FakeTransport({"ATZ": ["ATZ", "ELM327 v2.1"], "AT@1": ["OBDII to RS232 Interpreter"],
                                   "ATDPN": ["A6"], "0100": ["7E8 06 41 00 BE 3F B8 13"],
                                   "01FF": Sequence([None, None])})
        elm = ELM(transport)
        elm.device_name = "OBDII"
        elm.device_address = "AA:BB:CC:DD:EE:FF"
        await elm.setup()
        await elm.request(0x01, 0xFF)
        app.link = elm
        app.connected_at = obd_tool.time.monotonic() - 65
        app.connections = 2
        rows = self.rows(app)
        self.assertEqual(rows["Status"]["tone"], "good")
        self.assertEqual(rows["Name"]["text"], "OBDII")
        self.assertEqual(rows["Reported version"]["text"], "ELM327 v2.1")
        self.assertEqual(rows["Reported description"]["text"], "OBDII to RS232 Interpreter")
        self.assertIn("500 kbps", rows["Protocol"]["text"])
        self.assertEqual(rows["Connected for"]["text"], "1:05")
        self.assertEqual(rows["Times connected"]["text"], "2")
        self.assertEqual(rows["Replies lost"]["text"], "2")
        self.assertEqual(rows["Replies lost"]["tone"], "bad")
        self.assertEqual(int(rows["Commands sent"]["text"]), elm.transport.sent)
        arabic = self.rows(app, "ar")
        self.assertEqual(arabic["الحالة"]["text"], "متصل")

    async def test_adapter_that_rejects_description_shows_none(self):
        transport = FakeTransport({"ATZ": ["ELM327 v1.5"], "AT@1": ["?"], "ATDPN": ["A6"],
                                   "0100": ["7E8 06 41 00 BE 3F B8 13"]})
        elm = ELM(transport)
        await elm.setup()
        self.assertIsNone(elm.description)

    async def test_session_records_and_clears_the_connection(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=1, interval=0))
        elm = ELM(demo.DemoTransport())
        elm.demo = True
        await elm.setup()
        await app.session(elm, None)
        self.assertEqual(app.connections, 1)
        self.assertIsNone(app.link)
        self.assertIn("Looking", self.rows(app)["Status"]["text"])
        self.assertEqual(self.rows(app)["Times connected"]["text"], "1")


class DashboardAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_retrying_and_reports_why(self):
        args = SimpleNamespace(demo=False, port=0, no_browser=True, samples=1, interval=0)
        app = obd_tool.DashboardApp(args)
        app.RETRY_SECONDS = 0
        attempts = []

        async def connect():
            attempts.append(app.state())
            if len(attempts) < 3:
                raise OBDError("Adapter not found.")

        app.connect = connect
        with mock.patch("builtins.print"):
            await app.run()
        self.assertEqual(len(attempts), 3)
        self.assertFalse(attempts[0]["connected"])
        self.assertEqual(attempts[0]["message"], "Looking for the adapter ...")
        self.assertEqual(attempts[2]["message"], "Adapter not found.")

    async def test_connected_state_after_session(self):
        args = SimpleNamespace(demo=True, port=0, no_browser=True, samples=1, interval=0)
        app = obd_tool.DashboardApp(args)
        with mock.patch("builtins.print"):
            await app.run()
        self.assertTrue(app.state()["connected"])
        self.assertTrue(app.state()["demo"])

    async def test_page_starts_hidden_until_data_arrives(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        self.assertIn('<div class="page" id="page-overview" hidden>', page)


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_fills_every_dashboard_section(self):
        elm = ELM(demo.DemoTransport())
        await elm.setup()
        snapshot = await obd_tool.read_snapshot(elm)
        self.assertEqual(snapshot["vehicle"]["VIN"]["7E8"], demo.DEMO_VIN)
        self.assertEqual(snapshot["codes"]["Stored"]["7E8"], ["P0171"])
        self.assertEqual(snapshot["freeze"]["code"], "P0171")
        self.assertEqual(sorted(snapshot["monitors"]), ["7E8", "7E9"])
        self.assertEqual(dict(snapshot["this_drive"])["Catalyst"], True)
        self.assertEqual(dict(snapshot["this_drive"])["Evaporative system"], False)
        self.assertNotIn(None, snapshot["values"].values())

        reader = obd_tool.LiveReader(elm, snapshot["values"], snapshot["values"], snapshot["voltage"])
        await reader.step()
        state = obd_tool.dashboard_state(snapshot, reader, demo=True)
        self.assertTrue(state["demo"])
        self.assertIn("lean", state["codes"]["Stored"]["7E8"][0]["description"])


class ClearTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, rpm="00 00"):
        return FakeTransport({
            "010C": [f"7E8 04 41 0C {rpm}"],
            "03": ["7E8 02 43 00"],
            "07": ["7E8 02 47 00"],
            "0A": ["7E8 02 4A 00"],
            "04": ["7E8 01 44"],
        })

    async def test_cancel_sends_nothing(self):
        transport = self.transport()
        with mock.patch("builtins.input", return_value="no"):
            await obd_tool.run_clear(ELM(transport), None)
        self.assertNotIn("04", transport.sent)

    async def test_confirm_sends_clear(self):
        transport = self.transport()
        with mock.patch("builtins.input", return_value="CLEAR"):
            await obd_tool.run_clear(ELM(transport), None)
        self.assertIn("04", transport.sent)

    async def test_refused_with_engine_running(self):
        transport = self.transport(rpm="1A F8")
        with mock.patch("builtins.input", return_value="CLEAR"), self.assertRaises(OBDError):
            await obd_tool.run_clear(ELM(transport), None)
        self.assertNotIn("04", transport.sent)


if __name__ == "__main__":
    unittest.main()
