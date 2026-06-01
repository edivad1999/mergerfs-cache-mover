import argparse
import json
import logging
import os
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import psutil

from .config import load_config
from .filesystem import (
    _format_bytes,
    get_hardlink_groups,
    get_mtime,
    is_excluded,
    is_symlink,
)
from .status import read_status, utc_now


MAX_LOG_LINES = 2000
DEFAULT_LOG_LINES = 200


def disk_usage_snapshot(path):
    try:
        usage = shutil.disk_usage(path)
        percent = (usage.used / usage.total) * 100 if usage.total else 0
        return {
            'path': path,
            'exists': os.path.exists(path),
            'total': usage.total,
            'used': usage.used,
            'free': usage.free,
            'percent': percent,
            'total_human': _format_bytes(usage.total),
            'used_human': _format_bytes(usage.used),
            'free_human': _format_bytes(usage.free),
        }
    except OSError as e:
        return {
            'path': path,
            'exists': os.path.exists(path),
            'error': str(e),
        }


def command_contains(process, needle):
    cmdline = process.info.get('cmdline') or []
    return needle in ' '.join(cmdline)


def process_summary(process):
    return {
        'pid': process.info.get('pid'),
        'name': process.info.get('name'),
        'status': process.info.get('status'),
        'cmdline': ' '.join(process.info.get('cmdline') or [])[:240],
    }


def service_health(started_at):
    cron_processes = []
    mover_processes = []
    current_pid = os.getpid()

    for process in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
        try:
            if process.info.get('pid') == current_pid:
                continue

            name = process.info.get('name') or ''
            if name in ('cron', 'crond') or command_contains(process, 'cron -f'):
                cron_processes.append(process_summary(process))
            if command_contains(process, 'cache-mover.py'):
                mover_processes.append(process_summary(process))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    uptime_seconds = time.time() - started_at
    return {
        'cron_running': bool(cron_processes),
        'cron_processes': cron_processes,
        'mover_running': bool(mover_processes),
        'mover_processes': mover_processes,
        'webui_uptime_seconds': uptime_seconds,
        'webui_uptime_human': format_duration(uptime_seconds),
    }


def safe_config_summary(config):
    settings = config.get('Settings', {})
    paths = config.get('Paths', {})
    notification_urls = settings.get('NOTIFICATION_URLS') or []
    excluded_dirs = settings.get('EXCLUDED_DIRS') or []

    return {
        'paths': {
            'cache_path': paths.get('CACHE_PATH'),
            'backing_path': paths.get('BACKING_PATH'),
            'log_path': paths.get('LOG_PATH'),
            'status_path': paths.get('STATUS_PATH'),
        },
        'settings': {
            'schedule': settings.get('SCHEDULE'),
            'threshold_percentage': settings.get('THRESHOLD_PERCENTAGE'),
            'target_percentage': settings.get('TARGET_PERCENTAGE'),
            'age_threshold_days': settings.get('AGE_THRESHOLD_DAYS'),
            'max_workers': settings.get('MAX_WORKERS'),
            'log_level': settings.get('LOG_LEVEL'),
            'keep_empty_dirs': settings.get('KEEP_EMPTY_DIRS'),
            'notifications_enabled': settings.get('NOTIFICATIONS_ENABLED'),
            'notification_urls_count': len(notification_urls),
            'notify_threshold': settings.get('NOTIFY_THRESHOLD'),
            'excluded_dirs_count': len(excluded_dirs),
            'web_ui_host': settings.get('WEB_UI_HOST'),
            'web_ui_port': settings.get('WEB_UI_PORT'),
        },
    }


def build_status_payload(config, started_at):
    paths = config.get('Paths', {})
    return {
        'generated_at': utc_now(),
        'service': service_health(started_at),
        'storage': {
            'cache': disk_usage_snapshot(paths.get('CACHE_PATH')),
            'backing': disk_usage_snapshot(paths.get('BACKING_PATH')),
        },
        'config': safe_config_summary(config),
        'last_run': read_status(config),
    }


