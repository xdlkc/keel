import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel.traffic import Device, Mapping, TrafficCollector, TrafficError, main


class Client:
    def __init__(self):
        self.counters = {}

    def ping(self):
        if isinstance(self.counters, Exception):
            raise self.counters
        return self.counters


class Identity:
    def __init__(self, value="generation-a"):
        self.value = value

    def current(self):
        return self.value


class TrafficContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "traffic.sqlite3"
        self.mapping = Mapping(
            manager_socket="/run/manager.sock",
            manager_unit=None,
            devices=(Device(8388, "Phone A"),),
        )
        self.wall = [1000.0]
        self.monotonic = [100.0]
        self.client = Client()
        self.identity = Identity()

    def open_collector(self):
        collector = TrafficCollector(
            self.mapping,
            self.database,
            wall_clock=lambda: self.wall[0],
            monotonic_clock=lambda: self.monotonic[0],
        )
        self.addCleanup(collector.close)
        return collector

    def advance(self):
        self.wall[0] += 1.0
        self.monotonic[0] += 1.0

    def rows(self):
        connection = sqlite3.connect(str(self.database))
        self.addCleanup(connection.close)
        return connection.execute(
            "SELECT manager_total, tracked_total, rate_bytes_per_second, rate_reason "
            "FROM samples ORDER BY rowid"
        ).fetchall()

    def test_recovers_delta_across_failure_and_collector_restart(self):
        collector = self.open_collector()
        self.client.counters = {8388: 100}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        self.advance()
        self.client.counters = {8388: 130}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        self.advance()
        self.client.counters = TrafficError("temporary outage")
        self.assertFalse(collector.collect_once(self.client, self.identity))
        collector.close()

        collector = self.open_collector()
        self.advance()
        self.client.counters = {8388: 170}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        self.advance()
        self.client.counters = {8388: 180}
        self.assertTrue(collector.collect_once(self.client, self.identity))

        rows = self.rows()
        self.assertEqual([row[:2] for row in rows], [
            (100, 0), (130, 30), (170, 70), (180, 80),
        ])
        self.assertEqual([row[2] for row in rows], [None, 30.0, None, 10.0])
        self.assertEqual([row[3] for row in rows], [
            "baseline", "ok", "interrupted", "ok",
        ])


    def test_temporarily_omitted_port_keeps_counter_checkpoint(self):
        collector = self.open_collector()
        for counters in [{8388: 100}, {8388: 130}, {}, {8388: 170}, {8388: 180}]:
            self.client.counters = counters
            self.assertTrue(collector.collect_once(self.client, self.identity))
            self.advance()

        rows = self.rows()
        self.assertEqual([row[:2] for row in rows], [
            (100, 0), (130, 30), (170, 70), (180, 80),
        ])
        self.assertEqual(rows[2][2:4], (None, "interrupted"))
        self.assertEqual(rows[3][2:4], (10.0, "ok"))

    def test_only_one_collector_can_own_database(self):
        collector = self.open_collector()
        with self.assertRaises(TrafficError):
            self.open_collector()

        self.client.counters = {8388: 100}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        collector.close()

        collector = self.open_collector()
        self.advance()
        self.client.counters = {8388: 130}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        self.assertEqual(self.rows()[-1][:4], (130, 30, None, "interrupted"))

    def test_rolled_back_sample_does_not_advance_memory_or_checkpoint(self):
        collector = self.open_collector()
        self.client.counters = {8388: 100}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        collector.connection.execute("""
            CREATE TRIGGER reject_130 BEFORE INSERT ON samples
            WHEN NEW.manager_total = 130
            BEGIN SELECT RAISE(ABORT, 'reject sample'); END;
        """)
        collector.connection.commit()
        self.advance()
        self.client.counters = {8388: 130}
        with self.assertRaises(sqlite3.IntegrityError):
            collector.collect_once(self.client, self.identity)
        collector.connection.execute("DROP TRIGGER reject_130")
        collector.connection.commit()

        self.advance()
        self.client.counters = {8388: 170}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        self.assertEqual(self.rows(), [
            (100, 0, None, "baseline"),
            (170, 70, 35.0, "ok"),
        ])

    def test_unversioned_database_baselines_once_without_losing_total(self):
        connection = sqlite3.connect(str(self.database))
        connection.executescript("""
            CREATE TABLE samples (
                port INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                observed_epoch REAL NOT NULL,
                manager_total INTEGER NOT NULL,
                tracked_total INTEGER NOT NULL,
                rate_bytes_per_second REAL,
                rate_reason TEXT NOT NULL
            );
            CREATE TABLE device_status (
                port INTEGER PRIMARY KEY,
                last_attempt_at TEXT NOT NULL,
                last_error TEXT
            );
            CREATE TABLE collector_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL,
                stale_after REAL NOT NULL
            );
            INSERT INTO samples VALUES (
                8388, '2026-01-01T00:00:00.000Z', 1.0,
                1000, 500, NULL, 'baseline'
            );
        """)
        connection.commit()
        connection.close()

        collector = self.open_collector()
        self.client.counters = {8388: 1200}
        self.assertTrue(collector.collect_once(self.client, self.identity))
        self.advance()
        self.client.counters = {8388: 1300}
        self.assertTrue(collector.collect_once(self.client, self.identity))

        rows = self.rows()
        self.assertEqual(rows[1][:4], (1200, 500, None, "baseline"))
        self.assertEqual(rows[2][:4], (1300, 600, 100.0, "ok"))

    def test_two_once_commands_share_totals_but_not_rate_baselines(self):
        mapping_path = Path(self.temp_dir.name) / "mapping.json"
        mapping_path.write_text(json.dumps({
            "managerSocket": "/run/manager.sock",
            "devices": [{"port": 8388, "label": "Phone A"}],
        }), encoding="utf-8")
        arguments = [
            "collect", "--mapping", str(mapping_path),
            "--database", str(self.database), "--once",
        ]
        with patch("keel.traffic.ManagerClient") as client_type, patch(
            "keel.traffic.ManagerIdentity"
        ) as identity_type:
            client_type.return_value.__enter__.return_value = self.client
            identity_type.return_value = self.identity
            self.client.counters = {8388: 100}
            self.assertEqual(main(arguments), 0)
            self.client.counters = {8388: 130}
            self.assertEqual(main(arguments), 0)

        self.assertEqual(self.rows(), [
            (100, 0, None, "baseline"),
            (130, 30, None, "interrupted"),
        ])

    def test_new_generation_and_counter_reset_count_only_current_lifetime(self):
        collector = self.open_collector()
        steps = [
            (100, "generation-a"),
            (130, "generation-a"),
            (20, "generation-b"),
            (30, "generation-b"),
            (10, "generation-b"),
            (15, "generation-b"),
        ]
        for total, generation in steps:
            self.client.counters = {8388: total}
            self.identity.value = generation
            self.assertTrue(collector.collect_once(self.client, self.identity))
            self.advance()

        rows = self.rows()
        self.assertEqual([row[:2] for row in rows], [
            (100, 0), (130, 30), (20, 50),
            (30, 60), (10, 70), (15, 75),
        ])
        self.assertEqual([row[2] for row in rows], [
            None, 30.0, None, 10.0, None, 5.0,
        ])
        self.assertEqual([row[3] for row in rows], [
            "baseline", "ok", "reset", "ok", "reset", "ok",
        ])


if __name__ == "__main__":
    unittest.main()
