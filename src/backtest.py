"""Backtesting engine — replays historical OHLCV data through Engine A logic.

Usage (CLI):
    python src/backtest.py --symbol BTCUSDT --interval 1m --days 30 --capital 10000
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

import requests

# ---------------------------------------------------------------------------
# Inline copies of constants / helpers from engine_a / data_fetcher
# (kept here so the backtest is self-contained with no async overhead)
# ---------------------------------------------------------------------------

BOX_LOOKBACK: int = int(os.getenv("BOX_LOOKBACK", "50"))
VOLUME_SPIKE_MULTIPLIER: float = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3.0"))
VOLUME_MA_WINDOW: int = 20          # mirrors VOLUME_MA_MINUTES in data_fetcher
TRAILING_STOP_PCT: float = 0.02
STOP_LOSS_PCT: float = 0.015

BINANCE_TESTNET: bool = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
BINANCE_BASE: str = (
    "https://testnet.binance.vision/api/v3"
    if BINANCE_TESTNET
    else "https://api.binance.com/api/v3"
)

ANNUALISATION_FACTOR: float = 252 * 24 * 60  # minutes per trading year (1-min candles)


# ---------------------------------------------------------------------------
# Minimal Candle — mirrors data_fetcher.Candle so we can import selectively
# ---------------------------------------------------------------------------

try:
    from .data_fetcher import Candle  # type: ignore
except ImportError:
    @dataclass
    class Candle:  # type: ignore[no-redef]
        open_time: int
        open: float
        high: float
        low: float
        close: float
        volume: float


# ---------------------------------------------------------------------------
# BacktestDataLoader
# ---------------------------------------------------------------------------

class BacktestDataLoader:
    """Load historical OHLCV data from a CSV file or the Binance REST API."""

    @staticmethod
    def from_csv(path: str) -> List[Candle]:
        """Load candles from a CSV file.

        Expected columns (order matters if header is absent):
            open_time, open, high, low, close, volume
        """
        candles: List[Candle] = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                candles.append(
                    Candle(
                        open_time=int(float(row["open_time"])),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )
        return candles

    @staticmethod
    def from_binance_api(
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> List[Candle]:
        """Fetch klines from the Binance REST API, paginating as needed.

        Args:
            symbol:    e.g. "BTCUSDT"
            interval:  e.g. "1m", "5m", "1h"
            start_ms:  start time as Unix milliseconds
            end_ms:    end time as Unix milliseconds

        Returns:
            List of Candle objects sorted by open_time ascending.
        """
        url = f"{BINANCE_BASE}/klines"
        candles: List[Candle] = []
        current_start = start_ms
        limit = 1000  # Binance max per request

        while current_start < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": limit,
            }
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break

            for row in rows:
                candles.append(
                    Candle(
                        open_time=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )

            last_open_time = int(rows[-1][0])
            if last_open_time >= end_ms or len(rows) < limit:
                break
            # Advance past the last returned candle
            current_start = last_open_time + 1

        # De-duplicate and sort
        seen: set = set()
        unique: List[Candle] = []
        for c in candles:
            if c.open_time not in seen:
                seen.add(c.open_time)
                unique.append(c)
        unique.sort(key=lambda c: c.open_time)
        return unique


# ---------------------------------------------------------------------------
# BacktestResult
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Aggregated performance metrics from a completed backtest run."""

    total_pnl: float
    total_trades: int
    win_rate: float                  # 0.0 – 1.0
    max_drawdown_pct: float          # e.g. 0.15 = 15 %
    sharpe_ratio: float              # annualised
    equity_curve: List[float]        # capital at each candle
    trades: List[dict]               # [{open_time, side, entry, exit, pnl}, …]

    def summary(self) -> str:
        lines = [
            "═══════════════════════════════════════",
            "          BACKTEST SUMMARY             ",
            "═══════════════════════════════════════",
            f"  Total PnL         : {self.total_pnl:+.4f} USDT",
            f"  Total Trades      : {self.total_trades}",
            f"  Win Rate          : {self.win_rate * 100:.1f}%",
            f"  Max Drawdown      : {self.max_drawdown_pct * 100:.2f}%",
            f"  Sharpe Ratio      : {self.sharpe_ratio:.3f}",
            f"  Final Capital     : {self.equity_curve[-1]:.4f}" if self.equity_curve else "",
            "═══════════════════════════════════════",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal state helpers (mirrors engine_a logic, pure / synchronous)
# ---------------------------------------------------------------------------

def _calc_box(candles_window: List[Candle]):
    """Return (support, resistance) from the lookback window."""
    support = min(c.low for c in candles_window)
    resistance = max(c.high for c in candles_window)
    return support, resistance


def _vol_ma(candles_window: List[Candle]) -> float:
    """Rolling mean of volume over up to VOLUME_MA_WINDOW candles."""
    if not candles_window:
        return 0.0
    window = candles_window[-VOLUME_MA_WINDOW:]
    return sum(c.volume for c in window) / len(window)


def _is_volume_spike(candles_window: List[Candle]) -> bool:
    """True when the latest candle's volume exceeds 3× the 20-candle MA."""
    if len(candles_window) < 2:
        return False
    ma = _vol_ma(candles_window[:-1])
    if ma == 0:
        return False
    return candles_window[-1].volume >= ma * VOLUME_SPIKE_MULTIPLIER


def _sharpe(returns: List[float]) -> float:
    """Annualised Sharpe ratio from per-candle returns (assumes 1-min candles)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0
    if std_r == 0:
        return 0.0
    return (mean_r / std_r) * math.sqrt(ANNUALISATION_FACTOR)


def _max_drawdown(equity: List[float]) -> float:
    """Maximum drawdown as a fraction (e.g. 0.20 = 20 %)."""
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Replay historical candles through Engine A's box/breakout logic.

    Parameters
    ----------
    candles:
        Full list of historical Candle objects, sorted ascending by open_time.
    initial_capital:
        Starting capital in quote currency (e.g. USDT).
    engine_a_allocation:
        Fraction of total capital allocated to this engine (default 0.25,
        mirroring ENGINE_A_ALLOCATION from engine_a.py).
    """

    def __init__(
        self,
        candles: List[Candle],
        initial_capital: float,
        engine_a_allocation: float = 0.25,
    ) -> None:
        self.candles = candles
        self.initial_capital = initial_capital
        self.engine_a_allocation = engine_a_allocation

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        candles = self.candles
        allocated = self.initial_capital * self.engine_a_allocation
        free_capital = allocated          # capital not in a position
        total_capital = self.initial_capital

        # Strategy state
        mode = "BOX"                      # "BOX" | "BREAKOUT"
        position: Optional[dict] = None   # active position
        box_orders: List[dict] = []       # pending limit orders for box strategy

        trades: List[dict] = []
        equity_curve: List[float] = [total_capital]
        candle_buffer: List[Candle] = []  # rolling window for indicators

        for i, candle in enumerate(candles):
            candle_buffer.append(candle)
            # Keep buffer bounded (same as deque maxlen=500 in data_fetcher)
            if len(candle_buffer) > 500:
                candle_buffer.pop(0)

            price = candle.close

            # ── 1. Check / fill pending limit orders ──────────────────
            filled_orders = []
            for order in box_orders:
                filled, fill_price = self._try_fill_limit(order, candle)
                if filled:
                    if order["side"] == "BUY":
                        # Enter long position
                        size = order["size"]
                        cost = fill_price * size
                        if cost <= free_capital:
                            free_capital -= cost
                            position = {
                                "side": "BUY",
                                "entry_price": fill_price,
                                "size": size,
                                "peak_price": fill_price,
                                "open_time": candle.open_time,
                            }
                            filled_orders.append(order)
                    elif order["side"] == "SELL" and position is not None:
                        # Close long via box sell limit
                        pnl, free_capital = self._close_position(
                            position, fill_price, free_capital, candle.open_time, trades
                        )
                        total_capital += pnl
                        position = None
                        filled_orders.append(order)

            box_orders = [o for o in box_orders if o not in filled_orders]

            # ── 2. Risk management for open position ──────────────────
            if position is not None:
                # Update peak for trailing stop
                if position["side"] == "BUY":
                    position["peak_price"] = max(position["peak_price"], candle.high)
                else:
                    position["peak_price"] = min(position["peak_price"], candle.low)

                ep = position["entry_price"]
                pk = position["peak_price"]
                triggered = False

                if position["side"] == "BUY":
                    stop_loss_hit = candle.low <= ep * (1 - STOP_LOSS_PCT)
                    trailing_hit = candle.low <= pk * (1 - TRAILING_STOP_PCT)
                else:
                    stop_loss_hit = candle.high >= ep * (1 + STOP_LOSS_PCT)
                    trailing_hit = candle.high >= pk * (1 + TRAILING_STOP_PCT)

                if stop_loss_hit or trailing_hit:
                    # Fill at the trigger price (conservative estimate)
                    if stop_loss_hit:
                        exit_price = ep * (1 - STOP_LOSS_PCT) if position["side"] == "BUY" else ep * (1 + STOP_LOSS_PCT)
                    else:
                        exit_price = pk * (1 - TRAILING_STOP_PCT) if position["side"] == "BUY" else pk * (1 + TRAILING_STOP_PCT)

                    pnl, free_capital = self._close_position(
                        position, exit_price, free_capital, candle.open_time, trades
                    )
                    total_capital += pnl
                    position = None
                    # After stop/exit, revert to BOX mode (mirrors engine reset)
                    mode = "BOX"
                    box_orders.clear()
                    triggered = True

            # ── 3. Strategy logic (mirrors on_market_update) ──────────
            window = candle_buffer

            is_spike = _is_volume_spike(window)

            if is_spike and mode == "BOX":
                # Cancel box orders, switch to breakout
                box_orders.clear()
                mode = "BREAKOUT"
                # Enter market-like order (fill at close)
                if position is None and len(window) >= 2:
                    side = "BUY" if price > window[-2].close else "SELL"
                    size = free_capital / price if price > 0 else 0.0
                    if size > 0:
                        cost = price * size
                        free_capital -= cost
                        position = {
                            "side": side,
                            "entry_price": price,
                            "size": size,
                            "peak_price": price,
                            "open_time": candle.open_time,
                        }

            elif mode == "BOX" and not box_orders and position is None:
                # Place box limit orders if we have enough lookback
                if len(window) >= BOX_LOOKBACK:
                    lb = window[-BOX_LOOKBACK:]
                    support, resistance = _calc_box(lb)
                    order_size = (free_capital * 0.5) / price if price > 0 else 0.0
                    if order_size > 0:
                        box_orders = [
                            {"side": "BUY",  "price": support * 1.001,    "size": order_size},
                            {"side": "SELL", "price": resistance * 0.999, "size": order_size},
                        ]

            # After a breakout position is closed by stop, mode resets to BOX
            # (handled above in risk-management block)

            # ── 4. Mark-to-market equity ──────────────────────────────
            unrealised = 0.0
            if position is not None:
                ep = position["entry_price"]
                sz = position["size"]
                if position["side"] == "BUY":
                    unrealised = (price - ep) * sz
                else:
                    unrealised = (ep - price) * sz

            # Non-allocated capital stays at initial_capital * (1 - allocation)
            non_allocated = self.initial_capital * (1 - self.engine_a_allocation)
            equity_curve.append(non_allocated + free_capital + unrealised)

        # ── End of replay: close any open position at last price ──────
        if position is not None and candles:
            last_price = candles[-1].close
            last_time = candles[-1].open_time
            pnl, free_capital = self._close_position(
                position, last_price, free_capital, last_time, trades
            )
            total_capital += pnl

        # ── Metrics ───────────────────────────────────────────────────
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / len(trades) if trades else 0.0
        total_pnl = sum(t["pnl"] for t in trades)

        # Per-candle returns for Sharpe
        returns = []
        for j in range(1, len(equity_curve)):
            prev = equity_curve[j - 1]
            returns.append((equity_curve[j] - prev) / prev if prev else 0.0)

        sharpe = _sharpe(returns)
        max_dd = _max_drawdown(equity_curve)

        return BacktestResult(
            total_pnl=total_pnl,
            total_trades=len(trades),
            win_rate=win_rate,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            equity_curve=equity_curve,
            trades=trades,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_fill_limit(order: dict, candle: Candle):
        """Return (filled: bool, fill_price: float).

        A limit BUY fills when candle.low <= order price.
        A limit SELL fills when candle.high >= order price.
        Fill price is exactly the limit price (assumes it's within the candle range).
        """
        lp = order["price"]
        if order["side"] == "BUY" and candle.low <= lp:
            return True, lp
        if order["side"] == "SELL" and candle.high >= lp:
            return True, lp
        return False, 0.0

    @staticmethod
    def _close_position(
        position: dict,
        exit_price: float,
        free_capital: float,
        close_time: int,
        trades: List[dict],
    ):
        """Close position, record trade, return (pnl, updated_free_capital)."""
        ep = position["entry_price"]
        sz = position["size"]
        side = position["side"]

        if side == "BUY":
            pnl = (exit_price - ep) * sz
        else:
            pnl = (ep - exit_price) * sz

        # Return cost basis + pnl to free capital
        free_capital += ep * sz + pnl

        trades.append(
            {
                "open_time": position["open_time"],
                "close_time": close_time,
                "side": side,
                "entry": ep,
                "exit": exit_price,
                "size": sz,
                "pnl": pnl,
            }
        )
        return pnl, free_capital


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Run Engine A backtest from Binance historical data."
    )
    parser.add_argument("--symbol",   default="BTCUSDT",  help="Binance symbol (default: BTCUSDT)")
    parser.add_argument("--interval", default="1m",       help="Candle interval (default: 1m)")
    parser.add_argument("--days",     type=int, default=30, help="Number of days to backtest (default: 30)")
    parser.add_argument("--capital",  type=float, default=10000.0, help="Initial capital in USDT (default: 10000)")
    parser.add_argument("--allocation", type=float, default=0.25,  help="Engine A allocation fraction (default: 0.25)")
    parser.add_argument("--csv",      default=None, help="Path to CSV file (skips API fetch)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.csv:
        print(f"Loading candles from CSV: {args.csv}")
        candles = BacktestDataLoader.from_csv(args.csv)
    else:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - args.days * 24 * 60 * 60 * 1000
        print(
            f"Fetching {args.days}d of {args.interval} candles for {args.symbol} "
            f"from Binance ({'testnet' if BINANCE_TESTNET else 'mainnet'}) …"
        )
        candles = BacktestDataLoader.from_binance_api(
            symbol=args.symbol,
            interval=args.interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    print(f"Loaded {len(candles)} candles.")

    engine = BacktestEngine(
        candles=candles,
        initial_capital=args.capital,
        engine_a_allocation=args.allocation,
    )
    result = engine.run()
    print(result.summary())
