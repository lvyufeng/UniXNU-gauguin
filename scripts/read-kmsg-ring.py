#!/usr/bin/env python3
"""Read the mtdoops kmsg ring on gauguin, without ever writing to the device.

The ring is eight 2 MiB mtdoops slots in `/dev/block/sda15`, which the recovery
kernel binds with `block2mtd.block2mtd=/dev/block/sda15,2097152` and
`mtdoops.mtddev=0`. Each slot that holds a record starts with an 8-byte header,
`<seq:u32 LE, magic:u32 LE>` where the magic is `0x5d005d00`, and the text
record that follows repeats the seq as `# Index:`. An erased slot reads
`0xFFFFFFFF` throughout.

Default mode parses a capture already on the host (`--file`). `--pull` is the
only mode that talks to a device, and it is deliberately narrow:

  1. `blockdev --getsize64 /dev/block/sda15` must report exactly 16777216;
  2. `/proc/cmdline` must carry `block2mtd.block2mtd=/dev/block/sda15,2097152`
     as an exact token, and `/proc/mtd` must map `mtd0` to that same exact
     quoted path with size `01000000` and erasesize `00200000`;
  3. one `dd if=/dev/block/sda15 ...` reads the 16 MiB back over exec-out.

It never pushes, never reboots, never mounts and never writes a partition.

What the bytes prove is limited: seq values are the writer's own counter, and
a Linux `Restarting system` line is Linux logcat/kmsg. Nothing here shows that
a firmware phase ran, so `phase_verified` is always false and every record's
`origin` is `recovery_linux_or_unknown`. A run id must come from the caller.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone

MTD_DEVICE = "/dev/block/sda15"
RING_BYTES = 16 * 1024 * 1024
SLOT_BYTES = 2 * 1024 * 1024
SLOT_COUNT = 8
REQUIRED_BYTES = RING_BYTES

# First 8 bytes of a written slot: the writer's sequence number and a fixed
# magic. Measured on this device: seq 833 and 838 both carried 0x5d005d00.
HEADER = "<II"
MAGIC = 0x5D005D00
ERASED_WORD = 0xFFFFFFFF

# mtdoops erases a whole block before writing; gauguin's block is 2 MiB.
MTD_SIZE_FIELD = "01000000"
MTD_ERASE_FIELD = "00200000"
MTD0_RE = re.compile(
    r'^mtd0:\s+(?P<size>[0-9A-Fa-f]{8})\s+(?P<erase>[0-9A-Fa-f]{8})\s+'
    r'"(?P<path>[^"]*)"\s*$')
BLOCK2MTD_TOKEN_RE = re.compile(r"^block2mtd\.block2mtd=(?P<device>[^,]+),"
                                r"(?P<size>\d+)$")

DEFAULT_TIMEOUT = 60

# `adb exec-out` fuses the command's stdout and stderr onto one pipe, so dd's
# trailing summary ("256+0 records in / ... bytes copied") would otherwise land
# at the end of the capture and make it 84 bytes longer than the partition.
# Dropping stderr *on the dd command only* is what keeps the byte stream exactly
# the device's; the exit status and the exact length are still checked, and a
# capture with any extra bytes is a failure rather than something to truncate.
DD_COMMAND = "dd if={device} bs=65536 count={blocks} 2>/dev/null"
SNIPPET_LINES = 8
SNIPPET_MAX_BYTES = 512

HEAD_RE = re.compile(rb"# Index: (\d{1,10})")
BUILD_RE = re.compile(rb"## Build: ([^\n\t]{1,200})")
REASON_RE = re.compile(rb"## Reason: ([^\n\t]{1,100})")
DATE_RE = re.compile(rb"### (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
LOGTYPE_RE = re.compile(rb"#### Log type: ?([^\n\t]{0,80})")
POWERUP_RE = re.compile(rb"Powerup reason=(0x[0-9A-Fa-f]{1,16})")
LINUX_VERSION_RE = re.compile(rb"Linux version (\d+\.\d+\.\d+[^\s\x00\n]{0,80})")
REBOOT_RE = re.compile(rb"reboot: (Restarting system[^\n\x00]{0,120})")
MACHINE_RE = re.compile(rb"Machine model: ([^\n\x00]{1,120})")
RESTART_CMD_RE = re.compile(rb"Received sys\.powerctl='reboot,([a-z]+)'")

ORIGIN = "recovery_linux_or_unknown"


class RingError(Exception):
    """A capture or device state that makes the parse meaningless."""


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def clean(value):
    """Field text safe to print and to serialise: printable ASCII only."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "backslashreplace")
    return "".join(char if 32 <= ord(char) < 127 else "?" for char in value)


