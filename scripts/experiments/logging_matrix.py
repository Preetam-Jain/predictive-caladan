#!/usr/bin/env python3
"""Unattended logging/placement experiment, breadth first, with verified collection."""
from __future__ import annotations
import argparse
import copy
import csv
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import run as launcher


class MatrixNode(launcher.Node):
    def collect(self, destination):
        expected = super().collect(destination, compress=True)
        # Verification above has completed before any remote capture is removed.
        removed = self.rpc('prune-captures', verified=expected)
        launcher.save(destination / 'remote-pruned.json', removed)
        return expected


def plan(settings):
    rng = random.Random(settings['random_seed'])
    modes = [name for name in ('off', 'same_socket', 'other_socket')
             if settings['logging_' + name]]
    workloads = [name for name in launcher.CONDITIONS if settings[name]]
    rows = []
    for rep in range(1, settings['repetitions'] + 1):
        # Keep each configuration's off/on comparisons nearby, but rotate order.
        cells = [(b, w) for b in settings['be_cores'] for w in workloads]
        if rep > 1:
            rng.shuffle(cells)
        for be, workload in cells:
            order = modes.copy()
            rng.shuffle(order)
            # First run is a baseline, followed immediately by a real capture.
            if rep == 1 and be == settings['be_cores'][0] and workload == 'memcached_alone':
                order = modes.copy()
            for mode in order:
                cores = settings['application_cores'] - be
                rows.append(dict(id=f'r{rep:02d}-be{be:02d}-{workload}-{mode}',
                                 condition=workload, be_cores=be, cores=cores,
                                 writer_mode=mode, logging=mode != 'off',
                                 repetition=rep, fraction=settings['load_fraction'], phase='matrix'))
    return rows


def config_for(base, settings, row):
    c = copy.deepcopy(base)
    cores = row['cores']
    c['nodes']['server']['allowed_cpus'] = settings['server_allowed_cpus']
    c['allocation'].update(use_smt=False, memcached_cores=cores, memcached_guaranteed=cores,
                           antagonist_cores=row['be_cores'], antagonist_guaranteed=row['be_cores'],
                           writer_cpu=settings['same_socket_writer_cpu'] if row['writer_mode'] == 'same_socket'
                           else settings['other_socket_writer_cpu'])
    c['interventions'] = {name: False for name in c['interventions']}
    c['logging'].update(capture_seconds=settings['capture_seconds'], finalize_timeout_seconds=60, reject_dropped_records=True)
    c['suite'].update(warmup_seconds=settings['warmup_seconds'], measurement_seconds=settings['measurement_seconds'],
                      startup_timeout_seconds=120, client_timeout_seconds=210,
                      minimum_free_gb=15)
    c['stream_dram']['region_bytes_per_worker'] = settings['stream_bytes_per_worker']
    c['swaptions_gc'].update(user_threads=row['be_cores'], swaptions=settings['swaptions'], simulations=settings['simulations'])
    c['batch_monitor'].update(cpu=0, interval_us=100000)
    launcher.validate_config(c)
    return c


def publish(root, state):
    launcher.save(root / 'state.json', state)
    rows = []
    for t in state['trials']:
        if t['phase'] != 'matrix':
            continue
        s = t.get('summary', {})
        rows.append(dict(repetition=t['repetition'], workload=t['condition'],
                         memcached_cores=t['cores'], be_cores=0 if t['condition'] == 'memcached_alone' else t['be_cores'],
                         writer=t['writer_mode'], offered_rps=t['rps'], sent_rps=s.get('sent_rps'),
                         response_rps=s.get('response_rps'), p99_9_us=s.get('p99_9_us'), drops=s.get('drops'),
                         be_operations_per_second=s.get('be_operations_per_second'),
                         status=t['outcome'], errors='; '.join(t.get('errors', []))))
    launcher.save(root / 'summary.json', rows)
    if rows:
        with (root / 'summary.csv').open('w') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def can_start(settings):
    now = time.time()
    return (now < datetime.fromisoformat(settings['stop_new_trials']).timestamp() and
            now + settings['maximum_trial_seconds'] < datetime.fromisoformat(settings['reservation_end']).timestamp())


