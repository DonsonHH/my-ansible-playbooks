import importlib.util
import pathlib
import unittest
import sys
import json
import sqlite3
import time
import os
import stat
import tempfile
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'files'))

if sys.platform == 'win32':
    sys.modules['fcntl'] = MagicMock()

spec = importlib.util.spec_from_file_location('kenxu_meter', pathlib.Path(__file__).parents[1] / 'files' / 'kenxu_meter.py')
meter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meter)


class MeterTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'Requires POSIX file ownership')
    def test_bootstrap_prepares_xray_owned_log_before_root_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory);target = root / 'config.json';log = root / 'logs' / 'access.log'
            target.write_text(json.dumps({'inbounds':[{'protocol':'vless','port':443,'settings':{'clients':[]}}],'outbounds':[{'tag':'block'}]}))
            collector = meter.Meter.__new__(meter.Meter)
            collector.root = root
            collector.settings = {'node_id':'fixture','xray_config':str(target),'activity_enabled':True,'access_log':str(log)}
            collector.request = MagicMock(return_value={'nodeId':'fixture','clients':[],'revision':'fixture'})
            collector.restart_core = MagicMock()
            collector.runtime_users = MagicMock(return_value={})
            def validate(*args):
                self.assertEqual(args[1:3], ('run','-test'))
                self.assertTrue(log.is_file(), 'Root validation must not be the first creator')
                self.assertEqual((log.stat().st_uid,log.stat().st_gid),(os.getuid(),os.getgid()))
                self.assertEqual(stat.S_IMODE(log.stat().st_mode),0o600)
                return ''
            collector.run_command = MagicMock(side_effect=validate)
            with patch('pwd.getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())):
                collector.sync_locked(bootstrap=True)
            self.assertTrue(collector.run_command.called)
            collector.restart_core.assert_called_once()
            self.assertEqual(json.loads(target.read_text())['log']['access'],str(log))

    @unittest.skipIf(sys.platform == 'win32', 'Requires POSIX file ownership')
    def test_activity_log_is_private_before_validation_and_existing_bytes_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'activity' / 'access.log'
            owner = (os.getuid(), os.getgid())
            meter.prepare_activity_log(path, owner)
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_bytes(b'preserve-private-access-records\n')
            inode = path.stat().st_ino
            os.chmod(path, 0o644)
            meter.prepare_activity_log(path, owner)
            self.assertEqual(path.read_bytes(), b'preserve-private-access-records\n')
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(sys.platform == 'win32', 'Requires POSIX symlinks and special files')
    def test_activity_log_refuses_symlinks_hardlinks_and_special_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            owner = (os.getuid(), os.getgid())
            target = root / 'kept';target.write_bytes(b'kept')
            link = root / 'access.log';link.symlink_to(target)
            with self.assertRaises(OSError):meter.prepare_activity_log(link, owner)
            link.unlink();os.link(target, link)
            with self.assertRaises(ValueError):meter.prepare_activity_log(link, owner)
            link.unlink();os.mkfifo(link)
            with self.assertRaises((ValueError, OSError)):meter.prepare_activity_log(link, owner)
            link.unlink();parent = root / 'logs';parent.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):meter.prepare_activity_log(parent / 'access.log', owner)
            self.assertEqual(target.read_bytes(), b'kept')

    def test_queue_redacts_expired_metadata_without_dropping_accounting(self):
        collector = meter.Meter.__new__(meter.Meter)
        collector.db = sqlite3.connect(':memory:')
        collector.db.execute('CREATE TABLE queue(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        payload = {'at': int(time.time()*1000)-31*86400000, 'epoch': 'old', 'seq': 1, 'counters': [{'email':'kenxu:a:n','up':100,'down':900}], 'activity':[{'sources':['192.0.2.1']}]}
        collector.db.execute('INSERT INTO queue(payload) VALUES(?)', (json.dumps(payload),))
        collector.db.commit()
        collector.request = MagicMock(side_effect=RuntimeError('Offline'))
        with self.assertRaises(RuntimeError):
            collector.flush()
        saved = json.loads(collector.db.execute('SELECT payload FROM queue').fetchone()[0])
        self.assertEqual(saved, {**payload, 'activity': []})
        collector.request = MagicMock(return_value={'ok':True})
        collector.flush()
        self.assertEqual(collector.db.execute('SELECT count(*) FROM queue').fetchone()[0], 0)
        self.assertEqual(collector.request.call_args.args[1]['counters'], payload['counters'])
        collector.db.close()

    def test_patch_preserves_legacy_and_outbound_routing(self):
        original = {'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': {'clients': [{'id': 'legacy', 'email': 'private'}]}, 'streamSettings': {'security': 'reality', 'realitySettings': {'privateKey': 'fixture'}}}], 'outbounds': [{'tag': 'block', 'protocol': 'blackhole'}, {'tag': 'warp', 'protocol': 'socks'}], 'routing': {'rules': [{'outboundTag': 'warp', 'domain': ['geosite:openai']}]}}
        clients = [{'id': 'managed', 'email': 'kenxu:test:node', 'level': 88, 'flow': 'xtls-rprx-vision'}]
        patched, tag = meter.prepare_config(original, clients)
        self.assertEqual(patched['inbounds'][0]['settings']['clients'][0], original['inbounds'][0]['settings']['clients'][0])
        self.assertEqual(patched['inbounds'][0]['streamSettings'], original['inbounds'][0]['streamSettings'])
        self.assertEqual(patched['outbounds'], original['outbounds'])
        self.assertEqual(patched['routing']['rules'][-1], original['routing']['rules'][0])
        self.assertEqual(tag, 'kenxu-in-443')
        again, _ = meter.prepare_config(patched, clients)
        self.assertEqual(again, patched)
        removed, _ = meter.prepare_config(patched, [])
        self.assertEqual(len(removed['inbounds'][0]['settings']['clients']), 1)
        self.assertEqual(removed['routing']['rules'], original['routing']['rules'])
        logging, _ = meter.prepare_config(patched, clients, activity_log='/var/log/kenxu/access.log')
        self.assertTrue(logging['policy']['levels']['88']['statsUserOnline'])
        self.assertEqual(logging['log'], {'access': '/var/log/kenxu/access.log', 'loglevel': 'warning', 'dnsLog': False})
        self.assertNotIn('log', original)

    def test_counters_ignore_machine_and_unmanaged_users(self):
        result = meter.parse_counters({'stat': [{'name': 'user>>>kenxu:a:n>>>traffic>>>uplink', 'value': '120'}, {'name': 'user>>>kenxu:a:n>>>traffic>>>downlink', 'value': 300}, {'name': 'user>>>private>>>traffic>>>uplink', 'value': 1000}, {'name': 'inbound>>>all>>>traffic>>>downlink', 'value': 9000}]})
        self.assertEqual(result, [{'email': 'kenxu:a:n', 'up': 120, 'down': 300}])

    def test_shared_core_keeps_uk_users_on_uk_outbound(self):
        original = {'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': {'clients': [{'id': 'legacy', 'email': 'private'}]}}], 'outbounds': [{'tag': 'block'}, {'tag': 'uk-gusecure2'}], 'routing': {'rules': [{'type': 'field', 'user': ['personal-uk'], 'outboundTag': 'uk-gusecure2'}]}}
        clients = [{'id': 'a', 'email': 'kenxu:a:sg', 'level': 88}, {'id': 'b', 'email': 'kenxu:a:uk', 'level': 88}]
        routes = [{'nodeId': 'sg', 'clients': clients[:1]}, {'nodeId': 'uk', 'outboundTag': 'uk-gusecure2', 'clients': clients[1:]}]
        patched, _ = meter.prepare_config(original, clients, routes=routes)
        self.assertEqual(patched['routing']['rules'][1]['user'], ['kenxu:a:uk'])
        self.assertEqual(patched['routing']['rules'][1]['outboundTag'], 'uk-gusecure2')
        self.assertEqual(patched['routing']['rules'][-1], original['routing']['rules'][0])
        self.assertEqual(meter.prepare_config(patched, clients, routes=routes)[0], patched)
        self.assertEqual(meter.prepare_config(patched, [clients[0]], routes=[routes[0]])[0]['routing']['rules'][-1], original['routing']['rules'][0])

    def test_zero_exit_with_no_added_users_is_not_success(self):
        with self.assertRaises(RuntimeError):
            meter.validate_added('processing inbound: test\nfailed to build config: missing settings\nAdded 0 user(s) in total.', 2)
        meter.validate_added('Added 2 user(s) in total.', 2)


if __name__ == '__main__':
    unittest.main()
