import os
import tempfile
import unittest
from unittest.mock import patch

from cache_mover.cleanup import CleanupManager
from cache_mover.config import load_config
from cache_mover.filesystem import gather_files_to_move


SECONDS_PER_DAY = 86400


def make_config(cache_path, backing_path, age_threshold_days=0, threshold=70, target=25):
    return {
        'Paths': {
            'CACHE_PATH': cache_path,
            'BACKING_PATH': backing_path,
        },
        'Settings': {
            'THRESHOLD_PERCENTAGE': threshold,
            'TARGET_PERCENTAGE': target,
            'AGE_THRESHOLD_DAYS': age_threshold_days,
            'MAX_WORKERS': 1,
            'EXCLUDED_DIRS': ['excluded'],
            'KEEP_EMPTY_DIRS': False,
        },
    }


def write_file(path, content='data', mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as file:
        file.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class AgeThresholdConfigTest(unittest.TestCase):
    def test_load_config_defaults_age_threshold_to_zero(self):
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

            self.assertEqual(config['Settings']['AGE_THRESHOLD_DAYS'], 0)

    def test_load_config_reads_age_threshold_from_environment(self):
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

            with patch.dict(os.environ, {'AGE_THRESHOLD_DAYS': '5'}, clear=True):
                config = load_config(config_path)

            self.assertEqual(config['Settings']['AGE_THRESHOLD_DAYS'], 5)

    def test_load_config_rejects_negative_age_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            os.makedirs(cache_path)
            os.makedirs(backing_path)
            config_path = os.path.join(tmp, 'config.yml')
            write_file(
                config_path,
                "\n".join([
                    'Paths:',
                    f'  CACHE_PATH: {cache_path}',
                    f'  BACKING_PATH: {backing_path}',
                    'Settings:',
                    '  AGE_THRESHOLD_DAYS: -1',
                    '',
                ])
            )

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'AGE_THRESHOLD_DAYS'):
                    load_config(config_path)


class AgeThresholdGatherTest(unittest.TestCase):
    def test_gather_files_to_move_filters_age_exclusions_and_zero_byte_files(self):
        now = 2_000_000
        oldest = now - (3 * SECONDS_PER_DAY)
        old = now - (2 * SECONDS_PER_DAY)
        young = now - 3600

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            os.makedirs(cache_path)
            os.makedirs(backing_path)

            oldest_path = os.path.join(cache_path, 'oldest.bin')
            old_path = os.path.join(cache_path, 'old.bin')
            young_path = os.path.join(cache_path, 'young.bin')
            zero_path = os.path.join(cache_path, 'zero.bin')
            excluded_path = os.path.join(cache_path, 'excluded', 'old.bin')

            write_file(old_path, mtime=old)
            write_file(oldest_path, mtime=oldest)
            write_file(young_path, mtime=young)
            write_file(zero_path, content='', mtime=old)
            write_file(excluded_path, mtime=old)

            config = make_config(cache_path, backing_path)
            regular_files, hardlink_groups, symlinks = gather_files_to_move(
                config,
                age_threshold_days=1,
                now=now,
            )

            self.assertEqual(regular_files, [oldest_path, old_path])
            self.assertEqual(hardlink_groups, {})
            self.assertEqual(symlinks, {})


class AgeThresholdCleanupTest(unittest.TestCase):
    def test_check_usage_prioritizes_usage_then_age_then_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            os.makedirs(cache_path)
            os.makedirs(backing_path)

            usage_config = make_config(cache_path, backing_path, age_threshold_days=7)
            usage_manager = CleanupManager(usage_config)
            with patch('cache_mover.cleanup.get_fs_usage', return_value=80):
                _, needs_cleanup = usage_manager.check_usage()
            self.assertTrue(needs_cleanup)
            self.assertEqual(usage_manager.run_mode, 'usage')

            age_config = make_config(cache_path, backing_path, age_threshold_days=7)
            age_manager = CleanupManager(age_config)
            with patch('cache_mover.cleanup.get_fs_usage', return_value=20):
                _, needs_cleanup = age_manager.check_usage()
            self.assertTrue(needs_cleanup)
            self.assertEqual(age_manager.run_mode, 'age')

            disabled_config = make_config(cache_path, backing_path, age_threshold_days=0)
            disabled_manager = CleanupManager(disabled_config)
            with patch('cache_mover.cleanup.get_fs_usage', return_value=20):
                _, needs_cleanup = disabled_manager.check_usage()
            self.assertFalse(needs_cleanup)
            self.assertEqual(disabled_manager.run_mode, 'none')

    def test_age_mode_moves_file_even_when_cache_is_below_target(self):
        now = 2_000_000
        old = now - (2 * SECONDS_PER_DAY)

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            os.makedirs(cache_path)
            os.makedirs(backing_path)

            source_path = os.path.join(cache_path, 'old.bin')
            dest_path = os.path.join(backing_path, 'old.bin')
            write_file(source_path, content='payload', mtime=old)

            config = make_config(cache_path, backing_path, age_threshold_days=1)
            manager = CleanupManager(config)

            with patch('cache_mover.cleanup.get_fs_usage', return_value=20), \
                    patch('cache_mover.filesystem.time', return_value=now):
                _, needs_cleanup = manager.check_usage()
                result = manager.run_cleanup()

            self.assertTrue(needs_cleanup)
            self.assertEqual(manager.run_mode, 'age')
            self.assertEqual(result[0], 1)
            self.assertFalse(os.path.exists(source_path))
            self.assertTrue(os.path.exists(dest_path))
            with open(dest_path) as file:
                self.assertEqual(file.read(), 'payload')

    def test_age_mode_dry_run_reports_file_without_moving_it(self):
        now = 2_000_000
        old = now - (2 * SECONDS_PER_DAY)

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, 'cache')
            backing_path = os.path.join(tmp, 'backing')
            os.makedirs(cache_path)
            os.makedirs(backing_path)

            source_path = os.path.join(cache_path, 'old.bin')
            dest_path = os.path.join(backing_path, 'old.bin')
            write_file(source_path, content='payload', mtime=old)

            config = make_config(cache_path, backing_path, age_threshold_days=1)
            manager = CleanupManager(config, dry_run=True)

            with patch('cache_mover.cleanup.get_fs_usage', return_value=20), \
                    patch('cache_mover.filesystem.time', return_value=now):
                _, needs_cleanup = manager.check_usage()
                result = manager.run_cleanup()

            self.assertTrue(needs_cleanup)
            self.assertEqual(manager.run_mode, 'age')
            self.assertEqual(result[0], 1)
            self.assertTrue(os.path.exists(source_path))
            self.assertFalse(os.path.exists(dest_path))


if __name__ == '__main__':
    unittest.main()
