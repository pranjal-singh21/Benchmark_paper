# PHASE 0: Environment and Skeleton

**Hand this file to Claude Code as the task specification. Read `RESEARCH.md` first
for context, then execute this file.**

---

## 0. What Phase 0 is for

One sentence:

> **Prove that a real compiled ARM firmware binary boots inside Renode and prints its
> banner, reproducibly, from a clean clone, twice, with byte-identical output.**

That is the entire goal. Nothing else. No scenarios, no defects, no models, no
grading. If this does not work, nothing downstream can work, and every hour spent on
downstream code before this is verified is an hour wasted.

**Estimated effort:** half a day.

---

## 1. Environment

### 1.1 Target machine

| Property | Value |
|---|---|
| OS | Ubuntu 24.04 LTS, x86_64 |
| vCPU | 32 dedicated |
| RAM | 64 GB |
| Disk | 300 GB free |
| Network | outbound HTTPS |

Do all work as a non-root user. Everything installs under `$HOME`. Do not use `sudo`
for toolchain components.

### 1.2 Known environment traps

Record these in `TOOLCHAIN.md` as you hit them.

**Ubuntu 24.04 ships no `pip` and no `ensurepip`.** Bootstrap a virtual environment
via `get-pip.py`, and make every script use the venv Python explicitly. A script that
silently falls back to system Python will fail in a way that looks like a code bug.

**`set -u` with an unset variable kills a script before it prints anything.** An empty
output with a non-zero exit is indistinguishable from a catastrophe. Print what you
are about to do before doing it.

**`cmd | tee log` reports `tee`'s exit status**, which almost always succeeds. Use
`PIPESTATUS` or restructure. This trap has produced false green results in similar
projects three separate times.

**Renode is a Mono or .NET application.** Verify it starts and prints a version before
assuming anything else works.

---

## 2. Pinned versions

Install exactly these. Record each in `TOOLCHAIN.md` with a one-line reason.

| Component | Version | Reason for pinning |
|---|---|---|
| Renode | 1.16.1 | peripheral model behaviour differs across releases; determinism claims are version-specific |
| Zephyr | 3.5.0 | driver and CAN filter semantics changed in later versions |
| Zephyr SDK | 0.16.8 | matches Zephyr 3.5.0 |
| Python | 3.12 | Ubuntu 24.04 default |
| Board | `nucleo_h743zi` | STM32H743, Cortex-M7, well modelled in Renode |

Renode is installed from its released portable tarball, not from a package manager.
Zephyr is installed via `west` into `$HOME/zephyrproject`.

---

## 3. Repository skeleton

Create exactly this. Empty directories get a `.gitkeep`. Do not create files for
later phases.

```
detect/
  RESEARCH.md              <- copy in, already written
  PHASE0.md                <- this file
  TOOLCHAIN.md             <- create in this phase
  STATUS.md                <- create in this phase
  README.md
  .gitignore

  harness/
    __init__.py
    .gitkeep
    tests/
      __init__.py
      .gitkeep

  generators/
    .gitkeep

  systems/
    powertrain/
      firmware/
        bms/
          CMakeLists.txt
          prj.conf
          src/main.c
          boards/nucleo_h743zi.overlay
      .gitkeep
    sensor_node/
      .gitkeep

  defects/
    .gitkeep

  requirements/
    .gitkeep

  platforms/
    boards/
      bms_ecu.repl

  scripts/
    setup.sh
    build-firmware.sh
    boot-check.sh

  results/
    .gitkeep

  analysis/
    .gitkeep

  paper/
    .gitkeep
```

### 3.1 `.gitignore`

```
__pycache__/
*.pyc
.venv/
build/
.generated/
results/runs/
*.elf
*.bin
!systems/**/reference/*.elf
```

---

## 4. Tasks, in order

Execute in this order. Do not skip ahead. Verify each before continuing.

### TASK 0.1: Create the skeleton

Create the directory tree above. Initialise git. Make the first commit with the
skeleton and `RESEARCH.md` only.

**Verify:** `git log` shows one commit; `find . -type d | sort` matches the tree.

