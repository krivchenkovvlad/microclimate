"""Подключение к SQLite. База остаётся в папке notebooks."""
from pathlib import Path
import sqlite3

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "notebooks" / "microclimate.db"


def get_conn(db_path=DB_PATH, *, create=False):
    """Обычно открываем только существующую базу; создание включается явно."""
    path = Path(db_path).resolve()
    if not create and not path.is_file():
        raise FileNotFoundError(f"База не найдена: {path}")
    mode = "rwc" if create else "rw"
    conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_schema(conn):
    """Создаёт отсутствующие таблицы; существующие данные не изменяет."""
    schema = (PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
