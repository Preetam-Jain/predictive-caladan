#!/usr/bin/env python3
"""Join loadgen request traces with the server's packet log.

Inputs (all from one run):
  --client    loadgen --trace-prefix files (PREFIX.cNNN.sNNN.bin)
  --packets   pktlogctl dump files (PREFIX.pkt.kNNN.bin)
  --nicclock  IOKernel capture.nicclock.bin (NIC clock calibration)

Each request is identified by its connection (client port) and its byte range
in that connection's stream; the server log identifies segments by TCP
sequence numbers, which become byte offsets via the connection's irs/iss.

Outputs:
  OUT.requests.npy  one row per client request (see REQUEST_DTYPE)
  OUT.summary.json  match rates, clock fit and consistency checks; "ok" is
                    false if any check fails
  OUT.windows.npy   optional per-window labels (--window-us)

Times: server_rx_tsc / server_tx_tsc are on the server TSC, the same clock as
the IOKernel telemetry. server_rx is when the NIC received the segment holding
the request's last byte; server_tx is when the NIC completed sending the
segment holding the response's last byte.
"""
import argparse
import glob
import json
import os
import shutil
import struct
import sys
import tempfile

import numpy as np

PACKET_DTYPE = np.dtype([
    ('ts', '<u8'), ('seq_start', '<u4'), ('seq_end', '<u4'), ('remote_ip', '<u4'),
    ('remote_port', '<u2'), ('local_port', '<u2'), ('type', 'u1'), ('kthread', 'u1'),
    ('flags', '<u2'), ('tsc_low', '<u4'),
])
assert PACKET_DTYPE.itemsize == 32
PACKET_HEADER = struct.Struct('<8sIIIIQQQdIIII16sQ')
assert PACKET_HEADER.size == 96
RX, TX, CONN = 1, 2, 3

CLIENT_HEADER = struct.Struct('<8sIIIIHHIQQIIQ')
assert CLIENT_HEADER.size == 64
CLIENT_DTYPE = np.dtype([
    ('target_start_ns', '<u8'), ('actual_start_ns', '<i8'), ('latency_ns', '<i8'),
    ('req_end', '<u8'), ('resp_end', '<u8'), ('req_bytes', '<u4'), ('status', 'u1'),
    ('reserved', 'u1'), ('schedule', '<u2'),
])
assert CLIENT_DTYPE.itemsize == 48
STATUS_COMPLETED = 0

TELEMETRY_HEADER = struct.Struct('<8sIIIIdQIIIIdQQ16s')

REQUEST_DTYPE = np.dtype([
    ('port', '<u2'), ('status', 'u1'), ('flags', 'u1'), ('index', '<u4'),
    ('target_start_ns', '<u8'), ('actual_start_ns', '<i8'), ('client_latency_ns', '<i8'),
    ('server_rx_tsc', '<i8'), ('server_tx_tsc', '<i8'), ('server_latency_ns', '<i8'),
])
FLAG_NO_RX = 1          # no received segment covers the request's last byte
FLAG_NO_TX = 2          # no transmitted segment covers the response's last byte
FLAG_RX_RETRANSMIT = 4  # the covering RX segment was received more than once
FLAG_TX_RETRANSMIT = 8  # the covering TX segment was sent more than once
FLAG_NO_CONN = 16       # no server connection record for this port

TWO32 = 1 << 32
BLOCK_RECORDS = 1 << 24


class JoinError(Exception):
    pass


# ---------- inputs ----------

def read_packet_file(path):
    with open(path, 'rb') as f:
        raw = f.read(PACKET_HEADER.size)
    if len(raw) != PACKET_HEADER.size:
        raise JoinError(f'{path}: truncated header')
    (magic, version, header_bytes, record_bytes, kthread, records, generation, dropped,
     cycles_per_us, flags, chunks, writer_pid, _, experiment_id, _) = PACKET_HEADER.unpack(raw)
    if magic != b'CALPKTD\0' or version != 1 or header_bytes != 96 or record_bytes != 32:
        raise JoinError(f'{path}: not a version-1 packet log dump')
    if not flags & 1:
        raise JoinError(f'{path}: dump incomplete')
    if os.path.getsize(path) != header_bytes + records * record_bytes:
        raise JoinError(f'{path}: size does not match record count')
    return dict(path=path, kthread=kthread, records=records, generation=generation,
                dropped=dropped, cycles_per_us=cycles_per_us, exhausted=bool(flags & 2),
                writer_pid=writer_pid, experiment_id=experiment_id.hex())


