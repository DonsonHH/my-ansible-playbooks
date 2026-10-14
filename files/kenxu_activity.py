"""Bounded Xray metadata parsing; never inspect or log application payloads."""
import datetime
import ipaddress
import os
import re
import time

ACCESS = re.compile(r'^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\s+(?:from\s+)?(\S+)\s+accepted\s+((?:tcp|udp):\S+).*?\bemail:\s*(kenxu:[A-Za-z0-9:._-]{1,145})(?:\s|$)')


def endpoint(value):
    network = 'tcp'
    if value.startswith(('tcp:', 'udp:')):
        network, value = value.split(':', 1)
    host, port = value.rsplit(':', 1)
    host = host.strip('[]')
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    return network, host, port


def parse_access(line, now):
    match = ACCESS.search(line)
    if not match:
        return None
    try:
        stamp, source, target, email = match.groups()
        parsed = datetime.datetime.strptime(stamp, '%Y/%m/%d %H:%M:%S.%f' if '.' in stamp else '%Y/%m/%d %H:%M:%S')
        at = int(parsed.timestamp() * 1000)
        if at < now - 300000 or at > now + 300000:
            return None
        _, ip, _ = endpoint(source)
        ip = str(ipaddress.ip_address(ip))
        network, host, port = endpoint(target)
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = host.rstrip('.').encode('idna').decode('ascii').lower()
            if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
                return None
        return {'email': email, 'ip': ip, 'target': {'host': host, 'port': port, 'network': network}, 'at': at}
    except (ValueError, UnicodeError, OverflowError):
        return None


def access_batch(db, filename, now):
    """Advance the file cursor in the caller's same queue transaction."""
    db.execute('CREATE TABLE IF NOT EXISTS activity_cursor(path TEXT PRIMARY KEY,inode INTEGER NOT NULL,offset INTEGER NOT NULL)')
    result = {}
    try:
        with open(filename, 'rb') as stream:
            stat = os.fstat(stream.fileno())
            saved = db.execute('SELECT inode,offset FROM activity_cursor WHERE path=?', (filename,)).fetchone()
            stream.seek(saved[1] if saved and saved[0] == stat.st_ino and saved[1] <= stat.st_size else 0)
            start = stream.tell()
            while stream.tell() - start < 1048576:
                position = stream.tell()
                line = stream.readline(8192)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if len(line) == 8192:
                        # An oversized/hostile line must not pin the durable
                        # cursor forever. Skip bounded chunks; fragments never
                        # match ACCESS because they lack a timestamp prefix.
                        continue
                    stream.seek(position)
                    break
                event = parse_access(line.decode('utf8', 'replace'), now)
                if not event:
                    continue
                row = result.setdefault(event['email'], {'email': event['email'], 'sources': [], 'targets': [], 'connections': 0})
                if event['ip'] in row['sources']:
                    row['sources'].remove(event['ip'])
                row['sources'].insert(0, event['ip'])
                row['sources'] = row['sources'][:16]
                if event['target'] not in row['targets'] and len(row['targets']) < 40:
                    row['targets'].append(event['target'])
                row['connections'] = min(100000, row['connections'] + 1)
            db.execute('INSERT INTO activity_cursor VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET inode=excluded.inode,offset=excluded.offset', (filename, stat.st_ino, stream.tell()))
    except FileNotFoundError:
        pass
    return result


def online_sources(response):
    ips = response.get('ips', {})
    if not isinstance(ips, dict):
        return []
    result = []
    for value in sorted(ips, key=lambda key: int(ips[key]), reverse=True):
        try:
            result.append(str(ipaddress.ip_address(value)))
        except ValueError:
            continue
    return result[:16]
