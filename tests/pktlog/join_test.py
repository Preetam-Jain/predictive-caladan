#!/usr/bin/env python3
"""Synthetic end-to-end test of scripts/join_requests.py.

Builds a NIC clock stream, packet-log dumps and client traces for a known
scenario (probe connection on the same port, pipelined requests sharing a
segment, sequence-number wrap, a retransmitted segment, an unanswered request)
and checks every joined value.
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import join_requests as jr  # noqa: E402

CPU = 2400.0            # TSC cycles per us
NIC_KHZ = 156250        # NIC ticks per ms -> 6.4 ns per tick
TSC_PER_TICK = CPU * 1000 / NIC_KHZ
NIC0, TSC0 = 10_000_000_000, 5_000_000_000_000
EXPERIMENT = bytes(range(16))
PORT = 11211


def nic_at(tsc):
    return int(round(NIC0 + (tsc - TSC0) / TSC_PER_TICK))


def write_nicclock(path, duration_s=3):
    header = struct.pack('<8sIIIIdQIIIIdQQ16s', b'CALATLM\0', 10, 96 + 4, 24, 9, CPU,
                         NIC_KHZ, 2, 1, 1, 1, 0.0, 0, 0, EXPERIMENT) + struct.pack('<I', 4)
    rows = []
    rng = np.random.default_rng(1)
    for i in range(duration_s * 1000):
        mid = TSC0 + i * int(CPU * 1000)
        width = int(rng.integers(200, 2000))
        rows.append((mid - width // 2, nic_at(mid), mid + width - width // 2))
    body = np.array(rows, dtype='<u8').tobytes()
    header = header[:64] + struct.pack('<QQ', len(rows), 0) + header[80:]
    Path(path).write_bytes(header + body)


def rec(kind, tsc, s0, s1, rport, kthread, ts=None):
    return (ts if ts is not None else nic_at(tsc), s0 % 2**32, s1 % 2**32, 0x0a000001,
            rport, PORT, kind, kthread, 0, tsc & 0xffffffff)


def write_dump(path, kthread, records):
    arr = np.array(records, dtype=jr.PACKET_DTYPE)
    header = struct.pack('<8sIIIIQQQdIIII16sQ', b'CALPKTD\0', 1, 96, 32, kthread, len(arr),
                         7, 0, CPU, 1, 1, 1234, 0, EXPERIMENT, 0)
    Path(path).write_bytes(header + arr.tobytes())


def write_client(path, index, port, rows):
    arr = np.array(rows, dtype=jr.CLIENT_DTYPE)
    header = struct.pack('<8sIIIIHHIQQIIQ', b'CALCTRC\0', 1, 64, 48, index, port, 1, 0,
                         123, len(arr), 1, 0, 0)
    Path(path).write_bytes(header + arr.tobytes())


def main():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        write_nicclock(d / 'capture.nicclock.bin')
        us = int(CPU)
        k0, k1 = [], []
        # Port 100: a probe connection first, then the run's connection whose
        # sequence numbers wrap; 4 requests of 44 bytes, requests 1 and 2 share
        # one segment; request 3's segment is retransmitted.
        probe_irs, irs, iss = 777, 2**32 - 60, 2**32 - 10
        k0.append(rec(3, TSC0 + 10 * us, probe_irs, 999, 100, 0, ts=TSC0 + 10 * us))
        k0.append(rec(1, TSC0 + 20 * us, probe_irs + 1, probe_irs + 45, 100, 0))
        t = TSC0 + 1_000_000 * us // 1000 * 1000  # 1 s later
        k1.append(rec(3, t, irs, iss, 100, 1, ts=t))
        segs_rx = [(0, 44, t + 100 * us), (44, 132, t + 200 * us), (132, 176, t + 300 * us)]
        for s, e, at in segs_rx:
            k1.append(rec(1, at, irs + 1 + s, irs + 1 + e, 100, 1))
        k0.append(rec(1, t + 900 * us, irs + 1 + 132, irs + 1 + 176, 100, 0))  # retransmit
        # responses: 30 bytes each, server latency 5 us except request 2 (8 us)
        tx = [(0, 30, t + 105 * us), (30, 60, t + 205 * us), (60, 90, t + 208 * us),
              (90, 120, t + 305 * us)]
        for s, e, at in tx:
            k1.append(rec(2, at, iss + 1 + s, iss + 1 + e, 100, 1))
        client100 = [
            (0, 1000, 9_000, 44, 30, 44, 0, 0, 0),
            (0, 2000, 9_000, 88, 60, 44, 0, 0, 0),
            (0, 2001, 12_000, 132, 90, 44, 0, 0, 0),
            (0, 3000, 9_500, 176, 120, 44, 0, 0, 0),
        ]
        # Port 101: one answered request and one sent without response.
        irs2, iss2 = 5000, 9000
        t2 = t + 50 * us
        k0.append(rec(3, t2, irs2, iss2, 101, 0, ts=t2))
        k0.append(rec(1, t2 + 100 * us, irs2 + 1, irs2 + 45, 101, 0))
        k0.append(rec(1, t2 + 400 * us, irs2 + 45, irs2 + 89, 101, 0))
        k0.append(rec(2, t2 + 110 * us, iss2 + 1, iss2 + 31, 101, 0))
        client101 = [
            (0, 1000, 15_000, 44, 30, 44, 0, 0, 0),
            (0, 4000, -1, 88, 0, 44, 1, 0, 0),
        ]
        write_dump(d / 'run.pkt.k000.bin', 0, k0)
        write_dump(d / 'run.pkt.k001.bin', 1, k1)
        write_client(d / 'ctrace.c000.s000.bin', 0, 100, client100)
        write_client(d / 'ctrace.c001.s001.bin', 1, 101, client101)

        out = d / 'joined'
        proc = subprocess.run([sys.executable, str(REPO / 'scripts/join_requests.py'),
                               '--client', str(d / 'ctrace.*.bin'),
                               '--packets', str(d / 'run.pkt.k*.bin'),
                               '--nicclock', str(d / 'capture.nicclock.bin'),
                               '--out', str(out), '--window-us', '1000'],
                              capture_output=True, text=True)
        summary = json.loads((d / 'joined.summary.json').read_text())
        assert proc.returncode == 0, (proc.stdout, proc.stderr, summary)
        rows = np.load(str(out) + '.requests.npy')
        assert len(rows) == 6
        r100 = rows[rows['port'] == 100]
        tol = int(TSC_PER_TICK // 2) + 1  # NIC tick quantization, in TSC cycles
        expect_rx = [t + 100 * us, t + 200 * us, t + 200 * us, t + 300 * us]
        expect_tx = [t + 105 * us, t + 205 * us, t + 208 * us, t + 305 * us]
        assert np.all(np.abs(r100['server_rx_tsc'] - expect_rx) <= tol), r100['server_rx_tsc'] - expect_rx
        assert np.all(np.abs(r100['server_tx_tsc'] - expect_tx) <= tol), r100['server_tx_tsc'] - expect_tx
        assert list(np.round(r100['server_latency_ns'] / 1000)) == [5, 5, 8, 5]
        assert r100['flags'][3] & jr.FLAG_RX_RETRANSMIT
        assert not r100['flags'][:3].any()
        r101 = rows[rows['port'] == 101]
        assert abs(r101['server_rx_tsc'][0] - (t2 + 100 * us)) <= tol
        assert abs(r101["server_latency_ns"][0] - 10_000) <= 10
        assert r101['server_tx_tsc'][1] == -1 and r101['flags'][1] == 0  # unanswered, not an error
        assert abs(r101['server_rx_tsc'][1] - (t2 + 400 * us)) <= tol
        conn100 = next(c for c in summary['connections'] if c['port'] == 100)
        assert conn100['connections_on_port'] == 2 and conn100['irs'] == irs
        assert summary['clock_fit']['residual_ns_max'] < 1.0
        assert summary['requests']['rx_match_rate'] == 1.0
        assert summary['windows']['count'] >= 1

        # A mismatched experiment id must be rejected.
        bad = struct.pack('<8sIIIIQQQdIIII16sQ', b'CALPKTD\0', 1, 96, 32, 1, len(k1),
                          7, 0, CPU, 1, 1, 1234, 0, bytes(16), 0)
        (d / 'run.pkt.k001.bin').write_bytes(bad + np.array(k1, dtype=jr.PACKET_DTYPE).tobytes())
        proc = subprocess.run([sys.executable, str(REPO / 'scripts/join_requests.py'),
                               '--client', str(d / 'ctrace.*.bin'),
                               '--packets', str(d / 'run.pkt.k*.bin'),
                               '--nicclock', str(d / 'capture.nicclock.bin'),
                               '--out', str(d / 'bad')], capture_output=True, text=True)
        assert proc.returncode == 2 and 'experiment id' in proc.stdout, proc.stdout
    print('PASS: join (probe conn, pipelining, seq wrap, retransmit, unanswered, id check)')


def clock_samples(seconds, drift_ns, step_ns=0, seed=2):
    """1 ms NIC clock samples whose offset from a line follows a parabola of
    +-drift_ns over the run (like the 2026-11-24 r650 capture), with ~1.3 us
    TSC brackets; optional step in the NIC clock halfway through."""
    rng = np.random.default_rng(seed)
    n = seconds * 1000
    true_tsc = TSC0 + np.arange(n, dtype=np.int64) * int(CPU * 1000)
    t = np.linspace(-1, 1, n)
    offset_ns = drift_ns * (2 * t ** 2 - 1) + np.where(np.arange(n) >= n // 2, step_ns, 0)
    nic = (NIC0 + (true_tsc - TSC0) / TSC_PER_TICK + offset_ns * NIC_KHZ / 1e6).round().astype(np.int64)
    width = rng.integers(int(1.3 * CPU), int(1.5 * CPU), n)
    # The read lands at a consistent point inside the bracket; the 2026-11-24
    # capture shows ~10-40 ns of scatter around a fitted curve.
    early = (width // 2 + rng.normal(0, 15e-3 * CPU, n)).astype(np.int64)
    arr = np.zeros(n, dtype=[('tsc_before', '<u8'), ('nic', '<u8'), ('tsc_after', '<u8')])
    arr['tsc_before'], arr['nic'], arr['tsc_after'] = true_tsc - early, nic, true_tsc - early + width
    return arr, true_tsc, nic


def test_clock_drift_and_step():
    samples, true_tsc, nic = clock_samples(60, drift_ns=10_000)
    fit, convert, _ = jr.fit_nic_clock(CPU, NIC_KHZ, samples)
    err_ns = np.abs(convert(nic[::7]) - true_tsc[::7]) / CPU * 1000
    assert np.percentile(err_ns, 99) < 100, (np.percentile(err_ns, 99), fit)
    assert fit['residual_ns_p99'] < 200, fit
    # The old single-line fit would be off by microseconds on this clock.
    x = (nic - nic[0]).astype(float)
    line = np.polyfit(x, (true_tsc - true_tsc[0]).astype(float), 1)
    assert np.abs(np.polyval(line, x) - (true_tsc - true_tsc[0])).max() / CPU * 1000 > 3000
    samples, _, _ = clock_samples(60, drift_ns=0, step_ns=5_000)
    try:
        jr.fit_nic_clock(CPU, NIC_KHZ, samples)
    except jr.JoinError as e:
        assert 'residual' in str(e), e
    else:
        raise AssertionError('a 5 us clock step was not detected')
    gap = np.delete(samples, np.arange(20_000, 21_000))
    try:
        jr.fit_nic_clock(CPU, NIC_KHZ, gap)
    except jr.JoinError as e:
        assert 'gap' in str(e), e
    else:
        raise AssertionError('a 1 s sample gap was not detected')
    print('PASS: clock fit (drift accuracy, step and gap detection)')


if __name__ == '__main__':
    main()
    test_clock_drift_and_step()
