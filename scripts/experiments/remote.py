#!/usr/bin/env python3
"""Private SSH worker for run.py; all operations are restricted to one owned run."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def proc_start(pid):
    try:
        return Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def terminate(state):
    pid = state.get('pid')
    if not pid or proc_start(pid) != state.get('proc_start'):
        return
    prefix = ['sudo', '-n'] if state['sudo'] else []
    subprocess.run(prefix + ['/bin/kill', '-TERM', '--', '-%d' % pid],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if proc_start(pid) != state['proc_start']:
            return
        time.sleep(.1)
    subprocess.run(prefix + ['/bin/kill', '-KILL', '--', '-%d' % pid],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def worker(path):
    spec = json.loads(Path(path).read_text())
    state_path = Path(path).with_suffix('.status.json')
    if spec['sudo']:
        argv = ['sudo', '-n', '--', 'env'] + [key + '=' + value for key, value in spec.get('env', {}).items()] + spec['argv']
    else:
        argv = spec['argv']
    state = dict(label=spec['label'], argv=argv, sudo=spec['sudo'], started_unix=time.time())
    try:
        with open(spec['log'], 'wb') as log:
            process = subprocess.Popen(argv, cwd=spec['cwd'],
                                       env=dict(os.environ, **spec.get('env', {})),
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
            state.update(pid=process.pid, proc_start=proc_start(process.pid))
            save(state_path, state)
            try:
                state['returncode'] = process.wait(timeout=spec['timeout'])
            except subprocess.TimeoutExpired:
                terminate(state)
                process.wait()
                state.update(returncode=124, timed_out=True)
    except Exception as exc:
        state.update(returncode=127, error=str(exc))
    state['finished_unix'] = time.time()
    save(state_path, state)


def manifest(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or '.status.json.tmp' in path.name:
            continue
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        result[str(path.relative_to(root))] = dict(size=path.stat().st_size, sha256=digest.hexdigest())
    return result


def main(action, request):
    root = Path(request['root']).resolve()
    lock = root.parent.parent / '.launcher-lock.json'
    if action == 'init':
        root.parent.mkdir(parents=True, exist_ok=True)
        owner = {'token': request['token'], 'root': str(root)}
        try:
            with lock.open('x') as output:
                json.dump(owner, output)
        except FileExistsError:
            if json.loads(lock.read_text()) != owner:
                raise RuntimeError('Another run owns %s; do not delete this lock while it is running' % lock)
        root.mkdir(exist_ok=True)
        return owner
    if not lock.exists() or json.loads(lock.read_text()) != {'token': request['token'], 'root': str(root)}:
        raise RuntimeError('Run ownership check failed')
    if action == 'setup-info':
        import shutil
        for name in ('bash', 'git', 'tar', 'pgrep'):
            if not shutil.which(name):
                raise RuntimeError('Missing setup prerequisite: ' + name)
        if request['sudo']:
            subprocess.run(['sudo', '-n', 'true'], check=True, timeout=10)
        if subprocess.run(['pgrep', '-x', 'iokerneld'], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('Stop the running IOKernel before setup; it will not be killed automatically')
        return dict(home=os.environ['HOME'], kernel=os.uname().release,
                    free_bytes=shutil.disk_usage(root).free, hostname=os.uname().nodename)
    if action == 'setup-logs':
        import shutil
        repo = Path(request['repo'])
        process = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--absolute-git-dir'],
                                 capture_output=True, text=True)
        if process.returncode == 0:
            git_dir = Path(process.stdout.strip())
            for name in ('cloudlab-setup', 'cloudlab-deploy'):
                source = git_dir / name
                if source.is_dir():
                    shutil.copytree(source, root / name, dirs_exist_ok=True)
        if (repo / 'build/config').exists():
            shutil.copy2(repo / 'build/config', root / 'build.config')
        return True
    if action == 'preflight':
        import shutil
        missing = [p for p in request['executables'] if not os.access(p, os.X_OK)]
        for command in ('python3', 'stdbuf', 'tar', 'pgrep'):
            if not shutil.which(command):
                missing.append(command)
        if missing:
            raise RuntimeError('Missing executables: %s' % missing)
        if request['sudo']:
            subprocess.run(['sudo', '-n', 'true'], check=True, timeout=10)
        if subprocess.run(['pgrep', '-x', 'iokerneld'], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('An IOKernel is already running; stop it manually before launching')
        free = shutil.disk_usage(root).free
        if free < request['minimum_free_gb'] * 1024**3:
            raise RuntimeError('Insufficient free space for capture')
        # The stream workload uses this fixed SysV key. Never reuse another job's segment.
        if request.get('batch_key') is not None:
            for row in Path('/proc/sysvipc/shm').read_text().splitlines()[1:]:
                if int(row.split()[0]) == request['batch_key']:
                    raise RuntimeError('Shared-memory key already exists; refusing to reuse it')
        repo = request['repo']
        def git(*args):
            return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True).stdout
        if request.get('batch_key') is not None:
            save(root / 'ipc-guard.json', {'key': request['batch_key'], 'sudo': request['sudo']})
        binaries = {}
        for name in request['executables']:
            digest = hashlib.sha256()
            with open(name, 'rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
            binaries[name] = digest.hexdigest()
        return dict(free_bytes=free, binaries_sha256=binaries, revision=git('rev-parse', 'HEAD').strip(),
                    diff=git('diff', 'HEAD'), status=git('status', '--short'),
                    topology=subprocess.check_output(['lscpu', '-p=CPU,CORE,SOCKET,ONLINE'], text=True))
    if action == 'write':
        path = root / request['name']
        if path.parent != root:
            raise RuntimeError('Invalid config filename')
        path.write_text(request['text'])
        return str(path)
    if action == 'start':
        label = request['label']
        if not label.replace('-', '').replace('_', '').isalnum():
            raise RuntimeError('Invalid process label')
        spec_path = root / (label + '.job.json')
        if spec_path.exists():
            raise RuntimeError('Process label already used in this run')
        spec = dict(request, log=str(root / (label + '.log')))
        save(spec_path, spec)
        supervisor = subprocess.Popen([sys.executable, __file__, 'worker', str(spec_path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        status = spec_path.with_suffix('.status.json')
        for _ in range(100):
            if status.exists():
                return {'started': label}
            if supervisor.poll() is not None:
                raise RuntimeError('Job supervisor exited before recording process state')
            time.sleep(.1)
        os.killpg(supervisor.pid, signal.SIGTERM)
        raise RuntimeError('Job supervisor did not start in time')
    if action == 'status':
        path = root / (request['label'] + '.job.status.json')
        return json.loads(path.read_text()) if path.exists() else {'starting': True}
    if action == 'log':
        path = root / (request['label'] + '.log')
        if not path.exists():
            return ''
        with path.open('rb') as source:
            source.seek(max(0, path.stat().st_size - request.get('tail_bytes', 65536)))
            return source.read().decode(errors='replace')
    if action == 'ipc':
        guard = json.loads((root / 'ipc-guard.json').read_text())
        state_path = root / 'antagonist.job.status.json'
        if not state_path.exists():
            return {'ready': False}
        state = json.loads(state_path.read_text())
        for row in Path('/proc/sysvipc/shm').read_text().splitlines()[1:]:
            fields = row.split()
            if int(fields[0]) != guard['key']:
                continue
            # The creator must descend from this run's tracked antagonist process.
            ancestor = int(fields[4])
            for _ in range(20):
                if ancestor == state['pid']:
                    save(root / 'ipc-owned.json', dict(key=fields[0], shmid=fields[1],
                                                      ctime=fields[13], sudo=guard['sudo']))
                    return {'ready': True}
                try:
                    status = Path('/proc/%d/status' % ancestor).read_text()
                    ancestor = int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
                except (FileNotFoundError, StopIteration):
                    break
                if ancestor <= 1:
                    break
            raise RuntimeError('Shared memory was created by an unrelated process')
        return {'ready': False}
    if action == 'stop':
        ipc_error = None
        if ((root / 'ipc-guard.json').exists() and not (root / 'ipc-owned.json').exists()
                and (root / 'antagonist.job.status.json').exists()):
            try:
                main('ipc', request)
            except Exception as exc:
                ipc_error = str(exc)
        for path in root.glob('*.job.status.json'):
            state = json.loads(path.read_text())
            if 'returncode' not in state:
                terminate(state)
        # Wait for supervisors to persist exit statuses before copying results.
        for _ in range(100):
            if all('returncode' in json.loads(p.read_text()) for p in root.glob('*.job.status.json')):
                break
            time.sleep(.1)
        # ENOSPC can prevent a supervisor from recording its exit. Recover
        # only when the original process identity no longer exists.
        for path in root.glob('*.job.status.json'):
            state = json.loads(path.read_text())
            if ('returncode' not in state and state.get('pid') and
                    proc_start(state['pid']) != state.get('proc_start')):
                state.update(returncode=125, finished_unix=time.time(),
                             recovered_missing_exit_status=True)
                save(path, state)
        owned = root / 'ipc-owned.json'
        if owned.exists():
            ipc = json.loads(owned.read_text())
            for row in Path('/proc/sysvipc/shm').read_text().splitlines()[1:]:
                fields = row.split()
                if (fields[0], fields[1], fields[13]) == (ipc['key'], ipc['shmid'], ipc['ctime']):
                    prefix = ['sudo', '-n'] if ipc['sudo'] else []
                    subprocess.run(prefix + ['ipcrm', '-m', ipc['shmid']], check=True)
        if ipc_error:
            raise RuntimeError(ipc_error)
        if any('returncode' not in json.loads(p.read_text()) for p in root.glob('*.job.status.json')):
            raise RuntimeError('A process supervisor has not recorded its exit; retaining lock')
        return True
    if action == 'telemetry':
        import struct
        records = {}
        for path in root.glob('capture.*.bin'):
            with path.open('rb') as source:
                header = source.read(96)
            if len(header) != 96:
                return {'complete': False}
            flags = struct.unpack_from('<I', header, 52)[0]
            records[path.name] = {'complete': flags == 1, 'size': path.stat().st_size}
        required = {'capture.' + suffix + '.bin' for suffix in
                    ('bw', 'llc', 'pmc-failure', 'sched', 'queue', 'task', 'delay')}
        return {'complete': required.issubset(records) and all(v['complete'] for v in records.values()),
                'streams': records}
    if action == 'manifest':
        return manifest(root)
    if action == 'prune-captures':
        # Caller has verified local copies against the transfer manifest. Only
        # remove captures from this owned run after all its writers have stopped.
        if any('returncode' not in json.loads(p.read_text())
               for p in root.glob('*.job.status.json')):
            raise RuntimeError('Refusing to prune captures from a live run')
        removed = []
        for name, metadata in request['verified'].items():
            path = root / name
            if path.parent != root or not name.startswith('capture.') or not name.endswith('.bin'):
                continue
            if path.is_symlink() or path.stat().st_size != metadata['size']:
                raise RuntimeError('Capture changed after transfer: ' + name)
            path.unlink()
            removed.append(name)
        return removed
    if action == 'release':
        # Delete only the lock. Remote result files are intentionally retained.
        lock.unlink()
        return True
    raise RuntimeError('Unknown action: ' + action)


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(sys.argv[2])
    else:
        try:
            print(json.dumps(main(sys.argv[1], json.load(sys.stdin))))
        except Exception as exc:
            print(json.dumps({'error': str(exc)}))
            sys.exit(1)
