#!/usr/bin/env python3
"""Checkpointed two-target load search, using the desktop experiment launcher."""
from __future__ import annotations
import argparse
import copy
import csv
from datetime import datetime, timezone
import fcntl
import json
import math
from pathlib import Path
import shutil
import time
import tomllib
import uuid
import run as launcher

TEMPLATE = Path(__file__).with_suffix('.toml')
VERSION = 1


def client_ok(summary, rate, settings):
    return (summary['sent_rps'] >= rate * settings['minimum_send_fraction'] and
            summary['schedule_adherence_percent'] >= settings['minimum_schedule_adherence_percent'])


def classify(summary, rate, target, settings):
    if not client_ok(summary, rate, settings):
        return 'client_limited'
    latency = summary.get('p99_9_us')
    if (latency is None or not math.isfinite(latency) or latency >= target or
            summary['response_rps'] < rate * settings['minimum_response_fraction'] or summary['drops']):
        return 'fail'
    return 'pass'


def groups(trials, target, settings):
    rates = {}
    for trial in trials:
        rate = trial['rps']
        rates.setdefault(rate, []).append(classify(trial['summary'], rate, target, settings))
    result = {}
    for rate, outcomes in rates.items():
        # A mixed outcome is not a confirmed passing rate.
        outcome = 'fail' if 'fail' in outcomes else 'client_limited' if 'client_limited' in outcomes else 'pass'
        result[rate] = dict(outcome=outcome, passes=outcomes.count('pass'), observations=len(outcomes),
                            inconsistent=len(set(outcomes)) > 1)
    return result


def target_result(trials, target, settings):
    rates = groups(trials, target, settings)
    passes = [r for r, v in rates.items() if v['outcome'] == 'pass']
    low = max(passes, default=None)
    higher = [r for r, v in rates.items() if v['outcome'] != 'pass' and (low is None or r > low)]
    high = min(higher, default=None)
    confirmed = low is not None and rates[low]['passes'] >= settings['confirmations']
    status = 'searching'
    if low is not None and confirmed:
        if high is not None and (high - low) / low <= settings['relative_precision']:
            status = 'bracketed' if rates[high]['outcome'] == 'fail' else 'client_limited'
        elif low == settings['maximum_rps']:
            status = 'search_ceiling_reached'
    return dict(p99_9_target_us=target, highest_passing_rps=low, upper_rps=high,
                upper_kind=rates[high]['outcome'] if high is not None else None,
                confirmed=confirmed, status=status,
                nonmonotonic_observations=low is not None and any(
                    r < low and v['outcome'] == 'fail' for r, v in rates.items()),
                inconsistent_rates=[r for r, v in sorted(rates.items()) if v['inconsistent']])


