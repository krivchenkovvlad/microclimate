CREATE TABLE IF NOT EXISTS room (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    area_m2 REAL NOT NULL CHECK (area_m2 > 0)
);

CREATE TABLE IF NOT EXISTS node (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES room(id),
    device_uid TEXT NOT NULL UNIQUE,
    period_ms INTEGER NOT NULL CHECK (period_ms > 0)
);

CREATE TABLE IF NOT EXISTS sensor (
    id INTEGER PRIMARY KEY,
    node_id INTEGER NOT NULL REFERENCES node(id),
    model TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel (
    id INTEGER PRIMARY KEY,
    sensor_id INTEGER NOT NULL REFERENCES sensor(id),
    metric TEXT NOT NULL CHECK (
        metric IN ('temperature', 'humidity')
    ),
    unit TEXT NOT NULL CHECK (
        (metric = 'temperature' AND unit = '°C')
        OR
        (metric = 'humidity' AND unit = '%')
    ),
    UNIQUE (sensor_id, metric)
);

CREATE TABLE IF NOT EXISTS measurement (
    channel_id INTEGER NOT NULL REFERENCES channel(id),
    ts TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (channel_id, ts)
);

CREATE TABLE IF NOT EXISTS threshold (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channel(id),
    lower_bound REAL NOT NULL,
    upper_bound REAL NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    UNIQUE (id, channel_id),
    CHECK (lower_bound < upper_bound),
    CHECK (valid_to IS NULL OR valid_from < valid_to)
);

CREATE TABLE IF NOT EXISTS alert (
    id INTEGER PRIMARY KEY,
    threshold_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (
        direction IN ('below', 'above')
    ),
    UNIQUE (channel_id, ts),
    FOREIGN KEY (channel_id, ts)
        REFERENCES measurement(channel_id, ts),
    FOREIGN KEY (threshold_id, channel_id)
        REFERENCES threshold(id, channel_id)
);
