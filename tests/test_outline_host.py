import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.outline.host import hostctl


class HostBootstrapTests(unittest.TestCase):
    def test_retry_preserves_credentials_and_delivery_only_grants_viewer(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "access.json"
            access = hostctl.ensure_access(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(hostctl.ensure_access(path), access)
            delivery = hostctl.build_delivery("203.0.113.10", access)
            self.assertEqual(delivery["password"], access["viewer"])
            self.assertNotIn(access["admin"], json.dumps(delivery))
            path.chmod(0o644)
            with self.assertRaises(hostctl.HostError):
                hostctl.ensure_access(path)

    def test_ready_before_first_client_uses_a_new_key(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "server.yaml"
            keys = [{"id": "phone"}, {"id": "desktop"}]
            hostctl.atomic_write(config, json.dumps({"services": [{"keys": keys}]}))
            with patch.object(hostctl, "SERVER_PATH", config):
                for gauge in (b"shadowsocks_keys 1\n", b"shadowsocks_keys 2\n"):
                    with patch.object(hostctl.urllib.request, "urlopen", return_value=io.BytesIO(gauge)):
                        if gauge.endswith(b"1\n"):
                            with self.assertRaises(hostctl.HostError):
                                hostctl.wait_for_key("phone", attempts=1)
                        else:
                            hostctl.wait_for_key("phone", attempts=1)
                with self.assertRaises(hostctl.HostError):
                    hostctl.wait_for_key("unknown", attempts=1)

    def test_proxy_port_cannot_conflict_with_management(self):
        for port in (0, 22, 80, 443, 1023, 3000, 9090, 9091, 9100, 65536):
            with self.subTest(port=port), self.assertRaises(hostctl.HostError):
                hostctl.validate_port(port)
        self.assertEqual(hostctl.validate_port("18443"), 18443)