def next_trial(trials, settings):
    """Return (rate, phase), or None; decisions are reconstructed from saved trials."""
    largest_target = max(settings['p99_9_targets_us'])
    rates = groups(trials, largest_target, settings)
    rate = settings['start_rps']
    # Exponential exploration is shared by every latency target.
    while True:
        if rate not in rates:
            return rate, 'exponential'
        if rates[rate]['outcome'] != 'pass' or rate == settings['maximum_rps']:
            break
        rate = min(settings['maximum_rps'], max(rate + 1, round(rate * settings['rate_multiplier'])))
    for target in sorted(settings['p99_9_targets_us']):
        result = target_result(trials, target, settings)
        if result['status'] != 'searching':
            continue
        low, high = result['highest_passing_rps'], result['upper_rps']
        if low is None:
            # Failure at low load need not be monotonic; expansion above already
            # explored higher rates, and these probes check lower ones as well.
            tested = {t['rps'] for t in trials}
            candidate = settings['start_rps']
            while candidate > settings['minimum_rps']:
                candidate = max(settings['minimum_rps'], candidate // 2)
                if candidate not in tested:
                    return candidate, 'lower-load-probe'
            continue
        if high is not None and (high - low) / low > settings['relative_precision']:
            candidate = (high + low) // 2
            if candidate > low:
                return candidate, 'binary'
        if not result['confirmed']:
            return low, 'confirmation'
    return None


def conclusions(trials, settings):
    result = []
    for target in sorted(settings['p99_9_targets_us']):
        row = target_result(trials, target, settings)
        if row['highest_passing_rps'] is None:
            row['status'] = 'no_passing_rate_observed'
        result.append(row)
    return result


def config_for(base, settings, cores):
    c = copy.deepcopy(base)
    c['allocation'].update(use_smt=False, memcached_cores=cores, memcached_guaranteed=cores)
    c['suite'].update(warmup_seconds=settings['warmup_seconds'], measurement_seconds=settings['measurement_seconds'], repetitions=1)
    c['logging'].update(run_without_logging=True, run_with_logging=False)
    c['conditions'] = {n: n == 'memcached_alone' for n in launcher.CONDITIONS}
    c['calibration']['enabled'] = False
    c['validation'].update(minimum_send_fraction=settings['minimum_send_fraction'],
                           minimum_schedule_adherence_percent=settings['minimum_schedule_adherence_percent'])
    if any(c['interventions'].values()):
        raise ValueError('Isolated search requires interventions off')
    launcher.validate_config(c)
    return c


def import_screen(root, base, settings):
    source = launcher.REPO / settings['seed_results']
    trials = []
    for cores in settings['physical_cores']:
        latest = json.loads((source / f'{cores:02d}-physical-cores/latest.json').read_text())
        old = source / f'{cores:02d}-physical-cores' / latest['attempt']
        original = json.loads((old / 'config.json').read_text())
        expected = config_for(base, settings, cores)
        for key in ('allocation', 'network', 'memcached', 'interventions', 'nodes'):
            if original[key] != expected[key]:
                raise ValueError(f'Seed screen differs in {key}; cannot reuse {cores}-core result')
        for key in ('warmup_seconds', 'measurement_seconds'):
            if original['suite'][key] != expected['suite'][key]:
                raise ValueError('Seed measurement window differs')
        if latest['state'] != 'complete' or latest['run']['logging'] or latest['run']['rps'] != settings['start_rps']:
            raise ValueError('Seed is not a completed logging-off starting-rate run')
        trials.append(dict(cores=cores, rps=settings['start_rps'], phase='imported-screen',
                           summary=latest['summary'], source=str(old)))
    # Existing run_one provenance checks enforce identical remote sources/binaries.
    for role in ('server', 'client'):
        shutil.copy2(source / f'provenance-{role}.json', root / f'provenance-{role}.json')
    return trials


def publish(root, state, settings):
    launcher.save(root / 'state.json', state)
    rows = []
    for cores in settings['physical_cores']:
        trials = [t for t in state['trials'] if t['cores'] == cores]
        for row in conclusions(trials, settings):
            rows.append(dict(physical_cores=cores, **row))
    launcher.save(root / 'summary.json', rows)
    with (root / 'summary.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def execute_pending(root, state, base, settings):
    pending = state['pending']
    c = config_for(base, settings, pending['cores'])
    run = dict(id=pending['id'], condition='memcached_alone', rps=pending['rps'],
               logging=False, fraction=1.0, repetition=1, physical_cores=pending['cores'])
    latest_path = root / run['id'] / 'latest.json'
    for retry in range(settings['infrastructure_retries'] + 1):
        latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
        if latest.get('state') == 'complete':
            record = latest
        else:
            try:
                record = launcher.run_one(c, root, run, settings['warmup_seconds'], settings['measurement_seconds'])
            except Exception:
                record = json.loads(latest_path.read_text()) if latest_path.exists() else {}
                # The normal launcher rejects client-limited trials; retain their
                # summaries as censored observations, not server latency failures.
                errors = record.get('errors', [])
                client_limited = (record.get('summary') and record.get('validation_issues') and
                                  all(e.startswith('Validation:') for e in errors) and
                                  not client_ok(record['summary'], pending['rps'], settings))
                if not client_limited:
                    if retry == settings['infrastructure_retries']:
                        raise
                    launcher.say('Retrying infrastructure failure: ' + run['id'])
                    continue
        trial = dict(cores=pending['cores'], rps=pending['rps'], phase=pending['phase'],
                     summary=record['summary'], source=str(latest_path))
        state['trials'].append(trial)
        state['pending'] = None
        publish(root, state, settings)
        launcher.say(json.dumps(trial))
        return


def validate_settings(s):
    if not s['physical_cores'] or any(type(n) is not int or n < 1 for n in s['physical_cores']):
        raise ValueError('Positive physical core counts are required')
    if len(set(s['physical_cores'])) != len(s['physical_cores']):
        raise ValueError('Duplicate core counts')
    if not s['p99_9_targets_us'] or any(t <= 0 or not math.isfinite(t) for t in s['p99_9_targets_us']):
        raise ValueError('Positive latency targets are required')
    if not 0 < s['minimum_rps'] <= s['start_rps'] <= s['maximum_rps']:
        raise ValueError('Invalid search rates')
    if s['rate_multiplier'] <= 1 or not 0 < s['relative_precision'] < 1:
        raise ValueError('Invalid search multiplier/precision')
    if s['confirmations'] < 1 or s['maximum_trials_per_core'] < 1 or s['infrastructure_retries'] < 0:
        raise ValueError('Invalid trial limits')
    if s['stop_before']:
        stop = datetime.fromisoformat(s['stop_before'])
        if stop.tzinfo is None:
            raise ValueError('stop_before must include a timezone')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=TEMPLATE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--resume', type=Path)
    mode.add_argument('--status', type=Path, help='Print saved progress without SSH')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.status:
        root = args.status.resolve()
        state = json.loads((root / 'state.json').read_text())
        print(json.dumps(dict(status=state['status'], pending=state['pending'],
                              completed_trials=len(state['trials']),
                              summary=json.loads((root / 'summary.json').read_text())), indent=2))
        return
    if args.resume:
        root = args.resume.resolve()
        settings = json.loads((root / 'search-settings.json').read_text())
        base = json.loads((root / 'config.json').read_text())
    else:
        settings = launcher.merge(tomllib.loads(TEMPLATE.read_text()), tomllib.loads(args.config.read_text()))
        base = launcher.load_config(args.config.parent / settings['suite_config'])
        root = (args.output or launcher.RESULTS / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-peak-search-' + uuid.uuid4().hex[:6])).resolve()
    validate_settings(settings)
    for cores in settings['physical_cores']:
        config_for(base, settings, cores)
    if args.plan:
        print(json.dumps(settings, indent=2))
        return
    if not root.is_relative_to(launcher.RESULTS.resolve()) or root == launcher.RESULTS.resolve():
        raise ValueError('Search output must be inside results/')
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.search.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / 'state.json'
        if args.resume:
            state = json.loads(path.read_text())
            if state['version'] != VERSION:
                raise ValueError('Unsupported search checkpoint')
        else:
            if path.exists():
                raise ValueError('Output already has a search; use --resume')
            launcher.save(root / 'search-settings.json', settings)
            launcher.save(root / 'config.json', base)
            for source in (Path(__file__), Path(launcher.__file__), launcher.REMOTE):
                shutil.copy2(source, root / source.name)
            trials = import_screen(root, base, settings) if settings['reuse_screen'] else []
            state = dict(version=VERSION, status='running', trials=trials, pending=None)
        launcher.say('Results: ' + str(root))
        try:
            state['status'] = 'running'
            publish(root, state, settings)
            for cores in settings['physical_cores']:
                while True:
                    trials = [t for t in state['trials'] if t['cores'] == cores]
                    choice = next_trial(trials, settings)
                    if state['pending'] is None and choice is None:
                        break
                    if len([t for t in trials if t['phase'] != 'imported-screen']) >= settings['maximum_trials_per_core']:
                        state['status'] = 'trial_limit_reached'
                        publish(root, state, settings)
                        return
                    # Avoid starting a run that could overrun the reservation.
                    reserve = base['suite']['client_timeout_seconds'] + 2 * base['suite']['startup_timeout_seconds'] + 120
                    if settings['stop_before'] and (datetime.fromisoformat(settings['stop_before']) - datetime.now(timezone.utc)).total_seconds() < reserve:
                        state['status'] = 'reservation_deadline_reached'
                        publish(root, state, settings)
                        return
                    if state['pending'] is None:
                        rate, phase = choice
                        state['pending'] = dict(cores=cores, rps=rate, phase=phase,
                                                id=f'c{cores:02d}-trial{len(trials):03d}-{rate}rps')
                        publish(root, state, settings)
                    launcher.say('Trial: ' + json.dumps(state['pending']))
                    execute_pending(root, state, base, settings)
            state['status'] = 'complete'
        except KeyboardInterrupt:
            state['status'] = 'interrupted'
            raise
        except Exception as exc:
            state['status'] = 'failed'
            state['error'] = str(exc)
            raise
        finally:
            publish(root, state, settings)
        launcher.say('Search complete: ' + str(root / 'summary.json'))


if __name__ == '__main__':
    main()
