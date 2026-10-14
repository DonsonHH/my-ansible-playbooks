"""Design-contract replay only; no live Xray hook or deployment is installed."""
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'tools'))
from flow_contract import FlowReplay

def frame(seq, flows, total, unknown=(0, 0), epoch='epoch-a'):
    return {'schema': 1, 'epoch': epoch, 'seq': seq, 'flows': flows,
            'totals': [{'email': 'kenxu:alice:sg', 'up': total[0], 'down': total[1]}],
            'unknown': [{'email': 'kenxu:alice:sg', 'up': unknown[0], 'down': unknown[1]}]}

def flow(identifier='f1', up=10, down=90, host='api.example.com', closed=False):
    return {'id': identifier, 'email': 'kenxu:alice:sg', 'source': '192.0.2.1',
            'host': host, 'network': 'tcp', 'port': 443, 'up': up, 'down': down, 'closed': closed}

class FlowContractTests(unittest.TestCase):
    def test_retries_long_lived_connections_and_closed_short_connections(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG'})
        first = frame(1, [flow(), flow('short', 1, 9, closed=True)], (11, 99))
        self.assertEqual(r.apply(first)['total'], {'up': 11, 'down': 99})
        self.assertTrue(r.apply(first)['duplicate'])
        later = frame(2, [flow(up=20, down=180), flow('short', 1, 9, closed=True)], (21, 189))
        self.assertEqual(r.apply(later)['total'], {'up': 10, 'down': 90})
        self.assertEqual(r.totals['SG'], {'up': 21, 'down': 189})

    def test_two_sites_are_additive_and_unknown_is_never_spread(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG'})
        result = r.apply(frame(1, [flow(), flow('f2', 20, 180, 'another.example')], (33, 297), (3, 27)))
        self.assertEqual(result['classified'], {'up': 30, 'down': 270})
        self.assertEqual(result['unknown'], {'up': 3, 'down': 27})
        self.assertEqual(result['total'], {'up': 33, 'down': 297})

    def test_conflicting_retry_regression_or_mutable_flow_identity_is_atomic(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG'})
        r.apply(frame(1, [flow()], (10, 90)))
        for bad in [frame(1, [flow(down=91)], (10, 91)), frame(2, [flow(down=89)], (10, 89)), frame(2, [flow(host='swapped.example')], (10, 90)), frame(2, [flow(), flow()], (20, 180))]:
            with self.assertRaises(ValueError):
                r.apply(bad)
            self.assertEqual(r.totals['SG'], {'up': 10, 'down': 90})
        self.assertEqual(r.apply(frame(2, [flow(up=11, down=99)], (11, 99)))['total'], {'up': 1, 'down': 9})

    def test_restart_epoch_does_not_reset_history_or_allow_old_epoch_to_reopen(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG'})
        old = frame(1, [flow()], (10, 90))
        r.apply(old)
        r.apply(frame(1, [flow(up=2, down=8)], (2, 8), epoch='epoch-b'))
        self.assertEqual(r.totals['SG'], {'up': 12, 'down': 98})
        self.assertTrue(r.apply(old)['duplicate'])
        with self.assertRaises(ValueError):
            r.apply(frame(2, [flow(up=20, down=180)], (20, 180)))

    def test_route_ownership_and_reconciliation_must_match_without_fudging_bytes(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG', 'kenxu:alice:uk': 'UK'})
        sg, uk = flow(), flow('uk', 2, 8)
        uk['email'] = 'kenxu:alice:uk'
        f = frame(1, [sg, uk], (10, 90))
        f['totals'].append({'email': uk['email'], 'up': 2, 'down': 8})
        result = r.apply(f)
        self.assertEqual(result['routes']['SG'], {'up': 10, 'down': 90})
        self.assertEqual(result['routes']['UK'], {'up': 2, 'down': 8})
        for bad in [frame(2, [flow()], (10, 91)), frame(2, [flow(up=-1)], (-1, 90))]:
            with self.assertRaises(ValueError):
                r.apply(bad)
        foreign = frame(2, [flow()], (10, 90))
        foreign['flows'][0]['email'] = 'kenxu:foreign:node'
        with self.assertRaises(ValueError):
            r.apply(foreign)

    def test_missing_final_flow_and_counter_reset_never_pass_as_complete(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG'})
        r.apply(frame(1, [flow(closed=True)], (10, 90)))
        for bad in [frame(2, [], (10, 90)), frame(2, [flow(closed=False)], (10, 90)), frame(2, [flow(up=1, down=1, closed=True)], (1, 1)), frame(2, [flow(up=20, down=90, closed=True)], (20, 90))]:
            with self.assertRaises(ValueError):
                r.apply(bad)

    def test_metadata_is_host_ip_port_only_and_never_a_url(self):
        r = FlowReplay({'kenxu:alice:sg': 'SG'})
        for key, value in [('host','https://private.example/path?token=secret'),('source','not-an-ip'),('port',0),('network','https')]:
            bad = frame(1,[flow()],(10,90))
            bad['flows'][0][key] = value
            with self.assertRaises(ValueError):
                r.apply(bad)
        self.assertEqual(r.totals,{})

if __name__ == '__main__':
    unittest.main()
