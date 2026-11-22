import hmac
import http.client
import json
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from .actions import InstanceActions
from .config import load_config
from .errors import (
    AuthError,
    ConfigError,
    ConfirmationError,
    GcloudError,
    KeelError,
    MonitoringError,
)
from .gcloud import Gcloud, instance_view
from .metrics import MonitoringClient
from .traffic_server import parse_history_limit


STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 4096
MAX_FORWARDED_BODY_BYTES = 16 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _links(config):
    project_query = urlencode({"project": config.project})
    return {
        "console": "https://console.cloud.google.com/compute/instancesDetail/zones/{}/instances/{}?{}".format(
            quote(config.zone, safe=""), quote(config.instance, safe=""), project_query
        ),
        "monitoring": "https://console.cloud.google.com/monitoring?" + project_query,
        "billing": "https://console.cloud.google.com/billing",
        "dashboard": config.dashboard_url,
    }


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, config_path, cloud_factory, monitoring_factory, traffic_forward_port):
        self.config_path = config_path
        self.cloud_factory = cloud_factory
        self.monitoring_factory = monitoring_factory
        if traffic_forward_port is not None and (
            isinstance(traffic_forward_port, bool)
            or not isinstance(traffic_forward_port, int)
            or not 1 <= traffic_forward_port <= 65535
        ):
            raise ValueError("traffic_forward_port out of range")
        if traffic_forward_port is not None and traffic_forward_port == port:
            raise ValueError("转发端口不能与管理后台监听端口相同。")
        self.traffic_forward_port = traffic_forward_port
        self.session_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), KeelRequestHandler)


class KeelRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Keel"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        hosts = {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        return hosts

    def _valid_host(self):
        hosts = self.headers.get_all("Host") or []
        return len(hosts) == 1 and hosts[0].lower() in self._allowed_hosts()

    def _valid_origin(self, required=False):
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1 or (required and not origins):
            return False
        expected_host = self.headers.get("Host", "").lower()
        if self.server.server_address[1] == 80 and expected_host.endswith(":80"):
            expected_host = expected_host[:-3]
        expected_origin = "http://" + expected_host
        if origins and origins[0] != expected_origin:
            return False
        fetch_sites = self.headers.get_all("Sec-Fetch-Site") or []
        if len(fetch_sites) > 1:
            return False
        if fetch_sites and fetch_sites[0] not in {"same-origin", "none"}:
            return False
        return True

    def _valid_csrf(self):
        tokens = self.headers.get_all("X-Keel-CSRF") or []
        return len(tokens) == 1 and tokens[0].isascii() and hmac.compare_digest(
            tokens[0], self.server.session_token
        )

    def _send_bytes(self, status, content, content_type, extra_headers=None):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _send_json(self, status, payload):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, content, "application/json; charset=utf-8")

    def _error(self, status, code, message):
        self._send_json(status, {"error": code, "message": message})

    def _guard_request(self, origin_required=False):
        if not self._valid_host():
            self._error(403, "invalid_host", "请求主机无效，已拒绝。")
            return False
        if not self._valid_origin(required=origin_required):
            self._error(403, "cross_origin", "已拒绝跨站请求。")
            return False
        return True

    def _handle_error(self, error):
        if isinstance(error, ConfigError):
            self._error(400, "configuration", str(error))
        elif isinstance(error, ConfirmationError):
            self._error(400, "confirmation", str(error))
        elif isinstance(error, AuthError):
            self._error(401, "not_logged_in", str(error))
        elif isinstance(error, (MonitoringError, GcloudError)):
            self._error(502, "cloud_error", str(error))
        elif isinstance(error, KeelError):
            self._error(400, "request", str(error))
        else:
            self._error(500, "internal", "本地控制台发生错误，请重试。")

    def do_GET(self):
        if not self._guard_request():
            return
        try:
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                self._send_static("index.html", "text/html; charset=utf-8")
            elif path == "/devices":
                self._devices()
            elif path == "/static/app.js":
                self._send_static("app.js", "text/javascript; charset=utf-8")
            elif path == "/static/style.css":
                self._send_static("style.css", "text/css; charset=utf-8")
            elif path == "/api/traffic":
                self._traffic()
            elif path == "/api/session":
                self._session()
            elif path == "/api/status":
                self._status()
            elif path == "/api/metrics":
                self._metrics()
            else:
                self._error(404, "not_found", "未找到请求的资源。")
        except Exception as error:
            self._handle_error(error)

    def _send_static(self, name, content_type):
        try:
            content = (STATIC_DIR / name).read_bytes()
        except OSError:
            self._error(404, "not_found", "未找到请求的资源。")
            return
        self._send_bytes(200, content, content_type)

    def _session(self):
        payload = {"csrfToken": self.server.session_token}
        try:
            config = load_config(self.server.config_path)
        except ConfigError:
            config = None
        if config is not None:
            payload["links"] = _links(config)
        self._send_json(200, payload)

    def _devices(self):
        config = load_config(self.server.config_path)
        self._send_bytes(
            302,
            b"",
            "text/plain; charset=utf-8",
            {"Location": config.dashboard_url or "/"},
        )

    def _status(self):
        config = load_config(self.server.config_path)
        cloud = self.server.cloud_factory()
        view = instance_view(config, cloud.describe(config))
        view.update({
            "project": config.project,
            "instance": config.instance,
            "updatedAt": _now(),
            "links": _links(config),
        })
        self._send_json(200, view)

    def _metrics(self):
        config = load_config(self.server.config_path)
        cloud = self.server.cloud_factory()
        instance = instance_view(config, cloud.describe(config))
        if not instance["id"] or not instance["id"].isdigit():
            raise GcloudError("实例数据缺少有效的数字 ID，无法查询指标。")
        client = self.server.monitoring_factory(cloud.access_token)
        self._send_json(200, client.collect(config, instance["id"]))

    def _traffic(self):
        try:
            limit = parse_history_limit(urlsplit(self.path).query)
        except ValueError:
            self._error(400, "invalid_range", "history limit 必须是 1 到 360 的整数。")
            return
        port = self.server.traffic_forward_port
        target = "/api/traffic?limit={}".format(limit)
        if port is None:
            config = load_config(self.server.config_path)
            port = config.traffic_proxy_port
            target = "http://192.0.2.80" + target
        if not port or port == self.server.server_address[1]:
            self._error(503, "traffic_unavailable", "请配置独立的本机代理或 SSH 转发端口。")
            return
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Host": "192.0.2.80",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
            content = response.read(MAX_FORWARDED_BODY_BYTES + 1)
            if (
                response.status != 200
                or content_type != "application/json"
                or len(content) > MAX_FORWARDED_BODY_BYTES
            ):
                raise ValueError("invalid forwarded response")
            payload = json.loads(content.decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("devices"), list)
                or not isinstance(payload.get("collector"), dict)
            ):
                raise ValueError("invalid forwarded payload")
            self._send_json(200, payload)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
            self._error(
                503,
                "traffic_unavailable",
                "暂时无法读取设备流量，请检查节点或 SSH 转发和只读服务。",
            )
        finally:
            connection.close()

    def do_POST(self):
        if not self._guard_request(origin_required=True):
            return
        if not self._valid_csrf():
            self._error(403, "csrf", "会话令牌无效，请重新打开操作对话框后重试。")
            return
        try:
            path = urlsplit(self.path).path
            if path not in {"/api/actions/start", "/api/actions/stop"}:
                self._error(404, "not_found", "未找到请求的资源。")
                return
            config = load_config(self.server.config_path)
            body = self._read_json()
            confirmation = body.get("confirmation")
            if any(
                body.get(field) != getattr(config, field)
                for field in ("project", "zone", "instance")
            ):
                raise ConfirmationError("目标项目、可用区或实例与最新配置不一致，未执行任何操作。")
            cloud = self.server.cloud_factory()
            actions = InstanceActions(config, cloud)
            if path.endswith("/start"):
                actions.start(confirmation)
                message = "gcloud 启动命令已成功结束；请刷新确认实际状态。"
            else:
                actions.stop(confirmation)
                message = "gcloud 停止命令已成功结束；请刷新确认实际状态。"
            self._send_json(200, {"success": True, "message": message})
        except Exception as error:
            self._handle_error(error)

    def _read_json(self):
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            raise ConfirmationError("请求必须包含有效的 Content-Length。")
        try:
            length = int(lengths[0])
        except ValueError:
            raise ConfirmationError("请求长度无效。")
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ConfirmationError("请求正文为空或过大。")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ConfirmationError("请求必须使用 application/json。")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            raise ConfirmationError("请求正文不是有效 JSON。")
        if not isinstance(body, dict):
            raise ConfirmationError("请求正文必须是 JSON 对象。")
        return body

    def do_HEAD(self):
        if self._guard_request():
            self._error(405, "method", "不支持此请求方法。")

    def do_OPTIONS(self):
        if self._guard_request():
            self._error(405, "method", "不支持此请求方法。")

    do_PUT = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_PATCH = do_OPTIONS


def create_server(
    port=8765, config_path=None, cloud_factory=Gcloud,
    monitoring_factory=MonitoringClient, traffic_forward_port=None,
):
    return ConsoleHTTPServer(
        port, config_path, cloud_factory, monitoring_factory, traffic_forward_port
    )


def serve(port=8765, traffic_forward_port=None):
    server = create_server(port=port, traffic_forward_port=traffic_forward_port)
    actual_port = server.server_address[1]
    print("Keel：http://127.0.0.1:{}".format(actual_port), flush=True)
    print("仅监听本机回环地址。按 Ctrl+C 停止控制台。", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
