import json
import math
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from .errors import AuthError, MonitoringError


CPU_METRIC = "compute.googleapis.com/instance/cpu/utilization"
NETWORK_METRIC = "compute.googleapis.com/instance/network/sent_bytes_count"
REQUEST_TIMEOUT = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGES = 100


def _isoformat(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_value(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MonitoringClient:
    def __init__(self, access_token, opener=None, now=None):
        self.access_token = access_token
        self.opener = opener or urllib.request.urlopen
        self.now = now or (lambda: datetime.now(timezone.utc))

    def collect(self, config, instance_id):
        instance_id = str(instance_id)
        if not instance_id.isdigit():
            raise ValueError("Compute Engine instance ID must be numeric")

        end = self.now().astimezone(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(hours=1)
        token = self.access_token()
        if not token:
            raise AuthError("未获取到有效访问令牌，请重新登录 gcloud。")

        common = {
            "project": _filter_value(config.project),
            "project_path": quote(config.project, safe=""),
            "instance": instance_id,
            "start": _isoformat(start),
            "end": _isoformat(end),
        }
        cpu_series = self._fetch_series(CPU_METRIC, "ALIGN_MEAN", common, token)
        network_series = self._fetch_series(NETWORK_METRIC, "ALIGN_DELTA", common, token)
        cpu = self._cpu_points(cpu_series)
        network = self._network_points(network_series)
        return {
            "startTime": common["start"],
            "endTime": common["end"],
            "bucketSeconds": 60,
            "empty": not cpu and not network,
            "cpu": cpu,
            "network": network,
        }

    def _fetch_series(self, metric_type, aligner, common, token):
        filter_expression = (
            'metric.type = "{metric}" AND resource.type = "gce_instance" '
            'AND resource.labels.project_id = "{project}" '
            'AND resource.labels.instance_id = "{instance}"'
        ).format(metric=metric_type, **common)
        params = {
            "filter": filter_expression,
            "interval.startTime": common["start"],
            "interval.endTime": common["end"],
            "aggregation.alignmentPeriod": "60s",
            "aggregation.perSeriesAligner": aligner,
            "view": "FULL",
        }
        path = "/v3/projects/{}/timeSeries".format(common["project_path"])
        series = []
        seen_page_tokens = set()

        for page in range(MAX_PAGES):
            url = "https://monitoring.googleapis.com{}?{}".format(
                path, urlencode(params)
            )
            request = urllib.request.Request(url, method="GET")
            request.add_header("Authorization", "Bearer {}".format(token))
            request.add_header("Accept", "application/json")
            payload = self._get_json(request)
            self._validate_response(payload)
            page_series = payload.get("timeSeries", [])
            if not isinstance(page_series, list):
                raise MonitoringError("监控接口返回了无效数据。")
            series.extend(page_series)
            next_token = payload.get("nextPageToken")
            if next_token is None or next_token == "":
                return series
            if not isinstance(next_token, str) or next_token in seen_page_tokens:
                raise MonitoringError("监控接口分页异常，请稍后重试。")
            seen_page_tokens.add(next_token)
            params["pageToken"] = next_token
        raise MonitoringError("监控接口分页过多，已中止读取。")

    def _validate_response(self, payload):
        execution_errors = payload.get("executionErrors", [])
        unreachable = payload.get("unreachable", [])
        if not isinstance(execution_errors, list) or not all(
            isinstance(error, dict) for error in execution_errors
        ):
            raise MonitoringError("监控接口返回了无效的执行状态。")
        if not isinstance(unreachable, list) or not all(
            isinstance(location, str) for location in unreachable
        ):
            raise MonitoringError("监控接口返回了无效的可达性状态。")
        if execution_errors or unreachable:
            raise MonitoringError("Cloud Monitoring 未能返回完整指标，请稍后重试。")

    def _get_json(self, request):
        try:
            with self.opener(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise AuthError("监控请求未通过身份验证，请重新登录 gcloud。")
            if error.code == 403:
                raise MonitoringError("没有读取 Cloud Monitoring 的权限。")
            if error.code == 429:
                raise MonitoringError("监控请求过于频繁，请稍后重试。")
            raise MonitoringError("Cloud Monitoring 请求失败，请稍后重试。")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise MonitoringError("无法连接 Cloud Monitoring，请检查网络后重试。")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MonitoringError("监控响应过大，已中止读取。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            raise MonitoringError("监控接口返回了无效 JSON。")
        if not isinstance(payload, dict):
            raise MonitoringError("监控接口返回了无效数据。")
        return payload

    def _point_values(self, series):
        values = {}
        for item in series:
            if not isinstance(item, dict) or not isinstance(item.get("points"), list):
                continue
            for point in item["points"]:
                if not isinstance(point, dict):
                    continue
                interval = point.get("interval")
                raw_value = point.get("value")
                if not isinstance(interval, dict) or not isinstance(raw_value, dict):
                    continue
                timestamp = interval.get("endTime")
                if "doubleValue" in raw_value:
                    value = raw_value["doubleValue"]
                else:
                    value = raw_value.get("int64Value")
                if not isinstance(timestamp, str) or isinstance(value, bool):
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(number):
                    continue
                values.setdefault(timestamp, []).append(number)
        return values

    def _cpu_points(self, series):
        values = self._point_values(series)
        return [
            {"timestamp": timestamp, "value": (sum(items) / len(items)) * 100.0}
            for timestamp, items in sorted(values.items())
        ]

    def _network_points(self, series):
        values = self._point_values(series)
        return [
            {
                "timestamp": timestamp,
                "value": sum(items) * 8.0 / 60.0 / 1000000.0,
            }
            for timestamp, items in sorted(values.items())
        ]
