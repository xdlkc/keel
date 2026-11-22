import json
import os
import socket
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from keel.errors import ConfigError
from keel.traffic import (
    Device,
    ManagerClient,
    ManagerIdentity,
    Mapping,
    TrafficCollector,
    TrafficError,
    backoff_delay,
    build_parser,
    load_mapping,
    parse_manager_stat,
)


class MappingAndProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "traffic.json"

    def write_mapping(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_loads_static_dedicated_and_legacy_shared_mapping(self):
        self.write_mapping({
            "managerSocket": "/run/shadowsocks-rust/manager.sock",
            "managerUnit": "shadowsocks-rust.service",
            "devices": [
                {"port": 8388, "label": "Phone A"},
                {"port": 8390, "label": "Legacy shared", "shared": True},
            ],
        })
        mapping = load_mapping(self.path)
        self.assertEqual(mapping.manager_socket, "/run/shadowsocks-rust/manager.sock")
        self.assertEqual(mapping.manager_unit, "shadowsocks-rust.service")
        self.assertEqual([(device.port, device.label, device.shared) for device in mapping.devices], [
            (8388, "Phone A", False),
            (8390, "Legacy shared", True),
        ])

    def test_mapping_rejects_secrets_duplicates_and_invalid_ranges(self):
        invalid = [
            {"managerSocket": "/run/manager.sock", "devices": [], "password": "secret"},
            {"managerSocket": "relative.sock", "devices": [{"port": 1, "label": "A"}]},
            {"managerSocket": "/run/manager.sock", "devices": []},
            {"managerSocket": "/run/manager.sock", "devices": [
                {"port": 0, "label": "A"},
            ]},
            {"managerSocket": "/run/manager.sock", "devices": [
                {"port": 65536, "label": "A"},
            ]},
            {"managerSocket": "/run/manager.sock", "devices": [
                {"port": True, "label": "A"},
            ]},
            {"managerSocket": "/run/manager.sock", "devices": [
                {"port": 8388, "label": "A"},
                {"port": 8388, "label": "B"},
            ]},
            {"managerSocket": "/run/manager.sock", "devices": [
                {"port": 8388, "label": " A\n"},
            ]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.write_mapping(value)
                with self.assertRaises(ConfigError):
                    load_mapping(self.path)

    def test_stat_parser_accepts_whitespace_and_returns_integer_counters(self):
        self.assertEqual(parse_manager_stat('  stat  :  { "8388" : 1200, "8390": 0 }\n'), {
            8388: 1200,
            8390: 0,
        })

    def test_stat_parser_rejects_malformed_or_unsafe_values(self):
        invalid = [
            "pong",
            "stat: []",
            'stat: {"8388": -1}',
            'stat: {"8388": 1.5}',
            'stat: {"8388": true}',
            'stat: {"0": 1}',
            'stat: {"65536": 1}',
            'stat: {"8388": 1} trailing',
            'stat: {"8388": 1, "8388": 2}',
            'stat: {"8388": 9223372036854775808}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(TrafficError):
                    parse_manager_stat(raw)


class ManagerClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.manager_path = self.root / "manager.sock"
        self.manager = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.manager.bind(str(self.manager_path))
        self.manager.settimeout(2)
        self.addCleanup(self.manager.close)

    def test_client_owns_private_socket_and_sends_only_ping(self):
        runtime = self.root / "r"
        received = []
        client_addresses = []

        def respond():
            data, address = self.manager.recvfrom(4096)
            received.append(data)
            client_addresses.append(address)
            self.manager.sendto(b'stat: {"8388": 42}', address)

        thread = threading.Thread(target=respond)
        thread.start()
        with ManagerClient(self.manager_path, runtime, timeout=1) as client:
            self.assertEqual(client.ping(), {8388: 42})
            owned_path = Path(client.local_path)
            self.assertEqual(stat.S_IMODE(os.stat(owned_path).st_mode), 0o600)
        thread.join(timeout=2)
        self.assertEqual(received, [b"ping"])
        self.assertEqual(Path(client_addresses[0]), owned_path)
        self.assertFalse(owned_path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(runtime).st_mode), 0o700)

    def test_timeout_retires_reply_address_before_retry(self):
        with ManagerClient(self.manager_path, self.root / "r", timeout=0.1) as client:
            with self.assertRaises(TrafficError):
                client.ping()
            _, old_address = self.manager.recvfrom(256)
            self.assertFalse(Path(old_address).exists())

            def reply():
                _, fresh = self.manager.recvfrom(256)
                self.manager.sendto(b'stat: {"8388": 99}', fresh)

            thread = threading.Thread(target=reply)
            thread.start()
            self.assertEqual(client.ping(), {8388: 99})
            thread.join(2)
            self.assertNotEqual(client.local_path, old_address)

    def test_client_rejects_group_or_world_accessible_runtime_directory(self):
        runtime = self.root / "loose-runtime"
        runtime.mkdir(mode=0o777)
        runtime.chmod(0o777)
        with self.assertRaises(TrafficError):
            ManagerClient(self.manager_path, runtime)


class ManagerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.manager_path = self.root / "manager.sock"
        self.manager = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.manager.bind(str(self.manager_path))
        self.addCleanup(self.manager.close)

    def test_systemd_invocation_id_changes_manager_generation(self):
        units = self.root / "systemd" / "units"
        units.mkdir(parents=True)
        invocation = units / "invocation:shadowsocks-rust.service"
        first = "a" * 32
        second = "b" * 32
        invocation.symlink_to(first)
        identity = ManagerIdentity(
            self.manager_path,
            "shadowsocks-rust.service",
            proc_root=self.root / "proc",
            systemd_run_dir=self.root / "systemd",
        )
        self.assertIn(first, identity.current())
        invocation.unlink()
        invocation.symlink_to(second)
        self.assertIn(second, identity.current())

    def test_proc_pid_start_generation_is_resolved_from_socket_inode(self):
        proc = self.root / "proc"
        fd_dir = proc / "123" / "fd"
        fd_dir.mkdir(parents=True)
        (proc / "net").mkdir()
        inode = os.stat(self.manager_path).st_ino + 739
        (proc / "net" / "unix").write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "000: 0002 0000 0000 0002 01 {} {}\n".format(inode, self.manager_path),
            encoding="utf-8",
        )
        (fd_dir / "7").symlink_to("socket:[{}]".format(inode))
        fields_after_comm = ["0"] * 18 + ["4242"]
        (proc / "123" / "stat").write_text(
            "123 (ssmanager) S " + " ".join(fields_after_comm), encoding="utf-8"
        )
        identity = ManagerIdentity(
            self.manager_path,
            proc_root=proc,
            systemd_run_dir=self.root / "systemd",
        )
        self.assertIn("123:4242", identity.current())


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "traffic.sqlite3"
        self.mapping = Mapping(
            manager_socket="/run/manager.sock",
            manager_unit=None,
            devices=(Device(8388, "Phone A"),),
        )

    def rows(self):
        connection = sqlite3.connect(str(self.database))
        self.addCleanup(connection.close)
        return connection.execute(
            "SELECT manager_total, tracked_total, rate_bytes_per_second, "
            "rate_reason, observed_at FROM samples ORDER BY rowid"
        ).fetchall()

    def test_first_baseline_and_real_elapsed_combined_rate(self):
        wall_times = iter([1000.0, 1005.0])
        monotonic_times = iter([100.0, 103.0])
        collector = TrafficCollector(
            self.mapping,
            self.database,
            wall_clock=lambda: next(wall_times),
            monotonic_clock=lambda: next(monotonic_times),
        )

        class Client:
            def __init__(self):
                self.responses = iter([{8388: 1000}, {8388: 1500}])

            def ping(self):
                return next(self.responses)

        class Identity:
            def current(self):
                return "generation-a"

        client = Client()
        self.assertTrue(collector.collect_once(client, Identity()))
        self.assertTrue(collector.collect_once(client, Identity()))
        rows = self.rows()
        self.assertEqual(rows[0][:4], (1000, 0, None, "baseline"))
        self.assertEqual(rows[1][0:2], (1500, 500))
        self.assertAlmostEqual(rows[1][2], 500 / 3)
        self.assertEqual(rows[1][3], "ok")
        self.assertTrue(all(row[4].endswith("Z") for row in rows))

    def test_missing_reset_and_restart_never_fabricate_delta_or_rate(self):
        wall = [2000.0]
        monotonic = [50.0]
        collector = TrafficCollector(
            self.mapping,
            self.database,
            wall_clock=lambda: wall[0],
            monotonic_clock=lambda: monotonic[0],
        )

        class Client:
            response = None

            def ping(self):
                return self.response

        class Identity:
            value = "generation-a"

            def current(self):
                return self.value

        client = Client()
        identity = Identity()
        steps = [
            ({8388: 100}, "generation-a", 0, None, "baseline"),
            ({8388: 150}, "generation-a", 50, 50.0, "ok"),
            ({}, "generation-a", None, None, None),
            ({8388: 200}, "generation-a", 100, None, "interrupted"),
            ({8388: 250}, "generation-a", 150, 50.0, "ok"),
            ({8388: 275}, "generation-b", 425, None, "reset"),
            ({8388: 300}, "generation-b", 450, 25.0, "ok"),
            ({8388: 10}, "generation-b", 460, None, "reset"),
            ({8388: 30}, "generation-b", 480, 20.0, "ok"),
        ]
        for counters, generation, tracked, rate, reason in steps:
            client.response = counters
            identity.value = generation
            self.assertTrue(collector.collect_once(client, identity))
            wall[0] += 1
            monotonic[0] += 1
            if counters:
                row = self.rows()[-1]
                self.assertEqual(row[1], tracked)
                self.assertEqual(row[2], rate)
                self.assertEqual(row[3], reason)
        self.assertEqual(len(self.rows()), 8)
        collector.close()

        restarted = TrafficCollector(
            self.mapping,
            self.database,
            wall_clock=lambda: wall[0],
            monotonic_clock=lambda: monotonic[0],
        )
        client.response = {8388: 50}
        self.assertTrue(restarted.collect_once(client, identity))
        row = self.rows()[-1]
        self.assertEqual(row[1:4], (500, None, "interrupted"))

    def test_ping_failure_is_recorded_and_interrupts_next_rate(self):
        wall = [3000.0]
        monotonic = [10.0]
        collector = TrafficCollector(
            self.mapping,
            self.database,
            wall_clock=lambda: wall[0],
            monotonic_clock=lambda: monotonic[0],
        )

        class Client:
            fail = False
            total = 100

            def ping(self):
                if self.fail:
                    raise TrafficError("manager unavailable")
                return {8388: self.total}

        class Identity:
            def current(self):
                return "generation-a"

        client = Client()
        identity = Identity()
        self.assertTrue(collector.collect_once(client, identity))
        client.fail = True
        self.assertFalse(collector.collect_once(client, identity))
        status = collector.connection.execute(
            "SELECT consecutive_failures, last_error FROM collector_status WHERE id=1"
        ).fetchone()
        self.assertEqual(status[0], 1)
        self.assertIn("manager unavailable", status[1])
        self.assertEqual(len(self.rows()), 1)
        client.fail = False
        client.total = 150
        wall[0] += 2
        monotonic[0] += 2
        self.assertTrue(collector.collect_once(client, identity))
        self.assertEqual(self.rows()[-1][1:4], (50, None, "interrupted"))

    def test_history_is_trimmed_per_device(self):
        wall = [4000.0]
        monotonic = [20.0]
        collector = TrafficCollector(
            self.mapping,
            self.database,
            history_limit=3,
            wall_clock=lambda: wall[0],
            monotonic_clock=lambda: monotonic[0],
        )

        class Client:
            total = 0

            def ping(self):
                self.total += 10
                return {8388: self.total}

        class Identity:
            def current(self):
                return "generation-a"

        client = Client()
        for _ in range(5):
            self.assertTrue(collector.collect_once(client, Identity()))
            wall[0] += 1
            monotonic[0] += 1
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row[0] for row in rows], [30, 40, 50])
        self.assertEqual(rows[-1][1], 40)

    def test_long_interruption_keeps_observed_delta_but_drops_rate(self):
        collector = TrafficCollector(
            self.mapping,
            self.database,
            stale_after=2,
            wall_clock=lambda: 5000.0,
            monotonic_clock=lambda: self.monotonic,
        )

        class Client:
            total = 100

            def ping(self):
                return {8388: self.total}

        class Identity:
            def current(self):
                return "generation-a"

        self.monotonic = 10.0
        client = Client()
        collector.collect_once(client, Identity())
        self.monotonic = 15.0
        client.total = 150
        collector.collect_once(client, Identity())
        self.assertEqual(self.rows()[-1][1:4], (50, None, "interrupted"))

    def test_database_is_private_and_symlinks_are_rejected(self):
        collector = TrafficCollector(self.mapping, self.database)
        self.addCleanup(collector.close)
        self.assertEqual(stat.S_IMODE(os.stat(self.database).st_mode), 0o600)
        collector.close()
        target = Path(self.temp_dir.name) / "target.sqlite3"
        target.touch()
        self.database.unlink()
        self.database.symlink_to(target)
        with self.assertRaises(TrafficError):
            TrafficCollector(self.mapping, self.database)


