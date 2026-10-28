# Bring-up state

The first section is the one to read first: it is the current, verified state.
Everything below it is how the port got there, kept because most of it is a list
of things that look reasonable and are not. Where an old paragraph records a
guess that later measurement overturned, it is labelled **[historical, not
confirmed]** and is kept only so the next person does not repeat it.

## Where the port stands

Verified on the device unless the row says otherwise.

| Stage | State | Evidence |
| --- | --- | --- |
| 1 UEFI firmware | **Works** | Volume builds, SEC/PrePi/DXE run, panel shows the boot |
| 2 UniXNU menu | **Works** | Three entries and the countdown, `BootScreenDxe` strip at the bottom |
| 3 Payload volume | **Works** | `ShengBootMemoryDxe` publishes the ramdisk, Fat mounts it, `UniXNU.efi` loads |
| 4 DarwinBoot read + parse | **Works** | Kernel and ADT read by their correct names, `DB13 parsed Success` seen on the panel |
| 5 Entry and the pre-MMU probe | **Works** | Blue band at the kernel entry, magenta immediately before `SCTLR_EL1`; post-MMU unverified |
| 6 After the MMU | **Not verified** | No evidence yet of the MMU enabled, of the USB controller, or of userland |
| 7 Root, userland | Not reached | Needs stage 6 |

### What "works" means here, exactly

- **The payload in use is frozen.** `payload.img` is the 72 MiB baseline,
  SHA256 `55c429b920b25057ffe9df615c042fa6886e326b5881d58cc5cf64173e850907`.
  Inside it the kernel is 57,110,528 bytes,
  `6cc5894a917d0a93c117ace3338eda493b1a93cd85132cabda304eb4e00259d3`, and the ADT
  12,468 bytes, `65d42111e9f45588a6ac244ed66a28da35715072cfff699a07a10567f8768726`.
  `payload.json` and the standalone `kernel.macho` in the build tree are
  **stale** - they do not describe this payload - and must not be cited as its
  contents. See "Build" in [README.md](README.md): payload packaging exists but
  must be run explicitly; `build kernel` alone does not refresh `payload.img`.
- **The FAT loader is correct on the real payload.** The old long-name
  algorithm folded `kernel.macho.adt` and `kernel.macho` onto the same name
  `kernel.macho`, so a request for the kernel returned the ADT. It now assembles names
  from UTF-16 units in slots 1..10, 14..25 and 28..31, leaves byte 26 as
  reserved, and handles multi-segment ordinal/checksum pairs and the `0x00`
  terminator. The host regression compiles the real C, not a Python imitation.
  Its first validation passed 21 tests, kept as **historical**; the published
  code was then independently re-run on the real C: **26 tests (18 FAT + 8
  DarwinBoot), 0 skipped, 0 failed**. Details in
  [FAT-RECOVERY-VALIDATION.md](FAT-RECOVERY-VALIDATION.md) and
  [fat-recovery-verification.json](fat-recovery-verification.json).
- **DarwinBoot parses.** `DB13 parsed Success` is a checkpoint after the kernel
  read and `ParseKernel`, so it proves the file was read and its Mach-O header
  and load commands were accepted. It does not prove any segment was mapped at a
  usable address, and it does not prove XNU ran.
- **The boot reached the pre-MMU probe; post-MMU is unverified.** The
  kernel-entry patch draws a blue band, and the `SCTLR_EL1` write site draws
  magenta, so execution reached that instruction while the MMU was still off.
  What happens after it is not known: nothing yet shows the MMU on, an exception
  vector in use, the USB controller alive, or any userland - and nothing seen so
  far shows the kernel stopped there either. A reboot after the magenta band is
  consistent with either outcome, so do not read it as progress or as a stop.
- **Host regression, not device proof.** Independently re-run on the published
  code: the firmware C suite is **26 tests (18 FAT + 8 DarwinBoot), 0 skipped,
  0 failed**, and the committed host tooling (**129** = 53 auto-debug + 76
  kmsg-ring) passes. The 12 post-MMU-probe and 35 install-probe tests are not
  counted: those prototypes are experimental and were not committed. The first
  FAT validation's 21 stays recorded as historical. All of this is host fact,
  and is only as good as what it exercises.

Unverified claims to avoid: "iOS boots", "XNU completes initialization", or
"the MMU is enabled". Kernel entry and FAT read/parse are evidenced; later
stages are not.

### The host runner

`scripts/auto-debug.py` is the one-shot evidence collector. It defaults to
**dry run: no writes, no flash**. With `--run` it does exactly one temporary
`fastboot boot` of a hash-pinned image, never a persistent flash, and it refuses
to start unless the TWRP recovery, the boot-ID and the boot/recovery partition
hashes all check out.

