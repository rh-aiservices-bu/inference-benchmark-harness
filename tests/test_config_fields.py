"""Hand-edited configs must not silently discard intended checks or repeats."""

import copy
import json
from pathlib import Path
import re
import unittest

from bench.config import attempt_limit, load, validate

ROOT = Path(__file__).resolve().parents[1]


class ConfigFieldsTests(unittest.TestCase):
    def test_unknown_fields_report_their_location(self):
        cases = [
            ({"repeat": 3}, "config.repeat"),
            ({"endpoint": {"header": {}}}, "endpoint.header"),
            ({"workload": {"output_token": 64}}, "workload.output_token"),
            ({"load": {"repeat": 3}}, "load.repeat"),
            ({"metrics": [{"require": []}]}, "metrics[0].require"),
            ({"metrics": [{"required": [{"units": "ms"}]}]}, "metrics[0].required[0].units"),
            ({"goals": {"ttft_p95": 50}}, "goals.ttft_p95"),
            ({"kubernetes": {"contexts": "local"}}, "kubernetes.contexts"),
            ({"kubernetes": {"deployments": [{"replica": 2}]}}, "kubernetes.deployments[0].replica"),
            ({"kubernetes": {"routing": {"rule": 0}}}, "kubernetes.routing.rule"),
            ({"kubernetes": {"routing": {"gateway": {"sections": "http"}}}}, "kubernetes.routing.gateway.sections"),
            ({"kubernetes": {"routing": {"objectives": [{"priorities": 10}]}}}, "kubernetes.routing.objectives[0].priorities"),
        ]
        for changed, field in cases:
            with self.subTest(field=field):
                config = json.loads((ROOT / "examples/benchmark.json").read_text())
                for key, value in changed.items():
                    if key in ("endpoint", "load", "workload"):
                        config[key].update(value)
                    else:
                        config[key] = value
                with self.assertRaisesRegex(ValueError, re.escape(field)):
                    validate(config, ROOT)

    def test_wrong_container_types_fail_with_field_path(self):
        for field, value in (("load", []), ("metrics", {}), ("goals", None), ("kubernetes", [])):
            with self.subTest(field=field):
                config = load(ROOT / "examples/benchmark.json")
                config[field] = value
                with self.assertRaisesRegex(ValueError, field):
                    validate(config, ROOT)

    def test_legacy_attempt_limit_and_derived_dataset_hash_remain_supported(self):
        config = load(ROOT / "examples/benchmark.json")
        config["load"]["max_attempts_per_point"] = config["load"].pop("max_attempts_per_repeat")
        config["workload"] = {"type": "single_turn", "path": "examples/prompts.jsonl", "output_tokens": 16}
        first = validate(config, ROOT)
        self.assertEqual(attempt_limit(first), 3)
        self.assertEqual(validate(copy.deepcopy(first), ROOT), first)
