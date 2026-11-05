"""Calibration report (2024-12-13): kappa, confusion, per-class recall, the switch rule, gold rows judged."""
import csv
import pathlib
import tempfile
import unittest

from scripts import judge, judge_calibrate, judge_store
from scripts.sweep_common import CSV_COLUMNS


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()
        (self.root / "candidate-profile.md").write_text("# Profile\n", encoding="utf-8")

    def test_kappa_math(self):
        perfect = [[10, 0, 0], [0, 10, 0], [0, 0, 10]]
        self.assertEqual((judge_calibrate.kappa(perfect, True), judge_calibrate.kappa(perfect, False)), (1.0, 1.0))
        # one Apply called Maybe (adjacent) costs less under quadratic weights than under plain kappa
        near = [[10, 0, 0], [0, 10, 0], [0, 1, 9]]
        far = [[10, 0, 0], [0, 10, 0], [1, 0, 9]]
        self.assertGreater(judge_calibrate.kappa(near, True), judge_calibrate.kappa(far, True))
        self.assertEqual(judge_calibrate.kappa(near, False), judge_calibrate.kappa(far, False))
        self.assertIsNone(judge_calibrate.kappa([[0, 0, 0]] * 3, True))
        # everything in one class: chance agreement is total, kappa undefined
        self.assertIsNone(judge_calibrate.kappa([[10, 0, 0], [0, 0, 0], [0, 0, 0]], True))

    def seed(self, items):
        """items: [(company, gold_label, judge_label_or_None)]; writes gold rows and verdicts."""
        with judge_store.open_db(self.root) as conn:
            judge_store.upsert_gold(conn, [{"title": "IT Support Specialist", "company": c, "label": g} for c, g, _ in items], "form")
            run = judge.Run(self.root, "2024-12-13", "claude-sonnet-5-5", client=None)
            for company, _, label in items:
                if label is None:
                    continue
                row = {"job_key": judge_store.job_key({"title": "IT Support Specialist", "company": company}),
                       "title": "IT Support Specialist", "company": company, "job_url": f"https://j/{company}"}
                model = "gate" if label == "gate" else "claude-sonnet-5-5"
                verdict = {"label": "Skip" if label == "gate" else label, "score": 50, "confidence": "supported", "summary_line": f"line {company}"}
                run.store(conn, row, model, verdict)

    def test_report_joins_gold_with_the_latest_verdict_and_applies_the_switch_rule(self):
        items = [(f"A{i}", "Apply", "Apply") for i in range(4)] + [("A5", "Apply", "Maybe"), ("A6", "Apply", "Unscored")]
        items += [(f"S{i}", "Skip", "Skip") for i in range(5)] + [("S6", "Skip", "gate"), ("S7", "Skip", "Apply")]
        items += [("M1", "Maybe", "Maybe"), ("M2", "Maybe", None)]  # M2 has no verdict yet
        self.seed(items)
        with judge_store.open_db(self.root) as conn:
            result = judge_calibrate.report(conn, min_n=10, threshold=0.6)
        self.assertEqual((result["n"], result["unscored"]), (13, 1))
        self.assertEqual(result["matrix"], [[6, 0, 1], [0, 1, 0], [0, 1, 4]])  # rows Skip, Maybe, Apply
        self.assertEqual(result["classes"]["Apply"]["recall"], 0.8)
        self.assertEqual(result["classes"]["Skip"]["recall"], round(6 / 7, 3))
        self.assertEqual(sorted((m["gold"], m["judge"]) for m in result["misses"]), [("Apply", "Maybe"), ("Skip", "Apply")])
        # 2024-12-19: the gate's Skip (S6) is scored apart from the model's twelve labels, and READY reads the model pairs
        self.assertEqual((result["model"]["n"], result["gate"]), (12, {"n": 1, "agree": 1, "misses": []}))
        self.assertEqual(result["model"]["matrix"], [[5, 0, 1], [0, 1, 0], [0, 1, 4]])
        self.assertEqual(result["model"]["apply_recall"], 0.8)
        self.assertEqual([m["stage"] for m in result["misses"]], ["model", "model"])
        self.assertTrue(result["ready"])
        with judge_store.open_db(self.root) as conn:
            self.assertFalse(judge_calibrate.report(conn, min_n=150, threshold=0.6)["ready"])
            self.assertFalse(judge_calibrate.report(conn, min_n=10, threshold=0.6, min_apply_recall=0.9)["ready"])
        page = judge_calibrate.render(result, "2024-12-13")
        self.assertIn("Ready: the dashboard can switch", page)
        self.assertIn("line S7", page)
        self.assertIn("Model pairs", page)
        self.assertNotIn("~", page)

    def test_gate_agreement_alone_never_makes_the_judge_ready(self):
        # The first round: 50 of 86 pairs were gate Skips the owner agreed with, and the kappa mostly graded the gate.
        items = [(f"G{i}", "Skip", "gate") for i in range(40)] + [("A1", "Apply", "Apply"), ("S1", "Skip", "Skip")]
        self.seed(items)
        with judge_store.open_db(self.root) as conn:
            result = judge_calibrate.report(conn, min_n=10, threshold=0.6)
        self.assertEqual((result["n"], result["model"]["n"], result["gate"]["agree"]), (42, 2, 40))
        self.assertFalse(result["ready"])
        self.assertIn("Not yet: needs at least 10 model pairs", judge_calibrate.render(result, "2024-12-19"))

    def test_a_later_gate_or_unscored_row_never_hides_a_model_verdict(self):
        # 2024-12-20: the gate row is rewritten whenever the version key changes; newest-wins moved model pairs into gate pairs
        self.seed([("A1", "Apply", "Apply")])
        with judge_store.open_db(self.root) as conn:
            run = judge.Run(self.root, "2024-12-14", "claude-sonnet-5-5", client=None)
            row = {"job_key": judge_store.job_key({"title": "IT Support Specialist", "company": "A1"}),
                   "title": "IT Support Specialist", "company": "A1", "job_url": "https://j/A1"}
            run.store(conn, row, "gate", {"label": "Skip", "score": None, "confidence": "unknown", "summary_line": "gate"})
            run.prompt = dict(run.prompt, rubric_version="a-later-rubric")  # a new version key: the old model verdict stays
            run.store(conn, row, "claude-sonnet-5-5", {"label": "Unscored", "score": None, "confidence": "unknown", "summary_line": "x"})
            result = judge_calibrate.report(conn, min_n=1, threshold=0.6)
        self.assertEqual((result["model"]["n"], result["gate"]["n"], result["unscored"]), (1, 0, 0))

    def test_gold_rows_with_text_come_from_any_daily_csv(self):
        text = "Tier 2 support role with Intune and Jamf. " * 10
        rows = []
        for day, company in (("2024-12-12", "Old"), ("2024-12-13", "New")):
            base = {c: "" for c in CSV_COLUMNS}
            base.update(title="IT Support Specialist", company=company, description=text, job_url=f"https://j/{company}")
            with (self.root / "weekly-sweeps" / f"linkedin-raw-{day}.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerow(base)
            rows.append(base)
        with judge_store.open_db(self.root) as conn:
            judge_store.upsert_gold(conn, [{**rows[0], "label": "Apply"}], "form")
        picked = judge.gold_rows_with_text(self.root)
        self.assertEqual([r["company"] for r in picked], ["Old"])


if __name__ == "__main__":
    unittest.main()
