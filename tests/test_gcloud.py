import io
import json
import os
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from keel.config import Config
from keel.errors import AuthError, GcloudError
from keel.gcloud import ACTION_TIMEOUT, Gcloud, instance_view


class GcloudTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(project="project-a", zone="zone-a", instance="vm-a")
        self.cloud = Gcloud()

    @patch("keel.gcloud.subprocess.run")
    def test_describe_uses_safe_argv_explicit_project_zone_and_timeout(self, run):
        run.return_value = Mock(returncode=0, stdout='{"id": "123", "status": "RUNNING"}')
        with patch.dict(os.environ, {}, clear=True):
            result = self.cloud.describe(self.config)
            self.assertNotIn("CLOUDSDK_CORE_DISABLE_PROMPTS", os.environ)
            self.assertNotIn("CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API", os.environ)
        self.assertEqual(result["id"], "123")
        argv = run.call_args.args[0]
        self.assertEqual(argv, [
            "gcloud", "compute", "instances", "describe", "vm-a",
            "--project", "project-a", "--zone", "zone-a", "--format", "json",
        ])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["CLOUDSDK_CORE_DISABLE_PROMPTS"], "true")
        self.assertEqual(child_env["CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API"], "false")

    @patch("keel.gcloud.subprocess.run")
    def test_start_and_stop_are_synchronous_and_explicit(self, run):
        run.return_value = Mock(returncode=0, stdout="")
        self.cloud.start(self.config)
        self.cloud.stop(self.config)
        start_argv = run.call_args_list[0].args[0]
        stop_argv = run.call_args_list[1].args[0]
        self.assertEqual(start_argv[1:4], ["compute", "instances", "start"])
        self.assertEqual(stop_argv[1:4], ["compute", "instances", "stop"])
        for argv in (start_argv, stop_argv):
            self.assertIn("--quiet", argv)
            self.assertNotIn("--async", argv)
            self.assertIn("--project", argv)
            self.assertIn("--zone", argv)
        self.assertTrue(all(call.kwargs["timeout"] <= ACTION_TIMEOUT for call in run.call_args_list))
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["env"]["CLOUDSDK_CORE_DISABLE_PROMPTS"], "true")
            self.assertEqual(call.kwargs["env"]["CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API"], "false")

    @patch("keel.gcloud.subprocess.run")
    def test_access_token_stays_in_return_value_and_error_is_sanitized(self, run):
        run.return_value = Mock(returncode=0, stdout="very-secret-token\n")
        self.assertEqual(self.cloud.access_token(), "very-secret-token")
        self.assertEqual(run.call_args.args[0], ["gcloud", "auth", "print-access-token"])

        run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="very-secret-token and raw cloud details",
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(AuthError) as caught:
                self.cloud.access_token()
        self.assertNotIn("very-secret-token", str(caught.exception))
        self.assertNotIn("very-secret-token", stderr.getvalue())

    @patch("keel.gcloud.subprocess.run")
    def test_failed_write_command_raises_instead_of_returning_success(self, run):
        run.return_value = Mock(returncode=1, stdout="", stderr="raw error")
        with self.assertRaises(GcloudError):
            self.cloud.stop(self.config)

    def test_instance_view_extracts_only_describe_data(self):
        view = instance_view(self.config, {
            "id": 123,
            "name": "vm-a",
            "status": "RUNNING",
            "machineType": "https://compute.googleapis.com/compute/v1/projects/project-a/zones/zone-a/machineTypes/e2-small",
            "disks": [
                {"boot": False, "diskSizeGb": "100"},
                {"boot": True, "diskSizeGb": "20"},
            ],
            "networkInterfaces": [{"accessConfigs": [{"natIP": "192.0.2.10"}]}],
        })
        self.assertEqual(view["id"], "123")
        self.assertEqual(view["machineType"], "e2-small")
        self.assertEqual(view["bootDiskSizeGb"], "20")
        self.assertEqual(view["publicIp"], "192.0.2.10")


if __name__ == "__main__":
    unittest.main()
