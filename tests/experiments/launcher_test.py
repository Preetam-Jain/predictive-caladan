"""Offline tests: no real SSH connections, workload binaries, or privileged actions."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


launcher = module('experiment_launcher', ROOT / 'scripts/experiments/run.py')
remote = module('experiment_remote', ROOT / 'scripts/experiments/remote.py')


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.config = launcher.load_config(launcher.TEMPLATE)

    def test_strict_config(self):
        with self.assertRaisesRegex(ValueError, 'unknown keys'):
            launcher.merge(self.config, {'conditions': {'memcached_alnoe': True}})
        with self.assertRaisesRegex(ValueError, 'bool'):
            launcher.merge(self.config, {'conditions': {'memcached_alone': 'true'}})
        c = copy.deepcopy(self.config)
        c['suite']['load_fractions'] = [float('nan')]
        with self.assertRaises(ValueError):
            launcher.validate_config(c)

    def test_paired_plan_and_seed(self):
        self.config["conditions"] = {name: True for name in launcher.CONDITIONS}
        runs = launcher.plan(self.config, 1000000)
        self.assertEqual(len(runs), 54)
        self.assertEqual(runs, launcher.plan(self.config, 1000000))
        for offset in range(0, len(runs), 2):
            off, on = runs[offset:offset + 2]
            self.assertEqual({off['logging'], on['logging']}, {False, True})
            for key in ('condition', 'rps', 'fraction', 'repetition'):
                self.assertEqual(off[key], on[key])

    def test_loadgen_warmup_silent_and_configuration(self):
        c = self.config
        argv = launcher.client_command(c, c['nodes']['client'], '/tmp/run', 500000, 30, 120)
        self.assertEqual(argv[argv.index('--loadshift') + 1], '500000:30000000:silent,500000:120000000')
        for logging in (False, True):
            argv = launcher.iokernel_command(c, c['nodes']['server'], '/tmp/run', True, logging)
            index = argv.index('telemetry')
            self.assertEqual(argv[index + 1], str(c['allocation']['writer_cpu']))
            self.assertEqual(argv[index + 2], '/tmp/run/capture' if logging else '-')
        self.assertIn('runtime_guaranteed_kthreads 4', launcher.runtime_config(c, 'antagonist'))
        c['network']['directpath'] = True
        self.assertIn('enable_directpath 1\n', launcher.runtime_config(c, 'memcached'))
        self.assertIn('enable_directpath 1\n', launcher.runtime_config(c, 'client'))
        c['allocation']['use_smt'] = False
        c['allocation']['memcached_cores'] = c['allocation']['memcached_guaranteed'] = 1
        launcher.validate_config(c)
        self.assertIn('noht', launcher.iokernel_command(c, c['nodes']['server'], '/tmp/run', True, False))

    def test_summaries_and_overload(self):
        line = 'zero, 1000, 990, 10, 100.0, 1, 2, 3, 4, 5, 6, 10, 1700000000, 1234567890123456789\n'
        row = launcher.summaries(line)[0]
        self.assertEqual(row['p99_9_us'], 5)
        self.assertEqual(row['first_server_tsc'], 1234567890123456789)
        # Server degradation is an outcome; client inability to offer load is invalid.
        self.assertEqual(launcher.quality(row, 1000, self.config['validation']), [])
        row['sent_rps'] = 500
        self.assertTrue(launcher.quality(row, 1000, self.config['validation']))

    def test_calibration_search(self):
        c = copy.deepcopy(self.config)
        c['calibration'].update(start_rps=100, maximum_rps=800, confirmations=2, refinement_steps=4)
        def fake_run(c, directory, run, *args):
            rate = run['rps']
            return {'summary': dict(sent_rps=rate, response_rps=rate, drops=0,
                                   schedule_adherence_percent=100, p99_9_us=50 if rate <= 350 else 200)}
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, 'run_one', fake_run):
            value = launcher.calibrate(c, Path(directory))
        self.assertGreaterEqual(value['capacity_rps'], 337)
        self.assertLessEqual(value['capacity_rps'], 350)
        self.assertGreater(value['failing_rps'], 350)

    def test_batch_rates_use_real_frequency_and_skip_first_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'iokernel.log').write_text('time: detected 2400 ticks / us\n')
            (path / 'batch-throughput.log').write_text('123 999 100\n123 10 2400000100\n123 20 4800000100\n')
            rate = launcher.batch_throughput(path, 100, 2)
            self.assertEqual(rate, 15)

    def test_logging_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for logging, latency, throughput in ((False, 10, 1000), (True, 12, 900)):
                run = dict(id=str(logging), condition='memcached_alone', fraction=.7, repetition=1, logging=logging)
                launcher.save(root / str(logging) / 'latest.json', dict(state='complete', run=run,
                              summary=dict(p99_9_us=latency, response_rps=throughput)))
            launcher.report(root, self.config)
            comparison = json.loads((root / 'logging-comparison.json').read_text())[0]
            self.assertAlmostEqual(comparison['p99_9_increase_percent'], 20)
            self.assertTrue(comparison['flagged'])


class RemoteTest(unittest.TestCase):
    def test_lock_and_owned_process_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'node/suite/run'
            request = dict(root=str(root), token='test')
            remote.main('init', request)
            with self.assertRaisesRegex(RuntimeError, 'Another run'):
                remote.main('init', dict(root=str(root.parent / 'other'), token='other'))
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            try:
                supervisors = []
                real_popen = subprocess.Popen
                def spawn(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    supervisors.append(process)
                    return process
                with patch.object(remote.subprocess, 'Popen', side_effect=spawn):
                    remote.main('start', dict(request, label='owned', argv=[sys.executable, '-c',
                                       'import time; print("ready",flush=True); time.sleep(60)'],
                                       sudo=False, cwd=directory, timeout=60))
                remote.main('stop', request)
                for process in supervisors:
                    process.wait(timeout=10)
                state = remote.main('status', dict(request, label='owned'))
                self.assertIn('returncode', state)
                self.assertIsNone(unrelated.poll())
                manifest = remote.main('manifest', request)
                self.assertIn('owned.log', manifest)
                remote.main('release', request)
                self.assertTrue(root.exists())
            finally:
                unrelated.terminate()
                unrelated.wait()


class OfflineSSHIntegrationTest(unittest.TestCase):
    def test_complete_two_node_run_and_verified_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / 'tools'
            tools.mkdir()
            # The offline fixture tests orchestration without changing host NUMA policy.
            numactl = tools / 'numactl'
            numactl.write_text('#!/bin/sh\nshift\nshift\nexec "$@"\n')
            numactl.chmod(0o755)
            ssh = tools / 'ssh'
            ssh.write_text('#!/usr/bin/env python3\nimport os,sys\nos.execvp("sh",["sh","-c",sys.argv[-1]])\n')
            ssh.chmod(0o755)
            c = launcher.load_config(launcher.TEMPLATE)
            c['logging'].update(capture_seconds=15, finalize_timeout_seconds=10)
            c['suite'].update(warmup_seconds=0, measurement_seconds=1, client_timeout_seconds=30,
                              startup_timeout_seconds=15, minimum_free_gb=1)
            fake_iokernel = r'''#!/usr/bin/env python3
import os,sys,time,struct
print('time: detected 1000 ticks / us',flush=True)
print('main: core 0 running dataplane.',flush=True)
if 'telemetry' in sys.argv:
 i=sys.argv.index('telemetry'); prefix=sys.argv[i+2]; seconds=int(sys.argv[i+3])
 if prefix != '-':
  layouts=[('bw',16),('llc',64),('pmc-failure',24),('sched',24),('queue',64),('task',40),('delay',56)]
  def header(size,kind,complete):
   return struct.pack('<8sIIIIdQIIIIdQQ16s',b'CALATLM\0',8,96,size,kind,1000.,0,4,0,0,complete,1.,int(complete),0,b'x'*16)
  for kind,(suffix,size) in enumerate(layouts,1):
   open(prefix+'.'+suffix+'.bin','wb').write(header(size,kind,0))
  time.sleep(seconds)
  for kind,(suffix,size) in enumerate(layouts,1):
   open(prefix+'.'+suffix+'.bin','wb').write(header(size,kind,1)+bytes(size))
  print('telemetry: all streams drained and closed',flush=True)
while True: time.sleep(1)
'''
            # The raw script above must contain a NUL byte in the packed header, not a literal backslash.
            fake_iokernel = fake_iokernel.replace("b'CALATLM\\\\0'", "b'CALATLM\\0'")
            fake_client = '''#!/usr/bin/env python3
import sys,time
rate=int(sys.argv[sys.argv.index('--loadshift')+1].split(',')[-1].split(':')[0])
if sys.argv[sys.argv.index('--nvalues')+1] == '2':
 print('simulated client failure',flush=True); sys.exit(9)
print('zero, %d, %d, 0, 100, 1, 2, 3, 4, 5, 6, 10, 1700000000, 1000' % (rate,rate),flush=True)
'''
            for role in ('server', 'client'):
                repo = root / role / 'repo'
                artifact = root / role / 'artifact'
                repo.mkdir(parents=True)
                (repo / 'apps/loadgen/target/release').mkdir(parents=True)
                (artifact / 'memcached').mkdir(parents=True)
                for path, content in ((repo / 'iokerneld', fake_iokernel),
                                      (repo / 'apps/loadgen/target/release/loadgen', fake_client),
                                      (artifact / 'memcached/memcached', '#!/usr/bin/env python3\nimport time\nwhile True: time.sleep(1)\n')):
                    path.write_text(content)
                    path.chmod(0o755)
                c['nodes'][role].update(host=role, user='test', repo=str(repo), artifact=str(artifact),
                                         remote_results=str(root / role / 'results'), sudo=False)
            suite = root / 'suite'
            suite.mkdir()
            run = dict(id='one', condition='memcached_alone', logging=True, rps=1000, fraction=.5, repetition=1)
            with patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH']):
                record = launcher.run_one(c, suite, run)
            self.assertEqual(record['state'], 'complete')
            attempt = suite / 'one/attempt-001'
            self.assertTrue((attempt / 'server/capture.bw.bin').exists())
            self.assertTrue((attempt / 'client/transfer-manifest.json').exists())
            self.assertFalse((root / 'server/results/.launcher-lock.json').exists())
            # Failure still collects logs from both nodes and releases only owned locks.
            c['memcached']['nvalues'] = 2
            failed = dict(run, id='failed', logging=False)
            with patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH']):
                with self.assertRaisesRegex(RuntimeError, 'loadgen failed'):
                    launcher.run_one(c, suite, failed)
            failed_dir = suite / 'failed/attempt-001'
            self.assertIn('simulated client failure', (failed_dir / 'client/loadgen.log').read_text())
            self.assertTrue((failed_dir / 'server/transfer-manifest.json').exists())
            self.assertEqual(json.loads((failed_dir / 'run.json').read_text())['state'], 'failed')
            self.assertFalse((root / 'server/results/.launcher-lock.json').exists())



if __name__ == '__main__':
    unittest.main()
