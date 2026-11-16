"""Read-only live viewer for one uniquely imaged Harbor trial."""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
from http.server import ThreadingHTTPServer
from threading import Event, Thread
import webbrowser

from harness.frontends.audit_mirror import AuditMirror
from harness.frontends.trajectory_reader import TrajectoryReader
from harness.frontends.trajectory_viewer import make_handler


class TaskLiveViewer:
    def __init__(self, runner, root: Path, image_id: str, job_dir: Path, *,
                 open_browser: bool = True):
        self.runner, self.root, self.image_id, self.job_dir = runner, root, image_id, job_dir
        self.open_browser = open_browser
        self.trajectory = job_dir / 'live' / 'agent' / 'harness-trajectory.jsonl'
        self.trajectory.parent.mkdir(parents=True, exist_ok=True)
        self.reader = TrajectoryReader(self.trajectory)
        self.stop_event = Event()
        self.mirror = None
        token = secrets.token_urlsafe(24)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.reader, token))
        self.url = f'http://127.0.0.1:{self.server.server_port}/t/{token}/'
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.monitor_thread = Thread(target=self._monitor, daemon=True)

    def start(self):
        self.server_thread.start()
        self.monitor_thread.start()
        (self.job_dir / 'viewer.json').write_text(json.dumps({'url': self.url}, indent=2) + '\n', encoding='utf-8')
        if self.open_browser and not webbrowser.open(self.url):
            raise RuntimeError("browser did not open; see viewer.json for the URL")
        print(f'live viewer: {self.url}', flush=True)

    def _command(self, argv):
        return self.runner.run(argv, cwd=self.root, timeout=15)

    def _discover(self):
        result = self._command(['docker', 'ps', '--filter', f'ancestor={self.image_id}', '--format', '{{.ID}}'])
        containers = result.stdout.split() if result.returncode == 0 else []
        if len(containers) != 1 or not re.fullmatch(r'[0-9a-f]{12,64}', containers[0]):
            return None
        container = containers[0]
        result = self._command(['docker', 'inspect', '--format', '{{.Config.WorkingDir}}', container])
        workdir = result.stdout.strip() if result.returncode == 0 else ''
        if not workdir.startswith('/') or '..' in Path(workdir).parts:
            workdir = '/app'
        result = self._command(['docker', 'exec', container, 'sh', '-c',
            'find "$1/.harness/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null', 'sh', workdir])
        paths = result.stdout.splitlines() if result.returncode == 0 else []
        if len(paths) != 1:
            return None
        return container, paths[0]

    def _monitor(self):
        while not self.stop_event.wait(2):
            try:
                found = self._discover()
                if found:
                    self.mirror = AuditMirror(*found, self.trajectory)
                    self.reader.mirror = self.mirror
                    self.mirror.start()
                    return
            except Exception:
                continue

    def finish(self):
        self.stop_event.set()
        self.monitor_thread.join(timeout=20)
        if self.mirror:
            self.mirror.close()
        # Harbor's final capture is authoritative and includes objects created
        # after the last live mirror poll.
        captures = list(self.job_dir.glob('**/agent/harness-trajectory.jsonl'))
        captures = [p for p in captures if p != self.trajectory]
        if len(captures) == 1:
            self.reader.trajectory = captures[0].resolve()

    def close(self):
        self.finish()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
