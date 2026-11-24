#!/usr/bin/env python3
"""Desktop SSH experiment launcher. See suite.toml for all configuration switches."""
from __future__ import annotations
import argparse
import base64
import csv
import hashlib
import io
import ipaddress
import json
import math
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / 'results'
TEMPLATE = Path(__file__).with_name('suite.toml')
REMOTE = Path(__file__).with_name('remote.py')
CONDITIONS = ('memcached_alone', 'memcached_stream_dram', 'memcached_swaptions_gc')
# Seconds; actions that hash, dump or compress tens of GB need far longer than 40 s.
RPC_TIMEOUTS = {'manifest': 3600, 'stop': 120, 'pktlog-setup': 1900, 'pktlog-dump': 3700,
                'compress': 4 * 3600, 'prune-run': 600, 'pktlog-stat': 60}
PKTLOG_CTL = 'scripts/pktlog/pktlogctl'


def say(message):
    print(message, flush=True)


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
    tmp.replace(path)


def merge(schema, supplied, path='config'):
    if not isinstance(supplied, dict):
        raise ValueError(path + ' must be a table')
    unknown = set(supplied) - set(schema)
    if unknown:
        raise ValueError('%s: unknown keys %s' % (path, sorted(unknown)))
    result = {}
    for key, default in schema.items():
        value = supplied.get(key, default)
        name = path + '.' + key
        if isinstance(default, dict):
            value = merge(default, value, name)
        elif type(default) is float:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(name + ' must be a finite number')
        elif type(value) is not type(default):
            raise ValueError(name + ' must be ' + type(default).__name__)
        result[key] = value
    return result


def load_config(path):
    import tomllib
    config = merge(tomllib.loads(TEMPLATE.read_text()), tomllib.loads(Path(path).read_text()))
    validate_config(config)
    return config


def validate_config(c):
    for section, keys in {
        'setup': ['build_jobs', 'stage_timeout_seconds', 'smoke_seconds', 'smoke_rps', 'smoke_values'],
        'suite': ['repetitions', 'measurement_seconds', 'startup_timeout_seconds',
                  'client_timeout_seconds', 'minimum_free_gb'],
        'logging': ['capture_seconds', 'finalize_timeout_seconds'],
        'allocation': ['memcached_cores', 'memcached_guaranteed', 'antagonist_cores',
                       'antagonist_guaranteed', 'client_cores'],
        'memcached': ['memory_mb', 'nvalues', 'key_size', 'value_size', 'client_connections'],
        'stream_dram': ['region_bytes_per_worker', 'iterations_per_operation'],
        'swaptions_gc': ['user_threads', 'swaptions', 'simulations'],
        'batch_monitor': ['interval_us'],
        'calibration': ['capacity_rps', 'start_rps', 'maximum_rps', 'confirmations',
                        'measurement_seconds'],
    }.items():
        for key in keys:
            if c[section][key] <= 0:
                raise ValueError(section + '.' + key + ' must be positive')
    if not re.fullmatch(r'[a-fA-F0-9]{40}', c['setup']['artifact_revision']):
        raise ValueError('setup.artifact_revision must be a full commit SHA')
    for section in ('suite', 'calibration'):
        if c[section]['warmup_seconds'] < 0:
            raise ValueError(section + '.warmup_seconds cannot be negative')
    if c['calibration']['refinement_steps'] < 0 or c['calibration']['rate_multiplier'] <= 1:
        raise ValueError('Invalid calibration search settings')
    if c['calibration']['start_rps'] > c['calibration']['maximum_rps']:
        raise ValueError('Calibration start exceeds maximum')
    if not 0 <= c['memcached']['pctset'] <= 1000:
        raise ValueError('memcached.pctset is per thousand, between 0 and 1000')
    if not c['suite']['load_fractions'] or any(type(x) not in (int, float) or
            not math.isfinite(x) or not 0 < x <= 1 for x in c['suite']['load_fractions']):
        raise ValueError('load_fractions must contain numbers in (0, 1]')
    if len(set(c['suite']['load_fractions'])) != len(c['suite']['load_fractions']):
        raise ValueError('Duplicate load fractions')
    if not any(c['conditions'].values()) or not any((c['logging']['run_with_logging'],
                                                  c['logging']['run_without_logging'])):
        raise ValueError('Enable at least one condition and logging mode')
    if c['logging'].get('queue_snapshot_failures', False) and c['logging']['run_without_logging']:
        raise ValueError('queue_snapshot_failures requires binary logging on for every run')
    for role in ('memcached', 'antagonist'):
        if c['allocation'][role + '_guaranteed'] > c['allocation'][role + '_cores']:
            raise ValueError(role + ' guaranteed cores exceed maximum cores')
        if c['allocation']['use_smt'] and c['allocation'][role + '_guaranteed'] % 2:
            raise ValueError('Use even guarantees with SMT enabled')
    if c['allocation']['use_smt'] and c['allocation']['client_cores'] % 2:
        raise ValueError('Use an even client core count with SMT enabled')
    if c['allocation']['writer_cpu'] < 0 or c['batch_monitor']['cpu'] < -1:
        raise ValueError('Invalid CPU index')
    if c['logging']['capture_seconds'] > 86400:
        raise ValueError('capture_seconds must not exceed the IOKernel limit of 86400')
    if c['logging']['capture_seconds'] < c['suite']['warmup_seconds'] + c['suite']['measurement_seconds']:
        raise ValueError('Capture must cover warmup + measurement, plus time for startup/preload')
    if c['suite']['client_timeout_seconds'] <= c['suite']['warmup_seconds'] + c['suite']['measurement_seconds']:
        raise ValueError('client_timeout_seconds must also allow preload and response draining')
    for section in ('calibration', 'validation'):
        for key, value in c[section].items():
            if key.endswith('_fraction') and not 0 < value <= 1:
                raise ValueError(section + '.' + key + ' must be in (0, 1]')
            if key.endswith('adherence_percent') and not 0 < value <= 100:
                raise ValueError(section + '.' + key + ' must be in (0, 100]')
    for key in ('server_ip', 'client_ip', 'antagonist_ip', 'gateway'):
        ipaddress.IPv4Address(c['network'][key])
    ipaddress.IPv4Network('0.0.0.0/' + c['network']['netmask'])
    if len({c['network'][k] for k in ('server_ip', 'client_ip', 'antagonist_ip')}) != 3:
        raise ValueError('Runtime addresses must be distinct')
    if not 1 <= c['network']['port'] <= 65535:
        raise ValueError('Invalid memcached port')
    for node in c['nodes'].values():
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', node['host']) or not re.fullmatch(r'[A-Za-z0-9_.-]+', node['user']):
            raise ValueError('Invalid SSH host/user')
        if not 1 <= node['port'] <= 65535:
            raise ValueError('Invalid SSH port')
        for key in ('repo', 'artifact', 'remote_results'):
            if not node[key].startswith('/') or not re.fullmatch(r'[A-Za-z0-9_./-]+', node[key]):
                raise ValueError('Remote paths must be absolute, without spaces or shell metacharacters')
        if node['allowed_cpus'] and not re.fullmatch(r'[0-9,:-]+', node['allowed_cpus']):
            raise ValueError('Invalid allowed_cpus list')
    p = c['pktlog']
    if p['enabled']:
        if not c['network']['directpath']:
            raise ValueError('pktlog requires network.directpath (NIC completion timestamps)')
        if not c['logging']['run_with_logging']:
            raise ValueError('pktlog is only used on logging-on runs; enable logging.run_with_logging')
        if p['gib'] <= 0 or p['window_us'] <= 0 or p['slo_us'] <= 0:
            raise ValueError('pktlog.gib, window_us and slo_us must be positive')
        if p['node'] == c['nodes']['server']['numa_node']:
            raise ValueError('pktlog.node must be the socket that does NOT run Caladan')
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', p['segment_path']):
            raise ValueError('pktlog.segment_path must be an absolute path without spaces')
    if not 1 <= c['storage']['zstd_level'] <= 22 or c['storage']['zstd_threads'] < 0:
        raise ValueError('storage.zstd_level must be 1..22 and zstd_threads >= 0')
    if c['storage']['prune_remote'] and not (c['storage']['compress'] and c['storage']['verify_raw']):
        raise ValueError('storage.prune_remote requires compress and verify_raw')
    sch = c['schedule']
    if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in sch['load_steps']):
        raise ValueError('schedule.load_steps must be positive numbers')
    if sch['load_steps'] and (c['suite']['measurement_seconds'] * 1000000) % len(sch['load_steps']):
        raise ValueError('measurement_seconds must split evenly into load_steps')
    start, stop = sch['antagonist_start_seconds'], sch['antagonist_stop_seconds']
    total = c['suite']['warmup_seconds'] + c['suite']['measurement_seconds']
    if start != -1 and not 0 <= start < total:
        raise ValueError('schedule.antagonist_start_seconds must be -1 or inside warmup+measurement')
    if stop != -1 and not (max(start, 0) < stop < total):
        raise ValueError('schedule.antagonist_stop_seconds must be -1 or after the start, inside the run')