def as_text(raw):
    return None if raw is None else raw.decode("utf-8", "backslashreplace")


def text_snippet(region, start, stop):
    """The matched line plus printable neighbours, centred on the match.

    A record's text section is followed by a binary logcat packet stream, so
    every candidate line must be printable on its own before it reaches JSON.
    """
    before = region.rfind(b"\n", 0, start) + 1
    line_end = region.find(b"\n", stop)
    if line_end < 0:
        line_end = min(len(region), stop)
    first = max(0, region.rfind(b"\n", 0, before) + 1)
    begin = max(0, region.rfind(b"\n", 0, first - 1) + 1)
    tail = region.find(b"\n", line_end + 1)
    end = line_end if tail < 0 else tail
    picked = []
    for line in region[begin:end].split(b"\n"):
        if len(line) > 400:
            continue
        if any(byte < 9 or 13 < byte < 32 or byte > 126 for byte in line):
            continue
        if line.strip():
            picked.append(clean(line.strip()))
    joined = "\n".join(picked[-SNIPPET_LINES:])
    return joined[:SNIPPET_MAX_BYTES] or None


def new_record(index, offset):
    return {
        "slot": index,
        "offset": offset,
        "state": "erased",
        "header_seq": None,
        "header_magic": None,
        "magic_known": None,
        "seq": None,
        "record_index": None,
        "header_index_matches_seq": None,
        "build": None,
        "reason": None,
        "time": None,
        "log_type": None,
        "powerup_reason": None,
        "linux_version": None,
        "machine_model": None,
        "reboot_line": None,
        "reboot_command": None,
        "origin": ORIGIN,
        "raw_sha256": None,
        "text_snippet": None,
        "unparsed": False,
        "notes": [],
    }


def parse_slot(slot, index, offset):
    """One slot. A slot that is not exactly one record is reported, not guessed."""
    record = new_record(index, offset)
    if len(slot) != SLOT_BYTES:
        record["state"] = "missing" if len(slot) < SLOT_BYTES else "malformed"
        record["unparsed"] = True
        record["raw_sha256"] = sha256_of(slot)
        record["notes"].append(
            "slot is {} bytes, not exactly {}".format(len(slot), SLOT_BYTES))
        return record

    record["raw_sha256"] = sha256_of(slot)
    seq = int.from_bytes(slot[0:4], "little")
    magic = int.from_bytes(slot[4:8], "little")
    record["header_seq"] = seq
    record["header_magic"] = "0x{:08x}".format(magic)

    if seq == ERASED_WORD and magic == ERASED_WORD:
        record["state"] = "erased"
        record["magic_known"] = False
        return record
    if magic != MAGIC:
        # Not our record format. Keep the header for later, parse nothing.
        record["state"] = "unknown_magic"
        record["magic_known"] = False
        record["unparsed"] = True
        record["notes"].append(
            "header magic {:#010x} is not {:#010x}; not parsing".format(
                magic, MAGIC))
        return record
    record["magic_known"] = True

    head_end = slot.find(b"\n```\t\n", 0, 4096)
    if head_end < 0:
        record["state"] = "malformed"
        record["unparsed"] = True
        record["notes"].append("no terminated mtdoops text header")
        return record

    head = slot[:head_end]
    index_match = HEAD_RE.search(head)
    if index_match is None:
        record["state"] = "malformed"
        record["unparsed"] = True
        record["notes"].append("header carries no '# Index:'")
        return record
    record["record_index"] = int(index_match.group(1))
    record["seq"] = seq
    record["header_index_matches_seq"] = record["record_index"] == seq
    if not record["header_index_matches_seq"]:
        record["state"] = "malformed"
        record["unparsed"] = True
        record["notes"].append(
            "'# Index: {}' disagrees with header seq {}".format(
                record["record_index"], seq))
        return record

    for key, pattern in (
        ("build", BUILD_RE),
        ("reason", REASON_RE),
        ("time", DATE_RE),
        ("log_type", LOGTYPE_RE),
    ):
        match = pattern.search(head)
        if match is None:
            record["notes"].append("header has no {}".format(key))
            continue
        record[key] = clean(match.group(1).strip())

    region = slot[head_end + len(b"\n```\t\n"):]
    for key, pattern in (
        ("powerup_reason", POWERUP_RE),
        ("linux_version", LINUX_VERSION_RE),
        ("machine_model", MACHINE_RE),
        ("reboot_line", REBOOT_RE),
        ("reboot_command", RESTART_CMD_RE),
    ):
        match = pattern.search(region)
        if match is None:
            continue
        record[key] = clean(match.group(1).strip())
        if key == "reboot_line":
            record["text_snippet"] = text_snippet(region, *match.span())
    if record["powerup_reason"] is None:
        record["notes"].append("no Powerup reason in the text region")

    record["state"] = "parsed"
    return record


