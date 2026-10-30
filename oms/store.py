"""주문 처리와 SQLite 저장. orders 테이블 하나만 사용한다."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .domain import FillRequest, OrderRequest


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    quantity INTEGER,
                    price REAL,
                    time TEXT,
                    kind TEXT,
                    status TEXT,
                    filled_quantity INTEGER DEFAULT 0,
                    original_order_id INTEGER
                )
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _get(self, db, order_id: int) -> dict:
        row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise LookupError("주문을 찾을 수 없습니다.")
        return dict(row)

    def create(self, request: OrderRequest) -> dict:
        symbol = request.symbol.strip().upper()
        if not symbol:
            raise ValueError("종목명을 입력해 주세요.")
        with self.connection() as db:
            result = db.execute(
                """INSERT INTO orders (symbol, quantity, price, time, kind, status)
                   VALUES (?, ?, ?, ?, ?, 'submitted')""",
                (symbol, request.quantity, request.price, datetime.now(UTC).isoformat(), request.kind),
            )
            return self._get(db, result.lastrowid)

    def get(self, order_id: int) -> dict:
        with self.connection() as db:
            return self._get(db, order_id)

    def list_orders(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
            return [dict(row) for row in rows]

    def accept(self, order_id: int) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self._get(db, order_id)
            if order["kind"] not in ("buy", "sell") or order["status"] != "submitted":
                raise ValueError("제출 상태의 매수·매도 주문만 접수할 수 있습니다.")
            db.execute("UPDATE orders SET status = 'accepted' WHERE order_id = ?", (order_id,))
            return self._get(db, order_id)

    def fill(self, order_id: int, request: FillRequest) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self._get(db, order_id)
            if order["kind"] not in ("buy", "sell") or order["status"] not in ("accepted", "partially_filled"):
                raise ValueError("접수되었거나 일부 체결된 매수·매도 주문만 체결할 수 있습니다.")
            filled_quantity = order["filled_quantity"] + request.quantity
            if filled_quantity > order["quantity"]:
                raise ValueError("남은 주문 수량보다 많이 체결할 수 없습니다.")
            status = "completed" if filled_quantity == order["quantity"] else "partially_filled"
            db.execute(
                "UPDATE orders SET filled_quantity = ?, status = ? WHERE order_id = ?",
                (filled_quantity, status, order_id),
            )
            return self._get(db, order_id)

    def cancel(self, order_id: int) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            order = self._get(db, order_id)
            if order["kind"] not in ("buy", "sell") or order["status"] not in ("submitted", "accepted", "partially_filled"):
                raise ValueError("진행 중인 매수·매도 주문만 취소할 수 있습니다.")
            db.execute("UPDATE orders SET status = 'canceled' WHERE order_id = ?", (order_id,))
            remaining = order["quantity"] - order["filled_quantity"]
            # 원주문 취소와 취소 기록 저장은 같은 트랜잭션에서 함께 commit된다.
            result = db.execute(
                """INSERT INTO orders (symbol, quantity, price, time, kind, status, original_order_id)
                   VALUES (?, ?, ?, ?, 'cancel', 'completed', ?)""",
                (order["symbol"], remaining, order["price"], datetime.now(UTC).isoformat(), order_id),
            )
            return self._get(db, result.lastrowid)
