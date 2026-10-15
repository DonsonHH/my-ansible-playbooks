"""Review gates operate on a real SQLite queue, never a live node."""
import pathlib
import sqlite3
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).parents[1]/'tools'))
sys.path.insert(0,str(pathlib.Path(__file__).parents[1]/'files'))
from review_flow import await_empty_queue


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.database=sqlite3.connect(':memory:')
        self.database.executescript('CREATE TABLE queue(id INTEGER);CREATE TABLE flow_queue(id INTEGER);')

    def tearDown(self):
        self.database.close()

    def test_native_or_flow_backlog_blocks_promotion_without_deleting_data(self):
        for table in ['queue','flow_queue']:
            self.database.execute('INSERT INTO '+table+' VALUES(1)');self.database.commit()
            with self.assertRaisesRegex(AssertionError,'backlog'):
                await_empty_queue(self.database,timeout=0)
            self.assertEqual(self.database.execute('SELECT count(*) FROM '+table).fetchone()[0],1)
            self.database.execute('DELETE FROM '+table);self.database.commit()

    def test_a_normal_inflight_upload_can_drain_within_the_bounded_wait(self):
        self.database.execute('INSERT INTO flow_queue VALUES(1)');self.database.commit();waits=[]
        def flush(_):
            waits.append(True);self.database.execute('DELETE FROM flow_queue');self.database.commit()
        self.assertEqual(await_empty_queue(self.database,timeout=.1,sleep=flush),{'queue':0,'flow_queue':0})
        self.assertEqual(len(waits),1)


if __name__=='__main__':
    unittest.main()