---

### TASK 0.2: Bootstrap Python

Write `scripts/setup.sh` that:

1. Creates `$HOME/detect-venv` using `get-pip.py` bootstrap, since Ubuntu 24.04 has no
   `ensurepip`.
2. Installs `pyelftools`, `pyyaml`, `pytest`.
3. Exports `DETECT_VENV` and writes it to a file the other scripts source.
4. Prints every step before performing it.
5. Is idempotent: running it twice is harmless.

**Verify:**
```
bash scripts/setup.sh
$DETECT_VENV/bin/python -c "import elftools, yaml; print('ok')"
```

---

### TASK 0.3: Install Renode 1.16.1

Install the portable release under `$HOME/renode-1.16.1`. Add a wrapper at
`scripts/renode` that invokes it with the correct runtime.

**Verify:**
```
scripts/renode --version     # must print 1.16.1
```

If Renode will not start, this is a runtime problem, not a code problem. Diagnose it
now. Do not proceed.

---

### TASK 0.4: Install Zephyr 3.5.0 and SDK 0.16.8

Install via `west` into `$HOME/zephyrproject`, pinned to the v3.5.0 tag. Install the
SDK separately.

**Verify:**
```
west --version
ls $HOME/zephyr-sdk-0.16.8/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc
```

---

### TASK 0.5: Write the minimal BMS application

`systems/powertrain/firmware/bms/src/main.c`.

Requirements for this file, all of which matter later:

1. Prints a banner on the console at boot: `BMS ready`
2. Declares three global variables that will later be injection targets:
   ```c
   volatile int16_t  g_cell_temp_dC;   /* tenths of a degree */
   volatile uint16_t g_pack_mv;        /* millivolts */
   volatile uint8_t  g_tx_enable;      /* transmit enable */
   ```
3. Runs a periodic loop at a fixed tick, reading those globals.
4. **Contains no wall-clock reads, no floating point, and no randomness.** Any of
   these breaks determinism and therefore breaks every timing result in the paper.
5. Sends one periodic CAN frame so the bus is exercised.

`prj.conf` enables CAN, the console UART, and disables anything that introduces
non-determinism.

The devicetree overlay sets the CAN bit rate to 500000 and selects `fdcan1`.

---

### TASK 0.6: Defeat the linker

**This is the single most important task in Phase 0, and it is a silent failure if
missed.**

Zephyr links with `--gc-sections`, which discards any global that nothing else
references. `volatile` does **not** prevent this: `volatile` constrains the compiler,
and the collection happens in the linker.

If an injection target is discarded, it has no address. The injector has nothing to
write to. **The fault is never injected, the firmware behaves correctly for the input
it actually received, and the campaign reports PASS.** That is a false negative in a
verification tool, which is the worst class of bug this project can have.

Two independent defences, and neither may be trusted alone:

1. Add `-Wl,--undefined=<symbol>` for every injectable symbol, driven from a list in
   the build.
2. Add a **build gate** that reads the produced ELF and asserts each symbol from that
   list is present with a non-zero address. Fail the build if any is missing.

**Verify:**
```
$DETECT_VENV/bin/python scripts/check_symbols.py \
    systems/powertrain/firmware/bms/build/zephyr/zephyr.elf \
    g_cell_temp_dC g_pack_mv g_tx_enable
```
Must print each symbol with its address, and exit non-zero if any is absent.

Then deliberately break it: remove one `--undefined` flag, rebuild, and confirm the
gate fails. **A guard that has never been seen to fire is not a guard.**

---

### TASK 0.7: Write the platform description

`platforms/boards/bms_ecu.repl`.

Renode 1.16.1 does not ship a `nucleo_h743zi.repl`. Compose one using `using` from the
STM32H743 CPU description and the closest board description that does ship.

Verify the peripheral names that Zephyr actually uses. The CAN controller on this part
is `fdcan1`, not `can0`. The console is `usart3`.

**Watch for clock errors.** A platform description with a wrong clock frequency
produces firmware that runs at the wrong rate while appearing entirely healthy. The
symptom is subtle: a one-second loop printing more or fewer lines than expected in a
known interval. Check this explicitly before proceeding.

