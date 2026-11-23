"""Dashboard data and HTTP boundary checks; no remote nodes required."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('dashboard', Path(__file__).resolve().parents[2] / 'scripts/experiments/dashboard.py')
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTest(unittest.TestCase):
    def test_current_attempt_and_partial_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'search'
            attempt = run / 'trial' / 'attempt-001'
            attempt.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps({'pending': {'id': 'trial'}}))
            (attempt / 'run.json').write_text('{"started_unix": 123, "state": "running"}')
            (run / 'summary.json').write_text('[')
            data = dashboard.snapshot(root, 'search')
            self.assertEqual(data['current']['started_unix'], 123)
            self.assertEqual(data['summary'], [])
            self.assertTrue(data['warnings'])
            with self.assertRaises(ValueError):
                dashboard.snapshot(root, '../secret')
            (root / 'escape').symlink_to('/tmp', target_is_directory=True)
            with self.assertRaises(ValueError):
                dashboard.snapshot(root, 'escape')

    def test_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'search'
            run.mkdir()
            (run / 'state.json').write_text('{"status":"complete"}')
            server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.handler_for(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                self.assertEqual(json.load(urlopen(base + '/api/runs')), ['search'])
                self.assertEqual(json.load(urlopen(base + '/api/run?name=search'))['state']['status'], 'complete')
                self.assertIn(b'Caladan experiments', urlopen(base).read())
                with self.assertRaises(HTTPError) as err:
                    urlopen(base + '/api/run?name=..')
                self.assertEqual(err.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
