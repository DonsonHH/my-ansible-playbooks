import datetime
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'files'))
from kenxu_activity import parse_access, access_batch, online_sources


class ActivityTests(unittest.TestCase):
    def test_only_authenticated_metadata_and_no_url_paths(self):
        now = int(time.time()*1000)
        stamp = datetime.datetime.fromtimestamp(now/1000).strftime('%Y/%m/%d %H:%M:%S.%f')
        line = stamp + ' from tcp:[2001:db8::1]:12345 accepted tcp:chatgpt.com:443 [in -> out] email: kenxu:alice:node\n'
        parsed = parse_access(line, now)
        self.assertEqual(parsed['ip'], '2001:db8::1')
        self.assertEqual(parsed['target'], {'host': 'chatgpt.com', 'port': 443, 'network': 'tcp'})
        self.assertIsNone(parse_access(line.replace('kenxu:alice:node', 'private'), now))
        self.assertIsNone(parse_access(line.replace('chatgpt.com', 'https://private.example/path?token=secret'), now))
        self.assertIsNone(parse_access(line, now+3600000))

    def test_cursor_transaction_retry_rotation_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(':memory:')
            file = pathlib.Path(directory)/'access.log'
            now = int(time.time()*1000)
            stamp = datetime.datetime.fromtimestamp(now/1000).strftime('%Y/%m/%d %H:%M:%S.%f')
            line = stamp + ' from 192.0.2.1:1234 accepted tcp:example.com:443 [in -> out] email: kenxu:alice:node\n'
            file.write_text(line)
            with db:
                first = access_batch(db,str(file),now)
            self.assertEqual(first['kenxu:alice:node']['connections'],1)
            self.assertEqual(access_batch(db,str(file),now),{})
            file.write_text(line+line[:-1])
            with db:
                self.assertEqual(access_batch(db,str(file),now),{})
            with file.open('a') as stream:
                stream.write('\n')
            with db:
                self.assertEqual(access_batch(db,str(file),now)['kenxu:alice:node']['connections'],1)
            file.rename(pathlib.Path(directory)/'old.log')
            file.write_text(line)
            with db:
                self.assertEqual(access_batch(db,str(file),now)['kenxu:alice:node']['connections'],1)
            with file.open('a') as stream:
                stream.write('x' * 20000 + '\n' + line)
            with db:
                self.assertEqual(access_batch(db,str(file),now)['kenxu:alice:node']['connections'],1)
            db.close()

    def test_online_ips_are_valid_and_recent_first(self):
        self.assertEqual(online_sources({'ips': {'192.0.2.1':10,'2001:db8::1':20,'bad':30}}),['2001:db8::1','192.0.2.1'])
        self.assertEqual(online_sources({}),[])


if __name__ == '__main__':
    unittest.main()
