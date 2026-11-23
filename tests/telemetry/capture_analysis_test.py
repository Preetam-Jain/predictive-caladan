"""Small synthetic capture verifies the native streaming analyzer's units/window."""
import csv
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
HEADER=struct.Struct('<8sIIIIdQIIIIdQQ16s')


class CaptureAnalysisTest(unittest.TestCase):
    def test_window_units_counter_wrap_and_parked_samples(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);binary=p/'analyzer'
            subprocess.run(['g++','-std=c++17','-O2',str(ROOT/'scripts/telemetry_capture_stats.cc'),'-o',str(binary)],check=True)
            def stream(suffix,kind,layout,records):
                data=struct.Struct(layout)
                with (p/('capture.'+suffix+'.bin')).open('wb') as f:
                    f.write(HEADER.pack(b'CALATLM\0',9,100,data.size,kind,100.,0,2,3,1,1,51200.,len(records),0,b'x'*16))
                    f.write(struct.pack('<I',7))
                    for r in records:f.write(data.pack(*r))
            stream('bw',1,'<QQ',[(1000,2**32-6),(100001000,4)])
            stream('llc',2,'<7Q2I',[(1000,1,10,1,1,1,1,7,42),(50001000,1,30,1,1,1,1,7,42),(100001000,1,50,1,1,1,1,7,42)])
            stream('pmc-failure',3,'<QQII',[(999,1,7,1),(1500,1,7,1)])
            stream('sched',4,'<QQIHH',[(1500,1,42,7,1)])
            stream('queue',5,'<6Q4I',[(999,99999,0,0,0,0,42,1,7,1),(1001,100,0,0,0,0,42,1,7,3),(1500,300,0,0,0,0,42,2,7,0),(1800,0,0,0,0,0,42,1,7,9)])
            stream('task',6,'<3Q4I',[(1500,2,4,42,1,7,1)])
            stream('queue-snapshot-failure',8,'<Q4I',[(1800,42,1,7,1)])
            stream('delay',7,'<2Q3d4I',[(1500,0,8.,0.,2.,42,2,1,0),(1800,0,float('nan'),float('nan'),float('nan'),42,2,8,0)])
            subprocess.run([str(binary),str(p/'capture'),'1000','1','42',str(p),'100'],check=True,capture_output=True)
            counters=json.loads((p/'counters.json').read_text())
            self.assertAlmostEqual(counters['estimated_mean_bandwidth_GB_s'],5120/1e9)
            self.assertEqual(counters['counts']['queue'],3)
            self.assertEqual(counters['counts']['invalid_runnable_queue'],1)
            self.assertEqual(counters['counts']['pmc-failure'],1)
            with (p/'distributions.csv').open() as f:rows={r['signal']:r for r in csv.DictReader(f)}
            self.assertEqual(float(rows['runnable_us']['mean']),2.)
            self.assertEqual(float(rows['runnable_active_us']['mean']),1.)
            self.assertEqual(float(rows['runnable_parked_us']['mean']),3.)
            self.assertEqual(float(rows['task_elapsed_us']['mean']),2.)
            self.assertEqual(float(rows['process_max_delay_us']['mean']),8.)
            with (p/'timeseries.csv').open() as f:time_bin=next(csv.DictReader(f))
            self.assertEqual(float(time_bin['process_delay_mean_us']),8.)
            with (p/'per-core.csv').open() as f:core=next(csv.DictReader(f))
            self.assertEqual(int(core['observed_misses']),20)
            self.assertEqual(float(core['covered_seconds']),.5)
            result = subprocess.run([str(binary),'--snapshot-frequency',str(p/'capture'),
                                     '1000','1'],check=True,capture_output=True,text=True)
            frequency = json.loads(result.stdout)['42']
            self.assertEqual(frequency['attempts'],3)
            self.assertEqual(frequency['writer_busy'],1)
            self.assertEqual(frequency['changed'],0)
            self.assertAlmostEqual(frequency['rejection_percent'],100/3)


if __name__=='__main__':unittest.main()
