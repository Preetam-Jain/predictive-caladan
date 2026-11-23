#!/usr/bin/env python3
"""Read-only desktop dashboard. Run with python3 scripts/experiments/dashboard.py."""
from __future__ import annotations
import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_RESULTS = Path(__file__).resolve().parents[2] / 'results'
ASSET = Path(__file__).with_suffix('.html')


def read_json(path, fallback, warnings):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError) as exc:
        warnings.append(f'{path.name}: {exc}; retrying on next refresh')
        return fallback


def snapshot(root, name):
    # Only immediate experiment directories can be read through the API.
    if not name or Path(name).name != name:
        raise ValueError('Invalid run name')
    folder = (root / name).resolve()
    if folder.parent != root.resolve() or not folder.is_dir():
        raise ValueError('Unknown run')
    warnings = []
    state = read_json(folder / 'state.json', {}, warnings)
    pending = state.get('pending')
    current = {}
    if isinstance(pending, dict):
        trial_id = pending.get('id', '')
        if trial_id and Path(trial_id).name == trial_id:
            current = read_json(folder / trial_id / 'latest.json', {}, warnings)
            if not current:
                attempts = sorted((folder / trial_id).glob('attempt-*/run.json'))
                if attempts:
                    current = read_json(attempts[-1], {}, warnings)
    try:
        with (folder / 'launcher.log').open(errors='replace') as stream:
            log = ''.join(deque(stream, maxlen=65))[-20000:]
    except OSError:
        log = ''
    updated = max((p.stat().st_mtime for p in (folder / 'state.json', folder / 'launcher.log') if p.exists()), default=None)
    return dict(name=name, state=state, summary=read_json(folder / 'summary.json', [], warnings),
                settings=read_json(folder / 'search-settings.json', {}, warnings),
                current=current, updated=updated, log=log, warnings=warnings)


def handler_for(root):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/':
                    content, mime = ASSET.read_bytes(), 'text/html; charset=utf-8'
                elif parsed.path == '/api/runs':
                    runs = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / 'state.json').is_file()]
                    runs.sort(key=lambda p: (p / 'state.json').stat().st_mtime, reverse=True)
                    content, mime = json.dumps([p.name for p in runs]).encode(), 'application/json'
                elif parsed.path == '/api/run':
                    name = parse_qs(parsed.query).get('name', [''])[0]
                    content, mime = json.dumps(snapshot(root, name), allow_nan=False).encode(), 'application/json'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except ValueError as exc:
                self.send_error(400, str(exc))
            except OSError:
                self.send_error(503, 'Results temporarily unavailable')

        def log_message(self, *_):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=DEFAULT_RESULTS)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    root = args.results.resolve()
    if not root.is_dir():
        parser.error(f'Results directory does not exist: {root}')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(root))
    print(f'Experiment dashboard: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
