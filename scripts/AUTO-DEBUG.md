# Safe host auto-debug runner

`auto-debug.py` is a host-only, one-shot evidence collector. It never flashes,
erases, formats, pushes files to the device, or writes a partition. No firmware
changes are made by this script.

```sh
# Default: offline dry-run. No adb/fastboot subprocesses, no device changes.
python3 scripts/auto-debug.py --image /path/to/boot.img --output /tmp/debug-runs

# Explicit readonly inspection of the exact serial in TWRP recovery.
python3 scripts/auto-debug.py --inspect --serial d25f844e --output /tmp/debug-runs

# Explicitly opt into one temporary boot (NOT a persistent flash).
python3 scripts/auto-debug.py --run --serial d25f844e \
  --image /path/to/boot.img --expected-sha256 <64-hex-pinned-image-hash> \
  --output /tmp/debug-runs --command-timeout 30 --wait-timeout 120

# Same run, additionally pinning the recovery partition bytes exactly.
python3 scripts/auto-debug.py --run --image /path/to/boot.img \
  --expected-sha256 <64-hex-image-hash> \
  --expected-recovery-sha256 <64-hex-recovery-partition-hash>

# Collect recovery evidence only, including raw pmsg-ramoops files.
# This never initiates a reboot; it can wait for a manually initiated recovery.
python3 scripts/auto-debug.py --collect-only --output /tmp/debug-runs

python3 scripts/auto-debug.py --help
python3 -m unittest discover -s tests -p 'test_auto_debug.py' -v
```

Each invocation creates an exclusive UTC-named directory with a random suffix.
`result.json` indexes timestamped transitions, image hash/size, commands, their
full binary stdout/stderr files, exit codes and timeouts. Runs stage and hash a
private image copy before any device command and boot that exact copy. Pin the
expected hash independently; a hash computed from an untrusted image is not
approval of that image. Dry-run and inspect have `result: null` because no boot
attempt occurred; failures use `failed`.

## `--run` preconditions (refuse reboot otherwise)

1. `adb devices` shows the exact serial in state `recovery` and
   `ro.twrp.version` answers.
2. Readonly SHA256 of **both** `boot` and `recovery` partitions is readable.
   Unreadable baseline hashes refuse the reboot.
3. The `boot` and `recovery` partition hashes **must be identical**. They differ
   → reboot refused, result `failed`.
4. `--expected-recovery-sha256` (optional) must match the recovery partition
   hash exactly (compared case-insensitively) → mismatch refuses the reboot.
   Without it, a recovery hash different from `RECOMMENDED_RECOVERY_SHA256` only
   records a `warnings` entry; it never blocks.
5. The pre-boot kernel boot ID is read from `/proc/sys/kernel/random/boot_id`
   and saved as `pre_boot_id`; an unreadable or malformed ID refuses the reboot.
   The output must be exactly one canonical UUID — surrounding text such as
   `<uuid> garbage` is rejected rather than resynchronised.

It then reboots to bootloader, polls `fastboot devices` against a monotonic
deadline, executes `fastboot -s SERIAL boot IMAGE` **once**, and waits for a
confirmed TWRP recovery that also reports a **different** kernel boot ID.

## Boot ID freshness

A recovery session is only accepted when its boot ID differs from
`pre_boot_id`. A reconnect still carrying the old ID is *pending*, not a
return: it is recorded as the `recovery:stale-boot-id` transition, never
reported as `recovery_returned`, and the same single bounded deadline keeps
waiting for the new ID. If the deadline expires with only stale sightings the
result is `stale_boot_id` (exit code 1); with no recovery sightings at all it is
`timeout`.

`--collect-only` has no pre-boot ID to compare against, so it reports
`boot_id_freshness: "unknown"`, `pre_boot_id: null`,
`recovery_boot_verified: false`, `transport_verified: false` and
`partition_hashes_unchanged: null` even when it reaches TWRP. It never requires
a boot ID change: its `recovery_returned` means "recovery reachable and the
collection was complete", not "unchanged partitions verified".

## Result fields

- `phase_verified` — **always false**. See Limitations.
- `recovery_boot_verified` / `boot_id_verified` — a new recovery boot was
  reached and its kernel boot ID differs from the pre-boot one. Transport only,
  and it stays truthful even when the collection or the hashes afterwards fail.
