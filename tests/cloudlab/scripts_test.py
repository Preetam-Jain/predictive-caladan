"""Exercise deployment/setup locally; never opens SSH or installs packages."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts/cloudlab'


class ScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.remote = self.root / 'remote'
        self.tools = self.root / 'tools'
        for path in (self.source, self.remote, self.tools):
            path.mkdir()
        shutil.copytree(SCRIPTS, self.source / 'scripts/cloudlab')
        self.run_command(['git', 'init', '-q', str(self.source)])
        self.run_command(['git', '-C', str(self.source), 'config', 'user.name', 'Test'])
        self.run_command(['git', '-C', str(self.source), 'config', 'user.email', 'test@example.invalid'])
        self.environment = dict(os.environ, HOME=str(self.remote),
                                PATH=f'{self.tools}:{os.environ["PATH"]}',
                                CALL_LOG=str(self.root / 'calls'))
        self.tool('ssh', 'shift\nexec "$@"\n')
        self.tool('scp', 'destination=${!#}\nfor source in "${@:1:$#-1}"; do\n'
                         '  cp "$source" "${destination#*:}"\ndone\n')
        self.tool('apt-get', 'printf "apt %s\\n" "$*" >> "$CALL_LOG"\n')
        self.tool('sudo', 'exec "$@"\n')
        self.tool('make', 'printf "make %s\\n" "$*" >> "$CALL_LOG"\n'
                          'exit "${FAIL_BUILD:-0}"\n')

    def tool(self, name, body):
        path = self.tools / name
        path.write_text('#!/usr/bin/env bash\nset -eu\n' + body)
        path.chmod(0o755)

    def run_command(self, arguments, **kwargs):
        return subprocess.run(arguments, check=True, capture_output=True, text=True, **kwargs)

    def script(self, name, *arguments, **extra_environment):
        return subprocess.run([str(self.source / 'scripts/cloudlab' / name), *arguments],
                              env=dict(self.environment, **extra_environment),
                              capture_output=True, text=True, timeout=30)

    def test_deploy_preserves_diff_and_untracked_files(self):
        (self.source / 'tracked').write_text('before\n')
        (self.source / 'removed').write_text('delete me\n')
        (self.source / '.gitignore').write_text('ignored\n')
        self.run_command(['git', '-C', str(self.source), 'add', '.'])
        self.run_command(['git', '-C', str(self.source), 'commit', '-qm', 'baseline'])
        baseline = self.run_command(['git', '-C', str(self.source), 'rev-parse', 'HEAD']).stdout
        (self.source / 'tracked').write_text('after\n')
        (self.source / 'removed').unlink()
        (self.source / 'new file.c').write_text('new source\n')
        (self.source / 'ignored').write_text('not deployed\n')
        (self.source / 'paper.pdf').write_text('not deployed\n')
        result = self.script('deploy.sh', 'user@fake')
        self.assertEqual(result.returncode, 0, result.stderr)
        deployed = self.remote / 'predictive-caladan'
        self.assertEqual((deployed / 'tracked').read_text(), 'after\n')
        self.assertFalse((deployed / 'removed').exists())
        self.assertEqual((deployed / 'new file.c').read_text(), 'new source\n')
        self.assertFalse((deployed / 'ignored').exists())
        self.assertFalse((deployed / 'paper.pdf').exists())
        remote_diff = self.run_command(['git', '-C', str(deployed), 'diff', '--binary', 'HEAD']).stdout
        local_diff = self.run_command(['git', '-C', str(self.source), 'diff', '--binary', 'HEAD']).stdout
        self.assertEqual(remote_diff, local_diff)
        self.assertEqual(self.run_command(['git', '-C', str(self.source), 'rev-parse', 'HEAD']).stdout, baseline)
        result = self.script('deploy.sh', 'user@fake')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Destination already exists', result.stderr)

    def test_setup_skips_successful_submodule_repeat(self):
        for _ in range(2):
            result = self.script('setup.sh')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertEqual(calls.count('make submodules'), 1)

    def test_failed_submodules_are_not_retried(self):
        first = self.script('setup.sh', '--skip-packages', FAIL_BUILD='7')
        self.assertNotEqual(first.returncode, 0)
        second = self.script('setup.sh', '--skip-packages')
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('Previous submodule setup did not complete', second.stdout)
        self.assertEqual((self.root / 'calls').read_text().splitlines().count('make submodules'), 1)

    def test_build_runs_both_stages(self):
        (self.source / 'ksched').mkdir()
        self.tool('make', 'printf "make %s in %s\\n" "$*" "$PWD" >> "$CALL_LOG"\n')
        result = self.script('build.sh', '--jobs', '2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'calls').read_text().splitlines(),
                         [f'make -j2 in {self.source}',
                          f'make -j2 in {self.source / "ksched"}'])

    def test_build_stops_after_failure(self):
        result = self.script('build.sh', FAIL_BUILD='7')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 1)

    def test_machine_setup_requires_built_module(self):
        self.tool('pgrep', 'exit 1\n')
        result = self.script('machine-setup.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing ksched/ksched.ko', result.stdout)
        self.assertFalse((self.root / 'calls').exists())

    def test_loadgen_build_uses_existing_lockfile(self):
        for library in ('libbase.a', 'libnet.a', 'libruntime.a'):
            (self.source / library).touch()
        application = self.source / 'apps/loadgen'
        application.mkdir(parents=True)
        (application / 'Cargo.lock').touch()
        self.tool('rustup', 'exit 0\n')
        self.tool('rustc', 'printf "mock rustc\n"\n')
        self.tool('cargo', 'printf "cargo %s\n" "$*" >> "$CALL_LOG"\n')
        result = self.script('build-loadgen.sh', CARGO_HOME=str(self.root / 'cargo'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('cargo build --release --locked', (self.root / 'calls').read_text())

    def test_smoke_configs_and_commands(self):
        result = self.script('smoke.sh', 'configure')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.source / '.git/cloudlab-smoke-dpdk/server.config'
        self.assertIn('host_addr 192.168.1.101', config.read_text())
        self.assertNotIn('enable_directpath', config.read_text())
        client_config = self.source / '.git/cloudlab-smoke-dpdk/client.config'
        self.assertIn('runtime_spinning_kthreads 4', client_config.read_text())
        self.assertNotIn('runtime_spinning_kthreads', config.read_text())
        # Migrate only the exact previously generated client template.
        client_config.write_text(client_config.read_text().replace('runtime_spinning_kthreads 4\n', ''))
        result = self.script('smoke.sh', 'configure')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('runtime_spinning_kthreads 4', client_config.read_text())
        for arguments, expected in (
                (('iokernel', 'node0'), 'telemetry 4 - 300'),
                (('iokernel', 'node1'), 'nobw'),
                (('server',), '--mode spawner-server'),
                (('client',), '--mpps 0.02 --samples 1')):
            result = self.script('smoke.sh', '--dry-run', *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)
        result = self.script('smoke.sh', 'configure', NETWORK_MODE='directpath')
        self.assertEqual(result.returncode, 0, result.stderr)
        native_config = self.source / '.git/cloudlab-smoke/server.config'
        self.assertIn('enable_directpath', native_config.read_text())
        self.assertNotIn('enable_directpath', config.read_text())
        result = self.script('smoke.sh', '--dry-run', 'server')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cloudlab-smoke-dpdk/server.config', result.stdout)
        config.write_text('hand edited config\n')
        result = self.script('smoke.sh', 'configure')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config.read_text(), 'hand edited config\n')
        self.assertFalse((self.root / 'calls').exists())

    def test_capture_commands(self):
        result = self.script('smoke.sh', '--dry-run', 'capture', '/tmp/capture-test', '120')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('telemetry 4 /tmp/capture-test 120', result.stdout)
        result = self.script('smoke.sh', '--dry-run', 'capture')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(' 180', result.stdout)
        for invalid in ('0', '-1', '86401', '1;false'):
            result = self.script('smoke.sh', '--dry-run', 'capture', '/tmp/test', invalid)
            self.assertNotEqual(result.returncode, 0)
        self.tool('python3', 'printf "decode %s\n" "$*" >> "$CALL_LOG"\n')
        result = self.script('smoke.sh', 'check-capture', '/tmp/capture-test')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn('--check-prefix /tmp/capture-test', calls[0])

    def test_client_duration(self):
        for arguments, seconds in (((), 120), (('10',), 10), (('120',), 120)):
            result = self.script('smoke.sh', '--dry-run', 'client', *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'--runtime {seconds} --rampup 1', result.stdout)
        for invalid in ('0', '-1', '86401', 'hello'):
            result = self.script('smoke.sh', '--dry-run', 'client', invalid)
            self.assertNotEqual(result.returncode, 0)

    def test_shell_syntax_and_help(self):
        for script in SCRIPTS.glob('*.sh'):
            self.run_command(['bash', '-n', str(script)])
            result = self.script(script.name, '--help')
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
