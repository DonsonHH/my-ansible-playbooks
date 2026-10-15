"""Opt-in per-flow observer transport; keeps original user metering independent."""
import http.client
import json
import socket
import time
import pathlib


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(self.path)


class FlowCollector:
    def __init__(self, db, settings, request, observe=None):
        self.db, self.settings, self.request = db, settings, request
        self.observe = observe or self.local
        self.last_heartbeat = 0
        self.latest = None
        self.db.execute('CREATE TABLE IF NOT EXISTS flow_queue(epoch TEXT NOT NULL,seq INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(epoch,seq))')

    def local(self, method, path, body=None):
        connection = UnixHTTP(self.settings.get('flow_socket', '/var/lib/kenxu-flow/observer.sock'))
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers={'Content-Type':'application/json'})
            response = connection.getresponse()
            raw = response.read(2097153)
            if response.status != 200 or len(raw) > 2097152:
                raise ValueError('Invalid observer response')
            return json.loads(raw)
        finally:
            connection.close()

    @staticmethod
    def accounting(payload):
        return (payload['epoch'], payload['seq'], payload['at'], [(r['id'],r['email'],r['at'],r['up'],r['down']) for r in payload['records']])

    def collect(self):
        snapshot = self.observe('GET','/snapshot')
        self.latest = snapshot
        if not snapshot.get('records'):
            return False
        self.persist(snapshot)
        self.observe('POST','/ack',{'epoch':snapshot['epoch'],'seq':snapshot['seq']})
        return True

    def persist(self, snapshot):
        if snapshot.get('schema') != 1 or not isinstance(snapshot.get('epoch'),str) or not isinstance(snapshot.get('records'),list) or len(snapshot['records']) > 1000:
            raise ValueError('Invalid observer snapshot')
        if not snapshot['records']:
            return
        payload = {k:snapshot[k] for k in ('schema','epoch','seq','at','records')}
        if len(json.dumps(payload).encode()) > 524288:
            raise ValueError('Oversized observer batch')
        with self.db:
            row = self.db.execute('SELECT payload FROM flow_queue WHERE epoch=? AND seq=?',(payload['epoch'],payload['seq'])).fetchone()
            if row and self.accounting(json.loads(row[0])) != self.accounting(payload):
                raise ValueError('Conflicting local observer snapshot')
            if not row:
                self.db.execute('INSERT INTO flow_queue VALUES(?,?,?)',(payload['epoch'],payload['seq'],json.dumps(payload)))
        # Only the committed queue owns the bytes before acknowledging the core.

    def replay(self):
        socket_path=pathlib.Path(self.settings.get('flow_socket','/var/lib/kenxu-flow/observer.sock'))
        files=list((socket_path.parent/'spool').glob('*.json'))
        # Files are epoch-sequence; lexical order would send 10 before 2 and
        # poison the server's strict same-epoch delivery order after a restart.
        def order(file):
            epoch,sequence=file.stem.rsplit('-',1)
            return epoch,int(sequence)
        for file in sorted(files,key=order):
            if file.is_symlink() or not file.is_file() or file.stat().st_size>524288:
                raise ValueError('Invalid observer checkpoint')
            self.persist(json.loads(file.read_text()))
            file.unlink()

    def prune(self):
        cutoff = int(time.time()*1000)-30*86400000
        expired = self.db.execute("SELECT epoch,seq,payload FROM flow_queue WHERE EXISTS (SELECT 1 FROM json_each(payload,'$.records') WHERE json_extract(value,'$.at')<? AND (coalesce(json_extract(value,'$.host'),'')<>'' OR coalesce(json_extract(value,'$.source'),'')<>''))",(cutoff,))
        for epoch,seq,raw in expired:
            payload = json.loads(raw)
            for row in payload['records']:
                if row['at'] < cutoff:
                    row.update(host='',source='',port=0,quality='unknown-expired')
            with self.db:
                self.db.execute('UPDATE flow_queue SET payload=? WHERE epoch=? AND seq=?',(json.dumps(payload),epoch,seq))

    def flush(self):
        # Redact the complete backlog even if the first upload fails offline.
        self.prune()
        for epoch,seq,raw in self.db.execute('SELECT epoch,seq,payload FROM flow_queue ORDER BY rowid LIMIT 100').fetchall():
            payload = json.loads(raw)
            result = self.request('/api/meter/flows',payload)
            if not result.get('ok'):
                raise RuntimeError('Observer report rejected')
            with self.db:
                self.db.execute('DELETE FROM flow_queue WHERE epoch=? AND seq=?',(epoch,seq))

    def cycle(self, batches=16):
        self.replay()
        self.prune()
        failure=None
        try:
            for _ in range(batches):
                if not self.collect():
                    break
        except Exception as error:
            failure=error
        self.flush()
        if failure:
            raise failure
        if self.latest and (not self.last_heartbeat or time.monotonic()-self.last_heartbeat > 30):
            snapshot = self.latest
            backlog = self.db.execute('SELECT count(*) FROM flow_queue').fetchone()[0] + int(bool(snapshot.get('more')))
            self.request('/api/meter/flows/heartbeat', {'schema':1,'epoch':snapshot['epoch'],'at':int(time.time()*1000),'scope':snapshot['scope'],'users':snapshot['users'],'backlog':backlog})
            self.last_heartbeat = time.monotonic()
