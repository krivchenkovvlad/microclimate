"""Запуск из корня проекта: python -m unittest discover -s tests -v"""
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from db import get_conn, initialize_schema
from repo import (initialize_reference_data, get_channels, save_pair,
                  check_threshold, set_threshold, recent_alerts)
from receiver import parse_sample, collect_pairs


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.conn = get_conn(self.path, create=True)
        initialize_schema(self.conn)
        with self.conn:
            initialize_reference_data(self.conn)
        self.channels = get_channels(self.conn)
        # Fixed test time makes threshold tests independent of today's date.
        with self.conn:
            self.conn.execute("UPDATE threshold SET valid_from = ?", ("2026-01-01T00:00:00.000Z",))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_missing_database_is_not_created(self):
        path = self.path.parent / "missing.db"
        with self.assertRaises(FileNotFoundError):
            get_conn(path)
        self.assertFalse(path.exists())

    def test_foreign_keys_enabled(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError), self.conn:
            self.conn.execute("INSERT INTO measurement VALUES (999, 'x', 23)")

    def test_reference_data_can_be_initialized_twice(self):
        with self.conn:
            initialize_reference_data(self.conn)
        for table, count in [("room", 1), ("node", 1), ("sensor", 1), ("channel", 2), ("threshold", 2)]:
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], count)

    def test_channels_are_found_by_metric_not_fixed_id(self):
        with self.conn:
            self.conn.execute("DELETE FROM threshold")
            self.conn.execute("UPDATE channel SET id = id + 20")
            initialize_reference_data(self.conn)
        self.assertEqual(get_channels(self.conn), {"temperature": 21, "humidity": 22})

    def test_parse_sample(self):
        self.assertEqual(parse_sample("Temperature: 23.50 C | Humidity: 44.80 %"), (23.5, 44.8))
        for sample in ["", "boot", "Temperature: bad C | Humidity: 50 %",
                       "Temperature: nan C | Humidity: 50 %",
                       "Temperature: inf C | Humidity: 50 %",
                       "Temperature: 23 C | Humidity: 101 %",
                       "Temperature: 23 X | Humidity: 50 %"]:
            with self.subTest(sample=sample):
                self.assertIsNone(parse_sample(sample))

    def test_above_below_and_boundaries(self):
        for i, (value, expected) in enumerate([(30, "above"), (10, "below"),
                                               (20, "В пределах порога"),
                                               (26, "В пределах порога")]):
            ts = f"2026-02-01T00:00:0{i}.000Z"
            result = save_pair(self.conn, self.channels, ts, value, 50)
            self.assertEqual(result["temperature"], expected)
            alert_count = self.conn.execute("SELECT COUNT(*) FROM alert WHERE ts = ?", (ts,)).fetchone()[0]
            self.assertEqual(alert_count, int(expected in ("above", "below")))

    def test_humidity_above_and_below(self):
        for i, (value, expected) in enumerate([(30, "below"), (70, "above"),
                                               (40, "В пределах порога"), (60, "В пределах порога")]):
            result = save_pair(self.conn, self.channels, f"2026-02-02T00:00:0{i}.000Z", 23, value)
            self.assertEqual(result["humidity"], expected)

    def test_no_rule_before_start(self):
        result = save_pair(self.conn, self.channels, "2025-12-31T23:59:59.000Z", 30, 50)
        self.assertEqual(result["temperature"], "Нет действующего порога")
        self.assertEqual(len(recent_alerts(self.conn)), 0)

    def test_alert_check_is_repeatable(self):
        ts = "2026-02-01T00:00:00.000Z"
        save_pair(self.conn, self.channels, ts, 30, 50)
        with self.conn:
            check_threshold(self.conn, self.channels["temperature"], ts, 30)
        self.assertEqual(len(recent_alerts(self.conn)), 1)

    def test_duplicate_measurement_does_not_change_data(self):
        ts = "2026-02-01T00:00:00.000Z"
        save_pair(self.conn, self.channels, ts, 23, 50)
        with self.assertRaises(sqlite3.IntegrityError):
            save_pair(self.conn, self.channels, ts, 30, 80)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM measurement").fetchone()[0], 2)
        self.assertEqual(len(recent_alerts(self.conn)), 0)

    def test_overlap_rolls_back_pair_and_alert(self):
        with self.conn:
            self.conn.execute("""INSERT INTO threshold (channel_id, lower_bound, upper_bound, valid_from)
                                 VALUES (?, 30, 70, '2026-01-01T00:00:00.000Z')""", (self.channels["humidity"],))
        with self.assertRaises(ValueError):
            save_pair(self.conn, self.channels, "2026-02-01T00:00:00.000Z", 30, 50)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM measurement").fetchone()[0], 0)
        self.assertEqual(len(recent_alerts(self.conn)), 0)

    def test_threshold_change_preserves_history(self):
        ts = "2026-02-01T00:00:00.000Z"
        save_pair(self.conn, self.channels, ts, 27, 50)
        old_id = recent_alerts(self.conn)[0]["direction"]
        set_threshold(self.conn, self.channels["temperature"], 20, 28, "2026-03-01T00:00:00.000Z")
        rules = self.conn.execute("SELECT valid_from, valid_to FROM threshold WHERE channel_id = ? ORDER BY valid_from", (self.channels["temperature"],)).fetchall()
        self.assertEqual(rules[0]["valid_to"], rules[1]["valid_from"])
        result = save_pair(self.conn, self.channels, "2026-03-01T00:00:00.000Z", 27, 50)
        self.assertEqual(result["temperature"], "В пределах порога")
        self.assertEqual(recent_alerts(self.conn)[0]["direction"], old_id)
        self.assertEqual(check_threshold(self.conn, self.channels["temperature"], ts, 27), "above")
        self.conn.commit()

    def test_invalid_threshold_change_leaves_rules_unchanged(self):
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM threshold")]
        for lower, upper, start in [(30, 20, "2026-03-01T00:00:00.000Z"),
                                     (20, float("nan"), "2026-03-01T00:00:00.000Z"),
                                     (20, 28, "2025-01-01T00:00:00.000Z"),
                                     (20, 28, "2026-03-01T00:00:00+03:00")]:
            with self.assertRaises(ValueError):
                set_threshold(self.conn, self.channels["temperature"], lower, upper, start)
            self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM threshold")], before)

    def test_collect_with_simulated_serial(self):
        port = FakePort([b"boot\n", b"Temperature: 23.5 C | Humidity: 44.8 %\n"] * 5)
        fake_serial = types.SimpleNamespace(Serial=lambda *a, **k: port)
        stamps = [f"2026-02-01T00:00:0{i}.000Z" for i in range(5)]
        with patch.dict(sys.modules, {"serial": fake_serial}), patch("receiver.utc_now", side_effect=stamps), redirect_stdout(StringIO()):
            self.assertEqual(collect_pairs(self.conn, "fake"), 5)
        self.assertTrue(port.closed)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM measurement").fetchone()[0], 10)

    def test_idle_timeout_closes_port(self):
        port = FakePort([])
        fake_serial = types.SimpleNamespace(Serial=lambda *a, **k: port)
        with patch.dict(sys.modules, {"serial": fake_serial}), patch("receiver.time.monotonic", side_effect=[0, 0, 16]):
            with self.assertRaises(TimeoutError):
                collect_pairs(self.conn, "fake")
        self.assertTrue(port.closed)

    def test_keyboard_interrupt_closes_port(self):
        port = FakePort([])
        fake_serial = types.SimpleNamespace(Serial=lambda *a, **k: port)
        with patch.dict(sys.modules, {"serial": fake_serial}), patch.object(port, "readline", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                collect_pairs(self.conn, "fake")
        self.assertTrue(port.closed)


class FakePort:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def readline(self):
        return next(self.lines, b"")


if __name__ == "__main__":
    unittest.main()
