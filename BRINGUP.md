# Bring-up state

The last section is the one to read first. Everything above it is how the port got
there, kept because most of it is a list of things that look reasonable and are not.

## Where the port stands

| Stage | State | Evidence |
| --- | --- | --- |
| 1 UEFI firmware | **Works** | Volume builds, SEC/PrePi/DXE run, panel shows the boot |
| 2 UniXNU menu | **Works** | Three entries and the countdown, `BootScreenDxe` strip at the bottom |
| 3 Payload volume | **Works** | `ShengBootMemoryDxe` publishes the ramdisk, Fat mounts it, UniXNU.efi loads |
| 4 DarwinBoot | Not reached | Next |
| 5+ Kernel, root, userland | Not reached | Needs stage 4 |

The menu is now UniXNU's own, and `UniXNU.efi` is loaded and relocated out of the
payload volume. Getting there needed four changes, and no one of them is enough on
its own:

1. **The shim reports where it was loaded.** `x0` from ABL was `0xA59FE000`, which is
   not a device tree and not even a mapped address. `BootShim.S` now puts its own
   load address in `x0` instead — the boot image's start — and nothing downstream
   relies on ABL's pointer.
2. **The map covers the boot image.** `0xA2408000` (where the boot image lands) and
   `0xA59FE000` (ABL's tree) are both inside `0xA2400000-0xAC000000`, which the map
   left out, so the MMU never mapped either. The map now describes the first 15 MiB
   of that gap. All 137 MiB of it made the phone reset within seconds.
3. **The ramdisk comes from the header.** `ShengBootMemoryDxe` reads the Android
   header at the shim's address and takes kernel size, ramdisk size and page size
   from it. No `/chosen`, no searching.
4. **The publisher runs before Fat.** `ShengBootMemoryDxe` has a TRUE depex so it ran
   after the a-priori batch, and Fat — no depex either — ran inside it, found nothing
   to mount, and returned. The ramdisk then appeared with no filesystem driver left
   to attach to it. It is now listed ahead of `PartitionDxe` and Fat.

The menu used to read "no boot entry found" throughout, because UniXNU enumerates
`EFI_SIMPLE_FILE_SYSTEM_PROTOCOL` handles and there were none. That line now also
carries block-device and filesystem counts, which is how the states above were told
apart from the panel.

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

If a change makes the menu disappear, revert to those five things before debugging
anything else.

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
RAM that only a block device could ever reach, and this board has none by design. A
diagnostic added there is invisible. Use `DEBUG` (which drives the panel console) or
`BootScreenEvent` (which drives the strip).

## How to read a failed boot

One photo, not a video. `BootScreenDxe` keeps the eight-row strip at the bottom of
the panel and the console scrolls above it, so a photo carries both.

- Bottom strip `#N XXXX` is the last dispatcher event; `#0 <guid>` means a driver is
  parked on a depex that will never be satisfied.
- `>> START 2D2E62CF-...` followed by `ASSERT_EFI_ERROR (Status = Not Found)` names
  the driver and the library that failed, without needing a backtrace.
- A stop with no exception at all is almost always an unmapped read, not a hang.

`Bisect-apriori.py` and `dump-logbuffer.py` are still in this directory. Neither is
useful on this board as it stands: the batch was not the problem, and there is no
block device to write the log to.

## Next step

Stage 4: DarwinBoot. `UniXNU.efi` loads and relocates, so the payload volume, its
configuration and the loaders above it are all working; what has not been seen yet is
an entry being selected and XNU being started.

Two things are worth knowing before that:

**Do not grow the device-tree search.** It is settled as unreachable from UEFI on
this board, for the reason in item 2 above, and the port no longer depends on it. The
ADT handed to XNU is the one `scripts/make-gauguin-adt.py` generates.

**The device tree in the boot image is real and complete.** It sits at file offset
`0x490B000`, `d00dfeed`, 588841 bytes, version 17, with `adsp_mem`, `disp_rdump_memory`
and the rest of the nodes. `ABL` reads it from there, which is why TWRP has one at
`/sys/firmware/fdt`. The problem was never that it was missing — only that the address
ABL copies it to is outside what this firmware maps.