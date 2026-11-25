import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = REPO_ROOT / "deploy" / "tls-gateway"


def _load_json_no_duplicates(path: Path):
    def _pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f"Duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs_hook)


class TlsGatewayTemplateTests(unittest.TestCase):
    def test_sg_sing_box_template_routes_all_four_devices_to_loopback_outline(self):
        cfg = _load_json_no_duplicates(TEMPLATE_ROOT / "sg" / "mobile-gateway.example.json")
        self.assertEqual(len(cfg["inbounds"]), 1)
        inbound = cfg["inbounds"][0]
        self.assertEqual(inbound["type"], "trojan")
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["listen_port"], 19080)
        self.assertEqual(inbound["transport"], {"type": "ws", "path": "/keel/mobile-ws"})

        users = [user["name"] for user in inbound["users"]]
        self.assertEqual(users, ["computer-1", "phone-1", "computer-2", "phone-2"])
        for user in inbound["users"]:
            self.assertTrue(user["password"].startswith("REPLACE_WITH_"))

        outbounds = {item["tag"]: item for item in cfg["outbounds"]}
        self.assertEqual(set(outbounds), {
            "outline-computer-1",
            "outline-phone-1",
            "outline-computer-2",
            "outline-phone-2",
        })
        for tag, outbound in outbounds.items():
            self.assertEqual(outbound["type"], "shadowsocks")
            self.assertEqual(outbound["server"], "127.0.0.1")
            expected_port = 18382 if tag == "outline-phone-1" else 18443
            self.assertEqual(outbound["server_port"], expected_port)
            self.assertEqual(outbound["method"], "chacha20-ietf-poly1305")
            self.assertTrue(outbound["password"].startswith("REPLACE_WITH_"))

        rules = cfg["route"]["rules"]
        routed = {
            tuple(rule["auth_user"]): rule["outbound"]
            for rule in rules
            if "auth_user" in rule
        }
        self.assertEqual(routed, {
            ("computer-1",): "outline-computer-1",
            ("phone-1",): "outline-phone-1",
            ("computer-2",): "outline-computer-2",
            ("phone-2",): "outline-phone-2",
        })
        self.assertEqual(rules[-1], {"action": "reject"})

    def test_us_sing_box_template_blocks_private_ips_and_routes_direct(self):
        cfg = _load_json_no_duplicates(TEMPLATE_ROOT / "us" / "proxy.example.json")
        self.assertEqual(len(cfg["inbounds"]), 1)
        inbound = cfg["inbounds"][0]
        self.assertEqual(inbound["type"], "trojan")
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["listen_port"], 19080)
        self.assertEqual(inbound["transport"], {"type": "ws", "path": "/keel/mobile-ws"})
        self.assertTrue(all(u["password"].startswith("REPLACE_WITH_") for u in inbound["users"]))

        self.assertEqual(cfg["outbounds"], [{"type": "direct", "tag": "direct"}])
        self.assertEqual(cfg["route"]["rules"][0], {"ip_is_private": True, "action": "reject"})
        self.assertEqual(cfg["route"]["final"], "direct")

    def test_systemd_and_sysctl_templates_enforce_hardening_and_certs(self):
        sg_unit = (TEMPLATE_ROOT / "sg" / "keel-mobile-https.service").read_text(encoding="utf-8")
        self.assertIn("IPAddressDeny=any", sg_unit)
        self.assertIn("IPAddressAllow=localhost", sg_unit)
        self.assertIn("/opt/keel-mobile/bin/sing-box", sg_unit)

        sg_renew = (TEMPLATE_ROOT / "sg" / "cert-renew.service").read_text(encoding="utf-8")
        self.assertIn("--cert-name keel-domain", sg_renew)

        us_unit = (TEMPLATE_ROOT / "us" / "keel-us-proxy.service").read_text(encoding="utf-8")
        self.assertIn("/opt/keel-mobile/bin/sing-box", us_unit)
        self.assertIn("/etc/keel-us/proxy.json", us_unit)

        us_renew = (TEMPLATE_ROOT / "us" / "cert-renew.service").read_text(encoding="utf-8")
        self.assertIn("--cert-name keel-us-domain", us_renew)

        bbr = (TEMPLATE_ROOT / "us" / "90-keel-bbr.conf").read_text(encoding="utf-8")
        self.assertIn("net.ipv4.tcp_congestion_control = bbr", bbr)

        journal = (TEMPLATE_ROOT / "us" / "journald-size.conf").read_text(encoding="utf-8")
        self.assertIn("SystemMaxUse=100M", journal)

    def test_nginx_templates_enforce_acme_ws_and_private_subscription_headers(self):
        for host, cert_name in (("sg", "keel-domain"), ("us", "keel-us-domain")):
            conf = (TEMPLATE_ROOT / host / "nginx-keel.example.conf").read_text(encoding="utf-8")
            self.assertIn("location ^~ /.well-known/acme-challenge/", conf)
            self.assertIn(f"/etc/letsencrypt/live/{cert_name}/fullchain.pem", conf)
            self.assertIn("location = /keel/mobile-ws", conf)
            self.assertIn("proxy_pass http://127.0.0.1:19080;", conf)
            self.assertIn("location = /sub/REPLACE_WITH_256_BIT_TOKEN.yaml", conf)
            self.assertIn("proxy_pass http://127.0.0.1:19090;", conf)
            self.assertIn("location = /metrics-sync/REPLACE_WITH_256_BIT_SYNC_TOKEN.json", conf)
            self.assertIn("alias /var/lib/keel-quota/sync.json;", conf)
            self.assertIn("access_log off;", conf)
            self.assertIn('Cache-Control "private, no-store"', conf)
            self.assertIn('X-Robots-Tag "noindex, nofollow"', conf)

        sg_conf = (TEMPLATE_ROOT / "sg" / "nginx-keel.example.conf").read_text(encoding="utf-8")
        self.assertIn("server_name sg.example.com;", sg_conf)
        self.assertIn("server_name sub.example.com;", sg_conf)
        self.assertIn("proxy_pass http://127.0.0.1:3000;", sg_conf)

    def test_quota_and_sub_server_templates_enforce_limits_and_isolation(self):
        sg_quota = _load_json_no_duplicates(TEMPLATE_ROOT / "quota" / "sg-config.example.json")
        self.assertEqual(sg_quota["state_dir"], "/var/lib/keel-quota")
        self.assertEqual(sg_quota["total_limit_gib"], 200)
        self.assertIsNone(sg_quota["local_limit_gib"])
        self.assertIn("/metrics-sync/REPLACE_WITH_256_BIT_SYNC_TOKEN.json", sg_quota["peer_sync_url"])
        self.assertEqual(sg_quota["proxy_services"], ["keel-mobile-https.service", "keel-outline.service"])

        us_quota = _load_json_no_duplicates(TEMPLATE_ROOT / "quota" / "us-config.example.json")
        self.assertEqual(us_quota["state_dir"], "/var/lib/keel-quota")
        self.assertEqual(us_quota["total_limit_gib"], 200)
        self.assertEqual(us_quota["local_limit_gib"], 190)
        self.assertIn("/metrics-sync/REPLACE_WITH_256_BIT_SYNC_TOKEN.json", us_quota["peer_sync_url"])
        self.assertEqual(us_quota["proxy_services"], ["keel-us-proxy.service"])

        quota_service = (TEMPLATE_ROOT / "quota" / "keel-quota.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/bin/keel-quota", quota_service)

        quota_timer = (TEMPLATE_ROOT / "quota" / "keel-quota.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=60s", quota_timer)

        sub_service = (TEMPLATE_ROOT / "quota" / "keel-sub-server.service").read_text(encoding="utf-8")
        self.assertIn("User=www-data", sub_service)
        self.assertIn("ExecStart=/usr/local/bin/keel-sub-server", sub_service)
        self.assertIn("IPAddressDeny=any", sub_service)
        self.assertIn("IPAddressAllow=localhost", sub_service)

    def test_clash_profile_template_matches_dual_node_structure(self):
        profile = (TEMPLATE_ROOT / "clash-profile.example.yaml").read_text(encoding="utf-8")
        for expected in (
            'name: "新加坡 01"',
            'name: "美国 01"',
            "type: trojan",
            "port: 443",
            "path: /keel/mobile-ws",
            "udp: true",
            "skip-cert-verify: false",
            'name: "🚀 节点选择"',
            'name: "🛟 故障转移 (新加坡优先)"',
            "interval: 180",
            'name: "♻️ 自动测速"',
            "interval: 300",
            "tolerance: 50",
            "- GEOIP,LAN,DIRECT",
            "- GEOIP,CN,DIRECT",
            "- MATCH,🚀 节点选择",
        ):
            self.assertIn(expected, profile)

    def test_templates_contain_no_production_hosts_or_project_ids(self):
        import ipaddress
        import re

        ipv4_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        gcp_uuid_project_re = re.compile(r"\bproject-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{3,}\b")
        allowed_doc_nets = (
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/24"),
            ipaddress.ip_network("203.0.113.0/24"),
        )
        for path in sorted(TEMPLATE_ROOT.rglob("*")):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            self.assertIsNone(
                gcp_uuid_project_re.search(content),
                f"Real GCP project ID leaked into {path}",
            )
            for match in ipv4_re.findall(content):
                try:
                    addr = ipaddress.ip_address(match)
                except ValueError:
                    continue
                if addr.is_global and not any(addr in net for net in allowed_doc_nets):
                    self.fail(f"Non-documentation public IP {match!r} leaked into {path}")

    def test_bilingual_readmes_reference_tls_architecture_and_templates(self):
        for readme_name in ("README.md", "README.zh-CN.md"):
            content = (REPO_ROOT / readme_name).read_text(encoding="utf-8")
            self.assertIn("./docs/tls-443-architecture.md", content)
            self.assertIn("./deploy/tls-gateway/", content)
            self.assertIn("./docs/agent-onboarding.md", content)
