"""Offline coverage for scripts/read-kmsg-ring.py: no adb, no device.

Every device interaction is replaced by a fake that records the commands it was
given, so the pull path can be checked for what it does *not* do (no push, no
reboot, no dd on the device, no write to a partition) as well as for what it
does. `REAL_START_833` and `REAL_START_838` are the first bytes of the two
records actually captured from this device; the synthetic builder mirrors their
layout.
"""

import hashlib
import importlib.util
import io
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "read_kmsg_ring",
    Path(__file__).resolve().parents[1] / "scripts" / "read-kmsg-ring.py")
ring_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ring_mod)

SLOT = ring_mod.SLOT_BYTES
COUNT = ring_mod.SLOT_COUNT
TOTAL = ring_mod.RING_BYTES
SERIAL = "d25f844e"
MAGIC = ring_mod.MAGIC

# Verbatim from /dev/block/sda15: slot 0 (seq 833) and slot 4 (seq 838).
REAL_START_833 = (
    b'A\x03\x00\x00\x00]\x00]\n\n# Index: 833\t\n'
    b'## Build: #1 SMP PREEMPT Thu Jul 21 01:36:17 CST 2022\t\n'
    b'## Reason: Restart\n'
    b'### 2026-10-28 09:27:34\n'
    b'#### Log type: Kmsg\t\n```\t\n'
    b'<6>[    0.000000] Powerup reason=0x80001\n')
REAL_START_838 = (
    b'F\x03\x00\x00\x00]\x00]\n```\n# Index: 838\t\n'
    b'## Build: #1 SMP PREEMPT Thu Jul 21 01:36:17 CST 2022\t\n'
    b'## Reason: Restart\n'
    b'### 2026-10-28 12:39:28\n'
    b'#### Log type: Kmsg\t\n```\t\n')

CMD_LINE = (
    b"ramoops_memreserve=4M console=ttyMSM0,115200,n8 "
    b"block2mtd.block2mtd=/dev/block/sda15,2097152 mtdoops.mtddev=0 "
    b"mtdoops.record_size=2097152 printk.always_kmsg_dump=1")
MTD0 = b'mtd0: 01000000 00200000 "block2mtd: /dev/block/sda15"'
# A real dd writes an 84-byte summary. Measured on the device: an unsuppressed
# exec-out capture came back as 16777300 = 16777216 + 84, with the summary
# sitting at the end of the raw file.
# Exactly the 84-byte overshoot measured on the device.
DD_SUMMARY = (
    b"256+0 records in\n256+0 records out\n"
    b"134217728 bytes (128 MiB) copied, 1.234s, 9 MB/s\n")
if len(DD_SUMMARY) != 84:  # keep the fixture honest about the measurement
    raise AssertionError(len(DD_SUMMARY))


def record(seq, index=None, time=b"2026-10-28 12:39:28", powerup=b"0x40001",
           reboot=True, magic=MAGIC, fill=b"", body_bytes=None,
           header_prefix=b"\n```\n"):
    """A record shaped like the measured ones, not like a guess."""
    if index is None:
        index = seq
    head = (
        header_prefix
        + b"# Index: %d\t\n" % index
        + b"## Build: #1 SMP PREEMPT Thu Jul 21 01:36:17 CST 2022\t\n"
        + b"## Reason: Restart\n"
        + b"### " + time + b"\n"
        + b"#### Log type: Kmsg\t\n```\t\n"
    )
    text = (
        b"<6>[    0.000000] Powerup reason=" + powerup + b"\n"
        + b"<6>[    0.000000] Machine model: Qualcomm Technologies, Inc. Gauguin\n"
        + b"<6>[    0.000000] Linux version 4.19.113-perf-g1364bad74330 #1 SMP"
        b" PREEMPT Thu Jul 21 01:36:17 CST 2022\n"
    )
    text += fill
    if reboot:
        text += (b"<0>[  436.883277] reboot: Restarting system with command "
                 b"'bootloader'\n")
    blob = ((seq & 0xFFFFFFFF).to_bytes(4, "little")
            + magic.to_bytes(4, "little") + head + text)
    if body_bytes is not None:
        blob += body_bytes
    if len(blob) > SLOT:
        raise AssertionError("synthetic record exceeds a slot")
    return blob + b"\x00" * (SLOT - len(blob))


