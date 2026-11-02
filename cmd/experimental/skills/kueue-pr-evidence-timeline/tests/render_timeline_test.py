#!/usr/bin/env python3

# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline tests for scripts/render-timeline (no network, no cluster).

Run from the repository root:
    python3 -B -m unittest discover -s cmd/experimental/skills/kueue-pr-evidence-timeline/tests -p '*_test.py'
Refresh goldens after an intended output change: UPDATE_GOLDEN=1 (then review the diff).
"""

from __future__ import annotations

import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "render-timeline")
INJECTOR = os.path.join(SKILL_DIR, "..", "kueue-pr-evidence-common", "scripts", "inject-pr-notes")
FIXTURES = os.path.join(HERE, "fixtures")
PR_BODY = os.path.join(FIXTURES, "pr-body.txt")
SCENARIOS = ("preemption-in-cq", "fair-sharing-partial-borrowing", "tas-lws-group-hotswap",
             "priority-boost-real", "cohort-reclaim-real", "tas-elastic-replacement-real")


def load_module():
    loader = importlib.machinery.SourceFileLoader("render_timeline", SCRIPT)
    spec = importlib.util.spec_from_loader("render_timeline", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_timeline"] = mod  # dataclasses resolve via sys.modules
    loader.exec_module(mod)
    return mod


rt = load_module()


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = rt.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def render(scenario: str) -> str:
    rc, out, err = run_main("--capture-dir", os.path.join(FIXTURES, scenario))
    assert rc == 0, err
    return out


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def mermaid_blocks(md: str) -> list[list[str]]:
    return [b.split("\n") for b in re.findall(r"```mermaid\n(.*?)\n```", md, re.S)]


def check_mermaid(lines: list[str]) -> list[str]:
    """Structural sequenceDiagram check (mmdc is the authoritative validator)."""
    errs = []
    if not lines or lines[0] != "sequenceDiagram":
        errs.append("first line must be sequenceDiagram")
    declared, depth = set(), 0
    arrow = re.compile(r"^\s*(\w+)(->>|-->>|-x|--\)|-\))(\w+): (.+)$")
    for ln in lines[1:]:
        s = ln.strip()
        if m := re.match(r"^participant (\w+) as (.+)$", s):
            declared.add(m.group(1))
        elif s.startswith("par "):
            depth += 1
        elif s.startswith("and "):
            errs += [] if depth else ["'and' outside par"]
        elif s == "end":
            depth -= 1
        elif m := re.match(r"^Note over (\w+)(?:,(\w+))?: (.+)$", s):
            errs += [f"undeclared {p}" for p in m.groups()[:2] if p and p not in declared]
        elif m := arrow.match(s):
            errs += [f"undeclared {p}" for p in (m.group(1), m.group(3)) if p not in declared]
            if re.search(r"[;#{}<>\"`]|%%", m.group(4)):
                errs.append(f"unsafe message text: {s}")
        else:
            errs.append(f"unknown statement: {s}")
    if depth:
        errs.append("unbalanced par/end")
    return errs


class GoldenTest(unittest.TestCase):
    def test_fixtures_match_golden(self):
        for sc in SCENARIOS:
            with self.subTest(scenario=sc):
                got = render(sc)
                path = os.path.join(FIXTURES, sc, "expected.txt")
                if os.environ.get("UPDATE_GOLDEN"):
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(got)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(got, fh.read())

    def test_budget_and_mermaid(self):
        for sc in SCENARIOS:
            with self.subTest(scenario=sc):
                md = render(sc)
                self.assertLessEqual(rt.visible_lines(md) + 1, 25)
                blocks = mermaid_blocks(md)
                self.assertEqual(len(blocks), 1)
                self.assertEqual(check_mermaid(blocks[0]), [])
                self.assertIn("| # | checkpoint | base | head |", md)  # raw table next to Mermaid
                self.assertIn("```text", md)                           # text fallback
                self.assertIn("runner **envtest**", md)
                self.assertNotIn("pr-evidence:", md)                   # never writes markers


class ScenarioTest(unittest.TestCase):
    def test_preemption_same_with_inferred_preemptor_on_base(self):
        md = render("preemption-in-cq")
        self.assertTrue(md.startswith("**Same scheduling decisions**"))
        self.assertIn("| high? → low | InClusterQueue | 1000 > 100 | 1 | 1 |", md)
        self.assertIn("pending high · preempt high?→low (InClusterQueue) · requeue low · admit high | same |", md)
        self.assertIn("preemptor inferred: pending event awaiting preemption", md)

    def test_fair_sharing_loop_differs(self):
        md = render("fair-sharing-partial-borrowing")
        first = md.split("\n")[0]
        self.assertIn("differ** from `preemptor-pending`", first)
        self.assertIn("preemptions 1 → 0", first)
        self.assertIn("base capture ends after `preemptor-pending`", first)
        self.assertIn("gates `FairSharingPreemptWithinNominal=true`", md)
        self.assertIn("likely preemption loop", md)
        self.assertIn("| preemptor → borrower | InCohortReclamation | 0 vs 0 | 1 | 0 |", md)
        self.assertIn("| 3 | `borrower-not-reclaimed` ≠ | — (capture ended) | — |", md)

    def test_tas_map(self):
        md = render("tas-lws-group-hotswap")
        self.assertIn("TAS placement differs for lws/leader, lws/worker", md)
        self.assertIn("pods per `kubernetes.io/hostname` domain", md)
        self.assertIn("| lws/leader | x3 | · | 1 ≠ |", md)
        self.assertIn("|  | x6 | 1 | · ≠ |", md)
        self.assertIn("| lws/worker | x2 | 1 | 1 |", md)
        self.assertIn("re-place lws: leader x1→x6, worker x1→x3 | re-place lws: leader x1→x3, worker x1→x4", md)

    def test_real_producer_capture_requeue_after_finish_eviction(self):
        # Real kueue-pr-evidence-state-diff output (kueue#17991): newer trees emit
        # WaitingForQuota/WaitingForPreemptedWorkloads, and finishEviction clears
        # the admission one checkpoint after the eviction event. That is the
        # requeue, not a second (state-derived) preemption.
        md = render("priority-boost-real")
        first = md.split("\n")[0]
        self.assertIn("preemptions 0 → 1, requeues 0 → 1", first)
        self.assertIn("| wl-pending → wl-admitted | InClusterQueue | 0 > -100 (boosted) | 0 | 1 |", md)
        self.assertNotIn("? → wl-admitted", md)
        self.assertIn("requeue wl-admitted · pending wl-admitted", md)
        self.assertIn("pending  wl-pending  no quota: cpu, awaits preemption", md)

    def test_real_cohort_reclaim_requeue_precedes_readmission(self):
        # Real kueue-pr-evidence-state-diff output (kueue#13307): the base merge base
        # never reclaims (its capture ends at the failed wait), head preempts the
        # borrower. The borrower is evicted in one checkpoint and re-admitted on
        # another flavor two checkpoints later without ever being un-admitted
        # between snapshots; its requeue goes before that re-admission.
        md = render("cohort-reclaim-real")
        first = md.split("\n")[0]
        self.assertIn("differ** from `preemptor-created`", first)
        self.assertIn("preemptions 0 → 1, requeues 0 → 1", first)
        self.assertIn("base capture ends after `preemptor-created`", first)
        self.assertIn("| wl-a → wl-b | InCohortReclamation | 0 vs 0 | 0 | 1 |", md)
        self.assertIn("| — (capture ended) | admit wl-a · requeue wl-b · pending wl-b · admit wl-b |", md)
        self.assertIn("[/all/cohort-a/a → /all/b]", md)

    def test_real_tas_elastic_replacement_live_map(self):
        # Real kueue-pr-evidence-state-diff output (kueue#17460): the base merge base
        # double-counts the predecessor slice, so the replacement never fits.
        # The TAS map shows only pods live at each side's last checkpoint: the
        # replaced slice (Finished) and the released victim drop off on head.
        md = render("tas-elastic-replacement-real")
        self.assertIn("preemptions 0 → 1", md.split("\n")[0])
        self.assertIn("| replacement → victim | InClusterQueue | 10 > 1 | 0 | 1 |", md)
        self.assertIn("| original/workers | elastic-accounting-node | 2 | · ≠ |", md)
        self.assertIn("| replacement/workers | elastic-accounting-node | · | 4 ≠ |", md)
        self.assertIn("| victim/workers | elastic-accounting-node | 2 | · ≠ |", md)
        self.assertIn("pending  replacement  TAS: no fit for 2 pods (excluded resource cpu: 1)", md)
        self.assertIn("finish   original  slice replaced", md)
        self.assertNotIn("event    original", md)


class OrderingTest(unittest.TestCase):
    def test_order_field_not_list_position(self):
        src = os.path.join(FIXTURES, "fair-sharing-partial-borrowing")
        want = render("fair-sharing-partial-borrowing")
        rnd = random.Random(7)
        with tempfile.TemporaryDirectory() as tmp:
            for side in ("base", "head"):
                doc = read_json(os.path.join(src, f"capture.{side}.json"))
                rnd.shuffle(doc["checkpoints"])
                for cp in doc["checkpoints"]:
                    rnd.shuffle(cp["events"])
                    rnd.shuffle(cp["workloads"])
                with open(os.path.join(tmp, f"capture.{side}.json"), "w") as fh:
                    json.dump(doc, fh)
            rc, got, err = run_main("--capture-dir", tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(got, want)


def write_pair(tmp: str, base: dict, head: dict) -> None:
    for side, doc in (("base", base), ("head", head)):
        with open(os.path.join(tmp, f"capture.{side}.json"), "w") as fh:
            json.dump(doc, fh)


def fixture_pair(scenario: str) -> tuple[dict, dict]:
    d = os.path.join(FIXTURES, scenario)
    return read_json(os.path.join(d, "capture.base.json")), read_json(os.path.join(d, "capture.head.json"))


class SkipAndValidationTest(unittest.TestCase):
    def test_skip_when_no_decisions(self):
        base, head = fixture_pair("preemption-in-cq")
        for doc in (base, head):
            for cp in doc["checkpoints"]:
                cp["events"] = []
                cp["workloads"] = []
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, base, head)
            out_file = os.path.join(tmp, "out", "kueue-pr-evidence-timeline.md")
            os.makedirs(os.path.dirname(out_file))
            with open(out_file, "w") as fh:
                fh.write("stale")
            rc, out, _ = run_main("--capture-dir", tmp, "--out", out_file)
            self.assertEqual(rc, 0)
            self.assertEqual(out.count("\n"), 1)
            self.assertIn("no scheduling decisions in preemption-in-cq; nothing to inject", out)
            self.assertFalse(os.path.exists(out_file))

    def test_skip_status_only_scenario(self):
        rc, out, _ = run_main("--capture-dir", os.path.join(FIXTURES, "no-decisions"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "kueue-pr-evidence-timeline: no scheduling decisions in cq-status-only; "
                              "nothing to inject\n")

    def test_capture_source_required(self):
        rc, _, err = run_main()
        self.assertEqual(rc, 2)
        self.assertIn("--capture-dir", err)

    def assertRejected(self, mutate, needle: str):
        base, head = fixture_pair("preemption-in-cq")
        mutate(base, head)
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, base, head)
            rc, _, err = run_main("--capture-dir", tmp)
        self.assertEqual(rc, 2)
        self.assertIn(needle, err)

    def test_rejects_hand_written_base(self):
        self.assertRejected(lambda b, h: b["run"].update(producer="me"), "never hand-written")

    def test_rejects_base_not_from_merge_base(self):
        self.assertRejected(lambda b, h: b["run"].update(sha=b["run"]["headSha"]), "run.sha")

    def test_rejects_swapped_sides(self):
        self.assertRejected(lambda b, h: b["run"].update(side="head"), "run.side")

    def test_rejects_scenario_mismatch(self):
        self.assertRejected(lambda b, h: h["run"].update(scenario="other"), "scenario mismatch")

    def test_rejects_schema(self):
        self.assertRejected(lambda b, h: h.update(schemaVersion="v0"), "schemaVersion")

    def test_expect_shas(self):
        d = os.path.join(FIXTURES, "fair-sharing-partial-borrowing")
        rc, _, err = run_main("--capture-dir", d, "--expect-head", "deadbeef")
        self.assertEqual(rc, 2)
        self.assertIn("stale captures", err)
        rc, out, _ = run_main("--capture-dir", d, "--expect-base", "a0394b0b0570", "--print-shas")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "MERGE_BASE=a0394b0b05708c76afae7e8d56f5c1583b23f58a\n"
                              "HEAD_SHA=0925c3d8d531f77ac9a0ea562179e052b7722d46\n")

    def test_explicit_pair_flags(self):
        d = os.path.join(FIXTURES, "tas-lws-group-hotswap")
        rc, out, _ = run_main("--base-capture", os.path.join(d, "capture.base.json"),
                              "--head-capture", os.path.join(d, "capture.head.json"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, render("tas-lws-group-hotswap"))


class RobustnessTest(unittest.TestCase):
    def test_optional_event_count_and_explicit_preemptor(self):
        base, head = fixture_pair("fair-sharing-partial-borrowing")
        for ev in base["checkpoints"][2]["events"]:
            if ev["reason"] == "Preempted":
                ev["count"] = 3  # not in capture v1; honoured when a producer adds it
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, base, head)
            rc, out, _ = run_main("--capture-dir", tmp)
        self.assertEqual(rc, 0)
        self.assertIn("preemptions 3 → 0", out)
        self.assertIn("| preemptor → borrower | InCohortReclamation | 0 vs 0 | 3 | 0 |", out)

    def test_overflow_collapses_into_details(self):
        base, head = fixture_pair("preemption-in-cq")
        for doc in (base, head):
            cp1 = doc["checkpoints"][1]
            for i in range(2, 30):
                cp = copy.deepcopy(cp1)
                cp["index"], cp["name"], cp["events"] = i, f"extra-{i}", []
                doc["checkpoints"].append(cp)
        head["checkpoints"][-1]["events"] = [{
            "order": 99, "type": "Normal", "reason": "EvictedDueToPodsReadyTimeout",
            "involvedObject": {"kind": "Workload", "namespace": "default", "name": "high"},
            "message": "Exceeded the PodsReady timeout default/high"}]
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, base, head)
            rc, out, err = run_main("--capture-dir", tmp)
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(rt.visible_lines(out) + 1, 25)
        self.assertIn("<details><summary>Checkpoint table</summary>", out)
        self.assertIn("evictions 0 → 1", out)
        self.assertIn("evict high (PodsReadyTimeout)", out)

    def test_multiple_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sc in ("preemption-in-cq", "tas-lws-group-hotswap"):
                base, head = fixture_pair(sc)
                for doc in (base, head):  # one PR → one SHA pair
                    doc["run"].update(baseSha="1" * 40, headSha="2" * 40)
                base["run"]["sha"], head["run"]["sha"] = "1" * 40, "2" * 40
                os.makedirs(os.path.join(tmp, sc))
                write_pair(os.path.join(tmp, sc), base, head)
            rc, out, err = run_main("--capture-dir", tmp)
        self.assertEqual(rc, 0, err)
        self.assertIn("`preemption-in-cq`: **Same scheduling decisions**", out)
        self.assertIn("`tas-lws-group-hotswap`: **Scheduling decisions differ**", out)
        self.assertLessEqual(rt.visible_lines(out) + 1, 25)


class InjectorTest(unittest.TestCase):
    def test_dry_run_injection_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = os.path.join(tmp, "kueue-pr-evidence-timeline.md")
            rc, _, err = run_main("--capture-dir", os.path.join(FIXTURES, "fair-sharing-partial-borrowing"),
                                  "--out", block)
            self.assertEqual(rc, 0, err)
            p = subprocess.run(
                [sys.executable, "-B", INJECTOR, "--skill", "kueue-pr-evidence-timeline", "--file", block,
                 "--base", "a0394b0b05708c76afae7e8d56f5c1583b23f58a",
                 "--head", "0925c3d8d531f77ac9a0ea562179e052b7722d46",
                 "--body-in", PR_BODY, "--dry-run"],
                capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("+<!-- pr-evidence:kueue-pr-evidence-timeline:start -->", p.stdout)
        self.assertIn("+**kueue-pr-evidence-timeline** base a0394b0b0570 -> head 0925c3d8d531", p.stdout)
        self.assertNotIn("-STATE DIFF", p.stdout)  # state-diff block untouched


if __name__ == "__main__":
    unittest.main()
