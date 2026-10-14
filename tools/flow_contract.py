"""Offline executable design oracle, NOT an Xray hook or deployed collector.

Uses complete cumulative snapshots for small fixtures. Production ACK/compaction,
storage, time buckets and wire capture remain separate implementation gates.
"""
import copy
import hashlib
import json
import ipaddress
import re


def amount():
    return {'up': 0, 'down': 0}


def add(target, row):
    for key in ('up', 'down'):
        target[key] += row[key]


def checked(row):
    for key in ('up', 'down'):
        if type(row.get(key)) is not int or not 0 <= row[key] < 2**53:
            raise ValueError('Invalid unsigned byte count')
    return {'up': row['up'], 'down': row['down']}


class FlowReplay:
    def __init__(self, owners):
        self.owners = dict(owners)
        self.reports = {}
        self.epoch = None
        self.retired_epochs = set()
        self.last_seq = 0
        self.flows = {}
        self.unknown = {}
        self.totals = {}

    def apply(self, frame):
        if frame.get('schema') != 1 or type(frame.get('seq')) is not int or frame['seq'] < 1:
            raise ValueError('Invalid report identity')
        epoch, seq = frame.get('epoch'), frame['seq']
        if not isinstance(epoch, str) or not 1 <= len(epoch) <= 120:
            raise ValueError('Invalid process epoch')
        digest = hashlib.sha256(json.dumps(frame, sort_keys=True).encode()).hexdigest()
        previous = self.reports.get((epoch, seq))
        if previous:
            if previous != digest:
                raise ValueError('Conflicting retry')
            return {'duplicate': True, 'total': amount()}
        if epoch in self.retired_epochs or epoch == self.epoch and seq <= self.last_seq:
            raise ValueError('Out-of-order or retired epoch')
        # All validation happens in a private candidate. Invalid data never
        # advances sequences or changes a previously accepted byte total.
        old_flows = self.flows if epoch == self.epoch else {}
        old_unknown = self.unknown if epoch == self.epoch else {}
        current, classified, by_email, routes = {}, amount(), {}, {}
        for row in frame['flows']:
            identifier, email = row.get('id'), row.get('email')
            if not isinstance(identifier, str) or not identifier or identifier in current or email not in self.owners or type(row.get('closed')) is not bool:
                raise ValueError('Invalid flow ownership/identity')
            value = checked(row)
            try:
                ipaddress.ip_address(row['source'])
                host = row['host']
                if not isinstance(host, str) or not host or len(host) > 253:
                    raise ValueError()
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    if not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in host.split('.')):
                        raise ValueError()
                if type(row.get('port')) is not int or not 1 <= row['port'] <= 65535 or row.get('network') not in ('tcp', 'udp'):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ValueError('Invalid connection metadata') from None
            identity = tuple(row.get(k) for k in ('email', 'source', 'host', 'port', 'network'))
            old = old_flows.get(identifier)
            if old and (old['identity'] != identity or old['closed'] and (not row['closed'] or any(value[k] != old[k] for k in value)) or any(value[k] < old[k] for k in value)):
                raise ValueError('Mutable identity, reopening or regressed counter')
            delta = {k: value[k] - (old[k] if old else 0) for k in value}
            add(classified, delta)
            add(routes.setdefault(self.owners[email], amount()), delta)
            add(by_email.setdefault(email, amount()), value)
            current[identifier] = {**value, 'identity': identity, 'closed': row['closed']}
        if set(old_flows) - set(current):
            raise ValueError('A missing final flow cannot silently disappear')
        totals = {}
        for row in frame['totals']:
            email = row['email']
            if email not in self.owners or email in totals:
                raise ValueError('Unknown or duplicate total owner')
            totals[email] = checked(row)
        unknown, unknown_delta = {}, amount()
        for row in frame['unknown']:
            email = row['email']
            if email not in totals or email in unknown:
                raise ValueError('Invalid unknown owner')
            value = checked(row)
            old = old_unknown.get(email, amount())
            if any(value[k] < old[k] for k in value):
                raise ValueError('Regressed unknown counter')
            delta = {k: value[k] - old[k] for k in value}
            add(unknown_delta, delta)
            add(routes.setdefault(self.owners[email], amount()), delta)
            unknown[email] = value
        if set(old_unknown) - set(unknown) or set(by_email) - set(totals):
            raise ValueError('Incomplete snapshot')
        for email, row in totals.items():
            if any(row[k] != by_email.get(email, amount())[k] + unknown.get(email, amount())[k] for k in row):
                raise ValueError('Reconciliation mismatch; never rescale counters')
        new_totals = copy.deepcopy(self.totals)
        for route, value in routes.items():
            add(new_totals.setdefault(route, amount()), value)
        if self.epoch and epoch != self.epoch:
            self.retired_epochs.add(self.epoch)
        self.epoch, self.last_seq, self.flows, self.unknown, self.totals = epoch, seq, current, unknown, new_totals
        self.reports[(epoch, seq)] = digest
        total = amount()
        add(total, classified)
        add(total, unknown_delta)
        return {'duplicate': False, 'total': total, 'classified': classified, 'unknown': unknown_delta, 'routes': routes}
