# UniXNU-gauguin

Device repository for the Xiaomi Redmi Note 9 Pro 5G / Mi 10T Lite (`gauguin`,
Qualcomm **SM7125 "lagoon"**, Adreno **610**), a device repository for
[UniXNU](https://github.com/UniXNU/UniXNU).

| | |
| --- | --- |
| SoC | SM7125 (lagoon) |
| GPU | Adreno 610 |
| Target iOS | 16.7.16 (20H392), iPhone X `iPhone10,3` / `d22ap` |
| Root | HFS+/APFS in the existing `userdata` partition |
| UEFI | Mu-Silicium, derived from the `miatoll` package (same SM7125 family) |

`miatoll` is Xiaomi's codename for the SM7125 family, so its Mu-Silicium
platform package is the starting point for this board; the Adreno 610 has a
worked example on `tb128fu` in the UniXNU device list.

## State

Early port. Adreno 610 renders Metal on `tb128fu`, so the GPU path this device
needs is known to work on the same IP - that is why the port targets UniXNU
rather than the earlier ios-sweet tree, which has no GPU path at all.

## Build

```sh
cd ../UniXNU
./unixnu defconfig gauguin_defconfig -D ../UniXNU-gauguin
./unixnu fetch
./unixnu build kernel
./unixnu build package
./unixnu build firmware --uefi ../Mu-Silicium
./unixnu bootimg --uefi ../Mu-Silicium --dtb /path/to/gauguin.dtb
```

No Apple files are in this repository.
