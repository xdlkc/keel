import importlib.util
import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from keel.config import Config


SPEC = importlib.util.spec_from_file_location("keel_cli", Path(__file__).parent.parent / "keel.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(project="project-a", zone="zone-a", instance="vm-a")
        self.cloud = Mock()

    def test_default_port_and_required_confirmation(self):
        args = cli.build_parser().parse_args(["serve"])
        self.assertEqual(args.port, 8765)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["start"])

    def test_invalid_config_is_rejected_before_any_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "vps.local.json"
            config_path.write_text(json.dumps({
                "project": "project-a", "zone": "zone-a", "instance": "--help",
            }), encoding="utf-8")
            with patch("keel.config.default_config_path", return_value=config_path):
                with patch("keel.gcloud.subprocess.run") as run:
                    with redirect_stderr(io.StringIO()):
                        result = cli.main(["status"])
        self.assertEqual(result, 1)
        run.assert_not_called()

    def test_wrong_cli_confirmation_never_calls_cloud(self):
        with patch.object(cli, "load_config", return_value=self.config):
            with redirect_stderr(io.StringIO()):
                result = cli.main(
                    ["start", "--confirm", "vm-b"],
                    cloud_factory=Mock(return_value=self.cloud),
                )
        self.assertEqual(result, 1)
        self.cloud.start.assert_not_called()

    def test_cli_reports_command_completion_not_running_state(self):
        self.cloud.start.return_value = None
        stdout = io.StringIO()
        with patch.object(cli, "load_config", return_value=self.config):
            with redirect_stdout(stdout):
                result = cli.main(
                    ["start", "--confirm", "vm-a"],
                    cloud_factory=Mock(return_value=self.cloud),
                )
        self.assertEqual(result, 0)
        self.cloud.start.assert_called_once_with(self.config)
        self.assertIn("实际状态", stdout.getvalue())
        self.assertNotIn("已运行", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
