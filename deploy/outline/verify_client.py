"""Verify a private Keel delivery against the expected host without printing credentials."""
import argparse
import base64
import ipaddress
import json
import os
import re
from pathlib import Path
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_delivery(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Delivery must be an owner-only regular file owned by this user.")
        raw = f.read(8193)
    if len(raw) > 8192:
        raise ValueError("Unexpected delivery size.")
    return json.loads(raw)


def verify(delivery, host, port, device_id, label, fetch):
    origin = "https://" + str(ipaddress.IPv4Address(host))
    for field, prefix in (("subscriptionUrl", "/sub/"), ("dashboardUrl", "/d/keel-outline")):
        u = urlsplit(delivery[field])
        if (u.scheme != "https" or u.netloc != host or not u.path.startswith(prefix)
                or u.fragment or u.query):
            raise ValueError("Delivery URLs must point to the expected HTTPS origin and service.")
    if not re.fullmatch(r"/sub/[a-zA-Z0-9_-]{32,256}\.yaml", urlsplit(delivery["subscriptionUrl"]).path):
        raise ValueError("Unexpected subscription path.")
    client = fetch(delivery["subscriptionUrl"])
    groups = [g for g in client["proxy-groups"] if g["name"] == "Keel Outline" and g["type"] == "select"]
    if (len(groups) != 1 or label not in groups[0]["proxies"]
            or client.get("profile", {}).get("store-selected") is not True):
        raise ValueError("Expected to find the selected device in a persistent manual group.")
    proxies = [p for p in client["proxies"] if p["name"] == label]
    if len(proxies) != 1 or proxies[0]["type"] != "ss" or proxies[0]["server"] != host:
        raise ValueError("The selected node does not point to the expected Outline host.")
    if (proxies[0].get("udp") is not True or not isinstance(proxies[0].get("password"), str)
            or not proxies[0]["password"] or proxies[0].get("cipher") != "chacha20-ietf-poly1305"
            or type(proxies[0].get("port")) is not int or proxies[0]["port"] != port):
        raise ValueError("The selected node is missing its UDP setting or access key.")
    token = base64.b64encode((delivery["username"] + ":" + delivery["password"]).encode()).decode()
    auth = {"Authorization": "Basic " + token}
    endpoint = origin + "/api/dashboards/uid/keel-outline"
    try:
        fetch(endpoint)
    except HTTPError as err:
        if err.code not in (401, 403):
            raise
    else:
        raise ValueError("Grafana permits anonymous access to the dashboard.")
    dashboard = fetch(endpoint, auth)
    if dashboard["dashboard"]["uid"] != "keel-outline" or dashboard["meta"]["canEdit"]:
        raise ValueError("Expected a read-only Keel dashboard.")
    if urlsplit(delivery["dashboardUrl"]).path not in (
        "/d/keel-outline/keel-outline", dashboard["meta"]["url"]
    ):
        raise ValueError("Unexpected dashboard link.")
    orgs = fetch(origin + "/api/user/orgs", auth)
    if not orgs or any(org["role"] != "Viewer" for org in orgs):
        raise ValueError("Expected a Grafana Viewer credential.")
    prom = origin + "/api/datasources/proxy/uid/keel-prom/api/v1/query?"
    results = fetch(prom + urlencode({"query": "up"}), auth)["data"]["result"]
    healthy = {r["metric"]["job"] for r in results if float(r["value"][1]) == 1}
    if not {"outline", "node"}.issubset(healthy):
        raise ValueError("Prometheus cannot scrape both Outline and node_exporter.")
    # The ID is embedded only after encoding it as a PromQL string literal.
    expr = 'sum(shadowsocks_data_bytes{access_key=' + json.dumps(device_id) + ',dir="c<p"})'
    result = fetch(prom + urlencode({"query": expr}), auth)["data"]["result"]
    return bool(result and float(result[0]["value"][1]) > 0)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--delivery", type=Path, required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--device-id", required=True)
    p.add_argument("--label", required=True)
    args = p.parse_args()
    opener = build_opener(NoRedirect())

    def fetch(url, headers=None):
        with opener.open(Request(url, headers=headers or {}), timeout=15) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Unexpected response size.")
        return json.loads(raw)

    try:
        observed = verify(private_delivery(args.delivery), args.host, args.port, args.device_id, args.label, fetch)
    except (OSError, URLError, ValueError, TypeError, KeyError, IndexError) as err:
        # urllib and JSON exceptions may include a full secret URL or body.
        print("Verification failed (" + type(err).__name__ + "). Check the private delivery, TLS, label, viewer role and scrape targets.", file=sys.stderr)
        return 1
    print("Shared subscription, selected node, TLS, read-only Grafana and scrape targets verified.")
    if not observed:
        print("No download sample yet for this key. Connect the client, finish a request and retry after two scrapes.")
        return 2
    print("Prometheus has recorded download traffic for the supplied key ID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
