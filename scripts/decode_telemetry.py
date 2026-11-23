#!/usr/bin/env python3
"""Stream a version-4 through version-9 Caladan telemetry file to CSV."""
from collections import namedtuple
import argparse
import csv
import os
import struct
import sys
import uuid

HEADER = struct.Struct('<8sIIIIdQIIIIdQQ16s')
CAPTURE_COMPLETE = 1
# The file's stream type selects its versioned field layout.
LAYOUTS = {
    1: (struct.Struct('<QQ'), ('tsc', 'cas')),
    2: (struct.Struct('<QQQII'), ('tsc', 'generation', 'misses', 'core', 'pid')),
    3: (struct.Struct('<QQII'), ('tsc', 'generation', 'core', 'reason')),
    5: (struct.Struct('<6Q4I'),
        ('tsc', 'runnable_cycles', 'rx_cycles', 'timer_cycles', 'directpath_cycles',
         'storage_cycles', 'pid', 'tid', 'core', 'flags')),
    6: (struct.Struct('<3Q4I'),
        ('tsc', 'task_elapsed_us', 'kthread_elapsed_us', 'pid', 'tid', 'core', 'rcu_gen')),
    7: (struct.Struct('<2Q3d4I'),
        ('tsc', 'rx_cycles', 'max_us', 'min_us', 'avg_us', 'pid', 'active_threads',
         'flags', 'reserved')),
    4: (struct.Struct('<QQIHH'), ('tsc', 'generation', 'pid', 'core', 'reason')),
    8: (struct.Struct('<Q4I'), ('tsc', 'pid', 'tid', 'core', 'reason')),
}


LLC_OWNERSHIP_LAYOUT = (
    struct.Struct('<QQQQQQII'),
    ('tsc', 'sched_gen_at_request', 'misses', 'app_id_at_request',
     'app_owner_gen_at_request', 'app_owner_gen_at_collection', 'core', 'pid_at_request'),
)


LLC_SCHED_COLLECTION_LAYOUT = (
    struct.Struct('<QQQQQQQII'),
    LLC_OWNERSHIP_LAYOUT[1][:-2] + ('sched_gen_at_collection', 'core', 'pid_at_request'),
)


def record_layout(header):
    if header.version >= 7 and header.stream_type == 2:
        return LLC_SCHED_COLLECTION_LAYOUT
    if header.version >= 6 and header.stream_type == 2:
        return LLC_OWNERSHIP_LAYOUT
    return LAYOUTS[header.stream_type]


FileHeader = namedtuple('FileHeader', (
    'magic version header_bytes record_bytes stream_type cycles_per_us selector '
    'writer_core scheduler_core nr_cores flags bandwidth_multiplier produced dropped experiment_id'
))


def read_header(source):
    raw = source.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ValueError('truncated header')
    header = FileHeader._make(HEADER.unpack(raw))
    if header.magic != b'CALATLM\0' or header.version not in (4, 5, 6, 7, 8, 9):
        raise ValueError('unsupported capture format (requires version 4 through 9)')
    if (header.stream_type not in LAYOUTS
            or (header.version < 8 and header.stream_type > 4)
            or (header.version == 4 and header.stream_type == 4)
            or (header.version < 9 and header.stream_type == 8)):
        raise ValueError(f'unknown stream type {header.stream_type}')
    layout, _ = record_layout(header)
    if (header.header_bytes != HEADER.size + header.nr_cores * 4
            or header.record_bytes != layout.size):
        raise ValueError('invalid sizes for versioned stream layout')
    if header.header_bytes > 1024 * 1024:
        raise ValueError('header exceeds decoder safety limit')
    if header.flags != CAPTURE_COMPLETE:
        raise ValueError('capture incomplete: writer did not finalize this stream')
    if header.cycles_per_us <= 0:
        raise ValueError('invalid timestamp frequency')
    cpu_bytes = source.read(header.header_bytes - HEADER.size)
    if len(cpu_bytes) != header.header_bytes - HEADER.size:
        raise ValueError('truncated CPU list')
    cores = tuple(core[0] for core in struct.iter_unpack('<I', cpu_bytes))
    return header, cores


def iter_records(source, header):
    layout, _ = record_layout(header)
    records_remaining = header.produced
    records_per_chunk = max(1, 65536 // header.record_bytes)
    while records_remaining:
        chunk_records = min(records_remaining, records_per_chunk)
        chunk = source.read(chunk_records * header.record_bytes)
        if len(chunk) != chunk_records * header.record_bytes:
            raise ValueError('truncated record data')
        yield from layout.iter_unpack(chunk)
        records_remaining -= chunk_records
    if source.read(1):
        raise ValueError('extra data after declared records')


def decode(source, output):
    header, cores = read_header(source)
    print(f'experiment_id={uuid.UUID(bytes=header.experiment_id)} '
          f'stream={header.stream_type} cycles_per_us={header.cycles_per_us} '
          f'selector={header.selector:#x} writer_cpu={header.writer_core} '
          f'scheduler_cpu={header.scheduler_core} cores={cores} '
          f'bandwidth_multiplier={header.bandwidth_multiplier}', file=sys.stderr)
    csv_output = csv.writer(output)
    csv_output.writerow(record_layout(header)[1])
    for record in iter_records(source, header):
        values = list(record)
        if header.stream_type == 5 and values[-1] & 8:
            values[1] = None  # unavailable runnable delay, not zero
        if header.stream_type == 7 and values[-2] & 8:
            values[2:5] = [None, None, None]
        csv_output.writerow(values)
    print(f'completed: records={header.produced} dropped={header.dropped}', file=sys.stderr)


STREAM_FILES = (('bw', 1), ('llc', 2), ('pmc-failure', 3), ('sched', 4), ('queue', 5), ('task', 6), ('delay', 7))


def capture_streams(prefix):
    with open(f'{prefix}.bw.bin', 'rb') as source:
        header, _ = read_header(source)
    if header.version >= 8:
        optional = prefix + '.queue-snapshot-failure.bin'
        return STREAM_FILES + ((('queue-snapshot-failure', 8),)
                               if header.version >= 9 and os.path.exists(optional) else ())
    return STREAM_FILES[:4] if header.version >= 5 else STREAM_FILES[:3]


def check_capture(prefix):
    expected_identity = None
    with open(os.devnull, 'w') as sink:
        for suffix, expected_type in capture_streams(prefix):
            path = f'{prefix}.{suffix}.bin'
            print(f'Validating: {path}')
            with open(path, 'rb') as source:
                header, cores = read_header(source)
                identity = (header.version, header.experiment_id, header.cycles_per_us,
                            header.writer_core, header.scheduler_core, cores)
                if header.stream_type != expected_type:
                    raise ValueError(f'{suffix}: wrong stream type')
                if expected_identity is not None and identity != expected_identity:
                    raise ValueError('stream experiment IDs or hardware metadata do not match')
                expected_identity = identity
                source.seek(0)
                decode(source, sink)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', nargs='?')
    parser.add_argument('--check-prefix', help='validate every stream of a capture')
    args = parser.parse_args()
    if args.check_prefix and not args.capture:
        check_capture(args.check_prefix)
    elif args.capture and not args.check_prefix:
        with open(args.capture, 'rb') as source:
            decode(source, sys.stdout)
    else:
        parser.error('supply either a stream file or --check-prefix PREFIX')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(str(error))
