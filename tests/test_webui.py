import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from cache_mover.config import load_config
from cache_mover.status import read_status, write_run_status
from cache_mover.webui import build_server


SECONDS_PER_DAY = 86400


def write_file(path, content='data', mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as file:
        file.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def make_config(cache_path, backing_path, log_path, status_path, notification_urls=None):
    return {
        'Paths': {
            'CACHE_PATH': cache_path,
            'BACKING_PATH': backing_path,
            'LOG_PATH': log_path,
            'STATUS_PATH': status_path,
        },
        'Settings': {
            'THRESHOLD_PERCENTAGE': 70,
            'TARGET_PERCENTAGE': 25,
            'AGE_THRESHOLD_DAYS': 1,
            'MAX_WORKERS': 1,
            'EXCLUDED_DIRS': ['excluded'],
            'KEEP_EMPTY_DIRS': False,
            'SCHEDULE': '0 3 * * *',
            'LOG_LEVEL': 'INFO',
            'NOTIFICATIONS_ENABLED': bool(notification_urls),
            'NOTIFICATION_URLS': notification_urls or [],
            'NOTIFY_THRESHOLD': False,
            'WEB_UI_HOST': '127.0.0.1',
            'WEB_UI_PORT': 0,
        },
    }


class WebUIConfigTest(unittest.TestCase):
    def test_load_config_defaults_webui_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            os.makedirs(cache_path)
            os.makedirs(backing_path)
            config_path = os.path.join(tmp, 'config.yml')
            write_file(
                config_path,
                f"Paths:\n  CACHE_PATH: {cache_path}\n  BACKING_PATH: {backing_path}\n"
            )

            with patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)

            self.assertEqual(config['Paths']['STATUS_PATH'], '/var/log/cache-mover-status.json')
            self.assertEqual(config['Settings']['WEB_UI_HOST'], '0.0.0.0')
            self.assertEqual(config['Settings']['WEB_UI_PORT'], 9090)


class StatusWriterTest(unittest.TestCase):
    def test_write_run_status_persists_expected_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_path = os.path.join(tmp, 'status.json')
            config = {'Paths': {'STATUS_PATH': status_path}}

            for state in ('success', 'no_action', 'dry_run', 'error'):
                write_run_status(config, state, run_mode='age', error='boom' if state == 'error' else None)
                status = read_status(config)

                self.assertEqual(status['state'], state)
                self.assertEqual(status['run_mode'], 'age')
                self.assertIn('updated_at', status)
                if state == 'error':
                    self.assertEqual(status['error'], 'boom')
                else:
                    self.assertNotIn('error', status)


class WebUIEndpointTest(unittest.TestCase):
    def start_server(self, config):
        server = build_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def fetch_json(self, url, method='GET'):
        request = urllib.request.Request(url, method=method)
        if method == 'POST':
            request.data = b''
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())

    def test_api_status_returns_redacted_config_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            log_path = os.path.join(tmp, 'cache-mover.log')
            status_path = os.path.join(tmp, 'status.json')
            os.makedirs(cache_path)
            os.makedirs(backing_path)
            write_file(log_path, '')
            config = make_config(
                cache_path,
                backing_path,
                log_path,
                status_path,
                notification_urls=['discord://secret-token'],
            )
            write_run_status(config, 'success', run_mode='age', moved_count=2)

            base_url = self.start_server(config)
            payload = self.fetch_json(f'{base_url}/api/status')

            self.assertEqual(payload['last_run']['state'], 'success')
            self.assertEqual(payload['config']['settings']['notification_urls_count'], 1)
            self.assertNotIn('secret-token', json.dumps(payload))

    def test_api_logs_returns_bounded_recent_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            log_path = os.path.join(tmp, 'cache-mover.log')
            status_path = os.path.join(tmp, 'status.json')
            os.makedirs(cache_path)
            os.makedirs(backing_path)
            write_file(log_path, '\n'.join(f'line {i}' for i in range(10)) + '\n')
            config = make_config(cache_path, backing_path, log_path, status_path)

            base_url = self.start_server(config)
            payload = self.fetch_json(f'{base_url}/api/logs?lines=3')

            self.assertEqual(payload['line_count'], 3)
            self.assertEqual(payload['lines'], ['line 7', 'line 8', 'line 9'])

    def test_api_scan_reports_counts_without_moving_files(self):
        now = time.time()
        old = now - (2 * SECONDS_PER_DAY)

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            log_path = os.path.join(tmp, 'cache-mover.log')
            status_path = os.path.join(tmp, 'status.json')
            os.makedirs(cache_path)
            os.makedirs(backing_path)
            write_file(log_path, '')

            data_path = os.path.join(cache_path, 'old.bin')
            zero_path = os.path.join(cache_path, 'zero.bin')
            excluded_path = os.path.join(cache_path, 'excluded', 'old.bin')
            symlink_path = os.path.join(cache_path, 'link.bin')
            write_file(data_path, 'payload', mtime=old)
            write_file(zero_path, '', mtime=old)
            write_file(excluded_path, 'excluded', mtime=old)
            os.symlink(data_path, symlink_path)

            config = make_config(cache_path, backing_path, log_path, status_path)
            base_url = self.start_server(config)
            payload = self.fetch_json(f'{base_url}/api/scan', method='POST')

            self.assertEqual(payload['movable_files'], 2)
            self.assertEqual(payload['movable_bytes'], len('payload'))
            self.assertEqual(payload['symlinks'], 1)
            self.assertEqual(payload['zero_byte_skipped'], 1)
            self.assertEqual(payload['excluded_dirs_skipped'], 1)
            self.assertGreaterEqual(payload['age_eligible_files'], 1)
            self.assertTrue(os.path.exists(data_path))
            self.assertFalse(os.path.exists(os.path.join(backing_path, 'old.bin')))


if __name__ == '__main__':
    unittest.main()
