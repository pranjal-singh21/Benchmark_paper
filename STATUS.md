# Status

What was **observed**, not what was intended. A box is ticked only when the thing
was executed and its result seen. Writing a script is not evidence that it works;
running it is.

---

## Phase 0 — Environment and skeleton: **COMPLETE**

Goal: prove a real compiled ARM firmware binary boots inside Renode and prints its
banner, reproducibly, from a clean clone, twice, with byte-identical output.

- [x] TASK 0.1 Repository skeleton, `.gitignore`, git initialised
- [x] TASK 0.2 Python venv bootstrapped via `get-pip.py`
- [x] TASK 0.3 Renode 1.16.1 installed from the portable tarball, version verified
- [x] TASK 0.4 Zephyr 3.5.0 and SDK 0.16.8 installed and version verified
- [x] TASK 0.5 Minimal BMS application: banner, three injection targets, fixed tick, periodic CAN frame
- [x] TASK 0.6 Linker defeated: `--undefined` flags plus an ELF symbol gate, **and the gate was seen to fire**
- [x] TASK 0.7 Platform description written; CPU clock verified by counting reports in a known window of virtual time
- [x] TASK 0.8 `scripts/boot-check.sh` green, three times, with an identical hash
- [x] TASK 0.9 `TOOLCHAIN.md` and `STATUS.md` written

### The exit criterion, as executed

```
bash scripts/boot-check.sh                     -> exit 0
bash scripts/boot-check.sh                     -> exit 0   (incremental build)
rm -rf build/ && bash scripts/boot-check.sh    -> exit 0   (built from scratch)
bash scripts/boot-check.sh --prove-gate        -> exit 0   (symbol gate fired)
bash scripts/boot-check.sh --prove-infra-guard -> exit 0   (engine-fault guard fired)
bash scripts/boot-check.sh                     -> exit 0   (after restore)
```

**Console SHA-256, identical on all four passing runs:**

```
6f35f44a8034ebb366b632bca1c84e88b6f7437635293837de2ba320584db753
```

332 bytes. The observed console:

```
*** Booting Zephyr OS build zephyr-v3.5.0 ***
BMS ready
bms can_start rc=0
bms tick=0 temp_dC=250 pack_mv=48000 tx=1 txrc=0
bms tick=10 temp_dC=250 pack_mv=48000 tx=1 txrc=0
bms tick=20 temp_dC=250 pack_mv=48000 tx=1 txrc=0
bms tick=30 temp_dC=250 pack_mv=48000 tx=1 txrc=0
bms tick=40 temp_dC=250 pack_mv=48000 tx=1 txrc=0
```

`can_start rc=0` and `txrc=0` are the CAN controller accepting the frame, so the
bus is genuinely exercised rather than merely linked.

### Symbol addresses resolved

From `scripts/check_symbols.py`, reading `.symtab` with pyelftools. All in
`axiSram` at `0x24000000`.

| Symbol | Address | Size | Type |
|---|---|---|---|
| `g_gc_canary` | `0x24000000` | 4 | `volatile uint32_t` — negative control, referenced by no C |
| `g_pack_mv` | `0x24000050` | 2 | `volatile uint16_t` |
| `g_cell_temp_dC` | `0x24000052` | 2 | `volatile int16_t` |
| `g_tx_enable` | `0x24000074` | 1 | `volatile uint8_t` |

### Both guards were seen to fire

#### The symbol gate

`bash scripts/boot-check.sh --prove-gate` rebuilds with
`-DDETECT_OMIT_UNDEFINED=g_gc_canary`, withholding exactly one `-Wl,--undefined`
flag. Observed:

```
g_gc_canary        ABSENT
PASS: the symbol gate fired as required (exit 1).
```

This single experiment establishes three separate things that were previously
assumptions: that `--gc-sections` really does discard an unreferenced global in
this build, that `--undefined` really does prevent it, and that the gate really
does detect it. The script then rebuilds the normal binary and the hash returns
to the value above.

#### The engine-fault guard

`bash scripts/boot-check.sh --prove-infra-guard` asks Renode to load a platform
file that does not exist. Observed:

```
renode exit status was 0
There was an error executing command 'machine LoadPlatformDescription @.../no-such-platform.repl'
Could not find file '.../no-such-platform.repl'.
PASS: the engine-fault guard fired as required.
```

See deviation 12 for why this guard exists.

---

## Deviations, with the diagnostic that actually worked

### 1. The CAN controller had no medium, and the symptom looked like a firmware bug

**This is the most valuable finding in Phase 0.**