Latest hardened run, on boot image
`249b581b1732411ad7564e6d0cf78d567f2b18b4463bfc02a24b7334c34d7994` — a
**historical** image, built while the guessed ramoops writes were disabled by
an assembly `.if 0`. That block was later removed from the loader source, so the
image hash does not describe the source tree as it stands:

| Field | Value |
| --- | --- |
| Run directory | `/tmp/gauguin-verified-auto-debug/20261028T143609.024127Z-0a9deb08c902` |
| Pre-boot ID | `8aec1d8e-76ec-4ad8-83a9-7976bdb8e605` |
| Recovery boot ID | `3ca04640-d01a-404b-8981-b399cad8755b` |
| `recovery_boot_verified` / `collection_verified` / `transport_verified` | true / true / true |
| `partition_hashes_unchanged` | true |
| `phase_verified` | **false** |
| `errors` | none |
| `boot_command_to_recovery_elapsed` | 85.3945 s |

Those fields are transport and restoration facts. The elapsed time is one
observation of how long the boot and return took; it is not a phase signal,
and there is deliberately no "it reboots in N seconds so it got further" claim
anywhere in this repository. See [AUTOMATION-EVIDENCE.md](AUTOMATION-EVIDENCE.md)
for the surrounding experiments, including their negative results.

### Device state and its traps

- **Boot and recovery both hold the TWRP image**, 128 MiB each, SHA256
  `8f488a0a2a766f0a1076a21be0a9f0187b2ee8e598bebd64391236b6d155fb1b`. The
  current firmware experiments use **temporary `fastboot boot` only**. An
  earlier experiment had overwritten boot; it was restored from the verified
  backup, and subsequent full-partition hashes confirm the TWRP fallback.
- **DarwinBoot carries a 15-second diagnostic stall** after a successful parse,
  so the success line is visible on the panel. It has no compile-time switch
  yet. Any performance or timing comparison is meaningless until it is removed;
  it is for photographs, not for measurements.
- **`sda15` holds an old Linux/TWRP kmsg ring.** It is readable, and its
  contents are Linux 4.19 logs, **not XNU logs**. An empty pstore is not proof
  that DRAM was not preserved either: the standard pmsg round-trip gave a
  negative result, which does not settle what a reset path does with memory.
- **A standard pmsg test is not the only option.** A raw ring read is
  acceptable evidence of what that ring holds; what is not acceptable is
  reading a raw ring and calling its Linux lines XNU output, or using the
  absence of pstore data as proof of absence of execution.
- **The new-install probe is not accepted yet.** It has not been verified on
  the device; do not report it as done.

## The known-good build

Keep this in mind before changing anything: the port reached the menu at exactly one
point, and several changes made after it made things worse.

What that build is:

- panel height **2248** (`PcdFrameBufferHeight`), not the panel's native 2400
- `BootShim.S` passes its own load address in `x0`
- the map describes `0xA2400000-0xA3300000` (15 MiB) and no more of the gap
- `ShengBootMemoryDxe` is listed in `APRIORI.inc` ahead of `PartitionDxe` and Fat
- `HiiDatabaseDxe` is in the a-priori batch
- `BootScreenDxe` + `BootScreenLib` draw dispatch progress on the panel

If a change makes the menu disappear, revert to those six things before debugging
anything else.

The current firmware adds two things on top of that build: the kernel-entry
probes (blue/magenta) and the 15-second post-parse stall described above.

## Facts measured on the device

Taken from TWRP through adb, not inferred. These override anything in the source
tree that disagrees.

**Physical memory** (`/proc/iomem`, System RAM entries):

    80880000-808fefff     82900000-85ffffff     8b718000-8b7fffff
    9b000000-abffffff    ad000000-bbafffff      c7b00000-27fffffff

The device tree's `/memory` says `0x80000000` for `0x3bb00000`, but the first page
DXE would read at `0x80000000` is **not mapped**. `/memory` describes physical DRAM;
`/proc/iomem` describes what a kernel actually took over. They are different
questions and this port needs the second one.

**Panel**: `msm_drm.dsi_display0=qcom,mdss_dsi_j17_36_02_0a_dsc_video`, and that
node says 1080x2400 at 120 Hz, bpp 24. The tree also carries `rm69299` nodes; they
are not the active panel. The command line is what identifies it.

**Device tree**: TWRP has it at `/sys/firmware/fdt`, 588757 bytes, with a populated
`/chosen` (`linux,initrd-start = 0xa7c4c000`). So ABL does build and pass a tree — it
just does not hand *our* firmware a usable address.

## Things that look sensible and do not work

Each of these was tried, failed, and is worth not trying again.

**Walking memory for the device tree.** Two versions: a fixed window
`0x80000000..0xA0000000`, then every GCD range marked system memory. Both stopped the
boot dead with no output at all. An unmapped read is not an exception the firmware
can catch, so the search cannot even report that it failed — the screen just stops.
The GCD saying "system memory" does not promise that a read will return.

