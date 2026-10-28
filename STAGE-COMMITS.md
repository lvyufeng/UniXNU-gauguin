# Stage commits

What this bring-up stage claims, what it deliberately leaves out, and where each
claim lives. The published commit SHAs are in the commit table at the end; this
file's own location is found through `git log`, not through a hash written
inside it.

## Scope accepted for this stage

| Area | Accepted | Evidence record |
| --- | --- | --- |
| FAT long names (uefi-gauguin) | Old algorithm folded `kernel.macho.adt` and `kernel.macho` onto `kernel.macho`; name assembly now uses UTF-16 units at slots 1..10 / 14..25 / 28..31, byte 26 reserved, multi-segment ordinal/checksum and `0x00` terminator handled | [FAT-RECOVERY-VALIDATION.md](FAT-RECOVERY-VALIDATION.md), [fat-recovery-verification.json](fat-recovery-verification.json) |
| Loader / DarwinBoot read + parse | Kernel and ADT read by their correct names; `DB13 parsed Success` observed on the panel | [FAT-RECOVERY-VALIDATION.md](FAT-RECOVERY-VALIDATION.md), [BRINGUP.md](BRINGUP.md) |
| Kernel entry evidence | Blue band at the entry, magenta immediately before `SCTLR_EL1`: the boot **reached the pre-MMU probe**; **post-MMU remains unverified** | [BRINGUP.md](BRINGUP.md), [AUTOMATION-EVIDENCE.md](AUTOMATION-EVIDENCE.md) |
| Host automation | `scripts/auto-debug.py`, default dry run with no writes or flash; one hardened `--run` with a fresh boot ID and unchanged partition hashes, `phase_verified` false | [auto-debug-verification.json](auto-debug-verification.json), [AUTOMATION-EVIDENCE.md](AUTOMATION-EVIDENCE.md) |
| Restoration | Boot was damaged by an earlier mistaken write and has since been **restored and verified** (boot and recovery both match the 128 MiB TWRP backup); every current experiment is a **temporary `fastboot boot` only** | [FAT-RECOVERY-VALIDATION.md](FAT-RECOVERY-VALIDATION.md) |

Test counts are recorded at the point of validation and become historical once
tests are added; the authoritative count for a commit is whatever its record
says, not a number copied into a document later. 21 was the first FAT
validation and stays recorded as historical. The current, independently
re-run counts are **26** for the firmware C (18 FAT + 8 DarwinBoot) and **129**
for the committed host tooling (53 auto-debug + 76 kmsg-ring); see the commit
table.

## Explicitly excluded from this stage

These exist in branches or working trees but are **not** part of what this stage
claims, and none of them is verified on hardware:

| Excluded prototype | Why excluded |
| --- | --- |
| DWC3 / USB experiment driver | Not validated; USB enumeration is not a verified signal |
| SMMU bypass prototype | Writes shared SMMU control registers; no validated recovery |
| Install probe — `scripts/install-boot-probe.py`, its tests and the new-install path | Offline only; never accepted on the device |
| Post-MMU probe generator — `scripts/make-post-mmu-probe.py`, `tests/test_post_mmu_probe.py` | Offline design only, never run; omits the reset path |
| Guessed-ramoops write block | Removed from the loader source; it was never a validated logging protocol |
| `scripts/POST-MMU-PROBE.md` | Published in this stage as a **design note only, not an accepted tool**; it ships without `make-post-mmu-probe.py` and without its tests |

## What is not published

Only summaries and hashes go into this repository. The raw material stays on
the host and is **not** committed:

- raw TWRP `dmesg`, `/tmp/recovery.log`, `/proc/cmdline` and the pstore
  directory listing or files;
- the raw `sda15` kmsg-ring read;
- boot images (`boot.img` variants) and the 128 MiB TWRP backup image;
- the auto-debug run directory and its binary stdout/stderr captures
  (`/tmp/gauguin-verified-auto-debug/...` is a local reference, marked
  `not uploaded` in [auto-debug-verification.json](auto-debug-verification.json)).

Evidence that leaves the host is limited to the hashes, sizes and one-line
outcomes recorded in the JSON files here. The device serial is the script's
documented default scope and is deliberately not treated as a secret beyond
that; no other host path is recorded, and
[fat-recovery-verification.json](fat-recovery-verification.json) now uses paths
relative to this repository.

## Remote

This stage has exactly four repositories, all personal, none of them upstream:

1. [lvyufeng/UniXNU](https://github.com/lvyufeng/UniXNU)
2. [lvyufeng/uefi-gauguin](https://github.com/lvyufeng/uefi-gauguin)
3. [lvyufeng/DarwinBoot](https://github.com/lvyufeng/DarwinBoot), reached through
   the DarwinBoot gitlink in [lvyufeng/UniXNU](https://github.com/lvyufeng/UniXNU)
4. [lvyufeng/UniXNU-gauguin](https://github.com/lvyufeng/UniXNU-gauguin), this
   device repository

None of these is UniXNU upstream; nothing here is claimed as upstream support.

## Commit table

Published SHAs, as handed over by the release step. Each was pushed with the
remote exactly equal to the local HEAD.

| Object | Commit | Branch / location |
| --- | --- | --- |
| FAT long-name fix and loader changes (uefi-gauguin) | `41b84ac0c392f8c8a5330a0c58c4810efd76afc8` | `main` of [lvyufeng/uefi-gauguin](https://github.com/lvyufeng/uefi-gauguin) |
| DarwinBoot loader changes | `f87a859fbe4fce140926cd62d8ff6a31ecdaa7bb` | `main` of [lvyufeng/DarwinBoot](https://github.com/lvyufeng/DarwinBoot); the DarwinBoot gitlink that points at it lives in [lvyufeng/UniXNU](https://github.com/lvyufeng/UniXNU) (this device repository has no DarwinBoot gitlink) |
| UniXNU changes | `08d597b7943db70068ecc522fde67f85e6c7139c` | `gauguin/boot-to-kernel-read` of [lvyufeng/UniXNU](https://github.com/lvyufeng/UniXNU) |
| Device tooling in this repository (`auto-debug.py`, `read-kmsg-ring.py` and their tests) | `698e5b4b229d235659d9a0640893691b944e27cc` | `main` of [lvyufeng/UniXNU-gauguin](https://github.com/lvyufeng/UniXNU-gauguin) |
| This documentation set | see below | |

Acceptance counts for the published code, run independently against the real C:
**18 FAT + 8 DarwinBoot = 26 tests, 0 skipped, 0 failed.** For the device
tooling, only the tooling that was committed counts: `auto-debug.py` 53 +
`read-kmsg-ring.py` 76 = **129 tests**. The 12 post-MMU-probe and 35
install-probe tests are **not** counted — those prototypes are experimental and
were not committed.

The documentation's own commit is deliberately not written into the file it
would describe. Read it from history instead:

```sh
git log -- STAGE-COMMITS.md
```
