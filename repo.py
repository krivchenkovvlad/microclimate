"""Запросы и правила работы с данными. Транзакциями управляет вызывающий код."""
from datetime import datetime, timezone
import math


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_channels(conn, device_uid="ESP32-001"):
    rows = conn.execute("""
        SELECT channel.id, channel.metric
        FROM channel
        INNER JOIN sensor ON sensor.id = channel.sensor_id
        INNER JOIN node ON node.id = sensor.node_id
        WHERE node.device_uid = ? AND sensor.model = 'DHT22'
    """, (device_uid,)).fetchall()
    channels = {}
    for row in rows:
        if row["metric"] in channels:
            raise ValueError("У узла несколько DHT22: требуется выбрать датчик.")
        channels[row["metric"]] = row["id"]
    if set(channels) != {"temperature", "humidity"}:
        raise ValueError("У DHT22 должны быть каналы температуры и влажности.")
    return channels


def initialize_reference_data(conn):
    """Первичное наполнение. Повторный вызов не добавляет дубликаты."""
    conn.execute("""
        INSERT INTO room (name, area_m2) VALUES (?, ?)
        ON CONFLICT (name) DO NOTHING
    """, ("Учебная комната", 12.0))
    room_id = conn.execute("SELECT id FROM room WHERE name = ?", ("Учебная комната",)).fetchone()[0]
    conn.execute("""
        INSERT INTO node (room_id, device_uid, period_ms) VALUES (?, ?, ?)
        ON CONFLICT (device_uid) DO NOTHING
    """, (room_id, "ESP32-001", 2000))
    node_id = conn.execute("SELECT id FROM node WHERE device_uid = ?", ("ESP32-001",)).fetchone()[0]
    sensors = conn.execute("SELECT id FROM sensor WHERE node_id = ? AND model = ?", (node_id, "DHT22")).fetchall()
    if len(sensors) > 1:
        raise ValueError("Найдено несколько DHT22: требуется выбрать датчик.")
    if sensors:
        sensor_id = sensors[0][0]
    else:
        sensor_id = conn.execute("INSERT INTO sensor (node_id, model) VALUES (?, ?)", (node_id, "DHT22")).lastrowid
    for metric, unit in [("temperature", "°C"), ("humidity", "%")]:
        conn.execute("""
            INSERT INTO channel (sensor_id, metric, unit) VALUES (?, ?, ?)
            ON CONFLICT (sensor_id, metric) DO NOTHING
        """, (sensor_id, metric, unit))
    start = utc_now()
    for metric, lower, upper in [("temperature", 20.0, 26.0), ("humidity", 40.0, 60.0)]:
        channel_id = get_channels(conn)[metric]
        if conn.execute("SELECT id FROM threshold WHERE channel_id = ?", (channel_id,)).fetchone() is None:
            conn.execute("""
                INSERT INTO threshold (channel_id, lower_bound, upper_bound, valid_from)
                VALUES (?, ?, ?, ?)
            """, (channel_id, lower, upper, start))


def check_threshold(conn, channel_id, ts, value):
    rule = conn.execute("""
        SELECT id, lower_bound, upper_bound
        FROM threshold
        WHERE channel_id = ?
          AND valid_from <= ?
          AND (valid_to IS NULL OR ? < valid_to)
    """, (channel_id, ts, ts)).fetchall()

    if len(rule) == 0:
        return "Нет действующего порога"

    if len(rule) > 1:
        raise ValueError("Для канала действуют несколько порогов одновременно.")

    threshold_id, lower, upper = rule[0]

    if value < lower:
        direction = "below"
    elif value > upper:
        direction = "above"
    else:
        return "В пределах порога"

    conn.execute("""
        INSERT INTO alert (threshold_id, channel_id, ts, direction)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (channel_id, ts) DO NOTHING
    """, (threshold_id, channel_id, ts, direction))

    return direction


def save_pair(conn, channels, ts, temperature, humidity):
    """Одна транзакция: два измерения и связанные предупреждения."""
    results = {}
    with conn:
        for metric, value in [("temperature", temperature), ("humidity", humidity)]:
            channel_id = channels[metric]
            conn.execute(
                "INSERT INTO measurement (channel_id, ts, value) VALUES (?, ?, ?)",
                (channel_id, ts, value)
            )
            results[metric] = check_threshold(conn, channel_id, ts, value)
    return results


def recent_measurements(conn, limit=10):
    return conn.execute("""
        SELECT channel.metric, measurement.ts, measurement.value, channel.unit
        FROM measurement INNER JOIN channel ON channel.id = measurement.channel_id
        ORDER BY measurement.ts DESC, channel.id LIMIT ?
    """, (limit,)).fetchall()


def recent_alerts(conn, limit=10):
    return conn.execute("""
        SELECT channel.metric, alert.ts, measurement.value,
               threshold.lower_bound, threshold.upper_bound, alert.direction
        FROM alert
        INNER JOIN channel ON channel.id = alert.channel_id
        INNER JOIN measurement ON measurement.channel_id = alert.channel_id AND measurement.ts = alert.ts
        INNER JOIN threshold ON threshold.id = alert.threshold_id
        ORDER BY alert.ts DESC, channel.id LIMIT ?
    """, (limit,)).fetchall()



def set_threshold(conn, channel_id, lower, upper, valid_from=None):
    """Добавляет новое правило, завершая предыдущее бессрочное.

    Изменения только вперёд по времени: старые измерения и alert не переписываются.
    Периоды имеют вид [valid_from, valid_to).
    """
    if not (math.isfinite(lower) and math.isfinite(upper) and lower < upper):
        raise ValueError("Границы должны быть конечными числами, нижняя меньше верхней.")
    start = utc_now() if valid_from is None else valid_from
    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Начало действия должно быть в UTC.")
    canonical = parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if start != canonical:
        raise ValueError("Ожидается UTC-формат YYYY-MM-DDTHH:MM:SS.sssZ.")
    with conn:
        if conn.execute("SELECT id FROM channel WHERE id = ?", (channel_id,)).fetchone() is None:
            raise ValueError("Канал не найден.")
        rules = conn.execute("""
            SELECT id, valid_from, valid_to FROM threshold
            WHERE channel_id = ? ORDER BY valid_from
        """, (channel_id,)).fetchall()
        for rule in rules:
            if rule["valid_from"] >= start:
                raise ValueError("Новое правило должно начинаться позже всех существующих.")
            if rule["valid_to"] is not None and rule["valid_to"] > start:
                raise ValueError("Новое правило пересекает завершённый период.")
        open_rules = [rule for rule in rules if rule["valid_to"] is None]
        if len(open_rules) > 1:
            raise ValueError("Найдены пересекающиеся бессрочные правила.")
        if open_rules:
            conn.execute("UPDATE threshold SET valid_to = ? WHERE id = ?", (start, open_rules[0]["id"]))
        cursor = conn.execute("""
            INSERT INTO threshold (channel_id, lower_bound, upper_bound, valid_from, valid_to)
            VALUES (?, ?, ?, ?, NULL)
        """, (channel_id, lower, upper, start))
        return cursor.lastrowid
