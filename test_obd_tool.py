"""Offline tests using replies recorded from the car, so no adapter is needed."""
import asyncio
import contextlib
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bridge
import car_analysis
import car_copy
import car_modules
import car_systems
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

    async def test_several_values_in_one_request_when_the_car_allows_it(self):
        transport = FakeTransport({
            # 7E8's reply is cut off after its first frame, so only 7E9's complete one may be used.
            "010C0D": ["7E8 10 07 41 0C 1A F8 0D 32", "7E9 06 41 0C 1B 00 0D 33"],
            "010C": ["7E9 04 41 0C 1B 00"], "010D": ["7E9 03 41 0D 32"]})
        elm = ELM(transport)
        await elm.read_pid(0x0C)
        await elm.read_pid(0x0D)
        values = await elm.read_pids([0x0C, 0x0D])
        self.assertTrue(elm.can_group)
        self.assertEqual(values, {0x0C: 1728.0, 0x0D: 51})
        self.assertEqual(transport.sent[-1], "010C0D")

    async def test_falls_back_when_the_car_refuses_several_values(self):
        for refusal in (["NO DATA"], None, ["7E8 04 41 0C 1A F8"], ["7E8 05 41 0C 1A F8 99"]):
            transport = FakeTransport({"010C0D": refusal, "010C": ["7E8 04 41 0C 1A F8"], "010D": ["7E8 03 41 0D 32"]})
            elm = ELM(transport)
            await elm.read_pid(0x0C)
            await elm.read_pid(0x0D)
            for _ in obd_tool.GROUP_SIZES:
                self.assertIsNot(elm.can_group, False, refusal)
                self.assertEqual(await elm.read_pids([0x0C, 0x0D]), {0x0C: 1726.0, 0x0D: 50}, refusal)
            self.assertIs(elm.can_group, False, refusal)
            sent = len(transport.sent)
            await elm.read_pids([0x0C, 0x0D])
            self.assertNotIn("010C0D", transport.sent[sent:])

    async def test_a_clear_refusal_ends_the_group_trial_at_once(self):
        transport = FakeTransport({"010C0D": ["?"], "010C": ["7E8 04 41 0C 1A F8"], "010D": ["7E8 03 41 0D 32"]})
        elm = ELM(transport)
        await elm.read_pid(0x0C)
        await elm.read_pid(0x0D)
        await elm.read_pids([0x0C, 0x0D])
        self.assertIs(elm.can_group, False)

    async def test_lost_group_reply_tries_a_shorter_list(self):
        pids = [0x0C, 0x0D, 0x11, 0x04]
        transport = FakeTransport({"010C": ["7E8 04 41 0C 1A F8"], "010D": ["7E8 03 41 0D 32"],
                                   "0111": ["7E8 03 41 11 40"], "0104": ["7E8 03 41 04 40"],
                                   "010C0D11": ["7E8 07 41 0C 1A F8 0D 32 11", "7E8 21 40"],
                                   "010C0D": ["7E8 06 41 0C 1A F8 0D 33"]})
        elm = ELM(transport)
        elm.use_counts = False
        for pid in pids:
            await elm.read_pid(pid)
        await elm.read_pids(pids)  # the list of 4 gets no reply, so the next try is shorter
        self.assertIsNone(elm.can_group)
        self.assertEqual(elm.group_size, 3)
        self.assertIn("010C0D1104", transport.sent)
        values = await elm.read_pids([0x0C, 0x0D])
        self.assertTrue(elm.can_group)
        self.assertEqual(elm.group_size, 3)
        self.assertEqual(values[0x0D], 51)

    def test_short_lists_are_packed_into_single_frames(self):
        elm = ELM(FakeTransport({}))
        elm.group_size = 3
        elm.sizes = {0x0C: 2, 0x0D: 1, 0x11: 1, 0x04: 1, 0x05: 1, 0x42: 2, 0x24: 4}
        groups = []
        left = [0x0C, 0x0D, 0x11, 0x04, 0x42]
        while left:
            groups.append(elm.next_group(left))
            left = [pid for pid in left if pid not in groups[-1]]
        self.assertEqual(groups, [[0x0C, 0x42], [0x0D, 0x11, 0x04]])
        self.assertEqual(elm.next_group([0x24, 0x0C]), [0x24])  # too large to share a frame
        elm.group_size = obd_tool.MAX_PIDS_PER_REQUEST
        self.assertEqual(elm.next_group([0x0C, 0x0D, 0x11, 0x04, 0x05, 0x42, 0x24]), [0x0C, 0x0D, 0x11, 0x04, 0x05, 0x42])

    async def test_short_lists_read_fast_values_in_two_requests(self):
        fast = {"010C42": ["7E8 07 41 0C 1A F8 42 32 00"], "010D1104": ["7E8 07 41 0D 32 11 40 04 40"]}
        elm = ELM(FakeTransport(fast))
        elm.can_group, elm.group_size = True, 3
        elm.sizes = {0x0C: 2, 0x0D: 1, 0x11: 1, 0x04: 1, 0x42: 2, 0x05: 1}
        reader = obd_tool.LiveReader(elm, [0x0C, 0x0D, 0x11, 0x04, 0x42, 0x05], {})
        reader.next_slow = reader.slow.index(0x42)
        await reader.step()
        self.assertEqual(elm.transport.sent, 2)
        self.assertEqual(reader.values[0x0D], 50)
        self.assertEqual(reader.values[0x0C], 1726.0)
        self.assertEqual(reader.values[0x42], 12.8)

    async def test_other_module_answering_a_group_does_not_end_grouping(self):
        both = ["7E8 06 41 0C 1A F8 0D 32", "7E9 06 41 0C 1A F8 0D 32"]
        only_other = ["7E9 06 41 0C 1B 00 0D 33"]
        transport = FakeTransport({"010C0D": Sequence([both] + [only_other] * 6)})
        elm = ELM(transport)
        elm.can_group, elm.group_size = None, 3
        elm.sizes = {0x0C: 2, 0x0D: 1}
        reader = obd_tool.LiveReader(elm, [0x0C, 0x0D])
        reader.store(0x0D, 0)
        for pid, value in (await elm.read_pids([0x0C, 0x0D])).items():
            reader.store(pid, value)
        self.assertEqual(reader.values[0x0D], 50)
        sent = len(transport.sent)
        for _ in range(obd_tool.SOURCE_MISSES_ALLOWED - 1):
            for pid, value in (await elm.read_pids([0x0C, 0x0D])).items():
                reader.store(pid, value)
            self.assertEqual(reader.values[0x0D], 50)  # keeps the engine module's last reading
        self.assertIs(elm.can_group, True)
        self.assertEqual(len(transport.sent) - sent, obd_tool.SOURCE_MISSES_ALLOWED - 1)  # no extra requests
        for _ in range(4):
            await elm.read_pids([0x0C, 0x0D])
        self.assertIs(elm.can_group, True)

    async def test_grouping_is_tried_again_after_real_failures(self):
        good = ["7E8 06 41 0C 1A F8 0D 32"]
        transport = FakeTransport({"010C0D": Sequence([good, ["NO DATA"], ["NO DATA"], ["NO DATA"], good]),
                                   "010C": ["7E8 04 41 0C 1A F8"], "010D": ["7E8 03 41 0D 32"]})
        elm = ELM(transport)
        elm.use_counts = False
        elm.sizes, elm.group_size = {0x0C: 2, 0x0D: 1}, 3
        for _ in range(4):
            await elm.read_pids([0x0C, 0x0D])
        self.assertIs(elm.can_group, False)
        await elm.read_pids([0x0C, 0x0D])
        self.assertIs(elm.can_group, False)  # not yet
        elm.group_off_at -= obd_tool.GROUP_RETRY_SECONDS + 1
        await elm.read_pids([0x0C, 0x0D])
        self.assertIs(elm.can_group, True)

    def test_egr_error_ff_is_not_shown_as_a_percentage(self):
        formula = obd_tool.LIVE_PIDS[0x2D][2]
        self.assertEqual(formula(bytes([0xFF])), "Not reported")
        self.assertEqual(formula(bytes([0x80])), 0.0)
        self.assertEqual(i18n.t("Not reported", "ar"), "غير متوفر")

    def test_placeholder_temperatures_are_not_shown_as_readings(self):
        oil, catalyst = obd_tool.LIVE_PIDS[0x5C][2], obd_tool.LIVE_PIDS[0x3C][2]
        self.assertEqual(oil(bytes([0x00])), "Not reported")
        self.assertEqual(oil(bytes([0x82])), 90)
        self.assertEqual(catalyst(bytes([0x01, 0x8E])), "Not reported")
        self.assertAlmostEqual(catalyst(bytes([0x21, 0xBE])), 823.8)

    def test_page_asks_often_but_redraws_only_on_news(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        self.assertIn("ASK_MS = 150", page)
        self.assertIn("newReading && v.value !== null", page)
        self.assertIn("newReading && !v.note", page)

    async def test_a_silent_adapter_ends_the_connection_quickly(self):
        transport = FakeTransport({"0902": None, "010C": ["7E8 04 41 0C 1A F8"]})
        elm = ELM(transport)
        await elm.transport.send("0902")
        await elm.transport.send("0902")
        await elm.transport.send("010C")  # a reply starts the count again
        await elm.transport.send("0902")
        await elm.transport.send("0902")
        with self.assertRaises(OBDError) as caught:
            await elm.transport.send("0902")
        self.assertEqual(caught.exception.text, obd_tool.ADAPTER_SILENT)

    async def test_live_log_records_and_reviews_traffic(self):
        activity = obd_tool.Activity()
        transport = FakeTransport({"010C": ["7E8 04 41 0C 1A F8"], "0902": ["7E8 10 14 49 02 01 4B 4E 41"], "0105": ["NO DATA"], "0111": None})
        elm = ELM(transport)
        elm.transport.activity = activity
        for command in ("010C", "0902", "0105", "0111"):
            await elm.transport.send(command)
        activity.event("Adapter found, talking to it now.")
        view = activity.view(0, "en")
        by_command = {e["command"]: e for e in view["entries"] if e["kind"] == "command"}
        self.assertEqual(by_command["010C"]["state"], "ok")
        self.assertEqual(by_command["0105"]["state"], "trouble")
        self.assertEqual(by_command["0111"]["state"], "lost")
        self.assertEqual(by_command["0902"]["reply"], [])  # it identifies the car
        self.assertTrue(by_command["0902"]["hidden"])
        self.assertNotIn("4B 4E 41", str(view))
        self.assertIn("no reply", view["review"]["findings"][0]["text"])
        self.assertIn("7E8", [row for row in view["review"]["rows"] if row["label"] == "Modules answering"][0]["text"])
        self.assertEqual(activity.view(activity.next - 1, "en")["entries"], [])
        self.assertIn("السجل", i18n.t("Live log", "ar"))

    async def test_a_quiet_log_says_so(self):
        self.assertIn("Nothing has been sent", obd_tool.Activity().review("en")["findings"][0]["text"])

    def test_self_test_results_are_decoded_with_their_limits(self):
        payload = obd_tool.Reply(bytes([0x46, 0x01, 0x85, 0x0B, 0x02, 0xBC, 0x00, 0x64, 0x03, 0xE8,
                                        0x01, 0x86, 0x0B, 0x04, 0xB0, 0x00, 0x64, 0x03, 0xE8,
                                        0x21, 0xA9, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                        0x31, 0x01, 0x77, 0x00, 0x01, 0x00, 0x00, 0x00, 0x02]))
        results = obd_tool.decode_test_results("7E8", payload)
        self.assertEqual([r["state"] for r in results], ["pass", "fail", "not_run", "unknown"])
        self.assertAlmostEqual(results[0]["value"], 0.7)
        self.assertEqual((results[0]["low"], results[0]["high"], results[0]["unit"]), (0.1, 1.0, "V"))
        cut = obd_tool.Reply(bytes(payload))
        cut.complete = False
        self.assertEqual(obd_tool.decode_test_results("7E8", cut), [])

    def test_self_test_values_follow_their_sign_and_offset(self):
        self.assertEqual(obd_tool.scaled_test_value(0x81, bytes([0xFF, 0xFE])), -2)
        self.assertEqual(obd_tool.scaled_test_value(0x01, bytes([0xFF, 0xFE])), 65534)
        self.assertAlmostEqual(obd_tool.scaled_test_value(0x16, bytes([0x01, 0x90])), 0.0)
        self.assertIsNone(obd_tool.scaled_test_value(0x77, bytes([0, 1])))

    def test_monitors_are_named_from_their_ids(self):
        self.assertEqual(obd_tool.monitor_name(0x02, "en"), "Oxygen sensor B1S2")
        self.assertEqual(obd_tool.monitor_name(0x06, "en"), "Oxygen sensor B2S2")
        self.assertEqual(obd_tool.monitor_name(0x42, "en"), "Oxygen sensor heater B1S2")
        self.assertEqual(obd_tool.monitor_name(0xA3, "en"), "Misfire, cylinder 2")
        self.assertEqual(obd_tool.monitor_name(0x22, "en"), "Catalyst, bank 2")
        self.assertEqual(obd_tool.monitor_name(0xF0, "en"), "Monitor F0")
        self.assertIn("الأكسجين", obd_tool.monitor_name(0x01, "ar"))

    async def test_records_and_self_tests_reach_the_page(self):
        elm = ELM(demo.DemoTransport())
        elm.vehicle = {"Module name": {"7E8": "ECM", "7E9": "TCM"}}
        await elm.setup()
        records = await obd_tool.read_module_records(elm)
        self.assertEqual([r["hdr"] for r in records], ["7E0"])  # the demo's transmission keeps no records
        page = obd_tool.page_module_records(records, "en")
        states = {item["short"]: item["state"] for item in page[0]["codes"]}
        self.assertEqual(states, {"P0171": "active", "P0455": "history"})
        results = await obd_tool.read_test_results(elm)
        groups = obd_tool.page_test_results(results, "en")
        self.assertEqual(groups[0]["name"], "Oxygen sensor B1S2")  # the one outside its limits comes first
        self.assertEqual(groups[0]["tests"][0]["state"], "fail")
        self.assertEqual(groups[0]["tests"][0]["value"], "1.2 V")
        self.assertEqual(groups[0]["tests"][0]["limits"], "0.1 - 1")

    async def test_a_car_offering_no_records_or_tests_is_fine(self):
        elm = ELM(FakeTransport({}))
        self.assertEqual(await obd_tool.read_module_records(elm), [])
        self.assertEqual(await obd_tool.read_test_results(elm), [])
        self.assertEqual(obd_tool.page_module_records(None), [])
        self.assertEqual(obd_tool.page_test_results(None), [])

    async def test_a_value_stays_with_its_module_after_a_miss(self):
        both = ["7E8 03 41 1C 1E", "7E9 03 41 1C 01"]
        transport = FakeTransport({"011C": Sequence([both, ["7E9 03 41 1C 01"], ["7E9 03 41 1C 01"], both])})
        elm = ELM(transport)
        elm.use_counts = False
        reader = obd_tool.LiveReader(elm, [0x1C])
        reader.store(0x1C, await elm.read_pid(0x1C))
        first = reader.values[0x1C]
        for _ in range(3):
            reader.store(0x1C, await elm.read_pid(0x1C))
            self.assertEqual(reader.values[0x1C], first)

    async def test_a_value_moves_only_if_its_module_keeps_missing(self):
        transport = FakeTransport({"010D": Sequence([["7E8 03 41 0D 32"]] + [["7E9 03 41 0D 40"]] * obd_tool.SOURCE_MISSES_ALLOWED)})
        elm = ELM(transport)
        elm.use_counts = False
        self.assertEqual(await elm.read_pid(0x0D), 0x32)
        for _ in range(obd_tool.SOURCE_MISSES_ALLOWED - 1):
            self.assertIsNone(await elm.read_pid(0x0D))
        self.assertEqual(await elm.read_pid(0x0D), 0x40)

    def test_lower_address_takes_over_once_it_answers(self):
        elm = ELM(FakeTransport({}))
        self.assertEqual(elm.from_usual_module(0x0D, {"7E9": 1}), 1)
        self.assertEqual(elm.from_usual_module(0x0D, {"7E8": 2, "7E9": 1}), 2)
        self.assertEqual(elm.sources[0x0D], "7E8")

    def test_a_missed_reply_keeps_the_last_value_and_marks_it_when_old(self):
        reader = obd_tool.LiveReader(ELM(FakeTransport({})), [0x03], {0x03: "Closed loop"})
        reader.store(0x03, None)
        self.assertEqual(reader.values[0x03], "Closed loop")
        self.assertIsNone(reader.age_note(0x03))
        reader.read_at[0x03] -= obd_tool.SLOW_STALE_SECONDS + 1
        reader.store(0x03, None)
        self.assertIn("seconds ago", reader.age_note(0x03))
        fresh = obd_tool.LiveReader(ELM(FakeTransport({})), [0x03])
        fresh.store(0x03, None)
        self.assertIsNone(fresh.values[0x03])

    async def test_live_values_keep_updating_during_the_fault_code_recheck(self):
        args = SimpleNamespace(demo=True, samples=20, interval=0, port=0, no_browser=True)
        app = obd_tool.DashboardApp(args)
        elm = ELM(demo.DemoTransport())
        elm.demo = True
        await elm.setup()
        sent = []
        send = elm.transport.send

        async def spy(command, wait=None):
            sent.append(command)
            return await send(command, wait)

        elm.transport.send = spy
        with mock.patch.object(obd_tool, "REFRESH_SECONDS", 0):
            await app.session(elm, None)
        last_pending = len(sent) - 1 - sent[::-1].index("07")
        last_stored = last_pending - 1 - sent[last_pending - 1::-1].index("03")
        self.assertTrue(any(command.startswith("010C") for command in sent[last_stored:last_pending]))

    def test_a_group_reply_never_guesses_at_unexpected_bytes(self):
        sizes = {0x0C: 2, 0x0D: 1}
        split = lambda data: obd_tool.split_group(obd_tool.Reply(bytes(data)), sizes, {0x0C, 0x0D})
        self.assertEqual(split([0x41, 0x0C, 1, 2, 0x0D, 3]), {0x0C: bytes([1, 2]), 0x0D: bytes([3])})
        self.assertEqual(split([0x41, 0x0C, 1, 2, 0x0D]), {})  # cut short
        self.assertEqual(split([0x41, 0x0C, 1, 2, 0x05, 3]), {})  # not asked for
        self.assertEqual(split([0x7F, 0x01, 0x12]), {})  # a refusal
        cut = obd_tool.Reply(bytes([0x41, 0x0C, 1, 2]))
        cut.complete = False
        self.assertEqual(obd_tool.split_group(cut, sizes, {0x0C}), {})

    async def test_reply_count_is_learned_and_dropped_if_the_adapter_refuses(self):
        transport = FakeTransport({"010C": ["7E8 04 41 0C 1A F8", "7E9 04 41 0C 1A F8"], "010C2": ["7E8 04 41 0C 1A F8", "7E9 04 41 0C 1A F8"]})
        elm = ELM(transport)
        elm.can_group = False
        for _ in range(obd_tool.READS_BEFORE_COUNTING):
            await elm.read_pid(0x0C)
        self.assertEqual(transport.sent[-1], "010C")
        self.assertEqual(await elm.read_pid(0x0C), 1726.0)
        self.assertEqual(transport.sent[-1], "010C2")
        refused = FakeTransport({"010C": ["7E8 04 41 0C 1A F8"], "010C1": ["?"]})
        elm = ELM(refused)
        for _ in range(obd_tool.READS_BEFORE_COUNTING):
            await elm.read_pid(0x0C)
        self.assertEqual(await elm.read_pid(0x0C), 1726.0)
        self.assertFalse(elm.use_counts)
        await elm.read_pid(0x0C)
        self.assertEqual(refused.sent[-1], "010C")

    async def test_grouped_updates_reach_slow_values_much_sooner(self):
        pids = [0x0C, 0x0D, 0x11, 0x04, 0x05, 0x0F, 0x0B, 0x0E, 0x06, 0x07, 0x2F, 0x33, 0x46, 0x10]
        elm = obd_tool.ELM(obd_tool.DemoTransport())
        for pid in pids:
            await elm.read_pid(pid)
        reader = obd_tool.LiveReader(elm, pids, {pid: 0 for pid in pids})
        for _ in range(3):
            await reader.step()
        self.assertTrue(elm.can_group)
        self.assertTrue(all(reader.values[pid] != 0 or pid in (0x07,) for pid in (0x0C, 0x05, 0x10)))
        # The first update tries it with 2 slow values, then each later one takes 8.
        self.assertEqual(reader.next_slow, (2 + 2 * obd_tool.GROUP_SLOW_PER_STEP) % len(reader.slow))

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

    def test_not_reported_has_no_unit(self):
        reader = obd_tool.LiveReader(ELM(FakeTransport({})), [0x5C, 0x05], {0x5C: "Not reported", 0x05: 90})
        snapshot = {"freeze": None, "adapter": "ELM327", "protocol": "CAN", "vehicle": {}, "monitors": {}, "codes": {},
                    "this_drive": [], "more": [], "raw": {}}
        values = {v["pid"]: v for v in obd_tool.dashboard_state(snapshot, reader, lang="en")["values"]}
        self.assertEqual(values["5C"]["unit"], "")
        self.assertEqual(values["05"]["unit"], "C")

    async def test_page_state_carries_the_mark(self):
        elm = ELM(FakeTransport({}))
        reader = obd_tool.LiveReader(elm, [0x05], {0x05: 90})
        reader.read_at[0x05] -= obd_tool.SLOW_STALE_SECONDS + 1
        snapshot = {"freeze": None, "adapter": "ELM327", "protocol": "CAN", "vehicle": {}, "monitors": {}, "codes": {},
                    "this_drive": [], "more": [], "raw": {}}
        value = obd_tool.dashboard_state(snapshot, reader, lang="en")["values"][0]
        self.assertIn("seconds ago", value["note"])


class DataFolderTests(unittest.TestCase):
    def test_saved_files_go_to_the_chosen_folder_when_installed(self):
        code = ("import garage, i18n, car_modules, obd_tool; print(garage.CARS_DIR); print(i18n.SETTINGS_FILE); "
                "print(car_modules.DOWNLOAD_DIR); print(obd_tool.LOG_DIR); print(obd_tool.REPORT_DIR)")
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True,
                                    env={**os.environ, "FAHIS_DATA_DIR": folder})
            for line in result.stdout.splitlines():
                self.assertTrue(Path(line).is_relative_to(folder), line)

    def test_running_from_source_keeps_saving_beside_the_code(self):
        if "FAHIS_DATA_DIR" not in os.environ:
            self.assertEqual(garage.CARS_DIR.parent, ROOT)


class FaultDataTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(dtc_codes.set_car_make, None)

    def test_make_comes_from_the_vin(self):
        self.assertEqual(dtc_codes.make_from_vin("KNA00000000000000"), "Kia")
        self.assertEqual(dtc_codes.make_from_vin("JTH00000000000000"), "Lexus")
        self.assertEqual(dtc_codes.make_from_vin("JTD00000000000000"), "Toyota")
        self.assertIsNone(dtc_codes.make_from_vin("DEMO0000000000000"))
        self.assertIsNone(dtc_codes.make_from_vin(None))

    def test_a_makers_code_is_only_named_for_that_makers_car(self):
        self.assertIn("manufacturer-specific", obd_tool.describe_dtc("P1102", "en"))
        self.assertGreater(len(dtc_codes.oem_meanings("P1102")), 1)
        dtc_codes.set_car_make("Kia")
        self.assertEqual(obd_tool.describe_dtc("P1102", "en"), "HO2S11 Heater Circuit High Voltage (Kia)")
        self.assertEqual(list(dtc_codes.oem_meanings("P1102")), ["Kia"])
        self.assertEqual(obd_tool.describe_dtc("P0420", "en"), obd_tool.lookup_dtc("P0420", "en"))
        dtc_codes.set_car_make("Hyundai")  # no list for this make, so it stays honest
        self.assertIn("manufacturer-specific", obd_tool.describe_dtc("P1102", "en"))

    def test_only_manufacturer_ranges_are_in_the_maker_lists(self):
        for make, codes in dtc_codes.oem_codes().items():
            for code in codes:
                self.assertRegex(code, r"^(P1|P3[0-3]|B1|B2|C1|C2|U1|U2)", make)

    def test_plans_are_complete_in_both_languages(self):
        difficulties = set()
        for code, plans in dtc_codes._plans().items():
            for plan, arabic in zip(plans, dtc_codes.repair_plans(code, "ar")):
                self.assertIn(plan["source"], ("MechanicDB", "Fahis"))
                for fix, translated in zip(plan["fixes"], arabic["fixes"]):
                    difficulties.add(fix["difficulty"])
                    self.assertTrue(fix["steps"], code)
                    for text in [translated["title"], *translated["steps"]]:
                        self.assertRegex(text, r"[؀-ۿ]", code)
        for difficulty in difficulties:
            self.assertNotEqual(i18n.t(difficulty, "ar"), difficulty)

    def test_your_cars_codes_have_plans(self):
        for code in ("P0741", "P0700", "P0455", "P0420", "P2003"):
            self.assertTrue(dtc_codes.repair_plans(code), code)
        self.assertEqual(dtc_codes.repair_plans("P2003")[0]["source"], "Fahis")

    def test_another_makers_plan_is_hidden_for_a_known_car(self):
        code, plan = next((c, p[0]) for c, p in dtc_codes._plans().items() if p[0]["make"])
        self.assertTrue(dtc_codes.repair_plans(code))
        dtc_codes.set_car_make(plan["make"])
        self.assertTrue(dtc_codes.repair_plans(code))
        dtc_codes.set_car_make("Kia" if plan["make"] != "Kia" else "Ford")
        self.assertFalse(dtc_codes.repair_plans(code))

    def test_estimates_leave_out_what_is_unknown(self):
        unknown = {"parts_min": None, "parts_max": None, "labor_hours": None}
        self.assertEqual(obd_tool.fix_estimate(unknown, "en"), "")
        known = {"parts_min": 10.0, "parts_max": 60.0, "labor_hours": 0.8}
        self.assertEqual(obd_tool.fix_estimate(known, "en"), "Parts: $10 to $60, US estimate | Labor: about 0.8 hours")

    def test_licence_notices_ship_with_the_data(self):
        data = Path(obd_tool.__file__).parent / "data"
        self.assertIn("MIT License", (data / "LICENSE-dtc-database").read_text(encoding="utf-8"))
        self.assertIn("ODbL", (data / "LICENSE-MechanicDB").read_text(encoding="utf-8"))


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
        self.assertIn("about 1 V too high", output)
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
        self.assertEqual(by_hdr["7D1"]["codes"], [{"code": "U0123-01", "state": "active", "details": []}])
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
        self.assertEqual([(c["code"], c["state"]) for c in codes], [("P0455-00", "history"), ("U1112-00", "history")])
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

    def test_a_car_with_no_identity_is_not_saved(self):
        empty = {"VIN": {}, "Calibration ID": {}, "Calibration check": {}, "Module name": {}}
        garage.remember_connection(empty, "ELM327", "CAN")
        garage.remember(empty, "status", {"light": False})
        self.assertEqual(garage.summaries(), [])

    def test_past_connections_and_commands_are_kept_per_car(self):
        garage.save_session(self.VEHICLE, {"id": "a", "started": "2026-01-01T10:00:00", "seconds": 5})
        garage.save_session(self.VEHICLE, {"id": "a", "started": "2026-01-01T10:00:00", "seconds": 90})
        garage.add_test(self.VEHICLE, "codes", "done", "x" * (garage.MAX_TEST_OUTPUT + 5))
        record = garage.load_car(garage.car_key(self.VEHICLE))
        self.assertEqual([s["seconds"] for s in record["sessions"]], [90])
        self.assertEqual(len(record["tests"][0]["output"]), garage.MAX_TEST_OUTPUT)
        kinds = [item["kind"] for item in garage.timeline(record)]
        self.assertEqual(kinds[-1], "connection")
        self.assertIn("command", kinds)
        with mock.patch.object(garage, "MAX_TESTS", 2):
            for _ in range(4):
                garage.add_test(self.VEHICLE, "codes", "done", "")
        self.assertEqual(len(garage.load_car(garage.car_key(self.VEHICLE))["tests"]), 2)

    def test_fault_history_keeps_first_and_last_seen(self):
        record = {"history": [{"time": "A", "codes": ["P0700", "P2003"]}, {"time": "B", "codes": ["P0700"]}],
                  "status": {"time": "B", "active_codes": ["P0700"]}}
        faults = garage.fault_history(record)
        self.assertEqual([f["code"] for f in faults], ["P0700", "P2003"])
        self.assertEqual((faults[0]["first_seen"], faults[0]["last_seen"], faults[0]["times"], faults[0]["present"]), ("A", "B", 2, True))
        self.assertFalse(faults[1]["present"])

    def test_cars_can_be_named_and_summarised(self):
        garage.remember_connection(self.VEHICLE, "ELM327", "CAN")
        key = garage.car_key(self.VEHICLE)
        self.assertTrue(garage.set_name(key, "  My   car "))
        self.assertEqual(garage.summaries()[0]["name"], "My car")
        self.assertTrue(garage.set_name(key, ""))
        self.assertIsNone(garage.summaries()[0]["name"])
        self.assertFalse(garage.set_name("nobody", "x"))
        self.assertEqual(garage.model_year("KNAAA000AHT000000"), 2017)
        self.assertEqual(garage.model_year("KNAAA000AJA000000"), 2018)
        self.assertIsNone(garage.model_year("short"))

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
        for name in ("obd_tool.py", "car_modules.py", "car_analysis.py"):
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
        codes = car_modules.decode_uds_codes(reply)
        self.assertEqual([(c["code"], c["state"]) for c in codes], [("P2003-00", "stored"), ("P0700-00", "active")])
        self.assertEqual(codes[1]["details"], ["Asking for the warning light", "Failed on this drive"])

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

    def test_resting_voltage_waits_longer_than_a_real_fault(self):
        now = obd_tool.time.monotonic()
        resting = {0x42: 12.7, 0x0C: 2000.0}
        self.app.reader = SimpleNamespace(values=resting, voltage="13.8V")
        self.app.track_charging()
        self.app.charging_since = now - obd_tool.CHARGING_ALERT_SECONDS - 1
        self.assertFalse(self.app.charging_lasting())
        self.app.charging_since = now - obd_tool.CHARGING_RESTING_ALERT_SECONDS - 1
        self.assertTrue(self.app.charging_lasting())
        self.app.reader.values = {0x42: 11.9, 0x0C: 2000.0}  # draining the battery, and the wait starts again
        self.app.track_charging()
        self.assertFalse(self.app.charging_lasting())
        self.app.charging_since = now - obd_tool.CHARGING_ALERT_SECONDS - 1
        self.assertTrue(self.app.charging_lasting())
        self.app.reader.values = {0x42: 15.8, 0x0C: 2000.0}
        self.app.track_charging()
        self.app.charging_since = now - obd_tool.CHARGING_ALERT_SECONDS - 1
        self.assertTrue(self.app.charging_lasting())

    def test_charging_reading_resets_the_resting_wait(self):
        self.app.reader = SimpleNamespace(values={0x42: 12.7, 0x0C: 2000.0}, voltage="13.8V")
        self.app.track_charging()
        self.app.reader.values = {0x42: 14.2, 0x0C: 1500.0}
        self.app.track_charging()
        self.assertIsNone(self.app.charging_since)
        self.assertFalse(self.app.charging_lasting())

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

    async def test_adapter_stays_shown_only_while_the_car_is_silent(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=False, samples=1, interval=0))
        app.RETRY_SECONDS = 0
        elm = ELM(FakeTransport({"ATZ": ["ELM327 v2.1"], "AT@1": ["OBDII to RS232 Interpreter"], "0100": ["UNABLE TO CONNECT"]}))
        elm.device_name = "OBDII"
        elm.device_address = "AA:BB:CC:DD:EE:FF"
        seen = []

        async def connect():
            seen.append(self.rows(app))
            if len(seen) == 1:
                app.adapter_found(elm)
                await elm.setup()
            if len(seen) == 2:
                raise OBDError("Adapter not found.")

        app.connect = connect
        with mock.patch("builtins.print"):
            await app.run()
        self.assertIn("Looking", seen[0]["Status"]["text"])
        self.assertEqual(seen[1]["Status"]["text"], "Adapter connected, waiting for the car")
        self.assertEqual(seen[1]["Status"]["tone"], "bad")
        self.assertEqual(seen[1]["Name"]["text"], "OBDII")
        self.assertEqual(seen[1]["Reported version"]["text"], "ELM327 v2.1")
        self.assertIn("Looking", seen[2]["Status"]["text"])
        self.assertNotIn("Name", seen[2])

    async def test_missed_supported_values_request_is_retried(self):
        answer = ["7E8 06 41 00 80 00 00 00"]
        elm = ELM(FakeTransport({"0100": Sequence([["NO DATA"], ["NO DATA"], answer])}))
        self.assertEqual(await elm.supported_pids(), {0x01})

    async def test_car_offering_no_values_connects_again(self):
        elm = ELM(FakeTransport({"0100": ["NO DATA"]}))
        elm.vehicle = {}
        with self.assertRaises(OBDError) as caught:
            await obd_tool.read_snapshot(elm)
        self.assertEqual(caught.exception.text, obd_tool.CAR_SILENT)

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