def parse_log_line_count(query):
    try:
        requested = int((query.get('lines') or [DEFAULT_LOG_LINES])[0])
    except (TypeError, ValueError):
        requested = DEFAULT_LOG_LINES
    return max(1, min(requested, MAX_LOG_LINES))


def tail_lines(path, line_count):
    if not path or not os.path.exists(path):
        return []

    chunk_size = 8192
    data = b''
    with open(path, 'rb') as file:
        file.seek(0, os.SEEK_END)
        position = file.tell()

        while position > 0 and data.count(b'\n') <= line_count:
            read_size = min(chunk_size, position)
            position -= read_size
            file.seek(position)
            data = file.read(read_size) + data

    text = data.decode(errors='replace')
    return text.splitlines()[-line_count:]


def logs_payload(config, line_count):
    path = config.get('Paths', {}).get('LOG_PATH')
    try:
        lines = tail_lines(path, line_count)
        return {
            'path': path,
            'requested_lines': line_count,
            'line_count': len(lines),
            'lines': lines,
        }
    except OSError as e:
        return {
            'path': path,
            'requested_lines': line_count,
            'line_count': 0,
            'lines': [],
            'error': str(e),
        }


def run_deep_scan(config):
    started = time.time()
    cache_path = config['Paths']['CACHE_PATH']
    excluded_dirs = config['Settings']['EXCLUDED_DIRS']
    age_threshold_days = config['Settings'].get('AGE_THRESHOLD_DAYS', 0)
    cutoff_time = None
    if age_threshold_days > 0:
        cutoff_time = time.time() - (age_threshold_days * 86400)

    movable_files = []
    movable_bytes = 0
    symlinks = 0
    age_eligible_files = 0
    age_eligible_bytes = 0
    zero_byte_skipped = 0
    excluded_dirs_skipped = 0
    scanned_dirs = 0
    scanned_files = 0
    errors = []

    for root, dirs, files in os.walk(cache_path):
        scanned_dirs += 1
        kept_dirs = []
        for dirname in sorted(dirs):
            dir_path = os.path.join(root, dirname)
            if is_excluded(dir_path, excluded_dirs):
                excluded_dirs_skipped += 1
            else:
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        if is_excluded(root, excluded_dirs):
            continue

        for filename in sorted(files):
            scanned_files += 1
            file_path = os.path.join(root, filename)
            try:
                is_link, _ = is_symlink(file_path)
                file_is_age_eligible = cutoff_time is not None and get_mtime(file_path) <= cutoff_time

                if is_link:
                    symlinks += 1
                    if file_is_age_eligible:
                        age_eligible_files += 1
                    continue

                file_size = os.path.getsize(file_path)
                if file_size == 0:
                    zero_byte_skipped += 1
                    continue

                movable_files.append(file_path)
                movable_bytes += file_size

                if file_is_age_eligible:
                    age_eligible_files += 1
                    age_eligible_bytes += file_size
            except (OSError, IOError) as e:
                if len(errors) < 25:
                    errors.append({'path': file_path, 'error': str(e)})

    hardlink_groups = get_hardlink_groups(movable_files)
    hardlinked_files = sum(len(group) for group in hardlink_groups.values())
    duration = time.time() - started

    return {
        'generated_at': utc_now(),
        'duration_seconds': duration,
        'duration_human': format_duration(duration),
        'cache_path': cache_path,
        'scanned_dirs': scanned_dirs,
        'scanned_files': scanned_files,
        'movable_files': len(movable_files) + symlinks,
        'regular_files': len(movable_files) - hardlinked_files,
        'hardlink_groups': len(hardlink_groups),
        'hardlinked_files': hardlinked_files,
        'symlinks': symlinks,
        'movable_bytes': movable_bytes,
        'movable_bytes_human': _format_bytes(movable_bytes),
        'age_threshold_days': age_threshold_days,
        'age_eligible_files': age_eligible_files,
        'age_eligible_bytes': age_eligible_bytes,
        'age_eligible_bytes_human': _format_bytes(age_eligible_bytes),
        'zero_byte_skipped': zero_byte_skipped,
        'excluded_dirs_skipped': excluded_dirs_skipped,
        'errors': errors,
    }


