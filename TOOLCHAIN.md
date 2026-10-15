# Toolchain

Every version here is pinned, and `scripts/setup.sh` verifies each one and fails
loudly on a mismatch rather than silently accepting a newer release.

Determinism is the foundation of the whole study: two runs of the same scenario
must produce identical verdicts and identical latencies, and that only holds if
the stack underneath them does not move. A version not recorded in this file is
not pinned.

## Pinned versions

| Component | Pinned | Where it came from | Why this version |
|---|---|---|---|
| Renode | `1.16.1` | [portable release tarball](https://github.com/renode/renode/releases/download/v1.16.1/renode-1.16.1.linux-portable.tar.gz), not a package manager | Peripheral model behaviour and the monitor command surface change between releases, and a changed peripheral model silently changes measured latencies. Every timing number in the paper is specific to this version. |
| Zephyr RTOS | `3.5.0` | `west init -m .../zephyr --mr v3.5.0` into `$HOME/zephyrproject` | CAN filter and driver semantics changed in later versions. Newer Zephyr's init also aborts on some emulated platforms with an obscure failure mode. Do not upgrade without a green boot check. |
| Zephyr SDK | `0.16.8` | [sdk-ng v0.16.8](https://github.com/zephyrproject-rtos/sdk-ng/releases/tag/v0.16.8), minimal tarball plus the `arm-zephyr-eabi` toolchain only | The SDK release matched to Zephyr 3.5.0. Provides `arm-zephyr-eabi-gcc 12.2.0`. |
| Python | `3.12.3` | Ubuntu 24.04 system interpreter, in a venv at `$HOME/detect-venv` | The distribution default. Pinned by the distribution, not by us. |
| CMake | `3.31.6` | pip wheel | Last 3.x. CMake 4 rejects `cmake_minimum_required` below 3.5, which several Zephyr modules still declare. |
| Ninja | `1.11.1.4` | pip wheel | Build determinism; no system package needed. |
| west | `1.2.0` | pip wheel | The west released alongside Zephyr 3.5.0. |
| Board | `nucleo_h743zi` | Zephyr board definition | STM32H743ZI, Cortex-M7. Console on `usart3`, CAN on `fdcan1`, both already wired in the board devicetree. |

## Where it installs

Everything is installed **rootless**, entirely under `$HOME`. Nothing requires
`sudo`, which matters here because the development host has no passwordless sudo
at all.

| Component | Location |
|---|---|
| Renode portable build | `$HOME/renode-1.16.1/` |
| Zephyr SDK | `$HOME/zephyr-sdk-0.16.8/` |
| Zephyr workspace (west) | `$HOME/zephyrproject/` |
| Python venv | `$HOME/detect-venv/` |
| Rootless host tools | `$HOME/detect-hosttools/` |
| Downloads cache | `$HOME/detect-downloads/` |
| Generated environment | `<repo>/.detect-env` (not committed) |
| Space-free build symlink | `$HOME/detect-master` -> the repository |

Renode's portable Linux tarball bundles its own runtime — it reports
`runtime: Mono 4.0.30319.42000` — so no system Mono or .NET package is needed and
none is looked for. The Zephyr SDK registers itself as a CMake package under
`$HOME/.cmake`, which is how `west build` finds `arm-zephyr-eabi` without touching
`/opt` or `/usr`.

## Host tools without root

Zephyr's documented host dependencies include `dtc` and `gperf`, normally
installed with `apt`. `apt install` needs root, which this project does not use
for toolchain components, so `scripts/setup.sh` instead downloads the `.deb`
archives with `apt-get download` (which runs unprivileged) and unpacks them into
`$HOME/detect-hosttools` with `dpkg-deb -x` (which also runs unprivileged).

Observed versions: `dtc 1.7.0`, `gperf 3.1`, plus `libfdt1` for dtc's shared
library.

**`dtc` turns out not to be required.** Zephyr parses devicetree with its own
Python `edtlib`; `dtc` is only an optional validator and the build skips it when
absent. It is installed anyway because its absence would otherwise be an
unexplained warning in every build log, and an unexplained warning is a thing
people learn to ignore.

## Environment pinning

`scripts/boot-check.sh` fixes several ambient inputs, because the exit criterion
is a byte-identical hash and a host-derived value anywhere on that path would
break it for reasons unrelated to the firmware:

| Variable | Value | Reason |
|---|---|---|
| `LC_ALL`, `LANG` | `C` | `grep`, `sort` and `printf` are locale sensitive |
| `TZ` | `UTC` | Renode's diagnostic log timestamps; makes two logs comparable |
| `SOURCE_DATE_EPOCH` | `1700000000` | consumed by gcc for `__DATE__`/`__TIME__` and by `ar`/`ld` for embedded timestamps |
| `PYTHONHASHSEED` | `0` | west and cmake helpers iterate dictionaries |
| `BUILD_VERSION` | `zephyr-v3.5.0` | see below |

`BUILD_VERSION` deserves its own note. Zephyr derives the boot banner's version
string from `git describe` run inside `ZEPHYR_BASE`
(`cmake/gen_version_h.cmake`), so the banner is a function of the *Zephyr
checkout's git state*, not of anything in this repository. A commit in that tree,
or a Zephyr unpacked from a tarball where `git describe` fails, moves the banner
and therefore the console hash. Passing `-DBUILD_VERSION` removes git from the
equation. This does not weaken version checking: `scripts/setup.sh` and
`scripts/boot-check.sh` both verify the tag and the `VERSION` file directly, which
is where a wrong Zephyr is actually caught.

## Platform notes

The reference environment is Linux x86-64. This host is Windows 11 running
**WSL2 Ubuntu 24.04.3 LTS**; the Zephyr SDK and Renode have no first-class native
Windows support for this workflow, and all scripts are POSIX shell.

### The repository path contains a space, and the toolchain cannot

The repository is the Windows folder `Downloads\benchmark paper`, reached from
WSL as `/mnt/c/Users/djtde/Downloads/benchmark paper`. **Zephyr cannot build from
a path containing a space.** Its devicetree preprocessing splits the path at the
space; measured directly:

```
-- Application: /mnt/c/Users/djtde/Downloads/benchmark paper/.../bms
<command-line>: fatal error: /mnt/c/Users/djtde/Downloads/benchmark:
                No such file or directory
```

Redirecting the build directory does not help. The failure is in handling the
*source* path, so it occurs wherever the output is written.

The resolution is a space-free symlink:

```
/home/djtde/detect-master  ->  /mnt/c/Users/djtde/Downloads/benchmark paper
```

CMake does not resolve a symlink back to its real path, so the toolchain sees
`/home/djtde/detect-master/...` and builds normally. Every script re-executes
itself through that symlink before doing any work, so this is automatic: run
`bash scripts/boot-check.sh` from the Windows folder and it handles itself. The
symlink is created on demand by any script and by `scripts/setup.sh`; it holds no
state and can be deleted at any time.

Verified after the move: the console hash is unchanged, both negative controls
still fire, and `build-firmware.sh` works when invoked through the spaced path.

### Cost of building on /mnt/c

Measured on this host, wall clock per `boot-check.sh`:

| | Time |
|---|---|
| clean build plus boot | 142 s |
| incremental plus boot | 59 s |
| `build/` deleted, rebuilt, boot | 122 s |

Roughly 1.5 to 2 times slower than the same work on the WSL filesystem, from the
9p bridge. Acceptable for Phases 0 to 2. For the Phase 4 sweep, whose cost is
dominated by thousands of emulator runs rather than by compilation, set
`BUILD_DIR` to a path under `$HOME` to keep the object tree on the Linux
filesystem; `scripts/build-firmware.sh` already honours it.

One consequence worth knowing: the ELF is **path dependent**, because the source
path is recorded in its debug information. Moving the repository changes the ELF
hash. It does not change the console hash, which is what the exit criterion
measures, and which was byte-identical before and after this move.

Ubuntu 24.04 ships **no `pip` and no `ensurepip`**. `scripts/setup.sh` creates the
venv with `--without-pip` and bootstraps pip from `get-pip.py`. Every script uses
the venv interpreter by explicit absolute path, because a silent fall back to the
system interpreter fails in a way that looks like a code bug.
