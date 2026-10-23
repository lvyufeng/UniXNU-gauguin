#!/usr/bin/env python3
"""ADT generator for gauguin (SM7125, lagoon, Adreno 610).

Contract: `[payload.adt] tool` in board.toml is called with the keys of that
table as options and writes the ADT that DarwinBoot hands to XNU. The nodes XNU
needs are the same ones every UniXNU board describes; the values here are the
SM7125 ones.

Not verified on hardware yet - stage 5 of docs/porting/bring-up.md is what
confirms it.
"""
import argparse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--chip-id", default="0")
    ap.add_argument("--owned-memory-size", default="0x10000000")
    ap.add_argument("--cpu-regs", default="0,0x100")
    ap.add_argument("--psci-method", default="smc")
    args = ap.parse_args()
    print(f"make-gauguin-adt: output={args.output} chip_id={args.chip_id} "
          f"psci={args.psci_method}")
    print("make-gauguin-adt: this generator is a placeholder until the SM7125 "
          "nodes are filled in from the device tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
