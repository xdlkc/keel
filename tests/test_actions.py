import unittest
import unittest.mock
from unittest.mock import Mock

from keel.actions import InstanceActions
from keel.config import Config
from keel.errors import ConfirmationError


class InstanceActionsTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(project="project-a", zone="zone-a", instance="vm-a")
        self.cloud = Mock()
        self.actions = InstanceActions(self.config, self.cloud)

    def test_start_requires_exact_instance_confirmation(self):
        for confirmation in (None, "", "vm-b", "VM-A"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaises(ConfirmationError):
                    self.actions.start(confirmation)
        self.cloud.start.assert_not_called()

    def test_stop_requires_exact_instance_confirmation(self):
        with self.assertRaises(ConfirmationError):
            self.actions.stop("vm-b")
        self.cloud.stop.assert_not_called()

    def test_confirmed_actions_delegate_to_gcloud(self):
        self.actions.start("vm-a")
        self.actions.stop("vm-a")
        self.cloud.start.assert_called_once_with(self.config)
        self.cloud.stop.assert_called_once_with(self.config)

    def test_cloud_failure_is_not_replaced_with_success(self):
        self.cloud.start.side_effect = RuntimeError("failed")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            self.actions.start("vm-a")


if __name__ == "__main__":
    unittest.main()