class BookFixTests(unittest.IsolatedAsyncioTestCase):
    """Reply handling and fault details from ISO 15765."""

    def test_please_wait_then_long_reply_keeps_only_the_answer(self):
        reply = parse_can(["7E8 03 7F 19 78", "7E8 10 0A 59 02 AF 04 55 00", "7E8 21 28 07 41 00 28 00 00"])["7E8"]
        self.assertEqual(reply.hex(" "), "59 02 af 04 55 00 28 07 41 00")
        self.assertTrue(reply.complete)

    def test_lost_or_repeated_frames_are_not_trusted(self):
        lost = parse_can(["7E8 10 10 59 02 AF 04 55 00", "7E8 22 11 11 11 11 11 11 11", "7E8 23 22 22 22 22 22 22 22"])["7E8"]
        self.assertFalse(lost.complete)
        self.assertEqual(lost, bytes.fromhex("5902AF045500"))
        repeated = parse_can(["7E8 10 10 59 02 AF 04 55 00", "7E8 21 28 07 41 00 00 00 00", "7E8 21 28 07 41 00 00 00 00", "7E8 22 11 11 11"])["7E8"]
        self.assertFalse(repeated.complete)

    def test_frames_outside_a_message_change_nothing(self):
        late = parse_can(["7E8 10 0A 59 02 AF 04 55 00", "7E8 21 28 07 41 00", "7E8 21 28 07 41 00"])["7E8"]
        self.assertTrue(late.complete)
        self.assertEqual(late.hex(), "5902af04550028074100")
        self.assertEqual(parse_can(["7E8 30 00 00 00 00 00 00 00"]), {})
        self.assertEqual(parse_can(["7E8 21 28 07 41 00"]), {})
        self.assertFalse(parse_can(["7E8 00 11 22"])["7E8"].complete)

    def test_unfinished_tests_are_not_called_past_faults(self):
        reply = bytes.fromhex("5902FF") + bytes.fromhex("04550050") + bytes.fromhex("D1120020") + bytes.fromhex("03000000")
        self.assertEqual([c["code"] for c in car_modules.decode_uds_codes(reply)], ["U1112-00"])

    def test_severity_and_module_identity_details(self):
        severity = car_modules.decode_severity(bytes.fromhex("5908AF" "80000171" "0009" "20000455" "0020"))
        self.assertEqual(severity, {"P0171-00": "Check immediately", "P0455-00": "Needs maintenance"})
        self.assertEqual(car_modules.decode_severity(bytes.fromhex("7F1931")), {})
        self.assertEqual(car_modules.readable_date(bytes.fromhex("20260115")), "2026-01-15")
        self.assertEqual(car_modules.readable_date(bytes.fromhex("260115")), "2026-01-15")
        self.assertIsNone(car_modules.readable_date(bytes.fromhex("2026FF15")))
        self.assertIsNone(car_modules.decode_one_record(bytes.fromhex("590DAF04550020"), 0x0B))
        self.assertEqual(car_modules.refusal_reason(bytes.fromhex("7F2283")), "Needs the engine off")

    def test_a_different_stored_vin_is_pointed_out(self):
        snapshot = {"vehicle": {"VIN": {"7E8": "AAAAAAAAAAAAAAAAA"}, "Module name": {"7E8": "ECM"}}, "monitors": {"7E8": {}}, "codes": {},
                    "ecu_details": {"7E8": [{"label": "Stored VIN", "text": "BBBBBBBBBBBBBBBBB"}]}}
        self.assertIn("different VIN", obd_tool.page_ecus(snapshot, (), "en")[0]["warning"])
        snapshot["ecu_details"]["7E8"][0]["text"] = "AAAAAAAAAAAAAAAAA"
        self.assertIsNone(obd_tool.page_ecus(snapshot, (), "en")[0]["warning"])

    async def test_adapter_problems_get_their_own_message(self):
        transport = FakeTransport({"ATZ": ["ELM327 v2.1"], "0100": ["CAN ERROR"]})
        with self.assertRaises(OBDError) as caught:
            await ELM(transport).setup()
        self.assertEqual(caught.exception.text, obd_tool.BUS_PROBLEM)
        self.assertEqual(caught.exception.values["problem"], "CAN ERROR")

    async def test_adapter_reset_connects_again(self):
        elm = ELM(FakeTransport({"010C": ["LV RESET"]}))
        with self.assertRaises(OBDError) as caught:
            await elm.transport.send("010C")
        self.assertEqual(caught.exception.text, obd_tool.ADAPTER_RESTARTED)

    async def test_car_that_hides_its_protocol_number_still_connects(self):
        transport = FakeTransport({"ATZ": ["ELM327 v2.1"], "0100": ["7E8 06 41 00 98 3B 80 11"], "ATDPN": ["A"]})
        elm = ELM(transport)
        await elm.setup()
        self.assertIn("11-bit", elm.protocol)

    async def test_vin_falls_back_to_asking_the_engine_module(self):
        vin = list(b"AAAAAAAAAAAAAAAAA")
        lines = demo.frames("7E8", [0x62, 0xF1, 0x90] + vin)

        class Transport:
            header = None

            async def send(self, command, wait=5):
                if command.startswith("ATSH"):
                    self.header = command[4:]
                return lines if command == "22F190" and self.header == "7E0" else ["NO DATA"] if not command.startswith("AT") else ["OK"]

        vehicle = await obd_tool.read_identity(ELM(Transport()))
        self.assertEqual(vehicle["VIN"], {"7E8": "AAAAAAAAAAAAAAAAA"})

    async def test_freeze_frame_stays_on_the_module_with_the_code(self):
        class Transport:
            async def send(self, command, wait=5):
                table = {"020200": ["7E9 05 42 02 00 00 00", "7E8 05 42 02 00 01 71"],
                         "020000": ["7E8 07 42 00 00 08 00 00 01"], "022000": ["7E8 07 42 20 00 00 00 00 00"],
                         "020500": ["7E9 04 42 05 00 32", "7E8 04 42 05 00 7D"]}
                return table.get(command, ["NO DATA"])

        freeze = await obd_tool.read_freeze_frame(ELM(Transport()))
        self.assertEqual(freeze["code"], "P0171")
        self.assertEqual([(v["name"], v["text"]) for v in freeze["values"]], [("Coolant temp", "85")])

    def test_this_drive_names_follow_the_engine_type(self):
        tests = [name for name, _ in obd_tool.decode_monitors(bytes.fromhex("4141000000FF00"[:12]) + b"\x00", True)["tests"]]
        self.assertEqual(tests, [])
        spark = obd_tool.decode_monitors(bytes.fromhex("41410000" + "01" + "00"), False)["tests"]
        diesel = obd_tool.decode_monitors(bytes.fromhex("41410000" + "02" + "00"), True)["tests"]
        self.assertEqual(spark[0][0], "Catalyst")
        self.assertEqual(diesel[0][0], "NOx aftertreatment")

    def test_fuel_use_and_warning_colours(self):
        self.assertAlmostEqual(obd_tool.fuel_use({0x10: 10.0, 0x51: "Gasoline", 0x0D: 100})[0], 3.29, places=1)
        self.assertEqual(obd_tool.fuel_use({0x10: 10.0, 0x51: "Gasoline", 0x0D: 0})[1], "L/h")
        self.assertIsNone(obd_tool.fuel_use({0x10: 10.0, 0x51: "Diesel", 0x0D: 50}))
        self.assertEqual(obd_tool.fuel_use({0x5E: 6.0, 0x51: "Diesel", 0x0D: 100}), (6.0, "L/100km"))
        self.assertEqual(obd_tool.value_tone(0x05, 106), "warn")
        self.assertEqual(obd_tool.value_tone(0x05, 120), "bad")
        self.assertEqual(obd_tool.value_tone(0x05, 90), "")
        self.assertEqual(obd_tool.value_tone(0x2F, 8.2), "warn")
        self.assertEqual(obd_tool.value_tone(0x42, 11.0), "bad")
        self.assertEqual(obd_tool.value_tone(0x05, "Not reported"), "")

    def test_placeholder_temperatures_are_not_shown_as_readings(self):
        for pid in (0x05, 0x0F, 0x46):
            self.assertEqual(LIVE_PIDS[pid][2](bytes([0x00])), "Not reported")
            self.assertEqual(LIVE_PIDS[pid][2](bytes([0xFF])), "Not reported")
        self.assertEqual(LIVE_PIDS[0x05][2](bytes([0x7D])), 85)

    def test_frame_numbers_wrap_after_15(self):
        lines = ["7E8 10 80 " + " ".join(["01"] * 6)]
        lines += [f"7E8 {0x20 | (n % 16):02X} " + " ".join(["02"] * 7) for n in range(1, 19)]
        self.assertTrue(parse_can(lines)["7E8"].complete)

    def test_busy_module_keeps_its_place_and_late_answer_is_kept(self):
        elm = ELM(FakeTransport({}))
        elm.parsed(["7E8 03 7F 09 78"])
        self.assertEqual(elm.busy_modules(), {"7E8"})
        elm.sources[0x0C] = "7E8"
        for _ in range(obd_tool.SOURCE_MISSES_ALLOWED + 2):
            self.assertIsNone(elm.from_usual_module(0x0C, {"7E9": 700}))
        self.assertEqual(elm.sources[0x0C], "7E8")
        elm.parsed(["7E8 10 0B 49 06 01 AA BB CC", "7E8 21 DD 00 00 00 00 00 00"])
        self.assertEqual(elm.busy_modules(), set())
        self.assertTrue(elm.calibration["7E8"].startswith("AABBCC"))

    async def test_identity_does_not_ask_for_the_calibration_check(self):
        transport = FakeTransport({})
        await obd_tool.read_identity(ELM(transport))
        self.assertNotIn("0906", transport.sent)

    def test_refusal_reasons_are_named(self):
        self.assertEqual(car_modules.refusal_reason(bytes.fromhex("7F1933")), "Locked by the maker's security")
        self.assertEqual(car_modules.refusal_reason(bytes.fromhex("7F19EE")), "Reason not known")
        self.assertIsNone(car_modules.refusal_reason(bytes.fromhex("5902")))

    def test_first_or_latest_fault_is_decoded(self):
        self.assertEqual(car_modules.decode_one_record(bytes.fromhex("590BAF04550020")), "P0455-00")
        self.assertIsNone(car_modules.decode_one_record(bytes.fromhex("590BAF")))

    def test_fault_pattern_tells_returning_from_steady(self):
        history = [{"time": str(i), "codes": codes} for i, codes in enumerate((["P0300"], [], ["P0300", "P0171"], ["P0171"]))]
        record = {"history": history, "status": {"active_codes": ["P0171"]}}
        patterns = {fault["code"]: fault["pattern"] for fault in garage.fault_history(record)}
        self.assertEqual(patterns, {"P0300": "returns", "P0171": "stays"})

    def test_recheck_hint_uses_saved_conditions_and_skips_missing_ones(self):
        freeze = {"values": [{"name": "Engine RPM", "text": "2,000"}, {"name": "Engine RPM", "text": "2560"}, {"name": "Coolant temp", "text": "Not reported"}]}
        self.assertIn("2560 rpm", obd_tool.recheck_hint(freeze, "en"))
        self.assertIsNone(obd_tool.recheck_hint({"values": []}, "en"))


class EcuSectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_carry_on_after_one_silence_and_stop_after_two(self):
        class Transport:
            """Silent on the requests listed, like a real transmission computer that skipped one."""

            def __init__(self, silent):
                self.silent, self.asked = silent, []

            async def send(self, command, wait=5):
                if command.startswith("AT"):
                    return ["OK"]
                self.asked.append(command)
                if command in self.silent:
                    return ["NO DATA"]
                return [f"7E9 06 62 {command[2:4]} {command[4:6]} 41 42 43"]

        skipped_one = Transport({"22F191"})
        details = await car_modules.read_ecu_details(ELM(skipped_one), ["7E9"])
        self.assertEqual(skipped_one.asked, [request for request, _ in car_modules.ECU_DETAILS])
        self.assertEqual([item["label"] for item in details["7E9"]][:3], ["Part number", "Software version", "Supplier software version"])

        apart = Transport({"22F191", "22F197"})
        await car_modules.read_ecu_details(ELM(apart), ["7E9"])
        self.assertEqual(apart.asked, [request for request, _ in car_modules.ECU_DETAILS])

        stops = Transport({"22F191", "22F195"})
        await car_modules.read_ecu_details(ELM(stops), ["7E9"])
        self.assertEqual(stops.asked, ["22F187", "22F189", "22F191", "22F195"])

    async def test_details_are_read_only_and_stop_at_a_silent_computer(self):
        elm = ELM(demo.DemoTransport())
        details = await car_modules.read_ecu_details(elm, ["7E8", "7E9"])
        self.assertEqual(details["7E8"][0], {"label": "Part number", "text": "DEMO-PART-1"})
        self.assertEqual(details["7E9"], [])
        for request, _ in car_modules.ECU_DETAILS:
            self.assertIn(request[:2], car_modules.READ_ONLY_SERVICES)

    def test_binary_details_show_as_hex(self):
        self.assertEqual(car_modules.readable_detail(b"AB-12\x00\xff"), "AB-12")
        self.assertEqual(car_modules.readable_detail(bytes.fromhex("01FE")), "01 FE")

    def test_details_holding_only_filler_are_left_out(self):
        for filler in (b"\xff" * 46, b"\x00" * 8, b"   ", b""):
            self.assertIsNone(car_modules.readable_detail(filler))

    def test_page_lists_each_computer_with_its_state(self):
        snapshot = {"vehicle": {"Module name": {"7E8": "ECM", "7E9": "TCM", "7EA": "OLD"}, "Calibration ID": {"7E8": "CAL"}, "Calibration check": {}},
                    "monitors": {"7E8": {}, "7E9": {}}, "codes": {"Stored": {"7E9": ["P0700"]}},
                    "ecu_details": {"7E8": [{"label": "Part number", "text": "P1"}]}}
        ecus = {ecu["header"]: ecu for ecu in obd_tool.page_ecus(snapshot, {"7E9"}, "en")}
        self.assertEqual(ecus["7E8"]["role"], "Engine computer")
        self.assertEqual((ecus["7E8"]["state"], ecus["7E9"]["state"], ecus["7EA"]["state"]), ("answering", "busy", "silent"))
        self.assertEqual(ecus["7EA"]["role"], "Computer 7EA")
        self.assertEqual(ecus["7E9"]["faults"], ["P0700"])
        self.assertIn({"label": "Part number", "text": "P1"}, ecus["7E8"]["rows"])
        self.assertIn({"label": "Calibration ID", "text": "CAL"}, ecus["7E8"]["rows"])

    def test_start_screen_offers_both_choices_and_is_the_first_page(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        self.assertIn('return PAGES[page] ? page : "home";', page)
        self.assertRegex(page, r'class="home-card" href="#/overview"')
        self.assertRegex(page, r'class="home-card" href="#/ecu"')


class CarCopyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "copies"
        patcher = mock.patch.object(car_copy, "copy_folder", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=None, interval=0, address=None))
        self.app.RETRY_SECONDS = 0.2

    async def wait_for(self, check, seconds=30):
        for _ in range(int(seconds / 0.02)):
            if check():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out")

    def test_recorder_files_replies_by_module_and_keeps_out_changes(self):
        recorder = car_copy.CarRecorder()
        recorder.record("ATSH7E0", ["OK"])
        recorder.record("22F187", ["7E8 03 62 F1 87"])
        recorder.record("ATSH7DF", ["OK"])
        recorder.record("010C1", ["7E8 04 41 0C 0D 4C"])
        recorder.record("04", ["7E8 01 44"])
        recorder.record("ATE0", ["OK"])
        recorder.record("0100", None)
        self.assertEqual(set(recorder.as_data()), {"7E0|22F187", "7DF|010C"})

    async def test_copy_plays_replies_in_turn_and_never_clears(self):
        copy = {"replies": {"7DF|010C": [["7E8 04 41 0C 0D 4C"], ["7E8 04 41 0C 10 00"]], "7DF|0D": [], "7DF|04": [["7E8 01 44"]]}}
        transport = car_copy.CopyTransport(copy)
        self.assertEqual(await transport.send("010C1"), ["7E8 04 41 0C 0D 4C"])
        self.assertEqual(await transport.send("010C"), ["7E8 04 41 0C 10 00"])
        self.assertEqual(await transport.send("010C"), ["7E8 04 41 0C 0D 4C"])
        self.assertEqual(await transport.send("04"), ["NO DATA"])
        self.assertEqual(await transport.send("0999"), ["NO DATA"])
        self.assertEqual(await transport.send("ATE0"), ["OK"])

    async def test_copy_builds_a_list_reply_from_single_answers(self):
        copy = {"replies": {"7DF|010C": [["7E8 04 41 0C 0D 4C"]], "7DF|010D": [["7E8 03 41 0D 32"]]}}
        lines = await car_copy.CopyTransport(copy).send("010C0D")
        self.assertEqual(obd_tool.parse_can(lines)["7E8"], bytes.fromhex("410C0D4C0D32"))

    def test_only_real_copies_can_be_opened(self):
        self.folder.mkdir(parents=True)
        good = {"format": car_copy.COPY_FORMAT, "version": 1, "replies": {"7DF|0100": [["NO DATA"]]}}
        (self.folder / "car_20260101_000000.json").write_text(json.dumps(good), encoding="utf-8")
        (self.folder / "car_20260101_000001.json").write_text("{not json", encoding="utf-8")
        (self.folder / "car_20260101_000002.json").write_text(json.dumps({**good, "replies": {"a": "b"}}), encoding="utf-8")
        self.assertEqual(car_copy.load_copy(self.folder, "car_20260101_000000.json")["file"], "car_20260101_000000.json")
        for name in ("car_20260101_000001.json", "car_20260101_000002.json", "../car_20260101_000000.json", "missing.json", None):
            with self.assertRaises(ValueError):
                car_copy.load_copy(self.folder, name)
        self.assertEqual([copy["file"] for copy in car_copy.list_copies(self.folder)], ["car_20260101_000000.json"])

    async def test_saved_copy_runs_the_app_without_touching_saved_cars(self):
        running = asyncio.create_task(self.app.run())
        try:
            await self.wait_for(lambda: self.app.elm is not None and self.app.reader is not None)
            await self.wait_for(lambda: self.app.reader.updates >= 3)
            status, saved = await self.app.api("POST", "/api/copy/save", {}, {"lang": "en"})
            self.assertEqual(status, 200)
            with mock.patch.object(garage, "save_car") as saving:
                status, _ = await self.app.api("POST", "/api/copy/open", {}, {"file": saved["file"], "lang": "en"})
                self.assertEqual(status, 200)
                await self.wait_for(lambda: self.app.elm is not None and self.app.elm.transport.transport.__class__ is car_copy.CopyTransport and self.app.reader is not None)
                await self.wait_for(lambda: self.app.reader.updates >= 3)
                state = self.app.state("en")
                self.assertTrue(state["connected"])
                self.assertEqual(state["copy"]["name"], None)
                self.assertFalse(state["can_save_copy"])
                self.assertEqual(state["vehicle"]["VIN"]["7E8"], demo.DEMO_VIN)
                self.assertIn("P0171", [c["code"] for c in state["codes"]["Stored"]["7E8"]])
                self.assertIn("Engine RPM", [v["name"] for v in state["values"]])
                saving.assert_not_called()
            status, reply = await self.app.api("POST", "/api/run", {}, {"action": "clear", "params": {"answer": "CLEAR"}, "lang": "en"})
            self.assertEqual(status, 400)
            self.assertIn("no car to clear", reply["error"])
            await self.app.api("POST", "/api/copy/close", {}, {})
            await self.wait_for(lambda: self.app.elm is not None and self.app.elm.transport.transport.__class__ is demo.DemoTransport)
            self.assertIsNone(self.app.state("en")["copy"])
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_save_waits_for_a_finished_read(self):
        status, reply = await self.app.api("POST", "/api/copy/save", {}, {"lang": "en"})
        self.assertEqual(status, 409)
        self.assertIn("Connect to a car", reply["error"])

    def test_copies_stay_out_of_the_repo(self):
        self.assertIn("car_copies/", (ROOT / ".gitignore").read_text(encoding="utf-8"))


