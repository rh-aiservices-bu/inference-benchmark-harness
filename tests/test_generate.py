import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bench.matrix import load_matrix

ROOT = Path(__file__).resolve().parents[1]


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / 'comparison.json'
        self.args = ['--output', str(self.output), '--name', 'background-sweep',
                     '--question', 'Does background demand delay interactive responses?',
                     '--stream', 'interactive=examples/benchmark.json',
                     '--stream', 'background=examples/background.json',
                     '--sweep', 'background', '--hold', 'interactive=0.5',
                     '--axis', 'rates', '--values', '0.5,1,2', '--max-concurrency', '4']

    def cli(self, args):
        return subprocess.run([sys.executable, '-B', '-m', 'bench', 'matrix-create', *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_generated_matrix_expands_real_configs_and_exact_budget(self):
        before = (ROOT / 'examples/benchmark.json').read_bytes()
        result = self.cli(self.args)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary['rows'], summary['repeats_per_row'], summary['max_requests_first_pass']), (3, 3, 360))
        config = load_matrix(self.output)
        self.assertEqual([r['streams']['background']['load']['rates'] for r in config['rows']], [[.5], [1], [2]])
        for row in config['rows']:
            self.assertEqual(row['streams']['interactive']['load']['rates'], [.5])
            self.assertEqual(row['streams']['interactive']['endpoint']['model'], 'served-model')
            self.assertEqual(row['streams']['interactive']['goals'], {})
        self.assertFalse(summary['traffic_sent'])
        self.assertEqual(before, (ROOT / 'examples/benchmark.json').read_bytes())
        self.assertEqual([p.name for p in self.root.iterdir()], ['comparison.json'])

    def test_bad_inputs_leave_no_output(self):
        variants = [self.args + ['--stream', 'interactive=examples/background.json'],
                    self.args + ['--hold', 'unknown=1'],
                    self.args[:-2],
                    self.args + ['--values', '0,1'],
                    self.args + ['--values', 'NaN'],
                    self.args + ['--values', ''],
                    self.args + ['--repeats', '0'],
                    self.args + ['--min-overlap-seconds', '-1'],
                    self.args + ['--stream', 'missing=not-there.json', '--hold', 'missing=1'],
                    self.args + ['--axis', 'concurrency']]
        for args in variants:
            with self.subTest(args=args):
                result = self.cli(args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_file_and_symlink_are_never_overwritten(self):
        self.output.write_text('preserve me')
        self.assertNotEqual(self.cli(self.args).returncode, 0)
        self.assertEqual(self.output.read_text(), 'preserve me')
        self.output.unlink()
        target = self.root / 'absent.json'
        self.output.symlink_to(target)
        self.assertNotEqual(self.cli(self.args).returncode, 0)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(target.exists())

    def test_single_workload_concurrency_sweep_and_fraction_rejected(self):
        args = ['--output', str(self.output), '--name', 'baseline', '--question', 'Where does timing change?',
                '--stream', 'interactive=examples/benchmark.json', '--sweep', 'interactive',
                '--axis', 'concurrency', '--values', '1,2,4']
        result = self.cli(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = load_matrix(self.output)
        self.assertEqual([r['streams']['interactive']['load']['concurrency'] for r in config['rows']], [[1], [2], [4]])
        self.output.unlink()
        self.assertNotEqual(self.cli(args + ['--values', '1.5']).returncode, 0)
        self.assertFalse(self.output.exists())
