"""Exercise flag-created configs through the existing loader and command builder."""
import json
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bench.config import load
from bench.runner import command

ROOT = Path(__file__).resolve().parents[1]


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / 'benchmark.local.json'

    def configure(self, *flags, success=True):
        result = subprocess.run([sys.executable, '-m', 'bench', 'configure', '--url',
                                 'http://127.0.0.1:8000', '--model', 'fixture', '--output', str(self.output),
                                 *flags], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_defaults_generate_runnable_bounded_config(self):
        self.configure()
        config = load(self.output)
        self.assertEqual(config['load']['concurrency'], [1, 2, 4])
        self.assertEqual(config['load']['repeats'], 3)
        self.assertEqual(config['metrics'], [])
        self.assertEqual(config['goals'], {})
        argv = command(config, 1, self.root, 'aiperf')
        self.assertIn('--no-server-metrics', argv)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_no_overwrite_including_symlink(self):
        self.configure(); original = self.output.read_bytes()
        self.configure('--model', 'changed', success=False)
        self.assertEqual(self.output.read_bytes(), original)
        self.output.unlink(); self.output.symlink_to(self.root / 'missing')
        self.configure(success=False)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.root / 'missing').exists())

    def test_invalid_settings_leave_no_config(self):
        for flags in [('--deadline-seconds', '50'), ('--concurrency', '0'),
                      ('--rates', '1'), ('--arrival', 'poisson'),
                      ('--header', 'Authorization=secret'), ('--header', 'x-a=1', '--header', 'X-A=2')]:
            with self.subTest(flags=flags):
                self.configure(*flags, success=False)
                self.assertFalse(self.output.exists())

    def test_file_workload_resolves_from_generated_config(self):
        source = self.root / 'prompts.jsonl'; source.write_text('{"text":"hello"}\n')
        self.configure('--prompts', str(source), '--concurrency', '2', '--repeats', '1')
        config = load(self.output)
        self.assertEqual(config['workload']['path'], str(source.resolve()))
        self.assertEqual(config['load']['concurrency'], [2])
        self.assertEqual(config['load']['repeats'], 1)

    def test_rates_auth_and_metric_requirements_survive_generation(self):
        metrics = self.root / 'metrics.json'
        metrics.write_text(json.dumps([{'name':'engine','url':'http://127.0.0.1:8000/metrics',
                                       'required':[{'metric':'queue_depth','why':'Queue pressure'}]}]))
        self.configure('--rates', '0.5', '2', '--arrival', 'poisson', '--max-concurrency', '8',
                       '--api-key-env', 'INFERENCE_TOKEN', '--no-model-list', '--metrics-file', str(metrics))
        config = load(self.output)
        self.assertNotIn('models_path', config['endpoint'])
        self.assertEqual(config['endpoint']['api_key_env'], 'INFERENCE_TOKEN')
        self.assertEqual(config['metrics'][0]['required'][0]['metric'], 'queue_depth')
        argv = command(config, 0.5, self.root, 'aiperf')
        self.assertEqual(argv[argv.index('--request-rate-mode')+1], 'poisson')
        self.assertIn('--server-metrics', argv)

    def test_metrics_file_shape_is_checked(self):
        metrics = self.root / 'metrics.json'; metrics.write_text('{}')
        self.configure('--metrics-file', str(metrics), success=False)
        self.assertFalse(self.output.exists())

    def test_next_command_quotes_spaces(self):
        self.output = self.root / 'my config.json'
        result = self.configure()
        hint = json.loads(result.stdout)['next'].split('then run ', 1)[1]
        self.assertEqual(shlex.split(hint), ['make', 'verify', 'CONFIG=' + str(self.output.resolve())])

    def test_bad_dataset_reports_actual_row(self):
        source = self.root / 'bad.jsonl'; source.write_text('{"text":"valid"}\nnot-json\n')
        result = self.configure('--prompts', str(source), success=False)
        self.assertIn('Dataset line 2', result.stderr)
        source.write_text('[]\n')
        result = self.configure('--prompts', str(source), success=False)
        self.assertIn('Dataset line 1', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
