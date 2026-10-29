"""소수 수량·가격은 TEXT로 저장하고 계산할 때 Decimal로 읽는다."""

from decimal import Decimal

TABLES = {
    "orders": """CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        quantity TEXT NOT NULL, limit_price TEXT NOT NULL, status TEXT NOT NULL,
        broker_order_id TEXT, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "order_fills": """CREATE TABLE IF NOT EXISTS order_fills (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL UNIQUE,
        order_id TEXT NOT NULL REFERENCES orders(order_id), quantity TEXT NOT NULL,
        fill_price TEXT NOT NULL, fee TEXT NOT NULL, occurred_at TEXT NOT NULL)""",
    "account": """CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK (id = 1), initial_cash TEXT NOT NULL)""",
    "broker_orders": """CREATE TABLE IF NOT EXISTS broker_orders (
        client_order_id TEXT PRIMARY KEY, broker_order_id TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        quantity TEXT NOT NULL, limit_price TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED')))""",
    "broker_fills": """CREATE TABLE IF NOT EXISTS broker_fills (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL UNIQUE,
        client_order_id TEXT NOT NULL REFERENCES broker_orders(client_order_id),
        quantity TEXT NOT NULL, fill_price TEXT NOT NULL, fee TEXT NOT NULL, occurred_at TEXT NOT NULL)""",
}


def upgrade_legacy(db, *, broker=False):
    """구 정수·센트 DB를 한 번만 변환한다. 과거 체결에 수수료를 소급하지 않는다."""
    orders = "broker_orders" if broker else "orders"
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({orders})")}
    if "price_cents" not in columns:
        return
    names = [orders, "broker_fills"] if broker else [orders, "order_fills", "account"]
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        # 잠금을 얻은 뒤 재확인해 동시 재시작에서도 한 번만 변환한다.
        if "price_cents" not in {r["name"] for r in db.execute(f"PRAGMA table_info({orders})")}:
            db.commit()
            return
        for name in names:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            rows = [dict(r) for r in db.execute(f"SELECT * FROM {name}")] if exists else []
            db.execute(f"DROP TABLE IF EXISTS {name}")
            db.execute(TABLES[name])
            for row in rows:
                if "quantity" in row:
                    row["quantity"] = str(row["quantity"])
                if "price_cents" in row:
                    field = "limit_price" if name == orders else "fill_price"
                    row[field] = str(Decimal(row.pop("price_cents")) / 100)
                if name == orders:
                    row.setdefault("side", "buy")
                elif name.endswith("fills"):
                    row["fee"] = "0"
                elif name == "account":
                    row["initial_cash"] = str(Decimal(row.pop("initial_cash_cents")) / 100)
                fields = ",".join(row)
                placeholders = ",".join("?" for _ in row)
                db.execute(f"INSERT INTO {name} ({fields}) VALUES ({placeholders})", tuple(row.values()))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("legacy database foreign keys did not reconcile")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