def parse_ring(data):
    """Eight fixed slots. A capture of the wrong size is an error, not a hint."""
    if len(data) != REQUIRED_BYTES:
        raise RingError(
            "capture is {} bytes, expected {} (={} x {}): refusing to guess a "
            "slot layout".format(len(data), REQUIRED_BYTES, SLOT_COUNT,
                                 SLOT_BYTES))
    slots = [
        parse_slot(data[i * SLOT_BYTES:(i + 1) * SLOT_BYTES], i, i * SLOT_BYTES)
        for i in range(SLOT_COUNT)
    ]
    live = [slot for slot in slots if slot["state"] == "parsed"]
    indices = [slot["record_index"] for slot in live
               if slot["record_index"] is not None]
    return {
        "capture_bytes": len(data),
        "capture_sha256": sha256_of(data),
        "slot_bytes": SLOT_BYTES,
        "slot_count": SLOT_COUNT,
        "slots": slots,
        "live_slots": len(live),
        "erased_slots": sum(1 for s in slots if s["state"] == "erased"),
        "malformed_slots": sum(1 for s in slots if s["state"] == "malformed"),
        "missing_slots": sum(1 for s in slots if s["state"] == "missing"),
        "unknown_magic_slots": sum(
            1 for s in slots if s["state"] == "unknown_magic"),
        # Facts only: the observed record indices, in slot order.
        "record_indices": indices,
        "index_min": min(indices) if indices else None,
        "index_max": max(indices) if indices else None,
        "continuity_note": (
            "record indices are the writer's own sequence facts; their "
            "continuity is not evidence of which origin wrote them"),
    }


def build_report(data, source, pulled=None, device=None):
    parsed = parse_ring(data)
    return {
        "tool": "read-kmsg-ring",
        "schema": 1,
        "origin": ORIGIN,
        # Constant on purpose: nothing in a Linux capture can prove a phase ran.
        "phase_verified": False,
        "phase_evidence": "none: records are Linux kmsg/logcat",
        "run_id": None,
        "source": clean(source),
        "device": device,
        "pulled": pulled,
        **parsed,
    }


def parse_cmdline_binding(cmdline_text):
    """The exact block2mtd token, never a substring match."""
    for token in shlex.split(cmdline_text):
        match = BLOCK2MTD_TOKEN_RE.match(token)
        if match is None:
            continue
        if match.group("device") != MTD_DEVICE:
            raise RingError(
                "block2mtd binds {!r}, not {!r}".format(
                    clean(match.group("device")), MTD_DEVICE))
        if int(match.group("size")) != SLOT_BYTES:
            raise RingError(
                "block2mtd size is {}, expected {}".format(
                    clean(match.group("size")), SLOT_BYTES))
        return clean(token)
    raise RingError(
        "cmdline has no block2mtd.block2mtd={},{} token".format(
            MTD_DEVICE, SLOT_BYTES))


