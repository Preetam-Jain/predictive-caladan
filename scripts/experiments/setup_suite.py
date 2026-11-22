"""Run known node setup procedures and save their logs before any experiments."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid


def steps(c, role):
    """Ordered commands only; no implicit fixes, resets, checkouts, or retries."""
    settings, node = c['setup'], c['nodes'][role]
    scripts = node['repo'] + '/scripts/cloudlab/'
    result = []
    if settings['install_dependencies'] or settings['initialize_submodules']:
        argv = ['bash', scripts + 'setup.sh']
        if not settings['initialize_submodules']:
            argv += ['--packages-only']
        elif not settings['install_dependencies']:
            argv += ['--skip-packages']
        result.append(('dependencies-submodules', argv))
    if settings['build_runtime']:
        result.append(('build-runtime', ['bash', scripts + 'build.sh', '--jobs', str(settings['build_jobs'])]))
    if settings['apply_mlx5_fix']:
        result.append(('mlx5-fix', ['bash', scripts + 'fix-mlx5.sh']))
    if settings['configure_machine']:
        result.append(('configure-machine', ['bash', scripts + 'machine-setup.sh']))
    if settings['build_loadgen']:
        result.append(('build-loadgen', ['bash', scripts + 'build-loadgen.sh']))
    if role == 'server':
        workloads = [('build_memcached', '--memcached'), ('build_stream_dram', '--stream-dram'),
                     ('build_swaptions_gc', '--swaptions-gc')]
        selected = [flag for key, flag in workloads if settings[key]]
        if selected:
            argv = ['bash', scripts + 'build-workloads.sh', '--artifact', node['artifact'],
                    '--revision', settings['artifact_revision'], '--repository', settings['artifact_repository'],
                    '--jobs', str(settings['build_jobs']), *selected]
            if settings['install_dependencies']:
                argv += ['--install-deps']
            result.append(('build-workloads', argv))
    if settings['verify_machine']:
        result.append(('verify-machine', ['bash', scripts + 'machine-setup.sh', '--check-only']))
    result.append(('inventory', ['bash', scripts + 'specs.sh']))
    return result


def run_setup(c, suite_dir, launcher):
    settings = c['setup']
    setup_dir = suite_dir / 'setup'
    setup_dir.mkdir(exist_ok=True)
    completed = setup_dir / 'complete.json'
    if completed.exists():
        launcher.say('Skipping completed setup rehearsal: ' + str(completed))
        return
    # Earlier failed setup attempts retain logs; recover only their owned processes.
    previous = sorted(setup_dir.glob('attempt-*'))
    if previous:
        metadata = json.loads((previous[-1] / 'setup.json').read_text())
        for role, state in metadata['nodes'].items():
            node = launcher.Node(c['nodes'][role], suite_dir.name, 'recovery', state['token'])
            node.root = state['root']
            node.bootstrap()
            node.rpc('stop')
            node.rpc('setup-logs', repo=node.config['repo'])
            node.collect(previous[-1] / role)
            node.rpc('release')
    attempt = setup_dir / ('attempt-%03d' % (len(previous) + 1))
    attempt.mkdir()
    report = dict(state='running', started_unix=time.time(), nodes={}, stages=[], errors=[])
    nodes = {}
    interrupted = False
    try:
        for role in ('server', 'client'):
            token = uuid.uuid4().hex
            node = launcher.Node(c['nodes'][role], suite_dir.name,
                                 'setup-' + role + '-' + attempt.name, token)
            nodes[role] = node
            report['nodes'][role] = dict(root=node.root, token=token)
            launcher.save(attempt / 'setup.json', report)
            node.bootstrap()
            info = node.rpc('setup-info', sudo=node.config['sudo'])
            launcher.save(attempt / (role + '-info.json'), info)
            if settings['deploy_code']:
                destination = Path(node.config['repo'])
                if destination.parent != Path(info['home']):
                    raise ValueError('deploy_code requires nodes.%s.repo directly under its remote HOME (%s)' % (role, info['home']))
                argv = ['bash', str(launcher.REPO / 'scripts/cloudlab/deploy.sh'),
                        '--repo', destination.name, '--port', str(node.config['port']), '--batch']
                if node.config['identity_file']:
                    argv += ['--identity', str(Path(node.config['identity_file']).expanduser())]
                argv += [node.config['user'] + '@' + node.config['host']]
                launcher.say('Setup %s: deploying to a fresh checkout' % role)
                (attempt / role).mkdir(exist_ok=True)
                with (attempt / role / 'deployment.log').open('wb') as output:
                    subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, check=True,
                                   timeout=settings['stage_timeout_seconds'])
                report['stages'].append(dict(node=role, name='deploy', argv=argv, state='complete'))
                launcher.save(attempt / 'setup.json', report)
            for index, (name, argv) in enumerate(steps(c, role)):
                launcher.say('Setup %s: %s' % (role, name))
                stage = dict(node=role, name=name, argv=argv, state='running')
                report['stages'].append(stage)
                launcher.save(attempt / 'setup.json', report)
                label = '%02d-%s' % (index, name)
                # Build scripts run as the login user and invoke sudo themselves.
                # sudo -n was checked above; no password can be supplied over stdin.
                node.rpc('start', label=label, argv=['stdbuf', '-oL', '-eL', *argv],
                         cwd=node.config['repo'], sudo=False,
                         timeout=settings['stage_timeout_seconds'], env={})
                node.wait(label, settings['stage_timeout_seconds'] + 15)
                stage['state'] = 'complete'
                launcher.save(attempt / 'setup.json', report)
        report['state'] = 'configured'
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        report['state'] = 'failed'
        report['errors'].append(str(exc) or type(exc).__name__)
    finally:
        for role, node in nodes.items():
            if not node.initialized:
                continue
            stopped = False
            try:
                node.rpc('stop')
                stopped = True
            except Exception as exc:
                report['errors'].append(role + ' cleanup: ' + str(exc))
            try:
                node.rpc('setup-logs', repo=node.config['repo'])
                node.collect(attempt / role)
                if stopped:
                    node.rpc('release')
            except Exception as exc:
                report['errors'].append(role + ' log collection: ' + str(exc))
        if report['errors']:
            report['state'] = 'failed'
        report['finished_unix'] = time.time()
        launcher.save(attempt / 'setup.json', report)
    if interrupted:
        raise KeyboardInterrupt
    if report['state'] != 'configured':
        raise RuntimeError('Setup stopped: %s. Collected logs: %s' % ('; '.join(report['errors']), attempt))
    if settings['run_smoke_tests']:
        smoke_config = copy.deepcopy(c)
        smoke_config['memcached']['nvalues'] = settings['smoke_values']
        smoke_config['memcached']['trace_requests'] = False
        smoke_config['suite']['warmup_seconds'] = 0
        smoke_config['suite']['measurement_seconds'] = settings['smoke_seconds']
        smoke_dir = suite_dir / ('setup-smoke-' + suite_dir.name)
        smoke_dir.mkdir(exist_ok=True)
        # A small real memcached run exercises runtime networking; selected batch
        # pairings additionally prove their binary/runtime/monitor integration.
        conditions = ['memcached_alone'] + [name for name in launcher.CONDITIONS[1:] if c['conditions'][name]]
        for condition in conditions:
            launcher.say('Setup smoke test: ' + condition)
            run = dict(id=condition, condition=condition, rps=settings['smoke_rps'],
                       logging=False, fraction=1.0, repetition=1)
            launcher.run_one(smoke_config, smoke_dir, run, 0, settings['smoke_seconds'])
    report['state'] = 'complete'
    launcher.save(completed, report)
    launcher.say('Setup and requested smoke tests passed: ' + str(completed))