---

### TASK 0.8: Boot it

Write `scripts/boot-check.sh` that:

1. Prints each step before performing it.
2. Builds the firmware from clean.
3. Runs the symbol gate.
4. Launches Renode with a `.resc` that loads the platform, loads the ELF, shows the
   UART analyser, and runs for a fixed amount of **virtual** time.
5. Captures the console output to a file.
6. Computes and prints the SHA-256 of that output.
7. Exits 0 only if the banner appeared.
8. Checks the exit status of the correct command in every pipeline.

**Verify, and this is the Phase 0 exit criterion:**
```
bash scripts/boot-check.sh      # exit 0, prints a sha256
bash scripts/boot-check.sh      # exit 0, prints the SAME sha256
```

Then delete `build/` entirely and run it a third time. Same hash.

---

### TASK 0.9: Write `TOOLCHAIN.md` and `STATUS.md`

`TOOLCHAIN.md`: every pinned version, where it came from, and one line on why it is
pinned.

`STATUS.md`: what was **observed**, not what was intended. Include:
- the exit criterion result, with the actual hash
- every deviation encountered, with the diagnostic that actually worked, not the first
  one tried
- the symbol addresses resolved
- anything that surprised you

The `--gc-sections` finding is the model for how to record a deviation. A wrong hint
in a status file costs the next person real hours.

---

## 5. Exit criterion

Phase 0 is complete when, and only when:

```
   From a clean clone:
     bash scripts/setup.sh
     bash scripts/boot-check.sh    -> exit 0, sha256 = H
     bash scripts/boot-check.sh    -> exit 0, sha256 = H   (identical)
     rm -rf build && bash scripts/boot-check.sh
                                   -> exit 0, sha256 = H   (identical)

   AND the symbol gate fires when a --undefined flag is removed.
   AND TOOLCHAIN.md and STATUS.md are written.
   AND everything is committed.
```

**Do not proceed to Phase 1 until all of these hold.** Report the failure instead.

---

## 6. Rules while building

These are not style preferences. Each exists because of a specific failure mode.

**Print before you do.** Every script announces its step before executing it. An empty
output with a non-zero exit is indistinguishable from a catastrophe.

**Check the right exit status.** `cmd | tee log` reports `tee`'s status. Use
`PIPESTATUS` or restructure the pipeline.

**A silent no-op is worse than a crash.** If an injection target cannot be resolved,
if a build step is skipped, if a file is not found: fail loudly and name the thing.
Never continue with a default.

**Never normalise an out-of-range value into a valid one.** Silently converting bad
input into acceptable input is falsification, not robustness.

**Guards must be seen to fire.** Every check written in this phase must be
deliberately triggered once, to confirm it works. A guard that has never failed is an
untested claim.

**Pin everything, record everything.** If a version is not in `TOOLCHAIN.md`, it is
not pinned.

**Commit at every green step.** Small commits with real messages. The history is
evidence for the paper, particularly the ordering of requirements before defects in
Phase 2.

**Record deviations as they are found**, in `STATUS.md`, with the diagnostic that
actually worked.

---

## 7. What Phase 0 explicitly does not include

Do not build any of these yet. Building them before the exit criterion is met is
wasted work on an unverified foundation.

- The scenario language or its compiler
- Any defect or patch script
- Any requirement
- Any model integration
- The grader, the sweep, the batching layer
- System B
- Anything in `analysis/`

---

## 8. Immediately after Phase 0

For context only. Do not start these now.

**Phase 1** builds the engine: the scenario compiler, the in-emulator CAN toolkit, the
judge, provenance-enforced storage, static validation, batching, and the execution
trace differ. Its hardest exit criterion is determinism: identical verdicts, identical
latencies, and byte-identical event logs at one worker and at ten. Every timing result
in the paper rests on it.

**Phase 2** is the critical path: the golden firmware's eight safety functions, the 36
scripted defect patches, the 45 requirements written before consulting the defects,
and the exhaustive sweep that measures every defect's detection set.
