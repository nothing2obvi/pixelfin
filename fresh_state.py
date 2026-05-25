import json
import os
import sqlite3
from datetime import datetime, timezone


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "fresh.db")


def utc_now():
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
	os.makedirs(DATA_DIR, exist_ok=True)
	conn = sqlite3.connect(DB_PATH)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA foreign_keys = ON")
	init_db(conn)
	return conn


def init_db(conn):
	conn.executescript(
		"""
		CREATE TABLE IF NOT EXISTS servers (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			name TEXT NOT NULL,
			url TEXT NOT NULL,
			api_key TEXT NOT NULL,
			sync_user_id TEXT NOT NULL DEFAULT '',
			is_active INTEGER NOT NULL DEFAULT 0,
			last_checked TEXT,
			last_status TEXT,
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL
		);

		CREATE TABLE IF NOT EXISTS app_settings (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL
		);

		CREATE TABLE IF NOT EXISTS libraries (
			id TEXT NOT NULL,
			server_id INTEGER NOT NULL,
			name TEXT NOT NULL,
			collection_type TEXT,
			thumbnail_url TEXT,
			hidden INTEGER NOT NULL DEFAULT 0,
			selected_images TEXT NOT NULL,
			thresholds TEXT NOT NULL,
			zipnames TEXT NOT NULL,
			item_count INTEGER NOT NULL DEFAULT 0,
			task_count INTEGER NOT NULL DEFAULT 0,
			last_scanned TEXT,
			PRIMARY KEY (server_id, id),
			FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE
		);

		CREATE TABLE IF NOT EXISTS media_items (
			id TEXT NOT NULL,
			server_id INTEGER NOT NULL,
			library_id TEXT NOT NULL,
			name TEXT NOT NULL,
			year TEXT,
			item_type TEXT,
			sort_name TEXT,
			date_added TEXT,
			needs_attention INTEGER NOT NULL DEFAULT 0,
			details_url TEXT,
			raw_json TEXT NOT NULL,
			last_scanned TEXT,
			PRIMARY KEY (server_id, id),
			FOREIGN KEY(server_id, library_id) REFERENCES libraries(server_id, id) ON DELETE CASCADE
		);

		CREATE TABLE IF NOT EXISTS item_images (
			server_id INTEGER NOT NULL,
			item_id TEXT NOT NULL,
			code TEXT NOT NULL,
			label TEXT NOT NULL,
			url TEXT,
			width INTEGER NOT NULL DEFAULT 0,
			height INTEGER NOT NULL DEFAULT 0,
			status TEXT NOT NULL,
			is_low INTEGER NOT NULL DEFAULT 0,
			is_missing INTEGER NOT NULL DEFAULT 0,
			is_high INTEGER NOT NULL DEFAULT 0,
			last_checked TEXT,
			PRIMARY KEY (server_id, item_id, code, label),
			FOREIGN KEY(server_id, item_id) REFERENCES media_items(server_id, id) ON DELETE CASCADE
		);
		"""
	)
	_migrate_column(conn, "servers", "sync_user_id", "TEXT NOT NULL DEFAULT ''")
	_migrate_column(conn, "libraries", "high_thresholds", "TEXT NOT NULL DEFAULT '{}'")
	_migrate_column(conn, "libraries", "sort_order", "TEXT NOT NULL DEFAULT ''")
	_migrate_column(conn, "item_images", "is_high", "INTEGER NOT NULL DEFAULT 0")
	conn.commit()


def _migrate_column(conn, table, column, definition):
	existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
	if column not in existing:
		conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_json(conn, key, default):
	row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
	if not row:
		return default
	try:
		return json.loads(row["value"])
	except Exception:
		return default


def set_json(conn, key, value):
	conn.execute(
		"INSERT INTO app_settings(key, value) VALUES(?, ?) "
		"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
		(key, json.dumps(value)),
	)
	conn.commit()


def row_to_dict(row):
	return dict(row) if row else None


def rows_to_dicts(rows):
	return [dict(row) for row in rows]
