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

On hardware the Mu-only firmware boots, dispatches the whole DXE batch, and
`ShengBootMemoryDxe` now finds the payload: the ramdisk rides inside the kernel
image, the shim hands its address over, and the read-only FAT32 driver mounts it.
UniXNU draws its menu from `unixnu.toml`, times out, boots the `xnu` entry, and
DarwinBoot reads the 54 MiB kernel end to end.

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
file on every call, which made reading a 54 MiB kernel in 8 MiB pieces quadratic,
and it stored its cursor without the position that cursor sits at, which
overshot the chain and returned `EFI_VOLUME_CORRUPTED` at 40 MiB.

What is left is the jump: DarwinBoot parses the Mach-O, maps the segments and
hands control to XNU. See [BRINGUP.md](BRINGUP.md) for the evidence, the
known-good build, and a list of approaches that look reasonable and are not.

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

No Apple files are in this repository.