The firmware booted, printed `BMS ready`, printed `can_start rc=0`, and then
produced **nothing at all**. Zero periodic reports in five seconds of virtual
time. A board that boots perfectly and then goes silent reads unmistakably like a
hang in the application loop.

It is not. The firmware is correct.

The diagnostic that worked was grepping Renode's own log, not the console:

```
grep -i -E 'fdcan|mcan' results/boot-check/renode.log
```

which produced:

```
fdcan1: FrameSent is not initialized. Is the controller connected to medium?
```

Renode's `CAN.MCAN` model has no `FrameSent` event to raise unless the controller
is connected to a bus. Without it the transmit never completes, the Zephyr driver
waits for a completion interrupt that cannot arrive, and `can_send` blocks — before
the first `printk` of the loop, which is why not one report appeared.

The fix is two lines in the `.resc`, and it is an emulation-setup fix, not a
firmware change:

```
emulation CreateCANHub "canHub"
connector Connect sysbus.fdcan1 canHub
```

One node on the hub is sufficient for transmission to complete. Exact syntax
confirmed against Renode 1.16.1's own shipped
`scripts/complex/socketcan_bridge/nucleo_h743zi-socketcanbridge.resc`.

**Why this matters beyond Phase 0.** Every node in Phase 1 must be connected to the
hub. A node accidentally left unconnected will not error: it will boot, look
healthy, and silently transmit nothing. Any scenario asserting on its frames would
fail for a reason that has nothing to do with the firmware under test.

### 2. `--gc-sections` is real, but the three injection targets were never at risk

The specification's mechanism is correct and was confirmed. The nuance found in
practice: `main.c` reads all three injection targets on every loop iteration, so
they are reachable from the entry point and would survive **even without** their
`--undefined` flags. The literal test the spec suggests — remove one flag and watch
the gate fail — would therefore have passed and demonstrated nothing.

Rather than report a guard that cannot be triggered, `g_gc_canary` was added: a
global referenced by no line of C anywhere, whose only protection is its
`--undefined` flag. It is the negative control, and it makes the mechanism
observable without disturbing the three real targets. See the section above.

### 3. Zephyr's release tag has two spellings, and the first version guard rejected a correct tree

`git describe --tags --exact-match` in the Zephyr checkout returns
**`zephyr-v3.5.0`**, not `v3.5.0`. Zephyr publishes both. The first version of
`scripts/setup.sh` asserted equality with `v3.5.0` and killed the setup on a
correctly pinned tree.

Fixed by accepting either spelling and, more importantly, by making the
authoritative check the `VERSION` file, which is what the build itself reads. A
guard that rejects correct input is a real defect even though it fails safe.

### 4. Renode's file backend rotates, it does not truncate

`CreateFileBackend` on an existing path renames the old capture aside as
`<name>.1` rather than overwriting it. Rotations accumulate, and any glob over the
results directory would silently fold a previous run into the current hash.
`boot-check.sh` therefore wipes the entire run directory before each run rather
than deleting two named files.

### 5. The boot banner depends on the Zephyr tree's git state

Zephyr derives the banner's build version from `git describe` run inside
`ZEPHYR_BASE` (`cmake/gen_version_h.cmake`), so the console hash was a function of
the *Zephyr checkout's* git state, not of anything in this repository. A commit in
that tree, or a Zephyr unpacked from a tarball where `git describe` fails, would
move the hash for a reason unrelated to the firmware. Pinned with
`-DBUILD_VERSION=zephyr-v3.5.0`. The version gate is unaffected: the tag and the
`VERSION` file are checked directly.

### 6. Renode reports a four-part version string

`renode --version` prints `Renode v1.16.1.16908`, not `1.16.1`. An exact string
comparison fails against a correct install. The check is a substring match against
the pinned version, and the raw string is printed so a mismatch can be read.

### 7. No `sudo`, so host tools were installed rootless

`sudo` on this host requires a password, so `apt install` is unavailable. Zephyr's
host dependencies `dtc` and `gperf` were obtained with `apt-get download`, which
runs unprivileged, and unpacked with `dpkg-deb -x` into `$HOME/detect-hosttools`.
Observed `dtc 1.7.0`, `gperf 3.1`.

`dtc` proved not to be required: Zephyr parses devicetree with its own Python
`edtlib` and only uses `dtc` as an optional validator. It is installed anyway so
that its absence is not a permanent unexplained warning in every build log.

### 8. `ensurepip` is absent, exactly as the specification predicted

Ubuntu 24.04 ships no `pip` and no `ensurepip`. The venv is created with
`--without-pip` and pip is bootstrapped from `get-pip.py`. Every script invokes
the venv interpreter by absolute path via `.detect-env`.

### 9. Host is WSL2, not a dedicated Linux VM

