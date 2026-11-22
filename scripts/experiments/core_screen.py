#!/usr/bin/env python3
"""Low-load physical-core screen using the regular two-node experiment launcher."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import run as launcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=launcher.TEMPLATE)
    parser.add_argument('--cores', type=int, nargs='+', default=[1, 4, 16, 34])
    parser.add_argument('--rps', type=int, default=10000)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    config = launcher.load_config(args.config)
    config['allocation']['use_smt'] = False
    config['suite'].update(warmup_seconds=10, measurement_seconds=30, repetitions=1)
    config['logging'].update(run_with_logging=False, run_without_logging=True)
    config['conditions'] = {name: name == 'memcached_alone' for name in launcher.CONDITIONS}
    config['calibration']['enabled'] = False
    if any(config['interventions'].values()):
        raise ValueError('The isolated screen requires all interventions off')
    if not args.cores or any(n <= 0 for n in args.cores) or args.rps <= 0:
        raise ValueError('Core counts and request rate must be positive')
    launcher.say(f'Physical cores: {args.cores}; {args.rps} RPS; 10s warmup + 30s measurement; logging off')
    if not args.run:
        return
    root = launcher.RESULTS / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-core-screen-' + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    launcher.say('Results: ' + str(root))
    shutil.copy2(__file__, root / 'core_screen.py')
    shutil.copy2(launcher.__file__, root / 'launcher.py')
    launcher.save(root / 'screen.json', dict(cores=args.cores, offered_rps=args.rps, p99_9_target_us=50,
                                         original_requested_maximum=35, physical_cores_reserved_by_noht=2))
    rows = []
    for cores in args.cores:
        c = copy.deepcopy(config)
        c['allocation'].update(memcached_cores=cores, memcached_guaranteed=cores)
        launcher.validate_config(c)
        run = dict(id=f'{cores:02d}-physical-cores', condition='memcached_alone', rps=args.rps,
                   logging=False, fraction=1.0, repetition=1, physical_cores=cores)
        launcher.say(f'Starting {cores} physical cores')
        done = threading.Event()
        # Sample once during initialization/preload, before the timed measurement.
        def placement():
            node = c['nodes']['server']
            probe = '''import json,subprocess,os
from pathlib import Path
p=subprocess.run(['pgrep','-x','memcached'],capture_output=True,text=True)
if p.returncode: raise SystemExit(1)
pid=int(p.stdout.strip())
tasks=Path('/proc')/str(pid)/'task'
aff={t.name:sorted(os.sched_getaffinity(int(t.name))) for t in tasks.iterdir()}
print(json.dumps(dict(pid=pid,affinities=aff)))
'''
            argv = ['ssh', '-oBatchMode=yes', '-oConnectTimeout=10', '-p', str(node['port'])]
            if node['identity_file']:
                argv += ['-i', str(Path(node['identity_file']).expanduser())]
            argv += [node['user']+'@'+node['host'], 'sudo -n python3 -']
            while not done.wait(2):
                p = subprocess.run(argv, input=probe, text=True, capture_output=True, timeout=20)
                if p.returncode == 0:
                    info = json.loads(p.stdout)
                    if len(info['affinities']) == cores:
                        launcher.save(root / (run['id']+'-placement.json'), info)
                        return
        monitor = threading.Thread(target=placement, daemon=True)
        monitor.start()
        try:
            result = launcher.run_one(c, root, run, 10, 30)
            s = result['summary']
            row = dict(physical_cores=cores, **s)
            row['meets_50us'] = (s['p99_9_us'] is not None and s['p99_9_us'] < 50 and
                                 s['drops'] == 0 and s['response_rps'] >= args.rps * 0.99)
            rows.append(row)
            launcher.save(root / 'summary.json', rows)
            launcher.say(json.dumps(row))
        finally:
            done.set()
            monitor.join(timeout=25)
    launcher.say('Screen complete: ' + str(root / 'summary.json'))


if __name__ == '__main__':
    main()
