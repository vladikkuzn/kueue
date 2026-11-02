---
name: kueue-pr-evidence-timeline
description: Render a base vs head scheduling timeline for a kueue PR from kueue-pr-evidence-state-diff captures (pending, preempt, requeue, admit, TAS placement) as tables, a Mermaid sequenceDiagram, and a text fallback, then publish it to "Useful notes for your reviewer" with inject-pr-notes, dry-run first. Use after state-diff, or when asked who preempted whom or where pods landed before and after a PR.
license: Apache-2.0
metadata:
  copyright: The Kubernetes Authors
---

# Skill: PR evidence scheduling timeline

Evidence skill of the `kueue-pr-evidence-*` family
([#18141](https://github.com/kubernetes-sigs/kueue/issues/18141)). It reads
the `pr-evidence.capture/v1` files that `kueue-pr-evidence-state-diff` writes
for the merge base and the PR head (format:
[capture-schema.md](../kueue-pr-evidence-common/capture-schema.md)), and
renders who was pending, who preempted whom, what was requeued and admitted,
and where TAS placed the pods. It runs no scenarios, tests, or clusters.

## When to use

- `kueue-pr-evidence-state-diff` wrote captures for a scheduling, preemption,
  fair sharing, or TAS PR.
- A reviewer asks "who preempted whom", "in which order", or "where did the
  pods land" before and after the PR.

For object state tables, use `kueue-pr-evidence-state-diff`. When no capture
exists yet, run that skill first.

## Steps

`PR_WORKTREE` is a checkout of the PR head. `OUT` is the directory that holds
the state-diff captures for this PR.

1. Resolve the SHAs:

   ```bash
   ROOT="$(git rev-parse --show-toplevel)/cmd/experimental/skills"
   C="$ROOT/kueue-pr-evidence-common/scripts"
   T="$ROOT/kueue-pr-evidence-timeline/scripts"
   eval "$("$C/merge-base.sh" --worktree "$PR_WORKTREE")"  # MERGE_BASE HEAD_SHA
   OUT="${XDG_CACHE_HOME:-$HOME/.cache}/kueue-pr-evidence/pr-<number>"
   ```

   This skill runs no code on the base side, so it needs no
   `kueue-worktree.sh` tree. The state-diff run already built one.

2. Render. `--capture-dir` finds every `capture.base.json` and
   `capture.head.json` pair in `$OUT` and up to two levels below it. The
   `--expect-*` flags reject captures from another merge base or head:

   ```bash
   "$T/render-timeline" --capture-dir "$OUT" \
     --expect-base "$MERGE_BASE" --expect-head "$HEAD_SHA" \
     --out "$OUT/kueue-pr-evidence-timeline.md"
   ```

   Use `--base-capture B.json --head-capture H.json` (repeatable) to pick
   pairs explicitly. When the script prints `no scheduling decisions in
   <scenario>; nothing to inject`, relay that line and stop. Exit code 2 means
   bad input; relay the message.

3. Preview, show the diff to the user, and wait for approval:

   ```bash
   "$C/inject-pr-notes" --pr <number|url> --skill kueue-pr-evidence-timeline \
     --file "$OUT/kueue-pr-evidence-timeline.md" \
     --base "$MERGE_BASE" --head "$HEAD_SHA" --dry-run
   ```

4. Publish with the same command without `--dry-run`, then reply with the PR
   URL.

## Reading the block

- **Verdict:** `Same scheduling decisions`, or `Scheduling decisions differ
  from <checkpoint>` with base → head counts, a side whose capture ended early
  (its assertion failed), and TAS podsets that moved.
- **Meta line:** scenario, runner, Kubernetes version, and feature gates from
  the capture.
- **Checkpoint table:** one row per checkpoint, aligned by name and ordered by
  checkpoint index and event `order`. `≠` marks a row that differs.
- **Preemptions:** `preemptor → preemptee`, the reason constant verbatim
  (`InClusterQueue`, `InCohortReclamation`, `InCohortFairSharing`,
  `InCohortReclaimWhileBorrowing`), priorities, and a count per side. `?`
  marks a preemptor inferred because that side has no `PreemptedWorkload`
  event.
- **TAS map:** pods per topology domain for each `workload/podset` admitted
  at the last checkpoint of each side. Finished workloads, such as a replaced
  elastic slice, and released admissions are left out.
- **Mermaid** `sequenceDiagram`: a checkpoint that matches on both sides is
  drawn once, and one that differs gets a `par base / and head` block. The
  **text timeline** lists every decision with its event ref (`e<order>`, or
  `state` when derived from snapshot changes).

The renderer moves sections into `<details>` until the block fits the
injector's 25-line budget. It fails instead of truncating.

## Rules

- Take both sides from state-diff captures. The renderer checks
  `run.producer`, `run.side`, and that `run.sha` matches the recorded SHA.
- Keep the rendered block as generated. To shorten it, render fewer scenarios
  per run.
- Keep Kueue reason constants verbatim, for example `InCohortReclamation`.
- Publish through `inject-pr-notes` only, dry-run first. For offline checks,
  use `inject-pr-notes --body-in <file> --dry-run`.

## Tests

Run from the repository root:

```bash
python3 -B -m unittest discover -s cmd/experimental/skills/kueue-pr-evidence-timeline/tests -p '*_test.py'
```

Refresh goldens with `UPDATE_GOLDEN=1` and review the diff. The fixtures are
hand-written cases modelled on #17034 (fair sharing preemption loop) and
#17515 (TAS hotswap of an LWS group), plus real state-diff captures (`*-real`)
from #17991, #13307, and #17460.