The specification assumes Ubuntu 24.04 with 32 vCPU and 64 GB RAM. Actual host:
Windows 11 running WSL2 Ubuntu 24.04.3 LTS, **12 vCPU, 5 GB RAM**, 946 GB free.

Adequate for Phase 0 and Phase 1. **Not adequate for the Phase 4 sweep**, which
sizes ten parallel workers at three cores each. Either raise the WSL memory and
CPU allocation in `.wslconfig`, or run the sweep on the rented VM the plan
budgets for. This is a scheduling decision, not a blocker, and it is flagged now
because it is cheaper to resolve before Phase 4 than during it.

The repository is the Windows folder `Downloads\benchmark paper` itself, reached
from WSL as `/mnt/c/Users/djtde/Downloads/benchmark paper`. It was originally
built at `~/detect` inside the WSL filesystem and moved here; see deviation 15
for how a path containing a space is made to work.

### 10. Substantial verified prior work already existed on this host

`$HOME` contains prior experiments from a related project, including a
`clean-clone` tree that had already booted a Zephyr 3.5.0 ELF on this exact
emulated board, and an `h7-can` tree that had already transmitted CAN frames on
`fdcan1`. Zephyr 3.5.0, SDK 0.16.8 and Renode 1.16.1 were already installed.

These supplied load-bearing facts with *observed evidence* rather than derivation:
that Renode 1.16.1 ships `platforms/cpus/stm32h743.repl` and
`platforms/boards/nucleo_h753zi.repl` but **no** `nucleo_h743zi.repl`; that the
console is `usart3` and the CAN controller `fdcan1`; that `bus-speed` is the
correct devicetree property at this Zephyr version; and the working `can_start` /
`can_send` call sequence. `setup.sh`'s idempotency guards detected the existing
SDK and Zephyr and did not reinstall them.

### 11. Agent-environment trap: `wsl.exe -- bash -c '...'` delivers empty variables

Running `wsl.exe -d Ubuntu -- bash -c '... $var ...'` from Git Bash silently
delivers **empty** shell variables: a `for f in a b c` loop body sees `$f` as the
empty string. Loops appear to run the right number of times while doing nothing.
Nothing errors.

Anything non-trivial must be written to a script file and executed as
`wsl.exe -d Ubuntu -- bash /path/to/script.sh`, with `MSYS_NO_PATHCONV=1` set or
Git Bash rewrites `/mnt/...` into `C:/Program Files/Git/mnt/...`.

---

### 12. Renode exits 0 even when a monitor command fails

`renode --version` succeeding and the process returning 0 does **not** mean the
script it was given actually ran. Measured on Renode 1.16.1, with stdin closed:

| Provoked failure | Exit status | Log |
|---|---|---|
| `machine LoadPlatformDescription @/no/such/file.repl` | **0** | `There was an error executing command ...` / `Could not find file ...` |
| `sysbus LoadELF @/no/such/file.elf` | **0** | `There was an error executing command ...` |
| an unrecognised command | **0** | no error marker at all |

The consequence is specific and bad. An earlier version of `boot-check.sh`
treated `renode_rc == 0` as "the emulator ran fine". A run in which the firmware
was never loaded would therefore have reached the console assertions, found an
empty UART, and been reported as a **firmware failure** with exit 1. That is the
exact conflation of engine fault with test verdict that this project must not
make, and it would have counted as a legitimate negative result.

`boot-check.sh` now scans the Renode log for the markers above and classifies any
of them as an engine fault, exit 2. The guard is exercised by
`--prove-infra-guard`.

Two related findings from building the prover, both worth keeping:

- **stdin must be closed.** If a scripted command fails while stdin is attached,
  Renode drops to its interactive monitor and waits forever, so a command error
  surfaces as a 300 s wall-clock timeout instead of as the error it is. The
  Renode invocation now redirects from `/dev/null`.
- **An unrecognised command is not a usable negative control.** It exits 0 and
  emits none of the markers, so it proves nothing. A nonexistent ELF is no better:
  it errors, but leaves a machine with no firmware and the emulation then runs to
  the timeout. Only a nonexistent platform file fails fast *and* emits a marker.

This was found by an adversarial review of the first version of `boot-check.sh`,
which also independently reproduced the CAN medium finding in deviation 1 by
measuring the console with and without the hub.

### 13. The board devicetree already sets a CAN bit rate, and it is not ours

`boards/arm/nucleo_h743zi/nucleo_h743zi.dts` enables `fdcan1` itself, with
`pinctrl-0 = <&fdcan1_rx_pd0 &fdcan1_tx_pd1>`, `status = "okay"` and
**`bus-speed = <125000>`**. The board also already declares
`chosen { zephyr,canbus = &fdcan1; }`.