class TrafficCliTests(unittest.TestCase):
    def test_collect_defaults_and_backoff_are_bounded(self):
        args = build_parser().parse_args([
            "collect", "--mapping", "traffic.json", "--database", "traffic.sqlite3",
        ])
        self.assertEqual(args.poll_interval, 10.0)
        self.assertEqual(args.stale_after, 30.0)
        self.assertEqual(args.history, 360)
        self.assertEqual(args.runtime_dir, "/run/keel-traffic")
        self.assertEqual(backoff_delay(10, 60, 1), 10)
        self.assertEqual(backoff_delay(10, 60, 2), 20)
        self.assertEqual(backoff_delay(10, 60, 3), 40)
        self.assertEqual(backoff_delay(10, 60, 8), 60)

    def test_cli_rejects_invalid_ranges(self):
        base = ["collect", "--mapping", "traffic.json", "--database", "traffic.sqlite3"]
        invalid = [
            ["--poll-interval", "0"],
            ["--poll-interval", "3601"],
            ["--stale-after", "0"],
            ["--history", "9"],
            ["--history", "100001"],
            ["--manager-timeout", "0"],
            ["--max-backoff", "0"],
        ]
        for extra in invalid:
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(base + extra)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["serve", "--mapping", "x", "--database", "y", "--port", "0"])


if __name__ == "__main__":
    unittest.main()