def erased():
    return b"\xff" * SLOT


def ring_of(*slots):
    if len(slots) > COUNT:
        raise AssertionError("too many slots")
    return b"".join(list(slots) + [erased()] * (COUNT - len(slots)))


class Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeAdb:
    """Records every command; answers only the four read-only ones allowed."""

    def __init__(self, size=TOTAL, cmdline=CMD_LINE, mtd=MTD0, payload=None,
                 dd_rc=0, blockdev_rc=0, short=False, timeout_on=None,
                 extra=b""):
        self.shell_calls = []
        self.exec_calls = []
        self.size = size
        self.cmdline = cmdline
        self.mtd = mtd
        self.payload = payload if payload is not None else ring_of()
        self.dd_rc = dd_rc
        self.blockdev_rc = blockdev_rc
        self.short = short
        self.timeout_on = timeout_on
        self.extra = extra

    def shell(self, command):
        self.shell_calls.append(command)
        if self.timeout_on == "shell":
            raise subprocess.TimeoutExpired(cmd="adb shell " + command,
                                            timeout=60)
        if command.startswith("blockdev --getsize64"):
            if self.blockdev_rc:
                return Completed(self.blockdev_rc, b"", b"blockdev: not found")
            return Completed(0, b"%d\n" % self.size)
        if command == "cat /proc/cmdline":
            return Completed(0, self.cmdline)
        if command == "cat /proc/mtd":
            return Completed(0, self.mtd)
        return Completed(1, b"", b"unexpected: " + command.encode())

    def exec_out(self, command):
        self.exec_calls.append(command)
        if self.timeout_on == "exec":
            raise subprocess.TimeoutExpired(cmd="adb exec-out " + command,
                                            timeout=60)
        body = self.payload[:-4096] if self.short else self.payload
        body += self.extra
        # exec-out fuses stdout and stderr; only an in-command redirect stops
        # dd's summary from landing at the end of the byte stream.
        if self.dd_rc != 0:
            return Completed(self.dd_rc, body if "2>/dev/null" in command
                             else body + DD_SUMMARY, b"dd failed")
        return Completed(0, body if "2>/dev/null" in command
                         else body + DD_SUMMARY, b"")

    def all_argv(self):
        return list(self.shell_calls) + list(self.exec_calls)


class RealBytes(unittest.TestCase):
    """The two captured headers carry seq and magic as <II>."""

    def test_real_headers_are_seq_plus_magic(self):
        for raw, seq in ((REAL_START_833[:8], 833), (REAL_START_838[:8], 838)):
            self.assertEqual(len(raw), 8)
            self.assertEqual(int.from_bytes(raw[0:4], "little"), seq)
            self.assertEqual(int.from_bytes(raw[4:8], "little"), MAGIC)

    def test_real_record_starts_parse(self):
        for raw, seq in ((REAL_START_833, 833), (REAL_START_838, 838)):
            padded = raw + b"\x00" * (SLOT - len(raw))
            parsed = ring_mod.parse_slot(padded, 0, 0)
            self.assertEqual(parsed["state"], "parsed")
            self.assertEqual(parsed["seq"], seq)
            self.assertEqual(parsed["record_index"], seq)
            self.assertTrue(parsed["header_index_matches_seq"])
            self.assertEqual(parsed["header_magic"], "0x5d005d00")

    def test_real_833_keeps_its_powerup_reason(self):
        padded = REAL_START_833 + b"\x00" * (SLOT - len(REAL_START_833))
        self.assertEqual(ring_mod.parse_slot(padded, 0, 0)["powerup_reason"],
                         "0x80001")

    def test_real_838_keeps_its_index_and_time(self):
        padded = REAL_START_838 + b"\x00" * (SLOT - len(REAL_START_838))
        parsed = ring_mod.parse_slot(padded, 0, 0)
        self.assertEqual(parsed["record_index"], 838)
        self.assertEqual(parsed["time"], "2026-10-28 12:39:28")