Two consequences:

- No pinctrl overlay is needed. The overlay in this repository only overrides the
  bit rate.
- **The default is 125000, not 500000.** If the overlay were dropped, or its
  property misspelled, the bus would run at a quarter of the intended rate and
  nothing would report it. Every frame would still be sent and received, and
  every timing measurement would be wrong by a factor of four.

Verified that the override actually takes effect, rather than assuming it:

```
build/zephyr/include/generated/devicetree_generated.h
    #define DT_N_S_soc_S_can_4000a000_P_bus_speed 500000
build/zephyr/zephyr.dts
    bus-speed = < 0x7a120 >;          # 0x7a120 = 500000
```

The property name is `bus-speed` at Zephyr 3.5.0, per
`dts/bindings/can/can-controller.yaml`. It is not `bitrate` and not `bus_speed`.

**Correction, made in Phase 1 by testing it.** This section previously claimed a
misspelled property is silently ignored. That is false, and the claim came from a
review rather than from a measurement. Writing `bus_speed` produces a hard
devicetree error:

```
devicetree error: 'bus_speed' appears in /soc/can@4000a000 ... but is not
declared in 'properties:' in .../st,stm32h7-fdcan.yaml
```

An undeclared property on a node that has a binding is rejected. The misspelling
case is therefore loud and needs no guard.

The genuinely silent case is a **missing overlay**. Zephyr does not warn about an
overlay that is not there; it simply leaves the board default of 125000 standing.
Measured with the overlay removed and a pristine build: the firmware compiled
cleanly, Zephyr reported nothing, and `scripts/check_bitrate.py` was the only
thing that noticed, reporting 125000 against the declared 500000. That is what
the guard exists for, and it is what it was seen to fire on.

One wrinkle found while testing: with a *stale* build directory the overlay path
is cached, so deleting the file produces a hard preprocessing error instead of a
silent build. The silent case needs a pristine build, which is exactly the case a
fresh clone or a CI runner presents.

### 14. `uint16_t` millivolts caps the pack voltage at 65.535 V

`PHASE0.md` fixes the injection target's type as `volatile uint16_t g_pack_mv`
with units of millivolts. That representation cannot exceed **65535 mV, or
65.535 V**. The firmware here defaults it to `48000`, a 48 V pack, which fits.

This is not a problem in Phase 0 and it is not a defect in the specification,
which mandates the type but no particular value. It is recorded because Phase 2
implements over-voltage and under-voltage protection on a *powertrain* pack: a
400 V pack is 400000 mV and silently wraps to 7616 in this type. Any value above
65535 assigned to this symbol, by the firmware or by an injected write, wraps
without warning.

Phase 2 must therefore pick one of: widen the symbol to `uint32_t`, change the
unit to tens of millivolts or to volts, or keep a low-voltage pack. Deciding it
now costs nothing; discovering it from an injected value that silently wrapped
would look exactly like a firmware defect.

### 15. Zephyr cannot build from a path containing a space, and the fix is a symlink

The repository lives in `Downloads\benchmark paper`. Zephyr's devicetree
preprocessing splits that path at the space. Measured, not assumed:

```
-- Application: /mnt/c/Users/djtde/Downloads/benchmark paper/.../bms
-- Found devicetree overlay: paper/.../boards/nucleo_h743zi.overlay
<command-line>: fatal error: /mnt/c/Users/djtde/Downloads/benchmark:
                No such file or directory
CMake Error at .../extensions.cmake:3885 (message):
  failed to preprocess devicetree files (error code 1)
```

Note the second line: the overlay path is reported as `paper/...`, the fragment
after the space. Two builds were tried and both failed identically, one with the
build directory on the WSL filesystem and one with it beside the source, which
establishes that the failure is in handling the **source** path and cannot be
avoided by redirecting output.

The fix is a space-free symlink, `/home/djtde/detect-master`, pointing at the
repository. CMake does not resolve a symlink back to its real path, so the
toolchain only ever sees `/home/djtde/detect-master/...`. Verified: the same
application builds cleanly through the symlink.

Every script re-executes itself through the symlink when it detects a space in
its own root, creating the link if absent, so this is invisible in use. The
branch cannot recurse, because after the re-exec the root is the symlink and
contains no space.

**Re-verified end to end after the move**, every script invoked through the
spaced path:

| Check | Result |
|---|---|
| three runs, one after deleting `build/` | console hash `6f35f44a...`, **unchanged** |
| `--prove-gate` | fired, `g_gc_canary` absent |
| `--prove-infra-guard` | fired |
| `build-firmware.sh` | exit 0 |

