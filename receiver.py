"""Получение показаний через USB; без привязки к Jupyter и виджетам GUI."""
import math
import time
from repo import get_channels, save_pair, utc_now


def parse_sample(sample):
    parts = sample.split()
    if len(parts) != 7 or (parts[0], parts[2], parts[3], parts[4], parts[6]) != (
        "Temperature:", "C", "|", "Humidity:", "%"
    ):
        return None
    try:
        temperature, humidity = float(parts[1]), float(parts[5])
    except ValueError:
        return None
    if not (math.isfinite(temperature) and math.isfinite(humidity)):
        return None
    if not (-40 <= temperature <= 80 and 0 <= humidity <= 100):
        return None
    return temperature, humidity


def collect_pairs(conn, port, baudrate=115200, count=5, idle_timeout=15):
    """Сохраняет count пар. При отсутствии корректных строк завершает сбор."""
    import serial

    if count <= 0 or idle_timeout <= 0:
        raise ValueError("Количество пар и время ожидания должны быть положительными.")
    channels = get_channels(conn)
    saved = 0
    with serial.Serial(port, baudrate, timeout=1) as esp:
        last_received = time.monotonic()
        while saved < count:
            if time.monotonic() - last_received >= idle_timeout:
                raise TimeoutError(f"Нет корректных показаний уже {idle_timeout} секунд. Сохранено пар: {saved}.")
            sample = esp.readline().decode("utf-8", errors="replace").strip()
            values = parse_sample(sample)
            if values is None:
                continue
            temperature, humidity = values
            last_received = time.monotonic()
            results = save_pair(conn, channels, utc_now(), temperature, humidity)
            saved += 1
            print(f"Пара {saved}: {temperature} °C | {humidity} % | "
                  f"температура: {results['temperature']} | влажность: {results['humidity']}")
    return saved
