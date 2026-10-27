import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'tools'))
from meter_service import activity_log_directory, flow_meter_dropin


class MeterServiceTests(unittest.TestCase):
    def test_active_logging_preserves_flow_exception_and_adds_only_private_log_directory(self):
        self.assertEqual(activity_log_directory({'activity_enabled': True}), '/var/log/kenxu')
        content = flow_meter_dropin({'activity_enabled': True, 'access_log': '/var/log/kenxu/access.log'})
        self.assertEqual(content, '[Service]\nReadWritePaths=/var/lib/kenxu-flow /var/log/kenxu\n')
        self.assertNotIn('ProtectSystem=false', content)
        self.assertNotIn('ReadWritePaths=/var/log\n', content)

    def test_disabled_logging_does_not_gain_unneeded_log_write_permission(self):
        self.assertIsNone(activity_log_directory({}))
        self.assertEqual(flow_meter_dropin({'activity_enabled': False}), '[Service]\nReadWritePaths=/var/lib/kenxu-flow\n')

    def test_unreviewed_or_parent_traversing_log_destinations_cannot_expand_the_sandbox(self):
        for file in ['/var/log/access.log', '/etc/shadow', '/var/log/kenxu/../access.log', 'access.log', None]:
            with self.assertRaises(ValueError):
                flow_meter_dropin({'activity_enabled': True, 'access_log': file})


if __name__ == '__main__':
    unittest.main()