One thing that did change: the **ELF** hash. It was `16c08a88...` at the old path
and is `781c3344...` here, because the source path is recorded in the binary's
debug information. The ELF is path dependent; the console is not. Since the exit
criterion measures the console, the move is invisible to it. Phase 2 should not
use an ELF hash as a build identity across machines or directories.

Cost of the move: `/mnt/c` is roughly 1.5 to 2 times slower than the WSL
filesystem. Clean build plus boot went from about 80 s to 142 s. Acceptable now;
for the Phase 4 sweep, `BUILD_DIR` can point at `$HOME` to keep the object tree
on the Linux filesystem.

---

## Phase 1 - The engine: IN PROGRESS

Exit criteria, per `PHASE1-AMENDMENT.md` section 10. Seven of ten met.

| # | Criterion | State |
|---|---|---|
| 1 | Correct PASS on golden | **MET** |
| 2 | Correct FAIL on canary, expected assertion | **MET** |
| 3 | Determinism, as reworded | in progress |
| 4 | Batching, byte-identical, under 20 s each | not started |
| 5 | Provenance refused without provenance.json | **MET** |
| 6 | Static validation rejects five error classes | **MET** |
| 7 | Whole-bus matching fails on a peer-sent frame | **MET** |
| 8 | Bit-rate assertion fires | **MET** |
| 9 | Purity test passes and fires on a planted name | **MET** |
| 10 | Trace stability, IDENTICAL across runs | **MET, in serial mode only** |

### The founding observation, reproduced

| scenario | golden | canary | |
|---|---|---|---|
| boundary, 550 then 551 | PASS | **FAIL** | detects |
| well past, 900 | PASS | PASS | **does not detect** |

Both scenarios execute the same comparison and achieve the same coverage of it.
Only the one that probes the boundary can tell `>` from `>=`, because above the
limit the two operators behave identically. The failing assertion on the canary
is the expected one, not an unrelated timeout.

---

## Execution mode: SERIAL, and it is mandatory

```
   chosen execution mode:     serial   (emulation SetGlobalSerialExecution True)
   chosen quantum:            Renode default (no SetGlobalQuantum emitted)
   trace stability, serial:   IDENTICAL across 5 scenarios
   trace stability, parallel: DIVERGED on all 5
   canonical window W:        0 us  (event logs are exact in serial mode)
```

### Why parallel execution is not usable

Criterion 10 was run first, per the amendment, because it decides the
architecture. Two captures of the **same golden binary** under **identical
conditions**, in parallel mode:

| scenario | first divergence | delta, instructions |
|---|---|---|
| overtemp-boundary | step 2, bms | -42 |
| peer-heartbeat-present | step 2, bms | +33 |
| overtemp-well-past | step 3, bms | -24 |
| injection-lands | end of scenario | +36 |
| silence-stops-traffic | step 2, bms | -33 |

That is 0.08 percent of roughly 50000 instructions, and every divergence appears
at the first checkpoint **after the peer has transmitted**. The magnitude and the
placement both say the same thing: an interrupt landing at a different
instruction boundary, not a change of behaviour.

The size does not matter. What matters is that the noise floor is non-zero, so
`IDENTICAL` never occurs, so rung 4's Case A message could never fire and the
divergence classifier would call every comparison a divergence. The central
contribution of the paper is not implementable in parallel mode.

In serial mode all five return `IDENTICAL`, and the event logs are byte-equal at
window 0.

### Why the quantum is left at Renode's default

Renode's own multi-node CAN tests set both `SetGlobalSerialExecution` and a 25 us
`SetGlobalQuantum`. Only the first is load bearing here: serial execution at the
**default** quantum already produces byte-identical event logs across repeated
runs and under competing load. The quantum line is therefore not emitted.

This also removes a fidelity question before a reviewer can ask it. The firmware
ticks every 10 ms and the corpus deadlines are 50 to 300 ms, so Renode's default
quantum is orders of magnitude finer than the guest's own tick and cannot change
which tick a frame lands in. Behaviour is determined by the tick, so
synchronisation granularity at these values cannot affect it.

### Cost, measured

|  | startup | per virtual second |
|---|---|---|
| parallel | 23.9 s | 1.02 s |
| serial | 14.6 s | 7.12 s |

Serial execution costs **7x on simulation**. It does not cost 7x on a run,
because a run is dominated by fixed startup, and startup is exactly what batching
removes:

```
   typical scenario, 1.3 s of virtual time

     unbatched serial   15 + 7.1 x 1.3  =  24 s
     batched serial          7.1 x 1.3  =   9 s     under the 20 s target
```

