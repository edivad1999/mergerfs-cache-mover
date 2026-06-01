import json
import os
import tempfile
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def get_status_path(config):
    return config.get('Paths', {}).get('STATUS_PATH', '/var/log/cache-mover-status.json')


def atomic_write_json(path, payload):
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix='.cache-mover-status.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def read_status(config):
    path = get_status_path(config)
    try:
        with open(path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        return {
            'state': 'unavailable',
            'status_path': path,
            'error': str(e),
            'updated_at': utc_now(),
        }


def write_run_status(config, state, **details):
    payload = {
        'state': state,
        'updated_at': utc_now(),
        'pid': os.getpid(),
    }
    payload.update({key: value for key, value in details.items() if value is not None})
    atomic_write_json(get_status_path(config), payload)
    return payload
