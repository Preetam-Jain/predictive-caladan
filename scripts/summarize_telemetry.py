#!/usr/bin/env python3
"""Summarize all three telemetry files without materializing a dataset or CSV."""
import argparse
import heapq
from collections import Counter, defaultdict
import sys
import uuid

from decode_telemetry import capture_streams, iter_records, read_header

MICROSECONDS_PER_SECOND = 1_000_000
CAS_COUNTER_MODULUS = 1 << 32
MAX_INTERVAL_SAMPLES = 8192
LARGEST_GAPS_TO_REPORT = 5
LONG_GAP_MICROSECONDS = 100


class IntervalStats:
    def __init__(self, record_count):
        self.count = 0
        self.total = 0
        self.minimum = None
        self.maximum = 0
        self.samples = []
        self.sample_stride = max(1, record_count // MAX_INTERVAL_SAMPLES)

    def add(self, ticks):
        self.count += 1
        self.total += ticks
        self.minimum = ticks if self.minimum is None else min(self.minimum, ticks)
        self.maximum = max(self.maximum, ticks)
        if (self.count - 1) % self.sample_stride == 0 and len(self.samples) < MAX_INTERVAL_SAMPLES:
            self.samples.append(ticks)

    def describe(self, cycles_per_us):
        if not self.count:
            return 'no valid intervals'
        samples = sorted(self.samples)
        median = samples[len(samples) // 2] / cycles_per_us
        p99 = samples[min(len(samples) - 1, int(len(samples) * 0.99))] / cycles_per_us
        return (f'min={self.minimum / cycles_per_us:.3f} '
                f'mean={self.total / self.count / cycles_per_us:.3f} '
                f'p50~={median:.3f} p99~={p99:.3f} '
                f'max={self.maximum / cycles_per_us:.3f} us')


def summarize(prefix):
    headers = []
    bandwidth_intervals = None
    bandwidth_cas = 0
    bandwidth_nonpositive = 0
    largest_gaps = []
    long_gap_count = 0
    same_pid_generation_changes = Counter()
    bandwidth_first = bandwidth_last = None
    llc_first = llc_last = None
    core_stats = defaultdict(lambda: dict(records=0, intervals=0, misses=0, ticks=0))
    raw_core_stats = defaultdict(lambda: dict(records=0, intervals=0, misses=0, ticks=0))
    failure_counts = Counter()
    skipped_llc = Counter()
    scheduler_events = Counter()
    expected_identity = None

    for suffix, expected_type in capture_streams(prefix):
        with open(f'{prefix}.{suffix}.bin', 'rb') as source:
            header, cores = read_header(source)
            if header.stream_type != expected_type:
                raise ValueError(f'{suffix}: wrong stream type')
            identity = (header.version, header.experiment_id, header.cycles_per_us, header.writer_core,
                        header.scheduler_core, cores)
            if expected_identity is not None and identity != expected_identity:
                raise ValueError('stream experiment IDs or hardware metadata do not match')
            expected_identity = identity
            headers.append((suffix, header))
            previous_bandwidth = None
            previous_core = {}
            if suffix == 'bw':
                bandwidth_intervals = IntervalStats(header.produced)
            for record_index, record in enumerate(iter_records(source, header)):
                if suffix == 'bw':
                    tsc, cas = record
                    if bandwidth_first is None:
                        bandwidth_first = tsc
                    bandwidth_last = tsc
                    if previous_bandwidth is not None:
                        previous_tsc, previous_cas = previous_bandwidth
                        ticks = tsc - previous_tsc
                        if ticks > 0:
                            bandwidth_intervals.add(ticks)
                            if ticks >= LONG_GAP_MICROSECONDS * header.cycles_per_us:
                                long_gap_count += 1
                            gap = (ticks, previous_tsc, tsc, record_index)
                            if len(largest_gaps) < LARGEST_GAPS_TO_REPORT:
                                heapq.heappush(largest_gaps, gap)
                            elif ticks > largest_gaps[0][0]:
                                heapq.heapreplace(largest_gaps, gap)
                            bandwidth_cas += (cas - previous_cas) % CAS_COUNTER_MODULUS
                        else:
                            bandwidth_nonpositive += 1
                    previous_bandwidth = record
                elif suffix == 'llc':
                    if header.version >= 7:
                        # Collection scheduling generation is diagnostic metadata;
                        # application attribution still uses ownership generations.
                        record = record[:6] + record[7:]
                    if header.version >= 6:
                        (tsc, sched_gen, misses, app_id, owner_at_request,
                         owner_at_collection, core, pid) = record
                    else:
                        tsc, sched_gen, misses, core, pid = record
                        app_id = None
                        owner_at_request = owner_at_collection = sched_gen
                    llc_first = tsc if llc_first is None else min(llc_first, tsc)
                    llc_last = tsc if llc_last is None else max(llc_last, tsc)
                    stats = core_stats[core, pid, app_id]
                    stats['records'] += 1
                    raw_stats = raw_core_stats[core]
                    raw_stats['records'] += 1
                    previous = previous_core.get(core)
                    if previous is not None:
                        (previous_tsc, previous_sched_gen, previous_misses, previous_pid,
                         previous_app_id, previous_owner_request, previous_owner_collection) = previous
                        if sched_gen != previous_sched_gen and pid == previous_pid:
                            same_pid_generation_changes[core] += 1
                        if tsc <= previous_tsc:
                            skipped_llc['nonpositive time'] += 1
                        elif misses < previous_misses:
                            # Hardware width is absent: never invent wrap counts.
                            skipped_llc['counter decreased (wrap/reset)'] += 1
                        else:
                            ticks = tsc - previous_tsc
                            miss_delta = misses - previous_misses
                            raw_stats['intervals'] += 1
                            raw_stats['ticks'] += ticks
                            raw_stats['misses'] += miss_delta
                            same_owner = (
                                app_id != 0 and app_id == previous_app_id
                                and pid == previous_pid
                                and owner_at_request == owner_at_collection
                                and previous_owner_request == previous_owner_collection
                                and owner_at_request == previous_owner_request
                            )
                            if same_owner:
                                stats['intervals'] += 1
                                stats['ticks'] += ticks
                                stats['misses'] += miss_delta
                            else:
                                reason = ('application ownership changed/uncertain'
                                          if header.version >= 6 else 'assignment changed')
                                skipped_llc[reason] += 1
                    previous_core[core] = (tsc, sched_gen, misses, pid, app_id,
                                           owner_at_request, owner_at_collection)
                elif suffix == 'pmc-failure':
                    _, _, core, reason = record
                    failure_counts[core, reason] += 1
                elif suffix == 'sched':
                    _, _, pid, core, reason = record
                    scheduler_events[core, pid, reason] += 1

    bandwidth_header = headers[0][1]
    frequency = bandwidth_header.cycles_per_us
    print(f'Experiment: {uuid.UUID(bytes=bandwidth_header.experiment_id)}')
    for suffix, header in headers:
        print(f'{suffix}: records={header.produced:,} dropped={header.dropped:,}')
    if bandwidth_first is not None:
        span = (bandwidth_last - bandwidth_first) / frequency / MICROSECONDS_PER_SECOND
        print(f'Bandwidth timestamp span: {span:.6f} s')
    print('Bandwidth sample intervals: ' + bandwidth_intervals.describe(frequency))
    print('  p50/p99 are approximate, from a bounded systematic interval sample.')
    if bandwidth_intervals.total:
        mbps = bandwidth_cas / bandwidth_intervals.total * bandwidth_header.bandwidth_multiplier
        print(f'Estimated bandwidth, interval-weighted mean: {mbps:.3f} MB/s')
    print(f'Bandwidth nonpositive intervals skipped: {bandwidth_nonpositive}')
    print(f'Bandwidth intervals >= {LONG_GAP_MICROSECONDS} us: {long_gap_count}')
    print('Largest bandwidth intervals (times relative to capture start):')
    for ticks, start_tsc, end_tsc, record_index in sorted(largest_gaps, reverse=True):
        start_s = (start_tsc - bandwidth_first) / frequency / MICROSECONDS_PER_SECOND
        end_s = (end_tsc - bandwidth_first) / frequency / MICROSECONDS_PER_SECOND
        print(f'  {start_s:.6f}–{end_s:.6f} s: {ticks / frequency:.3f} us '
              f'(ending record index {record_index})')
    if llc_first is not None and bandwidth_first is not None:
        start = (llc_first - bandwidth_first) / frequency / MICROSECONDS_PER_SECOND
        end = (llc_last - bandwidth_first) / frequency / MICROSECONDS_PER_SECOND
        print(f'LLC record window relative to bandwidth start: {start:.6f}–{end:.6f} s')
        print('  This is sampling coverage, not proof of the client workload window.')
    print('Raw LLC by core (includes all application assignments and idle time):')
    print('  core records intervals misses covered_s misses/s')
    for core, stats in sorted(raw_core_stats.items()):
        seconds = stats['ticks'] / frequency / MICROSECONDS_PER_SECOND
        rate = stats['misses'] / seconds if seconds else 0
        print(f"  {core} {stats['records']} {stats['intervals']} "
              f"{stats['misses']} {seconds:.6f} {rate:.3f}")
    if headers[0][1].version >= 6:
        print('LLC by application (includes idle time with unchanged application ownership):')
        print('  core pid app_id records intervals misses covered_s misses/s')
    else:
        print('LLC by core/PID (legacy scheduling-generation check):')
        print('  core pid records intervals misses covered_s misses/s')
    for (core, pid, app_id), stats in sorted(core_stats.items()):
        seconds = stats['ticks'] / frequency / MICROSECONDS_PER_SECOND
        rate = stats['misses'] / seconds if seconds else 0
        app_column = f'{app_id} ' if app_id is not None else ''
        print(f"  {core} {pid} {app_column}{stats['records']} {stats['intervals']} "
              f"{stats['misses']} {seconds:.6f} {rate:.3f}")
    for reason, count in sorted(skipped_llc.items()):
        print(f'LLC intervals skipped: {reason}: {count}')
    for core, count in sorted(same_pid_generation_changes.items()):
        print(f'Generation changes with same PID: core={core} count={count}')
    print('  Generation changes can include idle/wake transitions, not just process switches.')
    print('PMC gather failures by core/reason (reason 1 = pending):')
    for (core, reason), count in sorted(failure_counts.items()):
        print(f'  core={core} reason={reason} count={count}')
    if headers[0][1].version >= 5:
        reason_names = {1: 'worker scheduled', 2: 'idle placeholder', 3: 'core explicitly idled'}
        print('Scheduler generation changes by core/PID/reason:')
        for (core, pid, reason), count in sorted(scheduler_events.items()):
            name = reason_names.get(reason, f'unknown reason {reason}')
            print(f'  core={core} pid={pid} reason={name} count={count}')
    else:
        print('Scheduler events were not recorded by this version-4 capture.')
    print('Writer-core activity is not sampled by the current implementation.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefix', help='file prefix, without .bw.bin/.llc.bin suffix')
    args = parser.parse_args()
    summarize(args.prefix)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(str(error))