**Serial execution is affordable once batched, and only once batched.** Deciding
this on the unbatched number, as was nearly done, would have rejected the only
configuration in which rung 4 works.

There is a second effect in serial execution's favour. With machines advancing
one at a time, a run occupies roughly one core instead of one per machine, so
more runs fit on the host at once. The 7x simulation penalty is partly offset by
better worker density, and the Phase 4 budget should be recomputed with both.

### Three attempts to measure it, and why the first two were wrong

Worth recording, because the same smell caught both: **more virtual time costing
less wall clock**, which is impossible.

1. Measured while the previous run was still finishing. Load average 6.6.
2. Idle host, but the FIRST run of a session pays cold start: assembly load, JIT,
   and extraction of the translation library into `/tmp`. Run order dominated.
3. One warm-up run discarded, then two timed runs per configuration, reporting
   the minimum. The minimum is the right statistic because contamination can only
   add time.

Measurement on a loaded host is not measurement, and the first number a machine
gives is not its steady state.

---

## Deviations found in Phase 1

### 16. A passing absence assertion stayed live and fired twice

The worst of the three, because of its class. `expect_no_can` sets `resolved`
only when something hits it, so an assertion that PASSED was never marked
resolved. It stayed live after its window closed, matched the next ordinary
status frame, and emitted `FORBID_HIT`. The same assertion then appeared twice in
the verdict, once passed and once failed, and the scenario failed on an
observation from outside its own window.

**This is a false-alarm generator: it produces FAIL on correct firmware.** That
is the exact mirror of the false pass this project exists to measure, and it is
equally fatal. A campaign built on it would report detections that never
happened.

Fixed by making a closed matcher stop matching, and by refusing any frame
arriving after a window has elapsed.

### 17. Renode holds the console file open

`ct_uart_expect` originally read the captured console from inside the emulator
and got `[Errno 32] Sharing violation`. Renode keeps the file backend open for
the whole emulation.

Rather than tap a second, different stream, which could disagree with the
artefact a person actually reads, the assertion is now recorded during the run
and decided host-side once the file is closed.

### 18. `os.getcwd()` resolved the symlink, putting the space back

Compiled scripts carried `/mnt/c/Users/djtde/Downloads/benchmark paper/...`,
because `os.getcwd()` and `Path.resolve()` always resolve to the physical path,
and this repository is reached through a space-free symlink precisely because its
real path contains a space Renode cannot parse. Renode splits an `@path` argument
at whitespace, so this would have failed deep inside the emulator with a message
pointing nowhere near the cause.

Paths are now re-based onto the space-free root, and a generated path containing
a space is refused at compile time naming the line.

**The engine reported exit 5, not a verdict, on 16 and 17.** That is the
crash-versus-failure discipline working on its first real test: an engine fault
must never be counted as a firmware failure, because a table built from those
verdicts is silently wrong.

---

---

## Renode multi-machine batching: three crashes, and the fix

Anyone attempting to batch multi-machine scenarios in Renode hits all three of
these. They are recorded here, in the README, and in one sentence of the paper's
setup section, because the failure mode of each is a native crash with no useful
message.

### 1. Removing a machine leaves the CAN hub holding a dead reference

`mach rem` does not detach the machine from a hub it is connected to. The hub
keeps the reference, and the next scenario's first transmit dies inside the
peripheral model:

```
FATAL UNHANDLED EXCEPTION: System.ArgumentException:
  Couldn't find machine for a given peripheral of type ...CAN.MCAN
    at MCAN.TxScan()
```

A hub per scenario removes the stale reference but does not fix the next problem.

### 2. Recreating a machine crashes the emulator natively, under ANY name

With a fresh hub per scenario, the second scenario still died, this time with a
SIGSEGV in native code rather than a managed exception:

```
Got a SIGSEGV while executing native code ... renode exit=-11
```

The obvious suspect is name reuse, so the machines were given per-scenario names
as well. **It crashed identically.** It is the machine lifecycle itself, not the
name: creating and destroying machines repeatedly in one process is not
something this build survives.

### 3. The fix: create once, reset between scenarios

```
    mach set "<node>"
    machine Reset
    sysbus LoadELF @<elf>
    <peripheral> CreateFileBackend @<this scenario's console> true
```

Verified: the console after a reset is identical to a cold boot, including the
Zephyr banner and every initialisation line. `machine Reset` returns the
peripherals and the CPU to their power-on state, reloading restores `.data` and
the entry point, and Zephyr's own startup zeroes `.bss`.

