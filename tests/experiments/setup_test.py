"""Setup orchestration tests with fake nodes; never installs packages or contacts hosts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


launcher = load('launcher_for_setup_test', ROOT / 'scripts/experiments/run.py')
setup = load('setup_for_test', ROOT / 'scripts/experiments/setup_suite.py')


class FakeNode:
    instances = []
    failure = None

    def __init__(self, config, suite_id, run_id, token):
        self.config = config
        self.root = config['remote_results'] + '/' + suite_id + '/' + run_id
        self.token = token
        self.initialized = False
        self.calls = []
        self.__class__.instances.append(self)

    def bootstrap(self):
        self.initialized = True

    def rpc(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == 'setup-info':
            return dict(home='/home/preetam', kernel='test', free_bytes=1000000000)
        return True

    def wait(self, label, timeout):
        if self.failure and self.failure in label:
            raise RuntimeError('simulated setup failure: ' + label)
        return dict(returncode=0)

    def collect(self, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'setup.log').write_text('captured setup output')
        self.calls.append(('collect', {}))


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.config = launcher.load_config(launcher.TEMPLATE)
        self.config['setup']['deploy_code'] = False
        FakeNode.instances = []
        FakeNode.failure = None
        self.smoke = []
        self.context = SimpleNamespace(Node=FakeNode, REPO=ROOT, CONDITIONS=launcher.CONDITIONS,
                                       save=launcher.save, say=lambda text: None,
                                       run_one=lambda *args: self.smoke.append(args))

    def test_order_and_runtime_workload_paths(self):
        stages = setup.steps(self.config, 'server')
        names = [name for name, _ in stages]
        self.assertLess(names.index('build-runtime'), names.index('configure-machine'))
        self.assertLess(names.index('build-runtime'), names.index('build-workloads'))
        self.assertLess(names.index('configure-machine'), names.index('verify-machine'))
        argv = dict(stages)['build-workloads']
        self.assertIn('--swaptions-gc', argv)
        self.assertIn(self.config['nodes']['server']['artifact'], argv)
        self.assertNotIn('build-workloads', dict(setup.steps(self.config, 'client')))

    def test_toggles_do_not_run_disabled_steps(self):
        for key in self.config['setup']:
            if type(self.config['setup'][key]) is bool:
                self.config['setup'][key] = False
        self.assertEqual([name for name, _ in setup.steps(self.config, 'server')], ['inventory'])

    def test_success_collects_both_nodes_and_only_smoke_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory)
            setup.run_setup(self.config, suite, self.context)
            report = json.loads((suite / 'setup/complete.json').read_text())
            self.assertEqual(report['state'], 'complete')
            self.assertEqual(len(self.smoke), 3)
            for args in self.smoke:
                self.assertFalse(args[2]['logging'])
                self.assertEqual(args[4], self.config['setup']['smoke_seconds'])
                self.assertEqual(args[0]['memcached']['nvalues'], 1000)
            for node in FakeNode.instances:
                self.assertEqual([name for name, _ in node.calls][-3:], ['setup-logs', 'collect', 'release'])
            self.assertFalse((suite / 'calibration.json').exists())
            setup.run_setup(self.config, suite, self.context)
            self.assertEqual(len(FakeNode.instances), 2)  # Completed setup is not rebuilt on resume.

    def test_failure_stops_before_smoke_and_preserves_logs(self):
        FakeNode.failure = 'build-workloads'
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'Collected logs'):
                setup.run_setup(self.config, suite, self.context)
            self.assertFalse(self.smoke)
            self.assertFalse((suite / 'setup/complete.json').exists())
            self.assertTrue((suite / 'setup/attempt-001/server/setup.log').exists())
            report = json.loads((suite / 'setup/attempt-001/setup.json').read_text())
            self.assertEqual(report['state'], 'failed')
            self.assertEqual(FakeNode.instances[0].calls[-1][0], 'release')

    def test_gc_incompatibility_fails_before_packages_or_artifact_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / 'tools'
            tools.mkdir()
            (root / '.git').mkdir()
            (root / 'runtime').mkdir()
            (root / 'runtime/cfg.c').write_text('/* no GC integration */\n')
            git = tools / 'git'
            git.write_text('#!/bin/sh\ncase "$*" in\n*--show-toplevel*) echo "$FIXTURE_ROOT";;\n*--absolute-git-dir*) echo "$FIXTURE_ROOT/.git";;\n*) exit 99;;\nesac\n')
            git.chmod(0o755)
            pgrep = tools / 'pgrep'
            pgrep.write_text('#!/bin/sh\nexit 1\n')
            pgrep.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'], FIXTURE_ROOT=str(root))
            result = subprocess.run(['bash', str(ROOT / 'scripts/cloudlab/build-workloads.sh'),
                                     '--artifact', str(root / 'artifact'), '--swaptions-gc', '--install-deps'],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('BLOCKED:', result.stdout + result.stderr)
            self.assertFalse((root / 'artifact').exists())
            self.assertTrue(list((root / '.git/cloudlab-setup').glob('workloads-*.log')))

    def test_deployment_rejects_invalid_port_without_ssh(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/cloudlab/deploy.sh'), '--port', '0', 'user@host'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid SSH port', result.stderr)


if __name__ == '__main__':
    unittest.main()
