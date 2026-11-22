import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = ROOT / "deploy" / "outline" / "grafana-dashboard.json"


class OutlineDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        cls.panels = cls.dashboard["panels"]
        cls.targets = [
            target
            for panel in cls.panels
            for target in panel.get("targets", [])
        ]
        cls.expressions = [target["expr"] for target in cls.targets]
        cls.traffic_targets = [
            target for target in cls.targets
            if "shadowsocks_data_bytes" in target["expr"]
        ]

    def test_identity_datasource_and_device_filter(self):
        self.assertEqual(self.dashboard["uid"], "keel-outline")
        variables = self.dashboard["templating"]["list"]
        self.assertEqual(len(variables), 1)
        device = variables[0]
        self.assertEqual(device["name"], "device")
        self.assertEqual(device["type"], "query")
        self.assertTrue(device["multi"])
        self.assertTrue(device["includeAll"])
        self.assertEqual(device["allValue"], ".+")
        self.assertEqual(device["datasource"], {"type": "prometheus", "uid": "keel-prom"})
        self.assertEqual(device["query"]["query"], "label_values(shadowsocks_data_bytes, access_key)")
        for panel in self.panels:
            self.assertEqual(panel["datasource"], {"type": "prometheus", "uid": "keel-prom"})
        for target in self.targets:
            self.assertEqual(target["datasource"], {"type": "prometheus", "uid": "keel-prom"})

    def test_client_side_traffic_has_direction_protocol_and_no_double_count(self):
        self.assertEqual(len(self.traffic_targets), 2)
        functions = set()
        for target in self.traffic_targets:
            expression = target["expr"]
            self.assertEqual(expression.count("shadowsocks_data_bytes"), 1)
            self.assertIn('access_key=~"$device"', expression)
            self.assertIn('dir=~"^(c>p|c<p)$"', expression)
            self.assertIn('proto=~"^(tcp|udp)$"', expression)
            self.assertIn("sum by (access_key, dir, proto)", expression)
            self.assertNotIn("p<t", expression)
            self.assertNotIn("p>t", expression)
            self.assertNotIn("server", expression)
            for label in ("{{access_key}}", "{{dir}}", "{{proto}}"):
                self.assertIn(label, target["legendFormat"])
            if "rate(" in expression:
                functions.add("rate")
                self.assertIn("[$__rate_interval]", expression)
            if "increase(" in expression:
                functions.add("increase")
                self.assertIn("[$__range]", expression)
        self.assertEqual(functions, {"rate", "increase"})

        rate_panel = next(
            panel for panel in self.panels
            if any("rate(shadowsocks_data_bytes" in target["expr"] for target in panel.get("targets", []))
        )
        volume_panel = next(
            panel for panel in self.panels
            if any("increase(shadowsocks_data_bytes" in target["expr"] for target in panel.get("targets", []))
        )
        self.assertEqual(rate_panel["fieldConfig"]["defaults"]["unit"], "Bps")
        self.assertEqual(volume_panel["fieldConfig"]["defaults"]["unit"], "bytes")

    def test_availability_and_node_exporter_panels_do_not_invent_data(self):
        self.assertIn("up", self.expressions)
        required_metrics = {
            "node_cpu_seconds_total",
            "node_memory_MemAvailable_bytes",
            "node_memory_MemTotal_bytes",
            "node_filesystem_avail_bytes",
            "node_filesystem_size_bytes",
            "node_network_receive_bytes_total",
            "node_network_transmit_bytes_total",
        }
        for metric in required_metrics:
            self.assertTrue(any(metric in expression for expression in self.expressions), metric)
        for expression in self.expressions:
            self.assertNotIn("or vector", expression)
            self.assertNotIn("absent(", expression)
            self.assertNotIn("job=", expression)
        network_panel = next(panel for panel in self.panels if panel["title"] == "VPS 网卡收发速率")
        self.assertEqual(network_panel["fieldConfig"]["defaults"]["unit"], "Bps")
        self.assertEqual(len(network_panel["targets"]), 2)


if __name__ == "__main__":
    unittest.main()
