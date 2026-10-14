import copy
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'tools'))
from flow_preflight import describe


class FlowPreflightTests(unittest.TestCase):
    def test_only_sanitized_facts_and_no_config_mutation(self):
        config = {'api': {'listen':'127.0.0.1:10085'}, 'policy': {'levels': {'88': {'statsUserUplink':True,'statsUserDownlink':True}}}, 'inbounds':[{'settings':{'clients':[{'id':'private-uuid','email':'kenxu:private:uk','level':88,'flow':'xtls-rprx-vision'}]}}], 'outbounds':[{'tag':'uk-gusecure2','settings':{'password':'private-password'}}]}
        original = copy.deepcopy(config)
        value = describe(config, {'token':'private-token','activity_enabled':True})
        self.assertEqual(config, original)
        self.assertTrue(value['apiLoopback'] and value['ukRoutePresent'] and value['userByteCounters'])
        self.assertEqual(value['visionClients'],1)
        self.assertNotIn('private-', str(value))
        self.assertFalse(value['mutationsPerformed'] or value['flowRolloutEnabled'])

    def test_non_loopback_or_unprepared_core_is_not_declared_ready(self):
        value = describe({'api':{'listen':'0.0.0.0:10085'}}, {})
        self.assertFalse(value['apiLoopback'] or value['userByteCounters'])


if __name__ == '__main__':
    unittest.main()
