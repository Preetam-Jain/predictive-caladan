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
    if action == 'pktlog-setup':
        # Reserve hugepages on the segment node if needed and create the segment
        # once per machine session; an existing segment is only checked.
        argv = ['bash', request['repo'] + '/scripts/cloudlab/pktlog-setup.sh', '--path', request['path'],
                '--gib', str(request['gib']), '--node', str(request['node']),
                '--ctl', request['ctl']]
        return run_json(argv, request['sudo'], timeout=1800)
    if action == 'pktlog-stat':
        return run_json([request['ctl'], 'stat', '--path', request['path']], request['sudo'])
    if action == 'pktlog-dump':
        # Writes ROOT/pktlog.pkt.kNNN.bin, then empties the segment. The reset
        # happens only after a successful, fsynced dump; on any failure the
        # segment keeps its records and the next run's preflight refuses to start.
        if any('returncode' not in json.loads(p.read_text()) for p in root.glob('*.job.status.json')):
            raise RuntimeError('Refusing to dump the packet log while run processes are alive')
        argv = [request['ctl'], 'dump', '--path', request['path'], '--out', str(root / 'pktlog')]
        capture = root / 'capture.bw.bin'
        if capture.exists():
            argv += ['--experiment-id-from', str(capture)]
        dump = run_json(argv, request['sudo'], timeout=3600)
        save(root / 'pktlog-dump.json', dump)
        reset = run_json([request['ctl'], 'reset', '--path', request['path']], request['sudo'], timeout=600)
        save(root / 'pktlog-reset.json', reset)
        return dict(dump=dump, reset=reset)
    if action == 'clock':
        # TSC and wall clock of this node, for stamping scheduled events.
        return run_json([request['ctl'], 'now'], False)
    if action == 'terminate':
        path = root / (request['label'] + '.job.status.json')
        if not path.exists():
            raise RuntimeError('Unknown process label: ' + request['label'])
        state = json.loads(path.read_text())
        if 'returncode' not in state:
            terminate(state)
        return True
    if action == 'compress':
        return compress(root, request)
    if action == 'prune-run':
        # The caller verified local copies (compressed bytes against the transfer
        # manifest and decompressed bytes against the raw hashes). Remove exactly
        # those files from this owned run, never anything else.
        if any('returncode' not in json.loads(p.read_text())
               for p in root.glob('*.job.status.json')):
            raise RuntimeError('Refusing to prune a live run')
        removed = []
        for name, metadata in request['verified'].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size != metadata['size']:
                raise RuntimeError('File changed after transfer: ' + name)
            path.unlink()
            removed.append(name)
        return removed
    raise RuntimeError('Unknown action: ' + action)


def run_json(argv, sudo, timeout=120):
    """Run a tool that prints one JSON object; raise with its error otherwise."""
    if sudo:
        argv = ['sudo', '-n', '--'] + argv
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    lines = [line for line in proc.stdout.splitlines() if line.startswith('{')]
    try:
        result = json.loads(lines[-1]) if lines else None
    except ValueError:
        result = None
    if proc.returncode or not isinstance(result, dict) or result.get('ok') is False:
        raise RuntimeError('%s failed (exit %d): %s %s' % (
            ' '.join(argv), proc.returncode, (result or {}).get('error', proc.stdout[-2000:]),
            proc.stderr[-2000:]))
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1 << 22), b''):
            digest.update(block)
    return digest.hexdigest()


def compress(root, request):
    """zstd every *.bin file in ROOT, verify, then delete the raw file.

    For each file: hash the raw bytes, compress to FILE.zst, hash what
    decompressing FILE.zst produces, and delete FILE only if the two hashes
    match. compression-manifest.json maps each original name to its raw and
    compressed sizes and hashes, so the receiver can verify end to end.
    """
    import shutil
    if not shutil.which('zstd'):
        raise RuntimeError('zstd is not installed on ' + os.uname().nodename)
    if any('returncode' not in json.loads(p.read_text()) for p in root.glob('*.job.status.json')):
        raise RuntimeError('Refusing to compress while run processes are alive')
    manifest_path = root / 'compression-manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    level = int(request['level'])
    threads = int(request.get('threads', 0))
    for path in sorted(root.glob('*.bin')):
        if path.is_symlink() or not path.is_file():
            continue
        name = path.name
        target = path.with_name(name + '.zst')
        raw_sha = sha256_file(path)
        raw_size = path.stat().st_size
        if target.exists():
            target.unlink()  # a partial file from an interrupted attempt
        argv = ['zstd', '-q', '-%d' % level, '-T%d' % threads, '--long=27', '-o', str(target), str(path)]
        if level > 19:
            argv.insert(1, '--ultra')
        started = time.monotonic()
        subprocess.run(argv, check=True, timeout=7200)
        seconds = time.monotonic() - started
        digest = hashlib.sha256()
        with subprocess.Popen(['zstd', '-q', '-dc', '--long=27', str(target)], stdout=subprocess.PIPE) as check:
            for block in iter(lambda: check.stdout.read(1 << 22), b''):
                digest.update(block)
        if check.returncode or digest.hexdigest() != raw_sha:
            target.unlink(missing_ok=True)
            raise RuntimeError('Compressed copy of %s does not decompress to the original' % name)
        manifest[name] = dict(raw_size=raw_size, raw_sha256=raw_sha,
                              zst_name=target.name, zst_size=target.stat().st_size,
                              zst_sha256=sha256_file(target), seconds=round(seconds, 2), level=level)
        save(manifest_path, manifest)
        path.unlink()
    return manifest


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(sys.argv[2])
    else:
        try:
            print(json.dumps(main(sys.argv[1], json.load(sys.stdin))))
        except Exception as exc:
            print(json.dumps({'error': str(exc)}))
            sys.exit(1)
