# Standalone post-MMU delay probe (offline design only)

**This is a design memo.** It records a design for a post-MMU probe; it makes
no promise that `make-post-mmu-probe.py`, its tests or any probe described here
is committed in this stage. Nothing below has been run on hardware.

`scripts/make-post-mmu-probe.py` reads a supplied **already-patched** kernel and
emits a new assembly file or prints assembly to stdout. It does **not** patch
kernel, payload, boot image or device, and does not assemble, link, install or
boot. No hardware verification has been performed.

## Usage

```sh
python3 scripts/make-post-mmu-probe.py /path/to/already-patched-kernel \
  --code-va 0xYOUR_PLANNED_CODE_VA --target-va 0xYOUR_VERIFIED_CPACR_VA \
  --magenta-va 0xYOUR_KNOWN_STAGE2_HOOK_VA \
  --delay-seconds 1 --max-iterations 1000000 \
  --output /path/to/new-post-mmu-probe.S
```

Replace placeholders with real aligned unsigned 64-bit addresses. Code/target
arguments are mandatory; target must be verified MOV + 4 (CPACR). Magenta VA is
needed only for a replaced SCTLR instruction and asserts a **known** existing
stage-2 destination, not an inferred offset. Hook body/MMU behavior is **not**
verified. Native SCTLR is acceptable. Output is exclusively created; existing
files, symlinks and hardlinks fail. Do not redirect stdout over input/baseline.

## Static validation

- Little-endian 64-bit arm64 Mach-O, bounded load commands, uniquely file-backed
  executable LC_UNIXTHREAD PC (ARM_THREAD_STATE64 flavor 6/count 68).
- Actual `_start_first_cpu` via llvm-nm when available. Found symbols must be
  aligned/executable in `[PC, PC + 0x4000)`. Failed llvm-nm is an error;
  absent tool/symbol allows only the bounded unique quartet fallback.
- Exactly one quartet within that window (at/after symbol when present): SCTLR
  `0xd5181000` or exact B to known magenta VA; ISB `0xd5033fdf` at +4; MOV
  `0xd2a00600` at +0x20; CPACR `0xd5181040` at +0x24. All words are uniquely
  executable/file-backed. Ambiguous/missing quartets fail.
- Explicit aligned code/target VAs; target MOV+4; signed imm26 branch reach in
  both directions (hypothetical install B and actual continuation B); no
  quartet overlap or address wrap. Delay 1–10s; cap 1–1,000,000,000 iterations.

Fallback identifies only a quartet, not a function from constants or blindly
following the patched entry B. No runtime reachability is proven. Free cave
space, mappings, backing, kernel slide, install B and cache maintenance remain
unverified. A future installer must independently establish them.

## Assembly

100 bytes, excluding alignment before entry; must be placed at exact code VA.
Final B is a fixed `.inst`, not position-independent. No linker movement or
injected instructions is allowed. No literal pool, data, MMIO or calls. This
**does not fit** an older 0x60-byte cave and reserves no space in the kernel.

Would displace only MOV, not SCTLR/ISB. Saves x0–x3 and NZCV in a 48-byte,
16-byte-aligned stack frame. Reads CNTFRQ_EL0, skips zero frequency, multiplies
architected 32-bit frequency by 1–10 seconds without 64-bit overflow. CNTPCT_EL0
reads after ISB use modular elapsed ticks. Loop exits at tick threshold **or**
iteration cap, even with stagnant counter. Restores NZCV, x0–x3 and SP; replays
`mov x0, #0x300000`; B to target. x1–x3 preserved; x0 takes the displaced value.
x4–x30 including LR untouched. No BL, BLR, RET, WFE, WFI or SMC emitted.

## Exact limitations

Bounded **instruction-loop**, not bounded wall time. Timer reads may trap or
stall; cap cannot recover exceptions, non-returning register accesses, hung
CPU or interrupts that never return. Fast CPUs may hit the cap **early**.
Stagnant counter cannot force infinite looping, but wall time is not bounded.
This perturbs timing, supplies no visible signal and proves no device phase.

SP must already be writable **kernel virtual** memory, 16-byte aligned with
48 writable bytes below it. A nominal post-MMU site does not prove that. Bad
SP/mapping faults at first store; no recovery. Probe/target must be executable
at planned VAs including slide. Tool cannot establish MMU enabled, ISB ran,
or probe entry/continuation succeeded.

**PSCI SYSTEM_RESET omitted**; no reset flag (unsupported options fail). SMC
itself can trap before returning, without local recovery. Returning SMC can
clobber additional registers (x4–x17 under relevant SMCCC conventions), so
x0–x3-only saving is insufficient. Reset/infinite WFE is not safe continuation.
Any future unsafe reset experiment requires separate review, not this generator.

## Offline tests

```sh
python3 -m unittest discover -s tests -p 'test_post_mmu_probe.py' -v
```

Synthetic Mach-Os cover structure, ranges, preservation, replay/B encoding,
output refusal and LLVM AArch64 assembly compilation when available. Passing
proves **none** of the SP, mapping, reachability, MMU, timer or boot assumptions.
