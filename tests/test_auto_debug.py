"""Offline unittest coverage: no adb, fastboot or physical device required."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "auto_debug", Path(__file__).resolve().parents[1] / "scripts" / "auto-debug.py")
auto_debug = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auto_debug)

PRE_ID = "11111111-2222-3333-4444-555555555555"
NEW_ID = "99999999-8888-7777-6666-555555555555"
OTHER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REC = auto_debug.RECOMMENDED_RECOVERY_SHA256
OTHER = "b" * 64


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeExecutor:
    """Simulated device. `boot_delay` device polls elapse between the boot
    command and the new recovery boot ID becoming visible; `stale_polls` device
    polls first report the OLD boot ID while already in recovery."""

    def __init__(self, boot_rc=0, disconnected=False, fastboot_missing=False,
                 serial="d25f844e", twrp=True, boot_id=PRE_ID, new_boot_id=NEW_ID,
                 stale_polls=0, boot_delay=1, hashes=None, post_hashes=None,
                 boot_id_readable=True, boot_id_text=None, pstore=None,
                 log_rc=0, pstore_rc=0, pstore_list_rc=0, slow_boot_id=0.0,
                 clock=None, adb_state="recovery"):
        self.calls = []
        self.boot_rc = boot_rc
        self.disconnected = disconnected
        self.fastboot_missing = fastboot_missing
        self.serial = serial
        self.twrp = twrp
        self.boot_id_readable = boot_id_readable
        self.pre_id = boot_id
        self.new_id = new_boot_id
        self.stale_polls = stale_polls
        self.boot_delay = boot_delay
        self.hashes = hashes or {"boot": REC, "recovery": REC}
        self.post_hashes = post_hashes
        self.booted = False
        self.boots = 0
        self.devices_polls = 0
        self.raw_pmsg = b"pmsg\x00\xff\r\nretained"
        self.boot_id_text = boot_id_text
        # Default: one valid pstore file and no hostile entries.
        self.pstore = ["pmsg-ramoops-0"] if pstore is None else pstore
        self.log_rc = log_rc
        self.pstore_rc = pstore_rc
        self.pstore_list_rc = pstore_list_rc
        self.slow_boot_id = slow_boot_id
        self.clock = clock
        self.adb_state = adb_state

    def current_id(self):
        if not self.booted:
            return self.pre_id
        if self.stale_polls > 0:
            self.stale_polls -= 1
            return self.pre_id
        if self.devices_polls < self.boot_delay:
            return self.pre_id
        return self.new_id

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        rc, out, err = 0, b"", b""
        if argv == ["adb", "devices"]:
            self.devices_polls += 1
            # `disconnected` models a device lost AFTER the temporary boot.
            if not (self.booted and self.disconnected):
                out = f"List of devices attached\n{self.serial}\t{self.adb_state}\n".encode()
        elif argv == ["fastboot", "devices"]:
            if not self.fastboot_missing:
                out = f"{self.serial}\tfastboot\n".encode()
        elif argv[0] == "fastboot" and "boot" in argv:
            self.boots += 1
            self.booted = True
            self.devices_polls = 0
            rc, out, err = self.boot_rc, b"Sending boot image\n", b"boot details\n"
        elif "boot_id" in argv[-1]:
            if self.clock is not None and self.slow_boot_id:
                # Simulate a boot-ID read that consumes its whole budget.
                self.clock.now += timeout if self.slow_boot_id else 0
            if self.boot_id_text is not None:
                out = self.boot_id_text.encode()
            elif self.boot_id_readable:
                out = (self.current_id() + "\n").encode()
            else:
                rc = 1
        elif "getprop" in argv:
            out = b"3.7.0\n" if self.twrp else b""
        elif "sha256sum" in argv:
            partition = argv[-1].rsplit("/", 1)[-1]
            table = self.hashes
            if self.booted and self.post_hashes is not None:
                table = self.post_hashes
            if table.get(partition):
                out = (table[partition] + "  " + argv[-1] + "\n").encode()
            else:
                rc = 1
        elif argv[-1] == "/sys/fs/pstore":
            rc = self.pstore_list_rc
            out = "".join(name + "\n" for name in self.pstore).encode()
        elif "sh" in argv:
            rc, out = self.pstore_rc, self.raw_pmsg
        elif "exec-out" in argv:
            rc, out = self.log_rc, b"captured\n"
        return subprocess.CompletedProcess(argv, rc, out, err)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "image.img"
        self.image.write_bytes(b"offline test image")
        self.digest = hashlib.sha256(self.image.read_bytes()).hexdigest()

    def runner(self, fake=None, mode="--run", extra=(), clock=None):
        argv = ["--output", str(self.root / "runs"), "--image", str(self.image),
                "--expected-sha256", self.digest, "--wait-timeout", "10",
                "--poll-interval", "2", "--command-timeout", "3"]
        if mode:
            argv.append(mode)
        argv.extend(extra)
        clock = clock or Clock()
        fake = fake or FakeExecutor()
        # Fake device and runner share one virtual clock, so a simulated slow
        # device command advances the runner's deadline accounting too.
        if hasattr(fake, "clock"):
            fake.clock = clock
        runner = auto_debug.Runner(auto_debug.parse_args(argv), executor=fake,
                                   monotonic=clock.monotonic, sleep=clock.sleep,
                                   timestamp=lambda: "2026-01-01T00:00:00+00:00")
        return runner, fake, clock

    @staticmethod
    def boot_commands(fake):
        return [argv for argv, _ in fake.calls
                if argv[0] == "fastboot" and "boot" in argv]

    @staticmethod
    def boot_id_reads(fake):
        return [argv for argv, _ in fake.calls if "boot_id" in argv[-1]]

    # -- image pinning -----------------------------------------------------

    def test_wrong_hash_refuses_reboot_and_all_commands(self):
        runner, fake, _ = self.runner(extra=("--expected-sha256", "0" * 64))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertEqual(fake.calls, [])
        self.assertIn("SHA256 mismatch", report["errors"][0])

    def test_boot_failure_prevents_recovery_success(self):
        runner, fake, _ = self.runner(FakeExecutor(boot_rc=1))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(report["phase_verified"])
        self.assertFalse(report["recovery_boot_verified"])
        self.assertEqual(len(self.boot_commands(fake)), 1)
        boot_record = next(c for c in report["commands"]
                           if c["argv"][0] == "fastboot" and "boot" in c["argv"])
        self.assertEqual(boot_record["returncode"], 1)
        self.assertEqual((runner.output / boot_record["stdout"]).read_bytes(), b"Sending boot image\n")
        self.assertEqual((runner.output / boot_record["stderr"]).read_bytes(), b"boot details\n")
        self.assertNotIn("dmesg.bin", report["artifacts"])

    def test_private_staged_image_is_booted(self):
        runner, fake, _ = self.runner()
        report = runner.run()
        staged = runner.output / "boot-image.img"
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_bytes(), self.image.read_bytes())
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertIn("boot-image.img", report["artifacts"])
        self.assertEqual(self.boot_commands(fake),
                         [["fastboot", "-s", "d25f844e", "boot", str(staged)]])

    # -- boot ID freshness -------------------------------------------------

    def test_run_saves_pre_boot_id_and_requires_a_different_one(self):
        runner, fake, _ = self.runner()
        report = runner.run()
        self.assertEqual(report["pre_boot_id"], PRE_ID)
        self.assertEqual(report["recovery_boot_id"], NEW_ID)
        self.assertNotEqual(report["pre_boot_id"], report["recovery_boot_id"])
        self.assertEqual(report["boot_id_freshness"], "pinned-pre-boot-id")
        self.assertTrue(report["boot_id_verified"])
        self.assertTrue(report["recovery_boot_verified"])
        self.assertTrue(report["transport_verified"])
        self.assertFalse(report["phase_verified"])
        # The pre-boot ID is read before the reboot, never after it.
        reboot_index = next(i for i, (argv, _) in enumerate(fake.calls) if "reboot" in argv)
        reads = [i for i, (argv, _) in enumerate(fake.calls) if "boot_id" in argv[-1]]
        self.assertLess(reads[0], reboot_index)
        self.assertGreaterEqual(len(reads), 3)

    def test_old_boot_id_reconnect_is_not_recovery_returned(self):
        runner, fake, clock = self.runner(FakeExecutor(stale_polls=2))
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertEqual(report["recovery_boot_id"], NEW_ID)
        self.assertTrue(report["recovery_boot_verified"])
        self.assertIn("recovery:stale-boot-id", [t["state"] for t in report["transitions"]])
        self.assertGreater(clock.now, 0)

    def test_only_stale_boot_ids_time_out_inside_one_deadline(self):
        runner, fake, clock = self.runner(FakeExecutor(stale_polls=10 ** 6))
        report = runner.run()
        self.assertEqual(report["result"], "stale_boot_id")
        self.assertNotEqual(report["result"], "recovery_returned")
        self.assertFalse(report["recovery_boot_verified"])
        self.assertFalse(report["phase_verified"])
        self.assertIsNone(report["recovery_boot_id"])
        self.assertIsNone(report["boot_command_to_recovery_elapsed"])
        self.assertEqual(len(self.boot_commands(fake)), 1)
        self.assertEqual(clock.now, 10)
        self.assertIn("recovery:timeout", [t["state"] for t in report["transitions"]])
        self.assertIn("new recovery boot was never confirmed", " ".join(report["errors"]))

    def test_unreadable_new_boot_id_is_never_fresh(self):
        fake = FakeExecutor()
        def executor(argv, timeout):
            result = fake(argv, timeout)
            if "boot_id" in argv[-1] and fake.booted:
                return subprocess.CompletedProcess(argv, 1, b"", b"denied")
            return result
        runner, _, clock = self.runner(fake)
        runner.executor = executor
        report = runner.run()
        self.assertEqual(report["result"], "timeout")
        self.assertFalse(report["recovery_boot_verified"])
        self.assertIsNone(report["recovery_boot_id"])
        self.assertEqual(clock.now, 10)

    def test_unreadable_pre_boot_id_refuses_reboot(self):
        runner, fake, _ = self.runner(FakeExecutor(boot_id_readable=False))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))
        self.assertEqual(self.boot_commands(fake), [])
        self.assertIn("boot ID unavailable", report["errors"][0])

    def test_collect_only_reports_unknown_freshness_and_no_pre_id(self):
        runner, fake, _ = self.runner(mode="--collect-only")
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertEqual(report["boot_id_freshness"], "unknown")
        self.assertIsNone(report["pre_boot_id"])
        self.assertFalse(report["recovery_boot_verified"])
        self.assertFalse(report["boot_id_verified"])
        self.assertFalse(report["phase_verified"])
        self.assertEqual((runner.output / "pstore-pmsg-ramoops-0").read_bytes(), fake.raw_pmsg)
        self.assertFalse(any("reboot" in argv or argv[0] == "fastboot" for argv, _ in fake.calls))

    def test_collect_only_still_returns_on_unchanged_boot_id(self):
        runner, fake, _ = self.runner(mode="--collect-only")
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertFalse(report["transport_verified"])
        self.assertIsNone(report["partition_hashes_unchanged"])
        # A pre-boot ID is never read in this mode.
        self.assertEqual(self.boot_id_reads(fake)[0], ["adb", "-s", "d25f844e", "shell", "cat",
                                                       auto_debug.BOOT_ID_PATH])

    # -- transport and partition hashes -----------------------------------

    def test_boot_command_to_new_recovery_elapsed(self):
        runner, fake, clock = self.runner(FakeExecutor(boot_delay=3))
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        boot_record = next(c for c in report["commands"]
                           if c["argv"][0] == "fastboot" and "boot" in c["argv"])
        # boot image counts as one poll; the ID flips on the third poll (2s apart).
        self.assertGreater(report["boot_command_to_recovery_elapsed"], 0)
        self.assertEqual(report["boot_command_to_recovery_elapsed"], 4.0)
        self.assertGreaterEqual(report["boot_command_to_recovery_elapsed"],
                                boot_record["elapsed"])
        self.assertLessEqual(clock.now, 10 + 3 * 3 + 1)

    def test_unchanged_partition_hashes_are_error_free(self):
        runner, fake, _ = self.runner()
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertTrue(report["partition_hashes_unchanged"])
        self.assertTrue(report["boot_recovery_hashes_equal"])
        self.assertTrue(report["transport_verified"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])

    def test_changed_post_partition_hash_fails_the_result(self):
        fake = FakeExecutor(post_hashes={"boot": REC, "recovery": OTHER})
        runner, _, _ = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(report["partition_hashes_unchanged"])
        self.assertFalse(report["transport_verified"])
        self.assertTrue(report["recovery_boot_verified"])
        self.assertFalse(report["phase_verified"])
        self.assertIn("hashes changed", " ".join(report["errors"]))
        self.assertEqual((runner.output / "pstore-pmsg-ramoops-0").read_bytes(), fake.raw_pmsg)

    def test_pre_boot_and_recovery_hashes_must_be_equal(self):
        for hashes in ({"boot": REC, "recovery": OTHER},
                       {"boot": REC, "recovery": OTHER}, {"boot": REC, "recovery": "c" * 64}):
            with self.subTest(hashes=hashes):
                runner, fake, _ = self.runner(FakeExecutor(hashes=hashes))
                report = runner.run()
                self.assertEqual(report["result"], "failed")
                self.assertFalse(report["boot_recovery_hashes_equal"])
                self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))
                self.assertEqual(self.boot_commands(fake), [])
                self.assertIn("hashes differ", report["errors"][0])

    def test_expected_recovery_hash_pin_must_match_exactly(self):
        runner, fake, _ = self.runner(extra=("--expected-recovery-sha256", OTHER))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))
        self.assertIn("--expected-recovery-sha256", report["errors"][0])

    def test_matching_recovery_pin_is_accepted_case_insensitively(self):
        runner, fake, _ = self.runner(extra=("--expected-recovery-sha256", REC.upper()))
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertEqual(report["warnings"], [])
        self.assertEqual(len(self.boot_commands(fake)), 1)

    def test_recommended_hash_without_pin_is_silent(self):
        runner, _, _ = self.runner()
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertEqual(report["warnings"], [])

    def test_other_recovery_hash_without_pin_only_warns(self):
        fake = FakeExecutor(hashes={"boot": OTHER, "recovery": OTHER},
                            post_hashes={"boot": OTHER, "recovery": OTHER})
        runner, _, _ = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("RECOMMENDED_RECOVERY_SHA256", report["warnings"][0])

    def test_mismatch_with_recommended_hash_only_warns(self):
        fake = FakeExecutor(hashes={"boot": OTHER, "recovery": OTHER},
                            post_hashes={"boot": OTHER, "recovery": OTHER})
        runner, _, _ = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("does not match RECOMMENDED_RECOVERY_SHA256", report["warnings"][0])

    def test_missing_baseline_hash_refuses_reboot(self):
        fake = FakeExecutor(hashes={"boot": REC})
        runner, _, _ = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))

    # -- bounded transport -------------------------------------------------

    def test_recovery_timeout_does_not_retry_boot(self):
        runner, fake, clock = self.runner(FakeExecutor(disconnected=True))
        report = runner.run()
        self.assertEqual(report["result"], "timeout")
        self.assertEqual(len(self.boot_commands(fake)), 1)
        self.assertEqual(clock.now, 10)
        self.assertFalse(report["phase_verified"])
        self.assertFalse(report["recovery_boot_verified"])

    def test_disconnected_fastboot_never_boots(self):
        runner, fake, clock = self.runner(FakeExecutor(fastboot_missing=True))
        report = runner.run()
        self.assertEqual(report["result"], "timeout")
        self.assertEqual(self.boot_commands(fake), [])
        self.assertEqual(clock.now, 10)
        self.assertFalse(report["boot_id_verified"])

    def test_default_dry_run_has_zero_subprocess_calls(self):
        runner, fake, _ = self.runner(mode=None)
        report = runner.run()
        self.assertEqual(report["mode"], "dry-run")
        self.assertIsNone(report["result"])
        self.assertEqual(fake.calls, [])
        self.assertNotIn("boot-image.img", report["artifacts"])
        self.assertEqual(report["boot_id_freshness"], "unknown")
        self.assertFalse(report["recovery_boot_verified"])
        self.assertFalse(report["phase_verified"])
        self.assertTrue(report["plan"])

    def test_dry_run_accepts_pins_and_still_runs_no_commands(self):
        runner, fake, _ = self.runner(mode=None,
                                      extra=("--expected-recovery-sha256", REC))
        report = runner.run()
        self.assertEqual(report["mode"], "dry-run")
        self.assertIsNone(report["result"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(report["image"]["sha256"], self.digest)

    def test_old_id_after_boot_is_not_counted_as_elapsed(self):
        runner, fake, _ = self.runner(FakeExecutor(stale_polls=10 ** 6))
        report = runner.run()
        self.assertIsNone(report["boot_command_to_recovery_elapsed"])
        self.assertEqual(len(self.boot_commands(fake)), 1)

    def test_collect_only_never_requires_boot_id_change(self):
        runner, fake, _ = self.runner(mode="--collect-only")
        report = runner.run()
        self.assertFalse(report["transport_verified"])
        self.assertTrue(report["collection_verified"])
        self.assertIsNone(report["partition_hashes_unchanged"])
        self.assertEqual(report["recovery_boot_id"], PRE_ID)
        self.assertEqual(report["boot_id_freshness"], "unknown")

    def test_inspect_is_readonly_and_does_not_reboot(self):
        runner, fake, _ = self.runner(mode="--inspect")
        report = runner.run()
        self.assertIsNone(report["result"])
        self.assertTrue(fake.calls)
        self.assertTrue(report["boot_recovery_hashes_equal"])
        self.assertFalse(report["recovery_boot_verified"])
        self.assertFalse(any("reboot" in argv or argv[0] == "fastboot" for argv, _ in fake.calls))

    def test_no_forbidden_partition_commands_in_any_call(self):
        runner, fake, _ = self.runner()
        runner.run()
        for argv, limit in fake.calls:
            self.assertLessEqual(limit, 3)
            self.assertFalse(set(argv) & {"flash", "erase", "format", "push", "dd", "write"})
        self.assertEqual(fake.calls[0][0], ["adb", "devices"])

    def test_wrong_serial_and_missing_twrp_refuse_reboot(self):
        for fake in (FakeExecutor(serial="other"), FakeExecutor(twrp=False)):
            with self.subTest(fake=fake):
                runner, fake, _ = self.runner(fake)
                self.assertEqual(runner.run()["result"], "failed")
                self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))

    def test_unique_output_directories(self):
        first, _, _ = self.runner(mode=None)
        second, _, _ = self.runner(mode=None)
        self.assertNotEqual(first.output, second.output)
        self.assertTrue(first.output.is_dir())
        self.assertTrue(second.output.is_dir())

    def test_partial_boot_timeout_captured_and_never_retried(self):
        fake = FakeExecutor()
        def executor(argv, timeout):
            if argv[0] == "fastboot" and "boot" in argv:
                fake.calls.append((argv, timeout))
                raise subprocess.TimeoutExpired(argv, timeout, output=b"partial out", stderr=b"partial err")
            return fake(argv, timeout)
        runner, _, _ = self.runner(fake)
        runner.executor = executor
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        record = report["commands"][-1]
        self.assertTrue(record["timed_out"])
        self.assertIsNone(record["returncode"])
        self.assertEqual((runner.output / record["stdout"]).read_bytes(), b"partial out")
        self.assertEqual((runner.output / record["stderr"]).read_bytes(), b"partial err")
        self.assertEqual(len(self.boot_commands(fake)), 1)

    def test_poll_commands_are_capped_by_remaining_deadline(self):
        runner, fake, clock = self.runner(mode="--collect-only")
        def executor(argv, timeout):
            fake.calls.append((argv, timeout))
            clock.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout, output=b"partial")
        runner.executor = executor
        report = runner.run()
        self.assertEqual(report["result"], "timeout")
        self.assertEqual(clock.now, 10)
        self.assertEqual(fake.calls, [(["adb", "devices"], 3.0)] * 2)

    def test_run_cli_requires_hash_and_image(self):
        for argv in (["--run"], ["--run", "--image", str(self.image)]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as error:
                auto_debug.parse_args(argv)
            self.assertEqual(error.exception.code, 2)

    def test_cli_rejects_malformed_recovery_pin(self):
        with self.assertRaises(SystemExit) as error:
            auto_debug.parse_args(["--expected-recovery-sha256", "xyz"])
        self.assertEqual(error.exception.code, 2)

    # -- deadline plumbing ------------------------------------------------

    def test_boot_id_read_is_bounded_by_the_wait_deadline(self):
        """A boot-ID read slower than the budget cannot outlive the wait."""
        clock = Clock()
        argv = ["--output", str(self.root / "runs5"), "--image", str(self.image),
                "--expected-sha256", self.digest, "--run",
                "--wait-timeout", "10", "--poll-interval", "2",
                "--command-timeout", "12"]   # one slow read exceeds the whole window
        fake = FakeExecutor(slow_boot_id=10 ** 6)
        fake.clock = clock
        runner = auto_debug.Runner(auto_debug.parse_args(argv), executor=fake,
                                   monotonic=clock.monotonic, sleep=clock.sleep,
                                   timestamp=lambda: "T")
        report = runner.run()
        # Every read is clamped by --command-timeout, so the wait cannot be
        # stretched past its own deadline by a slow device.
        # Only reads inside the recovery wait are bounded by it; the pre-reboot
        # baseline read happens before that window opens.
        boot_index = next(i for i, c in enumerate(report["commands"])
                          if c["argv"][0] == "fastboot" and "boot" in c["argv"])
        writes = [c for c in report["commands"][boot_index:]
                  if "boot_id" in c["argv"][-1]]
        self.assertTrue(writes)
        for record in writes:
            self.assertLessEqual(record["timeout"], 10)
        self.assertLessEqual(clock.now, 10 + 2 * 10)
        self.assertEqual(report["result"], "timeout")
        self.assertFalse(report["recovery_boot_verified"])

    def test_boot_id_read_that_eats_the_whole_budget_is_not_fresh(self):
        """Last read consumes the deadline: it cannot confirm the new boot."""
        clock = Clock()
        # Wait budget (2s) is smaller than one command timeout (3s), so a single
        # slow read really does drain the whole window.
        argv = ["--output", str(self.root / "runs4"), "--image", str(self.image),
                "--expected-sha256", self.digest, "--run",
                "--wait-timeout", "2", "--poll-interval", "2",
                "--command-timeout", "30"]
        fake = FakeExecutor()
        runner = auto_debug.Runner(auto_debug.parse_args(argv), executor=fake,
                                   monotonic=clock.monotonic, sleep=clock.sleep,
                                   timestamp=lambda: "T")
        def executor(argv, timeout):
            # Burn the whole remaining budget on the first post-boot read.
            if "boot_id" in argv[-1] and fake.booted:
                clock.now += timeout
                return subprocess.CompletedProcess(argv, 0, (NEW_ID + "\n").encode(), b"")
            return fake(argv, timeout)
        runner.executor = executor
        report = runner.run()
        self.assertFalse(report["recovery_boot_verified"])
        self.assertIsNone(report["boot_command_to_recovery_elapsed"])
        self.assertFalse(report["transport_verified"])
        self.assertIn(report["result"], ("timeout", "stale_boot_id"))

    def test_read_ending_exactly_at_the_deadline_is_untimely(self):
        """Wait budget 3s; a 3s boot-ID read ends exactly at the deadline."""
        clock = Clock()
        argv = ["--output", str(self.root / "runs3"), "--image", str(self.image),
                "--expected-sha256", self.digest, "--run",
                "--wait-timeout", "10", "--poll-interval", "2",
                "--command-timeout", "30"]
        fake = FakeExecutor()
        runner = auto_debug.Runner(auto_debug.parse_args(argv), executor=fake,
                                   monotonic=clock.monotonic, sleep=clock.sleep,
                                   timestamp=lambda: "T")
        def executor(cmd, timeout):
            if "boot_id" in cmd[-1] and fake.booted:
                clock.now += max(timeout, 1)   # one read drains the window
                return subprocess.CompletedProcess(cmd, 0, (NEW_ID + "\n").encode(), b"")
            return fake(cmd, timeout)
        runner.executor = executor
        report = runner.run()
        self.assertFalse(report["recovery_boot_verified"])

    def test_exhausted_deadline_is_a_miss_not_a_failure(self):
        """Pool of 1s: the poll command eats it; the next budget is <= 0 and the
        run ends as a normal timeout with no extra command."""
        runner, fake, clock = self.runner(mode="--collect-only")
        argv = ["--output", str(self.root / "runs2"), "--collect-only",
                "--wait-timeout", "1", "--poll-interval", "1",
                "--command-timeout", "3"]
        runner = auto_debug.Runner(auto_debug.parse_args(argv), executor=fake,
                                   monotonic=clock.monotonic, sleep=clock.sleep,
                                   timestamp=lambda: "T")
        def executor(cmd, timeout):
            fake.calls.append((cmd, timeout))
            clock.now += timeout          # the command consumes its whole budget
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        runner.executor = executor
        report = runner.run()
        self.assertEqual(report["result"], "timeout")
        self.assertNotIn("command deadline exhausted", " ".join(report["errors"]))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1], 1.0)
        self.assertEqual(clock.now, 1.0)

    def test_no_command_ever_exceeds_the_wait_budget(self):
        runner, fake, _ = self.runner()
        runner.run()
        for argv, limit in fake.calls:
            self.assertLessEqual(limit, 10)

    def test_elapsed_starts_when_the_boot_command_finishes(self):
        clock = Clock()
        runner, fake, _ = self.runner(FakeExecutor(boot_delay=3), clock=clock)
        def executor(argv, timeout):
            if argv[0] == "fastboot" and "boot" in argv:
                clock.now += 100          # the boot command itself is slow
            return fake(argv, timeout)
        runner.executor = executor
        report = runner.run()
        self.assertEqual(report["result"], "recovery_returned")
        # 100s of boot-command time must NOT be in the elapsed.
        self.assertLess(report["boot_command_to_recovery_elapsed"], 100)
        self.assertGreater(report["boot_command_to_recovery_elapsed"], 0)

    # -- boot ID parsing ---------------------------------------------------

    def test_trailing_text_after_boot_id_is_rejected(self):
        fake = FakeExecutor(boot_id_text=PRE_ID + " garbage\n")
        runner, fake, clock = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))
        self.assertIn("boot ID unavailable", report["errors"][0])

    def test_leading_text_before_boot_id_is_rejected(self):
        fake = FakeExecutor(boot_id_text="warning: noise\n" + PRE_ID + "\n")
        runner, fake, _ = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(any("reboot" in argv for argv, _ in fake.calls))

    def test_boot_id_with_surrounding_whitespace_is_accepted(self):
        fake = FakeExecutor(boot_id_text="\n  " + PRE_ID.upper() + "\t\n")
        runner, fake, _ = self.runner(fake)
        report = runner.run()
        self.assertEqual(report["pre_boot_id"], PRE_ID)

    # -- collection validity ----------------------------------------------

    def test_failed_log_read_is_not_a_full_success(self):
        runner, fake, _ = self.runner(FakeExecutor(log_rc=1))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(report["collection_verified"])
        self.assertFalse(report["transport_verified"])
        self.assertFalse(report["phase_verified"])
        # The new recovery WAS reached; the fields stay honest about that.
        self.assertTrue(report["recovery_boot_verified"])
        self.assertTrue(report["partition_hashes_unchanged"])

    def test_failed_pstore_listing_is_not_a_full_success(self):
        runner, _, _ = self.runner(FakeExecutor(pstore_list_rc=1))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(report["collection_verified"])
        self.assertIn("pstore listing unavailable", report["errors"])

    def test_rejected_pstore_entry_invalidates_the_collection(self):
        runner, fake, _ = self.runner(
            FakeExecutor(pstore=["pmsg-ramoops-0", "../secret", ".link"]))
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(report["collection_verified"])
        self.assertIn("rejected unsafe/duplicate pstore entry", report["errors"])
        self.assertFalse(any("secret" in " ".join(call[0]) for call in fake.calls))

    def test_collect_only_with_failed_reads_fails(self):
        runner, _, _ = self.runner(FakeExecutor(log_rc=1), mode="--collect-only")
        report = runner.run()
        self.assertEqual(report["result"], "failed")
        self.assertFalse(report["collection_verified"])
        self.assertFalse(report["transport_verified"])
        self.assertIsNone(report["partition_hashes_unchanged"])

    # -- inspect -----------------------------------------------------------

    def test_inspect_without_confirmed_recovery_fails_and_stops(self):
        for fake in (FakeExecutor(twrp=False), FakeExecutor(serial="other"),
                     FakeExecutor(adb_state="unauthorized"),
                     FakeExecutor(adb_state="offline")):
            with self.subTest(fake=fake):
                runner, fake, _ = self.runner(fake, mode="--inspect")
                report = runner.run()
                self.assertEqual(report["result"], "failed")
                self.assertFalse(any("sha256sum" in argv for argv, _ in fake.calls))
                self.assertFalse(any("reboot" in argv or argv[0] == "fastboot"
                                     for argv, _ in fake.calls))
                self.assertIsNone(report["partition_hashes_unchanged"])

    # -- hash record hygiene ----------------------------------------------

    def test_no_duplicate_pre_recovery_slot_record(self):
        runner, _, _ = self.runner()
        report = runner.run()
        self.assertNotIn("recovery_partition_sha256", report)
        self.assertEqual(len(report["pre_partition_sha256"]), 2)
        self.assertTrue(report["boot_recovery_hashes_equal"])

    def test_boot_recovery_hashes_equal_is_only_the_pre_comparison(self):
        # pre equal, post changed: within-baseline equality stays true, while the
        # across-run comparison is what fails.
        fake = FakeExecutor(post_hashes={"boot": REC, "recovery": OTHER})
        runner, _, _ = self.runner(fake)
        report = runner.run()
        self.assertTrue(report["boot_recovery_hashes_equal"])
        self.assertIs(report["partition_hashes_unchanged"], False)
        self.assertEqual(report["result"], "failed")

    def test_result_json_carries_collection_validity(self):
        runner, _, _ = self.runner()
        report = runner.run()
        stored = json.loads((runner.output / "result.json").read_text())
        self.assertTrue(stored["collection_verified"])
        self.assertTrue(stored["transport_verified"])
        self.assertFalse(stored["phase_verified"])

    def test_help_is_available_without_device(self):
        with self.assertRaises(SystemExit) as error:
            auto_debug.parse_args(["--help"])
        self.assertEqual(error.exception.code, 0)

    def test_result_json_is_owner_only_and_complete(self):
        runner, _, _ = self.runner()
        report = runner.run()
        path = runner.output / "result.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(path.read_text())
        for key in ("result", "phase_verified", "recovery_boot_verified",
                    "transport_verified", "boot_id_freshness", "pre_boot_id",
                    "recovery_boot_id", "boot_command_to_recovery_elapsed",
                    "partition_hashes_unchanged", "warnings"):
            self.assertIn(key, stored)
        self.assertEqual(stored["result"], report["result"])
        self.assertFalse(stored["phase_verified"])


if __name__ == "__main__":
    unittest.main()