def read_client_file(path):
    with open(path, 'rb') as f:
        raw = f.read(CLIENT_HEADER.size)
    if len(raw) != CLIENT_HEADER.size:
        raise JoinError(f'{path}: truncated header')
    (magic, version, header_bytes, record_bytes, index, port, protocol, _, start_unix_ns,
     records, flags, _, _) = CLIENT_HEADER.unpack(raw)
    if magic != b'CALCTRC\0' or version != 1 or header_bytes != 64 or record_bytes != 48:
        raise JoinError(f'{path}: not a version-1 client trace')
    if not flags & 1:
        raise JoinError(f'{path}: trace incomplete')
    if os.path.getsize(path) != header_bytes + records * record_bytes:
        raise JoinError(f'{path}: size does not match record count')
    data = np.fromfile(path, dtype=CLIENT_DTYPE, offset=header_bytes, count=records)
    return dict(path=path, index=index, port=port, protocol=protocol,
                start_unix_ns=start_unix_ns, records=data)


def read_nicclock(path):
    with open(path, 'rb') as f:
        raw = f.read(TELEMETRY_HEADER.size)
        fields = TELEMETRY_HEADER.unpack(raw)
    magic, version, header_bytes, record_bytes, stream_type, cycles_per_us, selector = fields[:7]
    flags, produced = fields[10], fields[12]
    if magic != b'CALATLM\0' or version < 10 or stream_type != 9 or record_bytes != 24:
        raise JoinError(f'{path}: not a NIC clock stream')
    if flags != 1:
        raise JoinError(f'{path}: NIC clock stream incomplete')
    samples = np.fromfile(path, dtype=np.dtype([('tsc_before', '<u8'), ('nic', '<u8'),
                                                ('tsc_after', '<u8')]),
                          offset=header_bytes, count=produced)
    return cycles_per_us, selector, samples, fields[14].hex()


