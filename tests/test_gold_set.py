"""Gold set (2024-12-13): the verdict store, the stratified labelling form and the seed import."""
import csv
import json
import pathlib
import tempfile
import unittest

from scripts import gold_form, gold_import, judge_store
from scripts.sweep_common import CSV_COLUMNS

TEXT = "Tier 2 desktop support for a 400-seat office — Intune, Jamf, Okta, ~2 yrs Active Directory. " * 5


def row(title, company, *, location="Raleigh, NC", high="90000", description=TEXT, remote="false", url=""):
    base = {c: "" for c in CSV_COLUMNS}
    base.update(title=title, company=company, location=location, max_amount=high, pay_interval="yearly" if high else "",
                description=description, is_remote=remote, site="indeed", job_url=url or f"https://x/{company}/{title}")
    return base


class GoldSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()

    def pull(self, day, rows):
        with (self.root / "weekly-sweeps" / f"linkedin-raw-{day}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def test_gate_names_what_failed(self):
        self.assertTrue(judge_store.passes_gate(judge_store.gate(row("IT Support Specialist", "A"))))
        low = judge_store.gate(row("IT Support Specialist", "A", high="52000"))
        self.assertEqual((low["pay"], judge_store.near_miss(low)), (False, True))
        hourly = judge_store.gate({**row("IT Support Specialist", "A", high="40"), "pay_interval": "hourly"})
        self.assertTrue(hourly["pay"])  # $40/h is $83,200
        annualized = judge_store.gate({**row("IT Support Specialist", "A", high="62400"), "pay_interval": "hourly"})
        self.assertFalse(annualized["pay"])  # the sweeps already wrote $30/h as 62400; it must not be multiplied again
        elsewhere = judge_store.gate(row("IT Support Specialist", "A", location="Denver, CO"))
        self.assertFalse(elsewhere["place"])
        # 2024-12-19, one place rule: a remote flag with a city is not evidence; the flag counts with a country-only location
        self.assertFalse(judge_store.gate(row("IT Support Specialist", "A", location="Denver, CO", remote="true"))["place"])
        self.assertTrue(judge_store.gate(row("IT Support Specialist", "A", location="United States", remote="true"))["place"])
        self.assertTrue(judge_store.gate(row("IT Support Specialist", "A", location="Remote (Denver, CO)"))["place"])
        self.assertFalse(judge_store.gate(row("Senior Director of IT", "A"))["title"])
        self.assertFalse(judge_store.gate(row("IT Support Specialist", "A", description="short"))["text"])
        two = judge_store.gate(row("Plumber", "A", high="30000"))
        self.assertFalse(judge_store.near_miss(two))  # two failures is not a near-miss

    def test_pay_written_only_in_the_text_still_gates(self):
        # 2024-12-13 pilot: an MSP posting with blank pay fields and "$20 to $30/hr" in the text reached the model.
        self.assertEqual(judge_store.pay_from_text("Compensation: $20 to $30/hr depending on experience"), 62400)
        self.assertEqual(judge_store.pay_from_text("Salary $55,000 - $62,000 per year"), 62000)
        self.assertEqual(judge_store.pay_from_text("up to $85k annually"), 85000)
        self.assertEqual(judge_store.pay_from_text("pay $28 an hour"), 28 * 2080)
        self.assertIsNone(judge_store.pay_from_text("We offer a $500 signing bonus and a $50 phone stipend"))
        self.assertIsNone(judge_store.pay_from_text("Competitive pay"))
        low = judge_store.gate(row("IT Support Specialist", "A", high="", description="Great team. $20 to $30/hr. " + TEXT))
        self.assertFalse(low["pay"])
        fine = judge_store.gate(row("IT Support Specialist", "A", high="", description="Great team. $30 to $40/hr. " + TEXT))
        self.assertTrue(fine["pay"])
        self.assertTrue(judge_store.gate(row("IT Support Specialist", "A", high="90000", description="$20/hr " + TEXT))["pay"])  # CSV fields win


    def test_form_offers_text_rows_only_once_in_the_40_30_30_mix_and_skips_labelled_ones(self):
        passes = [row(f"IT Support Specialist {i}", f"Pass {i}") for i in range(10)]
        near = [row(f"IT Support Specialist {i}", f"Near {i}", high="50000") for i in range(10)]
        other = [row(f"Plumber {i}", f"Other {i}", high="30000") for i in range(10)]
        blank = [row("IT Support Specialist blank", "Blank", description="")]
        self.pull("2024-12-13", passes + near + other + blank)
        self.pull("2024-12-12", passes[:3])  # the same roles again: deduped by role key
        rows = gold_form.candidate_rows(self.root)
        self.assertEqual(len(rows), 30)
        with judge_store.open_db(self.root) as conn:
            judge_store.upsert_gold(conn, [{**passes[0], "label": "Apply"}], "curated")
            labelled = judge_store.gold_keys(conn)
        picked = gold_form.stratify(rows, 10, seed=1, exclude=labelled)
        self.assertEqual(len(picked), 10)
        kinds = [("pass" if judge_store.passes_gate(r["gate"]) else "near" if judge_store.near_miss(r["gate"]) else "other")
                 for r in picked]
        self.assertEqual((kinds.count("pass"), kinds.count("near"), kinds.count("other")), (4, 3, 3))
        self.assertNotIn(judge_store.job_key(passes[0]), [r["job_key"] for r in picked])
        page = gold_form.build_html(picked, "2024-12-13")
        for r in picked:
            self.assertIn(f'data-key="{r["job_key"]}"', page)
        self.assertEqual(page.count('data-label="Maybe"'), 10)
        self.assertNotIn("~", page)
        self.assertNotIn("—", page)

    def test_a_short_bucket_is_topped_up_from_the_others(self):
        self.pull("2024-12-13", [row(f"IT Support Specialist {i}", f"Pass {i}") for i in range(12)])
        picked = gold_form.stratify(gold_form.candidate_rows(self.root), 10, seed=1)
        self.assertEqual(len(picked), 10)

    def test_seeds_map_past_decisions_and_the_form_wins(self):
        data = {"apply": [{"title": "IT Specialist", "company": "Example Agency", "apply_url": "https://usajobs/1"}],
                "skip": [{"title": "Technical Support Engineer", "company": "Salesforce", "tag": "Off lane"}]}
        self.assertEqual([(r["label"], r["company"]) for r in gold_import.seed_curated(data)],
                         [("Apply", "Example Agency"), ("Skip", "Salesforce")])
        pipeline = "\n".join([
            "| Hash | Company | Title | Location | Salary | Score | Decision | Status | Date Added | Last Updated | Notes |",
            "|------|---------|-------|----------|--------|-------|----------|--------|------------|--------------|-------|",
            "| x | Exampleco | Systems Administrator (Greenhouse 1) | Remote | $90k | 4 | Pursue | Applied | 2024-10-29 | 2024-10-30 | n |",
            "| x | Sampleco | IT Support Analyst | Denver | $70k | 3 | Skip | passed | 2024-11-15 | 2024-11-15 | n |",
            "| x | Found Co | Help Desk | Remote | ? | 2 | Found | Found 2024-11-25, closed unapplied | 2024-11-25 | 2024-11-25 | n |",
        ])
        seeded = gold_import.seed_pipeline(pipeline)
        self.assertEqual([(r["label"], r["title"]) for r in seeded],
                         [("Apply", "Systems Administrator"), ("Skip", "IT Support Analyst")])
        with judge_store.open_db(self.root) as conn:
            judge_store.upsert_gold(conn, seeded, "pipeline")
            key = judge_store.job_key({"company": "Exampleco", "title": "Systems Administrator"})
            judge_store.upsert_gold(conn, gold_import.form_labels({"labels": [
                {"job_key": key, "label": "Maybe", "reason": "pay", "title": "Systems Administrator", "company": "Exampleco"}]}), "form")
            judge_store.upsert_gold(conn, seeded, "pipeline")  # a re-seed must not undo the owner's label
            rows = {r["job_key"]: r for r in judge_store.gold_rows(conn)}
        self.assertEqual((rows[key]["label"], rows[key]["source"]), ("Maybe", "form"))
        with self.assertRaises(ValueError):
            with judge_store.open_db(self.root) as conn:
                judge_store.upsert_gold(conn, [{"title": "x", "company": "y", "label": "Yes"}], "form")

    def test_export_is_canonical_carries_the_posting_text_and_restores_the_table(self):
        # 2024-12-19: the owner's labels lived only in the git-ignored verdicts.db on an unbacked laptop.
        self.pull("2024-12-18", [row("IT Support Specialist", "A"), row("Help Desk", "B", description=TEXT + " second")])
        key_a = judge_store.job_key({"company": "A", "title": "IT Support Specialist"})
        with judge_store.open_db(self.root) as conn:
            judge_store.upsert_gold(conn, [{"title": "Help Desk", "company": "B", "label": "Skip", "reason": "curated Skip"}], "curated")
            judge_store.upsert_gold(conn, gold_import.form_labels({"labels": [
                {"job_key": key_a, "label": "Apply", "reason": "", "title": "IT Support Specialist", "company": "A"}]}), "form")
            path = gold_import.export_gold(conn, self.root)
            first = path.read_bytes()
            gold_import.export_gold(conn, self.root)
            original = {r["job_key"]: dict(r) for r in judge_store.gold_rows(conn)}
        self.assertEqual(first, path.read_bytes())  # nothing changed, so the file is byte for byte the same
        self.assertEqual(sorted(p.name for p in (self.root / "dashboard").iterdir()), ["gold-labels.json", "verdicts.db"])
        payload = json.loads(first)
        keys = [r["job_key"] for r in payload["labels"]]
        self.assertEqual(keys, sorted(keys))
        a = next(r for r in payload["labels"] if r["job_key"] == key_a)
        self.assertEqual((a["label"], a["source"], a["posting_date"]), ("Apply", "form", "2024-12-18"))
        self.assertTrue(a["posting_text"].startswith("Tier 2 desktop support"))
        # 2024-12-20: the CSV the label was made on is pruned; the export keeps the text and the gold run still has the row
        self.assertEqual(a["posting"]["location"], row("IT Support Specialist", "A")["location"])
        for csv_path in (self.root / "weekly-sweeps").glob("*.csv"):
            csv_path.unlink()
        with judge_store.open_db(self.root) as conn:
            gold_import.export_gold(conn, self.root)
        kept = next(r for r in json.loads(path.read_text(encoding="utf-8"))["labels"] if r["job_key"] == key_a)
        self.assertEqual((kept["posting_text"], kept["posting_date"], kept["posting"]), (a["posting_text"], "2024-12-18", a["posting"]))
        from scripts import judge
        fallback = {r["job_key"]: r for r in judge.gold_rows_with_text(self.root)}
        self.assertTrue(fallback[key_a]["description"].startswith("Tier 2 desktop support"))
        self.assertEqual(fallback[key_a]["location"], a["posting"]["location"])
        other = self.root / "other"
        (other / "dashboard").mkdir(parents=True)
        with judge_store.open_db(other) as conn:
            self.assertEqual(gold_import.restore_gold(conn, path), 2)
            restored = {r["job_key"]: dict(r) for r in judge_store.gold_rows(conn)}
        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
