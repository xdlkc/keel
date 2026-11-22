import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from keel.traffic import Device, Mapping, TrafficCollector
from keel.traffic_server import create_server


class TrafficServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "traffic.sqlite3"
        self.mapping = Mapping(
            manager_socket="/run/private-manager.sock",
            manager_unit=None,
            devices=(
                Device(8388, "Phone <A>"),
                Device(8390, "Legacy shared", shared=True),
            ),
        )
        self.wall = [1000.0]
        self.monotonic = [10.0]
        collector = TrafficCollector(
            self.mapping,
            self.database,
            wall_clock=lambda: self.wall[0],
            monotonic_clock=lambda: self.monotonic[0],
        )

        class Client:
            responses = iter([{8388: 100, 8390: 500}, {8388: 150, 8390: 700}])

            def ping(self):
                return next(self.responses)

        class Identity:
            def current(self):
                return "generation-a"

        client = Client()
        collector.collect_once(client, Identity())
        self.wall[0] += 1
        self.monotonic[0] += 1
        collector.collect_once(client, Identity())
        collector.close()
        self.now = [1002.0]
        self.server = create_server(
            mapping=self.mapping,
            database_path=self.database,
            port=0,
            now=lambda: self.now[0],
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, headers=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        if raw:
            return response.status, content, response
        return response.status, json.loads(content.decode("utf-8"))

    def test_aggregate_api_has_labels_totals_rates_and_recent_history(self):
        status, payload = self.request("GET", "/api/traffic?limit=2")
        self.assertEqual(status, 200)
        self.assertTrue(payload["generatedAt"].endswith("Z"))
        self.assertEqual(payload["collector"]["consecutiveFailures"], 0)
        self.assertEqual(len(payload["devices"]), 2)
        dedicated, shared = payload["devices"]
        self.assertEqual(dedicated["label"], "Phone <A>")
        self.assertFalse(dedicated["shared"])
        self.assertTrue(shared["shared"])
        self.assertEqual(dedicated["totalBytes"], 50)
        self.assertEqual(shared["totalBytes"], 200)
        self.assertEqual(dedicated["rateBytesPerSecond"], 50)
        self.assertEqual(len(dedicated["history"]), 2)
        self.assertIsNone(dedicated["history"][0]["rateBytesPerSecond"])
        serialized = json.dumps(payload)
        self.assertNotIn("private-manager", serialized)
        self.assertNotIn("managerTotal", serialized)
        self.assertNotIn("rxBytes", serialized)
        self.assertNotIn("txBytes", serialized)

    def test_stale_devices_keep_total_but_expire_current_rate(self):
        self.now[0] = 1032.0
        status, payload = self.request("GET", "/api/traffic")
        self.assertEqual(status, 200)
        for device in payload["devices"]:
            self.assertTrue(device["stale"])
            self.assertIsNone(device["rateBytesPerSecond"])
            self.assertEqual(device["rateUnavailableReason"], "stale")
            self.assertIsNotNone(device["totalBytes"])

    def test_failed_poll_immediately_hides_rate_but_keeps_total(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("UPDATE device_status SET last_error='offline' WHERE port=8388")
        status, payload = self.request("GET", "/api/traffic?limit=1",
                                       headers={"Host": "192.0.2.80"})
        self.assertEqual(status, 200)
        device = next(d for d in payload["devices"] if d["port"] == 8388)
        self.assertIsNone(device["rateBytesPerSecond"])
        self.assertEqual(device["totalBytes"], 50)
        self.assertEqual(device["history"][0]["rateBytesPerSecond"], 50)

    def test_history_limit_is_bounded(self):
        for query in ("limit=0", "limit=361", "limit=nope", "limit=1&limit=2", "other=1"):
            with self.subTest(query=query):
                status, payload = self.request("GET", "/api/traffic?" + query)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "invalid_range")
        status, payload = self.request("GET", "/api/traffic?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["devices"][0]["history"]), 1)

    def test_only_dashboard_static_files_and_aggregate_api_are_served(self):
        for path in ("/", "/devices"):
            status, content, response = self.request("GET", path, raw=True)
            self.assertEqual(status, 200)
            self.assertIn(b"width=device-width", content)
            self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        for path in ("/static/style.css", "/static/traffic.css", "/static/devices.js"):
            status, _, _ = self.request("GET", path, raw=True)
            self.assertEqual(status, 200, path)
        for path in (
            "/api/status", "/api/session", "/api/actions/start",
            "/static/../traffic.py", "/static/%2e%2e/traffic.py", "/etc/passwd",
        ):
            with self.subTest(path=path):
                status, payload = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"], "not_found")
        status, payload = self.request("POST", "/api/actions/start")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"], "method")

    def test_host_origin_and_missing_database_errors_are_sanitized(self):
        status, payload = self.request("GET", "/api/traffic", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "invalid_host")
        status, payload = self.request(
            "GET", "/api/traffic", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "cross_origin")

        missing = create_server(
            mapping=self.mapping,
            database_path=self.root / "missing" / "traffic.sqlite3",
            port=0,
        )
        thread = threading.Thread(target=missing.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", missing.server_address[1], timeout=5
            )
            connection.request("GET", "/api/traffic")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(payload["error"], "collector_unavailable")
            self.assertNotIn(str(self.root), payload["message"])
            self.assertFalse((self.root / "missing").exists())
        finally:
            missing.shutdown()
            thread.join(timeout=5)
            missing.server_close()

    def test_head_dashboard_returns_no_body(self):
        status, content, _ = self.request("HEAD", "/devices", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(content, b"")


if __name__ == "__main__":
    unittest.main()
