import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    version TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                    kind TEXT NOT NULL, level TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY, timestamp REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    timestamp REAL NOT NULL, deployment_id TEXT NOT NULL,
                    version TEXT NOT NULL, success INTEGER NOT NULL,
                    latency_ms REAL NOT NULL, prediction INTEGER, label INTEGER
                );
                CREATE INDEX IF NOT EXISTS requests_window ON requests(deployment_id,timestamp);
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, timestamp REAL NOT NULL, data TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rows(self, sql, parameters=()):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters)]

    def state(self):
        rows = self.rows("SELECT data FROM state WHERE id=1")
        return json.loads(rows[0]["data"]) if rows else None

    @staticmethod
    def save_state(connection, data):
        connection.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(data),))

    @staticmethod
    def event(connection, timestamp, kind, payload=None, level="info"):
        connection.execute(
            "INSERT INTO events(timestamp,kind,level,payload) VALUES(?,?,?,?)",
            (timestamp, kind, level, json.dumps(payload or {}, ensure_ascii=False)),
        )

    def events(self, limit=100):
        rows = self.rows("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def models(self):
        return [
            json.loads(row["metadata"]) for row in self.rows("SELECT metadata FROM models ORDER BY version")
        ]

    def model(self, version):
        rows = self.rows("SELECT metadata FROM models WHERE version=?", (version,))
        return json.loads(rows[0]["metadata"]) if rows else None

    def experiment(self, experiment_id):
        rows = self.rows("SELECT data FROM experiments WHERE id=?", (experiment_id,))
        return json.loads(rows[0]["data"]) if rows else None

    @staticmethod
    def save_experiment(connection, experiment):
        connection.execute(
            "INSERT OR REPLACE INTO experiments VALUES(?,?,?)",
            (experiment["id"], experiment["started_at"], json.dumps(experiment)),
        )
