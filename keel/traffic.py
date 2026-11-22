import argparse
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConfigError, KeelError, TrafficError


MAX_COUNTER = (1 << 63) - 1
MAX_DEVICES = 256
UNIT_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,120}\.service")


@dataclass(frozen=True)
class Device:
    port: int
    label: str
    shared: bool = False


@dataclass(frozen=True)
class Mapping:
    manager_socket: str
    manager_unit: str
    devices: tuple


def _config_error(message):
    raise ConfigError("设备流量映射无效：{}".format(message))


def load_mapping(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        raise ConfigError("缺少设备流量映射文件，请复制 deploy/traffic.example.json 并修改。")
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("设备流量映射必须是可读的 UTF-8 JSON 文件。")
    if not isinstance(data, dict):
        _config_error("顶层必须是 JSON 对象。")
    if set(data) - {"managerSocket", "managerUnit", "devices"}:
        _config_error("含有不支持的配置项；映射文件不能保存密钥。")

    manager_socket = data.get("managerSocket")
    if not isinstance(manager_socket, str) or not os.path.isabs(manager_socket):
        _config_error("managerSocket 必须是绝对 Unix socket 路径。")
    if "\x00" in manager_socket or len(manager_socket.encode("utf-8")) > 100:
        _config_error("managerSocket 路径无效或过长。")

    manager_unit = data.get("managerUnit")
    if manager_unit is not None and (
        not isinstance(manager_unit, str) or not UNIT_PATTERN.fullmatch(manager_unit)
    ):
        _config_error("managerUnit 必须是简单的 systemd service 单元名。")

    raw_devices = data.get("devices")
    if not isinstance(raw_devices, list) or not 1 <= len(raw_devices) <= MAX_DEVICES:
        _config_error("devices 必须包含 1 到 256 个端口映射。")
    devices = []
    ports = set()
    for item in raw_devices:
        if not isinstance(item, dict) or set(item) - {"port", "label", "shared"}:
            _config_error("每个设备只能包含 port、label 和 shared。")
        port = item.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            _config_error("设备端口必须在 1 到 65535 之间。")
        if port in ports:
            _config_error("设备端口不能重复。")
        label = item.get("label")
        if (
            not isinstance(label, str)
            or not label
            or label != label.strip()
            or len(label) > 80
            or any(ord(char) < 32 for char in label)
        ):
            _config_error("设备标签必须是 1 到 80 个字符且无控制字符。")
        shared = item.get("shared", False)
        if not isinstance(shared, bool):
            _config_error("shared 必须是布尔值。")
        ports.add(port)
        devices.append(Device(port=port, label=label, shared=shared))
    return Mapping(
        manager_socket=manager_socket,
        manager_unit=manager_unit,
        devices=tuple(devices),
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TrafficError("管理器响应含重复字段。")
        result[key] = value
    return result


def parse_manager_stat(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeError:
            raise TrafficError("管理器响应不是 UTF-8。")
    if not isinstance(raw, str) or len(raw) > 65536:
        raise TrafficError("管理器响应格式无效。")
    match = re.fullmatch(r"\s*stat\s*:\s*(\{.*\})\s*", raw, re.DOTALL)
    if not match:
        raise TrafficError("管理器响应不是 stat 计数器对象。")
    try:
        data = json.loads(match.group(1), object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError):
        raise TrafficError("管理器 stat 响应不是有效 JSON。")
    if not isinstance(data, dict):
        raise TrafficError("管理器 stat 响应必须是对象。")
    result = {}
    for port_text, total in data.items():
        if not isinstance(port_text, str) or not re.fullmatch(r"[0-9]+", port_text):
            raise TrafficError("管理器响应含无效端口。")
        port = int(port_text)
        if not 1 <= port <= 65535 or port in result:
            raise TrafficError("管理器响应含无效或重复端口。")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or not 0 <= total <= MAX_COUNTER
        ):
            raise TrafficError("管理器响应含无效字节计数。")
        result[port] = total
    return result


class ManagerClient:
    def __init__(self, manager_socket, runtime_dir, timeout=2.0):
        self.manager_socket = str(manager_socket)
        self.timeout = timeout
        self._socket = None
        self.local_path = None
        runtime = Path(runtime_dir)
        try:
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            details = runtime.lstat()
        except OSError:
            raise TrafficError("无法创建私有客户端 socket 目录。")
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
            raise TrafficError("客户端 socket 目录权限必须为 0700。")
        if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
            raise TrafficError("客户端 socket 目录必须由采集进程用户拥有。")
        self.runtime = runtime
        self._bind_socket()

    def _bind_socket(self):
        local_path = self.runtime / "c{}".format(secrets.token_hex(6))
        if len(str(local_path).encode("utf-8")) > 100:
            raise TrafficError("客户端 Unix socket 路径过长。")
        owned_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            owned_socket.bind(str(local_path))
            os.chmod(str(local_path), 0o600)
            owned_socket.settimeout(self.timeout)
        except OSError:
            owned_socket.close()
            try:
                local_path.unlink()
            except OSError:
                pass
            raise TrafficError("无法绑定私有客户端 Unix socket。")
        self._socket = owned_socket
        self.local_path = str(local_path)

    def ping(self):
        if self._socket is None:
            self._bind_socket()
        try:
            self._socket.connect(self.manager_socket)
            self._socket.send(b"ping")
            response = self._socket.recv(65537)
        except OSError:
            # A delayed reply must never satisfy a later poll.
            self.close()
            raise TrafficError("无法读取 ssmanager；请检查管理器 socket 和权限。")
        return parse_manager_stat(response)

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self.local_path is not None:
            try:
                Path(self.local_path).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self.local_path = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class ManagerIdentity:
    def __init__(
        self, manager_socket, manager_unit=None, proc_root=Path("/proc"),
        systemd_run_dir=Path("/run/systemd"),
    ):
        self.manager_socket = str(manager_socket)
        self.manager_unit = manager_unit
        self.proc_root = Path(proc_root)
        self.systemd_run_dir = Path(systemd_run_dir)
        self._socket_key = None
        self._socket_identity = None

    def current(self):
        try:
            details = os.stat(self.manager_socket)
        except OSError:
            raise TrafficError("无法确认管理器 socket 身份。")
        if not stat.S_ISSOCK(details.st_mode):
            raise TrafficError("管理器路径不是 Unix socket。")
        socket_key = (details.st_dev, details.st_ino, details.st_ctime_ns)
        suffix = "socket:{}:{}:{}".format(*socket_key)
        invocation = self._invocation_id()
        if invocation:
            return "systemd:{}".format(invocation)
        if socket_key != self._socket_key:
            self._socket_key = socket_key
            self._socket_identity = self._proc_identity() or suffix
        return self._socket_identity

    def _invocation_id(self):
        if not self.manager_unit:
            return None
        path = self.systemd_run_dir / "units" / ("invocation:" + self.manager_unit)
        try:
            invocation = os.readlink(str(path))
        except OSError:
            return None
        if not re.fullmatch(r"[0-9a-f]{32}", invocation):
            return None
        return invocation

    def _proc_identity(self):
        if not self.proc_root.is_absolute():
            return None
        try:
            lines = (self.proc_root / "net" / "unix").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return None
        for line in lines:
            fields = line.split(None, 7)
            if len(fields) >= 8 and fields[7] == self.manager_socket and fields[6].isdigit():
                pid_start = self._find_socket_owner(fields[6])
                if pid_start:
                    return "proc:{}:{}".format(*pid_start)
        return None

    def _find_socket_owner(self, inode):
        target = "socket:[{}]".format(inode)
        try:
            processes = sorted(
                (entry for entry in os.scandir(str(self.proc_root)) if entry.name.isdigit()),
                key=lambda entry: int(entry.name),
            )
        except OSError:
            return None
        for process in processes:
            try:
                descriptors = list(os.scandir(str(Path(process.path) / "fd")))
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    if os.readlink(descriptor.path) != target:
                        continue
                    raw = (Path(process.path) / "stat").read_text(encoding="utf-8")
                    fields = raw[raw.rfind(")") + 1:].split()
                    start_time = int(fields[19])
                    return int(process.name), start_time
                except (OSError, UnicodeError, ValueError, IndexError):
                    continue
        return None


def _utc_timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


class TrafficCollector:
    def __init__(
        self, mapping, database_path, history_limit=360, stale_after=30.0,
        wall_clock=None, monotonic_clock=None,
    ):
        self.mapping = mapping
        self.history_limit = history_limit
        self.stale_after = stale_after
        if not 1 <= history_limit <= 100000:
            raise ValueError("history_limit out of range")
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after out of range")
        self.wall_clock = wall_clock or time.time
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.baselines = {}
        try:
            import fcntl
        except ImportError:
            raise TrafficError("服务端采集需要支持文件锁的 Unix 主机。")
        self.manager_identity = None
        self.interrupted = True
        self.failures = 0
        database_path = Path(database_path)
        self._lock_descriptor = None
        try:
            details = database_path.lstat()
            if not stat.S_ISREG(details.st_mode):
                raise TrafficError("流量数据库必须是普通文件，不能是符号链接。")
            if hasattr(os, "geteuid") and details.st_uid != os.geteuid():
                raise TrafficError("流量数据库必须由采集进程用户拥有。")
        except FileNotFoundError:
            pass
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(database_path), flags, 0o600)
            os.close(descriptor)
            os.chmod(str(database_path), 0o600)
            lock_path = database_path.with_name(database_path.name + ".lock")
            self._lock_descriptor = os.open(str(lock_path), flags, 0o600)
            try:
                fcntl.flock(
                    self._lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except OSError:
                raise TrafficError("已有采集进程正在使用此流量数据库。")
            os.chmod(str(lock_path), 0o600)
            self.connection = sqlite3.connect(str(database_path), timeout=5)
        except TrafficError:
            self.close()
            raise
        except (OSError, sqlite3.Error):
            self.close()
            raise TrafficError("无法安全打开流量 SQLite 数据库。")
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS samples (
                    port INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    observed_epoch REAL NOT NULL,
                    manager_total INTEGER NOT NULL,
                    tracked_total INTEGER NOT NULL,
                    rate_bytes_per_second REAL,
                    rate_reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS samples_port_observed
                    ON samples(port, observed_epoch);
                CREATE TABLE IF NOT EXISTS collector_counters (
                    port INTEGER PRIMARY KEY,
                    manager_identity TEXT NOT NULL,
                    manager_total INTEGER NOT NULL,
                    tracked_total INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_status (
                    port INTEGER PRIMARY KEY,
                    last_attempt_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS collector_status (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL,
                    stale_after REAL NOT NULL
                );
            """)
            self.connection.commit()
        except Exception:
            self.close()
            raise

    def collect_once(self, client, identity_provider):
        try:
            return self._collect_once(client, identity_provider)
        except TrafficError as error:
            failures = self.failures + 1
            timestamp = _utc_timestamp(self.wall_clock())
            message = str(error)[:200]
            with self.connection:
                for device in self.mapping.devices:
                    self.connection.execute(
                        "INSERT INTO device_status (port, last_attempt_at, last_error) "
                        "VALUES (?, ?, ?) ON CONFLICT(port) DO UPDATE SET "
                        "last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error",
                        (device.port, timestamp, message),
                    )
                self.connection.execute(
                    "INSERT INTO collector_status (id, last_attempt_at, last_success_at, "
                    "last_error, consecutive_failures, stale_after) VALUES (1, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at, "
                    "last_error=excluded.last_error, "
                    "consecutive_failures=excluded.consecutive_failures, "
                    "stale_after=excluded.stale_after",
                    (timestamp, message, failures, self.stale_after),
                )
            self.baselines.clear()
            self.manager_identity = None
            self.interrupted = True
            self.failures = failures
            return False

    def _collect_once(self, client, identity_provider):
        identity = identity_provider.current()
        counters = client.ping()
        if identity_provider.current() != identity:
            raise TrafficError("采集期间管理器发生重启，请重试。")
        epoch = self.wall_clock()
        monotonic = self.monotonic_clock()
        timestamp = _utc_timestamp(epoch)
        interrupted = self.interrupted
        baselines = dict(self.baselines)

        with self.connection:
            for device in self.mapping.devices:
                if device.port not in counters:
                    baselines.pop(device.port, None)
                    self.connection.execute(
                        "INSERT INTO device_status (port, last_attempt_at, last_error) "
                        "VALUES (?, ?, ?) ON CONFLICT(port) DO UPDATE SET "
                        "last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error",
                        (device.port, timestamp, "管理器响应缺少此端口，已重置速率基线。"),
                    )
                    continue
                manager_total = counters[device.port]
                previous = self.connection.execute(
                    "SELECT manager_identity, manager_total, tracked_total "
                    "FROM collector_counters WHERE port=?",
                    (device.port,),
                ).fetchone()
                if previous is None:
                    legacy = self.connection.execute(
                        "SELECT tracked_total FROM samples WHERE port=? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (device.port,),
                    ).fetchone()
                    previous_tracked = legacy[0] if legacy else 0
                else:
                    previous_tracked = previous[2]
                baseline = baselines.get(device.port)
                rate = None
                tracked_total = previous_tracked
                if previous is None:
                    reason = "baseline"
                elif previous[0] != identity or manager_total < previous[1]:
                    tracked_total += manager_total
                    reason = "reset"
                else:
                    tracked_total += manager_total - previous[1]
                    reason = "interrupted"
                    if not interrupted and baseline is not None:
                        baseline_total, baseline_monotonic, baseline_identity = baseline
                        elapsed = monotonic - baseline_monotonic
                        if (
                            baseline_identity == identity
                            and manager_total >= baseline_total
                            and 0 < elapsed <= self.stale_after
                        ):
                            rate = (manager_total - baseline_total) / elapsed
                            reason = "ok"
                baselines[device.port] = (manager_total, monotonic, identity)
                self.connection.execute(
                    "INSERT INTO samples (port, observed_at, observed_epoch, manager_total, "
                    "tracked_total, rate_bytes_per_second, rate_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        device.port, timestamp, epoch, manager_total, tracked_total, rate, reason,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO collector_counters "
                    "(port, manager_identity, manager_total, tracked_total) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(port) DO UPDATE SET "
                    "manager_identity=excluded.manager_identity, "
                    "manager_total=excluded.manager_total, "
                    "tracked_total=excluded.tracked_total",
                    (device.port, identity, manager_total, tracked_total),
                )
                self.connection.execute(
                    "DELETE FROM samples WHERE port=? AND rowid < ("
                    "SELECT rowid FROM samples WHERE port=? ORDER BY rowid DESC "
                    "LIMIT 1 OFFSET ?)",
                    (device.port, device.port, self.history_limit - 1),
                )
                self.connection.execute(
                    "INSERT INTO device_status (port, last_attempt_at, last_error) "
                    "VALUES (?, ?, NULL) ON CONFLICT(port) DO UPDATE SET "
                    "last_attempt_at=excluded.last_attempt_at, last_error=NULL",
                    (device.port, timestamp),
                )
            self.connection.execute(
                "INSERT INTO collector_status (id, last_attempt_at, last_success_at, "
                "last_error, consecutive_failures, stale_after) VALUES (1, ?, ?, NULL, 0, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at, "
                "last_success_at=excluded.last_success_at, last_error=NULL, "
                "consecutive_failures=0, stale_after=excluded.stale_after",
                (timestamp, timestamp, self.stale_after),
            )
        self.baselines = baselines
        self.manager_identity = identity
        self.interrupted = False
        self.failures = 0
        return True

    def close(self):
        try:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()
        finally:
            descriptor = self._lock_descriptor
            self._lock_descriptor = None
            if descriptor is not None:
                os.close(descriptor)


def backoff_delay(poll_interval, maximum, failures):
    if failures <= 1:
        return min(poll_interval, maximum)
    exponent = min(failures - 1, 30)
    return min(maximum, poll_interval * (2 ** exponent))


def _bounded_number(minimum, maximum, integer=False):
    def parse(raw):
        try:
            value = int(raw) if integer else float(raw)
        except ValueError:
            raise argparse.ArgumentTypeError("数值无效")
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                "数值必须在 {} 到 {} 之间".format(minimum, maximum)
            )
        return value
    return parse


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m keel.traffic",
        description="Keel ssmanager 设备端口流量采集与只读面板。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="持续采集 ssmanager 端口字节计数")
    collect.add_argument("--mapping", required=True, help="可信静态端口／标签映射 JSON")
    collect.add_argument("--database", required=True, help="私有 SQLite 数据库路径")
    collect.add_argument("--runtime-dir", default="/run/keel-traffic", help="0700 私有运行目录")
    collect.add_argument("--poll-interval", type=_bounded_number(1.0, 3600.0), default=10.0)
    collect.add_argument("--stale-after", type=_bounded_number(1.0, 86400.0), default=30.0)
    collect.add_argument("--history", type=_bounded_number(10, 100000, integer=True), default=360)
    collect.add_argument("--manager-timeout", type=_bounded_number(0.1, 30.0), default=2.0)
    collect.add_argument("--max-backoff", type=_bounded_number(1.0, 3600.0), default=60.0)
    collect.add_argument("--once", action="store_true", help="只采集一次，用于诊断")

    serve = commands.add_parser("serve", help="启动仅回环地址的只读设备流量面板")
    serve.add_argument("--mapping", required=True, help="可信静态端口／标签映射 JSON")
    serve.add_argument("--database", required=True, help="以只读方式打开的 SQLite 数据库")
    serve.add_argument(
        "--port", type=_bounded_number(1, 65535, integer=True), default=8766,
        help="回环监听端口（默认 8766）",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        mapping = load_mapping(args.mapping)
        if args.command == "serve":
            from .traffic_server import serve
            serve(mapping=mapping, database_path=args.database, port=args.port)
            return 0
        if args.stale_after < args.poll_interval:
            print("错误：stale-after 不能小于 poll-interval。", file=sys.stderr)
            return 2
        os.umask(0o077)
        collector = TrafficCollector(
            mapping,
            args.database,
            history_limit=args.history,
            stale_after=args.stale_after,
        )
        identity = ManagerIdentity(mapping.manager_socket, mapping.manager_unit)
        try:
            with ManagerClient(
                mapping.manager_socket, args.runtime_dir, timeout=args.manager_timeout
            ) as client:
                while True:
                    success = collector.collect_once(client, identity)
                    if args.once:
                        return 0 if success else 1
                    if not success:
                        delay = backoff_delay(
                            args.poll_interval, args.max_backoff, collector.failures
                        )
                        print("采集失败；将在 {:.1f} 秒后重试。".format(delay), file=sys.stderr)
                    else:
                        delay = args.poll_interval
                    time.sleep(delay)
        finally:
            collector.close()
    except (KeelError, OSError, sqlite3.Error) as error:
        message = str(error) if isinstance(error, KeelError) else "本地流量存储或 socket 操作失败。"
        print("错误：{}".format(message), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        sys.exit(130)