class AnalysisTests(unittest.TestCase):
    """Patterns over a whole connection."""

    SNAPSHOT = {"codes": {"Stored": {}, "Pending": {}}, "test_results": []}

    def drive(self, seconds, step=0.5, **change):
        """Feed a run of readings. Each keyword is a pid name mapped to a value or a function of the time."""
        pids = {"rpm": 0x0C, "speed": 0x0D, "coolant": 0x05, "short": 0x06, "long": 0x07, "front": 0x14, "rear": 0x15,
                "volts": 0x42, "air": 0x10, "map": 0x0B, "baro": 0x33, "fuel": 0x2F}
        base = {"rpm": 800, "speed": 0, "coolant": 90, "short": 0.0, "long": 0.0, "volts": 14.0, **change}
        stats = car_analysis.DriveStats()
        ticks = int(seconds / step)
        for tick in range(ticks):
            now = tick * step
            values = {pids[name]: (value(now) if callable(value) else value) for name, value in base.items()}
            values.update({0x03: "Closed loop", 0x51: "Gasoline"})
            stats.update(values, {pid: now for pid in values}, now)
        return stats, values

    def by_id(self, stats, values, snapshot=None, previous=None):
        found = car_analysis.findings(stats, values, snapshot or self.SNAPSHOT, previous or {}, lambda monitor: f"M{monitor:02X}")
        return {item["id"]: item for item in found}

    def test_healthy_idle_is_reported_as_fine(self):
        stats, values = self.drive(60, front=lambda now: 0.1 + 0.7 * (int(now) % 2), rear=0.7)
        found = self.by_id(stats, values)
        self.assertEqual({found[key]["state"] for key in ("mixture", "idle", "temperature", "front_o2", "rear_o2", "charging")}, {"good"})
        self.assertNotIn("fuel_cap", found)
        self.assertEqual(found["trend"]["state"], "unknown")

    def test_lean_at_idle_only_points_to_an_air_leak(self):
        stats, values = self.drive(120, long=lambda now: 18.0 if now < 60 else 2.0, speed=lambda now: 0 if now < 60 else 60, air=10.0)
        item = self.by_id(stats, values)["mixture"]
        self.assertEqual(item["state"], "warn")
        self.assertIn("air leak after", item["text"])

    def test_rich_mixture_and_very_lean_mixture(self):
        stats, values = self.drive(60, long=-14.0)
        self.assertIn("removing", self.by_id(stats, values)["mixture"]["text"])
        stats, values = self.drive(60, long=20.0, short=8.0)
        self.assertEqual(self.by_id(stats, values)["mixture"]["state"], "bad")

    def test_mixture_text_names_only_what_was_seen(self):
        stats, values = self.drive(60, long=-3.0)
        item = self.by_id(stats, values)["mixture"]
        self.assertEqual((item["state"], item["values"]), ("good", {"value": "-3.0"}))

    def test_cold_engine_gives_no_mixture_verdict(self):
        stats, values = self.drive(60, coolant=40, long=20.0)
        found = self.by_id(stats, values)
        self.assertEqual(found["mixture"]["state"], "unknown")
        self.assertEqual(found["temperature"]["state"], "unknown")

    def test_rough_idle_and_stuck_sensors(self):
        stats, values = self.drive(60, rpm=lambda now: 800 + 220 * (int(now * 2) % 2), front=0.45, rear=1.2)
        found = self.by_id(stats, values)
        self.assertEqual((found["idle"]["state"], found["front_o2"]["state"], found["rear_o2"]["state"]), ("warn", "warn", "warn"))
        self.assertIn("above the 0 to 1 V", found["rear_o2"]["text"])

    def test_rear_sensor_is_judged_only_on_its_range(self):
        swing = lambda now: 0.1 + 0.8 * (int(now) % 2)
        stats, values = self.drive(60, front=swing, rear=swing)
        self.assertEqual(self.by_id(stats, values)["rear_o2"]["state"], "good")
        stats, values = self.drive(10, front=swing, rear=1.2)
        self.assertEqual(self.by_id(stats, values)["rear_o2"]["state"], "unknown")

    def test_revving_while_parked_is_not_a_rough_idle(self):
        stats = car_analysis.DriveStats()
        for tick in range(120):
            now = tick * 0.5
            rpm = 3000 if 20 <= now < 30 else 800
            values = {0x0C: rpm, 0x0D: 0, 0x05: 90, 0x49: 60.0 if rpm > 1000 else 14.9, 0x03: "Closed loop"}
            stats.update(values, {pid: now for pid in values}, now)
        self.assertEqual(car_analysis.check_idle(stats)["state"], "good")

    def test_pressure_is_only_compared_once_the_engine_has_settled(self):
        stats = car_analysis.DriveStats()
        for now, rpm, pressure, read in ((0, 800, 35, 0), (1, 0, 35, 0), (2, 0, 60, 2), (8, 0, 99, 8)):
            values = {0x0C: rpm, 0x0B: pressure, 0x33: 100}
            stats.update(values, {0x0C: now, 0x0B: read, 0x33: now}, now)
        self.assertEqual((stats.engine_off_gap.count, stats.engine_off_gap.average), (1, -1))

    def test_implausible_fuel_estimate_is_hidden(self):
        stats, _ = self.drive(3600, step=1, speed=60, air=0.5, rpm=2000)
        summary = {label: number for label, number, _ in car_analysis.drive_summary(stats)}
        self.assertIsNone(summary["Average fuel use (estimate)"])
        self.assertIsNone(summary["Fuel used (estimate)"])

    def test_thermostat_overheating_and_charging(self):
        stats, values = self.drive(1000, step=2, coolant=55, speed=50, air=10.0)
        self.assertIn("thermostat", self.by_id(stats, values)["temperature"]["text"])
        stats, values = self.drive(30, coolant=118)
        self.assertEqual(self.by_id(stats, values)["temperature"]["state"], "bad")
        stats, values = self.drive(700, step=2, volts=12.4)
        self.assertEqual(self.by_id(stats, values)["charging"]["state"], "warn")
        stats, values = self.drive(30, volts=lambda now: 12.6 if now < 15 else 14.1)
        self.assertEqual(self.by_id(stats, values)["charging"]["state"], "good")

    def test_engine_off_pressure_check(self):
        stats, values = self.drive(20, rpm=0, map=80, baro=100)
        self.assertEqual(self.by_id(stats, values)["map"]["state"], "warn")
        stats, values = self.drive(20, rpm=0, map=99, baro=100)
        self.assertEqual(self.by_id(stats, values)["map"]["state"], "good")
        stats, values = self.drive(10)
        self.assertNotIn("map", self.by_id(stats, values))

    def test_leak_code_after_refuelling_points_to_the_fuel_cap(self):
        snapshot = {"codes": {"Stored": {"7E8": []}, "Pending": {"7E8": ["P0455"]}}, "test_results": []}
        stats, values = self.drive(5, fuel=31.4)
        found = self.by_id(stats, values, snapshot, {"fuel": [8.2, 7.8]})
        self.assertIn("fuel was added recently", found["fuel_cap"]["text"])
        self.assertEqual((found["fuel_cap"]["values"]["before"], found["fuel_cap"]["values"]["now"]), (8, 31))
        self.assertEqual(found["pending"]["values"]["codes"], "P0455")
        self.assertIn("fuel cap is tight", self.by_id(stats, values, snapshot, {})["fuel_cap"]["text"])
        self.assertEqual([item["state"] for item in car_analysis.findings(stats, values, snapshot, {}, str)][:2], ["warn", "warn"])

    def test_self_test_margins_and_misfire_counts(self):
        result = lambda monitor, value, low, high, state="pass", **more: {"monitor": monitor, "test": 1, "value": value, "low": low, "high": high, "state": state, **more}
        check = lambda *results: {item["id"]: item for item in car_analysis.check_self_tests({"test_results": list(results)}, lambda monitor: f"M{monitor:02X}")}
        self.assertEqual(check(result(0x21, 0.5, 0, 1))["margins"]["state"], "good")
        self.assertEqual(check(result(0x21, 0.95, 0, 1))["margins"]["state"], "warn")
        self.assertEqual(check(result(0x21, 0, 0, 1))["margins"]["state"], "good", "zero against a top limit is the best result")
        self.assertEqual(check(result(0x21, 0.11, 0.1, 6553.5, open_high=True, open_low=False))["margins"]["state"], "warn")
        self.assertEqual(check(result(0x21, 5, 0.1, 6553.5, open_high=True, open_low=False))["margins"]["state"], "good")
        self.assertEqual(check(result(0x21, 2, 0, 1, "fail"))["margins"]["state"], "bad")
        found = check(result(0xA3, 4, 0, 100, open_low=True))
        self.assertEqual(found["misfires"]["values"]["names"], "MA3")
        self.assertEqual(car_analysis.check_self_tests({"test_results": []}, str), [])

    def test_change_since_earlier_visits_is_noticed(self):
        stats, values = self.drive(60, long=9.0)
        earlier = {"sessions": [{"averages": {"trim_idle": -3.0, "idle_rpm": 790}}, {"averages": {"trim_idle": -2.0, "idle_rpm": 800}}]}
        item = self.by_id(stats, values, previous=earlier)["trend"]
        self.assertEqual(item["state"], "warn")
        self.assertEqual((item["values"]["before"], item["values"]["now"]), ("-2.5", "9"))
        same = {"sessions": [{"averages": {"trim_idle": 8.0}}, {"averages": {"trim_idle": 9.5}}, {"averages": None}]}
        self.assertEqual(self.by_id(stats, values, previous=same)["trend"]["state"], "good")

    def test_drive_summary_adds_up(self):
        stats, values = self.drive(3600, step=1, speed=60, air=10.0, rpm=2000)
        summary = {label: number for label, number, _ in car_analysis.drive_summary(stats)}
        self.assertAlmostEqual(summary["Distance (estimate)"], 60, delta=0.1)
        self.assertAlmostEqual(summary["Average fuel use (estimate)"], 5.48, delta=0.05)
        self.assertEqual((summary["Hard accelerations"], summary["Hard brakings"]), (0, 0))
        stats, _ = self.drive(20, step=0.5, speed=lambda now: 0 if now < 5 else 40 if now < 10 else 0)
        self.assertEqual((stats.hard_accelerations, stats.hard_brakings), (1, 1))

    def test_held_values_and_long_gaps_are_not_counted(self):
        stats = car_analysis.DriveStats()
        values = {0x0C: 800, 0x0D: 60, 0x05: 90}
        stats.update(values, {0x0C: 1, 0x0D: 1, 0x05: 1}, 0)
        stats.update(values, {0x0C: 1, 0x0D: 1, 0x05: 1}, 600)
        self.assertEqual(stats.ranges[0x0C].count, 1)
        self.assertAlmostEqual(stats.distance_km, 60 * car_analysis.LONGEST_GAP_SECONDS / 3600)

    def test_page_shows_translated_findings_for_the_demo_car(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=3, interval=0, address=None))
        asyncio.run(app.connect())
        app.snapshot["codes"]["Pending"] = {"7E8": ["P0455"]}
        app.elm = app.reader.elm
        for lang in ("en", "ar"):
            analysis = obd_tool.page_analysis(app.stats, app.reader.values, app.snapshot, {"fuel": [10]}, lang)
            self.assertGreaterEqual(analysis["attention"], 2)
            self.assertNotIn("{", " ".join(item["text"] for item in analysis["findings"]))
            self.assertEqual(len(analysis["drive"]), 10)
        self.assertIn("غطاء خزان الوقود", [item["title"] for item in analysis["findings"]])

    def test_earlier_visits_include_fuel_from_older_records(self):
        record = {"sessions": [{"fuel": 40.0, "averages": {}}, "damaged"], "status": {"values": {"Fuel level": "7.8 %"}}}
        self.assertEqual(obd_tool.earlier_visits(record)["fuel"], [40.0, 7.8])
        self.assertEqual(obd_tool.earlier_visits({}), {"sessions": [], "fuel": [], "followup": [], "test_history": []})