def fit_nic_clock(cycles_per_us, nic_khz, samples, window_s=1.0, max_residual_ns=200.0,
                  max_gap_s=0.5):
    """Map NIC ticks -> TSC.

    The NIC oscillator's rate relative to the TSC drifts slowly (sub-ppm), so
    one line over a whole run is off by up to ~10 us. Instead: fit a line to
    each window_s window (using the half of its samples with the narrowest TSC
    brackets), evaluate it at the window's center, and convert by linear
    interpolation between those centers. Outside the first/last center the
    edge segment is extended.
    """
    if len(samples) < 10:
        raise JoinError('fewer than 10 NIC clock samples')
    if not nic_khz:
        raise JoinError('NIC clock frequency missing from the stream header')
    nic = samples['nic'].astype(np.int64)
    before = samples['tsc_before'].astype(np.int64)
    width = samples['tsc_after'].astype(np.int64) - before
    if (width < 0).any() or (np.diff(nic) <= 0).any():
        raise JoinError('NIC clock samples are not monotonic')
    ticks_per_s = nic_khz * 1000.0
    nic0, tsc0 = int(nic[0]), int(before[0])
    x = (nic - nic0).astype(np.float64)
    y = (before - tsc0) + width / 2.0
    max_gap = float(np.diff(x).max() / ticks_per_s)
    if max_gap > max_gap_s:
        raise JoinError(f'{max_gap:.3f} s gap between NIC clock samples (writer stalled?)')

    window = (x // (window_s * ticks_per_s)).astype(np.int64)
    edges = np.concatenate(([0], np.flatnonzero(np.diff(window)) + 1, [len(x)]))
    centers, values = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        w = width[lo:hi]
        m = w <= np.percentile(w, 50)
        if m.sum() < 20:
            continue
        xs, ys = x[lo:hi][m], y[lo:hi][m]
        line = np.polyfit(xs, ys, 1)
        center = xs.mean()
        centers.append(center)
        values.append(np.polyval(line, center))
    if len(centers) < 2:
        raise JoinError('fewer than two usable NIC clock windows')
    centers, values = np.array(centers), np.array(values)
    first_slope = (values[1] - values[0]) / (centers[1] - centers[0])
    last_slope = (values[-1] - values[-2]) / (centers[-1] - centers[-2])

    def map_x(xx):
        v = np.interp(xx, centers, values)
        lo, hi = xx < centers[0], xx > centers[-1]
        v[lo] = values[0] + first_slope * (xx[lo] - centers[0])
        v[hi] = values[-1] + last_slope * (xx[hi] - centers[-1])
        return v

    keep = width <= np.percentile(width, 50)
    residual_ns = np.abs(y[keep] - map_x(x[keep])) / cycles_per_us * 1000
    slope = (values[-1] - values[0]) / (centers[-1] - centers[0])
    expected = cycles_per_us * 1000.0 / nic_khz
    fit = dict(method='piecewise', window_s=window_s, windows=int(len(centers)),
               samples=int(len(samples)), samples_used=int(keep.sum()),
               cycles_per_nic_tick=float(slope), expected_cycles_per_nic_tick=expected,
               bracket_width_ns_p50=float(np.median(width) / cycles_per_us * 1000),
               max_sample_gap_s=max_gap,
               residual_ns_p50=float(np.median(residual_ns)),
               residual_ns_p99=float(np.percentile(residual_ns, 99)),
               residual_ns_max=float(residual_ns.max()),
               nic_first=int(nic[0]), nic_last=int(nic[-1]))
    if abs(slope / expected - 1) > 0.01:
        raise JoinError(f'NIC clock rate {slope:.6f} cycles/tick differs from the reported '
                        f'{expected:.6f}; is the NIC in real-time clock mode?')
    if fit['residual_ns_p99'] > max_residual_ns:
        raise JoinError(f'NIC clock fit residual p99 {fit["residual_ns_p99"]:.0f} ns exceeds '
                        f'{max_residual_ns:.0f} ns (clock step or irregular samples?)')

    def convert(ticks):
        xx = (ticks.astype(np.int64) - nic0).astype(np.float64)
        return (tsc0 + map_x(xx)).round().astype(np.int64)
    return fit, convert, (int(nic[0]), int(nic[-1]))


# ---------- packet partitioning ----------

def partition_packets(files, server_port, tmp):
    """One pass over all dump files: split records by client port into temp files."""
    handles, counts = {}, {}
    other_ports = 0
    for meta in files:
        mm = np.memmap(meta['path'], dtype=PACKET_DTYPE, mode='r', offset=96,
                       shape=(meta['records'],))
        for start in range(0, meta['records'], BLOCK_RECORDS):
            block = np.array(mm[start:start + BLOCK_RECORDS])
            mine = block['local_port'] == server_port
            other_ports += int((~mine).sum())
            block = block[mine]
            order = np.argsort(block['remote_port'], kind='stable')
            block = block[order]
            ports, first = np.unique(block['remote_port'], return_index=True)
            bounds = list(first) + [len(block)]
            for i, port in enumerate(ports):
                part = block[bounds[i]:bounds[i + 1]]
                if port not in handles:
                    handles[port] = open(os.path.join(tmp, f'{int(port)}.bin'), 'wb')
                    counts[port] = 0
                part.tofile(handles[port])
                counts[port] += len(part)
        del mm
    for f in handles.values():
        f.close()
    return {int(p): (os.path.join(tmp, f'{int(p)}.bin'), int(n)) for p, n in counts.items()}, other_ports


def unwrap(offsets32):
    """Extend 32-bit offsets (in time order) to 64 bits."""
    o = offsets32.astype(np.int64)
    d = np.diff(o)
    wraps = np.concatenate(([0], np.cumsum((d < -(TWO32 // 2)).astype(np.int64)
                                           - (d > TWO32 // 2).astype(np.int64))))
    return o + wraps * TWO32


def segments(records, base_seq, to_tsc):
    """Byte ranges [start, end) relative to the first data byte, sorted by start."""
    if len(records) == 0:
        return dict(start=np.zeros(0, np.int64), end=np.zeros(0, np.int64),
                    tsc=np.zeros(0, np.int64), dup=np.zeros(0, bool))
    tsc = to_tsc(records['ts'])
    order = np.argsort(tsc, kind='stable')
    rec, tsc = records[order], tsc[order]
    rel = (rec['seq_start'].astype(np.int64) - base_seq - 1) % TWO32
    length = (rec['seq_end'].astype(np.int64) - rec['seq_start'].astype(np.int64)) % TWO32
    start = unwrap(rel)
    end = start + length
    # keep the earliest copy of each start offset; mark retransmitted starts
    order = np.lexsort((tsc, start))
    start, end, tsc = start[order], end[order], tsc[order]
    first = np.concatenate(([True], start[1:] != start[:-1]))
    counts = np.diff(np.concatenate((np.nonzero(first)[0], [len(start)])))
    return dict(start=start[first], end=end[first], tsc=tsc[first], dup=counts > 1)


def cover(seg, byte_index):
    """For each byte index, the segment containing it (or -1)."""
    idx = np.searchsorted(seg['start'], byte_index, side='right') - 1
    ok = (idx >= 0)
    idx_safe = np.where(ok, idx, 0)
    ok &= byte_index < seg['end'][idx_safe] if len(seg['end']) else False
    return np.where(ok, idx_safe, -1)


# ---------- main join ----------

def join(args):
    client_files = sorted(set(sum((glob.glob(p) for p in args.client), [])))
    packet_files = sorted(set(sum((glob.glob(p) for p in args.packets), [])))
    if not client_files:
        raise JoinError('no client trace files')
    if not packet_files:
        raise JoinError('no packet log files')
    packets = [read_packet_file(p) for p in packet_files]
    generations = {p['generation'] for p in packets}
    if len(generations) != 1:
        raise JoinError(f'packet files come from different segment generations: {generations}')
    cycles_per_us, nic_khz, clock_samples, capture_id = read_nicclock(args.nicclock)
    ids = {p['experiment_id'] for p in packets}
    if ids != {capture_id}:
        raise JoinError(f'packet files experiment id {ids} does not match the capture {capture_id}')
    fit, to_tsc, nic_range = fit_nic_clock(cycles_per_us, nic_khz, clock_samples,
                                           args.clock_window_s, args.max_clock_residual_ns)

    clients = [read_client_file(p) for p in client_files]
    by_port = {}
    for c in clients:
        if c['port'] in by_port:
            raise JoinError(f'two client traces for port {c["port"]}')
        by_port[c['port']] = c

    summary = dict(ok=True, failures=[], clock_fit=fit, cycles_per_us=cycles_per_us,
                   nic_khz=nic_khz, packet_files=len(packets),
                   packet_records=int(sum(p['records'] for p in packets)),
                   packet_dropped=int(packets[0]['dropped']),
                   packet_log_exhausted=any(p['exhausted'] for p in packets),
                   client_files=len(clients), connections=[])
    if summary['packet_dropped'] or summary['packet_log_exhausted']:
        summary['failures'].append('packet log dropped records (segment full)')

    tmp = tempfile.mkdtemp(prefix='join-', dir=args.tmp)
    try:
        parts, other = partition_packets(packets, args.server_port, tmp)
        clock_check = dict(outside_span=0, rx_count=0, tx_count=0, rx_before_nic=0, tx_before_nic=0,
                           rx_samples=[], tx_samples=[])
        summary['records_other_local_ports'] = other
        total = sum(len(c['records']) for c in clients)
        out = np.lib.format.open_memmap(args.out + '.requests.npy', mode='w+',
                                        dtype=REQUEST_DTYPE, shape=(total,))
        row = 0
        for port in sorted(by_port):
            c = by_port[port]
            r = c['records']
            n = len(r)
            res = out[row:row + n]
            res['port'] = port
            res['index'] = np.arange(n, dtype=np.uint32)
            res['status'] = r['status']
            res['target_start_ns'] = r['target_start_ns']
            res['actual_start_ns'] = r['actual_start_ns']
            res['client_latency_ns'] = r['latency_ns']
            res['server_rx_tsc'] = -1
            res['server_tx_tsc'] = -1
            res['server_latency_ns'] = -1
            res['flags'] = 0
            row += n
            info = dict(port=port, client_file=os.path.basename(c['path']), requests=n)
            summary['connections'].append(info)
            if port not in parts:
                res['flags'] = FLAG_NO_CONN
                info['error'] = 'no server records for this port'
                continue
            recs = np.fromfile(parts[port][0], dtype=PACKET_DTYPE)
            conns = recs[recs['type'] == CONN]
            if len(conns) == 0:
                res['flags'] = FLAG_NO_CONN
                info['error'] = 'no CONN record for this port'
                continue
            # The run's connection is the last one opened on this port (earlier
            # ones are readiness probes). Its data is everything after its CONN.
            conn = conns[np.argmax(conns['ts'])]
            conn_tsc = int(conn['ts'])
            info.update(connections_on_port=int(len(conns)), irs=int(conn['seq_start']),
                        iss=int(conn['seq_end']))
            data = recs[recs['type'] != CONN]
            data_tsc = to_tsc(data['ts']) if len(data) else np.zeros(0, np.int64)
            mine = data_tsc >= conn_tsc - int(args.conn_slack_us * cycles_per_us)
            data, data_tsc = data[mine], data_tsc[mine]
            margin = int(args.clock_window_s * nic_khz * 1000)
            outside = int(((data['ts'].astype(np.int64) < nic_range[0] - margin) |
                           (data['ts'].astype(np.int64) > nic_range[1] + margin)).sum())
            clock_check['outside_span'] += outside
            # Independent check: the runtime wrote each record (tsc_low, low 32
            # bits of rdtsc) after the NIC completion, so converted NIC time
            # must not be later than tsc_low beyond the conversion error.
            delay = (data['tsc_low'].astype(np.int64) - (data_tsc & 0xffffffff)) % TWO32
            delay = np.where(delay >= TWO32 // 2, delay - TWO32, delay)
            delay_ns = delay * 1000.0 / cycles_per_us
            for kind, name in ((RX, 'rx'), (TX, 'tx')):
                d = delay_ns[data['type'] == kind]
                clock_check[name + '_count'] += len(d)
                clock_check[name + '_before_nic'] += int((d < -args.max_clock_residual_ns).sum())
                if len(d):
                    clock_check[name + '_samples'].append(d[np.linspace(0, len(d) - 1, min(len(d), 20000)).astype(np.int64)])
            rx = segments(data[data['type'] == RX], int(conn['seq_start']), to_tsc)
            tx = segments(data[data['type'] == TX], int(conn['seq_end']), to_tsc)
            info.update(rx_segments=int(len(rx['start'])), tx_segments=int(len(tx['start'])),
                        rx_bytes=int(rx['end'].max()) if len(rx['end']) else 0,
                        tx_bytes=int(tx['end'].max()) if len(tx['end']) else 0,
                        client_bytes_sent=int(r['req_end'].max()) if n else 0,
                        client_bytes_received=int(r['resp_end'].max()) if n else 0)

            sent = r['req_end'] > 0
            ri = cover(rx, r['req_end'].astype(np.int64) - 1)
            has_rx = sent & (ri >= 0)
            res['server_rx_tsc'] = np.where(has_rx, rx['tsc'][np.maximum(ri, 0)] if len(rx['tsc']) else -1, -1)
            flags = np.where(sent & ~has_rx, FLAG_NO_RX, 0)
            if len(rx['dup']):
                flags |= np.where(has_rx & rx['dup'][np.maximum(ri, 0)], FLAG_RX_RETRANSMIT, 0)

            answered = r['resp_end'] > 0
            ti = cover(tx, r['resp_end'].astype(np.int64) - 1)
            has_tx = answered & (ti >= 0)
            res['server_tx_tsc'] = np.where(has_tx, tx['tsc'][np.maximum(ti, 0)] if len(tx['tsc']) else -1, -1)
            flags |= np.where(answered & ~has_tx, FLAG_NO_TX, 0)
            if len(tx['dup']):
                flags |= np.where(has_tx & tx['dup'][np.maximum(ti, 0)], FLAG_TX_RETRANSMIT, 0)
            res['flags'] = flags
            both = has_rx & has_tx
            res['server_latency_ns'] = np.where(
                both, ((res['server_tx_tsc'] - res['server_rx_tsc']) * 1000.0 / cycles_per_us).round(), -1)
            info.update(sent=int(sent.sum()), rx_matched=int(has_rx.sum()),
                        answered=int(answered.sum()), tx_matched=int(has_tx.sum()))
        out.flush()
        processing = dict(records_outside_clock_span=clock_check['outside_span'])
        for name in ('rx', 'tx'):
            d = np.concatenate(clock_check[name + '_samples']) if clock_check[name + '_samples'] else np.zeros(0)
            processing[name + '_records'] = clock_check[name + '_count']
            processing[name + '_tsc_before_nic_time'] = clock_check[name + '_before_nic']
            if len(d):
                processing[name + '_nic_to_runtime_ns_p50'] = float(np.median(d))
                processing[name + '_nic_to_runtime_ns_p99'] = float(np.percentile(d, 99))
            count = clock_check[name + '_count']
            if count and clock_check[name + '_before_nic'] > 0.001 * count:
                summary['failures'].append(
                    f'{clock_check[name + "_before_nic"]} {name.upper()} records were written by the '
                    'runtime before their converted NIC time (clock conversion error)')
        if clock_check['outside_span']:
            summary['failures'].append(f'{clock_check["outside_span"]} packet records fall outside '
                                       'the NIC clock calibration span')
        summary['nic_to_runtime'] = processing
        checks(out, summary, args, cycles_per_us)
        if args.window_us:
            windows(out, args, cycles_per_us, summary)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    summary['ok'] = not summary['failures']
    with open(args.out + '.summary.json', 'w') as f:
        json.dump(summary, f, indent=2)
        f.write('\n')
    return summary


def checks(out, summary, args, cycles_per_us):
    sent = out['actual_start_ns'] >= 0
    sent &= out['status'] != 3
    completed = out['status'] == STATUS_COMPLETED
    rx_ok = (out['server_rx_tsc'] >= 0)
    tx_ok = (out['server_tx_tsc'] >= 0)
    n_sent, n_done = int(sent.sum()), int(completed.sum())
    rx_rate = float(rx_ok[sent].mean()) if n_sent else 0.0
    tx_rate = float(tx_ok[completed].mean()) if n_done else 0.0
    both = completed & rx_ok & tx_ok
    client = out['client_latency_ns'][both].astype(np.float64)
    server = out['server_latency_ns'][both].astype(np.float64)
    tolerance_ns = args.latency_tolerance_us * 1000
    violations = int((server > client + tolerance_ns).sum())
    negative = int((server < 0).sum())
    summary['requests'] = dict(
        total=int(len(out)), sent=n_sent, completed=n_done,
        rx_match_rate=rx_rate, tx_match_rate_of_completed=tx_rate,
        server_longer_than_client=violations, server_negative=negative,
        retransmit_flagged=int(((out['flags'] & (FLAG_RX_RETRANSMIT | FLAG_TX_RETRANSMIT)) != 0).sum()),
        client_latency_us_p50=float(np.median(client) / 1000) if len(client) else None,
        client_latency_us_p99=float(np.percentile(client, 99) / 1000) if len(client) else None,
        server_latency_us_p50=float(np.median(server) / 1000) if len(server) else None,
        server_latency_us_p99=float(np.percentile(server, 99) / 1000) if len(server) else None,
        client_minus_server_us_p50=float(np.median(client - server) / 1000) if len(client) else None,
    )
    if rx_rate < args.min_match_rate:
        summary['failures'].append(f'RX match rate {rx_rate:.6f} below {args.min_match_rate}')
    if tx_rate < args.min_match_rate:
        summary['failures'].append(f'TX match rate {tx_rate:.6f} below {args.min_match_rate}')
    if len(client) and violations > args.max_violation_fraction * len(client):
        summary['failures'].append(f'{violations} requests have server latency above client latency')
    if negative:
        summary['failures'].append(f'{negative} requests have negative server latency')
    # Requests on one connection are sent in order, so server receive times
    # should follow send order (a few equal times from coalesced segments).
    inversions = 0
    for port in np.unique(out['port']):
        rows = out[(out['port'] == port) & rx_ok]
        rows = rows[np.argsort(rows['actual_start_ns'], kind='stable')]
        inversions += int((np.diff(rows['server_rx_tsc']) < 0).sum())
    summary['requests']['rx_order_inversions'] = inversions
    # A retransmitted segment arrives after later bytes, so only inversions
    # beyond the retransmissions indicate a join error.
    if inversions > summary['requests']['retransmit_flagged']:
        summary['failures'].append(f'{inversions} server receive-time inversions within a connection')


def windows(out, args, cycles_per_us, summary):
    sel = (out['server_rx_tsc'] >= 0) & (out['status'] == STATUS_COMPLETED)
    rx = out['server_rx_tsc'][sel]
    lat = out['client_latency_ns'][sel]
    width = int(args.window_us * cycles_per_us)
    t0 = int(rx.min())
    w = (rx - t0) // width
    order = np.lexsort((lat, w))
    w, lat = w[order], lat[order]
    ids, first, counts = np.unique(w, return_index=True, return_counts=True)

    def pct(q):
        return lat[first + np.floor(q * (counts - 1)).astype(np.int64)]
    sums = np.add.reduceat(lat.astype(np.float64), first)
    over = np.add.reduceat((lat > args.slo_us * 1000).astype(np.int64), first)
    win = np.zeros(len(ids), dtype=[('start_tsc', '<i8'), ('count', '<u4'),
                                    ('mean_ns', '<f8'), ('p50_ns', '<i8'), ('p99_ns', '<i8'),
                                    ('p999_ns', '<i8'), ('max_ns', '<i8'),
                                    ('frac_over_slo', '<f8')])
    win['start_tsc'] = t0 + ids * width
    win['count'] = counts
    win['mean_ns'] = sums / counts
    win['p50_ns'], win['p99_ns'], win['p999_ns'] = pct(0.5), pct(0.99), pct(0.999)
    win['max_ns'] = lat[first + counts - 1]
    win['frac_over_slo'] = over / counts
    np.save(args.out + '.windows.npy', win)
    summary['windows'] = dict(window_us=args.window_us, slo_us=args.slo_us, count=int(len(win)),
                              requests_per_window_p50=float(np.median(counts)))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--client', nargs='+', required=True, help='client trace files or globs')
    p.add_argument('--packets', nargs='+', required=True, help='packet dump files or globs')
    p.add_argument('--nicclock', required=True, help='capture.nicclock.bin')
    p.add_argument('--server-port', type=int, default=11211)
    p.add_argument('--out', required=True, help='output prefix')
    p.add_argument('--tmp', default=None, help='directory for temporary partitions')
    p.add_argument('--conn-slack-us', type=float, default=10000)
    p.add_argument('--clock-window-s', type=float, default=1.0)
    p.add_argument('--max-clock-residual-ns', type=float, default=200.0)
    p.add_argument('--min-match-rate', type=float, default=0.999)
    p.add_argument('--latency-tolerance-us', type=float, default=2.0)
    p.add_argument('--max-violation-fraction', type=float, default=0.001)
    p.add_argument('--window-us', type=float, default=0)
    p.add_argument('--slo-us', type=float, default=50)
    args = p.parse_args()
    try:
        summary = join(args)
    except JoinError as e:
        print(json.dumps(dict(ok=False, failures=[str(e)])))
        sys.exit(2)
    print(json.dumps(dict(ok=summary['ok'], failures=summary['failures'],
                          requests=summary.get('requests'))))
    sys.exit(0 if summary['ok'] else 1)


if __name__ == '__main__':
    main()
