"""A live-only match must not hide a stale configuration on disk."""
import contextlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'tools'))
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'files'))
import review_flow as review


class PersistenceReviewTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'Requires POSIX observer socket permissions')
    def test_current_runtime_and_revision_cannot_hide_missing_persisted_users(self):
        with tempfile.TemporaryDirectory() as directory:
            path_type = pathlib.Path
            root = path_type(directory)

            def private_path(value):
                text = str(value)
                return root / text.lstrip('/') if text.startswith(('/etc/', '/var/', '/usr/', '/proc/')) else path_type(value)

            files = {
                '/etc/kenxu-meter/config.json': {},
                '/usr/local/etc/xray/config.json': {'api': {'listen': '127.0.0.1:10085'}, 'inbounds': [{'protocol': 'vless', 'port': 443, 'tag': 'managed', 'settings': {'clients': [{'email': 'legacy-fixture', 'id': 'keep-legacy'}]}}]},
                '/var/lib/kenxu-meter/desired.json': {'revision': 'same'},
            }
            for name, value in files.items():
                file = private_path(name)
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(json.dumps(value), encoding='utf-8')
            executable = private_path('/proc/4242/exe')
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'fixture-core')
            socket = private_path('/var/lib/kenxu-flow/observer.sock')
            socket.parent.mkdir(parents=True)
            socket.write_bytes(b'')
            socket.chmod(0o600)
            with contextlib.closing(sqlite3.connect(private_path('/var/lib/kenxu-meter/queue.sqlite'))) as database:
                database.executescript('CREATE TABLE queue(payload TEXT);CREATE TABLE flow_queue(payload TEXT);')
                database.commit()
            client = {'email': 'kenxu:fixture:route', 'id': 'fixture-id', 'level': 88}
            desired = {'revision': 'same', 'clients': [client]}

            class Reader:
                def request(self, *args):
                    return desired

                def runtime_users(self, *args):
                    return {client['email']: {'id': client['id'], 'level': 88, 'flow': ''}}

            class Connection:
                status = 200

                def request(self, *args): pass
                def close(self): pass
                def getresponse(self): return self
                def read(self): return b'{"scope":"all-managed","users":[],"totals":[]}'

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(review.pathlib, 'Path', side_effect=private_path))
                stack.enter_context(patch.object(review, 'native', return_value={}))
                stack.enter_context(patch.object(review, 'UnixHTTP', return_value=Connection()))
                stack.enter_context(patch.object(review.subprocess, 'check_output', return_value='4242'))
                stack.enter_context(patch.object(review.subprocess, 'call', return_value=0))
                stack.enter_context(patch.dict(sys.modules, {'kenxu_meter': types.SimpleNamespace(Meter=Reader)}))
                with self.assertRaisesRegex(AssertionError, 'ersisted'):
                    review.review()


if __name__ == '__main__':
    unittest.main()
