# DETECT

Can a language model write a firmware test that actually catches a bug?

Fault-injection campaigns are generated from plain-English safety requirements and
executed against a correct build and a defective build of the same firmware. A
campaign counts as a detector only if it **passes on the correct build and fails on
the defective one**.

See `RESEARCH.md` for the full research design and `PHASE0.md` for the current phase
specification. See `TOOLCHAIN.md` for pinned versions and `STATUS.md` for what has
actually been observed.

## Layout

| Path | Contents |
|---|---|
| `harness/` | the engine: scenario compiler, judge, storage, validation (Phase 1) |
| `generators/` | model wrappers and the four observation rungs (Phase 3) |
| `systems/` | firmware under test: `powertrain` (System A), `sensor_node` (System B) |
| `defects/` | scripted source transformations (Phase 2) |
| `requirements/` | the requirement corpus (Phase 2) |
| `platforms/` | Renode `.repl` platform descriptions |
| `scripts/` | setup, build and check entry points |
| `results/` | run records, always with provenance |
| `analysis/` | figure and table generation (Phase 4) |
| `paper/` | LaTeX source |

## Quick start

Run these from a WSL shell, in this folder:

```
bash scripts/setup.sh        # toolchain into $HOME, idempotent
bash scripts/boot-check.sh   # exit 0 and a stable sha256
bash scripts/build-firmware.sh bms   # build and gate, without booting
```

`boot-check.sh --prove-gate` and `boot-check.sh --prove-infra-guard` are the two
negative controls: each deliberately breaks something and requires the
corresponding guard to fire.

## A note on the folder name

This folder's name contains a space, and Zephyr cannot build from a path
containing one: its devicetree preprocessing splits the path at the space. The
scripts handle it by re-executing themselves through a space-free symlink,
`$HOME/detect-master`, which points back here. Nothing needs to be done about
this; it is described in `TOOLCHAIN.md` and measured in `STATUS.md` deviation 15.

The build must be driven from WSL. Windows has no Zephyr SDK or Renode for this
workflow, and every script is POSIX shell.