def plan(c, capacity):
    groups = []
    rng = random.Random(c['suite']['random_seed'])
    modes = ([False] if c['logging']['run_without_logging'] else []) + ([True] if c['logging']['run_with_logging'] else [])
    for repetition in range(1, c['suite']['repetitions'] + 1):
        block = [(condition, fraction) for condition in CONDITIONS if c['conditions'][condition]
                 for fraction in c['suite']['load_fractions']]
        if c['suite']['randomize_order']:
            rng.shuffle(block)
        for condition, fraction in block:
            order = list(modes)
            if c['suite']['randomize_order']:
                rng.shuffle(order)
            for logging in order:
                groups.append(dict(condition=condition, fraction=fraction, repetition=repetition,
                                   logging=logging, rps=max(1, round(capacity * fraction))))
    for index, run in enumerate(groups, 1):
        run['id'] = '%03d-%s-r%d-%s-log%s' % (
            index, run['condition'], run['repetition'], format(run['fraction'], '.3g'),
            'on' if run['logging'] else 'off')
    return groups


def binary(node, value, artifact=False):
    return value if value.startswith('/') else str(Path(node['artifact' if artifact else 'repo']) / value)


def runtime_config(c, role, pktlog=False):
    a, net = c['allocation'], c['network']
    cores = a['client_cores'] if role == 'client' else a[role + '_cores']
    guaranteed = cores if role == 'client' else a[role + '_guaranteed']
    priority = 'be' if role == 'antagonist' else 'lc'
    text = ('host_addr %s\nhost_netmask %s\nhost_gateway %s\n'
            'runtime_kthreads %d\nruntime_guaranteed_kthreads %d\nruntime_priority %s\n') % (
                net[{'memcached': 'server_ip', 'client': 'client_ip', 'antagonist': 'antagonist_ip'}[role]],
                net['netmask'], net['gateway'], cores, guaranteed, priority)
    if role == 'client':
        text += 'runtime_spinning_kthreads %d\n' % cores
    if role != 'antagonist':
        text += 'runtime_qdelay_us 10\n'
    if net['directpath'] and role != 'antagonist':
        text += 'enable_directpath 1\n'
    if pktlog and role == 'memcached':
        text += 'pktlog_path %s\n' % c['pktlog']['segment_path']
    return text


def iokernel_command(c, node, root, server, logging):
    argv = [node['repo'] + '/iokerneld', 'ias', 'numanode', str(node['numa_node']), 'nicpci', node['nic_pci']]
    if server and logging and c['logging'].get('queue_snapshot_failures', False):
        argv += ['queue_snapshot_failure_logging', 'on']
    if not c['allocation']['use_smt']:
        argv.append('noht')
    if node['allowed_cpus']:
        argv.append(node['allowed_cpus'])
    settings = c['interventions'] if server else dict(bandwidth=False, hyperthread=False, timesharing=False, core_allocation=False)
    for field, flag in [('bandwidth', 'bw_intervention'), ('hyperthread', 'ht_intervention'),
                        ('timesharing', 'timesharing'), ('core_allocation', 'core_allocation')]:
        argv += [flag, 'on' if settings[field] else 'off']
    if server:
        argv += ['telemetry', str(c['allocation']['writer_cpu']),
                 root + '/capture' if logging else '-', str(c['logging']['capture_seconds'])]
    else:
        argv += ['nobw']
    return argv


def load_steps(c, rps, measurement):
    """(rate, microseconds) for each measured schedule segment."""
    steps = c['schedule']['load_steps'] or [1.0]
    share = measurement * 1000000 // len(steps)
    return [(max(1, round(rps * f)), share) for f in steps]


def client_command(c, node, root, rps, warmup, measurement, probe=False, trace=False):
    m = c['memcached']
    schedule = []
    if warmup:
        schedule.append('%d:%d:silent' % (rps, warmup * 1000000))
    for rate, micros in ([(rps, measurement * 1000000)] if probe else load_steps(c, rps, measurement)):
        schedule.append('%d:%d' % (rate, micros))
    extra = ['--trace-prefix', root + '/ctrace'] if trace and not probe else []
    return [node['repo'] + '/apps/loadgen/target/release/loadgen',
            '%s:%d' % (c['network']['server_ip'], c['network']['port']),
            '--config', root + '/client.config', '--mode', 'runtime-client',
            '--protocol', 'memcached', '--transport', 'tcp',
            '--conns', str(1 if probe else m['client_connections']),
            '--nvalues', str(1 if probe else m['nvalues']), '--key_size', str(m['key_size']),
            '--value_size', str(m['value_size']), '--pctset', str(m['pctset']),
            '--output', 'trace' if m['trace_requests'] and not probe else 'normal',
            '--loadshift', ','.join(schedule)] + extra