class HeaderValidation(unittest.TestCase):
    def test_seq_zero_is_a_valid_sequence(self):
        parsed = ring_mod.parse_slot(record(0, index=0), 0, 0)
        self.assertEqual(parsed["state"], "parsed")
        self.assertEqual(parsed["seq"], 0)

    def test_seq_above_65535_is_valid(self):
        for seq in (0x10000, 0x7FFFFFFF, 0xFFFFFFFE):
            parsed = ring_mod.parse_slot(record(seq), 0, 0)
            self.assertEqual(parsed["state"], "parsed", seq)
            self.assertEqual(parsed["seq"], seq)

    def test_erased_slot_is_all_ones(self):
        parsed = ring_mod.parse_slot(erased(), 0, 0)
        self.assertEqual(parsed["state"], "erased")
        self.assertFalse(parsed["unparsed"])

    def test_seq_ones_with_good_magic_is_not_erased(self):
        parsed = ring_mod.parse_slot(record(0xFFFFFFFF, index=7), 0, 0)
        self.assertNotEqual(parsed["state"], "erased")

    def test_wrong_magic_is_unknown_and_not_parsed(self):
        slot = record(833, magic=0x11223344)
        parsed = ring_mod.parse_slot(slot, 0, 0)
        self.assertEqual(parsed["state"], "unknown_magic")
        self.assertTrue(parsed["unparsed"])
        self.assertIsNone(parsed["record_index"])
        self.assertIsNone(parsed["time"])
        self.assertEqual(parsed["header_magic"], "0x11223344")
        self.assertEqual(parsed["raw_sha256"], hashlib.sha256(slot).hexdigest())
        self.assertIn("not parsing", parsed["notes"][0])

    def test_zero_magic_is_unknown(self):
        self.assertEqual(ring_mod.parse_slot(b"\x00" * SLOT, 0, 0)["state"],
                         "unknown_magic")

    def test_index_disagreeing_with_seq_is_malformed(self):
        parsed = ring_mod.parse_slot(record(837, index=838), 0, 0)
        self.assertEqual(parsed["state"], "malformed")
        self.assertFalse(parsed["header_index_matches_seq"])
        self.assertIn("disagrees", parsed["notes"][0])

    def test_build_and_reason_alone_do_not_make_a_record(self):
        blob = (b"\x41\x03\x00\x00\x00]\x00]" + b"\n```\n"
                + b"## Build: x\t\n## Reason: Restart\n```\t\n")
        padded = blob + b"\x00" * (SLOT - len(blob))
        parsed = ring_mod.parse_slot(padded, 0, 0)
        self.assertEqual(parsed["state"], "malformed")
        self.assertIn("no '# Index:'", parsed["notes"][0])

    def test_good_magic_without_terminated_header(self):
        padded = REAL_START_833[:8] + b"\x00" * (SLOT - 8)
        parsed = ring_mod.parse_slot(padded, 0, 0)
        self.assertEqual(parsed["state"], "malformed")
        self.assertIn("no terminated mtdoops text header", parsed["notes"])

    def test_incomplete_header_is_malformed(self):
        blob = (b"\x41\x03\x00\x00\x00]\x00]" + b"\n```\n# Index: 833\t\n"
                + b"## Reason: Restart\n")  # never closes the fence
        padded = blob + b"\x00" * (SLOT - len(blob))
        self.assertEqual(ring_mod.parse_slot(padded, 0, 0)["state"],
                         "malformed")

    def test_short_slot_is_missing(self):
        parsed = ring_mod.parse_slot(b"\x41\x03\x00\x00", 0, 0)
        self.assertEqual(parsed["state"], "missing")
        self.assertTrue(parsed["unparsed"])
        self.assertIn("not exactly", parsed["notes"][0])

    def test_oversized_slot_is_malformed(self):
        parsed = ring_mod.parse_slot(record(833) + b"\x00" * 16, 0, 0)
        self.assertEqual(parsed["state"], "malformed")


