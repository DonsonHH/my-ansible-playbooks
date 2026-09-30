import importlib.util
import pathlib
import unittest
import sys
from unittest.mock import MagicMock

if sys.platform == 'win32':
    sys.modules['fcntl'] = MagicMock()

spec = importlib.util.spec_from_file_location('kenxu_meter', pathlib.Path(__file__).parents[1] / 'files' / 'kenxu_meter.py')
meter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meter)


class MeterTests(unittest.TestCase):
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

    def test_counters_ignore_machine_and_unmanaged_users(self):
        result = meter.parse_counters({'stat': [{'name': 'user>>>kenxu:a:n>>>traffic>>>uplink', 'value': '120'}, {'name': 'user>>>kenxu:a:n>>>traffic>>>downlink', 'value': 300}, {'name': 'user>>>private>>>traffic>>>uplink', 'value': 1000}, {'name': 'inbound>>>all>>>traffic>>>downlink', 'value': 9000}]})
        self.assertEqual(result, [{'email': 'kenxu:a:n', 'up': 120, 'down': 300}])


if __name__ == '__main__':
    unittest.main()