The taps are attached once and deliberately survive the reset, because the
controllers do. `ct_reset` clears every accumulator in the toolkit but **not**
the tap table: clearing it would leave the next scenario observing nothing at
all, which reports as a clean absence, which is a silent false pass.

Batched results are byte-identical to unbatched across all five scenarios, at
window 0, with a 0 us latency delta. That equality is the evidence the reset is a
real reset and not an approximation of one.

---

## What the process actually died of, and why the first diagnosis was wrong

The 10-worker determinism measurement kept dying. The first diagnosis was that
its process group was being torn down when the launching shell exited, and the
fix was assumed to be `setsid`.

**That diagnosis was wrong, and testing it is what showed it.** All three detach
strategies -- `nohup`, `setsid`, `setsid --fork` -- survive an abandoned shell
here; a 45-second detached job outlives its parent under all of them.

The real cause is the out-of-memory killer:

```
Out of memory: Killed process 12972 (renode) ... anon-rss:561168kB
Out of memory: Killed process 12236 (systemd)
Out of memory: Killed process 12237 ((sd-pam))
```

One batch peaks at **758 MB** of emulator resident memory. Ten concurrent need
about 7.6 GB and this host has 5.7 GB, so the kernel killed the emulators **and
the session's own systemd**, which is exactly why it presented as a process-group
failure. Six workers also died. Four fit.

### Consequences for Phase 4

| quantity | measured |
|---|---|
| cores per worker, serial execution | **~1.0** (99.6% median, 151% peak) |
| memory per worker | **~758 MB** |
| workers that fit on this host, 5.7 GB | **4** |
| workers on the budgeted VM, 32 cores / 64 GB | **~28**, core-bound not memory-bound |

Serial execution advances machines one at a time, so a worker occupies roughly
one core rather than one per machine. That is a real benefit and it partly offsets
the simulation penalty: more runs fit on a host at once than the parallel
configuration would allow.

### `scripts/job.sh`

Long jobs now run detached with a heartbeat, and **liveness is measured by
heartbeat age, never by process name**. `pgrep -f check-determinism.sh` matches
the wrapper shell whose own command line contains that string, so it reports a
dead job as alive. It did exactly that here. A stale heartbeat with no exit
status means killed, which is a different thing from exited non-zero, and the
message says so and names the usual cause.

The determinism check is checkpointed: a batch whose exit status is recorded is
skipped, so a killed run resumes rather than restarting. At Phase 4's scale a
restart from zero costs a night.

---

## The methodological lesson worth keeping

> Serial execution measured on the unbatched path appeared prohibitive at 27 s
> per scenario. On the batched path it is 8 s. Serial is the only configuration
> in which trace comparison is possible, so a decision made on the unbatched
> number would have removed the mechanism the paper is about.

The same shape of error appeared three times in one day, and each time the tell
was a number that could not be true:

| measurement | impossible thing | actual cause |
|---|---|---|
| cost model, attempt 1 | 3000 ms of virtual time cost less than 50 ms | measured while the previous run was still finishing |
| cost model, attempt 2 | same inversion, on an idle host | the first run of a session pays cold start |
| serial cost | 6x, later 3.5x, later 7x on simulation only | three different baselines, none stated |

The habit that caught all three: when a measurement implies something impossible,
the measurement is wrong, not the world. Measurement on a loaded host is not
measurement, and the first number a machine gives is not its steady state.

---

---

## CORRECTION: the first trace primitive measured nothing

This retracts the trace-stability results reported earlier in this file. They
were not wrong about the numbers; they were wrong about what the numbers meant.

### What was claimed

Trace checkpoints recorded each CPU's retired-instruction count
(`GetCurrentInstructionsCount`). On that basis it was reported that traces
DIVERGED in parallel execution and were IDENTICAL in serial, and criterion 10 was
marked met.

### What is actually true

The counter tracks **elapsed virtual time, not work done**. Measured on ONE
binary, same virtual duration, provably different work:

| injected value | fault raised? | frames carrying `fault_code=01` | ExecutedInstructions |
|---|---|---|---|
| 250 | no | 0 | **62639** |
| 900 | yes | 5 | **62639** |

Identical to the digit, while the firmware demonstrably did different things. The
same value, 62639, also appears in the golden and canary runs of every scenario.

So the earlier results were reporting quantum drift in parallel mode and its
absence in serial mode, and never execution at all. **The check passed while
measuring nothing**, which is precisely the failure class this project exists to
measure, occurring inside the instrument.

`GetAllOpcodesCounters` was tried as a replacement and returns no usable data on
this build without registering opcode patterns first.

### What replaced it

The divergence signal is now the firmware's **observable behaviour**: every frame
on the bus from every node, and every byte of console, taken from the event log
with the harness's own bookkeeping lines removed.

