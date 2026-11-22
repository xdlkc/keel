import json
import tempfile
import unittest
from pathlib import Path

from keel.config import Config, load_config
from keel.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "vps.local.json"
        self.addCleanup(self.temp_dir.cleanup)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_loads_exact_local_configuration(self):
        self.write({"project": "project-a", "zone": "zone-a", "instance": "vm-a"})
        self.assertEqual(
            load_config(self.path),
            Config(project="project-a", zone="zone-a", instance="vm-a"),
        )

    def test_accepts_realistic_gcp_identifiers_and_existing_mock_names(self):
        projects = ["project-a", "my-project-123", "example.com:project-a"]
        for project in projects:
            with self.subTest(project=project):
                self.write({"project": project, "zone": "zone-a", "instance": "vm-a"})
                self.assertEqual(load_config(self.path).project, project)
        self.write({"project": "project-a", "zone": "us-central1-a", "instance": "a"})
        config = load_config(self.path)
        self.assertEqual(config.zone, "us-central1-a")
        self.assertEqual(config.instance, "a")

    def test_rejects_option_like_and_malformed_identifiers(self):
        invalid_values = {
            "project": ["-project-a", "--project=other", "Project-a", "project_a", "ab"],
            "zone": ["-zone-a", "--zone=other", "Zone-a", "zone_a", "zone-a-"],
            "instance": ["-vm-a", "--help", "VM-A", "vm_a", "1vm-a", "vm-a-", "a" * 64],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = {"project": "project-a", "zone": "zone-a", "instance": "vm-a"}
                    data[field] = value
                    self.write(data)
                    with self.assertRaises(ConfigError):
                        load_config(self.path)

    def test_missing_file_has_actionable_error(self):
        with self.assertRaisesRegex(ConfigError, "vps.example.json"):
            load_config(self.path)

    def test_blank_required_fields_are_rejected(self):
        for field in ("project", "zone", "instance"):
            data = {"project": "project-a", "zone": "zone-a", "instance": "vm-a"}
            data[field] = "  "
            with self.subTest(field=field):
                self.write(data)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_unknown_fields_are_rejected(self):
        self.write({
            "project": "project-a",
            "zone": "zone-a",
            "instance": "vm-a",
            "token": "must-not-be-here",
        })
        with self.assertRaisesRegex(ConfigError, "不支持"):
            load_config(self.path)

    def test_invalid_json_is_rejected(self):
        self.path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_invalid_utf8_is_a_configuration_error(self):
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ConfigError, "UTF-8"):
            load_config(self.path)


if __name__ == "__main__":
    unittest.main()
