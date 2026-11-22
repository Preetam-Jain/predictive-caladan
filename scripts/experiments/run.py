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


def runtime_config(c, role):
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
    return text


def iokernel_command(c, node, root, server, logging):
    argv = [node['repo'] + '/iokerneld', 'ias', 'numanode', str(node['numa_node']), 'nicpci', node['nic_pci']]
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


def client_command(c, node, root, rps, warmup, measurement, probe=False):
    m = c['memcached']
    schedule = []
    if warmup:
        schedule.append('%d:%d:silent' % (rps, warmup * 1000000))
    schedule.append('%d:%d' % (rps, measurement * 1000000))
    return [node['repo'] + '/apps/loadgen/target/release/loadgen',
            '%s:%d' % (c['network']['server_ip'], c['network']['port']),
            '--config', root + '/client.config', '--mode', 'runtime-client',
            '--protocol', 'memcached', '--transport', 'tcp',
            '--conns', str(1 if probe else m['client_connections']),
            '--nvalues', str(1 if probe else m['nvalues']), '--key_size', str(m['key_size']),
            '--value_size', str(m['value_size']), '--pctset', str(m['pctset']),
            '--output', 'trace' if m['trace_requests'] and not probe else 'normal',
            '--loadshift', ','.join(schedule)]


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
        subprocess.run(self.ssh + [shlex.join(['python3', '-c', code])], check=True, timeout=40)
        self.rpc('init')
        self.initialized = True

    def rpc(self, action, **data):
        payload = dict(root=self.root, token=self.token, **data)
        proc = subprocess.run(self.ssh + [shlex.join(['python3', self.helper, action])],
                              input=json.dumps(payload), text=True, capture_output=True,
                              timeout=120 if action in ('manifest', 'stop') else 40)
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

    def collect(self, destination):
        expected = self.rpc('manifest')
        destination.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(destination).free < sum(v['size'] for v in expected.values()):
            raise RuntimeError('Not enough local disk space to collect remote results')
        proc = subprocess.Popen(self.ssh + [shlex.join(['tar', '-C', self.root, '-cf', '-', '.'])],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=proc.stdout, mode='r|') as archive:
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
    if not server:
        executables.append(n['repo'] + '/apps/loadgen/target/release/loadgen')
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


def validate_capture(directory, reject_drops):
    sys.path.insert(0, str(REPO / 'scripts'))
    from decode_telemetry import capture_streams, read_header
    prefix = str(directory / 'capture')
    identity = None
    streams = {}
    for suffix, kind in capture_streams(prefix):
        with open(prefix + '.' + suffix + '.bin', 'rb') as source:
            header, cores = read_header(source)
            current = (header.version, header.experiment_id, header.cycles_per_us,
                       header.writer_core, header.scheduler_core, cores)
            if header.stream_type != kind or (identity is not None and identity != current):
                raise RuntimeError('Telemetry streams have mismatched identities')
            identity = current
            if Path(source.name).stat().st_size != header.header_bytes + header.produced * header.record_bytes:
                raise RuntimeError('Truncated or extra telemetry data: ' + source.name)
            streams[suffix] = dict(produced=header.produced, dropped=header.dropped)
    save(directory / 'capture-validation.json', streams)
    if any(not v['produced'] for k, v in streams.items() if k in ('bw', 'queue', 'task', 'delay')):
        raise RuntimeError('Expected telemetry stream is empty')
    if reject_drops and any(v['dropped'] for v in streams.values()):
        raise RuntimeError('Telemetry records were dropped; see capture-validation.json')
    return streams


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
        for role, node in nodes.items():
            config_role = 'memcached' if role == 'server' else 'client'
            node.rpc('write', name=config_role + '.config', text=runtime_config(c, config_role))
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
        if run['condition'] != 'memcached_alone':
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
        client.start('loadgen', client_command(c, client.config, client.root, run['rps'], warmup, duration),
                     c['suite']['client_timeout_seconds'])
        def heartbeat():
            server.assert_alive(*labels)
            client.assert_alive('iokernel')
        state = client.wait('loadgen', c['suite']['client_timeout_seconds'] + 10, heartbeat)
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
            try:
                for retry in range(3):
                    try:
                        node.collect(attempt / role)
                        break
                    except Exception:
                        if retry == 2:
                            raise
                        say('Retrying result transfer from ' + role)
                        time.sleep(2)
                if stopped:
                    node.rpc('release')
            except Exception as exc:
                record['errors'].append(role + ' collection: ' + str(exc))
                record['state'] = 'failed'
        record['finished_unix'] = time.time()
        try:
            rows = summaries((attempt / 'client/loadgen.log').read_text())
            if len(rows) != 1:
                raise RuntimeError('Expected exactly one measured loadgen summary')
            row = rows[0]
            record['summary'] = {k: v if math.isfinite(v) else None for k, v in row.items()}
            issues = quality(row, run['rps'], c['validation'])
            record['validation_issues'] = issues
            if run['condition'] != 'memcached_alone' and c['batch_monitor']['enabled']:
                record['summary']['be_operations_per_second'] = batch_throughput(
                    attempt / 'server', row['first_server_tsc'], duration)
            if run['logging']:
                record['telemetry'] = validate_capture(attempt / 'server', c['logging']['reject_dropped_records'])
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
                  'response_rps', 'p99_us', 'p99_9_us', 'drops', 'be_operations_per_second']
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
