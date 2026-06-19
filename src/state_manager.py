"""SQLite-based state persistence for the trading system."""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from typing import Generator

from .engine_a import BreakoutPosition, BoxRange, EngineAState, StrategyMode
from .engine_b import HedgePosition

# web3 v7 renamed geth_poa_middleware; patch the name so sweeper.py (which uses
# the old name) can be imported without modification.
try:
    import web3.middleware as _w3mw
    if not hasattr(_w3mw, "geth_poa_middleware"):
        from web3.middleware import ExtraDataToPOAMiddleware as _poa
        _w3mw.geth_poa_middleware = _poa
except Exception:
    pass

from .sweeper import SweepRecord

logger = logging.getLogger(__name__)

STATE_DB_PATH = os.getenv("STATE_DB_PATH", "./trading_state.db")

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS engine_a_state (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_b_position (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sweep_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    amount_usdc     REAL    NOT NULL,
    tx_hash         TEXT    NOT NULL,
    gas_used        INTEGER NOT NULL DEFAULT 0,
    gas_price_gwei  REAL    NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    price       REAL    NOT NULL,
    size        REAL    NOT NULL,
    pnl         REAL    NOT NULL,
    timestamp   TEXT    NOT NULL
);
"""


class StateManager:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or STATE_DB_PATH
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._cursor() as cur:
            cur.executescript(_CREATE_TABLES_SQL)
        logger.debug("State DB initialised at %s", self._db_path)

    # ------------------------------------------------------------------
    # Engine A state
    # ------------------------------------------------------------------

    def save_engine_a_state(self, state: EngineAState) -> None:
        """Serialize EngineAState dataclass to JSON and upsert into DB."""
        box_data = None
        if state.box is not None:
            box_data = {"support": state.box.support, "resistance": state.box.resistance}

        position_data = None
        if state.position is not None:
            position_data = asdict(state.position)

        payload = json.dumps({
            "mode": state.mode.name,
            "box": box_data,
            "open_order_ids": state.open_order_ids,
            "allocated_capital": state.allocated_capital,
            "realized_pnl": state.realized_pnl,
            "position": position_data,
            "box_last_updated": state.box_last_updated,
            "cooldown_until": getattr(state, "cooldown_until", 0.0),
        })

        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO engine_a_state (id, payload) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (payload,),
            )
        logger.debug("engine_a_state saved")

    def load_engine_a_state(self) -> EngineAState | None:
        """Load and deserialize EngineAState from DB; returns None if not found."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT payload FROM engine_a_state WHERE id = 1"
            ).fetchone()

        if row is None:
            return None

        data = json.loads(row["payload"])

        box = None
        if data.get("box") is not None:
            box = BoxRange(
                support=data["box"]["support"],
                resistance=data["box"]["resistance"],
            )

        position = None
        if data.get("position") is not None:
            p = data["position"]
            position = BreakoutPosition(
                side=p["side"],
                entry_price=p["entry_price"],
                size=p["size"],
                peak_price=p["peak_price"],
                order_id=p.get("order_id", ""),
                trailing_stop_distance=p.get("trailing_stop_distance", 0.0),
                stop_loss_distance=p.get("stop_loss_distance", 0.0),
            )

        return EngineAState(
            mode=StrategyMode[data["mode"]],
            box=box,
            open_order_ids=data.get("open_order_ids", []),
            allocated_capital=data.get("allocated_capital", 0.0),
            realized_pnl=data.get("realized_pnl", 0.0),
            position=position,
            box_last_updated=data.get("box_last_updated", 0.0),
            cooldown_until=data.get("cooldown_until", 0.0),
        )

    # ------------------------------------------------------------------
    # Engine B position
    # ------------------------------------------------------------------

    def save_engine_b_position(self, position: HedgePosition) -> None:
        """Serialize HedgePosition dataclass to JSON and upsert into DB."""
        payload = json.dumps(asdict(position))
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO engine_b_position (id, payload) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (payload,),
            )
        logger.debug("engine_b_position saved")

    def load_engine_b_position(self) -> HedgePosition | None:
        """Load and deserialize HedgePosition from DB; returns None if not found."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT payload FROM engine_b_position WHERE id = 1"
            ).fetchone()

        if row is None:
            return None

        data = json.loads(row["payload"])
        return HedgePosition(**data)

    # ------------------------------------------------------------------
    # Sweep history
    # ------------------------------------------------------------------

    def save_sweep_history(self, records: list[SweepRecord]) -> None:
        """Replace the entire sweep_history table with the provided records."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM sweep_history")
            cur.executemany(
                "INSERT INTO sweep_history"
                " (timestamp, amount_usdc, tx_hash, gas_used, gas_price_gwei)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (r.timestamp, r.amount_usdc, r.tx_hash, r.gas_used, r.gas_price_gwei)
                    for r in records
                ],
            )
        logger.debug("sweep_history saved (%d records)", len(records))

    def load_sweep_history(self) -> list[SweepRecord]:
        """Load all sweep records ordered by timestamp."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT timestamp, amount_usdc, tx_hash, gas_used, gas_price_gwei"
                " FROM sweep_history ORDER BY timestamp"
            ).fetchall()

        return [
            SweepRecord(
                timestamp=row["timestamp"],
                amount_usdc=row["amount_usdc"],
                tx_hash=row["tx_hash"],
                gas_used=row["gas_used"],
                gas_price_gwei=row["gas_price_gwei"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Trade log
    # ------------------------------------------------------------------

    def log_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        size: float,
        pnl: float,
        timestamp: str,
    ) -> None:
        """Append a trade record to the trades table."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO trades (symbol, side, price, size, pnl, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, side, price, size, pnl, timestamp),
            )
        logger.debug("trade logged: %s %s %.6f @ %.2f pnl=%.4f", symbol, side, size, price, pnl)

    def get_daily_pnl(self, date_str: str) -> float:
        """Return sum of pnl for all trades on the given YYYY-MM-DD date."""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total"
                " FROM trades"
                " WHERE timestamp LIKE ?",
                (f"{date_str}%",),
            ).fetchone()
        return float(row["total"]) if row else 0.0