def parse_mtd0(mtd_text):
    """mtd0 must be our exact path with the geometry a 2 MiB slot implies."""
    line = next((ln for ln in mtd_text.splitlines()
                 if ln.strip().startswith("mtd0:")), None)
    if line is None:
        raise RingError("/proc/mtd has no mtd0")
    match = MTD0_RE.match(line.strip())
    if match is None:
        raise RingError("mtd0 line is not parseable: {!r}".format(
            clean(line.strip())[:200]))
    if match.group("size").lower() != MTD_SIZE_FIELD:
        raise RingError("mtd0 size is {}, expected {}".format(
            clean(match.group("size")), MTD_SIZE_FIELD))
    if match.group("erase").lower() != MTD_ERASE_FIELD:
        raise RingError("mtd0 erasesize is {}, expected {}".format(
            clean(match.group("erase")), MTD_ERASE_FIELD))
    # The quoted name block2mtd registers is "block2mtd: <device>"; the device
    # has to match exactly, so a longer or differently rooted path fails here.
    path = match.group("path")
    prefix = "block2mtd: "
    if not path.startswith(prefix):
        raise RingError("mtd0 name is {!r}, expected it to start with {!r}".format(
            clean(path), prefix))
    if path[len(prefix):] != MTD_DEVICE:
        raise RingError("mtd0 path is {!r}, expected {!r}".format(
            clean(path[len(prefix):]), MTD_DEVICE))
    return clean(line.strip())


def verify_mapping(shell, serial, expected_bytes):
    """Both halves of the identity that makes these bytes the ring.

    `shell` takes one device command and returns a completed process. Returns
    the evidence that was checked, because a capture is worthless if this
    silently failed.
    """
    if expected_bytes != REQUIRED_BYTES:
        raise RingError("expected-bytes must be {}, got {}".format(
            REQUIRED_BYTES, expected_bytes))

    size = shell("blockdev --getsize64 {}".format(MTD_DEVICE))
    if size.returncode != 0:
        raise RingError("blockdev failed on {}: {}".format(
            MTD_DEVICE, clean(as_text(size.stderr).strip())[:200]))
    try:
        reported = int(clean(as_text(size.stdout).strip()).splitlines()[-1])
    except (IndexError, ValueError):
        raise RingError("blockdev printed no size: {!r}".format(
            clean(as_text(size.stdout))[:200]))
    if reported != expected_bytes:
        raise RingError("{} is {} bytes, expected {}".format(
            MTD_DEVICE, reported, expected_bytes))

    cmdline = shell("cat /proc/cmdline")
    if cmdline.returncode != 0:
        raise RingError("cannot read /proc/cmdline")
    cmdline_text = as_text(cmdline.stdout)
    binding = parse_cmdline_binding(cmdline_text)

    mtd = shell("cat /proc/mtd")
    if mtd.returncode != 0:
        raise RingError("cannot read /proc/mtd")
    mtd_text = as_text(mtd.stdout)
    mtd0 = parse_mtd0(mtd_text)

    return {
        "serial": clean(serial),
        "device": MTD_DEVICE,
        "getsize64": reported,
        "cmdline_binding": binding,
        "cmdline_raw": clean(cmdline_text.strip()),
        "mtd0": mtd0,
        "mtd_raw": clean(mtd_text.strip()),
    }


