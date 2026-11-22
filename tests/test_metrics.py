import json
import unittest
import unittest.mock
from datetime import datetime, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from keel.config import Config
from keel.errors import MonitoringError
from keel.metrics import MonitoringClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.payload


class MonitoringClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(project="project-a", zone="zone-a", instance="vm-a")
        self.now = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)

    def point(self, timestamp, value, integer=False):
        key = "int64Value" if integer else "doubleValue"
        return {
            "interval": {"endTime": timestamp},
            "value": {key: str(value) if integer else value},
        }

    def test_minute_buckets_pagination_delta_rates_and_nic_sum(self):
        pages = [
            {
                "timeSeries": [{
                    "metric": {"type": "compute.googleapis.com/instance/cpu/utilization"},
                    "points": [self.point("2026-01-01T00:59:00Z", 0.25)],
                }],
                "nextPageToken": "cpu-page-2",
            },
            {
                "timeSeries": [{
                    "metric": {"type": "compute.googleapis.com/instance/cpu/utilization"},
                    "points": [self.point("2026-01-01T01:00:00Z", 0.5)],
                }]
            },
            {
                "timeSeries": [
                    {
                        "metric": {"type": "compute.googleapis.com/instance/network/sent_bytes_count"},
                        "resource": {"labels": {"device_id": "nic0"}},
                        "points": [self.point("2026-01-01T00:59:00Z", 60000000, True)],
                    },
                    {
                        "metric": {"type": "compute.googleapis.com/instance/network/sent_bytes_count"},
                        "resource": {"labels": {"device_id": "nic1"}},
                        "points": [self.point("2026-01-01T00:59:00Z", 60000000, True)],
                    },
                ],
                "nextPageToken": "network-page-2",
            },
            {
                "timeSeries": [{
                    "metric": {"type": "compute.googleapis.com/instance/network/sent_bytes_count"},
                    "resource": {"labels": {"device_id": "nic0"}},
                    "points": [self.point("2026-01-01T01:00:00Z", 120000000, True)],
                }]
            },
        ]
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(json.dumps(pages[len(requests) - 1]).encode("utf-8"))

        tokens = []
        client = MonitoringClient(
            access_token=lambda: tokens.append("secret") or "secret",
            opener=opener,
            now=lambda: self.now,
        )
        result = client.collect(self.config, "123456789")

        self.assertEqual(tokens, ["secret"])
        self.assertEqual(len(requests), 4)
        self.assertEqual(result["bucketSeconds"], 60)
        self.assertEqual(result["startTime"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["endTime"], "2026-01-01T01:00:00Z")
        self.assertEqual(result["cpu"], [
            {"timestamp": "2026-01-01T00:59:00Z", "value": 25.0},
            {"timestamp": "2026-01-01T01:00:00Z", "value": 50.0},
        ])
        self.assertEqual(result["network"], [
            {"timestamp": "2026-01-01T00:59:00Z", "value": 16.0},
            {"timestamp": "2026-01-01T01:00:00Z", "value": 16.0},
        ])

        for index, (request, timeout) in enumerate(requests):
            parsed = urlsplit(request.full_url)
            query = parse_qs(parsed.query)
            self.assertEqual(parsed.path, "/v3/projects/project-a/timeSeries")
            self.assertIn('resource.labels.instance_id = "123456789"', query["filter"][0])
            self.assertIn('resource.labels.project_id = "project-a"', query["filter"][0])
            self.assertEqual(query["aggregation.alignmentPeriod"], ["60s"])
            self.assertEqual(query["view"], ["FULL"])
            self.assertEqual(timeout, 15)
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            expected_aligner = "ALIGN_MEAN" if index < 2 else "ALIGN_DELTA"
            self.assertEqual(query["aggregation.perSeriesAligner"], [expected_aligner])
        self.assertEqual(parse_qs(urlsplit(requests[1][0].full_url).query)["pageToken"], ["cpu-page-2"])
        self.assertEqual(parse_qs(urlsplit(requests[3][0].full_url).query)["pageToken"], ["network-page-2"])

    def test_no_time_series_is_an_empty_result_not_zeroes(self):
        def opener(request, timeout):
            return FakeResponse(b"{}")

        client = MonitoringClient(
            access_token=lambda: "secret",
            opener=opener,
            now=lambda: self.now,
        )
        result = client.collect(self.config, 123)
        self.assertTrue(result["empty"])
        self.assertEqual(result["cpu"], [])
        self.assertEqual(result["network"], [])

    def test_incomplete_or_malformed_monitoring_responses_are_errors(self):
        payloads = [
            {"executionErrors": [{"code": 13, "message": "private details"}]},
            {"unreachable": ["projects/project-a"]},
            {"executionErrors": {}},
            {"unreachable": [None]},
            {"nextPageToken": 0},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                def opener(request, timeout):
                    return FakeResponse(json.dumps(payload).encode("utf-8"))

                client = MonitoringClient(
                    access_token=lambda: "secret",
                    opener=opener,
                    now=lambda: self.now,
                )
                with self.assertRaises(MonitoringError):
                    client.collect(self.config, "123")

    def test_malformed_nested_points_do_not_fabricate_values(self):
        payload = {
            "timeSeries": [
                None,
                {"points": None},
                {"points": [
                    None,
                    {"interval": None, "value": {"int64Value": "1"}},
                    {"interval": {"endTime": None}, "value": {"int64Value": "1"}},
                    {"interval": {"endTime": "2026-01-01T01:00:00Z"}, "value": {"int64Value": []}},
                ]},
            ],
        }

        def opener(request, timeout):
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        client = MonitoringClient(
            access_token=lambda: "secret",
            opener=opener,
            now=lambda: self.now,
        )
        result = client.collect(self.config, "123")
        self.assertTrue(result["empty"])
        self.assertEqual(result["cpu"], [])
        self.assertEqual(result["network"], [])

    def test_non_numeric_instance_id_is_rejected_before_network_or_auth(self):
        opener = Mock()
        token = Mock()
        client = MonitoringClient(access_token=token, opener=opener, now=lambda: self.now)
        with self.assertRaises(ValueError):
            client.collect(self.config, "vm-a")
        opener.assert_not_called()
        token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