class ParseSingleSlotFields(unittest.TestCase):
    def test_fields_come_from_the_record_text(self):
        parsed = ring_mod.parse_slot(record(838), 0, 0)
        self.assertEqual(parsed["record_index"], 838)
        self.assertEqual(parsed["time"], "2026-10-28 12:39:28")
        self.assertEqual(parsed["powerup_reason"], "0x40001")
        self.assertEqual(parsed["reason"], "Restart")
        self.assertEqual(parsed["log_type"], "Kmsg")
        self.assertEqual(parsed["linux_version"], "4.19.113-perf-g1364bad74330")
        self.assertEqual(parsed["reboot_line"],
                         "Restarting system with command 'bootloader'")

    def test_origin_is_never_a_phase_claim(self):
        self.assertEqual(ring_mod.parse_slot(record(838), 0, 0)["origin"],
                         ring_mod.ORIGIN)

    def test_reboot_line_far_past_256k_is_still_found(self):
        filler = b"<6>[%12.6f] filler line with printable text\n" % 1.0
        deep = record(839, fill=filler * 6000)
        self.assertGreater(deep.find(b"reboot: Restarting"), 256 * 1024)
        parsed = ring_mod.parse_slot(deep, 0, 0)
        self.assertEqual(parsed["state"], "parsed")
        self.assertIn("Restarting system", parsed["reboot_line"])

    def test_binary_tail_never_reaches_the_snippet(self):
        packets = bytes(range(256)) * 64
        parsed = ring_mod.parse_slot(record(840, body_bytes=packets), 0, 0)
        snippet = parsed["text_snippet"] or ""
        self.assertTrue(all(32 <= ord(char) < 127 or char == "\n"
                            for char in snippet))

    def test_control_bytes_in_fields_are_neutralised(self):
        slot = record(841).replace(b"### 2026-10-28 12:39:28",
                                   b"### 2026-10-28 12:39:28\x1b[31m")
        parsed = ring_mod.parse_slot(slot, 0, 0)
        self.assertNotIn("\x1b", json.dumps(parsed))

    def test_no_reboot_line_is_null(self):
        parsed = ring_mod.parse_slot(record(842, reboot=False), 0, 0)
        self.assertEqual(parsed["state"], "parsed")
        self.assertIsNone(parsed["reboot_line"])

    def test_missing_powerup_is_noted_not_invented(self):
        slot = record(843).replace(b"Powerup reason=0x40001",
                                   b"No powerup marker here")
        parsed = ring_mod.parse_slot(slot, 0, 0)
        self.assertIsNone(parsed["powerup_reason"])
        self.assertIn("no Powerup reason in the text region", parsed["notes"])


class ParseRing(unittest.TestCase):
    def test_wrong_capture_size_refuses_to_guess(self):
        for size in (0, SLOT, TOTAL - 1, TOTAL + 1):
            with self.assertRaises(ring_mod.RingError) as caught:
                ring_mod.parse_ring(b"\xff" * size)
            self.assertIn("expected", str(caught.exception))

    def test_empty_ring(self):
        parsed = ring_mod.parse_ring(ring_of())
        self.assertEqual(parsed["live_slots"], 0)
        self.assertEqual(parsed["erased_slots"], COUNT)
        self.assertEqual(parsed["record_indices"], [])
        self.assertIsNone(parsed["index_min"])

    def test_partial_ring_offsets(self):
        parsed = ring_mod.parse_ring(
            ring_of(record(837), record(838), record(839)))
        self.assertEqual(parsed["live_slots"], 3)
        self.assertEqual(parsed["record_indices"], [837, 838, 839])
        self.assertEqual([s["offset"] for s in parsed["slots"]],
                         [i * SLOT for i in range(COUNT)])

    def test_index_gap_is_reported_as_facts_only(self):
        parsed = ring_mod.parse_ring(ring_of(record(837), record(839)))
        self.assertEqual(parsed["record_indices"], [837, 839])
        self.assertEqual(parsed["index_min"], 837)
        self.assertEqual(parsed["index_max"], 839)
        self.assertNotIn("index_contiguous", parsed)
        self.assertNotIn("wrapped", json.dumps(parsed).lower())
        self.assertIn("not evidence", parsed["continuity_note"])

    def test_full_ring(self):
        parsed = ring_mod.parse_ring(
            ring_of(*[record(830 + i) for i in range(COUNT)]))
        self.assertEqual(parsed["live_slots"], COUNT)
        self.assertEqual(parsed["erased_slots"], 0)

    def test_mixed_states_are_counted_separately(self):
        # An all-zero slot has magic 0, so it is unknown_magic too, not erased.
        parsed = ring_mod.parse_ring(
            ring_of(record(837), b"\x00" * SLOT, record(839, magic=0xDEAD)))
        self.assertEqual(parsed["live_slots"], 1)
        self.assertEqual(parsed["unknown_magic_slots"], 2)
        self.assertEqual(parsed["erased_slots"], COUNT - 3)
        self.assertEqual(parsed["record_indices"], [837])

    def test_capture_hash(self):
        capture = ring_of(record(837))
        self.assertEqual(ring_mod.parse_ring(capture)["capture_sha256"],
                         hashlib.sha256(capture).hexdigest())


