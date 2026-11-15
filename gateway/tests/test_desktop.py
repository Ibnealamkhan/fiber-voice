"""Exercise the desktop port handoff and parent-owned shutdown, without a router."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


class DesktopLifecycleTests(unittest.TestCase):
    def test_dynamic_loopback_port_and_pipe_shutdown(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / 'ready.json'
            env = dict(os.environ, GATEWAY_RUNTIME=temporary, GATEWAY_READY_FILE=str(ready))
            env.pop('GATEWAY_PARENT_PID', None)
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen([sys.executable, '-u', str(root / 'desktop.py')],
                                           stdin=subprocess.PIPE, stdout=log, stderr=log, env=env)
                try:
                    deadline = time.monotonic() + 20
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    if not ready.exists():
                        log.seek(0)
                        self.fail('Gateway did not start: ' + log.read().decode(errors='replace'))
                    port = json.loads(ready.read_text())['port']
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    url = f'http://127.0.0.1:{port}/api/registration/status'
                    with opener.open(url, timeout=3) as response:
                        self.assertIsInstance(json.load(response), dict)
                    request = urllib.request.Request(url, headers={'Origin': 'https://example.com'})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        opener.open(request, timeout=3)
                    self.assertEqual(error.exception.code, 403)
                    error.exception.close()
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if not process.stdin.closed:
                        process.stdin.close()