def summaries(text):
    rows = []
    for fields in csv.reader(io.StringIO(text)):
        if len(fields) < 14:
            continue
        try:
            values = [float(x.strip()) for x in fields[1:14]]
        except ValueError:
            continue
        rows.append(dict(sent_rps=values[0], response_rps=values[1], drops=int(values[2]),
                         schedule_adherence_percent=values[3], min_us=values[4], p50_us=values[5],
                         p90_us=values[6], p99_us=values[7], p99_9_us=values[8],
                         p99_99_us=values[9], max_us=values[10],
                         start_unix=values[11], first_server_tsc=int(fields[13])))
    return rows


def quality(row, rps, settings, calibration=False):
    issues = []
    if row['sent_rps'] < rps * settings['minimum_send_fraction']:
        issues.append('Client actual send rate below requested load')
    if row['schedule_adherence_percent'] < settings['minimum_schedule_adherence_percent']:
        issues.append('Client missed too many scheduled sends')
    if calibration:
        if row['response_rps'] < rps * settings['minimum_response_fraction'] or row['drops']:
            issues.append('Server cannot sustain this load')
        if row['p99_9_us'] is None or not math.isfinite(row['p99_9_us']) or row['p99_9_us'] > settings['maximum_p99_9_us']:
            issues.append('Calibration p99.9 limit exceeded')
    return issues


