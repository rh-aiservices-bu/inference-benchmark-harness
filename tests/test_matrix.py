import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from bench.config import load
from bench.evidence import write_json
from bench.matrix import load_matrix, matrix_campaign, observe, plan_matrix

ROOT = Path(__file__).resolve().parents[1]


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = json.loads((ROOT / 'examples/matrix.json').read_text())
        for name in ('benchmark.json', 'background.json'):
            (self.root / name).write_text((ROOT / 'examples' / name).read_text())
        self.path = self.root / 'matrix.json'
        self.save()
        self.calls = []

    def save(self):
        self.path.write_text(json.dumps(self.spec))
        return load_matrix(self.path)

    def group(self, streams, directory, aiperf, lock_fd, min_overlap_seconds, **kwargs):
        self.calls.append(tuple(streams))
        for name, config in streams.items():
            folder = directory / name
            folder.mkdir()
            write_json(folder / 'config.json', config)
            write_json(folder / 'preflight.json', [])
            write_json(folder / 'raw.json', {'preserved': True})
        return {'evidence': 'complete', 'reasons': [], 'outcome': 'ready', 'streams': {}}

    def test_product_budget_and_no_write_plan(self):
        config = self.save()
        self.assertEqual(len(config['rows']), 4)
        planned = plan_matrix(config, self.root / 'unused', 'aiperf')
        self.assertEqual(planned['max_requests_first_pass'], 360)
        self.assertEqual(planned['max_requests_with_manual_retries'], 1080)
        self.assertEqual(planned['min_overlap_seconds'], config['min_overlap_seconds'])
        self.assertEqual(planned['experiments'][0]['budgets']['interactive']['requests'], 20)
        self.assertFalse((self.root / 'unused').exists())
        self.assertEqual(config['rows'][2]['streams']['interactive']['load']['rates'], [.5])

    def test_complete_resume_never_replays(self):
        config = self.save()
        with patch('bench.mixed.execute_group', side_effect=self.group):
            state = matrix_campaign(config, self.root / 'run', 'aiperf')
            self.assertEqual(len(self.calls), 12)
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(len(state['completed']), 12)
            again = matrix_campaign(config, self.root / 'run', 'aiperf', True)
            self.assertEqual(again, state)
            self.assertEqual(len(self.calls), 12)

    def test_final_row_checkpoint_resume_finalizes_without_traffic(self):
        for outcome in ('ready', 'goal_not_met', 'request_errors'):
            with self.subTest(outcome=outcome):
                config = self.save()
                root = (self.root / outcome).resolve()
                def group(*args, **kwargs):
                    result = self.group(*args, **kwargs)
                    if len(self.calls) <= config['repeats']:
                        result['outcome'] = outcome
                    return result
                def crash_after_last_row(path, value):
                    write_json(path, value)
                    if path == root / 'state.json' and value.get('next_row') == len(config['rows']):
                        raise RuntimeError('crash after durable last-row checkpoint')
                self.calls.clear()
                with patch('bench.mixed.execute_group', side_effect=group):
                    with patch('bench.matrix.write_json', side_effect=crash_after_last_row):
                        with self.assertRaisesRegex(RuntimeError, 'last-row checkpoint'):
                            matrix_campaign(config, root, 'aiperf')
                            matrix_campaign(config, root, 'aiperf', True)
                saved = json.loads((root / 'state.json').read_text())
                self.assertEqual(saved['next_row'], len(config['rows']))
                with patch('bench.mixed.execute_group', side_effect=AssertionError('traffic replayed')):
                    resumed = matrix_campaign(config, root, 'aiperf', True)
                    self.assertEqual(resumed['status'], 'complete' if outcome == 'ready' else outcome)
                    self.assertEqual(resumed['completed'], saved['completed'])
                    self.assertEqual(resumed, json.loads((root / 'state.json').read_text()))
                    self.assertEqual(matrix_campaign(config, root, 'aiperf', True), resumed)

    def test_failed_peer_replaces_entire_group_and_preserves_first(self):
        self.spec['stages'] = [self.spec['stages'][1]]
        config = self.save()
        def failed(*args, **kw):
            result = self.group(*args, **kw)
            result.update(evidence='invalid', reasons=['peer_failed'])
            return result
        with patch('bench.mixed.execute_group', side_effect=failed):
            state = matrix_campaign(config, self.root / 'run', 'aiperf')
        self.assertEqual(state['completed'], [])
        self.assertEqual(state['next_repeat'], 0)
        original = self.root / 'run/row-001-repeat-01-attempt-001'
        self.assertTrue((original / 'interactive/raw.json').is_file())
        with patch('bench.mixed.execute_group', side_effect=self.group):
            state = matrix_campaign(config, self.root / 'run', 'aiperf', True)
        self.assertEqual(len(state['completed']), 6)
        self.assertIn('attempt-002', state['completed'][0])
        self.assertTrue(original.exists())

    def test_goal_miss_retained_across_resume_and_repeats_finish(self):
        config = self.save()
        def missed(*args, **kw):
            result = self.group(*args, **kw)
            result['outcome'] = 'goal_not_met'
            return result
        with patch('bench.mixed.execute_group', side_effect=missed):
            state = matrix_campaign(config, self.root / 'run', 'aiperf')
        self.assertEqual(len(state['completed']), 3)
        self.assertEqual(state['next_row'], 1)
        with patch('bench.mixed.execute_group', side_effect=self.group):
            state = matrix_campaign(config, self.root / 'run', 'aiperf', True)
        self.assertEqual(state['status'], 'goal_not_met')
        self.assertEqual(len(state['completed']), 12)

    def test_evidence_and_config_tampering_refuse_resume(self):
        config = self.save()
        with patch('bench.mixed.execute_group', side_effect=self.group):
            matrix_campaign(config, self.root / 'run', 'aiperf')
        changed = copy.deepcopy(config)
        changed['repeats'] = 2
        with self.assertRaisesRegex(ValueError, 'changed'):
            matrix_campaign(changed, self.root / 'run', 'aiperf', True)
        (self.root / 'run/row-001-repeat-01-attempt-001/interactive/raw.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            matrix_campaign(config, self.root / 'run', 'aiperf', True)

    def test_profile_mismatch_sends_no_traffic_and_preserves_observation(self):
        (self.root / 'expected.json').write_text('{"limit":2}')
        self.spec['profiles'] = {'limit-two': {'expected': 'expected.json', 'observe': [sys.executable, '-c', 'print(\'{"limit":1}\')']}}
        self.spec['stages'][0].update(kind='policy', profile='limit-two')
        config = self.save()
        with patch('bench.mixed.execute_group', side_effect=AssertionError('traffic')):
            state = matrix_campaign(config, self.root / 'run', 'aiperf')
        self.assertEqual(state['status'], 'profile_mismatch')
        self.assertEqual(state['completed'], [])
        actual = json.loads((self.root / 'run/row-001-repeat-01-attempt-001/profile-before/observed.json').read_text())
        self.assertEqual(actual, {'limit': 1})

    def test_changed_profile_after_group_invalidates_whole_repeat(self):
        (self.root / 'expected.json').write_text('{}')
        self.spec['profiles'] = {'fixed': {'expected': 'expected.json', 'observe': [sys.executable, '-c', 'print("{}")']}}
        self.spec['stages'][0]['profile'] = 'fixed'
        config = self.save()
        with patch('bench.matrix.observe', side_effect=[True, False]), patch('bench.mixed.execute_group', side_effect=self.group):
            state = matrix_campaign(config, self.root / 'run', 'aiperf')
        self.assertEqual(state['status'], 'evidence_invalid')
        self.assertEqual(state['completed'], [])

    def test_invalid_policy_stage_and_expansion_fail_before_writes(self):
        self.spec['stages'][0]['kind'] = 'policy'
        with self.assertRaisesRegex(ValueError, 'observed serving profile'):
            self.save()
        self.spec['stages'][0].pop('kind')
        self.spec['stages'][0]['streams']['interactive']['load']['rates'] = list(range(1,1002))
        with self.assertRaisesRegex(ValueError, '1000'):
            self.save()

    def test_duplicate_path_names_rejected(self):
        self.spec['stages'][1]['id'] = self.spec['stages'][0]['id']
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.save()
        self.spec['stages'][1]['id'] = '../escape'
        with self.assertRaisesRegex(ValueError, 'Names'):
            self.save()

    def test_running_checkpoint_refuses_blind_replay(self):
        config = self.save()
        with patch('bench.mixed.execute_group', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                matrix_campaign(config, self.root / 'run', 'aiperf')
        with self.assertRaisesRegex(ValueError, 'owner vanished'):
            matrix_campaign(config, self.root / 'run', 'aiperf', True)

    def test_attempt_limit_includes_failed_groups(self):
        self.spec['max_attempts_per_repeat'] = 1
        config = self.save()
        def failed(*args, **kw):
            result = self.group(*args, **kw)
            result['evidence'] = 'invalid'
            return result
        with patch('bench.mixed.execute_group', side_effect=failed):
            matrix_campaign(config, self.root / 'run', 'aiperf')
        with patch('bench.mixed.execute_group', side_effect=AssertionError('replay')):
            state = matrix_campaign(config, self.root / 'run', 'aiperf', True)
        self.assertEqual(state['status'], 'attempt_budget_exhausted')

    def test_pause_finishes_group_and_resume_skips_it(self):
        config = self.save()
        def paused(*args, **kw):
            result = self.group(*args, **kw)
            (self.root / 'run/pause-request.json').write_text('{}')
            return result
        with patch('bench.mixed.execute_group', side_effect=paused):
            state = matrix_campaign(config, self.root / 'run', 'aiperf')
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(len(state['completed']), 1)
        with patch('bench.mixed.execute_group', side_effect=self.group):
            state = matrix_campaign(config, self.root / 'run', 'aiperf', True)
        self.assertEqual(len(state['completed']), 12)
        self.assertEqual(len(self.calls), 12)

    def test_profile_comparison_preserves_json_types(self):
        profile = {'observe': [sys.executable, '-c', 'print(\'{"enabled":1}\')'],
                   'expected': {'enabled': True}, 'deadline_seconds': 5}
        with (self.root / '.lock').open('a') as lock:
            self.assertFalse(observe(profile, self.root / 'observed', lock.fileno()))

    def test_rejected_concurrent_owner_writes_no_ledger(self):
        import fcntl
        config = self.save()
        run = self.root / 'run'
        run.mkdir()
        with (run / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'still owns'):
                matrix_campaign(config, run, 'aiperf', True)
        self.assertFalse((run / 'provenance.jsonl').exists())
