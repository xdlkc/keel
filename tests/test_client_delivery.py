import unittest
from urllib.error import HTTPError

from deploy.outline.verify_client import verify


class ClientDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.host = "203.0.113.8"
        self.base = "https://" + self.host
        self.delivery = dict(subscriptionUrl=self.base + "/sub/" + "x" * 43 + ".yaml",
                             dashboardUrl=self.base + "/d/keel-outline/keel-outline",
                             username="viewer", password="fixture")
        self.node = dict(name="Desktop", type="ss", server=self.host, port=18443,
                         udp=True, cipher="chacha20-ietf-poly1305", password="fixture")
        self.traffic = "123"

    def fetch(self, url, headers=None):
        if url == self.delivery["subscriptionUrl"]:
            return {"proxies": [self.node], "profile": {"store-selected": True},
                    "proxy-groups": [{"name": "Keel Outline", "type": "select", "proxies": ["Desktop"]}]}
        if url.endswith("/api/dashboards/uid/keel-outline"):
            if not headers:
                raise HTTPError(url, 401, "authentication required", None, None)
            return {"dashboard": {"uid": "keel-outline"}, "meta": {"canEdit": False, "url": "/d/keel-outline/title"}}
        if url.endswith("/api/user/orgs"):
            return [{"role": "Viewer"}]
        if url.endswith("?query=up"):
            return {"data": {"result": [{"metric": {"job": job}, "value": [1, "1"]} for job in ("node", "outline")]}}
        if "shadowsocks_data_bytes" in url:
            return {"data": {"result": [{"value": [1, self.traffic]}]}}
        self.fail("unexpected endpoint")

    def verify(self):
        return verify(self.delivery, self.host, 18443, "desktop", "Desktop", self.fetch)

    def test_end_to_end_and_no_traffic_are_distinct(self):
        self.assertTrue(self.verify())
        self.traffic = "0"
        self.assertFalse(self.verify())

    def test_bad_port_and_broken_dashboard_link_are_rejected(self):
        self.node["port"] = 443
        with self.assertRaises(ValueError):
            self.verify()
        self.node["port"] = 18443
        self.delivery["dashboardUrl"] = self.base + "/d/keel-outline-wrong"
        with self.assertRaises(ValueError):
            self.verify()

    def test_wrong_origin_rejected_before_any_request(self):
        self.delivery["dashboardUrl"] = "https://other.invalid/d/keel-outline/title"
        with self.assertRaises(ValueError):
            verify(self.delivery, self.host, 18443, "desktop", "Desktop",
                   lambda *args: self.fail("unexpected request"))
