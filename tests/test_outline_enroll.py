import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
OUTLINE_DIR = ROOT / "deploy" / "outline"
sys.path.insert(0, str(OUTLINE_DIR))
import generate as generator
import enroll as enroller


class OutlineEnrollTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        os.chmod(self.root, 0o700)
        self.inventory_path = self.root / "inventory.json"
        self.arguments = [
            "--inventory", str(self.inventory_path),
            "--host", "203.0.113.10",
            "--port", "443",
            "--device-id", "alice-phone",
            "--label", "Alice Phone",
        ]

    def enroll(self, arguments=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = enroller.main(self.arguments if arguments is None else arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_creates_private_inventory_and_rerun_preserves_it(self):
        with mock.patch.object(
            enroller.secrets, "token_urlsafe", return_value="alice-test-secret"
        ) as token_urlsafe:
            result, stdout, stderr = self.enroll()

        self.assertEqual(result, 0, stderr)
        token_urlsafe.assert_called_once_with(32)
        self.assertNotIn("alice-test-secret", stdout + stderr)
        self.assertEqual(stat.S_IMODE(self.inventory_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        expected = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
            ],
        }
        self.assertEqual(json.loads(self.inventory_path.read_text(encoding="utf-8")), expected)
        self.assertEqual(generator.load_inventory(self.inventory_path)["devices"], expected["devices"])
        original = self.inventory_path.read_bytes()

        with mock.patch.object(
            enroller.secrets, "token_urlsafe", side_effect=AssertionError("secret regenerated")
        ):
            rerun_result, rerun_stdout, rerun_stderr = self.enroll()

        self.assertEqual(rerun_result, 0, rerun_stderr)
        self.assertNotIn("alice-test-secret", rerun_stdout + rerun_stderr)
        self.assertEqual(self.inventory_path.read_bytes(), original)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"inventory.json"})

    def test_adds_one_slot_without_changing_existing_devices(self):
        original = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
            ],
        }
        self.inventory_path.write_text(json.dumps(original), encoding="utf-8")
        os.chmod(self.inventory_path, 0o600)
        arguments = list(self.arguments)
        arguments[arguments.index("alice-phone")] = "bob-laptop"
        arguments[arguments.index("Alice Phone")] = "Bob Laptop"

        with mock.patch.object(
            enroller.secrets, "token_urlsafe", return_value="bob-test-secret"
        ) as token_urlsafe:
            result, stdout, stderr = self.enroll(arguments)

        self.assertEqual(result, 0, stderr)
        token_urlsafe.assert_called_once_with(32)
        self.assertNotIn("alice-test-secret", stdout + stderr)
        self.assertNotIn("bob-test-secret", stdout + stderr)
        saved = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["devices"][0], original["devices"][0])
        self.assertEqual(saved["devices"][1], {
            "id": "bob-laptop", "label": "Bob Laptop", "secret": "bob-test-secret",
        })
        self.assertEqual(stat.S_IMODE(self.inventory_path.stat().st_mode), 0o600)
        self.assertEqual(generator.load_inventory(self.inventory_path)["devices"], saved["devices"])

    def test_conflicts_preserve_existing_inventory_and_do_not_generate_secrets(self):
        original = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
            ],
        }
        cases = [
            ("different label", "--label", "Different Phone", "different label"),
            ("duplicate label", "--device-id", "bob-laptop", "label already"),
            ("changed host", "--host", "203.0.113.11", "host and port"),
            ("changed port", "--port", "8443", "host and port"),
        ]
        for name, option, value, message in cases:
            with self.subTest(name=name):
                self.inventory_path.write_text(json.dumps(original), encoding="utf-8")
                os.chmod(self.inventory_path, 0o600)
                before = self.inventory_path.read_bytes()
                arguments = list(self.arguments)
                arguments[arguments.index(option) + 1] = value

                with mock.patch.object(enroller.secrets, "token_urlsafe") as token_urlsafe:
                    result, stdout, stderr = self.enroll(arguments)

                self.assertEqual(result, 1)
                self.assertIn(message, stderr)
                token_urlsafe.assert_not_called()
                self.assertEqual(self.inventory_path.read_bytes(), before)
                self.assertNotIn("alice-test-secret", stdout + stderr)
                self.assertEqual({path.name for path in self.root.iterdir()}, {"inventory.json"})

    def test_rejects_invalid_inputs_before_creating_inventory(self):
        cases = [
            ("DNS host", "--host", "outline.example.com"),
            ("IPv6 host", "--host", "2001:db8::10"),
            ("invalid IPv4", "--host", "203.0.113.999"),
            ("zero port", "--port", "0"),
            ("large port", "--port", "65536"),
            ("uppercase id", "--device-id", "Alice"),
            ("underscore id", "--device-id", "alice_phone"),
            ("empty label", "--label", ""),
            ("untrimmed label", "--label", " Alice Phone"),
            ("unprintable label", "--label", "Alice\nPhone"),
            ("reserved label", "--label", "direct"),
            ("reserved group", "--label", "keel outline"),
            ("long label", "--label", "x" * 81),
        ]
        for name, option, value in cases:
            with self.subTest(name=name):
                arguments = list(self.arguments)
                arguments[arguments.index(option) + 1] = value

                with mock.patch.object(enroller.secrets, "token_urlsafe") as token_urlsafe:
                    result, stdout, stderr = self.enroll(arguments)

                self.assertEqual(result, 1, stdout)
                self.assertTrue(stderr)
                token_urlsafe.assert_not_called()
                self.assertFalse(self.inventory_path.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_file_and_parent_links_without_touching_targets(self):
        original = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
            ],
        }
        actual = self.root / "actual"
        actual.mkdir(mode=0o700)
        target = actual / "inventory.json"
        target.write_text(json.dumps(original), encoding="utf-8")
        os.chmod(target, 0o600)
        before = target.read_bytes()

        file_link = self.root / "inventory-link.json"
        file_link.symlink_to(target)
        arguments = list(self.arguments)
        arguments[1] = str(file_link)
        result, stdout, stderr = self.enroll(arguments)
        self.assertEqual(result, 1)
        self.assertNotIn("alice-test-secret", stdout + stderr)
        self.assertEqual(target.read_bytes(), before)

        directory_link = self.root / "directory-link"
        directory_link.symlink_to(actual, target_is_directory=True)
        for relative in ("inventory.json", "nested/inventory.json"):
            with self.subTest(relative=relative):
                if relative.startswith("nested"):
                    (actual / "nested").mkdir(mode=0o700)
                arguments = list(self.arguments)
                arguments[1] = str(directory_link / relative)
                result, stdout, stderr = self.enroll(arguments)
                self.assertEqual(result, 1)
                self.assertNotIn("alice-test-secret", stdout + stderr)
                self.assertEqual(target.read_bytes(), before)
                self.assertFalse((actual / "nested" / "inventory.json").exists())

        hard_link = self.root / "hard-link.json"
        os.link(target, hard_link)
        arguments = list(self.arguments)
        arguments[1] = str(hard_link)
        result, stdout, stderr = self.enroll(arguments)
        self.assertEqual(result, 1)
        self.assertNotIn("alice-test-secret", stdout + stderr)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(hard_link.read_bytes(), before)

    def test_rejects_insecure_permissions_without_chmod_or_replacement(self):
        insecure = self.root / "insecure"
        insecure.mkdir(mode=0o755)
        os.chmod(insecure, 0o755)
        arguments = list(self.arguments)
        arguments[1] = str(insecure / "inventory.json")

        result, _, stderr = self.enroll(arguments)

        self.assertEqual(result, 1)
        self.assertIn("permissions", stderr)
        self.assertEqual(stat.S_IMODE(insecure.stat().st_mode), 0o755)
        self.assertFalse((insecure / "inventory.json").exists())

        original = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
            ],
        }
        self.inventory_path.write_text(json.dumps(original), encoding="utf-8")
        os.chmod(self.inventory_path, 0o644)
        before = self.inventory_path.read_bytes()

        result, stdout, stderr = self.enroll()

        self.assertEqual(result, 1)
        self.assertIn("权限", stderr)
        self.assertNotIn("alice-test-secret", stdout + stderr)
        self.assertEqual(stat.S_IMODE(self.inventory_path.stat().st_mode), 0o644)
        self.assertEqual(self.inventory_path.read_bytes(), before)

    def test_failed_publish_and_secret_collision_preserve_existing_inventory(self):
        original = {
            "host": "203.0.113.10",
            "port": 443,
            "devices": [
                {"id": "alice-phone", "label": "Alice Phone", "secret": "alice-test-secret"},
            ],
        }
        arguments = list(self.arguments)
        arguments[arguments.index("alice-phone")] = "bob-laptop"
        arguments[arguments.index("Alice Phone")] = "Bob Laptop"
        for name, replacement_error in (("publish", True), ("collision", False)):
            with self.subTest(name=name):
                self.inventory_path.write_text(json.dumps(original), encoding="utf-8")
                os.chmod(self.inventory_path, 0o600)
                before = self.inventory_path.read_bytes()
                replace = mock.patch.object(
                    enroller.os, "replace", side_effect=OSError("injected publish failure")
                ) if replacement_error else mock.patch.object(enroller.os, "replace", wraps=os.replace)

                with replace as replaced, mock.patch.object(
                    enroller.secrets, "token_urlsafe", return_value="new-test-secret" if replacement_error else "alice-test-secret"
                ):
                    result, stdout, stderr = self.enroll(arguments)
                if replacement_error:
                    replaced.assert_called_once()

                self.assertEqual(result, 1)
                self.assertNotIn("alice-test-secret", stdout + stderr)
                self.assertEqual(self.inventory_path.read_bytes(), before)
                self.assertEqual({path.name for path in self.root.iterdir()}, {"inventory.json"})

    def test_serializes_concurrent_writers_without_losing_a_slot(self):
        first_generated = threading.Event()
        second_generated = threading.Event()
        release_first = threading.Event()
        outcomes = {}

        def run(name, device_id, label):
            try:
                outcomes[name] = enroller.enroll(
                    self.inventory_path, "203.0.113.10", 443, device_id, label
                )
            except Exception as error:
                outcomes[name] = error

        def token_urlsafe(size):
            if size != 32:
                raise AssertionError("unexpected secret size")
            name = threading.current_thread().name
            if name == "first":
                first_generated.set()
                if not release_first.wait(5):
                    raise RuntimeError("timed out waiting to release first writer")
            elif name == "second":
                second_generated.set()
            return name + "-test-secret"

        first = threading.Thread(
            target=run, args=("first", "alice-phone", "Alice Phone"), name="first"
        )
        second = threading.Thread(
            target=run, args=("second", "bob-laptop", "Bob Laptop"), name="second"
        )
        with mock.patch.object(enroller.secrets, "token_urlsafe", side_effect=token_urlsafe):
            first.start()
            try:
                self.assertTrue(first_generated.wait(2))
                second.start()
                self.assertFalse(second_generated.wait(0.1), "second writer entered the critical section")
            finally:
                release_first.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(outcomes, {"first": True, "second": True})
        saved = generator.load_inventory(self.inventory_path)
        self.assertEqual(
            {device["id"]: device["label"] for device in saved["devices"]},
            {"alice-phone": "Alice Phone", "bob-laptop": "Bob Laptop"},
        )
        self.assertEqual(len({device["secret"] for device in saved["devices"]}), 2)


if __name__ == "__main__":
    unittest.main()
