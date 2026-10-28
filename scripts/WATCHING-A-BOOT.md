# Watching a boot without a camera

The panel is gauguin's only live output, and on this board every channel that would
normally replace it is broken for the same reason the port needs watching: there is
no serial console, the USB stack does not enumerate while the boot is failing, and
the Mu-only build deliberately carries no UFS driver. A run that stops therefore
used to leave nothing behind at all.

Two things fix that. Neither needs a video. **Current state, what works and what does
not, and the known-good build are in [BRINGUP.md](../BRINGUP.md).**

## Which channel shows what, today

| Phase | What shows it on the panel |
| --- | --- |
| DXE dispatch | the `#N` strip and the console above it |
| UniXNU menu | the menu, the countdown, the `xnu` entry |
| DarwinBoot reads and parses | `DB13 parsed Success`, then a 15 s stall |
| XNU entry, up to the pre-MMU probe | blue band at the entry, magenta before `SCTLR_EL1`; post-MMU unverified |
| After the MMU | **nothing yet** - no verified signal exists |

The last row is the point of the current work. A reset after the magenta band is
not evidence of anything except that the phone reset, and the 15-second stall is
a diagnostic to be removed before any timing comparison (see BRINGUP.md).

## 1. Dispatch progress on the panel

Two pieces, both of which run with no protocol at all:

- `BootScreenDxe` (`Depex = TRUE`, links only `BaseLib`/`BaseMemoryLib`) clears the
  panel early and lays out a header plus a strip at the bottom.
- `BootScreenLib` is linked into `DxeMain` and called by the dispatcher itself, so
  every driver start is a straight store into the framebuffer ABL left scanning out.
  It allocates nothing, takes no lock, and cannot wait — which matters because the
  interesting case is a driver that never returns.

Neither goes through `EFI_GRAPHICS_OUTPUT_PROTOCOL`, ConOut, ConSplitter or the font
package, because each of those is published by a driver that may never run.

What it draws:

| Drawing | Means |
| --- | --- |
| `#12 8090 ..` cyan | driver 12 is starting |
| `#12 8090 OK` green | it returned `EFI_SUCCESS` |
| `#12 8090 F!` red | it returned an error |
| `#0 7A14 F!` | a driver is parked on a depex that will never arrive |

The a-priori order is fixed, so `#N` maps straight back to the Nth `INF` line in
`Platforms/Xiaomi/gauguinPkg/Include/APRIORI.inc`.

## 2. Reading a run

One photo. The console scrolls above the strip, so a photo carries the driver log
and the last event together. What to look for:

- `>> START <guid>` with no `<< DONE` after it names the driver that stopped.
- `ASSERT_EFI_ERROR (Status = Not Found)` next to it names the library too — this is
  how `UsbKbDxe` was caught calling `UefiHiiServicesLib` with no HII database in the
  volume.
- A stop with **no** exception and no output is often an unmapped read — a pattern
  to check, not a diagnosis.
- Past DarwinBoot, read the bands (blue, then magenta). A band appearing tells you
  the screen change was visible and the timing relationship the band has to
  `SCTLR_EL1`; it does not by itself show that anything after that instruction
  ran.
- **A missing band is weak evidence.** The band can be missed for reasons other
  than "the code did not run": the write may not have landed, the panel may not
  have refreshed or begun scanning that area, the photo may be at the wrong
  moment, a reset may have erased the frame. Treat its absence as a need to look
  again, not as proof of non-execution.

An empty panel is a meaningful result but not decisive. `BootScreenDxe`
painting nothing at all points either way: the volume may never have been
dispatched, or the panel may not have been scanned/refreshed, the boot may have
reset before the first paint, or the camera may have caught it too late. A black
screen on its own does not prove DXE never ran.

## 3. Automating the return leg

When nobody is holding the camera, `scripts/auto-debug.py` in this directory runs
the same observation as a host script. Default mode is a dry run with no device
writes and no flash; `--run` does one temporary `fastboot boot` of a hash-pinned
image and then collects the recovery side, and `--inspect` is read-only. The
script's fields describe transport and restoration (`recovery_boot_verified`,
`partition_hashes_unchanged`) and it keeps `phase_verified` false on purpose: a
new boot ID is a new kernel boot, not evidence of a firmware phase. See
`AUTO-DEBUG.md` for the flags and `../AUTOMATION-EVIDENCE.md` for the recorded
runs, including the negative pstore experiment.

Two read-only sources that are *not* firmware telemetry: `sda15` carries an old
Linux/TWRP kmsg ring, and pstore can be empty after a run in which the firmware
did execute. Neither an empty pstore nor a Linux line in that ring says anything
about XNU or about whether DRAM survived the reset.

## Not used on this board

`PlogDxe` and `read-logdump.py` are historical attempts to persist firmware
logs. The payload publishes a **read-only memory BlockIo** device, so absence
of all BlockIo is not the explanation. No writable logdump backend has been
validated for the current run; an empty dump does not identify the failed
stage, and these tools are not used as the automatic phase oracle.

`Bisect-apriori.py` is likewise kept and likewise misleading: the driver batch turned
out not to be the problem, and cutting it also removes the drivers that carry the
output. See BRINGUP.md.