cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bms)

target_sources(app PRIVATE src/main.c)

# ---------------------------------------------------------------------------
# Defeat the linker.
#
# Zephyr compiles with -fdata-sections and links with --gc-sections, so every
# global lands in its own section and the linker discards any section no root
# reaches. `volatile` does not prevent this: volatile constrains the compiler,
# and the collection happens in the linker.
#
# A discarded injection target has no address. The harness would have nothing to
# write to, the fault would never be injected, the firmware would behave
# correctly for the input it actually received, and the campaign would report
# PASS. A false negative in a verification tool is the worst class of bug this
# project can have, so there are two independent defences and neither is trusted
# alone:
#
#   1. this file adds -Wl,--undefined=<symbol> per symbol, which makes the
#      linker treat the symbol as an undefined reference from the start and
#      therefore as a garbage-collection root;
#   2. scripts/check_symbols.py reads the produced ELF and fails the build if
#      any listed symbol is absent, has address zero, or has size zero.
#
# Both are driven from the same list, injectable_symbols.txt, so the two
# defences can never disagree about what they are protecting.
# ---------------------------------------------------------------------------

set(INJECTABLE_SYMBOLS_FILE "${CMAKE_CURRENT_SOURCE_DIR}/injectable_symbols.txt")

if(NOT EXISTS "${INJECTABLE_SYMBOLS_FILE}")
  message(FATAL_ERROR
    "injectable_symbols.txt is missing at ${INJECTABLE_SYMBOLS_FILE}.\n"
    "It is the single list that drives both the --undefined link flags and the\n"
    "post-link symbol gate. Building without it would produce a binary whose\n"
    "injection targets are unprotected and unchecked, so the build stops here.")
endif()

file(STRINGS "${INJECTABLE_SYMBOLS_FILE}" _raw_symbol_lines)

set(_kept_symbols "")
foreach(_line IN LISTS _raw_symbol_lines)
  string(STRIP "${_line}" _sym)
  if(_sym STREQUAL "" OR _sym MATCHES "^#")
    continue()
  endif()
  # The file carries "<symbol> <unit> <min> <max> [flags]". Only the first field
  # is the symbol name. Symbols flagged not-injectable are still force-kept and
  # still gated: the linker-collection negative control depends on that.
  string(REGEX REPLACE "^([^ \t]+).*$" "\\1" _sym "${_sym}")

  # DETECT_OMIT_UNDEFINED exists solely so the guard can be seen to fire. It
  # names one symbol whose --undefined flag is deliberately withheld, which for
  # the unreferenced canary means the linker collects it and the symbol gate
  # must fail. It is never set by a normal build.
  if(DEFINED DETECT_OMIT_UNDEFINED AND "${_sym}" STREQUAL "${DETECT_OMIT_UNDEFINED}")
    message(WARNING
      "DETECT: deliberately omitting -Wl,--undefined=${_sym}. "
      "This build is the negative control for the symbol gate and must fail it.")
  else()
    zephyr_link_libraries(-Wl,--undefined=${_sym})
    list(APPEND _kept_symbols "${_sym}")
  endif()
endforeach()

if(_kept_symbols STREQUAL "")
  message(FATAL_ERROR
    "injectable_symbols.txt yielded no symbols. Refusing to link a binary with\n"
    "no protected injection targets rather than silently producing one.")
endif()

message(STATUS "DETECT: forcing retention of: ${_kept_symbols}")
