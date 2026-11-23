"""Search decisions and checkpoint recovery without SSH or real workloads."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/experiments'))
spec = importlib.util.spec_from_file_location('peak_search', ROOT / 'scripts/experiments/peak_search.py')
search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(search)


def sample(rate, latency=20, send_fraction=1, drops=0):
    return dict(sent_rps=rate*send_fraction, response_rps=rate*send_fraction,
                schedule_adherence_percent=100, p99_9_us=latency, drops=drops)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.settings = tomllib.loads(search.TEMPLATE.read_text())

    def trial(self, rate, **kwargs):
        return dict(cores=1, rps=rate, summary=sample(rate, **kwargs), phase='test')

    def test_joint_search_and_confirmations(self):
        trials = [self.trial(10000)]
        for _ in range(60):
            choice = search.next_trial(trials, self.settings)
            if choice is None:
                break
            rate, phase = choice
            latency = 30 if rate <= 100000 else 75 if rate <= 200000 else 150
            trials.append(self.trial(rate, latency=latency))
        else:
            self.fail('Search did not terminate')
        rows = search.conclusions(trials, self.settings)
        for row, cap in zip(rows, (100000, 200000)):
            self.assertTrue(row['confirmed'])
            self.assertEqual(row['status'], 'bracketed')
            self.assertLessEqual(row['highest_passing_rps'], cap)
            self.assertGreater(row['upper_rps'], cap)
            self.assertLessEqual((row['upper_rps']-row['highest_passing_rps'])/row['highest_passing_rps'], .05)

    def test_seed_is_reused(self):
        trials = [self.trial(10000)]
        self.assertEqual(search.next_trial(trials, self.settings), (20000, 'exponential'))

    def test_client_limit_is_not_server_failure(self):
        self.assertEqual(search.classify(sample(20000, send_fraction=.5), 20000, 50, self.settings), 'client_limited')
        trials = [self.trial(10000) for _ in range(3)] + [self.trial(10500, send_fraction=.5)]
        self.assertEqual(search.target_result(trials, 50, self.settings)['status'], 'client_limited')

    def test_backpressure_is_distinct_from_client_cpu_limit(self):
        summary = sample(20000, send_fraction=.5)
        summary['send_backpressure'] = True
        self.assertEqual(search.classify(summary,20000,50,self.settings),'backpressure')
        summary['schedule_adherence_percent'] = 90
        self.assertEqual(search.classify(summary,20000,50,self.settings),'client_limited')

    def test_backpressure_bracket_is_qualified(self):
        trials = [self.trial(10000) for _ in range(3)]
        bad = self.trial(10500,send_fraction=.5)
        bad['summary']['send_backpressure'] = True
        trials.append(bad)
        result = search.target_result(trials,50,self.settings)
        self.assertEqual(result['status'],'backpressure_limited')
        self.assertEqual(result['upper_kind'],'backpressure')

    def test_mixed_results_are_not_confirmed(self):
        trials = [self.trial(10000), self.trial(10000, latency=60)]
        result = search.target_result(trials, 50, self.settings)
        self.assertIsNone(result['highest_passing_rps'])
        self.assertEqual(result['inconsistent_rates'], [10000])

    def test_failed_50us_seed_does_not_prevent_higher_probe(self):
        trials = [self.trial(10000, latency=59)]
        self.assertEqual(search.next_trial(trials, self.settings), (20000, 'exponential'))
        trials.append(self.trial(20000, latency=110))
        self.assertEqual(search.next_trial(trials, self.settings), (5000, 'lower-load-probe'))
        trials.append(self.trial(30000, latency=40))
        self.assertTrue(search.target_result(trials, 50, self.settings)['nonmonotonic_observations'])

    def test_no_pass_is_reported_as_observation_not_proof(self):
        self.settings['maximum_rps'] = 10000
        trials = [self.trial(r, latency=150) for r in (10000,5000,2500,1250,1000)]
        self.assertIsNone(search.next_trial(trials, self.settings))
        self.assertTrue(all(r['status']=='no_passing_rate_observed' for r in search.conclusions(trials,self.settings)))

    def test_search_ceiling_requires_confirmation(self):
        self.settings['maximum_rps'] = 10000
        trials = [self.trial(10000)]
        self.assertEqual(search.next_trial(trials,self.settings),(10000,'confirmation'))
        trials *= 3
        self.assertIsNone(search.next_trial(trials,self.settings))
        self.assertEqual(search.target_result(trials,50,self.settings)['status'],'search_ceiling_reached')

    def test_completed_pending_trial_is_imported_without_rerun(self):
        base=search.launcher.load_config(search.launcher.TEMPLATE)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            state=dict(trials=[],pending=dict(cores=1,rps=20000,phase='exponential',id='test'))
            path=root/'test/latest.json';path.parent.mkdir()
            path.write_text(json.dumps(dict(state='complete',summary=sample(20000))))
            with patch.object(search.launcher,'run_one',side_effect=AssertionError('Should not rerun')):
                search.execute_pending(root,state,base,self.settings)
            self.assertIsNone(state['pending'])
            self.assertEqual(len(state['trials']),1)

    def test_infrastructure_failure_is_bounded_and_not_latency_data(self):
        base=search.launcher.load_config(search.launcher.TEMPLATE)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            state=dict(trials=[],pending=dict(cores=1,rps=20000,phase='exponential',id='test'))
            with patch.object(search.launcher,'run_one',side_effect=RuntimeError('SSH down')) as run:
                with self.assertRaises(RuntimeError):search.execute_pending(root,state,base,self.settings)
            self.assertEqual(run.call_count,2)
            self.assertEqual(state['trials'],[])
            self.assertIsNotNone(state['pending'])


if __name__ == '__main__':
    unittest.main()