class Report(unittest.TestCase):
    def test_report_cannot_claim_a_phase(self):
        report = ring_mod.build_report(ring_of(record(837)), "x.bin")
        self.assertFalse(report["phase_verified"])
        self.assertEqual(report["origin"], ring_mod.ORIGIN)
        self.assertIsNone(report["run_id"])

    def test_report_is_json_serialisable_and_clean(self):
        text = json.dumps(ring_mod.build_report(ring_of(record(837)), "x.bin"))
        self.assertIn(ring_mod.ORIGIN, text)
        self.assertNotIn("\ufffd", text)


class CmdlineBinding(unittest.TestCase):
    def test_exact_token_is_accepted(self):
        self.assertEqual(
            ring_mod.parse_cmdline_binding(CMD_LINE.decode()),
            "block2mtd.block2mtd=/dev/block/sda15,2097152")

    def test_substring_prefix_is_rejected(self):
        text = ("x block2mtd.block2mtd=/dev/block/sda15extra,2097152 "
                "mtdoops.mtddev=0")
        with self.assertRaises(ring_mod.RingError):
            ring_mod.parse_cmdline_binding(text)

    def test_longer_path_inside_token_is_rejected(self):
        with self.assertRaises(ring_mod.RingError):
            ring_mod.parse_cmdline_binding(
                "block2mtd.block2mtd=/dev/block/sda155,2097152")

    def test_wrong_size_is_rejected(self):
        with self.assertRaises(ring_mod.RingError):
            ring_mod.parse_cmdline_binding(
                "block2mtd.block2mtd=/dev/block/sda15,1048576")

    def test_absent_token_is_rejected(self):
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.parse_cmdline_binding("ramoops_memreserve=4M")
        self.assertIn("no block2mtd", str(caught.exception))

    def test_quoted_token_is_split_correctly(self):
        text = '"block2mtd.block2mtd=/dev/block/sda15,2097152" foo'
        self.assertEqual(ring_mod.parse_cmdline_binding(text),
                         "block2mtd.block2mtd=/dev/block/sda15,2097152")


class Mtd0Line(unittest.TestCase):
    def test_exact_geometry_is_accepted(self):
        self.assertEqual(ring_mod.parse_mtd0(MTD0.decode()), MTD0.decode())

    def test_wrong_size_field_is_rejected(self):
        line = 'mtd0: 00800000 00200000 "block2mtd: /dev/block/sda15"'
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.parse_mtd0(line)
        self.assertIn("size", str(caught.exception))

    def test_wrong_erasesize_is_rejected(self):
        line = 'mtd0: 01000000 00100000 "block2mtd: /dev/block/sda15"'
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.parse_mtd0(line)
        self.assertIn("erasesize", str(caught.exception))

    def test_wrong_path_is_rejected(self):
        line = 'mtd0: 01000000 00200000 "block2mtd: /dev/block/sda14"'
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.parse_mtd0(line)
        self.assertIn("path", str(caught.exception))

    def test_path_prefix_is_rejected(self):
        line = 'mtd0: 01000000 00200000 "block2mtd: /dev/block/sda15x"'
        with self.assertRaises(ring_mod.RingError):
            ring_mod.parse_mtd0(line)

    def test_unquoted_line_is_rejected(self):
        with self.assertRaises(ring_mod.RingError):
            ring_mod.parse_mtd0(
                "mtd0: 01000000 00200000 block2mtd: /dev/block/sda15")

    def test_missing_mtd0_is_rejected(self):
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.parse_mtd0('mtd1: 01000000 00200000 "x"')
        self.assertIn("no mtd0", str(caught.exception))


