#!/usr/bin/env python3
"""Core-logic tests for both walkthrough scripts. Python 3 stdlib, no deps.

    python3 -m unittest discover -s tests

Covers the parts that fail silently: what makes a beat shippable, what order
the report puts beats in, how a unified diff maps to anchorable lines, and
what happens to an anchor that does not land on one.
"""
import importlib.util
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "walkthrough" / "scripts"


def load(stem):
    """Import a script whose filename is not a legal module name."""
    spec = importlib.util.spec_from_file_location(
        stem.replace("-", "_"), SCRIPTS / (stem + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rr = load("render-report")
va = load("validate-anchors")


def beat(**kw):
    """A shippable clean beat, overridden by kw."""
    out = {
        "n": 1,
        "tier": "core",
        "state": "clean",
        "claim": "does what it says",
        "where": "a.ts:1",
        "slots": {"what": "adds a thing", "proof": "a.ts:1"},
    }
    out.update(kw)
    return out


class BeatValidation(unittest.TestCase):
    def test_clean_beat_with_proof_is_shippable(self):
        self.assertEqual(rr.validate(beat()), [])

    def test_clean_beat_without_proof_is_not(self):
        problems = rr.validate(beat(slots={"what": "adds a thing"}))
        self.assertIn("clean with no proof", problems[0])

    def test_accepted_beat_also_needs_proof(self):
        problems = rr.validate(beat(state="accepted", slots={"what": "x"}))
        self.assertIn("accepted with no proof", problems[0])

    def test_every_beat_needs_a_what(self):
        problems = rr.validate(beat(slots={"proof": "a.ts:1"}))
        self.assertIn("no what", problems[0])

    def test_flag_needs_risk_and_fix(self):
        problems = rr.validate(beat(state="flag", slots={"what": "x", "proof": "a.ts:1"}))
        self.assertEqual(len(problems), 2)
        self.assertIn("flag with no risk", problems[0])
        self.assertIn("flag with no fix", problems[1])

    def test_flag_with_risk_and_fix_is_shippable(self):
        self.assertEqual(
            rr.validate(
                beat(
                    state="flag",
                    slots={
                        "what": "x",
                        "proof": "`npm view pkg versions`",
                        "risk": "reddens a clean PR",
                        "fix": "pin it",
                    },
                )
            ),
            [],
        )

    def test_unknown_slot_is_rejected(self):
        problems = rr.validate(beat(slots={"what": "x", "proof": "a.ts:1", "notes": "y"}))
        self.assertIn("unknown slot 'notes'", problems[0])

    def test_unknown_state_is_rejected(self):
        problems = rr.validate(beat(state="probably-fine"))
        self.assertIn("is not one of", problems[0])

    def test_states_other_than_clean_and_accepted_need_no_proof(self):
        for state in ("flag", "unverified", "dropped"):
            slots = {"what": "x"}
            if state == "flag":
                slots.update(risk="r", fix="f")
            with self.subTest(state=state):
                self.assertEqual(rr.validate(beat(state=state, slots=slots)), [])


class ProofEvidence(unittest.TestCase):
    """PROOF has to name something a reader can re-run or open."""

    def proof(self, value):
        return rr.validate(beat(slots={"what": "x", "proof": value}))

    def test_backticked_command_counts(self):
        self.assertEqual(self.proof("`npm view @scope/pkg versions` shows every major is 0"), [])

    def test_path_and_line_counts(self):
        self.assertEqual(self.proof("dist/index.js:1426 defaults it true"), [])

    def test_bare_prose_does_not_count(self):
        self.assertIn("names no command", self.proof("looked at it and it seemed fine")[0])

    def test_inferred_is_a_legal_value(self):
        """README and SKILL.md both declare `inferred` legal and honest."""
        self.assertEqual(self.proof("inferred"), [])

    def test_inferred_with_a_reason_is_legal(self):
        self.assertEqual(self.proof("inferred from the surrounding call sites"), [])


class ReportOrdering(unittest.TestCase):
    """The page is ordered by what is owed, not by beat number."""

    def render(self, beats):
        return rr.render({"repo": "acme/widget", "number": 42}, beats, "", {})

    def test_sections_run_flags_then_accepted_then_clean_then_dropped(self):
        html = self.render(
            [
                beat(n=1, state="dropped"),
                beat(n=2, state="clean"),
                beat(n=3, state="accepted"),
                beat(n=4, state="flag", slots={"what": "x", "risk": "r", "fix": "f"}),
            ]
        )
        order = [
            html.index("Needs your call"),
            html.index("Accepted"),
            html.index("Walked and clean"),
            html.index("Dropped"),
        ]
        self.assertEqual(order, sorted(order))

    def test_empty_sections_are_omitted(self):
        html = self.render([beat(n=1, state="clean")])
        self.assertIn("Walked and clean", html)
        self.assertNotIn("Needs your call", html)
        self.assertNotIn("Dropped", html)

    def test_flags_open_expanded_and_clean_beats_collapsed(self):
        # Matched loosely on purpose: attributes get added to these elements as
        # the page grows, and that must not read as a behavior change.
        flag = self.render([beat(n=1, state="flag", slots={"what": "x", "risk": "r", "fix": "f"})])
        clean = self.render([beat(n=1, state="clean")])
        self.assertRegex(flag, r'<details class="beat s-flag"[^>]*\sopen[\s>]')
        self.assertNotRegex(clean, r'<details class="beat s-clean"[^>]*\sopen[\s>]')

    def test_unverified_beats_count_as_clean_in_the_tiles(self):
        html = self.render([beat(n=1, state="clean"), beat(n=2, state="unverified")])
        self.assertRegex(html, r'<div class="count is-clean"[^>]*>\s*<span class="n">2</span>')

    def test_a_failing_beat_carries_the_unproven_chip(self):
        html = rr.render({"repo": "r"}, [beat(n=1)], "", {1: ["beat 1: no what"]})
        self.assertIn("unproven", html)


class Escaping(unittest.TestCase):
    """Beat content is author-controlled but quotes code from the PR under review."""

    def test_markup_in_content_is_escaped(self):
        self.assertEqual(rr.md("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_backticks_become_code_after_escaping(self):
        self.assertEqual(rr.md("use `<T>` here"), "use <code>&lt;T&gt;</code> here")

    def test_ampersands_are_escaped(self):
        self.assertEqual(rr.md("a && b"), "a &amp;&amp; b")


class DiffParsing(unittest.TestCase):
    def test_maps_added_and_context_lines_to_sides(self):
        files = va.parse_diff(
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -10,3 +10,4 @@\n"
            " ctx\n"
            "-gone\n"
            "+new\n"
            "+also new\n"
        )
        self.assertEqual(files["x.py"]["RIGHT"], {10, 11, 12})
        self.assertEqual(files["x.py"]["LEFT"], {10, 11})

    def test_strips_the_b_prefix(self):
        files = va.parse_diff("+++ b/src/a.go\n@@ -0,0 +1 @@\n+x\n")
        self.assertIn("src/a.go", files)

    def test_deleted_files_are_skipped(self):
        files = va.parse_diff("--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        self.assertEqual(files, {})

    def test_hunk_header_without_counts_means_one_line(self):
        files = va.parse_diff("+++ b/x.py\n@@ -5 +5 @@\n-was\n+only\n")
        self.assertEqual(files["x.py"]["RIGHT"], {5})
        self.assertEqual(files["x.py"]["LEFT"], {5})

    def test_an_added_line_that_looks_like_a_header_is_not_one(self):
        """Counting by the header's totals is what keeps '++ x' from resyncing."""
        files = va.parse_diff("+++ b/x.py\n@@ -1,0 +1,2 @@\n+++ not a header\n+second\n")
        self.assertEqual(files["x.py"]["RIGHT"], {1, 2})

    def test_no_newline_marker_is_ignored(self):
        files = va.parse_diff("+++ b/x.py\n@@ -1 +1 @@\n+x\n\\ No newline at end of file\n")
        self.assertEqual(files["x.py"]["RIGHT"], {1})

    def test_several_files_stay_separate(self):
        files = va.parse_diff(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+a\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -0,0 +7 @@\n+b\n"
        )
        self.assertEqual(files["a.py"]["RIGHT"], {1})
        self.assertEqual(files["b.py"]["RIGHT"], {7})


class Snapping(unittest.TestCase):
    def test_nearest_valid_line_wins(self):
        self.assertEqual(va.snap(10, {3, 8, 20}), 8)

    def test_ties_prefer_the_earlier_line(self):
        self.assertEqual(va.snap(10, {8, 12}), 8)

    def test_beyond_the_window_returns_nothing(self):
        self.assertIsNone(va.snap(100, {1, 2}))

    def test_exactly_at_the_window_edge_still_snaps(self):
        self.assertEqual(va.snap(100, {100 - va.SNAP_WINDOW}), 100 - va.SNAP_WINDOW)

    def test_no_valid_lines_returns_nothing(self):
        self.assertIsNone(va.snap(10, set()))


class AnchorValidation(unittest.TestCase):
    DIFF = "+++ b/x.py\n@@ -10,2 +10,3 @@\n ctx\n+a\n+b\n"

    def run_on(self, comments, body="head"):
        payload = {"body": body, "event": "COMMENT", "comments": comments}
        report = va.validate(payload, va.parse_diff(self.DIFF))
        return payload, report

    def test_a_valid_anchor_is_left_alone(self):
        payload, report = self.run_on([{"path": "x.py", "line": 11, "side": "RIGHT", "body": "ok"}])
        self.assertEqual(report, [])
        self.assertEqual(payload["comments"][0]["line"], 11)

    def test_a_near_miss_is_snapped(self):
        payload, report = self.run_on([{"path": "x.py", "line": 15, "side": "RIGHT", "body": "ok"}])
        self.assertEqual(payload["comments"][0]["line"], 12)
        self.assertIn("snap", report[0])

    def test_a_file_outside_the_diff_is_folded_into_the_body(self):
        payload, report = self.run_on([{"path": "other.py", "line": 3, "side": "RIGHT", "body": "note"}])
        self.assertEqual(payload["comments"], [])
        self.assertIn("note", payload["body"])
        self.assertIn("other.py", payload["body"])
        self.assertIn("fold", report[0])

    def test_folding_keeps_the_original_body(self):
        payload, _ = self.run_on(
            [{"path": "other.py", "line": 3, "side": "RIGHT", "body": "note"}], body="original"
        )
        self.assertTrue(payload["body"].startswith("original"))

    def test_an_unsnappable_line_in_a_known_file_is_folded(self):
        payload, report = self.run_on([{"path": "x.py", "line": 900, "side": "RIGHT", "body": "far"}])
        self.assertEqual(payload["comments"], [])
        self.assertIn("no hunk within", report[0])

    def test_a_stale_start_line_is_dropped_when_the_anchor_moves(self):
        payload, _ = self.run_on(
            [{"path": "x.py", "line": 15, "side": "RIGHT", "start_line": 14, "start_side": "RIGHT", "body": "r"}]
        )
        comment = payload["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertNotIn("start_side", comment)

    def test_side_is_respected(self):
        payload, _ = self.run_on([{"path": "x.py", "line": 10, "side": "LEFT", "body": "ok"}])
        self.assertEqual(payload["comments"][0]["line"], 10)

    def test_a_non_integer_line_is_folded_rather_than_crashing(self):
        payload, report = self.run_on([{"path": "x.py", "line": None, "side": "RIGHT", "body": "n"}])
        self.assertEqual(payload["comments"], [])
        self.assertIn("fold", report[0])


if __name__ == "__main__":
    unittest.main()
