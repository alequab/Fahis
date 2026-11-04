"""Offline tests using replies recorded from the car, so no adapter is needed."""
import asyncio
import html
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import car_modules
import dashboard
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
        self.assertIn("not running", LIVE_PIDS[0x03][2](bytes([0, 0])))
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

    async def test_server_serves_page_and_data(self):
        server = await dashboard.start(lambda lang: '{"ok": true}', 0)
        port = server.sockets[0].getsockname()[1]
        self.assertEqual(server.sockets[0].getsockname()[0], "127.0.0.1")

        async def get(path):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            reply = await reader.read()
            writer.close()
            return reply

        async with server:
            self.assertIn(b'{"ok": true}', await get("/data"))
            self.assertIn(b"Car Dashboard", await get("/"))
            self.assertIn(b"Commands for a connected car", await get("/commands"))
            self.assertIn(b"404", await get("/secret"))


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
    """Answers by module: replies are keyed like '7D1:1902FF'."""

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
            "7D1:1902FF": ["7D9 07 59 02 FF C1 23 01 09"],
            "7E0:1902FF": ["7E8 03 59 02 FF"],
            "770:1902FF": ["778 03 7F 19 11"],
            "770:1800FF00": ["778 03 7F 18 11"],
            "7C6:1902FF": ["7CE 03 7F 19 11"],
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

    def test_commands_page_is_translated_and_lists_real_commands(self):
        page = dashboard.COMMANDS_PAGE.read_text(encoding="utf-8")
        found = set(re.findall(r'data-i18n="([^"]+)"', page)) | set(re.findall(r'\btr\("([^"]+)"', page))
        self.assertEqual({html.unescape(text).replace("\\\\", "\\") for text in found} - set(i18n.ARABIC), set())
        listed = set(re.findall(r'<code dir="ltr">\.\\obd (\w+)', page))
        real = set(re.findall(r'add_parser\("(\w+)"', (ROOT / "obd_tool.py").read_text(encoding="utf-8")))
        real |= {"live", "log", "dashboard"}
        self.assertTrue(listed)
        self.assertEqual(listed - real, set())
        self.assertEqual(listed, {"dashboard", "report", "battery", "codes", "info", "live", "log", "scan", "profile", "clear"})

    def test_every_page_string_has_arabic(self):
        page = dashboard.PAGE.read_text(encoding="utf-8")
        found = set(re.findall(r'data-i18n="([^"]+)"', page))
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

        server = await dashboard.start(lambda lang: f'{{"lang": "{lang}"}}', 0)
        port = server.sockets[0].getsockname()[1]

        async def get(path):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            reply = await reader.read()
            writer.close()
            return reply

        async with server:
            self.assertIn(b'{"lang": "ar"}', await get("/data?lang=ar"))
            self.assertIn(b'{"lang": "en"}', await get("/data?lang=xx"))
            self.assertIn("السرعة".encode().decode("utf-8").encode("unicode_escape").decode().lower().encode(),
                          (await get("/i18n?lang=ar")).lower())


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
        page = dashboard.PAGE.read_text(encoding="utf-8")
        self.assertIn('<div id="content" hidden>', page)


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_fills_every_dashboard_section(self):
        elm = ELM(demo.DemoTransport())
        await elm.setup()
        snapshot = await obd_tool.read_snapshot(elm)
        self.assertEqual(snapshot["vehicle"]["VIN"]["7E8"], demo.DEMO_VIN)
        self.assertEqual(snapshot["codes"]["Stored"]["7E8"], ["P0171"])
        self.assertEqual(snapshot["freeze"]["code"], "P0171")
        self.assertEqual(sorted(snapshot["monitors"]), ["7E8", "7E9"])
        self.assertIn(0x41, snapshot["raw"])
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
