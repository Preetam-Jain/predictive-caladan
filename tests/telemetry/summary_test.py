"""Synthetic counter arithmetic and validation for the streaming summary."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

SUMMARY = Path(__file__).resolve().parents[2] / 'scripts/summarize_telemetry.py'
IDENTITY = bytes.fromhex('12345678123442348234123456789abc')
LAYOUTS = {'bw': (1, '<QQ'), 'llc': (2, '<QQQII'), 'pmc-failure': (3, '<QQII'), 'sched': (4, '<QQIHH')}


class SummaryTest(unittest.TestCase):
    def run_summary(self, records, different_uuid=False, version=4):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'run'
            for suffix, (kind, layout) in LAYOUTS.items():
                if version == 4 and suffix == 'sched':
                    continue
                values = records.get(suffix, [])
                identity = bytes(16) if different_uuid and kind == 2 else IDENTITY
                header = struct.pack('<8sIIIIdQIIIIdQQ16s', b'CALATLM\0', version,
                                     96, struct.calcsize(layout), kind, 1000.0,
                                     0, 4, 10, 0, 1, 64000.0 if kind == 1 else 0,
                                     len(values), 0, identity)
                Path(f'{prefix}.{suffix}.bin').write_bytes(
                    header + b''.join(struct.pack(layout, *value) for value in values))
            return subprocess.run([sys.executable, str(SUMMARY), str(prefix)],
                                  capture_output=True, text=True)

    def test_wrap_rates_and_assignment_validity(self):
        result = self.run_summary({
            'bw': [(1000, (1 << 32) - 5), (2000, 3), (4000, 7)],
            'llc': [(1000, 1, 10, 1, 11), (2000, 1, 20, 1, 11),
                    (3000, 2, 30, 1, 11), (4000, 2, 5, 1, 11),
                    (5000, 2, 8, 1, 11)],
            'pmc-failure': [(1500, 1, 1, 1)],
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mean: 256.000 MB/s', result.stdout)
        self.assertIn('1 11 5 2 13 0.000002 6500000.000', result.stdout)
        self.assertIn('assignment changed: 1', result.stdout)
        self.assertIn('counter decreased (wrap/reset): 1', result.stdout)
        self.assertIn('core=1 reason=1 count=1', result.stdout)
        self.assertIn('0.000001–0.000003 s: 2.000 us', result.stdout)
        self.assertIn('Generation changes with same PID: core=1 count=1', result.stdout)

    def test_scheduler_reasons(self):
        result = self.run_summary({'sched': [(1000, 1, 42, 7, 1),
                                              (2000, 2, 42, 7, 2),
                                              (3000, 3, 42, 7, 3)]}, version=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('worker scheduled', 'idle placeholder', 'core explicitly idled'):
            self.assertIn(f'core=7 pid=42 reason={name} count=1', result.stdout)

    def test_empty_capture(self):
        result = self.run_summary({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no valid intervals', result.stdout)

    def test_nonpositive_intervals(self):
        result = self.run_summary({'bw': [(1000, 1), (1000, 2), (900, 3)]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('nonpositive intervals skipped: 2', result.stdout)

    def test_mixed_experiments_rejected(self):
        result = self.run_summary({}, different_uuid=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('do not match', result.stderr)


if __name__ == '__main__':
    unittest.main()
