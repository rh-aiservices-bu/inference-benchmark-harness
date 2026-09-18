"""Keep default smoke and measurement artifacts separate; respect operator paths."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MakeCommands(unittest.TestCase):
    def command(self, target, *overrides, env_run=None):
        env = {k: v for k, v in os.environ.items() if k not in ('RUN', 'CONFIG', 'MAKEFLAGS', 'MFLAGS')}
        if env_run is not None:
            env['RUN'] = env_run
        result = subprocess.run(['make', '-s', '-n', target, *overrides], cwd=ROOT,
                                env=env, text=True, capture_output=True, check=True)
        return shlex.split(result.stdout.strip().splitlines()[-1])

    def test_defaults_keep_smoke_and_benchmark_separate(self):
        for target, directory in [('smoke', 'results/smoke'), ('benchmark', 'results/benchmark'),
                                  ('resume-smoke', 'results/smoke'), ('resume', 'results/benchmark')]:
            with self.subTest(target=target):
                args = self.command(target)
                self.assertEqual(args[args.index('--run') + 1], directory)
                self.assertEqual(args[args.index('--config') + 1], 'benchmark.local.json')

    def test_explicit_paths_override_defaults(self):
        for target in ('smoke', 'benchmark'):
            args = self.command(target, 'CONFIG=/private/my config.json', 'RUN=/private/my results')
            self.assertEqual(args[args.index('--config') + 1], '/private/my config.json')
            self.assertEqual(args[args.index('--run') + 1], '/private/my results')

    def test_environment_run_is_preserved(self):
        for target in ('smoke', 'benchmark'):
            args = self.command(target, env_run='/private/chosen results')
            self.assertEqual(args[args.index('--run') + 1], '/private/chosen results')

    def test_matrix_uses_its_own_output_and_explicit_input(self):
        for target in ('matrix-plan', 'matrix-run', 'matrix-resume'):
            args = self.command(target, 'MATRIX=/private/matrix.json')
            self.assertEqual(args[args.index('--run') + 1], 'results/matrix')
            self.assertEqual(args[args.index('--config') + 1], '/private/matrix.json')
        env = {k: v for k, v in os.environ.items() if k not in ('MATRIX', 'MAKEFLAGS', 'MFLAGS')}
        result = subprocess.run(['make', '-s', 'matrix-run', 'CONFIG=wrong.json'], cwd=ROOT,
                                env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Set MATRIX=', result.stderr)

    def test_configure_honors_config_output_path(self):
        args = self.command('configure', 'CONFIG=/private/new config.json',
                            'ARGS=--url http://localhost:8000 --model fixture')
        self.assertEqual(args[args.index('--output') + 1], '/private/new config.json')
