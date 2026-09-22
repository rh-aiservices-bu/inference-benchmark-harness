"""Reporting must survive partial evidence without changing benchmark outcomes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bench.report import read_report, format_report

ROOT = Path(__file__).resolve().parents[1]


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads((ROOT / 'examples/benchmark.json').read_text())
        self.config['load'].update(concurrency=[1], repeats=3)
        self.save('config.json', self.config)
        self.state = {'status': 'complete', 'completed': []}
        self.save('state.json', self.state)

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def summary(self, ttft=10):
        return {'evidence': 'complete', 'requests': 10, 'failed_requests': 1,
                'measurements': {'ttft_p95_ms': ttft, 'latency_p95_ms': 90,
                                 'request_throughput_rps': 2},
                'goals': [{'status': 'missed'}], 'metrics': []}

    def attempt(self, repeat, summary=None, accepted=True):
        name = f'point-01-repeat-{repeat:02d}-attempt-001'
        self.save(name + '/summary.json', summary or self.summary())
        if accepted:
            self.state['completed'].append(name)
        self.save('state.json', self.state)
        return name

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'bench', 'report', '--run', str(self.root), *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_summary_ranges_preserve_failures_and_never_average_p95(self):
        self.attempt(1, self.summary(10))
        self.attempt(2, self.summary(40))
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        output = format_report(read_report(self.root))
        self.assertIn('2/3 checkpointed repeats', output)
        self.assertIn('| 2 | 2/20 | 10–40 | 90 | 2 |', output)
        self.assertIn('Goals: 2 missed', output)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_failed_scrape_keeps_client_observations_separate(self):
        s = self.summary(70)
        s.update(evidence='invalid', reasons=['required_metrics_window_incomplete:router'],
                 metrics=[{'producer': 'router', 'window_covered': False, 'missing_samples': ['queue_size']}])
        self.attempt(1, s, accepted=False)
        output = format_report(read_report(self.root))
        self.assertIn('0/3 checkpointed repeats; 1 unaccepted', output)
        self.assertIn('| unaccepted | 1 | 1/10 | 70 |', output)
        self.assertIn('router: collection incomplete; missing queue_size', output)
        self.assertIn('required_metrics_window_incomplete:router', output)

    def test_corrupt_summary_keeps_other_results_and_returns_nonzero(self):
        self.attempt(1)
        name = self.attempt(2)
        (self.root / name / 'summary.json').write_text('{incomplete')
        result = self.cli('--format', 'text')
        self.assertEqual(result.returncode, 2)
        self.assertIn('partial report', result.stdout)
        self.assertIn('| 1 | 1/10 | 10 |', result.stdout)
        self.assertIn('summary.json: missing, unreadable or malformed', result.stdout)

    def test_missing_accepted_directory_and_bad_state_do_not_crash(self):
        self.state['completed'] = ['point-01-repeat-01-attempt-001']
        self.save('state.json', self.state)
        result = self.cli('--format', 'text')
        self.assertEqual(result.returncode, 2)
        self.assertIn('accepted attempt directory missing', result.stdout)
        self.save('state.json', {'status': 'complete', 'completed': [['invalid']]})
        self.assertIn('invalid completed-attempt list', self.cli('--format', 'text').stdout)

    def test_preflight_failure_does_not_need_summary(self):
        self.state['status'] = 'preflight_failed'
        self.save('state.json', self.state)
        self.save('point-01-attempt-001/preflight.json',
                  [{'name': 'metrics', 'status': 'fail', 'detail': 'SECRET endpoint response'}])
        result = self.cli('--format', 'text')
        self.assertEqual(result.returncode, 2)
        self.assertIn('metrics: fail (preflight.json; unaccepted attempt)', result.stdout)
        self.assertNotIn('SECRET', result.stdout)

    def test_projection_omits_private_config_and_raw_errors(self):
        self.config['endpoint'].update(url='https://SECRET-ENDPOINT', headers={'Authorization': 'SECRET-TOKEN'})
        self.save('config.json', self.config)
        s = self.summary()
        s['raw_error'] = 'SECRET-PROMPT'
        self.attempt(1, s)
        output = format_report(read_report(self.root))
        self.assertNotIn('SECRET', output)
        self.assertNotIn(str(self.root), output)

    def test_unknown_nonfinite_and_malformed_measurements(self):
        self.attempt(1, self.summary(float('nan')))
        s = self.summary()
        s['measurements'] = []
        self.attempt(2, s)
        output = format_report(read_report(self.root))
        self.assertIn('| unknown | 90 (partial) | 2 (partial) |', output)

    def test_json_stays_detailed_and_make_defaults_to_summary(self):
        self.attempt(1)
        result = self.cli()
        self.assertIn('attempts', json.loads(result.stdout))
        output = subprocess.run(['make', '-s', 'report', f'RUN={self.root}', f'PYTHON={sys.executable}'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(output.returncode, 0)
        self.assertTrue(output.stdout.startswith('# Benchmark report'))
        detailed = subprocess.run(['make', '-s', 'report', f'RUN={self.root}', f'PYTHON={sys.executable}', 'FORMAT=json'],
                                  cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(json.loads(detailed.stdout)['status'], 'complete')

    def test_mixed_group_rejection_never_accepts_successful_peer(self):
        name = 'row-001-repeat-01-attempt-001'
        self.state.update(kind='matrix', completed=[name])
        self.save('state.json', self.state)
        stream_config = copy.deepcopy(self.config)
        stream_config['load'].update(concurrency=[2])
        self.save('config.json', {'repeats': 3, 'rows': [{'streams': {'interactive': stream_config}}]})
        self.save(name + '/summary.json', {'evidence': 'complete', 'streams': {'interactive': self.summary()}})
        failed = 'row-001-repeat-02-attempt-001'
        self.save(failed + '/summary.json', {'evidence': 'invalid', 'streams': {'interactive': self.summary(200)},
                                            'reasons': ['insufficient_arrival_overlap']})
        output = format_report(read_report(self.root))
        self.assertIn('1/3 checkpointed repeats', output)
        self.assertIn('| interactive | 2 concurrent | full run | 1 | 1/10 | 10 |', output)
        self.assertIn('| unaccepted | 1 | 1/10 | 200 |', output)

    def test_shared_cohort_is_separate_and_has_no_throughput(self):
        name = 'row-001-repeat-01-attempt-001'
        self.state.update(kind='matrix', completed=[name])
        self.save('state.json', self.state)
        cohort = self.summary(20)
        cohort['measurements'].pop('request_throughput_rps')
        self.save(name + '/summary.json', {'evidence': 'complete',
                  'streams': {'a': self.summary(), 'b': self.summary()}, 'shared_window': {'a': cohort}})
        output = format_report(read_report(self.root))
        self.assertIn('| shared arrivals | 1 | 1/10 | 20 | 90 | unknown |', output)
        self.assertIn('do not add their counts', output)

    def test_checkpoint_change_is_reported(self):
        self.attempt(1)
        original = Path.read_text
        count = 0
        def read(path, *args, **kwargs):
            nonlocal count
            if path == self.root / 'state.json':
                count += 1
                if count == 2:
                    return json.dumps({'status': 'running'})
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.assertIn('Checkpoint changed', format_report(read_report(self.root)))

    def test_export_without_timezone_is_not_claimed_as_utc(self):
        name = self.attempt(1)
        self.save(name + '/native/profile_export_aiperf.json',
                  {'start_time': '2026-09-18T10:00:00', 'end_time': '2026-09-18T10:00:05'})
        output = format_report(read_report(self.root))
        self.assertIn('2026-09-18T10:00:05 (timezone not exported)', output)
        self.save(name + '/native/profile_export_aiperf.json',
                  {'start_time': '2026-09-18T10:00:00-04:00', 'end_time': '2026-09-18T10:00:05-04:00'})
        self.assertIn('2026-09-18T14:00:05+00:00 (UTC)', format_report(read_report(self.root)))
