#!/usr/bin/env python3
"""Read back what the firmware wrote to the phone's logdump partition.

The panel is the only live output on gauguin, and a phone photographed by hand is a
poor way to read a boot that failed. PlogDxe copies the firmware's two RAM rings into
the first 256 KiB of the `logdump` partition on a timer, so a run that hangs still
leaves its log in flash, where it survives the reset into recovery.

    # The phone is in recovery with adb answering:
    python3 scripts/read-logdump.py --pull
    python3 scripts/read-logdump.py                 # decode the pull already here
    python3 scripts/read-logdump.py --file x.bin    # decode a dump taken by hand

The dump is written by the firmware as:

    0x000  magic   0x474F4C50 ("PLOG")
    0x004  version 2
    0x008  seq     how many copies were written this boot
    0x00C  total   bytes of index and records that follow
    0x010  id      that many bytes: PLOG_RECORD { kind, base, length, reserved, text }

and each record carries the address its ring lives at, so a dump says which region it
came from. Version 1 was a single header and flat text; it is still decoded, and the
two streams cannot be told apart in it, which is why the format changed.

Everything here is best effort: a phone that was power cycled between the firmware
stopping and the dump reading empties the partition, so an absent or all-zero block is
the normal "the run left nothing" answer, not a bug in this script.
"""

import argparse
import pathlib
import struct
import subprocess
import sys

LOG_PARTITION = "/dev/block/by-name/logdump"
SIZE = 0x40000  # 256 KiB: what PlogDxe writes, and enough for both rings

LOCAL = pathlib.Path("/tmp/logdump.bin")

MAGIC = 0x474F4C50  # "PLOG"
VERSION_2 = 2

# Ring headers, as their writers define them.
P2LG_MAGIC = 0x474C3250  # FrameBufferSerialPortLib: DebugPrint stream
ULUG_MAGIC = 0x474E554C47554C55  # LogBufferLib: dispatch/exception events

STREAM_NAMES = {
    1: "CONSOLE",  # DebugPrint
    2: "EVENTS",  # dispatcher + exceptions
}

RECORD = struct.Struct("<IIII")  # kind, base, length, reserved


def pull(serial=None):
    """Copy the partition out through adb.

    dd with a block count, not cat: the partition is 64 MiB and only the first 256 KiB
    carry the log.
    """
    cmd = f"dd if={LOG_PARTITION} of=/tmp/logdump.bin bs=4096 count={SIZE // 4096} 2>&1"
    print(f"adb shell {cmd}")
    adb = ["adb"] + (["-s", serial] if serial else [])
    subprocess.run(adb + ["shell", cmd], check=True)
    subprocess.run(adb + ["pull", "/tmp/logdump.bin", str(LOCAL)], check=True)


def unwrap(text, head, total, capacity):
    """Order a ring's bytes oldest first.

    A ring that never wrapped has `head` bytes starting at 0; one that wrapped holds
    exactly `capacity` bytes of order, starting at `head`.
    """
    if total <= capacity:
        return text[:head]
    return text[head:] + text[:head]


def decode_v2(data):
    """Yield (kind, base, text) for every record in a version 2 dump."""
    total = struct.unpack_from("<I", data, 0x0C)[0]
    end = min(0x10 + total, len(data))
    off = 0x10
    while off + RECORD.size <= end:
        kind, base, length, _ = RECORD.unpack_from(data, off)
        text_off = off + RECORD.size
        if text_off + length > end:
            break
        yield kind, base, data[text_off:text_off + length]
        off = text_off + ((length + 3) & ~3)


def decode_v1(data):
    """The old layout: a header whose word 1 is the text length, then the text."""
    length = struct.unpack_from("<I", data, 0x04)[0]
    if 0 < length <= len(data) - 0x10:
        yield 0, 0, data[0x10:0x10 + length]


def describe(base):
    """Name a ring by the address its writer uses, since the record only has the address."""
    known = {
        0xAFBFC000: "FrameBufferSerialPortLib ring",
        0x9FFF7000: "LogBufferLib ring",
    }
    return known.get(base, "unknown region")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", action="store_true", help="copy the partition off the phone first")
    ap.add_argument("--serial", help="adb serial, when more than one device is attached")
    ap.add_argument("--file", help="decode this dump instead of the one pulled from the phone")
    ap.add_argument("--out", help="write each stream to a file under this directory")
    ap.add_argument("--quiet", action="store_true", help="write the files but print no text")
    args = ap.parse_args()

    if args.pull:
        try:
            pull(args.serial)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            sys.exit(f"could not pull the logdump partition: {exc}")

    path = pathlib.Path(args.file) if args.file else LOCAL
    if not path.is_file():
        sys.exit(f"no dump at {path}; run with --pull while the phone is in recovery")

    data = path.read_bytes()
    if not any(data):
        sys.exit(
            f"{path} is all zero: nothing was written to logdump.\n"
            "That is the normal answer when the firmware never reached PlogDxe (it needs a block\n"
            "I/O device, so no storage driver means no log), or when the phone was power cycled\n"
            "rather than reset. It does not mean the dump failed."
        )

    magic, version = struct.unpack_from("<II", data, 0)
    if magic != MAGIC:
        sys.exit(
            f"{path}: magic is 0x{magic:08X}, expected 0x{MAGIC:08X}.\n"
            "Either the firmware never ran with PlogDxe in the volume, or this is stale data\n"
            "from some other writer. The partition reads back as all zero on a device that never\n"
            "wrote a log, so a foreign magic is worth looking at before it is dismissed."
        )

    seq = struct.unpack_from("<I", data, 0x08)[0]
    print(f"# logdump: version {version}, copy #{seq} of this boot")

    records = list(decode_v2(data) if version >= VERSION_2 else decode_v1(data))
    if not records:
        sys.exit("the header is valid but no record decodes: the block was truncated mid-write")

    out_dir = pathlib.Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for kind, base, text in records:
        name = STREAM_NAMES.get(kind, f"STREAM{kind}")
        print(f"\n===== {name} ({len(text)} bytes, id 0x{base:08X}, {describe(base)}) =====")
        decoded = text.decode("ascii", "replace")
        if out_dir:
            target = out_dir / f"{name.lower()}.log"
            target.write_text(decoded)
            print(f"# written to {target}")
        if not args.quiet:
            sys.stdout.write(decoded if decoded.endswith("\n") else decoded + "\n")


if __name__ == "__main__":
    main()