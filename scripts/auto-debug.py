#!/usr/bin/env python3
"""One-shot, non-flashing host debug runner. Default: offline dry-run.

Only --run permits `adb reboot bootloader` and one `fastboot boot`.
A changed kernel boot ID confirms a new recovery boot, NOT a firmware phase.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
import uuid


DEFAULT_SERIAL = "d25f844e"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
BOOT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)
RECOMMENDED_RECOVERY_SHA256 = (
    "8f488a0a2a766f0a1076a21be0a9f0187b2ee8e598bebd64391236b6d155fb1b"
)
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PSTORE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
PARTITIONS = ("boot", "recovery")
# Every comparison uses this ordered tuple: `recovery_*` is the slot the --run
# session booted from, `post_*` is the slot it came back to.


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def execute(argv, timeout):
    """No local shell; capture both streams, including partial timeout output."""
    return subprocess.run(argv, timeout=timeout, capture_output=True, check=False)


def as_bytes(value):
    if value is None:
        return b""
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def private_write(path, data):
    # New files only: do not follow an existing artifact symlink.
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(as_bytes(data))


def digest_pair(hashes):
    """Ordered boot/recovery tuple, or None when either hash is missing."""
    if not hashes or len(hashes) != len(PARTITIONS):
        return None
    return tuple(hashes[partition] for partition in PARTITIONS)


class Runner:
    def __init__(self, args, executor=execute, monotonic=time.monotonic,
                 sleep=time.sleep, timestamp=utc_now):
        self.args = args
        self.executor = executor
        self.monotonic = monotonic
        self.sleep = sleep
        self.timestamp = timestamp
        root = Path(args.output).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        # UTC name plus random suffix, exclusive mkdir: never overwrite a run.
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.output = root / (name + "-" + uuid.uuid4().hex[:12])
        self.output.mkdir(mode=0o700)
        self.commands = []
        self.transitions = []
        self.last_state = None
        self.pinned_boot_id = None
        self.report = {
            "mode": args.mode, "serial": args.serial, "started_utc": timestamp(),
            "result": None, "phase_verified": False,
            "phase_evidence": "No explicit firmware evidence adapter is installed.",
            # Transport facts are kept separate from the (unproven) XNU phase.
            "recovery_boot_verified": False,
            "transport_verified": False,
            "collection_verified": False,
            "boot_id_verified": False,
            "boot_id_freshness": "unknown",
            "pre_boot_id": None,
            "recovery_boot_id": None,
            "boot_command_to_recovery_elapsed": None,
            "partition_hashes_unchanged": None,
            "boot_recovery_hashes_equal": None,
            "commands": self.commands, "transitions": self.transitions,
            "artifacts": [], "errors": [], "warnings": [],
        }

    def artifact(self, name, data):
        private_write(self.output / name, data)
        self.report["artifacts"].append(name)

    def transition(self, state):
        if state != self.last_state:
            self.transitions.append({"utc": self.timestamp(),
                                     "monotonic": self.monotonic(), "state": state})
            self.last_state = state

    def warning(self, message):
        self.report["warnings"].append(message)

    def command(self, argv, timeout=None):
        """Run one bounded command; an exhausted deadline is a normal miss.

        `timeout` is the caller's remaining wait budget. It is clamped by
        --command-timeout, so no single command can outlive the wait deadline,
        and a budget that is already gone returns without launching anything.
        Callers check the deadline themselves; this never raises for it.
        """
        if self.args.mode == "dry-run":
            raise RuntimeError("dry-run cannot execute subprocesses")
        limit = self.args.command_timeout if timeout is None else min(
            self.args.command_timeout, timeout)
        if limit <= 0:
            return None, b"", b"deadline exhausted"
        record = {"argv": list(argv), "utc": self.timestamp(), "timeout": limit}
        started = self.monotonic()
        try:
            completed = self.executor(list(argv), timeout=limit)
            out, err, rc = as_bytes(completed.stdout), as_bytes(completed.stderr), completed.returncode
            record["timed_out"] = False
        except subprocess.TimeoutExpired as exc:
            out, err, rc = as_bytes(exc.stdout), as_bytes(exc.stderr), None
            record["timed_out"] = True
        except OSError as exc:
            out, err, rc = b"", str(exc).encode(), None
            record["timed_out"] = False
            record["launch_error"] = True
        index = len(self.commands) + 1
        prefix = f"command-{index:03d}"
        self.artifact(prefix + ".stdout", out)
        self.artifact(prefix + ".stderr", err)
        record.update({"returncode": rc, "elapsed": self.monotonic() - started,
                       "stdout": prefix + ".stdout", "stderr": prefix + ".stderr"})
        self.commands.append(record)
        return rc, out, err

    def adb(self, *argv, timeout=None):
        return self.command(["adb", "-s", self.args.serial, *argv], timeout)

    def device_state(self, output):
        for line in output.decode("utf-8", "replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == self.args.serial:
                return fields[1]
        return "disconnected"

    def read_boot_id(self, timeout=None):
        """Current kernel boot ID, or None when unreadable or malformed.

        The whole output must be exactly one canonical UUID: trailing or leading
        text ("<uuid> garbage") is rejected rather than resynchronised. The
        caller passes its remaining wait budget, so this read can never outlive
        the wait that asked for it.
        """
        rc, output, _ = self.adb("shell", "cat", BOOT_ID_PATH, timeout=timeout)
        if rc != 0:
            return None
        text = output.decode("utf-8", "replace").strip()
        if not BOOT_ID_RE.fullmatch(text):
            return None
        return text.lower()

    def remaining(self, deadline):
        return None if deadline is None else deadline - self.monotonic()

    def expired(self, deadline):
        """True once the wait budget is spent (a miss, never a failure)."""
        return deadline is not None and self.remaining(deadline) <= 0

    def recovery(self, deadline=None, require_new_boot=False):
        """Confirmed TWRP; with require_new_boot, also a new kernel boot ID.

        Every command inside is bounded by the remaining wait budget, and the
        budget is re-checked after each one: once it is gone this returns False
        (a miss, not a failure). A recovery reconnect carrying the pre-boot ID
        stays "pending": the same deadline keeps waiting for the new ID, and
        such a session is never reported as a returned recovery.
        """
        budget = self.remaining(deadline)
        if deadline is not None and budget <= 0:
            return False
        rc, output, _ = self.command(["adb", "devices"], budget)
        if deadline is not None and self.remaining(deadline) <= 0:
            return False
        state = self.device_state(output) if rc == 0 else "adb-unavailable"
        self.transition("adb:" + state)
        if state != "recovery":
            return False
        rc, output, _ = self.adb("shell", "getprop", "ro.twrp.version",
                                 timeout=self.remaining(deadline))
        if deadline is not None and self.remaining(deadline) <= 0:
            return False
        if rc != 0 or not output.strip():
            self.transition("recovery:twrp-unverified")
            return False
        self.transition("recovery:twrp-confirmed")
        if self.pinned_boot_id is not None:
            budget = self.remaining(deadline)
            if deadline is not None and budget <= 0:
                return False
            started = self.monotonic()
            boot_id = self.read_boot_id(budget)
            finished = self.monotonic()
            # A read whose own duration reached the budget leaves no room to
            # act on its answer before the deadline, so it is untimely.
            if (deadline is not None
                    and (finished >= deadline or finished - started >= budget)):
                # The read consumed (or nearly consumed) the budget: the answer
                # is untimely, so it cannot confirm recovery in this window.
                self.transition("recovery:boot-id-unverified")
                return False
            if boot_id is not None:
                self.transition("recovery:boot-id-observed")
            if boot_id == self.pinned_boot_id:
                self.transition("recovery:stale-boot-id")
                return False
            if boot_id is None:
                self.transition("recovery:boot-id-unverified")
                if require_new_boot:
                    return False
            else:
                self.report["recovery_boot_id"] = boot_id
                self.report["boot_id_verified"] = True
        return True

    def wait(self, kind):
        deadline = self.monotonic() + self.args.wait_timeout
        while self.monotonic() < deadline:
            if kind == "recovery":
                found = self.recovery(deadline, require_new_boot=True)
            else:
                rc, output, _ = self.command(["fastboot", "devices"],
                                             self.remaining(deadline))
                if self.remaining(deadline) <= 0:
                    break
                state = self.device_state(output) if rc == 0 else "unavailable"
                self.transition("fastboot:" + state)
                found = rc == 0 and state == "fastboot"
            if found and self.monotonic() <= deadline:
                return True
            left = self.remaining(deadline)
            if left > 0:
                self.sleep(min(self.args.poll_interval, left))
        self.transition(kind + ":timeout")
        return False

    def partition_hashes(self, label, deadline=None):
        hashes = {}
        for partition in PARTITIONS:
            rc, output, _ = self.adb("shell", "sha256sum", "/dev/block/by-name/" + partition,
                                     timeout=self.remaining(deadline))
            fields = output.decode("utf-8", "replace").split()
            if rc == 0 and fields and HASH_RE.fullmatch(fields[0]):
                hashes[partition] = fields[0].lower()
            else:
                # One deterministic message per unreadable partition; failures
                # below add the consequence (equality unverified / changed).
                self.report["errors"].append(label + ": cannot hash " + partition)
        self.report[label + "_partition_sha256"] = hashes
        return hashes

    def read_only_baseline(self):
        """--run only: pre partition hashes, boot ID and pstore list before reboot.

        Refuses reboot when either baseline hash or the pre-boot ID is missing.
        """
        pre = self.partition_hashes("pre")
        if digest_pair(pre) is None:
            raise RuntimeError("readonly baseline partition hashes unavailable; refusing reboot")
        boot_id = self.read_boot_id()
        if boot_id is None:
            raise RuntimeError(
                "pre-boot kernel boot ID unavailable from " + BOOT_ID_PATH + "; refusing reboot")
        self.pinned_boot_id = boot_id
        self.report["pre_boot_id"] = boot_id
        self.report["boot_id_freshness"] = "pinned-pre-boot-id"
        rc, listing, _ = self.adb("shell", "ls", "-1", "/sys/fs/pstore")
        self.artifact("pre-pstore-list.txt", listing)
        if rc != 0:
            self.report["errors"].append("pre pstore listing unavailable")
        self.transition("baseline:recorded")

    def compare_hashes(self):
        """Post pair vs the single pre baseline; equality across the run.

        Returns True only when both pairs were readable and identical. A changed
        pair is an error, an unreadable pair leaves equality unknown; both are
        False. `boot_recovery_hashes_equal` is NOT reused here: it only ever
        means the pre-boot run saw boot == recovery.
        """
        before = digest_pair(self.report.get("pre_partition_sha256"))
        after = digest_pair(self.report.get("post_partition_sha256"))
        if before is None and self.report["pre_boot_id"] is None:
            # No pre-boot session at all (--collect-only): nothing was promised,
            # so equality is unknown rather than an error.
            self.report["partition_hashes_unchanged"] = None
            return False
        if before is None or after is None:
            self.report["partition_hashes_unchanged"] = None
            self.report["errors"].append(
                "pre-reboot boot/recovery hashes unavailable; nothing to compare"
                if before is None else
                "post-recovery boot/recovery hashes unavailable; equality unverified")
            return False
        unchanged = before == after
        self.report["partition_hashes_unchanged"] = unchanged
        if not unchanged:
            self.report["errors"].append(
                "boot/recovery partition hashes changed across the temporary boot")
        return unchanged

    def check_recovery_hashes(self):
        """Pre-boot: boot and recovery must hold the same bytes (--run only).

        Reads the `pre` baseline (the only pre-boot record) and sets
        `boot_recovery_hashes_equal` to that within-baseline comparison.
        An explicit --expected-recovery-sha256 must match exactly; without it a
        recovery hash different from RECOMMENDED_RECOVERY_SHA256 only warns.
        """
        pair = digest_pair(self.report.get("pre_partition_sha256"))
        if pair is None:
            raise RuntimeError("boot/recovery hashes unavailable; refusing reboot")
        boot, recovery = pair
        self.report["boot_recovery_hashes_equal"] = boot == recovery
        if recovery != RECOMMENDED_RECOVERY_SHA256:
            self.warning("recovery partition does not match RECOMMENDED_RECOVERY_SHA256 "
                         + RECOMMENDED_RECOVERY_SHA256)
        if self.args.expected_recovery_sha256:
            if recovery != self.args.expected_recovery_sha256.lower():
                raise RuntimeError("recovery partition SHA256 mismatch with "
                                   "--expected-recovery-sha256; refusing reboot")
        if boot != recovery:
            raise RuntimeError("boot and recovery partition hashes differ; "
                               "refusing reboot")
        self.transition("baseline:hashes-equal")

    def image_evidence(self):
        if not self.args.image:
            return
        source = Path(self.args.image).expanduser().resolve(strict=True)
        if not source.is_file():
            raise RuntimeError("image must be a regular file")
        digest, size = hashlib.sha256(), 0
        # --run boots the private staged bytes we hash, not a changeable source path.
        staged = self.output / "boot-image.img"
        stream = staged.open("xb") if self.args.mode == "run" else None
        try:
            if stream:
                os.chmod(staged, 0o600)
            with source.open("rb") as image:
                for block in iter(lambda: image.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
                    if stream:
                        stream.write(block)
        finally:
            if stream:
                stream.close()
        actual = digest.hexdigest()
        self.report["image"] = {"source": str(source), "size_bytes": size,
                                "sha256": actual, "expected_sha256": self.args.expected_sha256}
        if stream:
            self.report["artifacts"].append(staged.name)
            self.report["image"]["boot_path"] = str(staged)
        if size == 0:
            raise RuntimeError("empty image")
        if self.args.expected_sha256 and actual != self.args.expected_sha256.lower():
            raise RuntimeError("image SHA256 mismatch; refusing reboot")

    def collect(self, deadline=None):
        """Collect recovery evidence. Returns (collection_complete, hashes_unchanged).

        Every read is mandatory: a failed log/pstore read, a skip because the
        collection budget ran out, an unknown boot ID, or a boot ID change
        mid-collection marks the collection incomplete, and partition equality
        is then only recorded, never credited. A collection that is incomplete
        is never reported as a full success.
        """
        complete = True

        def missing(message):
            nonlocal complete
            complete = False
            self.report["errors"].append(message)

        for name, argv in (
            ("dmesg.bin", ("exec-out", "dmesg")),
            ("recovery.log", ("exec-out", "cat", "/tmp/recovery.log")),
            ("cmdline.bin", ("exec-out", "cat", "/proc/cmdline")),
        ):
            rc, output, _ = self.adb(*argv, timeout=self.remaining(deadline))
            self.artifact(name, output)
            if rc is None:
                missing("collection budget exhausted before " + name)
            elif rc != 0:
                missing("collection failed: " + name)
        # Re-read after collection: this boot ID must still be the confirmed one.
        boot_id = self.read_boot_id(self.remaining(deadline))
        confirmed = self.report["recovery_boot_id"]
        if boot_id is None:
            missing("post-collection boot ID unavailable")
        elif confirmed is not None and boot_id != confirmed:
            missing("boot ID changed during collection")
        else:
            self.report["recovery_boot_id"] = boot_id
        rc, listing, _ = self.adb("shell", "ls", "-1", "/sys/fs/pstore",
                                  timeout=self.remaining(deadline))
        self.artifact("pstore-list.txt", listing)
        if rc != 0:
            missing("pstore listing unavailable")
        else:
            names = listing.decode("utf-8", "replace").splitlines()
            seen = set()
            for name in names:
                name = name.rstrip("\r")
                if not PSTORE_NAME_RE.fullmatch(name) or name in seen:
                    missing("rejected unsafe/duplicate pstore entry")
                    continue
                seen.add(name)
                path = "/sys/fs/pstore/" + name
                # Validated fixed-root basename; reject directories and symlinks.
                # exec-out preserves raw pmsg bytes (no PTY newline conversion).
                script = f"test -f {path} && test ! -L {path} && cat {path}"
                rc, data, _ = self.adb("exec-out", "sh", "-c", shlex.quote(script),
                                       timeout=self.remaining(deadline))
                if rc == 0:
                    self.artifact("pstore-" + name, data)
                else:
                    missing("pstore read failed: " + name)
        if self.partition_hashes("post", deadline):
            self.transition("post:hashes-recorded")
        unchanged = self.compare_hashes()
        if not complete:
            self.transition("collection:incomplete")
        elif unchanged:
            self.transition("collection:verified")
        elif unchanged is False:
            self.transition("collection:hash-mismatch")
        # collection_verified: every read succeeded and the boot ID held.
        # transport_verified: additionally the partitions compared equal, which
        # is impossible without a pre baseline; --collect-only therefore keeps it
        # false instead of assuming success.
        self.report["collection_verified"] = complete
        self.report["transport_verified"] = complete and unchanged is True
        return complete, unchanged

    def collect_only(self):
        """Wait for recovery and collect. No pre-boot ID exists in this mode.

        With no pre baseline there is no hash comparison and no freshness, so
        transport_verified stays false and partition equality stays unknown; the
        result only says a recovery was reachable and its collection was valid.
        """
        self.report["boot_id_freshness"] = "unknown"
        self.report["boot_id_verified"] = False
        self.report["recovery_boot_verified"] = False
        if not self.wait("recovery"):
            self.report["result"] = "timeout"
            return
        collected, _ = self.collect(self.collection_deadline())
        self.report["result"] = "recovery_returned" if collected else "failed"

    def collection_deadline(self):
        """Readonly collection gets its own bounded budget, not the wait's last
        milliseconds: the wait has already expired by the time it runs."""
        return self.monotonic() + self.args.wait_timeout

    def inspect(self):
        """Readonly: must confirm TWRP before any other device command."""
        if not self.recovery():
            self.report["result"] = "failed"
            self.report["errors"].append(
                "exact serial is not confirmed TWRP recovery; refusing to inspect")
            return
        hashes = self.partition_hashes("pre")
        pair = digest_pair(hashes)
        self.report["boot_recovery_hashes_equal"] = None if pair is None else pair[0] == pair[1]

    def run(self):
        try:
            self.image_evidence()
            if self.args.mode == "dry-run":
                self.report["plan"] = [
                    "Confirm exact serial in adb recovery and ro.twrp.version",
                    "Capture readonly boot/recovery SHA256, pre-boot ID and pstore listing",
                    "Require boot/recovery partition hashes to be equal",
                    "Only --run: reboot bootloader, bounded fastboot wait, boot image ONCE",
                    "Bounded recovery wait for a DIFFERENT kernel boot ID",
                    "Collect raw logs/pstore, re-read boot ID and post SHA256",
                ]
            elif self.args.mode == "collect-only":
                self.collect_only()
            elif self.args.mode == "inspect":
                self.inspect()
            else:
                self.run_session()
        except (OSError, RuntimeError) as exc:
            self.report["errors"].append(str(exc))
            self.report["result"] = "failed"
        finally:
            self.report["phase_verified"] = False
            self.report["errors"] = list(dict.fromkeys(self.report["errors"]))
            self.report["warnings"] = list(dict.fromkeys(self.report["warnings"]))
            self.report["finished_utc"] = self.timestamp()
            self.artifact("result.json", json.dumps(self.report, indent=2) + "\n")
        return self.report

    def run_session(self):
        if not self.recovery():
            raise RuntimeError("exact serial is not confirmed TWRP recovery; refusing reboot")
        self.read_only_baseline()
        self.check_recovery_hashes()
        rc, _, _ = self.adb("reboot", "bootloader")
        if rc != 0:
            raise RuntimeError("adb reboot bootloader failed")
        if not self.wait("fastboot"):
            self.report["result"] = "timeout"
            return
        rc, _, _ = self.command(["fastboot", "-s", self.args.serial,
                               "boot", self.report["image"]["boot_path"]])
        if rc != 0:
            raise RuntimeError("fastboot boot failed or timed out; no retry")
        # Timed from the moment the boot command finished, not from before it.
        boot_command_completed = self.monotonic()
        self.transition("fastboot:boot-command-completed")
        if not self.wait("recovery"):
            stale = any(t["state"] == "recovery:stale-boot-id" for t in self.transitions)
            self.report["result"] = "stale_boot_id" if stale else "timeout"
            if stale:
                self.report["errors"].append(
                    "only the pre-boot kernel boot ID came back within the deadline; "
                    "a new recovery boot was never confirmed")
            return
        # Boot command completion to confirmed NEW recovery boot ID. This is a
        # transport observation only and says nothing about firmware progress.
        self.report["boot_command_to_recovery_elapsed"] = (
            self.monotonic() - boot_command_completed)
        self.transition("recovery:new-boot-id-confirmed")
        # A new recovery boot was reached; whether the collection was valid and
        # whether the partitions are unchanged are separate facts.
        self.report["recovery_boot_verified"] = True
        self.report["boot_id_verified"] = True
        complete, _ = self.collect(self.collection_deadline())
        self.report["result"] = (
            "recovery_returned"
            if complete and self.report["partition_hashes_unchanged"] is True
            else "failed")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run", dest="mode", action="store_const", const="run",
                       help="explicitly allow reboot and ONE temporary fastboot boot")
    modes.add_argument("--inspect", dest="mode", action="store_const", const="inspect",
                       help="readonly connected-device baseline; never reboot")
    modes.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run",
                       help="offline image evidence and plan; no subprocess calls (default)")
    modes.add_argument("--collect-only", dest="mode", action="store_const", const="collect-only",
                       help="wait for recovery and collect artifacts; never reboot")
    parser.set_defaults(mode="dry-run")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="exact device serial (default: %(default)s)")
    parser.add_argument("--image", help="boot image; required for --run")
    parser.add_argument("--expected-sha256", help="pinned 64-hex image hash; required for --run")
    parser.add_argument("--expected-recovery-sha256",
                        help="optional pinned 64-hex recovery partition hash; must match exactly")
    parser.add_argument("--output", default="auto-debug-runs", help="parent directory for unique UTC run directories")
    parser.add_argument("--command-timeout", type=positive, default=30, help="per-command seconds (default: 30)")
    parser.add_argument("--wait-timeout", type=positive, default=120, help="seconds per bounded device wait (default: 120)")
    parser.add_argument("--poll-interval", type=positive, default=2, help="seconds between device polls (default: 2)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", args.serial):
        parser.error("serial must be a nonempty device identifier")
    if args.expected_sha256 and not HASH_RE.fullmatch(args.expected_sha256):
        parser.error("--expected-sha256 must contain exactly 64 hexadecimal digits")
    if args.expected_recovery_sha256:
        if not HASH_RE.fullmatch(args.expected_recovery_sha256):
            parser.error("--expected-recovery-sha256 must contain exactly 64 hexadecimal digits")
        args.expected_recovery_sha256 = args.expected_recovery_sha256.lower()
    if args.mode == "run" and (not args.image or not args.expected_sha256):
        parser.error("--run requires --image and --expected-sha256")
    return args


def main(argv=None):
    runner = Runner(parse_args(argv))
    report = runner.run()
    print(json.dumps({"output": str(runner.output), "mode": report["mode"],
                      "result": report["result"], "phase_verified": False,
                      "recovery_boot_verified": report["recovery_boot_verified"],
                      "collection_verified": report["collection_verified"],
                      "transport_verified": report["transport_verified"],
                      "boot_id_freshness": report["boot_id_freshness"],
                      "partition_hashes_unchanged": report["partition_hashes_unchanged"],
                      "boot_command_to_recovery_elapsed":
                          report["boot_command_to_recovery_elapsed"]}))
    return 1 if report["result"] in ("timeout", "failed", "stale_boot_id") else 0


if __name__ == "__main__":
    raise SystemExit(main())