**[historical, not confirmed]** The paragraph above used to conclude that reading
memory on this board is categorically unsafe. That was a guess built on two
failed windows and is not established: some reads are clearly fine (the payload,
the panel and the firmware are all read), and the failures say something about
*those* windows, not about every address range. When probing new addresses,
treat an unmapped read as a hazard to be tested for, not as a proven property of
the whole board.

**Trusting the device tree over a measurement.** The panel node says 2400, so 2400
was set. The Mu logo appeared and the panel went dark: the console laid text past the
last row ABL is actually scanning out. Native panel height and visible extent are
different numbers. 2248 works; 2400 does not.

**Auditing a driver batch by deleting drivers.** An earlier round bisected the
a-priori list from 69 down to 36 and the failure did not move. It was not a driver
ordering problem: the real faults were a missing `HiiDatabaseDxe` (which made
`UsbKbDxe`, a driver with no depex of its own, assert on `EFI_NOT_FOUND`) and the x0
guard above. Bisecting a batch also removes the very drivers that carry the output —
`UniXNUDxe` and `SimpleFbDxe` were among the 33 that got dropped.

**Writing progress to the Log Buffer.** `LogBufferWriteString` writes to 32 KiB of
RAM whose only reachable backend in this design is a block device, and the Mu-only
build ships no writable one. A diagnostic added there is invisible. Use `DEBUG`
(which drives the panel console) or `BootScreenEvent` (which drives the strip).
**[historical]** The earlier phrasing here — "this board has none by design" —
overstated it: what the payload publishes is a **read-only memory BlockIo**, so
the absence of a writable block device, not of BlockIo itself, is the real
limitation. That is a known limitation, not a proven fault of the board.

**Writing a ramoops record at a guessed address.** DarwinBoot used to write
assumed pstore records to several offsets in the ramoops window (`0xb0000000`
onwards). No experiment ever showed such a record surviving a reset, and the
region is Linux's, not XNU's. The unvalidated write block was first disabled
and is removed from the finalized loader source; it is not a log protocol.

## How to read a failed boot

One photo, not a video. `BootScreenDxe` keeps the eight-row strip at the bottom of
the panel and the console scrolls above it, so a photo carries both.
`scripts/WATCHING-A-BOOT.md` covers the mechanical side; the reading rules are:

- Bottom strip `#N XXXX` is the last dispatcher event; `#0 <guid>` means a driver is
  parked on a depex that will never be satisfied.
- `>> START 2D2E62CF-...` followed by `ASSERT_EFI_ERROR (Status = Not Found)` names
  the driver and the library that failed, without needing a backtrace.
- A stop with no exception at all is most often an unmapped read, but "most
  often" is a pattern to check against, not a diagnosis.
- After DarwinBoot, blue confirms the kernel-entry probe wrote the panel;
  magenta confirms the probe immediately before the SCTLR write did so. A
  missing band is not proof of non-execution. Blue plus magenta does not prove
  the subsequent MMU enable or C initialization succeeded.

`Bisect-apriori.py` and `dump-logbuffer.py` are still in this directory. Neither is
useful on this board as it stands: the batch was not the problem, and there is no
writable block device for the log to land on. **[historical]** Only the read-only
BlockIo the payload publishes exists; "no block device at all" was an earlier,
unconfirmed way of saying it.

## Next step

Stage 6: prove something **after** the MMU comes up. The entry and pre-MMU
side is evidenced as far as a static kernel patch can take it, so the next
experiment needs a signal that can only be produced with the MMU on and at
least one mapping in place - and it needs to be recoverable, because a fault
there currently just resets the phone.

Before that experiment:

- remove the 15-second post-parse stall (there is no compile-time switch for it
  yet), so any later comparison of behaviour is about the firmware and not
  about the stall;
- keep `phase_verified` honest - a changed boot ID is a new kernel boot, not a
  phase;
- keep `fastboot boot` temporary. Boot stays TWRP. `boot_backup.img` is 128 MiB
  and matches both partitions.

`scripts/POST-MMU-PROBE.md` is published as a **design note only**, not as an
accepted tool: the generator it describes and its tests are not part of this
stage. Nothing it emits has been run on hardware, and it deliberately omits the
PSCI reset path - read it as a design sketch, not as a validated instrument.

## Old assumptions kept for reference

**Do not grow the device-tree search.** It is still the case that the boot image's
own tree is out of reach from UEFI at the address ABL uses, and the port does
not depend on it. The ADT handed to XNU is the one inside the payload
(12,468 bytes, the hash above).

**The device tree in the boot image is real and complete.** It sits at file offset
`0x490B000`, `d00dfeed`, 588841 bytes, version 17, with `adsp_mem`, `disp_rdump_memory`
and the rest of the nodes. `ABL` reads it from there, which is why TWRP has one at
`/sys/firmware/fdt`. The problem was never that it was missing — only that the address
ABL copies it to is outside what this firmware maps.