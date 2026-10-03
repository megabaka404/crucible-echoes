from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from crucible_echoes.catalog import Catalog
from crucible_echoes.simulation import run_batch
from tools.offer_telemetry_audit import audit_report, run_audit


class OfferTelemetryAuditTests(unittest.TestCase):
    def test_raw_candidate_slots_reconcile_with_per_game_and_batch_counters(self):
        catalog = Catalog.load()
        report = run_batch(2, 20261012, 7, catalog=catalog)
        a = audit_report(report, catalog)
        self.assertTrue(a["reconciled"])
        self.assertEqual(2, a["games"])
        self.assertEqual(sum(a["outcome_slots"].values()), a["candidate_slots"])
        self.assertEqual(sum(row["offers"] for row in a["content"]), a["candidate_slots"])
        self.assertEqual(sum(row["resolved_offers"] for row in a["content"]), a["resolved_slots"])
        self.assertEqual(a, audit_report(deepcopy(report), catalog))
        report.content["ingredients"][0]["offer_count"] += 1
        with self.assertRaisesRegex(ValueError, "Aggregate"):
            audit_report(report, catalog)

    def test_aborts_missing_details_and_legacy_are_rejected(self):
        catalog = Catalog.load()
        report = run_batch(1, 45, 7, catalog=catalog)
        report.games_detail[0]["strategy_events"].pop("offer_tracking")
        with self.assertRaisesRegex(ValueError, "Legacy"):
            audit_report(report, catalog)
        report = run_batch(1, 45, 7, catalog=catalog, retain_details=False)
        with self.assertRaisesRegex(ValueError, "every game detail"):
            audit_report(report, catalog)
        report = run_batch(1, 45, 7, catalog=catalog, max_actions=1)
        with self.assertRaisesRegex(ValueError, "zero aborted"):
            audit_report(report, catalog)

    def test_audit_has_fingerprints_snapshot_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audit.json"
            args = dict(games=1, seed=46, difficulties=[7], output=output)
            report = run_audit(**args)
            self.assertTrue(report["complete"])
            self.assertIn("source", report["fingerprint"])
            self.assertTrue(output.with_suffix(".snapshot.zip").exists())
            before = output.read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                run_audit(**args)
            self.assertEqual(before, output.read_bytes())
