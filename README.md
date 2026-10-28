# UniXNU-gauguin

Device repository for the Xiaomi Redmi Note 9 Pro 5G / Mi 10T Lite / Mi 10i
(`gauguin`, Qualcomm **SM7225** Snapdragon 750G, Adreno **619**), a device
repository for [UniXNU](https://github.com/UniXNU/UniXNU).

| | |
| --- | --- |
| SoC | SM7225 (Snapdragon 750G) |
| GPU | Adreno 619 |
| Target iOS | 16.7.16 (20H392), iPhone X `iPhone10,3` / `d22ap` |
| Root | HFS+/APFS in the existing `userdata` partition |
| UEFI | Mu-Silicium, derived from the `miatoll` package |

SM7225 and SM7125 are different chips and an easy pair to confuse. The Redmi
Note 9 Pro **without** 5G (Xiaomi's `curtana` / `excalibur`) is the SM7125
Snapdragon 720G with an Adreno 618; `gauguin` is the SM7225 750G with an Adreno
619, and the two share Little Kernel and platform naming closely enough that
miatoll's SM7125 platform package is still the starting point for the firmware
here. Check any inherited constant against this phone before trusting it.

The Adreno 619 has no worked example in the UniXNU device list. The nearest is
the Adreno 610 on `tb128fu`, which is the same family of driver but a different
IP, so the GPU path will need checking against this chip rather than assumed.

## State

Early port, with the firmware running on the device. The port targets UniXNU
rather than the earlier ios-sweet tree because ios-sweet has no GPU path at all,
and UniXNU does have one for this family of Adreno IP - the 610 on `tb128fu`,
which is not this chip's 619 but is the closest thing to a reference.

**Where it stands (verified):** the payload volume mounts, the `xnu` entry is
selected, DarwinBoot reads and parses the 57,110,528-byte kernel with the
correct 12,468-byte ADT, and the panel shows `DB13 parsed Success`. On a
temporary boot the panel then shows a blue band at the kernel entry and magenta
immediately before `SCTLR_EL1`, so XNU code at least executes up to - not past -
the MMU enable. **Nothing after the MMU is verified**: no stage proves the MMU
came up, the USB controller or any userland ran. That is the next thing to
prove, and [BRINGUP.md](BRINGUP.md) is the document that tracks it.

What is evidenced and what is not, with the machine-readable run records, is in
[AUTOMATION-EVIDENCE.md](AUTOMATION-EVIDENCE.md) and
[auto-debug-verification.json](auto-debug-verification.json) (host automation)
and [fat-recovery-verification.json](fat-recovery-verification.json) (payload
and restoration). Read a green host test as a host test: the host FAT suite
passes on the real C code, and the offline suite here passes, but neither is
execution on the phone.

Four faults sat between "the menu appears" and "the kernel is in memory", and
all four looked like the same silent stop:

- `Debug()` at `PcdUniXNUDebugLevel` (0x40) never returned inside UniXNU, so
  every log line the port expected was dropped. `DEBUG_ERROR` prints normally.
  Until this was found, every diagnosis was a photograph of a stale frame.
- `UniXNUConnectAll()` does not come back on this board; it is called when the
  boot option names the firmware, and the payload is reached without it.
- `AllocatePool` above roughly 64 KiB hangs, so the drawing code takes large
  buffers from the page allocator. The logo needs 781 KiB.
- `AllocatePages` returned `0x35C989000` - fourteen gigabytes, past the end of
  every described range - and the first write to it hung the boot. Allocations
  are capped at the top of this phone's RAM now.

The FAT reader had a fifth: it walked the cluster chain from the head of the
file on every call, which made reading the kernel in pieces quadratic, and it
stored its cursor without the position that cursor sits at, which overshot the
chain and returned `EFI_VOLUME_CORRUPTED` at 40 MiB.

A sixth was in long-name handling, and it was silent rather than noisy: the old
algorithm decoded both `kernel.macho.adt` and `kernel.macho` to the name
`kernel.macho`, so a request for the kernel opened `kernel.macho.adt` instead.
Name assembly now takes UTF-16 code
units from slots 1..10, 14..25 and 28..31 of each 32-byte record, treats byte 26
as reserved (not as a character), and handles multi-segment names, the ordinal
and checksum that span segments, and the `0x00` terminator. The host
regression compiles the actual C and covers the real payload's cluster chains.
Its first validation passed 21 tests (historical); the published code was
independently re-run at **26 tests (18 FAT + 8 DarwinBoot), 0 skipped, 0
failed**. See [FAT-RECOVERY-VALIDATION.md](FAT-RECOVERY-VALIDATION.md).

The kernel-entry handoff is now evidenced. What remains is proof of progress
through and after MMU enable. See [BRINGUP.md](BRINGUP.md) for the evidence,
the known-good build, and the unverified experiments.

For this bring-up branch use [lvyufeng/UniXNU](https://github.com/lvyufeng/UniXNU/tree/gauguin/boot-to-kernel-read)
and [lvyufeng/uefi-gauguin](https://github.com/lvyufeng/uefi-gauguin).
The DarwinBoot gitlink in UniXNU points at [lvyufeng/DarwinBoot](https://github.com/lvyufeng/DarwinBoot);
these are fork-local diagnostics and fixes, not claimed upstream support.

## Build

```sh
cd ../UniXNU
./unixnu defconfig gauguin_defconfig -D ../UniXNU-gauguin
./unixnu fetch
./unixnu build kernel
./unixnu build package
./unixnu build firmware --uefi ../uefi-gauguin
./unixnu bootimg --uefi ../uefi-gauguin --dtb /path/to/gauguin.dtb
```

**This particular command list omits payload packaging.** UniXNU does provide
`./unixnu build payload`, but `build kernel` does not automatically run it:
it updates standalone kernel/ADT artifacts, not the FAT image. `bootimg` uses
an existing `out/gauguin/payload/payload.img` by default; a fresh build without
that file must explicitly package a payload before testing XNU.

For the current regression, keep the tested payload frozen at
`55c429b920b25057ffe9df615c042fa6886e326b5881d58cc5cf64173e850907` (72 MiB,
kernel `6cc5894a...59d3`, ADT `65d42111...726`). A new kernel needs an explicit
payload build, content/hash verification, and a new boot image; do not mix its
standalone manifest with this restored baseline.

No Apple files are in this repository.
