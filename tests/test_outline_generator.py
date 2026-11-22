import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
GENERATOR_PATH = ROOT / "deploy" / "outline" / "generate.py"
SPEC = importlib.util.spec_from_file_location("outline_generator", GENERATOR_PATH)
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class OutlineGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.inventory_path = self.root / "inventory.local.json"
        self.inventory = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
                {"id": "bob-laptop", "label": "Bob Laptop", "secret": "bob-test-secret"},
            ],
        }
        self.write_inventory(self.inventory)

    def write_inventory(self, value):
        self.inventory_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.inventory_path, 0o600)

    def generate(self, output_name="generated"):
        output = self.root / output_name
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = generator.main([
                "--inventory", str(self.inventory_path),
                "--output", str(output),
            ])
        return result, output, stdout.getvalue(), stderr.getvalue()

    def test_generates_deterministic_private_outline_and_shared_clash_configs(self):
        result, first, stdout, stderr = self.generate("first")
        self.assertEqual(result, 0, stderr)
        second_result, second, _, second_stderr = self.generate("second")
        self.assertEqual(second_result, 0, second_stderr)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
        self.assertEqual({path.name for path in first.iterdir()}, {"outline.yml", "clash.yaml"})
        for name in ("outline.yml", "clash.yaml"):
            self.assertEqual(stat.S_IMODE((first / name).stat().st_mode), 0o600)
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        combined_log = stdout + stderr
        for device in self.inventory["devices"]:
            self.assertNotIn(device["secret"], combined_log)

        outline = json.loads((first / "outline.yml").read_text(encoding="utf-8"))
        self.assertEqual(list(outline), ["services"])
        self.assertEqual(len(outline["services"]), 1)
        service = outline["services"][0]
        self.assertEqual(service["listeners"], [
            {"type": "tcp", "address": ":443"},
            {"type": "udp", "address": ":443"},
        ])
        self.assertEqual(service["keys"], [
            {"id": "alice-phone", "cipher": "chacha20-ietf-poly1305", "secret": "alice-test-secret"},
            {"id": "bob-laptop", "cipher": "chacha20-ietf-poly1305", "secret": "bob-test-secret"},
        ])

        clash = json.loads((first / "clash.yaml").read_text(encoding="utf-8"))
        self.assertFalse(clash["allow-lan"])
        self.assertEqual(clash["profile"], {"store-selected": True})
        self.assertEqual(len(clash["proxy-groups"]), 1)
        group = clash["proxy-groups"][0]
        self.assertEqual(group["type"], "select")
        self.assertNotIn(group["type"], {"url-test", "fallback", "load-balance"})
        self.assertEqual(group["proxies"], ["Alice Phone", "Bob Laptop"])
        self.assertEqual(clash["rules"], [
            "IP-CIDR,203.0.113.10/32,DIRECT,no-resolve",
            "MATCH,Keel Outline",
        ])
        self.assertEqual(group["name"], "Keel Outline")
        self.assertEqual(len(clash["proxies"]), 2)
        for proxy, device in zip(clash["proxies"], self.inventory["devices"]):
            self.assertEqual(proxy, {
                "name": device["label"],
                "type": "ss",
                "server": "203.0.113.10",
                "port": 443,
                "cipher": "chacha20-ietf-poly1305",
                "password": device["secret"],
                "udp": True,
            })
        self.assertEqual(len({proxy["password"] for proxy in clash["proxies"]}), 2)

    def test_domain_and_ipv6_server_bypass_rules(self):
        cases = [
            ("outline.example.com", "DOMAIN,outline.example.com,DIRECT"),
            ("2001:db8::10", "IP-CIDR6,2001:db8::10/128,DIRECT,no-resolve"),
        ]
        for index, (host, expected_rule) in enumerate(cases):
            with self.subTest(host=host):
                self.inventory["host"] = host
                self.write_inventory(self.inventory)
                result, output, _, stderr = self.generate("host-{}".format(index))
                self.assertEqual(result, 0, stderr)
                clash = json.loads((output / "clash.yaml").read_text(encoding="utf-8"))
                self.assertEqual(clash["rules"][0], expected_rule)
                self.assertTrue(all(proxy["server"] == host for proxy in clash["proxies"]))

    def test_invalid_or_world_readable_inventory_is_quiet_and_writes_nothing(self):
        invalid_values = [
            {"host": "https://outline.example.com", "port": 443, "devices": self.inventory["devices"]},
            {"host": "203.0.113.10", "port": True, "devices": self.inventory["devices"]},
            {"host": "203.0.113.10", "port": 443, "devices": []},
            {"host": "203.0.113.10", "port": 443, "devices": [
                {"id": "Duplicate", "label": "Phone", "secret": "do-not-print-this"},
            ]},
            {"host": "203.0.113.10", "port": 443, "devices": [
                {"id": "phone", "label": "Phone", "secret": "do-not-print-this"},
                {"id": "laptop", "label": "Laptop", "secret": "do-not-print-this"},
            ]},
        ]
        for index, value in enumerate(invalid_values):
            with self.subTest(index=index):
                self.write_inventory(value)
                result, output, stdout, stderr = self.generate("invalid-{}".format(index))
                self.assertEqual(result, 1)
                self.assertFalse(output.exists())
                self.assertNotIn("do-not-print-this", stdout + stderr)

        self.write_inventory(self.inventory)
        os.chmod(self.inventory_path, 0o644)
        result, output, stdout, stderr = self.generate("permissions")
        self.assertEqual(result, 1)
        self.assertIn("权限", stderr)
        self.assertFalse(output.exists())
        for device in self.inventory["devices"]:
            self.assertNotIn(device["secret"], stdout + stderr)

    def test_reserved_mihomo_labels_are_rejected_without_output(self):
        reserved_labels = (
            "Keel Outline", "keel outline", "DIRECT", "direct", "REJECT",
            "REJECT-DROP", "PASS", "PASS-RULE", "GLOBAL", "COMPATIBLE",
        )
        for index, label in enumerate(reserved_labels):
            with self.subTest(label=label):
                self.inventory["devices"][0]["label"] = label
                self.write_inventory(self.inventory)
                result, output, _, stderr = self.generate("reserved-{}".format(index))
                self.assertEqual(result, 1)
                self.assertIn("保留", stderr)
                self.assertFalse(output.exists())

    def test_inventory_output_collision_is_rejected_without_replacing_inventory(self):
        for name in ("outline.yml", "clash.yaml"):
            with self.subTest(name=name):
                output_name = "collision-" + name
                output = self.root / output_name
                output.mkdir(mode=0o700)
                self.inventory_path = output / name
                self.write_inventory(self.inventory)
                original = self.inventory_path.read_bytes()

                result, _, _, stderr = self.generate(output_name)

                self.assertEqual(result, 1)
                self.assertIn("清单", stderr)
                self.assertEqual(self.inventory_path.read_bytes(), original)
                self.assertEqual(
                    {path.name for path in output.iterdir()},
                    {name},
                )

    def test_existing_non_private_output_dir_is_rejected_without_chmod(self):
        output = self.root / "existing"
        output.mkdir()
        os.chmod(output, 0o755)
        (output / "outline.yml").write_text("original", encoding="utf-8")

        result, _, _, stderr = self.generate("existing")

        self.assertEqual(result, 1)
        self.assertIn("权限", stderr)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
        self.assertEqual((output / "outline.yml").read_text(encoding="utf-8"), "original")
        self.assertFalse((output / "clash.yaml").exists())

    def test_serialization_failure_preserves_existing_outputs(self):
        output = self.root / "existing"
        output.mkdir(mode=0o700)
        for name in ("outline.yml", "clash.yaml"):
            (output / name).write_text("original " + name, encoding="utf-8")

        with mock.patch.object(
            generator, "_json_yaml", side_effect=["new outline\n", RuntimeError("injected")]
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                generator.generate(self.inventory_path, output)

        for name in ("outline.yml", "clash.yaml"):
            self.assertEqual(
                (output / name).read_text(encoding="utf-8"),
                "original " + name,
            )

    def test_output_preflight_failure_preserves_existing_outputs(self):
        output = self.root / "existing"
        output.mkdir(mode=0o700)
        (output / "outline.yml").write_text("original outline", encoding="utf-8")
        (output / "clash.yaml").mkdir()

        result, _, _, stderr = self.generate("existing")

        self.assertEqual(result, 1, stderr)
        self.assertEqual(
            (output / "outline.yml").read_text(encoding="utf-8"),
            "original outline",
        )
        self.assertTrue((output / "clash.yaml").is_dir())
        self.assertEqual({path.name for path in output.iterdir()}, {"outline.yml", "clash.yaml"})

    def test_second_output_failure_rolls_back_both_existing_outputs(self):
        output = self.root / "existing"
        output.mkdir(mode=0o700)
        for name in ("outline.yml", "clash.yaml"):
            (output / name).write_text("original " + name, encoding="utf-8")
            os.chmod(output / name, 0o600)
        original_replace = os.replace
        failed = False

        def fail_second_publish(source, destination):
            nonlocal failed
            if not failed and Path(destination) == output / "clash.yaml":
                failed = True
                raise OSError("injected replacement failure")
            return original_replace(source, destination)

        with mock.patch.object(generator.os, "replace", side_effect=fail_second_publish):
            result, _, _, _ = self.generate("existing")

        self.assertTrue(failed)
        self.assertEqual(result, 1)
        for name in ("outline.yml", "clash.yaml"):
            self.assertEqual(
                (output / name).read_text(encoding="utf-8"),
                "original " + name,
            )
            self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
        self.assertEqual({path.name for path in output.iterdir()}, {"outline.yml", "clash.yaml"})

    def test_active_transaction_preserves_existing_outputs(self):
        result, output, _, _ = self.generate("busy")
        self.assertEqual(result, 0)
        before = (output / "clash.yaml").read_bytes()
        (output / ".keel-transaction").mkdir(mode=0o700)
        result, _, _, _ = self.generate("busy")
        self.assertEqual(result, 1)
        self.assertEqual((output / "clash.yaml").read_bytes(), before)
        self.assertTrue((output / ".keel-transaction").is_dir())

    def test_generation_replaces_output_symlinks_without_touching_targets(self):
        output = self.root / "existing"
        output.mkdir(mode=0o700)
        for name in ("outline.yml", "clash.yaml"):
            target = self.root / (name + ".target")
            target.write_text("unchanged", encoding="utf-8")
            (output / name).symlink_to(target)
        result, _, stdout, stderr = self.generate("existing")
        self.assertEqual(result, 0, stderr)
        for name in ("outline.yml", "clash.yaml"):
            self.assertFalse((output / name).is_symlink())
            self.assertEqual((self.root / (name + ".target")).read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
        for device in self.inventory["devices"]:
            self.assertNotIn(device["secret"], stdout + stderr)


if __name__ == "__main__":
    unittest.main()
