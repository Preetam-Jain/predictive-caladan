"""Run with python3 tests/telemetry/decoder_test.py."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid

DECODER = Path(__file__).resolve().parents[2] / 'scripts/decode_telemetry.py'
RECORDS = {1: struct.pack('<QQ', 100, 99),
           2: struct.pack('<QQQII', 100, 9, 99, 7, 42),
           3: struct.pack('<QQII', 100, 9, 7, 1),
           4: struct.pack('<QQIHH', 100, 9, 42, 7, 2)}


def fixture(cores, stream_type=1, complete=True, version=4):
    record = RECORDS[stream_type]
    if version >= 6 and stream_type == 2:
        record = struct.pack('<QQQQQQII', 100, 9, 99, 123, 8, 8, 7, 42)
    if version >= 7 and stream_type == 2:
        record = struct.pack('<QQQQQQQII', 100, 9, 99, 123, 8, 8, 10, 7, 42)
    return (struct.pack('<8sIIIIdQIIIIdQQ16s', b'CALATLM\0', version,
                        96 + 4 * len(cores), len(record), stream_type, 2400.0,
                        0, 4, 0, len(cores), int(complete), 153600.0, 1, 17,
                        uuid.UUID('12345678-1234-4234-8234-123456789abc').bytes)
            + b''.join(struct.pack('<I', core) for core in cores) + record)


class DecoderTest(unittest.TestCase):
    def decode(self, data):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(data)
            stream.flush()
            return subprocess.run([sys.executable, str(DECODER), stream.name],
                                  capture_output=True, text=True)

    def test_all_streams_and_variable_cpu_counts(self):
        for stream_type in RECORDS:
            for count in (0, 2, 300):
                with self.subTest(stream_type=stream_type, count=count):
                    result = self.decode(fixture(list(range(count)), stream_type,
                                                 version=5 if stream_type == 4 else 4))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('completed: records=1 dropped=17', result.stderr)
                    self.assertIn('experiment_id=12345678-1234-4234-8234-123456789abc', result.stderr)

    def test_version5_requires_scheduler_file(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'run'
            suffixes = ('bw', 'llc', 'pmc-failure', 'sched')
            for kind, suffix in enumerate(suffixes[:3], 1):
                Path(f'{prefix}.{suffix}.bin').write_bytes(fixture([], kind, version=5))
            command = [sys.executable, str(DECODER), '--check-prefix', str(prefix)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            Path(f'{prefix}.sched.bin').write_bytes(fixture([], 4, version=5))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('sched.bin', result.stdout)

    def test_scheduler_record_not_allowed_in_version4(self):
        self.assertNotEqual(self.decode(fixture([], 4)).returncode, 0)

    def test_version6_ownership_layout(self):
        result = self.decode(fixture([], 2, version=6))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('app_owner_gen_at_request', result.stdout)
        self.assertIn('app_owner_gen_at_collection', result.stdout)
        self.assertIn('100,9,99,123,8,8,7,42', result.stdout)

    def test_version7_collection_generation(self):
        result = self.decode(fixture([], 2, version=7))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sched_gen_at_collection', result.stdout)
        self.assertIn('100,9,99,123,8,8,10,7,42', result.stdout)

    def test_version8_streams(self):
        new_records = {
            5: struct.pack('<6Q4I', 100, 1, 2, 3, 4, 5, 42, 43, 7, 3),
            6: struct.pack('<3Q4I', 100, 12, 34, 42, 43, 7, 9),
            7: struct.pack('<2Q3d4I', 100, 200, 12.5, 1.5, 7.5, 42, 2, 5, 0),
        }
        for kind, record in new_records.items():
            with self.subTest(kind=kind):
                header = bytearray(fixture([], version=8)[:96])
                struct.pack_into('<II', header, 16, len(record), kind)
                result = self.decode(header + record)
                self.assertEqual(result.returncode, 0, result.stderr)
                struct.pack_into('<I', header, 8, 7)
                self.assertNotEqual(self.decode(header + record).returncode, 0)

    def test_incomplete(self):
        self.assertNotEqual(self.decode(fixture([], complete=False)).returncode, 0)

    def test_truncation_and_extra_data(self):
        data = fixture([1, 2])
        for corrupted in (data[:99], data[:-1], data + b'x'):
            self.assertNotEqual(self.decode(corrupted).returncode, 0)

    def test_wrong_record_size(self):
        data = bytearray(fixture([1]))
        struct.pack_into('<I', data, 16, 48)
        self.assertNotEqual(self.decode(data).returncode, 0)


if __name__ == '__main__':
    unittest.main()