def execute(root, state, c, row, warmup, duration, settings):
    state['pending'] = row
    publish(root, state)
    launcher.say('Trial: ' + json.dumps(row))
    record = {}
    # Bound the entire run, including transfers. run_one still executes cleanup
    # on the alarm; its remote processes also have independent lifetime limits.
    def deadline(*_):
        raise TimeoutError('Trial wall-clock limit reached')
    old = signal.signal(signal.SIGALRM, deadline)
    try:
        for retry in range(settings['infrastructure_retries'] + 1):
            if retry and not can_start(settings):
                break
            signal.alarm(settings['maximum_trial_seconds'])
            try:
                record = launcher.run_one(c, root, row, warmup=warmup, duration=duration)
                break
            except Exception as exc:
                path = root / row['id'] / 'latest.json'
                record = json.loads(path.read_text()) if path.exists() else dict(state='failed', errors=[str(exc)])
                errors = record.get('errors', [])
                # Source or binary drift invalidates comparisons. Do not quietly continue.
                if any('changed during' in e or 'Binary changed' in e for e in errors):
                    raise RuntimeError('Provenance changed; stopping suite: ' + '; '.join(errors))
                if record.get('summary') and all(e.startswith('Validation:') for e in errors):
                    break # Overload/dropped telemetry is retained, not rerun until it passes.
                if retry == settings['infrastructure_retries']:
                    break
                launcher.say('Retrying infrastructure failure: ' + '; '.join(errors))
            finally:
                signal.alarm(0)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    trial = dict(row, summary=record.get('summary', {}),
                 outcome=record.get('state', 'failed'), errors=record.get('errors', []),
                 source=str(root / row['id'] / 'latest.json'))
    # A fully constructed client summary is still useful for overload analysis.
    trial['summary'].setdefault('send_backpressure', False)
    logs = sorted((root / row['id']).glob('attempt-*/client/loadgen.log'))
    if logs:
        trial['summary']['send_backpressure'] = 'TCP send buffer was full' in logs[-1].read_text()
    state['trials'].append(trial)
    state['pending'] = None
    publish(root, state)
    launcher.say(json.dumps(trial))
    # Measurement-quality failures are retained as observations. An unresolved
    # infrastructure/cleanup failure makes later conditions unsafe to launch.
    if trial['outcome'] != 'complete' and (
            not record.get('summary') or
            any(not e.startswith('Validation:') for e in trial['errors'])):
        raise RuntimeError('Infrastructure failure; suite stopped: ' + row['id'])
    return trial


def passing(t, settings):
    s = t.get('summary', {})
    return (t['outcome'] == 'complete' and s.get('p99_9_us') is not None and
            s['p99_9_us'] < settings['maximum_baseline_p99_9_us'] and not s.get('drops') and
            s.get('sent_rps', 0) >= .99*t['rps'] and s.get('response_rps', 0) >= .99*t['rps'] and
            s.get('schedule_adherence_percent', 0) >= 99)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=Path(__file__).with_suffix('.toml'))
    p.add_argument('--output', type=Path)
    p.add_argument('--resume', type=Path)
    p.add_argument('--run', action='store_true')
    args = p.parse_args()
    settings = tomllib.loads(args.config.read_text())['matrix']
    base = launcher.load_config(args.config.parent / settings['suite_config'])
    jobs = plan(settings)
    if not args.run:
        print(json.dumps(dict(matrix_runs=len(jobs), first_repetition=[r for r in jobs if r['repetition']==1], settings=settings), indent=2))
        return
    root = (args.resume or args.output or launcher.RESULTS / ('logging-matrix-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.resume:
            settings = json.loads((root / 'search-settings.json').read_text())
            base = json.loads((root / 'config.json').read_text())
            jobs = json.loads((root / 'plan.json').read_text())
            state = json.loads((root / 'state.json').read_text())
            # Re-enter a pending trial using run_one's owned-process recovery.
        else:
            state = dict(kind='logging_matrix', status='running', trials=[], pending=None, rates={})
            launcher.save(root / 'config.json', base)
            launcher.save(root / 'search-settings.json', settings)
            launcher.save(root / 'plan.json', jobs)
            for filename in ('logging_matrix.py', 'run.py', 'remote.py'):
                shutil.copy2(Path(__file__).with_name(filename), root / filename)
        launcher.Node = MatrixNode
        publish(root, state)
        try:
            # One quick isolated verification per actual LC allocation, with bounded
            # backoff if the candidate from the 34-core search is too aggressive.
            for be in settings['be_cores']:
                cores = settings['application_cores']-be
                if str(cores) in state['rates']:
                    continue
                rate = round(settings['reference_passing_rps'] * cores / settings['reference_cores'] * settings['load_fraction']/10000)*10000
                found = False
                for attempt in range(settings['calibration_attempts']):
                    if not can_start(settings):
                        state['status']='deadline_reached';return
                    row = dict(id=f'calibration-c{cores:02d}-{attempt:02d}-{rate}rps',
                               condition='memcached_alone',be_cores=be,cores=cores,
                               writer_mode='off',logging=False,repetition=0,fraction=1.,phase='calibration',rps=rate)
                    c = config_for(base, settings, row)
                    trial=execute(root,state,c,row,settings['calibration_warmup_seconds'],settings['calibration_measurement_seconds'],settings)
                    if passing(trial,settings):
                        state['rates'][str(cores)]=rate;found=True;break
                    if not trial['summary']:
                        raise RuntimeError('Calibration infrastructure failed; inspect saved logs')
                    rate=round(rate*settings['calibration_backoff']/10000)*10000
                if not found:
                    raise RuntimeError(f'No stable passing load for {cores} cores within bounded calibration')
                publish(root,state)
            done={t['id'] for t in state['trials']}
            for row in jobs:
                if row['id'] in done:
                    continue
                if not can_start(settings):
                    state['status']='deadline_reached';break
                row=dict(row,rps=state['rates'][str(row['cores'])])
                c=config_for(base,settings,row)
                execute(root,state,c,row,settings['warmup_seconds'],settings['measurement_seconds'],settings)
            else:
                state['status']='complete_with_failures' if any(t['outcome']!='complete' for t in state['trials']) else 'complete'
        except KeyboardInterrupt:
            state['status']='interrupted'
        except Exception as exc:
            state['status']='failed';state['error']=str(exc);raise
        finally:
            state['pending']=None
            publish(root,state)
            launcher.say('Final status: '+state['status'])


if __name__ == '__main__':
    main()
