import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .traffic import _utc_timestamp


STATIC_DIR = Path(__file__).with_name("static")
DEFAULT_HISTORY_LIMIT = 120
MAX_HISTORY_LIMIT = 360


def parse_history_limit(query):
    if query == "":
        return DEFAULT_HISTORY_LIMIT
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("invalid query")
    if not values:
        return DEFAULT_HISTORY_LIMIT
    if set(values) != {"limit"} or len(values["limit"]) != 1:
        raise ValueError("invalid query")
    raw = values["limit"][0]
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("invalid limit")
    limit = int(raw)
    if not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ValueError("invalid limit")
    return limit


def load_snapshot(mapping, database_path, history_limit, now):
    uri = Path(database_path).absolute().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        status_row = connection.execute(
            "SELECT last_attempt_at, last_success_at, last_error, "
            "consecutive_failures, stale_after FROM collector_status WHERE id=1"
        ).fetchone()
        stale_after = status_row[4] if status_row else 30.0
        collector = {
            "lastAttemptAt": status_row[0] if status_row else None,
            "lastSuccessAt": status_row[1] if status_row else None,
            "error": status_row[2] if status_row else "采集器尚未写入状态。",
            "consecutiveFailures": status_row[3] if status_row else 0,
            "staleAfterSeconds": stale_after,
        }
        devices = []
        for device in mapping.devices:
            latest = connection.execute(
                "SELECT observed_at, observed_epoch, tracked_total, "
                "rate_bytes_per_second, rate_reason FROM samples "
                "WHERE port=? ORDER BY rowid DESC LIMIT 1",
                (device.port,),
            ).fetchone()
            device_status = connection.execute(
                "SELECT last_error FROM device_status WHERE port=?",
                (device.port,),
            ).fetchone()
            history_rows = connection.execute(
                "SELECT observed_at, tracked_total, rate_bytes_per_second, rate_reason "
                "FROM samples WHERE port=? ORDER BY rowid DESC LIMIT ?",
                (device.port, history_limit),
            ).fetchall()
            history_rows.reverse()
            history = [
                {
                    "timestamp": row[0],
                    "totalBytes": row[1],
                    "rateBytesPerSecond": row[2],
                    "rateUnavailableReason": None if row[2] is not None else row[3],
                }
                for row in history_rows
            ]
            stale = bool(latest and now - latest[1] > stale_after)
            poll_failed = bool(device_status and device_status[0])
            rate = latest[3] if latest and not stale and not poll_failed else None
            reason = None
            if latest:
                if poll_failed:
                    reason = "interrupted"
                elif stale:
                    reason = "stale"
                elif rate is None:
                    reason = latest[4]
            devices.append({
                "port": device.port,
                "label": device.label,
                "shared": device.shared,
                "observedAt": latest[0] if latest else None,
                "stale": stale,
                "totalBytes": latest[2] if latest else None,
                "rateBytesPerSecond": rate,
                "rateUnavailableReason": reason,
                "error": device_status[0] if device_status else None,
                "history": history,
            })
        return {
            "generatedAt": _utc_timestamp(now),
            "collector": collector,
            "devices": devices,
            "measurement": {
                "source": "ssmanager",
                "direction": "combined",
                "totalScope": "tracked-since-monitoring",
            },
        }
    finally:
        connection.close()


class TrafficHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mapping, database_path, port, now=None):
        self.mapping = mapping
        self.database_path = Path(database_path)
        self.now = now or time.time
        super().__init__(("127.0.0.1", port), TrafficRequestHandler)


class TrafficRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KeelTraffic"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def _valid_host(self):
        port = self.server.server_address[1]
        hosts = {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}
        # A dedicated service-UID route maps this virtual host to the read-only listener.
        hosts.update({"192.0.2.80", "192.0.2.80:80"})
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        values = self.headers.get_all("Host") or []
        return len(values) == 1 and values[0].lower() in hosts

    def _valid_origin(self):
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1:
            return False
        expected_host = self.headers.get("Host", "").lower()
        if self.server.server_address[1] == 80 and expected_host.endswith(":80"):
            expected_host = expected_host[:-3]
        if origins and origins[0] != "http://" + expected_host:
            return False
        fetch_sites = self.headers.get_all("Sec-Fetch-Site") or []
        return len(fetch_sites) <= 1 and (
            not fetch_sites or fetch_sites[0] in {"same-origin", "none"}
        )

    def _guard_request(self):
        if not self._valid_host():
            self._error(403, "invalid_host", "请求主机无效，已拒绝。")
            return False
        if not self._valid_origin():
            self._error(403, "cross_origin", "已拒绝跨站请求。")
            return False
        return True

    def _send_bytes(self, status, content, content_type):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _send_json(self, status, payload):
        content = json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self._send_bytes(status, content, "application/json; charset=utf-8")

    def _error(self, status, code, message):
        self._send_json(status, {"error": code, "message": message})

    def _send_static(self, name, content_type):
        try:
            content = (STATIC_DIR / name).read_bytes()
        except OSError:
            self._error(404, "not_found", "未找到请求的资源。")
            return
        self._send_bytes(200, content, content_type)

    def do_GET(self):
        if not self._guard_request():
            return
        path = urlsplit(self.path).path
        if path in {"/", "/devices"}:
            self._send_static("devices.html", "text/html; charset=utf-8")
            return
        static_files = {
            "/static/style.css": ("style.css", "text/css; charset=utf-8"),
            "/static/traffic.css": ("traffic.css", "text/css; charset=utf-8"),
            "/static/devices.js": ("devices.js", "text/javascript; charset=utf-8"),
        }
        if path in static_files:
            self._send_static(*static_files[path])
            return
        if path != "/api/traffic":
            self._error(404, "not_found", "未找到请求的资源。")
            return
        try:
            limit = parse_history_limit(urlsplit(self.path).query)
        except ValueError:
            self._error(400, "invalid_range", "history limit 必须是 1 到 360 的整数。")
            return
        try:
            snapshot = load_snapshot(
                self.server.mapping,
                self.server.database_path,
                limit,
                self.server.now(),
            )
            self._send_json(200, snapshot)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self._error(
                503,
                "collector_unavailable",
                "暂时无法读取采集数据库，请确认采集器已运行且数据文件权限正确。",
            )

    do_HEAD = do_GET

    def do_OPTIONS(self):
        if self._guard_request():
            self._error(405, "method", "只读面板不支持此请求方法。")

    do_POST = do_OPTIONS
    do_PUT = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_PATCH = do_OPTIONS


def create_server(mapping, database_path, port=8766, now=None):
    return TrafficHTTPServer(mapping, database_path, port, now=now)


def serve(mapping, database_path, port=8766):
    server = create_server(mapping, database_path, port=port)
    print("Keel 设备流量：http://127.0.0.1:{}/devices".format(
        server.server_address[1]
    ), flush=True)
    print("只读服务仅监听本机回环地址；不包含实例管理或 gcloud 操作。", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
