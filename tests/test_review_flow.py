"""Review gates operate on a real SQLite queue, never a live node."""
import pathlib
import sqlite3
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).parents[1]/'tools'))
sys.path.insert(0,str(pathlib.Path(__file__).parents[1]/'files'))
from review_flow import await_empty_queue,assert_desired_applied


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

    def test_live_counters_and_empty_queues_do_not_hide_a_stale_desired_revision(self):
        desired={'revision':'new','clients':[{'email':'fixture-user','id':'fixture-id','level':88,'flow':'xtls-rprx-vision'}]}
        runtime={'fixture-user':{'id':'fixture-id','level':88,'flow':'xtls-rprx-vision'}}
        with self.assertRaisesRegex(AssertionError,'revision is stale'):
            assert_desired_applied(desired,{'revision':'old'},runtime)
        self.assertEqual(assert_desired_applied(desired,{'revision':'new'},runtime),{'desiredRevisionCurrent':True,'managedRuntimeMatches':True,'desiredClients':1})

    def test_revision_alone_does_not_prove_the_expected_clients_are_running(self):
        desired={'revision':'same','clients':[{'email':'fixture-user','id':'fixture-id','level':88}]}
        with self.assertRaisesRegex(AssertionError,'runtime users differ'):
            assert_desired_applied(desired,{'revision':'same'},{})


if __name__=='__main__':
    unittest.main()