class NewFeatureTests(unittest.IsolatedAsyncioTestCase):
    """The used-car check, fault follow-up, self-test trends, cold start, moment recorder, start test, fill-ups and adapter rating."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for target, name, value in ((garage, "CARS_DIR", self.folder / "cars"), (obd_tool, "LOG_DIR", self.folder / "logs"),
                                    (obd_tool, "REPORT_DIR", self.folder / "reports")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    SNAPSHOT = {"codes": {"Stored": {"7E8": []}, "Pending": {"7E8": []}, "Permanent": {"7E8": []}},
                "monitors": {"7E8": {"mil": False, "tests": [["Misfire", True], ["Catalyst", True]]}},
                "vehicle": {"VIN": {"7E8": "AAAAAAAAAHA000000"}}, "module_records": [{"codes": []}],
                "ecu_details": {"7E8": [{"label": "Stored VIN", "text": "AAAAAAAAAHA000000"}, {"label": "Last programmed", "text": "2017-04-05"}]},
                "test_results": []}

    def check(self, values=None, **changes):
        snapshot = {**self.SNAPSHOT, **changes}
        found = car_analysis.used_car_check(snapshot, values if values is not None else {0x31: "65535+", 0x30: "255+"}, 2017)
        return {item["id"]: item for item in found}

    def test_clean_car_passes_the_used_car_check(self):
        found = self.check()
        self.assertEqual({item["state"] for item in found.values()}, {"good"})
        self.assertEqual(set(found), {"cleared", "unfinished", "codes", "past", "vin", "programmed"})

    def test_recently_cleared_car_is_flagged(self):
        found = self.check({0x31: 12, 0x30: 2}, monitors={"7E8": {"mil": False, "tests": [["Misfire", True], ["Catalyst", False]]}})
        self.assertEqual((found["cleared"]["state"], found["unfinished"]["state"]), ("bad", "warn"))
        self.assertEqual(found["unfinished"]["names"], ["Catalyst"])
        self.assertEqual(list(found)[0], "cleared", "the worst sign comes first")

    def test_used_car_check_spots_swapped_parts_and_hidden_faults(self):
        found = self.check({0x31: 5000, 0x30: 200, 0x21: 340},
                           codes={"Stored": {}, "Pending": {}, "Permanent": {"7E8": ["P0420"]}},
                           module_records=[{"codes": [{"code": "P0455-00", "state": "history"}]}],
                           ecu_details={"7E8": [{"label": "Stored VIN", "text": "BBBBBBBBBHB111111"}, {"label": "Last programmed", "text": "2023-06-01"}]},
                           test_results=[{"state": "fail"}])
        self.assertEqual({key: found[key]["state"] for key in ("codes", "light_distance", "past", "vin", "programmed", "failed_tests")},
                         {"codes": "warn", "light_distance": "warn", "past": "warn", "vin": "bad", "programmed": "warn", "failed_tests": "bad"})
        self.assertIn("permanent", found["codes"]["text"])
        self.assertEqual(self.check(codes={"Stored": {"7E8": ["P0300"]}, "Pending": {}, "Permanent": {}})["codes"]["state"], "bad")
        self.assertEqual(self.check({})["cleared"]["state"], "unknown")

    def test_check_page_and_report_are_translated(self):
        values = {0x31: 12, 0x30: 2}
        snapshot = {**self.SNAPSHOT, "monitors": {"7E8": {"mil": True, "tests": [["Catalyst", False]]}}, "codes": {"Stored": {"7E8": ["P0300"]}}}
        for lang in ("en", "ar"):
            page = obd_tool.page_car_check(snapshot, values, lang)
            self.assertEqual((page["bad"], page["warn"]), (2, 1))
            self.assertNotIn("{", " ".join(item["text"] for item in page["findings"]))
        self.assertIn(i18n.ARABIC["Catalyst"], " ".join(item["text"] for item in page["findings"]))
        report = obd_tool.car_check_report(snapshot, values, "en")
        self.assertIn("[Check this] Codes cleared recently?", report)
        self.assertIn("cleared only 12 km and 2 warm-ups ago", report)
        only_distance = obd_tool.page_car_check(self.SNAPSHOT, {0x31: 1234}, "en")["findings"]
        self.assertIn("No sign of a recent clear: 1234 km since", " ".join(item["text"] for item in only_distance))
        self.assertIn("doesn't replace a mechanic's inspection", report)

    async def test_check_report_is_saved_but_not_in_demo(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=False, samples=None, interval=0, address=None))
        self.assertEqual((await app.api("POST", "/api/check/save", {}, {}))[0], 409)
        app.snapshot, app.reader = self.SNAPSHOT, SimpleNamespace(values={0x31: 12})
        status, saved = await app.api("POST", "/api/check/save", {}, {"lang": "en"})
        self.assertEqual(status, 200)
        self.assertIn("Used-car check", Path(saved["file"]).read_text(encoding="utf-8"))
        app.args.demo = True
        self.assertEqual((await app.api("POST", "/api/check/save", {}, {}))[0], 409)

    def test_fault_followup_tracks_a_code_through_pending_confirmed_and_gone(self):
        entry = lambda time, **kinds: {"time": time, "codes": sorted({c for found in kinds.values() for c in found}), **kinds,
                                       "modules": ["7E8"], "where": {c: "7E8" for found in kinds.values() for c in found}}
        record = {"history": [entry("2026-11-01T10:00:00", pending=["P0455"]), entry("2026-11-02T10:00:00", pending=["P0455"], stored=["P0300"]),
                              entry("2026-11-03T10:00:00", stored=["P0300"]), entry("2026-11-04T10:00:00", stored=["P0300"]),
                              {"time": "2026-11-05T10:00:00", "codes": ["P0300"]}]}
        cases = {case["code"]: case for case in garage.fault_followup(record)}
        self.assertEqual((cases["P0455"]["status"], cases["P0455"]["clean_readings"], cases["P0455"]["confirmed"]), ("gone", 2, False))
        self.assertEqual((cases["P0300"]["status"], cases["P0300"]["first_seen"][:10], cases["P0300"]["confirmed"]), ("seen", "2026-11-02", True))
        found = {item["id"]: item for item in car_analysis.check_followup({"followup": list(cases.values())})}
        self.assertEqual(found["followup_P0455"]["state"], "good")
        self.assertIn("never confirmed", found["followup_P0455"]["text"])
        self.assertEqual(found["followup_P0300"]["state"], "warn")
        self.assertEqual(garage.fault_followup({}), [])
        cleared = car_analysis.check_followup({"followup": [cases["P0455"]]}, {0x31: 5})[0]
        self.assertEqual(cleared["state"], "unknown")
        self.assertIn("isn't proof of a fix", cleared["text"])

    def test_code_is_not_called_gone_when_its_computer_was_silent(self):
        seen = {"time": "2026-11-01T10:00:00", "codes": ["P0700"], "stored": ["P0700"], "modules": ["7E8", "7E9"], "where": {"P0700": "7E8"}}
        silent = {"time": "2026-11-02T10:00:00", "codes": [], "stored": [], "modules": ["7E9"], "where": {}}
        answered = {"time": "2026-11-03T10:00:00", "codes": [], "stored": [], "modules": ["7E8", "7E9"], "where": {}}
        old = {"time": "2026-11-02T10:00:00", "codes": []}
        status = lambda *readings: (garage.fault_followup({"history": list(readings)})[0]["status"], garage.fault_followup({"history": list(readings)})[0]["clean_readings"])
        self.assertEqual(status(seen, silent), ("unclear", 0))
        self.assertEqual(status(seen, silent, answered), ("gone", 1))
        self.assertEqual(status(seen, old), ("unclear", 0))
        self.assertEqual(car_analysis.check_followup({"followup": garage.fault_followup({"history": [seen, silent]})}), [])

    def test_status_readings_keep_code_kinds_and_self_test_values(self):
        vehicle = {"VIN": {"7E8": "AAAAAAAAAHA000000"}}
        result = {"module": "7E8", "monitor": 0x21, "test": 0x85, "value": 0.4, "state": "pass"}
        data = {"active_codes": ["P0455"], "codes": {"Stored": {"7E8": []}, "Pending": {"7E8": ["P0455"]}}, "test_results": [result, {"state": "not_run"}]}
        garage.remember(vehicle, "status", data)
        garage.remember(vehicle, "status", data)
        record = garage.load_car(garage.car_key(vehicle))
        self.assertEqual(record["history"][-1]["pending"], ["P0455"])
        self.assertEqual((record["history"][-1]["modules"], record["history"][-1]["where"]), (["7E8"], {"P0455": "7E8"}))
        self.assertEqual([entry["values"] for entry in record["test_history"]], [{"7E8:21:85": 0.4}], "two readings in one visit count once")

    def test_self_test_sliding_toward_its_limit_is_noticed(self):
        result = {"module": "7E8", "monitor": 0x21, "test": 0x85, "value": 0.9, "low": 0, "high": 1, "state": "pass"}
        snapshot = {"test_results": [result]}
        history = lambda *values: {"test_history": [{"values": {"7E8:21:85": value}} for value in values]}
        name = lambda monitor: f"M{monitor:02X}"
        self.assertEqual(car_analysis.check_test_trends(snapshot, history(0.3, 0.5, 0.9), name)["state"], "warn")
        self.assertEqual(car_analysis.check_test_trends(snapshot, history(0.88, 0.9, 0.9), name)["state"], "good")
        self.assertEqual(car_analysis.check_test_trends(snapshot, history(0.3, 0.9), name)["state"], "unknown")
        self.assertIsNone(car_analysis.check_test_trends({"test_results": []}, history(1, 2, 3), name))

    def cold_drive(self, rear=0.7, warm_at=300):
        stats = car_analysis.DriveStats()
        for second in range(0, 400, 2):
            values = {0x0C: 900, 0x0D: 0, 0x05: 20 + 50 * min(second / warm_at, 1.2), 0x1F: second + 5, 0x15: rear,
                      0x03: "Closed loop" if second >= 40 else "Open loop (cold)"}
            stats.update(values, {pid: second for pid in values}, second)
        return stats

    def test_cold_start_profile(self):
        stats = self.cold_drive()
        self.assertEqual((stats.cold_start, stats.closed_after, round(stats.warm_after / 60)), (True, 45, 5))
        self.assertEqual(car_analysis.check_cold_start(stats, {})["state"], "good")
        self.assertEqual(stats.averages()["cold"]["warm_after"], stats.warm_after)
        earlier = {"sessions": [{"averages": {"cold": {"warm_after": 150}}}, {"averages": {"cold": {"warm_after": 160}}}]}
        self.assertEqual(car_analysis.check_cold_start(stats, earlier)["state"], "warn")
        high = car_analysis.check_cold_start(self.cold_drive(rear=1.2), {})
        self.assertEqual((high["state"], high["values"]["value"]), ("warn", "1.20"))
        warm = car_analysis.DriveStats()
        warm.update({0x0C: 800, 0x05: 90, 0x1F: 600}, {}, 0)
        self.assertEqual(car_analysis.check_cold_start(warm, {})["state"], "unknown")
        self.assertIsNone(warm.averages()["cold"])

    async def test_moment_recorder_saves_readings_around_a_new_code(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=False, samples=None, interval=0, address=None))
        app.snapshot = {"codes": {"Stored": {"7E8": []}, "Pending": {"7E8": []}}, "test_results": []}
        app.reader = SimpleNamespace(values={0x0C: 800.0, 0x0D: 0}, read_at={})
        elm = SimpleNamespace(demo=False)
        with mock.patch.object(obd_tool, "MOMENT_SECONDS", 0.2):
            app.watch_moment(elm)
            self.assertIsNone(app.moment, "what was there at the start isn't news")
            app.snapshot["codes"]["Pending"]["7E8"] = ["P0455"]
            app.watch_moment(elm)
            self.assertEqual(app.moment["reason"], ["Faults seen once", "Fuel cap", "P0455"])
            await asyncio.sleep(0.25)
            app.reader.values[0x0C] = 950.0
            app.watch_moment(elm)
        self.assertIsNone(app.moment)
        rows = Path(app.moments[0]["file"]).read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(rows), 4)
        self.assertIn("950.0", rows[-1])
        app.watch_moment(elm)
        self.assertEqual(len(app.moments), 1, "the same trouble isn't saved twice")

    async def test_moment_recorder_writes_nothing_for_demo_or_copies(self):
        app = obd_tool.DashboardApp(SimpleNamespace(demo=False, samples=None, interval=0, address=None))
        app.reader = SimpleNamespace(values={0x0C: 800.0}, read_at={})
        app.recent.append((0, "now", {0x0C: 800.0}))
        app.moment = {"reason": ["P0455"], "until": 0}
        app.save_moment(SimpleNamespace(demo=True))
        self.assertEqual((app.moments, (self.folder / "logs").exists()), ([], False))

    async def start_test(self, volts, car="0B B8", rpm_after="1F 40"):
        """Run the start test against a scripted adapter. `volts` are the adapter's readings in turn."""
        readings = list(volts)

        class Transport:
            asked = 0

            async def send(self, command, wait=5):
                if command == "ATRV":
                    return [f"{readings.pop(0) if len(readings) > 1 else readings[0]}V"]
                if command.startswith("010C"):
                    Transport.asked += 1
                    return ["7E8 04 41 0C 00 00" if Transport.asked == 1 else f"7E8 04 41 0C {rpm_after}"]
                if command.startswith("0142"):
                    return [f"7E8 04 41 42 {car}"]
                return ["OK"] if command.startswith("AT") else ["NO DATA"]

        output = io.StringIO()
        with mock.patch.object(obd_tool, "START_TEST_SECONDS", 0.05), contextlib.redirect_stdout(output):
            await obd_tool.run_start_test(ELM(Transport()), None)
        return output.getvalue()

    async def test_start_test_corrects_the_adapter_reading_and_judges_the_dip(self):
        # The adapter reads 1 V above the car (13.0 against 12.0), so a dip to 10.9 at the adapter is 9.9 at the battery.
        text = await self.start_test([13.0, 13.0, 13.0, 13.0, 12.9, 10.9, 14.8], car="2E E0")
        self.assertIn("Resting voltage: 12.0 V", text)
        self.assertIn("Lowest voltage while starting: 9.9 V", text)
        self.assertIn("held up well", text)
        self.assertIn("Voltage with the engine running: 13.8 V", text)
        self.assertIn("probably weak", await self.start_test([12.4, 12.4, 12.4, 12.4, 8.1, 14.0], car="30 70"))
        self.assertIn("No start was seen", await self.start_test([12.4], car="30 70", rpm_after="00 00"))

    async def test_start_test_refuses_with_the_engine_running(self):
        class Transport:
            async def send(self, command, wait=5):
                return ["7E8 04 41 0C 0C 80"] if command.startswith("010C") else ["OK"]

        with self.assertRaises(OBDError):
            await obd_tool.run_start_test(ELM(Transport()), None)

    def test_fill_ups_are_noticed_between_visits_and_tank_size_is_kept(self):
        vehicle = {"VIN": {"7E8": "AAAAAAAAAHA000000"}}
        key = garage.car_key(vehicle)
        garage.save_session(vehicle, {"id": "a", "started": "2026-11-01T10:00:00", "fuel": 20.0, "fuel_end": 8.0})
        garage.save_session(vehicle, {"id": "a", "started": "2026-11-01T10:00:00", "fuel": 20.0, "fuel_end": 7.8})
        garage.save_session(vehicle, {"id": "b", "started": "2026-11-02T10:00:00", "fuel": 31.4, "fuel_end": 31.0})
        garage.save_session(vehicle, {"id": "b", "started": "2026-11-02T10:00:00", "fuel": 31.4, "fuel_end": 30.0})
        garage.save_session(vehicle, {"id": "c", "started": "2026-11-03T10:00:00", "fuel": 29.0})
        self.assertEqual(garage.load_car(key)["refuels"], [{"time": "2026-11-02T10:00:00", "before": 7.8, "after": 31.4}])
        self.assertTrue(garage.set_tank(key, 45))
        self.assertEqual(garage.load_car(key)["tank_litres"], 45)
        self.assertTrue(garage.set_tank(key, None))
        self.assertNotIn("tank_litres", garage.load_car(key))
        self.assertFalse(garage.set_tank("NOCAR", 45))

    async def test_tank_size_must_be_sensible(self):
        garage.save_car("CAR1", {"vehicle": {}})
        app = obd_tool.DashboardApp(SimpleNamespace(demo=False, samples=None, interval=0, address=None))
        for litres, status in ((45, 200), (5, 400), ("45", 400), (True, 400), (None, 200)):
            self.assertEqual((await app.api("POST", "/api/car/tank", {}, {"key": "CAR1", "litres": litres}))[0], status, litres)

    def test_adapter_rating(self):
        def rating(sent, lost, grouping, size=6, seconds=0.1):
            transport = SimpleNamespace(replied=sent - lost, timeouts=lost, sent=sent, total_seconds=seconds * (sent - lost))
            found = obd_tool.adapter_rating(SimpleNamespace(transport=transport, can_group=grouping, group_size=size))
            return found and (found[0], found[1].split(":")[0])

        self.assertIsNone(rating(20, 0, True))
        self.assertEqual(rating(1000, 2, True), ("good", "Fast"))
        self.assertEqual(rating(1000, 2, True, size=3, seconds=0.22), ("", "Average"))
        self.assertEqual(rating(1000, 2, False), ("bad", "Slow"))
        self.assertEqual(rating(1000, 60, True), ("bad", "Unreliable"))


