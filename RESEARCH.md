# RESEARCH.md IS NOT YET IN THIS REPOSITORY

**This file is a placeholder. It is not the research document. Do not treat any
sentence below as project design.**

## What is missing

`RESEARCH.md` is the single source of truth for project DETECT (DATE 2027, Track T,
topic T2). Section 3 of `PHASE0.md` lists it as `<- copy in, already written`, but the
file was not present in the working directory this phase was executed from, and the
copy available to the agent that created this skeleton was **truncated in transit**:
it was cut off inside section 16, "Paper outline", losing the remainder of section 16
and all of section 17, "Standing rules".

Writing a partial copy under the canonical filename was rejected deliberately. An
incomplete document silently occupying the position of the source of truth is exactly
the failure mode `PHASE0.md` section 6 forbids: a silent substitution that later reads
as a completed step.

## How to fix

Copy the authoritative file over this placeholder and commit it:

```
cp /path/to/RESEARCH.md ~/detect/RESEARCH.md
cd ~/detect && git add RESEARCH.md && git commit -m "docs: add RESEARCH.md, the source of truth"
```

The document should end with section 17, "Standing rules". If the copy pasted in ends
mid-sentence inside section 16, it is the truncated one.

## What Phase 0 did not depend on

Phase 0 has no technical dependency on this file. Its exit criterion is a reproducible
boot, and it was executed against `PHASE0.md`, which is present and complete. The
absence is recorded in `STATUS.md` under deviations.