- `collection_verified` — every collection read succeeded (dmesg,
  `/tmp/recovery.log`, `/proc/cmdline`, the boot ID re-read, the pstore listing
  and every pstore file) and the boot ID did not change meanwhile. A rejected
  pstore name or an exhausted collection budget also makes it false.
- `transport_verified` — `collection_verified` **and** `partition_hashes_unchanged
  is true`. Without a pre baseline (`--collect-only`) this can never be true.
- `boot_id_freshness` — `pinned-pre-boot-id` (`--run`) or `unknown`.
- `boot_command_to_recovery_elapsed` — seconds from the `fastboot boot` command
  *finishing* to the confirmed new recovery boot ID. `null` when no new ID was
  confirmed, so a stale reconnect can never produce a number.
- `partition_hashes_unchanged` — post-read `boot`/`recovery` pair vs the single
  `pre` baseline: `true` only when both pairs were readable and identical,
  `false` when they differ, `null` when no comparison was possible.
- `boot_recovery_hashes_equal` — only the pre-boot comparison of the same run's
  `boot` vs `recovery` partitions. It is **not** the across-reboot comparison
  (that is `partition_hashes_unchanged`).
- `pre_partition_sha256` / `post_partition_sha256` — the only two hash records;
  there is no second "recovery slot" snapshot.

An incomplete collection is `result: failed` even if the hashes match, and a
changed post hash is `result: failed` even if every read succeeded. `--run`
reports `recovery_returned` only when both hold. A pair that cannot be read
after the reboot records an error and leaves equality `null`.

## Deadlines

Every command is bounded by `--command-timeout` **and** by whatever remains of
its wait deadline; the boot-ID read is no exception. An exhausted budget returns
a miss (not an error message, not a failure) and the wait ends as `timeout` on
its own deadline — it is never stretched by a slow device. A boot-ID read whose
own duration reaches its budget is untimely and cannot confirm recovery.
Collection then gets its own bounded `--wait-timeout` budget, because the wait
itself has already expired by then. A disconnect, failed boot, or timeout never
causes a boot retry or an automatic reset. A timed-out boot command is `failed`
(its actual device effect is unknown); an expired device wait is `timeout`.

`--inspect` requires the confirmed TWRP recovery before any other device command
and is `failed` without it. `--run` likewise refuses to reboot.

Recovery collection reads dmesg, `/tmp/recovery.log`, `/proc/cmdline`, the
boot ID (re-read; a change during collection is an error), boot/recovery hashes
and `/sys/fs/pstore`. Pstore files are pulled as raw bytes through
`adb exec-out`, with basename validation and regular-file/no-symlink checks;
remote files are not deleted. No PTY text conversion is used for pmsg artifacts.
Collection failures are recorded; `recovery_returned` denotes recovery
transport availability, not complete artifacts or successful firmware execution.

## Limitations and privacy

- **`phase_verified` is always false.** A changed boot ID, reboot completion,
  successful fastboot transport, recovery return, old pstore entries, and
  unchanged partition hashes do not prove a probe or any firmware phase ran.
  There is no explicit evidence adapter yet, and no proven firmware-persistence
  mechanism is assumed here.
- A new boot ID proves a new kernel boot of the device, not which image ran; the
  boot ID is read over adb inside recovery only.
- Evidence can be stale or missing; collection cannot establish its boot of
  origin. No panel/camera, UART, RAM dump, or live firmware telemetry is provided.
- A temporary boot image can itself damage data or write storage. This runner
  cannot sandbox firmware; use only independently reviewed, known-safe images.
  Host USB tooling and device bootloader behavior are external dependencies.
- The runner does not record environment variables, credentials or all system
  properties, and creates run directories/files with owner-only permissions.
  Full kernel/recovery/pstore artifacts **may themselves contain private data**;
  inspect and redact copies before sharing. Raw evidence is intentionally not
  rewritten or printed to the terminal. Store it outside version control.
- `--wait-timeout` applies separately to fastboot and recovery waits; readonly
  collection additionally takes at most one command timeout per attempted read.
  Output sizes are not capped because complete raw command evidence is retained.
  Avoid an untrusted/malicious device that could exhaust host memory or storage.