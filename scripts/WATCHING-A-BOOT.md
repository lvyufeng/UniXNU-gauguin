# Watching a boot without a camera

The panel is gauguin's only live output, and on this board every channel that would
normally replace it is broken for the same reason the port needs watching: there is
no serial console, the USB stack does not enumerate while the boot is failing, and
the Mu-only build deliberately carries no UFS driver. A run that stops therefore
used to leave nothing behind at all.

Two things fix that. Neither needs a video. **Current state, what works and what does
not, and the known-good build are in [../BRINGUP.md](../BRINGUP.md).**

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
- A stop with **no** exception and no output is almost always an unmapped read.

An empty panel is a result worth trusting: `BootScreenDxe` painting nothing at all
means the volume was never dispatched, and no amount of DXE debugging will help.

## Not used on this board

`PlogDxe` copies the firmware's log rings into the `logdump` partition, and
`scripts/read-logdump.py` decodes it. Both are kept, and both are inert here: the
driver's DEPEX is `gEfiBlockIoProtocolGuid`, a Mu-only platform publishes no block
I/O by design, and so it never starts. `logdump` reads back as zero on every boot,
which is the correct answer rather than a fault.

`Bisect-apriori.py` is likewise kept and likewise misleading: the driver batch turned
out not to be the problem, and cutting it also removes the drivers that carry the
output. See BRINGUP.md.