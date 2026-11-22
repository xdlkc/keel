import http.client
import json
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock

from keel.errors import AuthError, GcloudError
from keel.server import create_server
from keel.traffic import Device, Mapping, TrafficCollector
from keel.traffic_server import create_server as create_traffic_server


class WebGuardTests(unittest.TestCase):
    def setUp(self):
        self.cloud = Mock()
        self.cloud.describe.return_value = {
            "id": "123456789",
            "name": "vm-a",
            "status": "RUNNING",
            "machineType": "zones/zone-a/machineTypes/e2-small",
            "disks": [{"boot": True, "diskSizeGb": "20"}],
            "networkInterfaces": [],
        }
        factory = Mock(return_value=self.cloud)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "vps.local.json"
        self.config_path.write_text(
            json.dumps({"project": "project-a", "zone": "zone-a", "instance": "vm-a"}),
            encoding="utf-8",
        )
        self.server = create_server(
            port=0, config_path=self.config_path, cloud_factory=factory
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:{}".format(self.port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, headers=None, details=False, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        if raw:
            return response.status, content
        payload = json.loads(content.decode("utf-8"))
        if details:
            return response.status, payload, response.getheader("Connection")
        return response.status, payload

    def session_token(self):
        status, payload = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        return payload["csrfToken"]

    def action(
        self, confirmation="vm-a", token=None, origin=None, instance="vm-a",
        project="project-a", zone="zone-a", operation="start",
    ):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Keel-CSRF"] = token
        if origin is not None:
            headers["Origin"] = origin
        return self.request(
            "POST",
            "/api/actions/{}".format(operation),
            body=json.dumps({
                "project": project,
                "zone": zone,
                "instance": instance,
                "confirmation": confirmation,
            }),
            headers=headers,
        )

    def test_invalid_host_is_rejected_before_cloud_access(self):
        status, payload = self.request("GET", "/api/status", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "invalid_host")
        self.cloud.describe.assert_not_called()

    def test_cross_origin_read_is_rejected_before_cloud_access(self):
        status, payload = self.request(
            "GET", "/api/status", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "cross_origin")
        self.cloud.describe.assert_not_called()

    def test_cross_origin_write_with_stolen_token_is_rejected(self):
        token = self.session_token()
        status, payload = self.action(token=token, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "cross_origin")
        self.cloud.start.assert_not_called()

    def test_loopback_alias_is_still_a_different_origin(self):
        token = self.session_token()
        status, payload = self.action(
            token=token, origin="http://localhost:{}".format(self.port)
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "cross_origin")
        self.cloud.start.assert_not_called()

    def test_port_80_accepts_canonical_loopback_host_and_origin(self):
        self.server.server_address = ("127.0.0.1", 80)
        status, payload = self.request(
            "GET",
            "/api/session",
            headers={"Host": "127.0.0.1", "Origin": "http://127.0.0.1"},
        )
        self.assertEqual(status, 200)
        self.assertIn("csrfToken", payload)

    def test_rejected_requests_close_the_http_connection(self):
        status, payload, connection = self.request(
            "GET", "/api/status", headers={"Host": "evil.example"}, details=True
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "invalid_host")
        self.assertEqual(connection, "close")

    def test_status_uses_the_fixed_official_billing_link(self):
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["links"]["billing"], "https://console.cloud.google.com/billing")

    def test_metrics_authentication_failure_has_explicit_state(self):
        self.cloud.access_token.side_effect = AuthError("未登录")
        status, payload = self.request("GET", "/api/metrics")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "not_logged_in")

    def test_write_requires_session_csrf_and_origin(self):
        for token in ("wrong", "é"):
            status, payload = self.action(token=token, origin=self.origin)
            self.assertEqual(status, 403)
            self.assertEqual(payload["error"], "csrf")
        status, payload = self.action(token=self.session_token())
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "cross_origin")
        self.cloud.start.assert_not_called()

    def test_wrong_confirmation_never_calls_cloud(self):
        status, payload = self.action(
            confirmation="vm-b", token=self.session_token(), origin=self.origin
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "confirmation")
        self.cloud.start.assert_not_called()

    def test_stale_project_or_zone_with_same_instance_name_never_executes(self):
        for operation in ("start", "stop"):
            for field, replacement in (("project", "project-b"), ("zone", "zone-b")):
                with self.subTest(operation=operation, field=field):
                    config = {"project": "project-a", "zone": "zone-a", "instance": "vm-a"}
                    config[field] = replacement
                    self.config_path.write_text(json.dumps(config), encoding="utf-8")
                    status, payload = self.action(
                        token=self.session_token(), origin=self.origin, operation=operation
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error"], "confirmation")
        self.cloud.start.assert_not_called()
        self.cloud.stop.assert_not_called()

    def test_success_is_returned_only_after_cloud_command_returns(self):
        status, payload = self.action(token=self.session_token(), origin=self.origin)
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        self.cloud.start.assert_called_once()

    def test_cloud_failure_does_not_return_success(self):
        self.cloud.start.side_effect = GcloudError("命令失败")
        status, payload = self.action(token=self.session_token(), origin=self.origin)
        self.assertEqual(status, 502)
        self.assertNotIn("success", payload)
        self.assertEqual(payload["error"], "cloud_error")

    def test_devices_view_is_available_without_tunnel_or_cloud_access(self):
        status, content = self.request("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b'href="/devices"', content)
        status, content = self.request("GET", "/devices", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"width=device-width", content)
        status, payload = self.request("GET", "/api/traffic")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "traffic_unavailable")
        self.cloud.describe.assert_not_called()

    def test_proxy_port_must_not_loop_back_into_admin(self):
        cfg = json.loads(self.config_path.read_text())
        cfg["trafficProxyPort"] = self.port
        self.config_path.write_text(json.dumps(cfg))
        self.assertEqual(self.request("GET", "/api/traffic")[0], 503)
        with self.assertRaises(ValueError):
            create_server(port=self.port, traffic_forward_port=self.port)

    def test_devices_view_uses_only_configured_fixed_loopback_forward(self):
        mapping = Mapping(
            manager_socket="/run/manager.sock",
            manager_unit=None,
            devices=(Device(8388, "Phone A"),),
        )
        database = Path(self.temp_dir.name) / "traffic.sqlite3"
        collector = TrafficCollector(
            mapping,
            database,
            wall_clock=lambda: 1000.0,
            monotonic_clock=lambda: 10.0,
        )

        class Client:
            def ping(self):
                return {8388: 100}

        class Identity:
            def current(self):
                return "generation-a"

        collector.collect_once(Client(), Identity())
        collector.close()
        traffic_server = create_traffic_server(mapping, database, port=0)
        traffic_thread = threading.Thread(target=traffic_server.serve_forever)
        traffic_thread.start()

        def stop_traffic_server():
            traffic_server.shutdown()
            traffic_thread.join(timeout=5)
            traffic_server.server_close()

        self.addCleanup(stop_traffic_server)
        main_server = create_server(
            port=0,
            config_path=self.config_path,
            cloud_factory=Mock(return_value=self.cloud),
            traffic_forward_port=traffic_server.server_address[1],
        )
        main_thread = threading.Thread(target=main_server.serve_forever)
        main_thread.start()
        self.port = main_server.server_address[1]
        try:
            status, payload = self.request("GET", "/api/traffic?limit=1")
            self.assertEqual(status, 200)
            self.assertEqual(payload["devices"][0]["label"], "Phone A")
            status, payload = self.request("GET", "/api/traffic?limit=0")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_range")
            self.cloud.describe.assert_not_called()
        finally:
            main_server.shutdown()
            main_thread.join(timeout=5)
            main_server.server_close()


if __name__ == "__main__":
    unittest.main()