def format_duration(seconds):
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def dashboard_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cache Mover Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #1c2430;
      --muted: #667085;
      --line: #d9dee7;
      --blue: #2867b2;
      --green: #16845b;
      --amber: #b76b00;
      --red: #b42318;
      --shadow: 0 8px 24px rgba(31, 42, 55, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 24px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }
    h1, h2 { margin: 0; letter-spacing: 0; }
    h1 { font-size: 20px; font-weight: 700; }
    h2 { font-size: 15px; font-weight: 700; }
    main { padding: 20px 24px 32px; max-width: 1440px; margin: 0 auto; }
    .grid { display: grid; gap: 16px; }
    .top { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .two { grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 16px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
      min-width: 0;
    }
    .metric-label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .metric-value { font-size: 24px; font-weight: 750; margin-top: 4px; overflow-wrap: anywhere; }
    .status-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      background: #eef2f7;
      color: var(--ink);
    }
    .ok { background: #e8f5ef; color: var(--green); }
    .warn { background: #fff3df; color: var(--amber); }
    .bad { background: #fdeceb; color: var(--red); }
    .bar { height: 10px; background: #edf1f5; border-radius: 999px; overflow: hidden; margin: 10px 0 8px; }
    .fill { height: 100%; width: 0; background: var(--blue); transition: width .2s ease; }
    .fill.warn { background: var(--amber); }
    .fill.bad { background: var(--red); }
    .kv { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .kv td { padding: 7px 0; border-bottom: 1px solid #eef1f4; vertical-align: top; }
    .kv td:first-child { color: var(--muted); width: 42%; padding-right: 12px; }
    .kv td:last-child { overflow-wrap: anywhere; }
    .toolbar { display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
    button {
      border: 1px solid #1f5f9f;
      background: var(--blue);
      color: #fff;
      border-radius: 8px;
      min-height: 36px;
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled { opacity: .55; cursor: wait; }
    pre {
      margin: 0;
      min-height: 360px;
      max-height: 520px;
      overflow: auto;
      padding: 12px;
      border-radius: 8px;
      background: #101923;
      color: #d6e3ef;
      font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      white-space: pre-wrap;
    }
    .muted { color: var(--muted); }
    .span-2 { grid-column: span 2; }
    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; }
      .top, .two { grid-template-columns: 1fr; }
      .span-2 { grid-column: span 1; }
      main { padding: 16px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Cache Mover Monitor</h1>
      <div class="muted" id="generatedAt">Waiting for status</div>
    </div>
    <div class="status-row">
      <span class="badge" id="cronBadge">Cron</span>
      <span class="badge" id="moverBadge">Mover</span>
      <span class="badge ok" id="webBadge">WebUI</span>
    </div>
  </header>
  <main>
    <section class="grid top">
      <div class="panel"><div class="metric-label">Cache Used</div><div class="metric-value" id="cacheUsed">-</div></div>
      <div class="panel"><div class="metric-label">Backing Free</div><div class="metric-value" id="backingFree">-</div></div>
      <div class="panel"><div class="metric-label">Last Run</div><div class="metric-value" id="lastRunState">-</div></div>
      <div class="panel"><div class="metric-label">WebUI Uptime</div><div class="metric-value" id="uptime">-</div></div>
    </section>

    <section class="grid two">
      <div class="panel">
        <h2>Storage</h2>
        <div id="storagePanels"></div>
      </div>
      <div class="panel">
        <h2>Configuration</h2>
        <table class="kv" id="configTable"></table>
      </div>
      <div class="panel">
        <h2>Last Run</h2>
        <table class="kv" id="runTable"></table>
      </div>
      <div class="panel">
        <div class="toolbar">
          <h2>Deep Scan</h2>
          <button id="scanButton">Run deep scan</button>
        </div>
        <table class="kv" id="scanTable"></table>
      </div>
      <div class="panel span-2">
        <div class="toolbar">
          <h2>Recent Logs</h2>
          <span class="muted" id="logMeta"></span>
        </div>
        <pre id="logs"></pre>
      </div>
    </section>
  </main>
<script>
const byId = (id) => document.getElementById(id);
const fmtBytes = (value) => {
  if (value === null || value === undefined) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let n = Number(value);
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(2)}${units[i]}`;
};
const fmtPercent = (value) => value === undefined ? '-' : `${Number(value).toFixed(1)}%`;
const fmtDuration = (value) => {
  if (value === undefined || value === null) return '-';
  let seconds = Math.round(Number(value));
  const days = Math.floor(seconds / 86400); seconds %= 86400;
  const hours = Math.floor(seconds / 3600); seconds %= 3600;
  const minutes = Math.floor(seconds / 60); seconds %= 60;
  if (days) return `${days}d ${hours}h ${minutes}m`;
  if (hours) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
};
async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}
function setBadge(el, enabled, okText, badText) {
  el.textContent = enabled ? okText : badText;
  el.className = `badge ${enabled ? 'ok' : 'bad'}`;
}
function rows(table, values) {
  table.textContent = '';
  for (const [key, value] of values) {
    const tr = document.createElement('tr');
    const left = document.createElement('td');
    const right = document.createElement('td');
    left.textContent = key;
    right.textContent = value === undefined || value === null || value === '' ? '-' : String(value);
    tr.append(left, right);
    table.append(tr);
  }
}
function storageBlock(name, data) {
  const panel = document.createElement('div');
  panel.style.marginTop = '14px';
  if (data.error) {
    panel.textContent = `${name}: ${data.error}`;
    return panel;
  }
  const title = document.createElement('div');
  title.innerHTML = `<strong>${name}</strong> <span class="muted">${data.path || ''}</span>`;
  const bar = document.createElement('div');
  bar.className = 'bar';
  const fill = document.createElement('div');
  fill.className = `fill ${data.percent >= 90 ? 'bad' : data.percent >= 75 ? 'warn' : ''}`;
  fill.style.width = `${Math.min(100, Math.max(0, data.percent || 0))}%`;
  bar.append(fill);
  const meta = document.createElement('div');
  meta.className = 'muted';
  meta.textContent = `${fmtPercent(data.percent)} used - ${data.used_human || fmtBytes(data.used)} of ${data.total_human || fmtBytes(data.total)} - ${data.free_human || fmtBytes(data.free)} free`;
  panel.append(title, bar, meta);
  return panel;
}
async function refreshStatus() {
  try {
    const data = await fetchJSON('/api/status');
    byId('generatedAt').textContent = `Updated ${data.generated_at}`;
    setBadge(byId('cronBadge'), data.service.cron_running, 'Cron running', 'Cron down');
    setBadge(byId('moverBadge'), data.service.mover_running, 'Mover running', 'Mover idle');
    byId('moverBadge').className = `badge ${data.service.mover_running ? 'warn' : 'ok'}`;
    byId('uptime').textContent = data.service.webui_uptime_human;
    byId('cacheUsed').textContent = data.storage.cache.error ? 'Error' : fmtPercent(data.storage.cache.percent);
    byId('backingFree').textContent = data.storage.backing.error ? 'Error' : (data.storage.backing.free_human || fmtBytes(data.storage.backing.free));
    const state = data.last_run?.state || 'unknown';
    byId('lastRunState').textContent = state.replace('_', ' ');

    const storagePanels = byId('storagePanels');
    storagePanels.textContent = '';
    storagePanels.append(storageBlock('Cache', data.storage.cache));
    storagePanels.append(storageBlock('Backing', data.storage.backing));

    const settings = data.config.settings;
    const paths = data.config.paths;
    rows(byId('configTable'), [
      ['Schedule', settings.schedule],
      ['Threshold / Target', `${settings.threshold_percentage}% / ${settings.target_percentage}%`],
      ['Age threshold', `${settings.age_threshold_days} day(s)`],
      ['Workers', settings.max_workers],
      ['Excluded dirs', settings.excluded_dirs_count],
      ['Notifications', settings.notifications_enabled ? `enabled (${settings.notification_urls_count})` : 'disabled'],
      ['Log path', paths.log_path],
      ['Status path', paths.status_path],
    ]);

    const run = data.last_run || {};
    rows(byId('runTable'), [
      ['State', run.state],
      ['Run mode', run.run_mode],
      ['Dry run', run.dry_run],
      ['Started', run.started_at],
      ['Ended', run.ended_at],
      ['Moved files', run.moved_count],
      ['Moved bytes', run.total_bytes !== undefined ? fmtBytes(run.total_bytes) : undefined],
      ['Duration', run.elapsed_time !== undefined ? fmtDuration(run.elapsed_time) : undefined],
      ['Final usage', run.final_usage !== undefined ? fmtPercent(run.final_usage) : undefined],
      ['Message', run.message || run.error],
    ]);
  } catch (error) {
    byId('generatedAt').textContent = `Status error: ${error.message}`;
  }
}
async function refreshLogs() {
  try {
    const data = await fetchJSON('/api/logs?lines=200');
    byId('logMeta').textContent = `${data.line_count} line(s)`;
    byId('logs').textContent = data.lines.join('\\n') || 'No log lines found.';
  } catch (error) {
    byId('logs').textContent = `Log error: ${error.message}`;
  }
}
async function runScan() {
  const button = byId('scanButton');
  button.disabled = true;
  button.textContent = 'Scanning';
  try {
    const data = await fetchJSON('/api/scan', { method: 'POST' });
    rows(byId('scanTable'), [
      ['Generated', data.generated_at],
      ['Duration', data.duration_human],
      ['Scanned dirs', data.scanned_dirs],
      ['Scanned files', data.scanned_files],
      ['Movable files', data.movable_files],
      ['Movable bytes', data.movable_bytes_human],
      ['Regular files', data.regular_files],
      ['Hardlink groups', data.hardlink_groups],
      ['Hardlinked files', data.hardlinked_files],
      ['Symlinks', data.symlinks],
      ['Age eligible files', data.age_eligible_files],
      ['Age eligible bytes', data.age_eligible_bytes_human],
      ['Zero-byte skipped', data.zero_byte_skipped],
      ['Excluded dirs skipped', data.excluded_dirs_skipped],
      ['Errors', data.errors.length],
    ]);
  } catch (error) {
    rows(byId('scanTable'), [['Error', error.message]]);
  } finally {
    button.disabled = false;
    button.textContent = 'Run deep scan';
  }
}
byId('scanButton').addEventListener('click', runScan);
refreshStatus();
refreshLogs();
setInterval(refreshStatus, 5000);
setInterval(refreshLogs, 5000);
</script>
</body>
</html>
"""


class CacheMoverWebHandler(BaseHTTPRequestHandler):
    server_version = "CacheMoverWebUI/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            self.send_bytes(dashboard_html().encode(), 'text/html; charset=utf-8')
        elif parsed.path == '/healthz':
            self.send_bytes(b'OK\n', 'text/plain; charset=utf-8')
        elif parsed.path == '/api/status':
            self.send_json(build_status_payload(self.server.config, self.server.started_at))
        elif parsed.path == '/api/logs':
            line_count = parse_log_line_count(parse_qs(parsed.query))
            self.send_json(logs_payload(self.server.config, line_count))
        else:
            self.send_error(404, 'Not found')

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/scan':
            self.send_json(run_deep_scan(self.server.config))
        else:
            self.send_error(404, 'Not found')

    def send_json(self, payload, status=200):
        self.send_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode(),
            'application/json; charset=utf-8',
            status=status,
        )

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logging.info("webui: " + fmt, *args)


class CacheMoverWebServer(ThreadingHTTPServer):
    allow_reuse_address = True


def build_server(config):
    settings = config.get('Settings', {})
    server = CacheMoverWebServer(
        (settings.get('WEB_UI_HOST', '0.0.0.0'), settings.get('WEB_UI_PORT', 9090)),
        CacheMoverWebHandler,
    )
    server.config = config
    server.started_at = time.time()
    return server


def main():
    parser = argparse.ArgumentParser(description='MergerFS Cache Mover WebUI')
    parser.add_argument('--config', help='Path to config')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    config = load_config(args.config)
    server = build_server(config)
    host, port = server.server_address
    logging.info(f"Starting Cache Mover WebUI on {host}:{port}")
    server.serve_forever()


if __name__ == '__main__':
    main()