This is not a weaker substitute for a basic-block trace. For this project it is
the more meaningful quantity, because the scenario language can only assert on
observable things. Case A does not need to mean "the two builds executed
identically"; it needs to mean **"nothing an assertion in this language could have
seen was different"**, and that is exactly what this measures.

The cost is a real and stated limit: a defect that changes internal state without
ever becoming observable classifies as Case A. That is the correct answer for a
generator being told what to do next, since no campaign in this language could
have caught it, but it is not the same statement as "the code paths were
identical", and the paper must not claim the stronger one.

### Criterion 10, re-established properly

Under the corrected definition the evidence is stronger than before, because it
already exists: the determinism measurement compared event logs across 60
pairs of runs at differing worker counts and found them **byte-identical, W = 0**.
Byte-identical event logs are byte-identical observable behaviour, so a capture
of the golden build is reproducible across runs by direct measurement rather than
by a proxy.

### The three cases, verified

| case | golden | defective | observable | classified |
|---|---|---|---|---|
| boundary, 550 then 551 | PASS | FAIL | DIVERGED | **DETECTED** |
| well past, 900 | PASS | PASS | IDENTICAL | **CASE A** |
| constructed oracle failure | PASS | PASS | DIVERGED | **CASE B** |

Case B had to be built deliberately, exactly as the amendment said: at the limit
the defective build raises a fault and the correct build does not, so behaviour
differs, while the assertion checks something true in both. The message it
produces names the differing byte:

```
First difference at 501200 us of virtual time:
  correct build:   501200  TX  bms  0x604  00 80 BB 00 78 EC 05 A4
  defective build: 501200  TX  bms  0x604  01 80 BB 00 78 EC 05 A5
```

Byte 0 is `fault_code`. A generator receiving that has been told which signal to
assert on, without being told the answer.

---

## Criterion 3, measured

```
   execution mode:            serial
   quantum:                   Renode default
   comparisons:               60 pairs, 1 worker against N workers
   verdict mismatches:        0
   latency samples:           24
   X, max |delta|:            0 us
   X, median:                 0 us
   X, p99:                    0 us
   W, canonical window:       0 us
   trace stability:           IDENTICAL (observable behaviour)
```

**The strong version holds: byte-identical across worker counts, W = 0, no
canonicalisation needed.** The reworded criterion allowed a bounded residual and
there is none to report.

Host wall clock differed as expected, 116 s against 59 s, which is the evidence
that virtual time is what the answers depend on.

One deviation from the criterion as written: it asks for 10 workers, and this
host runs 3. At ~758 MB of emulator residency per worker against 5.7 GB, ten
concurrent batches are killed by the kernel; the host itself has 11.7 GB with
0.5 GB free, so raising the WSL allocation would starve Windows. The comparison
is still 1 worker against a fully saturated host, which is the condition the
criterion exists to test. On the 32-core, 64 GB machine the Phase 4 budget
assumes, ~28 workers fit and the constraint is cores rather than memory.

---

## Not started, deliberately

Per `PHASE0.md` §7, none of the following exist yet: the scenario language or its
compiler, any defect or patch script, any requirement, any model integration, the
grader, the sweep, the batching layer, System B, or anything in `analysis/`.

### System B is planned and gated, not started

`CHANGES-ADDENDUM.md` reinstates System B with reduced scope: same STM32H743
target, `fdcan2` and `usart2`, 250 kbit/s, 2 real nodes, 4 safety functions, 8
defects, 10 requirements, rungs 1 and 4 only. It is conditional on **Gate 4**,
evaluated at the end of Day 1 after Phase 1 and the System A sweep, and requires
all four of: Phase 1 exit criteria met including determinism and batching; all
System A binaries building with non-empty detection sets; the Gate 1 pilot gap
above 5 points; and elapsed time under 18 hours.

Building it now would violate `PHASE0.md` §7 and pre-empt a gate whose inputs do
not yet exist, so it has not been started.

---

## Documents referenced but not present in this repository

Two specification documents are cited by work in flight but were never supplied as
files, and neither could be reconstructed faithfully:

| Document | Status |
|---|---|
| `RESEARCH.md` | placeholder committed; the available copy was truncated inside §16 |
| `CHANGES.md` | absent; `CHANGES-ADDENDUM.md` explicitly supersedes its CHANGE 1 and leaves the rest standing, so the remainder cannot be applied |

`CHANGES.md` matters concretely: the addendum's arithmetic implies System A is now
**24 defects, not 36**, and a System A run budget of 8,488. Neither figure can be
acted on without the document that sets it. Phase 2 should not begin until it is
in the repository.