class PullVerification(unittest.TestCase):
    def run_pull(self, fake, **kwargs):
        terms = dict(expected_bytes=TOTAL, shell=fake.shell,
                     exec_out=fake.exec_out)
        terms.update(kwargs)
        return ring_mod.pull(SERIAL, **terms)

    def test_commands_are_read_only(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            data, path, _ = self.run_pull(fake, out_root=tmp)
            self.assertEqual(len(data), TOTAL)
            self.assertTrue(Path(path).exists())
        for command in fake.shell_calls:
            self.assertTrue(
                command.startswith("blockdev --getsize64")
                or command in ("cat /proc/cmdline", "cat /proc/mtd"), command)
        for command in fake.exec_calls:
            self.assertTrue(command.startswith("dd if=/dev/block/sda15 "))
            self.assertNotIn(" of=", command)
            self.assertTrue(command.endswith(" 2>/dev/null"), command)
        # Strip the one redirect this tool is allowed to issue, then forbid
        # every other kind of write, push or shell redirection.
        joined = " ".join(fake.all_argv()).lower().replace(" 2>/dev/null", "")
        for forbidden in ("push", "reboot", "flash", "erase", "mount",
                          "unmount", "of=", ">", "tee "):
            self.assertNotIn(forbidden, joined)
        # The only redirect anywhere is dd's own stderr.
        for command in fake.all_argv():
            self.assertEqual(command.count("2>/dev/null"),
                             1 if command.startswith("dd ") else 0, command)

    def test_dd_stderr_is_suppressed_so_the_stream_is_exact(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            data, path, _ = self.run_pull(fake, out_root=tmp)
            self.assertEqual(len(data), TOTAL)
            self.assertEqual(Path(path).stat().st_size, TOTAL)
            self.assertNotIn(b"records out", Path(path).read_bytes()[-256:])

    def test_unsuppressed_summary_would_have_failed_the_length_gate(self):
        # Proof the fix is what saves the run, not the length check: the same
        # fixture without a 2>/dev/null-capable exec-out yields 16777300.
        fake = FakeAdb()
        merged = fake.payload + DD_SUMMARY
        self.assertEqual(len(merged), TOTAL + 84)
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.parse_ring(merged)
        self.assertIn("expected", str(caught.exception))

    def test_extra_payload_bytes_fail_and_are_never_truncated(self):
        fake = FakeAdb(extra=b"\x00" * 84)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError) as caught:
                self.run_pull(fake, out_root=tmp)
            self.assertIn("expected", str(caught.exception))
            # Nothing was written: a bad capture must not leave an artifact.
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_one_extra_byte_fails(self):
        fake = FakeAdb(extra=b"\n")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)

    def test_dd_nonzero_exit_still_fails_with_stderr_suppressed(self):
        fake = FakeAdb(dd_rc=1)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError) as caught:
                self.run_pull(fake, out_root=tmp)
            self.assertIn("dd failed", str(caught.exception))

    def test_expected_bytes_enforced_inside_pull(self):
        fake = FakeAdb()
        with self.assertRaises(ring_mod.RingError):
            ring_mod.pull(SERIAL, expected_bytes=TOTAL + 1, shell=fake.shell,
                          exec_out=fake.exec_out)
        self.assertEqual(fake.all_argv(), [])

    def test_serial_is_required(self):
        fake = FakeAdb()
        with self.assertRaises(ring_mod.RingError) as caught:
            ring_mod.pull("", expected_bytes=TOTAL, shell=fake.shell,
                          exec_out=fake.exec_out)
        self.assertIn("serial", str(caught.exception))
        self.assertEqual(fake.all_argv(), [])

    def test_wrong_device_size_stops_before_reading(self):
        fake = FakeAdb(size=TOTAL - SLOT)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)
            self.assertEqual(fake.exec_calls, [])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_blockdev_failure_stops_before_reading(self):
        fake = FakeAdb(blockdev_rc=1)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)
            self.assertEqual(fake.exec_calls, [])

    def test_unbound_cmdline_stops_before_reading(self):
        fake = FakeAdb(cmdline=b"ramoops_memreserve=4M")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError) as caught:
                self.run_pull(fake, out_root=tmp)
            self.assertIn("block2mtd", str(caught.exception))
            self.assertEqual(fake.exec_calls, [])

    def test_cmdline_prefix_trap_stops_before_reading(self):
        fake = FakeAdb(
            cmdline=b"block2mtd.block2mtd=/dev/block/sda15extra,2097152")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)
            self.assertEqual(fake.exec_calls, [])

    def test_mtd0_wrong_geometry_stops_before_reading(self):
        fake = FakeAdb(
            mtd=b'mtd0: 00800000 00200000 "block2mtd: /dev/block/sda15"')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)
            self.assertEqual(fake.exec_calls, [])

    def test_mtd0_other_device_stops(self):
        fake = FakeAdb(
            mtd=b'mtd0: 01000000 00200000 "block2mtd: /dev/block/sda14"')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)
            self.assertEqual(fake.exec_calls, [])

    def test_short_read_is_an_error(self):
        fake = FakeAdb(short=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)

    def test_dd_failure_is_an_error(self):
        fake = FakeAdb(dd_rc=1)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError):
                self.run_pull(fake, out_root=tmp)

    def test_timeout_during_shell_is_reported(self):
        fake = FakeAdb(timeout_on="shell")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError) as caught:
                self.run_pull(fake, out_root=tmp)
            self.assertIn("timed out", str(caught.exception))

    def test_timeout_during_dd_is_reported(self):
        fake = FakeAdb(timeout_on="exec")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ring_mod.RingError) as caught:
                self.run_pull(fake, out_root=tmp)
            self.assertIn("timed out", str(caught.exception))

    def test_capture_permissions_are_private(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            _, path, _ = self.run_pull(fake, out_root=tmp)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(Path(path).parent).st_mode & 0o777, 0o700)

    def test_capture_directories_are_exclusive(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            _, path1, _ = self.run_pull(fake, out_root=tmp)
            _, path2, _ = self.run_pull(fake, out_root=tmp)
            self.assertNotEqual(path1, path2)
            self.assertEqual(len(list(Path(tmp).iterdir())), 2)

    def test_mapping_records_raw_evidence(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            _, _, mapping = self.run_pull(fake, out_root=tmp)
        self.assertEqual(mapping["device"], "/dev/block/sda15")
        self.assertEqual(mapping["getsize64"], TOTAL)
        self.assertEqual(mapping["cmdline_binding"],
                         "block2mtd.block2mtd=/dev/block/sda15,2097152")
        self.assertIn("block2mtd", mapping["cmdline_raw"])
        self.assertEqual(mapping["mtd0"], MTD0.decode())
        self.assertIn("mtd0:", mapping["mtd_raw"])

    def test_report_from_pull_is_still_unverified(self):
        fake = FakeAdb()
        with tempfile.TemporaryDirectory() as tmp:
            data, path, mapping = self.run_pull(fake, out_root=tmp)
        report = ring_mod.build_report(data, path, pulled=mapping,
                                       device=ring_mod.MTD_DEVICE)
        self.assertFalse(report["phase_verified"])
        self.assertEqual(report["origin"], ring_mod.ORIGIN)


class CommandLine(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ring_mod.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write_capture(self, tmp, data):
        path = Path(tmp) / "capture.bin"
        path.write_bytes(data)
        return path

    def test_file_mode_parses_without_a_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_capture(tmp, ring_of(record(837)))
            code, out, _ = self.run_main(["--file", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("index=837", out)
        self.assertIn("phase_verified False", out)

    def test_json_mode_is_a_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_capture(tmp, ring_of(record(837)))
            code, out, _ = self.run_main(["--file", str(path), "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertFalse(report["phase_verified"])
        self.assertEqual(report["live_slots"], 1)

    def test_wrong_size_file_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_capture(tmp, b"\xff" * 4096)
            code, _, err = self.run_main(["--file", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("expected", err)

    def test_missing_file_exits_two(self):
        code, _, _ = self.run_main(["--file", "/nonexistent/capture.bin"])
        self.assertEqual(code, 2)

    def test_file_and_pull_are_exclusive(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main(["--file", "x.bin", "--pull", "--serial", SERIAL])
        self.assertNotEqual(caught.exception.code, 0)

    def test_no_source_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main([])
        self.assertNotEqual(caught.exception.code, 0)

    def test_wrong_expected_bytes_for_pull_is_an_error(self):
        code, _, err = self.run_main(["--pull", "--serial", SERIAL,
                                      "--expected-bytes", "1024"])
        self.assertEqual(code, 2)
        self.assertIn("expected-bytes", err)

    def test_zero_timeout_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_main(["--pull", "--serial", SERIAL, "--timeout", "0"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_default_timeout_is_bounded(self):
        self.assertLessEqual(ring_mod.DEFAULT_TIMEOUT, 60)
        self.assertGreater(ring_mod.DEFAULT_TIMEOUT, 0)


if __name__ == "__main__":
    unittest.main()