def pull(serial, expected_bytes, shell=None, exec_out=None, out_root=None,
         timeout=DEFAULT_TIMEOUT):
    """Read-only capture into an exclusive directory. No push, no reboot."""
    if not serial:
        raise RingError("--pull requires an explicit --serial")
    if expected_bytes != REQUIRED_BYTES:
        raise RingError("expected-bytes must be {}, got {}".format(
            REQUIRED_BYTES, expected_bytes))
    if shell is None:
        def shell(command):
            return subprocess.run(["adb", "-s", serial, "shell", command],
                                  capture_output=True, check=False,
                                  timeout=timeout)
    if exec_out is None:
        def exec_out(command):
            return subprocess.run(["adb", "-s", serial, "exec-out", command],
                                  capture_output=True, check=False,
                                  timeout=timeout)

    try:
        mapping = verify_mapping(shell, serial, expected_bytes)
        capture = exec_out(DD_COMMAND.format(
            device=MTD_DEVICE, blocks=expected_bytes // 65536))
    except subprocess.TimeoutExpired as expired:
        raise RingError("adb command timed out after {}s: {!r}".format(
            clean(str(expired.timeout)),
            clean(" ".join(expired.cmd) if isinstance(expired.cmd, list)
                  else str(expired.cmd))[:200]))
    if capture.returncode != 0:
        raise RingError("dd failed: {}".format(
            clean(as_text(capture.stderr).strip())[:200]))
    data = capture.stdout
    if len(data) != expected_bytes:
        raise RingError("dd returned {} bytes, expected {}".format(
            len(data), expected_bytes))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(out_root) if out_root else Path("/tmp/gauguin-kmsg-ring")
    outdir = root / "ring-{}-{}".format(stamp, uuid.uuid4().hex[:8])
    os.makedirs(root, mode=0o700, exist_ok=True)
    outdir.mkdir(mode=0o700, exist_ok=False)
    os.chmod(outdir, 0o700)
    raw = outdir / "sda15-ring.bin"
    with raw.open("xb") as stream:
        os.chmod(raw, 0o600)
        stream.write(data)
    return data, str(raw), mapping


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Parse the gauguin mtdoops kmsg ring. Read-only; never "
                    "writes a partition.")
    parser.add_argument("--file", help="capture already on the host (default mode)")
    parser.add_argument("--pull", action="store_true",
                        help="capture from the device (read-only dd)")
    parser.add_argument("--serial", help="required with --pull")
    parser.add_argument("--expected-bytes", type=int, default=REQUIRED_BYTES,
                        help="required device size (must be %d)" % REQUIRED_BYTES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="seconds per adb call (%d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if args.file and args.pull:
        parser.error("--file and --pull are mutually exclusive")
    if not args.file and not args.pull:
        parser.error("give --file <capture> (or --pull for a read-only capture)")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    try:
        if args.pull:
            if args.expected_bytes != REQUIRED_BYTES:
                raise RingError(
                    "--expected-bytes must be {} to match {} x {} slots".format(
                        REQUIRED_BYTES, SLOT_COUNT, SLOT_BYTES))
            data, path, mapping = pull(args.serial, args.expected_bytes,
                                       timeout=args.timeout)
            report = build_report(data, path, pulled=mapping,
                                  device=MTD_DEVICE)
            report["raw_path"] = path
        else:
            data = Path(args.file).read_bytes()
            report = build_report(data, str(Path(args.file).resolve()))
    except (RingError, OSError) as error:
        print("read-kmsg-ring: {}".format(clean(str(error))), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print("capture {} bytes  sha256 {}".format(
        report["capture_bytes"], report["capture_sha256"]))
    print("origin {}  phase_verified {} (a Linux log cannot prove a phase)".format(
        report["origin"], report["phase_verified"]))
    for slot in report["slots"]:
        if slot["state"] != "parsed":
            print("  slot {} [{:>8}] {} magic={} seq={}".format(
                slot["slot"], slot["offset"], slot["state"],
                slot["header_magic"], slot["header_seq"]))
            continue
        print("  slot {} [{:>8}] index={} seq={} {} powerup={} {}".format(
            slot["slot"], slot["offset"], slot["record_index"],
            slot["header_seq"], clean(slot["time"]) or "?",
            clean(slot["powerup_reason"]) or "?",
            clean(slot["reboot_line"]) or ""))
        if slot["text_snippet"]:
            for line in slot["text_snippet"].splitlines()[:3]:
                print("      | {}".format(line))
    print("live={} erased={} malformed={} missing={} unknown_magic={}".format(
        report["live_slots"], report["erased_slots"],
        report["malformed_slots"], report["missing_slots"],
        report["unknown_magic_slots"]))
    print("record_indices={}".format(report["record_indices"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())