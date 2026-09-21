"""Reject mismatched exports without rejecting unchanged compressed metric samples."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from bench.config import load
from bench.evidence import analyze

ROOT = Path(__file__).resolve().parents[1]


class EvidenceConsistencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = self.root / "native"
        self.native.mkdir()
        self.config = load(ROOT / "examples/benchmark.json")
        self.config["load"]["requests"] = 3
        self.config["goals"] = {"ttft_p95_ms": 50}
        self.url = "http://localhost/metrics"
        self.config["metrics"] = [{"name": "engine", "url": self.url,
                                   "required": [{"metric": "running", "why": "Occupancy"}]}]
        self.rows = [{"metadata": {"request_start_ns": i * 10**9,
                                   "request_end_ns": i * 10**9 + i * 100 * 10**6},
                      "metrics": {"request_latency": {"unit": "ms", "value": i * 100},
                                  "time_to_first_token": {"unit": "ms", "value": i * 10}}}
                     for i in (1, 2, 3)]
        self.summary = {"aiperf_version": "0.12.0", "schema_version": "1.4",
                        "request_count": {"avg": 3},
                        "request_latency": {"unit": "ms", "p95": 290, "count": 3},
                        "time_to_first_token": {"unit": "ms", "p95": 29, "count": 3}}
        self.server = {"summary": {"endpoint_info": {self.url: {
            "first_fetch_ns": 900_000_000, "last_fetch_ns": 4_000_000_000, "total_fetches": 5}}},
            "metrics": {"running": {"type": "gauge"}}}
        self.sample = {"timestamp_ns": 900_000_000, "endpoint_url": self.url,
                       "metrics": {"running": [{"value": 0}]}}

    def result(self, rows=None, summary=None, sample=None):
        (self.native / "profile_export.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (rows or self.rows)))
        (self.native / "profile_export_aiperf.json").write_text(json.dumps(summary or self.summary))
        (self.native / "server_metrics_export.json").write_text(json.dumps(self.server))
        (self.native / "server_metrics_export.jsonl").write_text(json.dumps(sample or self.sample) + "\n")
        return analyze(self.root, self.config, {"exit_code": 0})

    def test_unchanged_value_before_requests_remains_valid(self):
        result = self.result()
        self.assertEqual(result["evidence"], "complete")
        self.assertTrue(result["metrics"][0]["window_covered"])
        self.assertEqual(result["goals"][0]["status"], "met")

    def test_samples_outside_producer_window_cannot_prove_coverage(self):
        for ns in (899_999_999, 4_000_000_001, 900_000_000 + 86400 * 10**9):
            with self.subTest(timestamp=ns):
                result = self.result(sample={**self.sample, "timestamp_ns": ns})
                self.assertEqual(result["evidence"], "invalid")
                self.assertFalse(result["metrics"][0]["window_covered"])
                self.assertEqual(result["goals"][0]["status"], "unverified")

    def test_sample_cannot_borrow_another_producers_window(self):
        result = self.result(sample={**self.sample, "endpoint_url": "http://other/metrics"})
        self.assertEqual(result["evidence"], "invalid")

    def test_invalid_or_missing_record_timing_cannot_support_aggregate_goal(self):
        for metric in (None, {"unit": "s", "value": 0.01}, {"unit": "ms", "value": float("nan")}):
            with self.subTest(metric=metric):
                rows = copy.deepcopy(self.rows)
                if metric is None:
                    del rows[0]["metrics"]["time_to_first_token"]
                else:
                    rows[0]["metrics"]["time_to_first_token"] = metric
                result = self.result(rows=rows)
                self.assertIn("incomplete_timing_population:time_to_first_token", result["reasons"])
                self.assertEqual(result["goals"][0]["status"], "unverified")

    def test_outside_range_percentile_or_wrong_count_is_invalid(self):
        for patch, reason in (({"p95": 5}, "timing_percentile_out_of_bounds"),
                              ({"p95": 40}, "timing_percentile_out_of_bounds"),
                              ({"count": 2}, "timing_counts_disagree")):
            with self.subTest(patch=patch):
                summary = copy.deepcopy(self.summary)
                summary["time_to_first_token"].update(patch)
                result = self.result(summary=summary)
                self.assertIn(reason + ":time_to_first_token", result["reasons"])
                self.assertEqual(result["goals"][0]["status"], "unverified")

    def test_failed_requests_do_not_enter_successful_timing_population(self):
        rows = copy.deepcopy(self.rows)
        rows[-1] = {"metadata": rows[-1]["metadata"], "error": {"code": 503}}
        summary = copy.deepcopy(self.summary)
        summary.update(request_count={"avg": 2}, error_request_count={"avg": 1})
        summary["time_to_first_token"].update(p95=19.5, count=2)
        summary["request_latency"].update(p95=195, count=2)
        result = self.result(rows=rows, summary=summary)
        self.assertEqual(result["evidence"], "complete")
        self.assertEqual(result["failed_requests"], 1)
