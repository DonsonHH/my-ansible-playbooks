import pathlib
import sqlite3
import sys
import time
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).parents[1]/'files'))
from kenxu_flow import FlowCollector


class FlowCollectorTests(unittest.TestCase):
    def test_queue_commits_before_ack_and_retry_does_not_lose_bytes(self):
        db=sqlite3.connect(':memory:');saved=[];acks=[];now=int(time.time()*1000)
        snap={'schema':1,'epoch':'fixture','seq':1,'at':now,'scope':'all-managed','users':[],'records':[{'id':'one','email':'kenxu:fixture:a','at':now,'host':'example.test','port':443,'network':'tcp','source':'192.0.2.1','up':10,'down':90,'quality':'actual'}]}
        def observe(method,path,body=None):
            if method=='GET':
                return snap
            self.assertEqual(db.execute('SELECT count(*) FROM flow_queue').fetchone()[0],1)
            acks.append(body)
            return {'ok':True}
        def request(path,body):
            if path.endswith('heartbeat'):
                return {'ok':True}
            saved.append(body)
            return {'ok':True}
        collector=FlowCollector(db,{},request,observe)
        collector.collect();collector.collect();self.assertEqual(len(acks),2);self.assertEqual(db.execute('SELECT count(*) FROM flow_queue').fetchone()[0],1)
        collector.flush();self.assertEqual(len(saved),1);self.assertEqual(db.execute('SELECT count(*) FROM flow_queue').fetchone()[0],0);db.close()

    def test_expired_metadata_redacts_even_while_portal_is_offline(self):
        db=sqlite3.connect(':memory:')
        def offline(path,body):
            raise RuntimeError('Offline')
        collector=FlowCollector(db,{},offline)
        old=int(time.time()*1000)-31*86400000
        import json
        payload={'schema':1,'epoch':'old','seq':1,'at':old,'records':[{'id':'one','email':'kenxu:fixture:a','at':old,'host':'private.example','port':443,'source':'192.0.2.1','up':1,'down':9,'quality':'actual'}]}
        db.execute('INSERT INTO flow_queue VALUES(?,?,?)',('old',1,json.dumps(payload)));db.commit()
        with self.assertRaises(RuntimeError):
            collector.flush()
        raw=db.execute('SELECT payload FROM flow_queue').fetchone()[0];self.assertNotIn('private.example',raw);self.assertNotIn('192.0.2.1',raw);self.assertIn('unknown-expired',raw);db.close()

    def test_portal_outage_does_not_prevent_durable_local_capture_and_core_ack(self):
        db=sqlite3.connect(':memory:');acks=[];now=int(time.time()*1000)
        snapshot={'schema':1,'epoch':'offline','seq':1,'at':now,'scope':'all-managed','users':[],'records':[{'id':'queued','email':'kenxu:fixture:a','at':now,'host':'example.test','port':443,'network':'tcp','source':'192.0.2.1','up':1,'down':9,'quality':'actual'}]}
        def observer(method,path,body=None):
            if method=='GET':
                return snapshot if not acks else {**snapshot,'records':[],'seq':0}
            acks.append(body)
            return {'ok':True}
        def offline(path,body):
            raise RuntimeError('Portal unreachable')
        collector=FlowCollector(db,{},offline,observer)
        with self.assertRaises(RuntimeError):
            collector.cycle()
        self.assertEqual(len(acks),1);self.assertEqual(db.execute('SELECT count(*) FROM flow_queue').fetchone()[0],1);db.close()

    def test_offline_retention_covers_the_whole_queue_not_only_first_upload(self):
        import json
        db=sqlite3.connect(':memory:')
        def offline(path,body):
            raise RuntimeError('Offline')
        collector=FlowCollector(db,{},offline)
        old=int(time.time()*1000)-31*86400000
        for seq in range(105):
            payload={'schema':1,'epoch':'old','seq':seq+1,'at':old,'records':[{'id':'one','email':'kenxu:fixture:a','at':old,'host':'private.example','source':'192.0.2.1','up':1,'down':9,'quality':'actual'}]}
            db.execute('INSERT INTO flow_queue VALUES(?,?,?)',('old',seq+1,json.dumps(payload)))
        db.commit()
        with self.assertRaises(RuntimeError):
            collector.flush()
        self.assertEqual(db.execute("SELECT count(*) FROM flow_queue WHERE instr(payload,'private.example')>0 OR instr(payload,'192.0.2.1')>0").fetchone()[0],0)
        self.assertEqual(db.execute('SELECT count(*) FROM flow_queue').fetchone()[0],105)
        db.close()

    def test_graceful_tail_is_durable_and_flushes_even_when_core_stays_down(self):
        import tempfile,json
        now=int(time.time()*1000)
        with tempfile.TemporaryDirectory() as directory:
            spool=pathlib.Path(directory)/'spool';spool.mkdir()
            payload={'schema':1,'epoch':'shutdown','seq':2,'at':now,'records':[{'id':'tail','email':'kenxu:fixture:a','at':now,'host':'example.test','port':443,'network':'tcp','source':'192.0.2.1','up':2,'down':8,'quality':'actual'}]}
            file=spool/'shutdown-2.json';file.write_text(json.dumps(payload));db=sqlite3.connect(':memory:');uploaded=[]
            def observe(*args):
                raise ConnectionError('Core stopped')
            def request(path,body):
                uploaded.append(body)
                return {'ok':True}
            collector=FlowCollector(db,{'flow_socket':str(pathlib.Path(directory)/'flow.sock')},request,observe)
            with self.assertRaises(ConnectionError):
                collector.cycle()
            self.assertFalse(file.exists());self.assertEqual(len(uploaded),1);self.assertEqual(uploaded[0]['records'][0]['down'],8)
            self.assertEqual(db.execute('SELECT count(*) FROM flow_queue').fetchone()[0],0);db.close()

    def test_checkpoint_replay_keeps_numeric_sequence_order(self):
        import tempfile,json
        now=int(time.time()*1000)
        with tempfile.TemporaryDirectory() as directory:
            spool=pathlib.Path(directory)/'spool';spool.mkdir();db=sqlite3.connect(':memory:');uploaded=[]
            for seq in [10,2,1]:
                payload={'schema':1,'epoch':'shutdown','seq':seq,'at':now,'records':[{'id':'tail','email':'kenxu:fixture:a','at':now,'host':'example.test','port':443,'network':'tcp','source':'192.0.2.1','up':seq,'down':0,'quality':'actual'}]}
                (spool/('shutdown-'+str(seq)+'.json')).write_text(json.dumps(payload))
            def request(path,body):
                uploaded.append(body['seq']);return {'ok':True}
            collector=FlowCollector(db,{'flow_socket':str(pathlib.Path(directory)/'flow.sock')},request);collector.replay();collector.flush();self.assertEqual(uploaded,[1,2,10]);db.close()


if __name__=='__main__':
    unittest.main()
