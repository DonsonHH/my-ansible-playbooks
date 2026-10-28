"""Exercise the real repair/rollback with private files and a systemd model."""
import contextlib
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'tools'))
import repair_meter_sandbox as repair


class RepairRollbackTests(unittest.TestCase):
    def test_post_start_failure_restores_effective_sandbox_and_preserves_state(self):
        # Reloading a unit changes future starts, not the mount namespace of an
        # already running collector. A second `start` must be modeled as a no-op.
        for previous in [None, b'[Service]\n# Previously reviewed local fragment\n']:
            with self.subTest(existing_fragment=previous is not None), tempfile.TemporaryDirectory() as directory:
                path_type = pathlib.Path
                root = path_type(directory)

                def private_path(value):
                    text = str(value)
                    return root / text.lstrip('/') if text.startswith(('/etc/', '/var/', '/usr/', '/proc/')) else path_type(value)

                registration = private_path('/etc/kenxu-meter/config.json')
                target = private_path('/usr/local/etc/xray/config.json')
                state = private_path('/var/lib/kenxu-meter')
                log = private_path('/var/log/kenxu')
                drop = private_path('/etc/systemd/system/kenxu-meter.service.d/80-kenxu-activity-log.conf')
                for parent in [registration.parent, target.parent, state, log, drop.parent]:
                    parent.mkdir(parents=True, exist_ok=True)
                registration.write_text(json.dumps({'activity_enabled': True}), encoding='utf-8')
                target.write_bytes(b'{"fixture":"keep exact current identities"}')
                if previous is not None:
                    drop.write_bytes(previous)
                queue = state / 'queue.sqlite'
                with contextlib.closing(sqlite3.connect(queue)) as database:
                    database.executescript('CREATE TABLE queue(payload TEXT);CREATE TABLE flow_queue(payload TEXT);CREATE TABLE sequence(value INTEGER);INSERT INTO sequence VALUES(37);')
                    database.commit()
                protected = {file: hashlib.sha256(file.read_bytes()).digest() for file in [registration, target, queue]}
                baseline = {'/var/lib/kenxu-meter', '/usr/local/etc/xray', '/var/lib/kenxu-flow'}
                manager = set(baseline)
                running = {'active': True, 'paths': set(baseline)}
                operations = []

                def command(*args, **kwargs):
                    operations.append(args)
                    self.assertEqual(args[0], 'systemctl')
                    if args[1:3] == ('show', 'kenxu-meter'):
                        return 'strict' if 'ProtectSystem' in args else ' '.join(manager)
                    if args[1:3] == ('show', 'xray'):
                        return '4242'
                    if args[1] == 'daemon-reload':
                        manager.clear()
                        manager.update(baseline)
                        if drop.exists() and b'ReadWritePaths=/var/log/kenxu' in drop.read_bytes():
                            manager.add('/var/log/kenxu')
                    elif args[1:3] == ('stop', 'kenxu-meter'):
                        running['active'] = False
                    elif args[1:3] == ('start', 'kenxu-meter') and not running['active']:
                        running.update(active=True, paths=set(manager))
                    return ''

                class Reader:
                    def request(self, *args):
                        raise RuntimeError('Fixture failure after the collector has started')

                fake = types.SimpleNamespace(Meter=Reader, client_identity=lambda value: value)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(repair.pathlib, 'Path', side_effect=private_path))
                    stack.enter_context(patch.object(repair.os, 'geteuid', return_value=0, create=True))
                    stack.enter_context(patch.object(repair.os, 'umask'))
                    stack.enter_context(patch.object(repair, 'command', side_effect=command))
                    stack.enter_context(patch.object(repair.time, 'sleep'))
                    stack.enter_context(patch.object(sys, 'path', list(sys.path)))
                    stack.enter_context(patch.dict(sys.modules, {'kenxu_meter': fake}))
                    with self.assertRaisesRegex(RuntimeError, 'Fixture failure'):
                        repair.repair()
                self.assertEqual(drop.read_bytes() if drop.exists() else None, previous)
                self.assertTrue(running['active'])
                self.assertEqual(running['paths'], baseline, 'Rollback must restore the running mount namespace, not only the unit file')
                self.assertFalse(any(args[1:3] == ('restart', 'xray') for args in operations))
                self.assertEqual({file: hashlib.sha256(file.read_bytes()).digest() for file in protected}, protected)


if __name__ == '__main__':
    unittest.main()
