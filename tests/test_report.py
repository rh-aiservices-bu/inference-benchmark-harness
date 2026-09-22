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
        self.root = Path(self.temp.name).resolve()
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
        self.config['load']['repeats'] = 1
        self.save('config.json', self.config)
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

    def test_native_statistics_keep_units_ranges_and_evidence_unchanged(self):
        for repeat, itl, output_length in [(1, 4, 280), (2, 7, 300)]:
            name = self.attempt(repeat)
            self.save(name + '/native/profile_export_aiperf.json', {
                'inter_token_latency': {'unit': 'ms', 'p95': itl},
                'output_token_throughput': {'unit': 'tokens/sec', 'avg': 247},
                'input_sequence_length': {'unit': 'tokens', 'avg': 1000},
                'output_sequence_length': {'unit': 'tokens', 'avg': output_length},
                'benchmark_duration': {'unit': 'sec', 'avg': 60},
            })
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        data = read_report(self.root)
        output = format_report(data)
        self.assertIn('| full run | 4–7 | 247 | 1,000 | 280–300 | 60 |', output)
        self.assertNotIn('native_metrics', data[0]['attempts'][name])
        self.assertEqual(data[0]['native_metrics'][name]['workload']['input_tokens_mean'], 1000)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_invalid_native_metrics_preserve_other_metrics_and_missing_is_unknown(self):
        name = self.attempt(1)
        self.save(name + '/native/profile_export_aiperf.json', {
            'inter_token_latency': {'unit': 'sec', 'p95': 4},
            'output_token_throughput': {'unit': 'tokens/sec', 'avg': float('inf')},
            'input_sequence_length': [],
            'output_sequence_length': {'unit': 'tokens', 'avg': True},
            'benchmark_duration': {'unit': 'sec', 'avg': 60},
        })
        self.attempt(2)  # Older or incomplete export: absence cannot become zero.
        data = read_report(self.root)
        output = format_report(data)
        self.assertIn('| full run | unknown | unknown | unknown | unknown | 60 (partial) |', output)
        self.assertIn('native inter_token_latency.p95 unavailable', output)
        self.assertIn('| 2 | 2/20 | 10 | 90 | 2 |', output)
        self.assertEqual(data[0]['status'], 'complete')

    def test_native_metrics_never_leak_into_shared_cohort_or_accept_rejected_peer(self):
        name = 'row-001-repeat-01-attempt-001'
        rejected = 'row-001-repeat-02-attempt-001'
        self.state.update(kind='matrix', completed=[name])
        self.save('state.json', self.state)
        for directory, evidence, itl in [(name, 'complete', 4), (rejected, 'invalid', 900)]:
            self.save(directory + '/summary.json', {'evidence': evidence,
                      'streams': {'a': self.summary(), 'b': self.summary()},
                      'shared_window': {'a': self.summary(20)}})
            self.save(directory + '/a/native/profile_export_aiperf.json',
                      {'inter_token_latency': {'unit': 'ms', 'p95': itl}})
        output = format_report(read_report(self.root))
        native_table = output.split('## Streaming and workload')[1].split('## Checks')[0]
        self.assertIn('| a | full run | 4 |', native_table)
        self.assertIn('| a | unaccepted | 900 |', native_table)
        self.assertNotIn('shared arrivals', native_table)
        self.assertNotIn('4–900', native_table)

    def test_damaged_accepted_group_keeps_every_peer_unaccepted(self):
        name = 'row-001-repeat-01-attempt-001'
        self.state.update(kind='matrix', completed=[name])
        self.save('state.json', self.state)
        self.save('config.json', {'repeats': 1, 'rows': [{'streams': {'a': {}, 'b': {}}}]})
        for streams in ({}, {'a': self.summary()},
                        {'a': self.summary(), 'b': {**self.summary(), 'evidence': 'invalid'}}):
            with self.subTest(streams=streams):
                self.save(name + '/summary.json', {'evidence': 'complete', 'streams': streams})
                result = self.cli('--format', 'text')
                self.assertEqual(result.returncode, 2)
                self.assertIn('missing or incomplete stream evidence', result.stdout)
                self.assertNotIn('| full run |', result.stdout)

    def test_extreme_numeric_value_does_not_destroy_valid_sibling_data(self):
        self.attempt(1, self.summary(10**500))
        self.attempt(2, self.summary(20))
        self.assertIn('20 (partial)', self.cli('--format', 'text').stdout)

    def test_external_artifact_symlink_is_not_read(self):
        name = self.attempt(1)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'external.json'
            target.write_text(json.dumps({'evidence': 'invalid', 'reasons': ['SECRET-EXTERNAL']}))
            artifact = self.root / name / 'summary.json'
            artifact.unlink()
            artifact.symlink_to(target)
            result = self.cli('--format', 'text')
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('SECRET-EXTERNAL', result.stdout)
            self.assertIn('external artifact paths', result.stdout)

    def test_missing_config_and_inconsistent_checkpoint_are_explicit(self):
        self.attempt(1)
        result = self.cli('--format', 'text')
        self.assertEqual(result.returncode, 2)
        self.assertIn('complete status disagrees with planned repeat count', result.stdout)
        (self.root / 'config.json').unlink()
        result = self.cli('--format', 'text')
        self.assertEqual(result.returncode, 2)
        self.assertIn('config.json: missing', result.stdout)
        self.assertIn('| 1 | 1/10 | 10 | 90 | 2 |', result.stdout)

    def test_matrix_context_and_goal_thresholds_are_shareable_without_raw_config(self):
        name = 'row-001-repeat-01-attempt-001'
        self.state.update(kind='matrix', completed=[name], config_hash='a' * 64)
        self.save('state.json', self.state)
        config = copy.deepcopy(self.config)
        config['endpoint']['headers'] = {'Authorization': 'SECRET'}
        config['load'] = {'rates': [2], 'arrival': 'poisson', 'max_concurrency': 8}
        self.save('config.json', {'name': 'comparison', 'repeats': 1, 'rows': [{
            'stage': 'control', 'profile': 'baseline', 'question': 'What is the overhead?',
            'change': 'Enable admission', 'streams': {'interactive': config}}]})
        summary = self.summary()
        summary['goals'] = [{'goal': 'ttft_p95_ms', 'limit': 8, 'observed': 10, 'status': 'missed'}]
        self.save(name + '/summary.json', {'evidence': 'complete', 'streams': {'interactive': summary}})
        output = self.cli('--format', 'text').stdout
        self.assertIn('| 1 | control | baseline | What is the overhead? | Enable admission |', output)
        self.assertIn('2 req/s (poisson; cap 8)', output)
        self.assertIn('| ttft_p95_ms | 8 | 10 | 1 missed |', output)
        self.assertIn('Saved configuration SHA-256: ' + 'a' * 64, output)
        self.assertNotIn('SECRET', output)

    def test_replacement_attempts_and_out_of_range_slots_do_not_count_as_repeats(self):
        self.config['load']['repeats'] = 2
        self.save('config.json', self.config)
        first = self.attempt(1)
        second = 'point-01-repeat-01-attempt-002'
        self.save(second + '/summary.json', self.summary())
        for names in ([first, second], [first, 'point-02-repeat-02-attempt-001']):
            with self.subTest(names=names):
                self.state['completed'] = names
                self.save('state.json', self.state)
                self.save(names[1] + '/summary.json', self.summary())
                result = self.cli('--format', 'text')
                self.assertEqual(result.returncode, 2)
                self.assertIn('duplicate or out-of-range repeat slot', result.stdout)
                self.assertNotIn('Progress: 2/2', result.stdout)

    def test_missing_shared_evidence_preserves_full_results_but_flags_partial_report(self):
        name = 'row-001-repeat-01-attempt-001'
        self.state.update(kind='matrix', completed=[name])
        self.save('state.json', self.state)
        self.save('config.json', {'repeats': 1, 'rows': [{'streams': {'a': {}, 'b': {}}}]})
        for shared in (None, {}, {'a': self.summary()}, {'a': self.summary(), 'b': []}):
            self.save(name + '/summary.json', {'evidence': 'complete',
                      'streams': {'a': self.summary(), 'b': self.summary()}, 'shared_window': shared})
            result = self.cli('--format', 'text')
            self.assertEqual(result.returncode, 2)
            self.assertIn('missing or malformed shared-arrival evidence', result.stdout)
            self.assertIn('| a | unknown | full run |', result.stdout)