class RobustnessTests(unittest.IsolatedAsyncioTestCase):
    """Rare failures that used to leave the app stuck or the records wrong."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for target, name, value in ((garage, "CARS_DIR", self.folder / "cars"), (obd_tool, "LOG_DIR", self.folder / "logs")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        copies = mock.patch.object(car_copy, "copy_folder", return_value=self.folder / "copies")
        copies.start()
        self.addCleanup(copies.stop)
        self.app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=None, interval=0, address=None))
        self.app.RETRY_SECONDS = 0.2

    async def wait_for(self, check, seconds=30):
        for _ in range(int(seconds / 0.02)):
            if check():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out")

    async def test_bridge_answers_even_when_a_request_fails(self):
        self.app.api = mock.AsyncMock(side_effect=OSError("disk full"))
        reply = await bridge.answer(self.app, '{"id": 5, "kind": "api", "payload": {"method": "POST", "path": "/api/x"}}')
        self.assertEqual((reply["id"], reply["status"], reply["data"]["error"]), (5, 500, "disk full"))

    def test_car_file_that_is_not_a_record_is_set_aside(self):
        garage.CARS_DIR.mkdir(parents=True)
        (garage.CARS_DIR / "CAR1.json").write_text("null", encoding="utf-8")
        self.assertEqual(garage.load_car("CAR1"), {})
        self.assertTrue((garage.CARS_DIR / "CAR1.damaged").exists())
        self.assertEqual(garage.summaries(), [])

    async def test_saved_cars_and_copies_can_be_deleted(self):
        garage.save_car("CAR1", {"vehicle": {}})
        self.app.args.demo = False
        status, _ = await self.app.api("POST", "/api/car/delete", {}, {"key": "CAR1"})
        self.assertEqual(status, 200)
        self.assertFalse((garage.CARS_DIR / "CAR1.json").exists())
        for key in ("CAR1", "../CAR1", ""):
            self.assertEqual((await self.app.api("POST", "/api/car/delete", {}, {"key": key}))[0], 404)
        self.app.args.demo = True
        name = car_copy.save_copy(self.folder / "copies", car_copy.CarRecorder(), None, None)
        self.app.copy = {"file": name}
        status, reply = await self.app.api("POST", "/api/copy/delete", {}, {"file": name, "lang": "en"})
        self.assertEqual(status, 409)
        self.app.copy = None
        self.assertEqual((await self.app.api("POST", "/api/copy/delete", {}, {"file": name}))[0], 200)
        self.assertEqual(car_copy.list_copies(self.folder / "copies"), [])
        self.assertEqual((await self.app.api("POST", "/api/copy/delete", {}, {"file": "../x.json"}))[0], 409)

    async def test_closing_a_copy_after_disconnect_stays_disconnected(self):
        running = asyncio.create_task(self.app.run())
        try:
            await self.wait_for(lambda: self.app.elm is not None and self.app.reader is not None)
            _, saved = await self.app.api("POST", "/api/copy/save", {}, {})
            self.app.disconnect("en")
            await self.wait_for(lambda: self.app.link_state == "off")
            await self.app.api("POST", "/api/copy/open", {}, {"file": saved["file"]})
            await self.wait_for(lambda: self.app.elm is not None and self.app.reader is not None)
            await self.app.api("POST", "/api/copy/close", {}, {})
            await self.wait_for(lambda: self.app.link_state == "off")
            await asyncio.sleep(0.6)
            self.assertIsNone(self.app.elm)
            self.assertEqual(self.app.state("en")["link"], "off")
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_short_refusal_does_not_crash(self):
        class Transport:
            async def send(self, command, wait=5):
                return ["7E8 02 7F 22"] if command == "22F187" else ["OK"]

        reply = await ELM(Transport()).module_request("7E0", "7E8", "22F187")
        self.assertEqual(reply, bytes.fromhex("7F22"))
        self.assertIsNone(car_modules.refusal_reason(reply))

    async def test_clear_stays_done_when_the_car_goes_quiet_afterwards(self):
        elm = ELM(demo.DemoTransport())
        elm.demo = True
        job = self.app.new_job("clear", {"answer": "CLEAR"}, "en")
        with mock.patch.object(obd_tool, "run_clear", mock.AsyncMock()), \
                mock.patch.object(obd_tool, "read_snapshot", mock.AsyncMock(side_effect=OBDError(obd_tool.CAR_SILENT))):
            with self.assertRaises(OBDError):
                await self.app.run_job(elm, job)
        self.assertEqual(job.status, "done")

    async def test_adapter_restart_during_a_command_reconnects(self):
        elm = ELM(demo.DemoTransport())
        elm.demo = True
        job = self.app.new_job("info", {}, "en")
        with mock.patch.object(obd_tool, "run_info", mock.AsyncMock(side_effect=OBDError(obd_tool.ADAPTER_RESTARTED))):
            with self.assertRaises(OBDError):
                await self.app.run_job(elm, job)
        self.assertEqual(job.status, "failed")
        ordinary = self.app.new_job("info", {}, "en")
        with mock.patch.object(obd_tool, "run_info", mock.AsyncMock(side_effect=OBDError("Not clearing: the engine must be off with the ignition ON."))):
            await self.app.run_job(elm, ordinary)
        self.assertEqual(ordinary.status, "failed")

    async def test_failed_download_fails_the_command_not_the_connection(self):
        elm = ELM(demo.DemoTransport())
        elm.demo = True
        job = self.app.new_job("profile", {"name": "kia-nothere", "year": None}, "en")
        with mock.patch.object(car_modules, "local_profile_files", return_value=[]), \
                mock.patch.object(car_modules, "download_profile", side_effect=OSError("no internet")):
            await self.app.run_job(elm, job)
        self.assertEqual(job.status, "failed")
        self.assertIn("no internet", job.output)

    async def test_recording_keeps_earlier_files_and_starts_with_new_readings_only(self):
        now = time.monotonic()
        self.app.elm = object()
        self.app.reader = SimpleNamespace(values={0x0C: 800.0, 0x0D: 0}, read_at={0x0C: now, 0x0D: now})
        fixed = mock.Mock(wraps=obd_tool.datetime)
        fixed.now.return_value = obd_tool.datetime(2026, 1, 1, 12, 0, 0)
        with mock.patch.object(obd_tool, "datetime", fixed):
            self.assertIsNone(self.app.start_recording("en"))
            first = self.app.recording["path"]
            self.app.record_row()
            self.app.reader.read_at[0x0C] = now + 1
            self.app.record_row()
            self.app.stop_recording()
            self.assertIsNone(self.app.start_recording("en"))
            second = self.app.recording["path"]
            self.app.stop_recording()
        self.assertNotEqual(first, second)
        rows = first.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(rows[1].split(",")[1:], ["", ""])
        self.assertEqual(rows[2].split(",")[1:], ["800.0", ""])

    async def test_freeze_frame_goes_when_the_stored_code_goes(self):
        self.app.snapshot = {"monitors": {}, "codes": {"Stored": {"7E8": ["P0171"]}, "Pending": {}, "Permanent": {}}, "supported": set(),
                             "this_drive": None, "freeze": {"code": "P0171", "values": []}, "vehicle": {}}
        elm = SimpleNamespace(demo=True, vehicle=None,
                              request_all=mock.AsyncMock(side_effect=[{}, {"7E8": bytes.fromhex("4300")}, {}, {}]))
        with mock.patch.object(obd_tool, "remember_snapshot"):
            await self.app.refresh_status(elm)
        self.assertIsNone(self.app.snapshot["freeze"])

    async def test_refresh_does_not_save_over_a_newer_reading(self):
        old = {"monitors": {}, "codes": {"Stored": {"7E8": ["P0171"]}, "Pending": {}, "Permanent": {}}, "supported": set(),
               "this_drive": None, "freeze": None, "vehicle": {}}
        self.app.snapshot = old
        elm = SimpleNamespace(demo=True, vehicle=None, request_all=mock.AsyncMock(return_value={"7E8": bytes.fromhex("4300")}))
        with mock.patch.object(obd_tool, "remember_snapshot") as remember:
            parts = self.app.refresh_parts(elm)
            await parts.__anext__()
            self.app.snapshot = dict(old)
            async for _ in parts:
                pass
            remember.assert_not_called()


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = obd_tool.DashboardApp(SimpleNamespace(demo=True, samples=None, interval=0, address=None))
        self.app.RETRY_SECONDS = 0.2

    async def wait_for(self, check, seconds=30):
        for _ in range(int(seconds / 0.02)):
            if check():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out")

    async def test_disconnect_stops_saves_and_stays_off_until_asked(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        logs = mock.patch.object(obd_tool, "LOG_DIR", Path(folder.name) / "new" / "logs")
        logs.start()
        self.addCleanup(logs.stop)
        running = asyncio.create_task(self.app.run())
        try:
            await self.wait_for(lambda: self.app.elm is not None and self.app.reader is not None)
            elm = self.app.elm
            self.assertIsNone(self.app.start_recording("en"))
            status, reply = await self.app.api("POST", "/api/disconnect", {}, {"lang": "en"})
            self.assertEqual((status, reply["link"]), (200, "stopping"))
            await self.wait_for(lambda: self.app.link_state == "off")
            self.assertIsNone(self.app.recording)
            self.assertIsNotNone(self.app.last_summary)
            state = self.app.state("en")
            self.assertFalse(state["connected"])
            self.assertEqual(state["link"], "off")
            self.assertIn("safe to unplug", state["message"])
            sent = elm.transport.sent
            await asyncio.sleep(0.5)
            self.assertEqual(elm.transport.sent, sent, "nothing more may be sent to the car")
            self.assertIsNone(self.app.elm)
            status, _ = await self.app.api("POST", "/api/connect", {}, {})
            self.assertEqual(status, 200)
            await self.wait_for(lambda: self.app.elm is not None)
            self.assertEqual(self.app.state("en")["link"], "on")
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_disconnect_during_a_retry_wait_needs_no_connection(self):
        self.app.connect = mock.AsyncMock(side_effect=OBDError("Adapter not found. Check it is plugged in, the ignition is ON, and no phone is connected to it."))
        self.app.RETRY_SECONDS = 30
        running = asyncio.create_task(self.app.run())
        try:
            await self.wait_for(lambda: self.app.connect.await_count == 1 and self.app.connecting is None)
            self.assertIsNone(self.app.disconnect("en"))
            await self.wait_for(lambda: self.app.link_state == "off", 3)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_disconnect_waits_for_a_clear_to_finish(self):
        job = self.app.new_job("clear", {"answer": "CLEAR"}, "en")
        job.status = "running"
        status, reply = await self.app.api("POST", "/api/disconnect", {}, {"lang": "en"})
        self.assertEqual(status, 409)
        self.assertIn("being cleared", reply["error"])
        self.assertEqual(self.app.link_state, "on")

    async def test_disconnect_closes_the_adapter_and_lets_go_of_bluetooth(self):
        char = lambda *properties: SimpleNamespace(properties=list(properties))
        client = mock.AsyncMock()
        client.__aenter__.return_value = client
        client.services = [SimpleNamespace(uuid="0000fff0-0000", characteristics=[char("notify"), char("write")])]
        scanner = mock.Mock(find_device_by_filter=mock.AsyncMock(return_value=SimpleNamespace(name="OBD", address="AA")))
        talking = asyncio.Event()

        async def stay_connected(elm, _):
            talking.set()
            await asyncio.Event().wait()

        args = SimpleNamespace(address=None)
        with mock.patch("bleak.BleakClient", return_value=client), mock.patch("bleak.BleakScanner", scanner), \
                mock.patch.object(obd_tool, "run_battery", stay_connected):
            running = asyncio.create_task(obd_tool.connect_and_run(args, stay_connected))
            await asyncio.wait_for(talking.wait(), 5)
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        written = [call.args[1] for call in client.write_gatt_char.await_args_list]
        self.assertTrue(any(b"ATPC" in data for data in written), written)
        client.__aenter__.assert_awaited_once()
        client.__aexit__.assert_awaited_once()

    async def test_closing_the_app_still_ends_the_run(self):
        running = asyncio.create_task(self.app.run())
        await self.wait_for(lambda: self.app.elm is not None and self.app.reader is not None)
        self.app.disconnect("en")
        running.cancel()
        results = await asyncio.gather(running, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)


class CarSystemTests(unittest.TestCase):
    def test_every_live_value_has_a_system(self):
        self.assertEqual([pid for pid in LIVE_PIDS if pid not in car_systems.VALUE_SYSTEMS], [])

    def test_codes_go_to_the_right_system(self):
        for code, system in (("P0700", "transmission"), ("P0741-00", "transmission"), ("P0300", "engine"), ("P0171", "fuel_air"), ("P0455", "exhaust"),
                             ("P2003", "exhaust"), ("P0562", "electrical"), ("C0035", "brakes"), ("B0001", "body"), ("U0100", "network"), ("P1234", "engine")):
            self.assertEqual(car_systems.code_system(code), system, code)

    def test_every_system_name_is_translated_and_on_the_page(self):
        page = bridge.PAGE.read_text(encoding="utf-8")
        for system_id, name, what in car_systems.SYSTEMS:
            self.assertIn(name, i18n.ARABIC)
            self.assertIn(what, i18n.ARABIC)
            self.assertIn(f'{system_id}: "{name}"', page)
            self.assertRegex(page, rf'\b{system_id}: "M')

    def test_page_sorts_values_and_faults_into_systems(self):
        snapshot = {"vehicle": {"Module name": {"7E8": "ECM", "7E9": "TCM"}}, "more": [{"name": "Transmission actual gear", "text": "3", "path": "Engine.Generic"}],
                    "codes": {"Stored": {"7E8": ["P0700"]}, "Pending": {"7E8": []}}, "monitors": {"7E8": {"tests": [["Misfire", True], ["Catalyst", False]]}},
                    "module_records": [{"hdr": "7E0", "reply_id": "7E8", "codes": [{"code": "P0455-00", "state": "history"}]}],
                    "test_results": [{"module": "7E8", "monitor": 0x21, "test": 1, "value": 1, "low": 0, "high": 2, "unit": "", "state": "pass"}]}
        reader = SimpleNamespace(values={0x0C: 800, 0x0D: 0}, voltage="14.1V", age_note=lambda pid, lang=None: None)
        systems = {system["id"]: system for system in obd_tool.page_systems(snapshot, reader, "en")}
        self.assertEqual([f["code"] for f in systems["transmission"]["faults"]], ["P0700"])
        self.assertTrue(systems["transmission"]["faults"][0]["now"])
        self.assertFalse(systems["exhaust"]["faults"][0]["now"])
        self.assertIn("Transmission actual gear", [v["name"] for v in systems["transmission"]["values"]])
        self.assertIn("7E9 TCM", [v["text"] for v in systems["transmission"]["values"]])
        self.assertIn("Engine RPM", [v["name"] for v in systems["engine"]["values"]])
        self.assertIn("Speed", [v["name"] for v in systems["driving"]["values"]])
        self.assertEqual(systems["exhaust"]["checks"][0]["name"], "Catalyst")
        self.assertEqual(len(systems["exhaust"]["results"]), 1)


if __name__ == "__main__":
    unittest.main()
