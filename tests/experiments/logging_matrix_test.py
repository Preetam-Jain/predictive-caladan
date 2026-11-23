import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/experiments'))
import logging_matrix as matrix
import remote


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.settings=tomllib.loads(Path(matrix.__file__).with_suffix('.toml').read_text())['matrix']

    def test_breadth_before_depth_and_equal_allocations(self):
        jobs=matrix.plan(self.settings)
        self.assertEqual(len(jobs),243)
        for rep in range(1,10):
            block=jobs[(rep-1)*27:rep*27]
            self.assertEqual({r['repetition'] for r in block},{rep})
            self.assertEqual(len({(r['condition'],r['be_cores'],r['writer_mode']) for r in block}),27)
        base=matrix.launcher.load_config(Path(matrix.__file__).with_name('suite-client34.toml'))
        for row in jobs[:27]:
            c=matrix.config_for(base,self.settings,row)
            self.assertEqual(c['allocation']['memcached_cores']+c['allocation']['antagonist_cores'],33)
            self.assertEqual(c['nodes']['server']['allowed_cpus'],'1,3,7-75,79-143')
            self.assertFalse(any(c['interventions'].values()))
            self.assertEqual(c['stream_dram']['region_bytes_per_worker'],256*1024**2)

    def test_only_stopped_owned_captures_can_be_pruned(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'suite'/'run';request=dict(root=str(root),token='owned')
            remote.main('init',request)
            (root/'capture.bw.bin').write_bytes(b'data')
            (root/'keep.log').write_text('keep')
            verified={'capture.bw.bin':{'size':4},'keep.log':{'size':4}}
            (root/'writer.job.status.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'live run'):
                remote.main('prune-captures',dict(request,verified=verified))
            (root/'writer.job.status.json').write_text('{"returncode":0}')
            self.assertEqual(remote.main('prune-captures',dict(request,verified=verified)),['capture.bw.bin'])
            self.assertTrue((root/'keep.log').exists())
            with self.assertRaisesRegex(RuntimeError,'ownership'):
                remote.main('prune-captures',dict(request,token='wrong',verified={}))
            remote.main('release',request)

    def test_infrastructure_failure_stops_instead_of_advancing(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            row=dict(id='broken',phase='matrix',condition='memcached_alone',cores=25,
                     be_cores=8,writer_mode='off',rps=5000000,repetition=1)
            (root/'broken').mkdir()
            (root/'broken/latest.json').write_text(json.dumps(dict(state='failed',errors=['disk full'])))
            state=dict(trials=[],pending=None)
            with patch.object(matrix.launcher,'run_one',side_effect=RuntimeError('disk full')):
                with self.assertRaisesRegex(RuntimeError,'Infrastructure failure'):
                    matrix.execute(root,state,{},row,0,1,dict(self.settings,infrastructure_retries=0))
            self.assertEqual(len(state['trials']),1)
            self.assertEqual(state['trials'][0]['outcome'],'failed')

    def test_cleanup_recovers_missing_exit_status_only_for_gone_process(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'suite'/'run';request=dict(root=str(root),token='owned')
            remote.main('init',request)
            status=root/'writer.job.status.json'
            status.write_text(json.dumps(dict(pid=999999999,proc_start='123',sudo=False)))
            with patch.object(remote.time,'sleep'):
                remote.main('stop',request)
            recovered=json.loads(status.read_text())
            self.assertTrue(recovered['recovered_missing_exit_status'])
            self.assertEqual(recovered['returncode'],125)
            remote.main('release',request)


if __name__=='__main__':
    unittest.main()