class Node:
    def __init__(self, config, suite_id, run_id, token):
        self.config = config
        self.root = config['remote_results'] + '/' + suite_id + '/' + run_id
        self.token = token
        self.helper = config['remote_results'] + '/experiment-remote-' + hashlib.sha256(REMOTE.read_bytes()).hexdigest()[:12] + '.py'
        self.initialized = False
        self.ssh = ['ssh', '-T', '-oBatchMode=yes', '-oConnectTimeout=10', '-oServerAliveInterval=15',
                    '-oServerAliveCountMax=3', '-p', str(config['port'])]
        if config['identity_file']:
            self.ssh += ['-i', str(Path(config['identity_file']).expanduser())]
        self.ssh += [config['user'] + '@' + config['host']]

    def bootstrap(self):
        code = ('import base64,pathlib; p=pathlib.Path(%r); p.parent.mkdir(parents=True,exist_ok=True); '
                'p.write_bytes(base64.b64decode(%r))') % (self.helper, base64.b64encode(REMOTE.read_bytes()).decode())
        result = subprocess.run(self.ssh + [shlex.join(['python3', '-c', code])],
                                capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise RuntimeError('%s bootstrap failed: %s' %
                               (self.config['host'], result.stderr.strip() or result.stdout.strip()))
        self.rpc('init')
        self.initialized = True

    def rpc(self, action, **data):
        payload = dict(root=self.root, token=self.token, **data)
        timeout = RPC_TIMEOUTS.get(action, 40)
        proc = subprocess.run(self.ssh + [shlex.join(['python3', self.helper, action])],
                              input=json.dumps(payload), text=True, capture_output=True,
                              timeout=timeout)
        if proc.returncode:
            raise RuntimeError('%s %s: %s' % (self.config['host'], action, proc.stderr or proc.stdout))
        return json.loads(proc.stdout)

    def start(self, label, argv, timeout, env=None):
        self.rpc('start', label=label, argv=['numactl', '--cpunodebind=' + str(self.config['numa_node']),
                                             '--membind=' + str(self.config['numa_node']),
                                             'stdbuf', '-oL', '-eL', *argv],
                 cwd=self.config['repo'], sudo=self.config['sudo'], timeout=timeout, env=env or {})

    def assert_alive(self, *labels):
        for label in labels:
            state = self.rpc('status', label=label)
            if 'returncode' in state:
                raise RuntimeError('%s exited: %s' % (label, state))

    def wait(self, label, timeout, heartbeat=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.rpc('status', label=label)
            if 'returncode' in state:
                if state['returncode']:
                    raise RuntimeError('%s failed: %s' % (label, state))
                return state
            if heartbeat:
                heartbeat()
            time.sleep(2)
        raise TimeoutError(label + ' exceeded launcher timeout')

    def collect(self, destination, compress=False):
        expected = self.rpc('manifest')
        destination.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(destination).free < sum(v['size'] for v in expected.values()):
            raise RuntimeError('Not enough local disk space to collect remote results')
        command = (['taskset', '-c', '0,72', 'tar', '-I', 'gzip -1', '-C', self.root, '-cf', '-', '.']
                   if compress else ['tar', '-C', self.root, '-cf', '-', '.'])
        proc = subprocess.Popen(self.ssh + [shlex.join(command)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdout, mode='r|*') as archive:
                for member in archive:
                    target = (destination / member.name).resolve()
                    if not target.is_relative_to(destination.resolve()):
                        raise RuntimeError('Unsafe path in remote archive')
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, target.open('wb') as output:
                            shutil.copyfileobj(source, output)
                    else:
                        raise RuntimeError('Unexpected link/device in remote results')
            _, stderr = proc.communicate(timeout=120)
            if proc.returncode:
                raise RuntimeError(stderr.decode(errors='replace'))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for name, metadata in expected.items():
            path = destination / name
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
            if path.stat().st_size != metadata['size'] or digest.hexdigest() != metadata['sha256']:
                raise RuntimeError('Checksum mismatch: ' + name)
        save(destination / 'transfer-manifest.json', expected)
        return expected


def wait_log(node, label, phrase, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node.assert_alive(label)
        if phrase in node.rpc('log', label=label):
            return
        time.sleep(1)
    raise TimeoutError('Readiness message not seen for ' + label)


def preflight(c, node, condition, server):
    n = node.config
    executables = [n['repo'] + '/iokerneld']
    if c['storage']['compress']:
        executables.append('/usr/bin/zstd')
    if not server:
        executables.append(n['repo'] + '/apps/loadgen/target/release/loadgen')
    elif (c['pktlog']['enabled'] or c['schedule']['antagonist_start_seconds'] >= 0
          or c['schedule']['antagonist_stop_seconds'] >= 0):
        # packet log dumps and TSC stamps for scheduled events
        executables.append(n['repo'] + '/' + PKTLOG_CTL)
    if server:
        executables += [binary(n, c['memcached']['binary'], True)]
        if condition != 'memcached_alone':
            key = 'stream_dram' if condition == 'memcached_stream_dram' else 'swaptions_gc'
            executables += [binary(n, c[key]['binary'], key == 'swaptions_gc')]
            if c['batch_monitor']['enabled']:
                executables += [binary(n, c['batch_monitor']['binary'])]
    info = node.rpc('preflight', executables=executables, sudo=n['sudo'], repo=n['repo'],
                    minimum_free_gb=c['suite']['minimum_free_gb'],
                    batch_key=0x123 if server and condition != 'memcached_alone' else None)
    # CPU topology is retained for auditing; reservations are enforced by the IOKernel.
    return info


def compression_manifest(directory):
    path = directory / 'compression-manifest.json'
    return json.loads(path.read_text()) if path.exists() else {}


def decompress_head(source, target, size):
    """Write the first SIZE decompressed bytes of SOURCE (.zst) to TARGET."""
    with subprocess.Popen(['zstd', '-q', '-dc', '--long=27', str(source)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        data = proc.stdout.read(size)
        proc.kill()
    Path(target).write_bytes(data)


def decompress_to(source, target):
    subprocess.run(['zstd', '-q', '-d', '--long=27', '-f', '-o', str(target), str(source)], check=True)


def verify_raw(directory):
    """Decompress every .zst and compare with the raw sha256 recorded on the node."""
    manifest = compression_manifest(directory)
    checked = {}
    for name, meta in manifest.items():
        digest, size = hashlib.sha256(), 0
        with subprocess.Popen(['zstd', '-q', '-dc', '--long=27', str(directory / meta['zst_name'])],
                              stdout=subprocess.PIPE) as proc:
            for block in iter(lambda: proc.stdout.read(1 << 22), b''):
                digest.update(block)
                size += len(block)
        if proc.returncode or size != meta['raw_size'] or digest.hexdigest() != meta['raw_sha256']:
            raise RuntimeError('Decompressed %s does not match the raw file hashed on the node' % name)
        checked[name] = dict(size=size, sha256=digest.hexdigest())
    save(directory / 'raw-verification.json', checked)
    return checked


def validate_capture(directory, reject_drops):
    sys.path.insert(0, str(REPO / 'scripts'))
    from decode_telemetry import capture_streams, read_header
    manifest = compression_manifest(directory)
    sizes = {}
    temp = None
    if manifest and not (directory / 'capture.bw.bin').exists():
        # Header-only copies; record counts are checked against the raw sizes.
        import tempfile
        temp = Path(tempfile.mkdtemp(prefix='capture-headers-'))
        for name, meta in manifest.items():
            if name.startswith('capture.'):
                decompress_head(directory / meta['zst_name'], temp / name, min(meta['raw_size'], 1 << 20))
                sizes[name] = meta['raw_size']
        prefix = str(temp / 'capture')
    else:
        prefix = str(directory / 'capture')
    identity = None
    streams = {}
    try:
        for suffix, kind in capture_streams(prefix):
            with open(prefix + '.' + suffix + '.bin', 'rb') as source:
                header, cores = read_header(source)
                current = (header.version, header.experiment_id, header.cycles_per_us,
                           header.writer_core, header.scheduler_core, cores)
                if header.stream_type != kind or (identity is not None and identity != current):
                    raise RuntimeError('Telemetry streams have mismatched identities')
                identity = current
                size = sizes.get('capture.' + suffix + '.bin', Path(source.name).stat().st_size)
                if size != header.header_bytes + header.produced * header.record_bytes:
                    raise RuntimeError('Truncated or extra telemetry data: ' + source.name)
                streams[suffix] = dict(produced=header.produced, dropped=header.dropped)
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    save(directory / 'capture-validation.json', streams)
    if any(not v['produced'] for k, v in streams.items() if k in ('bw', 'queue', 'task', 'delay')):
        raise RuntimeError('Expected telemetry stream is empty')
    if reject_drops and any(v['dropped'] for v in streams.values()):
        raise RuntimeError('Telemetry records were dropped; see capture-validation.json')
    return streams


def validate_pktlog(c, attempt):
    """The dump must be complete and lossless, and every connection traced."""
    dump = json.loads((attempt / 'server' / 'pktlog-dump.json').read_text())
    if dump['exhausted'] or dump['dropped']:
        raise RuntimeError('Packet log segment filled up (%d records dropped); raise pktlog.gib '
                           'or shorten the run' % dump['dropped'])
    if not dump['records']:
        raise RuntimeError('Packet log is empty; did memcached attach the segment?')
    names = set(compression_manifest(attempt / 'client')) or {
        p.name for p in (attempt / 'client').glob('ctrace.*.bin')}
    traces = [n for n in names if n.startswith('ctrace.')]
    if len(traces) != c['memcached']['client_connections']:
        raise RuntimeError('Expected %d client traces, found %d' % (
            c['memcached']['client_connections'], len(traces)))
    return dict(records=dump['records'], files=dump['file_count'], client_traces=len(traces))


def join_run(c, attempt):
    """Decompress this run's traces to a temp dir and run join_requests.py."""
    import tempfile
    p = c['pktlog']
    temp = Path(tempfile.mkdtemp(prefix='join-input-'))
    try:
        for role, pattern in (('server', 'pktlog.pkt.'), ('server', 'capture.nicclock'),
                              ('client', 'ctrace.')):
            directory = attempt / role
            manifest = compression_manifest(directory)
            for name in (manifest or {q.name: None for q in directory.glob('*.bin')}):
                if name.startswith(pattern):
                    if manifest:
                        decompress_to(directory / manifest[name]['zst_name'], temp / name)
                    else:
                        shutil.copy2(directory / name, temp / name)
        argv = [p['python'], str(REPO / 'scripts/join_requests.py'),
                '--client', str(temp / 'ctrace.*.bin'), '--packets', str(temp / 'pktlog.pkt.k*.bin'),
                '--nicclock', str(temp / 'capture.nicclock.bin'),
                '--server-port', str(c['network']['port']), '--out', str(attempt / 'joined'),
                '--tmp', str(temp), '--window-us', str(p['window_us']), '--slo-us', str(p['slo_us'])]
        proc = subprocess.run(argv, capture_output=True, text=True)
        (attempt / 'join.log').write_text(proc.stdout + proc.stderr)
        summary_path = attempt / 'joined.summary.json'
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        if proc.returncode:
            raise RuntimeError('Join failed: %s' % (summary.get('failures') or proc.stdout[-500:]))
        return summary.get('requests')
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def recover_previous(c, suite_dir, run_dir):
    latest = run_dir / 'latest.json'
    if not latest.exists():
        attempts = sorted(run_dir.glob('attempt-*/run.json'))
        if not attempts:
            return
        metadata = json.loads(attempts[-1].read_text())
        attempt_dir = attempts[-1].parent
    else:
        metadata = json.loads(latest.read_text())
        attempt_dir = run_dir / metadata['attempt']
    if metadata['state'] == 'complete':
        return
    say('Recovering logs/processes from ' + str(attempt_dir))
    for role in ('server', 'client'):
        node = Node(c['nodes'][role], suite_dir.name, 'recovery', metadata['token'])
        node.root = metadata['remote'][role]
        node.bootstrap()
        node.rpc('stop')
        node.collect(attempt_dir / role)
        node.rpc('release')


def queue_snapshot_frequency(directory, first_tsc, duration):
    source = REPO / 'scripts/telemetry_capture_stats.cc'
    binary = RESULTS / '.analysis-tools' / ('snapshot-' + hashlib.sha256(source.read_bytes()).hexdigest()[:16])
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['g++', '-std=c++17', '-O3', str(source), '-o', str(binary)], check=True)
    prefix, temp = directory / 'capture', None
    manifest = compression_manifest(directory)
    if manifest and not (directory / 'capture.queue.bin').exists():
        import tempfile
        temp = Path(tempfile.mkdtemp(prefix='capture-raw-'))
        for name, meta in manifest.items():
            if name.startswith('capture.'):
                decompress_to(directory / meta['zst_name'], temp / name)
        prefix = temp / 'capture'
    try:
        output = subprocess.run([str(binary), '--snapshot-frequency', str(prefix),
                                 str(first_tsc), str(duration)], check=True, capture_output=True, text=True)
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    result = json.loads(output.stdout)
    if not result:
        raise RuntimeError('No queue observations inside measured window')
    save(directory / 'queue-snapshot-frequency.json', result)
    return result


def batch_throughput(directory, first_tsc, duration):
    log = (directory / 'batch-throughput.log').read_text()
    timing = log + (directory / 'iokernel.log').read_text()
    frequency = re.search(r'detected\s+(\d+)\s+ticks\s*/\s*us', timing)
    if not frequency:
        raise RuntimeError('Cannot find calibrated TSC frequency for BE throughput')
    ticks_per_second = int(frequency.group(1)) * 1000000
    end_tsc = first_tsc + duration * ticks_per_second
    intervals, previous = [], None
    total_ops, total_ticks = 0, 0
    for line in log.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            key, operations, tsc = int(fields[0], 16), int(fields[1]), int(fields[2])
        except ValueError:
            continue
        if key != 0x123:
            continue
        if previous is not None and tsc > previous:
            measured = previous >= first_tsc and tsc <= end_tsc
            intervals.append(dict(start_tsc=previous, end_tsc=tsc, operations=operations,
                                  operations_per_second=operations * ticks_per_second / (tsc - previous),
                                  inside_measurement=measured))
            if measured:
                total_ops += operations
                total_ticks += tsc - previous
        previous = tsc
    if not total_ticks:
        raise RuntimeError('No BE counter intervals fall inside the measured request window')
    save(directory / 'batch-throughput.json', intervals)
    return total_ops * ticks_per_second / total_ticks


def check_provenance(suite_dir, role, info):
    path = suite_dir / ('provenance-' + role + '.json')
    current = {k: info[k] for k in ('revision', 'diff', 'binaries_sha256')}
    if path.exists():
        prior = json.loads(path.read_text())
        if prior['revision'] != current['revision'] or prior['diff'] != current['diff']:
            raise RuntimeError(role + ' source changed during the suite; start a new suite')
        for name, digest in current['binaries_sha256'].items():
            if name in prior['binaries_sha256'] and prior['binaries_sha256'][name] != digest:
                raise RuntimeError('Binary changed during suite: ' + name)
        current['binaries_sha256'] = dict(prior['binaries_sha256'], **current['binaries_sha256'])
    save(path, current)


def run_one(c, suite_dir, run, warmup=None, duration=None):
    run_dir = suite_dir / run['id']
    run_dir.mkdir(exist_ok=True)
    recover_previous(c, suite_dir, run_dir)
    old = sorted(run_dir.glob('attempt-*'))
    attempt = run_dir / ('attempt-%03d' % (len(old) + 1))
    attempt.mkdir()
    token = uuid.uuid4().hex
    nodes = {role: Node(c['nodes'][role], suite_dir.name, run['id'] + '-' + attempt.name, token)
             for role in ('server', 'client')}
    record = dict(run=run, token=token, state='running', started_unix=time.time(),
                  remote={role: n.root for role, n in nodes.items()}, errors=[])
    save(attempt / 'run.json', record)
    save(attempt / 'config.json', c)
    warmup = c['suite']['warmup_seconds'] if warmup is None else warmup
    duration = c['suite']['measurement_seconds'] if duration is None else duration
    if run['id'].startswith('calibration-'):
        # Capacity search uses one constant rate and no scheduled changes.
        c = dict(c, schedule=dict(c['schedule'], load_steps=[], antagonist_start_seconds=-1.0,
                                  antagonist_stop_seconds=-1.0))
    pktlog_on = c['pktlog']['enabled'] and run['logging']
    server_ctl = c['nodes']['server']['repo'] + '/' + PKTLOG_CTL
    pktlog_request = dict(ctl=server_ctl, path=c['pktlog']['segment_path'],
                          sudo=c['nodes']['server']['sudo'])
    server, client = nodes['server'], nodes['client']
    limit = c['suite']['client_timeout_seconds'] + c['suite']['startup_timeout_seconds'] + c['logging']['capture_seconds'] + c['logging']['finalize_timeout_seconds']
    interrupted = False
    capture_started = None
    try:
        for role, node in nodes.items():
            node.bootstrap()
            info = preflight(c, node, run['condition'], role == 'server')
            save(attempt / (role + '-preflight.json'), info)
            check_provenance(suite_dir, role, info)
        if pktlog_on:
            state = server.rpc('pktlog-stat', **pktlog_request)
            save(attempt / 'pktlog-preflight.json', state)
            if not state['empty'] or state['in_use']:
                raise RuntimeError('Packet log segment is not empty or is in use; an earlier run was '
                                   'not dumped. See docs/packet-logging.md before resetting it.')
        for role, node in nodes.items():
            config_role = 'memcached' if role == 'server' else 'client'
            node.rpc('write', name=config_role + '.config',
                     text=runtime_config(c, config_role, pktlog_on and role == 'server'))
            if role == 'server':
                capture_started = time.monotonic()
            node.start('iokernel', iokernel_command(c, node.config, node.root, role == 'server', run['logging']), limit)
        for node in nodes.values():
            wait_log(node, 'iokernel', 'running dataplane', c['suite']['startup_timeout_seconds'])
        m = c['memcached']
        port = str(c['network']['port'])
        server.start('memcached', [binary(server.config, m['binary'], True), server.root + '/memcached.config',
                     '-U', '0', '-p', port, '-c', '32768', '-m', str(m['memory_mb']),
                     '-b', '32768', '-o', 'hashpower=%d,no_hashexpand,no_lru_crawler,no_lru_maintainer,no_slab_reassign' % m['hashpower']], limit)
        # A protocol-level request proves both networking and application readiness.
        # Unlike a kernel socket probe, this works with kernel-bypass runtime IPs.
        deadline = time.monotonic() + c['suite']['startup_timeout_seconds']
        probe_number = 0
        while True:
            probe_number += 1
            label = 'probe-%03d' % probe_number
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Memcached did not become ready')
            client.start(label, client_command(c, client.config, client.root, 100, 0, 1, True), min(15, remaining))
            try:
                client.wait(label, min(15, remaining) + 5, lambda: server.assert_alive('iokernel', 'memcached'))
                if summaries(client.rpc('log', label=label)):
                    break
            except (RuntimeError, TimeoutError):
                server.assert_alive('iokernel', 'memcached')
            time.sleep(1)
        labels = ['iokernel', 'memcached']

        def start_antagonist():
            server.rpc('write', name='antagonist.config', text=runtime_config(c, 'antagonist') +
                       ('enable_gc 1\n' if run['condition'] == 'memcached_swaptions_gc' else ''))
            if run['condition'] == 'memcached_stream_dram':
                b = c['stream_dram']
                argv = [binary(server.config, b['binary']), server.root + '/antagonist.config',
                        str(c['allocation']['antagonist_cores']), str(b['iterations_per_operation']),
                        'cacheantagonist:%d' % b['region_bytes_per_worker']]
                if b['use_barrier']:
                    argv += ['use_barrier']
                monitor_lines = c['allocation']['antagonist_cores']
            else:
                b = c['swaptions_gc']
                argv = [binary(server.config, b['binary'], True), server.root + '/antagonist.config',
                        '-ns', str(b['swaptions']), '-sm', str(b['simulations']), '-nt', str(b['user_threads'])]
                monitor_lines = b['user_threads']
            server.start('antagonist', argv, limit, {'SHMKEY': str(0x123)})
            labels.append('antagonist')
            deadline = time.monotonic() + c['suite']['startup_timeout_seconds']
            while not server.rpc('ipc')['ready']:
                server.assert_alive('antagonist')
                if time.monotonic() > deadline:
                    raise TimeoutError('Antagonist did not create its progress counters')
                time.sleep(1)
            if c['batch_monitor']['enabled']:
                argv = [binary(server.config, c['batch_monitor']['binary']),
                        '%d:%d:%d' % (0x123, c['batch_monitor']['interval_us'], monitor_lines)]
                if c['batch_monitor']['cpu'] >= 0:
                    argv = ['taskset', '-c', str(c['batch_monitor']['cpu']), *argv]
                server.start('batch-throughput', argv, limit)
                labels.append('batch-throughput')

        def stop_antagonist():
            for label in ('batch-throughput', 'antagonist'):
                if label in labels:
                    labels.remove(label)
                    server.rpc('terminate', label=label)

        sch = c['schedule']
        pending = []
        if run['condition'] != 'memcached_alone':
            if sch['antagonist_start_seconds'] < 0:
                start_antagonist()
            else:
                pending.append((sch['antagonist_start_seconds'], 'start'))
            if sch['antagonist_stop_seconds'] >= 0:
                pending.append((sch['antagonist_stop_seconds'], 'stop'))
        client.start('loadgen', client_command(c, client.config, client.root, run['rps'], warmup, duration,
                                               trace=pktlog_on),
                     c['suite']['client_timeout_seconds'])
        events = []
        load_start = None

        def heartbeat():
            nonlocal load_start
            server.assert_alive(*labels)
            client.assert_alive('iokernel')
            if pending and load_start is None and 'LOADGEN_LOAD_START' in client.rpc('log', label='loadgen'):
                load_start = time.monotonic()
            # Scheduled antagonist changes, bracketed by server TSC readings.
            while pending and load_start is not None and time.monotonic() - load_start >= pending[0][0]:
                offset, action = pending.pop(0)
                before = server.rpc('clock', ctl=server_ctl)
                (start_antagonist if action == 'start' else stop_antagonist)()
                after = server.rpc('clock', ctl=server_ctl)
                events.append(dict(action=action, planned_seconds=offset,
                                   actual_seconds=round(time.monotonic() - load_start, 3),
                                   server_tsc_before=before['tsc_before'], server_tsc_after=after['tsc_after']))
                record['schedule_events'] = events
                save(attempt / 'run.json', record)
        state = client.wait('loadgen', c['suite']['client_timeout_seconds'] + 10, heartbeat)
        if pending:
            raise RuntimeError('Load finished before scheduled antagonist events: %s' % pending)
        heartbeat()
        record['client_finished_unix'] = state['finished_unix']
        if run['logging']:
            if server.rpc('telemetry')['complete']:
                raise RuntimeError('Capture finished before client completion; increase capture_seconds')
            deadline = capture_started + c['logging']['capture_seconds'] + c['logging']['finalize_timeout_seconds']
            while not server.rpc('telemetry')['complete']:
                server.assert_alive('iokernel', 'memcached')
                client.assert_alive('iokernel')
                if time.monotonic() > deadline:
                    raise TimeoutError('Telemetry did not finalize')
                time.sleep(2)
            wait_log(server, 'iokernel', 'all streams drained and closed', c['logging']['finalize_timeout_seconds'])
        record['state'] = 'finished'
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        record['state'] = 'failed'
        record['errors'].append(str(exc) or type(exc).__name__)
    finally:
        for role, node in nodes.items():
            if not node.initialized:
                continue
            stopped = False
            try:
                node.rpc('stop')
                stopped = True
            except Exception as exc:
                record['errors'].append(role + ' cleanup: ' + str(exc))
                record['state'] = 'failed'
            if stopped and role == 'server' and pktlog_on:
                # On failure the segment keeps its records (no reset), and the
                # next run's preflight refuses to start.
                try:
                    say('Dumping packet log')
                    dumped = node.rpc('pktlog-dump', **pktlog_request)['dump']
                    record['pktlog_dump'] = {k: dumped[k] for k in
                                             ('records', 'dropped', 'exhausted', 'file_count', 'generation')}
                except Exception as exc:
                    record['errors'].append('server pktlog dump: ' + str(exc))
                    record['state'] = 'failed'
            compressed = False
            if stopped and c['storage']['compress']:
                try:
                    say('Compressing %s results' % role)
                    node.rpc('compress', level=c['storage']['zstd_level'], threads=c['storage']['zstd_threads'])
                    compressed = True
                except Exception as exc:
                    record['errors'].append(role + ' compression: ' + str(exc))
                    record['state'] = 'failed'
            try:
                for retry in range(3):
                    try:
                        transferred = node.collect(attempt / role)
                        break
                    except Exception:
                        if retry == 2:
                            raise
                        say('Retrying result transfer from ' + role)
                        time.sleep(2)
                if compressed and c['storage']['verify_raw']:
                    verify_raw(attempt / role)
                    if c['storage']['prune_remote']:
                        node.rpc('prune-run', verified=transferred)
                if stopped:
                    node.rpc('release')
            except Exception as exc:
                record['errors'].append(role + ' collection: ' + str(exc))
                record['state'] = 'failed'
        record['finished_unix'] = time.time()
        try:
            rows = summaries((attempt / 'client/loadgen.log').read_text())
            steps = load_steps(c, run['rps'], duration)
            if len(rows) != len(steps):
                raise RuntimeError('Expected %d measured loadgen summaries, found %d' % (len(steps), len(rows)))
            row = rows[0]
            finite = lambda r: {k: v if math.isfinite(v) else None for k, v in r.items()}
            record['summary'] = finite(row)
            issues = []
            for number, (step_row, (rate, _)) in enumerate(zip(rows, steps)):
                prefix = 'step %d: ' % (number + 1) if len(steps) > 1 else ''
                issues += [prefix + issue for issue in quality(step_row, rate, c['validation'])]
            if len(steps) > 1:
                record['summary']['steps'] = [dict(finite(r), rps=rate) for r, (rate, _) in zip(rows, steps)]
            record['validation_issues'] = issues
            if c['logging'].get('queue_snapshot_failures', False):
                record['queue_snapshot_frequency'] = queue_snapshot_frequency(
                    attempt / 'server', row['first_server_tsc'], duration)
                for label, name in (('memcached', 'lc'), ('antagonist', 'be')):
                    path = attempt / 'server' / (label + '.log')
                    if not path.exists():
                        continue
                    match = re.search(r'process pid:\s*(\d+)', path.read_text())
                    if match and match.group(1) in record['queue_snapshot_frequency']:
                        record['summary'][name + '_snapshot_rejection_percent'] = (
                            record['queue_snapshot_frequency'][match.group(1)]['rejection_percent'])
            if run['condition'] != 'memcached_alone' and c['batch_monitor']['enabled']:
                record['summary']['be_operations_per_second'] = batch_throughput(
                    attempt / 'server', row['first_server_tsc'], duration)
            if run['logging']:
                record['telemetry'] = validate_capture(attempt / 'server', c['logging']['reject_dropped_records'])
            if pktlog_on:
                record['pktlog'] = validate_pktlog(c, attempt)
                if c['pktlog']['join_after_collect']:
                    say('Joining client traces with the packet log')
                    record['join'] = join_run(c, attempt)
            if issues:
                raise RuntimeError('; '.join(issues))
            if record['state'] == 'finished':
                record['state'] = 'complete'
        except Exception as exc:
            record['state'] = 'failed'
            record['errors'].append('Validation: ' + str(exc))
        save(attempt / 'run.json', record)
        save(run_dir / 'latest.json', dict(attempt=attempt.name, **record))
    if interrupted:
        raise KeyboardInterrupt
    if record['state'] != 'complete':
        raise RuntimeError('%s failed: %s. Results: %s' % (run['id'], '; '.join(record['errors']), attempt))
    return record


def calibrate(c, suite_dir):
    settings = c['calibration']
    if not settings['enabled']:
        return dict(capacity_rps=settings['capacity_rps'], measured=False)
    if any(c['interventions'].values()):
        raise ValueError('Calibration requires interventions off; use a separate calibrated suite for controller experiments')
    trials = []
    def evaluate(rate):
        for _ in range(settings['confirmations']):
            run = dict(id='calibration-%03d-%drps' % (len(trials) + 1, rate),
                       condition='memcached_alone', logging=False, rps=rate, fraction=1.0, repetition=1)
            record = run_one(c, suite_dir, run, settings['warmup_seconds'], settings['measurement_seconds'])
            # High rates that saturate the client are not evidence of server capacity.
            issues = quality(record['summary'], rate, settings, True)
            trials.append(dict(rps=rate, issues=issues, run=run['id']))
            save(suite_dir / 'calibration-trials.json', trials)
            if issues:
                return False
        return True
    low, high = 0, None
    rate = settings['start_rps']
    while True:
        if evaluate(rate):
            low = rate
            if rate >= settings['maximum_rps']:
                raise RuntimeError('Calibration maximum still sustainable; raise maximum_rps to bracket capacity')
            rate = min(settings['maximum_rps'], max(rate + 1, round(rate * settings['rate_multiplier'])))
        else:
            high = rate
            break
    if not low:
        raise RuntimeError('Calibration starting load failed; reduce start_rps or inspect the run')
    for _ in range(settings['refinement_steps']):
        midpoint = (low + high) // 2
        if midpoint <= low:
            break
        if evaluate(midpoint):
            low = midpoint
        else:
            high = midpoint
    return dict(capacity_rps=low, failing_rps=high, measured=True, trials=trials)


def report(suite_dir, c):
    records = []
    for path in sorted(suite_dir.glob('*/latest.json')):
        value = json.loads(path.read_text())
        if not value['run']['id'].startswith('calibration-'):
            records.append(value)
    with (suite_dir / 'summary.csv').open('w', newline='') as output:
        fields = ['run', 'condition', 'fraction', 'repetition', 'logging', 'state', 'sent_rps',
                  'response_rps', 'p99_us', 'p99_9_us', 'drops', 'be_operations_per_second',
                  'lc_snapshot_rejection_percent', 'be_snapshot_rejection_percent']
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for value in records:
            run = value['run']
            row = dict(run=run['id'], **{k: run[k] for k in fields[1:5]}, state=value['state'])
            row.update({k: value.get('summary', {}).get(k) for k in fields[6:]})
            writer.writerow(row)
    pairs = {}
    for value in records:
        if value['state'] == 'complete':
            run = value['run']
            pairs.setdefault((run['condition'], run['fraction'], run['repetition']), {})[run['logging']] = value
    comparison = []
    for key, pair in pairs.items():
        if set(pair) != {False, True}:
            continue
        off, on = pair[False]['summary'], pair[True]['summary']
        latency = None if off['p99_9_us'] in (0, None) or on['p99_9_us'] is None else 100 * (on['p99_9_us'] / off['p99_9_us'] - 1)
        loss = None if not off['response_rps'] else 100 * (1 - on['response_rps'] / off['response_rps'])
        be_loss = None
        if off.get('be_operations_per_second'):
            be_loss = 100 * (1 - on['be_operations_per_second'] / off['be_operations_per_second'])
        comparison.append(dict(be_throughput_loss_percent=be_loss, condition=key[0], fraction=key[1], repetition=key[2],
                               p99_9_increase_percent=latency, response_throughput_loss_percent=loss,
                               flagged=(latency is None or loss is None or
                                  latency > c['validation']['maximum_logging_latency_increase_percent'] or
                                  loss > c['validation']['maximum_logging_throughput_loss_percent'] or
                                  (be_loss is not None and be_loss > c['validation']['maximum_logging_throughput_loss_percent']))))
    save(suite_dir / 'logging-comparison.json', comparison)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=TEMPLATE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true', help='Validate and preview; no SSH or file writes')
    mode.add_argument('--run', action='store_true', help='Execute configured setup, calibration, and the suite')
    mode.add_argument('--setup-only', action='store_true', help='Rehearse node setup and smoke tests; no experiments')
    mode.add_argument('--resume', type=Path, help='Resume a results suite directory with its saved config')
    args = parser.parse_args()
    if args.resume:
        suite_dir = args.resume.resolve()
        if suite_dir.parent != RESULTS.resolve():
            raise ValueError('Resume directory must be directly inside this repository\'s results/')
        import tomllib
        c = merge(tomllib.loads(TEMPLATE.read_text()), json.loads((suite_dir / 'config.json').read_text()))
        validate_config(c)
    else:
        c = load_config(args.config)
        suite_dir = RESULTS / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
    import importlib.util
    from types import SimpleNamespace
    setup_path = Path(__file__).with_name('setup_suite.py')
    spec = importlib.util.spec_from_file_location('experiment_setup', setup_path)
    setup_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup_module)
    if args.plan:
        say('Setup before --run: ' + ('enabled' if c['setup']['enabled'] else 'disabled; use --setup-only for a rehearsal'))
        for role in ('server', 'client'):
            if c['setup']['deploy_code']:
                say('Setup %s: deploy to fresh %s' % (role, c['nodes'][role]['repo']))
            for name, argv in setup_module.steps(c, role):
                say('Setup %s: %s: %s' % (role, name, shlex.join(argv)))
        if c['setup']['build_swaptions_gc'] and not (REPO / 'inc/runtime/gc.h').exists():
            say('SETUP BLOCKER: this checkout lacks the runtime GC integration required by swaptions-GC.')
        runs = plan(c, c['calibration']['capacity_rps'])
        say('Results: ' + str(suite_dir))
        say('Calibration: ' + ('automatic (rates below are provisional)' if c['calibration']['enabled'] else 'configured capacity'))
        for run in runs:
            say('%s: %d requests/s' % (run['id'], run['rps']))
        say('Example server command (logging on): ' + shlex.join(
            iokernel_command(c, c['nodes']['server'], '/REMOTE/RUN', True, True)))
        say('Example client command: ' + shlex.join(client_command(
            c, c['nodes']['client'], '/REMOTE/RUN', runs[0]['rps'],
            c['suite']['warmup_seconds'], c['suite']['measurement_seconds'])))
        seconds = len(runs) * (c['suite']['warmup_seconds'] + c['suite']['measurement_seconds'])
        say('%d runs; %.1f minutes warmup + measurement, excluding preload/calibration/finalization.' % (len(runs), seconds / 60))
        return
    if any('CHANGE_ME' in n['host'] or 'CHANGE_ME' in n['user'] for n in c['nodes'].values()):
        raise ValueError('Fill in both SSH hosts/users in suite.toml first')
    if c['nodes']['server']['host'] == c['nodes']['client']['host']:
        raise ValueError('Server and load generator must be different nodes')
    suite_dir.mkdir(parents=True, exist_ok=True)
    if not args.resume:
        save(suite_dir / 'config.json', c)
        shutil.copy2(args.config, suite_dir / 'source-config.toml')
        shutil.copy2(Path(__file__), suite_dir / 'launcher.py')
        shutil.copy2(REMOTE, suite_dir / 'remote.py')
        shutil.copy2(setup_path, suite_dir / 'setup_suite.py')
    say('Results: ' + str(suite_dir))
    setup_only = args.setup_only or (suite_dir / 'setup-only.json').exists()
    if setup_only:
        save(suite_dir / 'setup-only.json', {'requested': True})
    if setup_only or c['setup']['enabled']:
        context = SimpleNamespace(Node=Node, REPO=REPO, CONDITIONS=CONDITIONS,
                                  save=save, say=say, run_one=run_one)
        setup_module.run_setup(c, suite_dir, context)
        if setup_only:
            return
    if c['pktlog']['enabled']:
        # Once per suite: reserve hugepages and create the segment, or check the
        # existing one. Fails the suite before any run if the reservation fails.
        node = Node(c['nodes']['server'], suite_dir.name, 'pktlog-setup-' + uuid.uuid4().hex[:6],
                    uuid.uuid4().hex)
        node.bootstrap()
        try:
            p = c['pktlog']
            result = node.rpc('pktlog-setup', repo=node.config['repo'], path=p['segment_path'],
                              gib=p['gib'], node=p['node'], ctl=node.config['repo'] + '/' + PKTLOG_CTL,
                              sudo=node.config['sudo'])
            save(suite_dir / 'pktlog-setup.json', result)
            say('Packet log segment: %s' % json.dumps(result))
        finally:
            node.rpc('release')
    calibration_path = suite_dir / 'calibration.json'
    if calibration_path.exists():
        calibration = json.loads(calibration_path.read_text())
    else:
        calibration = calibrate(c, suite_dir)
        save(calibration_path, calibration)
    runs = plan(c, calibration['capacity_rps'])
    save(suite_dir / 'plan.json', runs)
    failures = 0
    for run in runs:
        latest = suite_dir / run['id'] / 'latest.json'
        if latest.exists() and json.loads(latest.read_text())['state'] == 'complete':
            say('Skipping completed ' + run['id'])
            continue
        say('Running ' + run['id'] + ' at %d requests/s' % run['rps'])
        try:
            run_one(c, suite_dir, run)
        except Exception as exc:
            failures += 1
            say(str(exc))
            if not c['suite']['continue_after_failure']:
                raise
            failed = json.loads(latest.read_text()) if latest.exists() else {}
            if (not failed.get('summary') or
                    any(not e.startswith('Validation:') for e in failed.get('errors', []))):
                raise RuntimeError('Infrastructure failure; refusing to advance suite') from exc
        finally:
            report(suite_dir, c)
    say('Suite complete: ' + str(suite_dir / 'summary.csv'))
    if failures:
        raise RuntimeError('%d runs failed; see summary.csv and per-run logs' % failures)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print('ERROR: ' + (str(exc) or type(exc).__name__), file=sys.stderr)
        sys.exit(1)
