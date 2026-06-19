"""EdgeX REST API client — async, rate-limited, with retry and dry-run/paper-trading support."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from dotenv import load_dotenv

from src.auth import EdgeXAuth

load_dotenv()

logger = logging.getLogger(__name__)

EDGEX_API_URL = os.getenv("EDGEX_API_URL", "https://testnet-api.edgex.exchange")
EDGEX_ACCOUNT_ID = os.getenv("EDGEX_ACCOUNT_ID", "")

# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    """Async token-bucket rate limiter.

    Args:
        rate: tokens replenished per second
        capacity: maximum burst size
    """

    def __init__(self, rate: float = 10.0, capacity: float = 20.0) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens: float = capacity
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until *tokens* are available, then consume them."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self.capacity,
                    self._tokens + elapsed * self.rate,
                )
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                # How long until we have enough tokens?
                wait = (tokens - self._tokens) / self.rate

            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Paper-trading ledger
# ---------------------------------------------------------------------------

@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    side: str          # "BUY" | "SELL"
    order_type: str    # "LIMIT" | "MARKET"
    price: float       # 0.0 for market orders
    size: float
    status: str = "OPEN"   # "OPEN" | "FILLED" | "CANCELLED"
    filled_price: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class PaperPosition:
    symbol: str
    size: float = 0.0        # positive = long, negative = short
    avg_entry: float = 0.0
    realised_pnl: float = 0.0

    def unrealised_pnl(self, mark_price: float) -> float:
        if self.size == 0:
            return 0.0
        return (mark_price - self.avg_entry) * self.size


class PaperLedger:
    """In-memory paper-trading ledger.

    Maintains open orders and positions. Call `mark(symbol, price)` to
    trigger limit-order fills whenever a new price is known.
    """

    def __init__(self, initial_balance: float = 10_000.0) -> None:
        self.cash: float = initial_balance
        self._orders: dict[str, PaperOrder] = {}
        self._positions: dict[str, PaperPosition] = {}

    # ------------------------------------------------------------------
    # Order management

    def place(self, payload: dict) -> PaperOrder:
        order_id = payload.get("clientOrderId") or str(uuid.uuid4())
        order = PaperOrder(
            order_id=order_id,
            symbol=payload.get("symbol", ""),
            side=payload.get("side", "BUY").upper(),
            order_type=payload.get("type", "LIMIT").upper(),
            price=float(payload.get("price", 0)),
            size=float(payload.get("quantity", payload.get("size", 0))),
        )
        self._orders[order_id] = order

        # Market orders fill immediately at the last known price (price=0)
        if order.order_type == "MARKET":
            self._fill(order, order.price or 0.0)

        logger.info("[PAPER] Placed %s %s %s @ %s qty=%s id=%s",
                    order.order_type, order.side, order.symbol,
                    order.price, order.size, order_id)
        return order

    def cancel(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status == "OPEN":
            order.status = "CANCELLED"
            logger.info("[PAPER] Cancelled order %s", order_id)
            return True
        return False

    def cancel_all(self, symbol: str) -> list[str]:
        cancelled = []
        for order in self._orders.values():
            if order.symbol == symbol and order.status == "OPEN":
                order.status = "CANCELLED"
                cancelled.append(order.order_id)
        if cancelled:
            logger.info("[PAPER] Cancelled %d orders for %s", len(cancelled), symbol)
        return cancelled

    def open_orders(self, symbol: str) -> list[PaperOrder]:
        return [o for o in self._orders.values()
                if o.symbol == symbol and o.status == "OPEN"]

    # ------------------------------------------------------------------
    # Price update → fill crossing limit orders

    def mark(self, symbol: str, price: float) -> list[PaperOrder]:
        """Check all open limit orders for *symbol* and fill those whose
        price has crossed *price*. Returns the list of newly-filled orders."""
        filled: list[PaperOrder] = []
        for order in list(self._orders.values()):
            if order.symbol != symbol or order.status != "OPEN":
                continue
            if order.order_type != "LIMIT":
                continue

            should_fill = (
                (order.side == "BUY" and price <= order.price) or
                (order.side == "SELL" and price >= order.price)
            )
            if should_fill:
                self._fill(order, price)
                filled.append(order)
        return filled

    # ------------------------------------------------------------------
    # Internal helpers

    def _fill(self, order: PaperOrder, price: float) -> None:
        fill_price = price if price > 0 else order.price
        order.status = "FILLED"
        order.filled_price = fill_price

        pos = self._positions.setdefault(
            order.symbol, PaperPosition(symbol=order.symbol)
        )
        signed_size = order.size if order.side == "BUY" else -order.size
        notional = fill_price * order.size

        if pos.size == 0:
            pos.avg_entry = fill_price
            pos.size = signed_size
        else:
            # Same direction → update average entry
            if (pos.size > 0) == (signed_size > 0):
                total = abs(pos.size) + abs(signed_size)
                pos.avg_entry = (
                    pos.avg_entry * abs(pos.size) + fill_price * abs(signed_size)
                ) / total
                pos.size += signed_size
            else:
                # Opposite direction → realise PnL on closed portion
                close_size = min(abs(pos.size), abs(signed_size))
                pnl = (fill_price - pos.avg_entry) * close_size * (1 if pos.size > 0 else -1)
                pos.realised_pnl += pnl
                self.cash += pnl
                net = pos.size + signed_size
                pos.size = net
                if net == 0:
                    pos.avg_entry = 0.0

        # Deduct/credit cash for the notional (simplified — no margin model)
        if order.side == "BUY":
            self.cash -= notional
        else:
            self.cash += notional

        logger.info(
            "[PAPER] FILLED %s %s %s @ %.4f qty=%.4f  cash=%.2f  pnl=%.4f",
            order.side, order.symbol, order.order_type,
            fill_price, order.size, self.cash, pos.realised_pnl,
        )

    # ------------------------------------------------------------------
    # Reporting

    def summary(self, prices: dict[str, float] | None = None) -> dict:
        prices = prices or {}
        positions = []
        for sym, pos in self._positions.items():
            mark = prices.get(sym, pos.avg_entry)
            positions.append({
                "symbol": sym,
                "size": pos.size,
                "avg_entry": pos.avg_entry,
                "realised_pnl": pos.realised_pnl,
                "unrealised_pnl": pos.unrealised_pnl(mark),
            })
        return {
            "cash": self.cash,
            "open_orders": sum(
                1 for o in self._orders.values() if o.status == "OPEN"
            ),
            "positions": positions,
        }


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class EdgeXClient:
    """Async REST client for the edgeX exchange.

    Args:
        auth: an :class:`~src.auth.EdgeXAuth` instance.  If *None*, one is
              created from environment variables.
        dry_run: when *True* no real HTTP calls are made; a
                 :class:`PaperLedger` is used instead.
        base_url: override the base API URL (defaults to ``EDGEX_API_URL``).
        rate: token-bucket replenishment rate (req/s).
        burst: token-bucket capacity (max burst).
        max_retries: maximum retry attempts on 429 / 5xx responses.
    """

    # Mocked responses used in dry-run mode for endpoints that don't
    # interact with the paper ledger.
    _DRY_ACCOUNT = {
        "accountId": EDGEX_ACCOUNT_ID or "dry-run-account",
        "collateral": "10000.00",
        "availableBalance": "10000.00",
        "unrealisedPnl": "0.00",
        "realisedPnl": "0.00",
    }
    _DRY_TICKER: dict[str, Any] = {
        "symbol": "",
        "lastPrice": "0.00",
        "markPrice": "0.00",
        "indexPrice": "0.00",
        "24hChange": "0.00",
        "24hVolume": "0.00",
    }
    _DRY_FUNDING: dict[str, Any] = {
        "symbol": "",
        "fundingRate": "0.0001",
        "nextFundingTime": 0,
    }

    def __init__(
        self,
        auth: EdgeXAuth | None = None,
        dry_run: bool = False,
        base_url: str | None = None,
        rate: float = 10.0,
        burst: float = 20.0,
        max_retries: int = 5,
    ) -> None:
        self.auth = auth or EdgeXAuth()
        self.dry_run = dry_run
        self.base_url = (base_url or EDGEX_API_URL).rstrip("/")
        self.max_retries = max_retries

        self._bucket = TokenBucket(rate=rate, capacity=burst)
        self._session: aiohttp.ClientSession | None = None
        self.ledger: PaperLedger | None = PaperLedger() if dry_run else None

    # ------------------------------------------------------------------
    # Session lifecycle

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "EdgeXClient":
        await self._get_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Core HTTP helpers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        extra_headers: dict | None = None,
    ) -> dict:
        """Execute a rate-limited HTTP request with exponential-backoff retry."""
        if self.dry_run:
            logger.debug("[DRY-RUN] %s %s params=%s body=%s", method, path, params, json)
            return {"code": 0, "msg": "OK", "data": {}}

        url = f"{self.base_url}{path}"
        headers = {**self.auth.get_headers(), **(extra_headers or {})}

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._bucket.acquire()

            try:
                session = await self._get_session()
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                ) as resp:
                    if resp.status in (429, 500, 502, 503, 504):
                        body = await resp.text()
                        logger.warning(
                            "HTTP %s on %s %s (attempt %d/%d): %s",
                            resp.status, method, path,
                            attempt + 1, self.max_retries + 1, body[:200],
                        )
                        if attempt < self.max_retries:
                            backoff = (2 ** attempt) + random.uniform(0, 1)
                            await asyncio.sleep(backoff)
                            continue
                        resp.raise_for_status()

                    resp.raise_for_status()
                    return await resp.json()

            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "Network error on %s %s (attempt %d/%d): %s — retrying in %.1fs",
                        method, path, attempt + 1, self.max_retries + 1, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    raise

        # Should be unreachable, but satisfies type checkers
        raise last_exc or RuntimeError("Request failed after all retries")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict | None = None,
                    extra_headers: dict | None = None) -> dict:
        return await self._request("POST", path, json=json,
                                   extra_headers=extra_headers)

    async def _delete(self, path: str, json: dict | None = None,
                      extra_headers: dict | None = None) -> dict:
        return await self._request("DELETE", path, json=json,
                                   extra_headers=extra_headers)

    # ------------------------------------------------------------------
    # Account endpoints

    async def get_account(self) -> dict:
        """Return account info and balances.

        Returns:
            Raw API response dict (or mocked data in dry-run mode).
        """
        if self.dry_run:
            logger.info("[DRY-RUN] get_account()")
            return self._DRY_ACCOUNT.copy()
        return await self._get("/api/v1/account")

    async def get_positions(self) -> dict:
        """Return all open positions.

        Returns:
            Raw API response dict containing a list of positions.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] get_positions()")
            summary = self.ledger.summary() if self.ledger else {}
            return {"positions": summary.get("positions", [])}
        return await self._get("/api/v1/account/positions")

    # ------------------------------------------------------------------
    # Order endpoints

    async def get_open_orders(self, symbol: str) -> dict:
        """Return open orders for *symbol*.

        Args:
            symbol: trading pair, e.g. ``"BTC-USDC"``.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] get_open_orders(%s)", symbol)
            orders = [
                {
                    "orderId": o.order_id,
                    "symbol": o.symbol,
                    "side": o.side,
                    "type": o.order_type,
                    "price": str(o.price),
                    "quantity": str(o.size),
                    "status": o.status,
                }
                for o in (self.ledger.open_orders(symbol) if self.ledger else [])
            ]
            return {"orders": orders}
        return await self._get("/api/v1/orders/open", params={"symbol": symbol})

    async def place_order(self, payload: dict) -> dict:
        """Place a new order.

        Args:
            payload: order dict with at minimum ``symbol``, ``side``,
                     ``type``, ``price``, and ``quantity`` keys.
                     The payload is signed with :meth:`~src.auth.EdgeXAuth.sign_order`
                     before being sent.

        Returns:
            API response dict containing the created order details.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] place_order(%s)", payload)
            order = self.ledger.place(payload) if self.ledger else None
            return {
                "code": 0,
                "msg": "OK",
                "data": {
                    "orderId": order.order_id if order else "dry-run",
                    "status": order.status if order else "OPEN",
                },
            }
        signed = self.auth.sign_order(payload)
        return await self._post("/api/v1/order", json=signed)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a single order by ID.

        Args:
            order_id: the exchange order identifier.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] cancel_order(%s)", order_id)
            cancelled = self.ledger.cancel(order_id) if self.ledger else False
            return {
                "code": 0,
                "msg": "OK",
                "data": {"orderId": order_id, "cancelled": cancelled},
            }
        signed = self.auth.sign_cancel(order_id)
        return await self._delete(f"/api/v1/order/{order_id}", json=signed)

    async def cancel_all_orders(self, symbol: str) -> dict:
        """Cancel all open orders for *symbol*.

        Fetches open orders first, then issues individual cancel requests
        concurrently (subject to the rate limiter).

        Args:
            symbol: trading pair, e.g. ``"BTC-USDC"``.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] cancel_all_orders(%s)", symbol)
            cancelled = self.ledger.cancel_all(symbol) if self.ledger else []
            return {
                "code": 0,
                "msg": "OK",
                "data": {"cancelledOrderIds": cancelled},
            }

        open_resp = await self.get_open_orders(symbol)
        orders = open_resp.get("data", {}).get("orders", []) or open_resp.get("orders", [])
        if not orders:
            return {"code": 0, "msg": "OK", "data": {"cancelledOrderIds": []}}

        tasks = [self.cancel_order(o["orderId"]) for o in orders]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled_ids = []
        for order, result in zip(orders, results):
            if not isinstance(result, Exception):
                cancelled_ids.append(order["orderId"])
        return {
            "code": 0,
            "msg": "OK",
            "data": {"cancelledOrderIds": cancelled_ids},
        }

    # ------------------------------------------------------------------
    # Market data endpoints

    async def get_ticker(self, symbol: str) -> dict:
        """Return the latest ticker for *symbol*.

        Args:
            symbol: trading pair, e.g. ``"BTC-USDC"``.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] get_ticker(%s)", symbol)
            ticker = {**self._DRY_TICKER, "symbol": symbol}
            # Notify the paper ledger so it can fill crossing limit orders
            if self.ledger:
                price_str = ticker.get("lastPrice", "0")
                try:
                    price = float(price_str)
                    self.ledger.mark(symbol, price)
                except (ValueError, TypeError):
                    pass
            return ticker
        resp = await self._get("/api/v1/market/ticker", params={"symbol": symbol})
        # Feed real price into ledger (if somehow dry_run is partly enabled)
        return resp

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
    ) -> dict:
        """Return historical candlestick (kline) data.

        Args:
            symbol: trading pair.
            interval: candle interval, e.g. ``"1m"``, ``"5m"``, ``"1h"``.
            limit: number of candles to return (max 1000).
        """
        if self.dry_run:
            logger.info("[DRY-RUN] get_klines(%s, %s, %d)", symbol, interval, limit)
            return {"symbol": symbol, "interval": interval, "candles": []}
        return await self._get(
            "/api/v1/market/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def get_funding_rate(self, symbol: str) -> dict:
        """Return the current funding rate for *symbol*.

        Args:
            symbol: trading pair.
        """
        if self.dry_run:
            logger.info("[DRY-RUN] get_funding_rate(%s)", symbol)
            return {**self._DRY_FUNDING, "symbol": symbol}
        return await self._get("/api/v1/market/funding-rate", params={"symbol": symbol})

    # ------------------------------------------------------------------
    # Convenience helpers

    async def update_paper_price(self, symbol: str, price: float) -> list[dict]:
        """Feed a new mark price into the paper ledger and return filled orders.

        Only meaningful in dry-run mode; no-op otherwise.

        Args:
            symbol: trading pair.
            price: new mark price.
        """
        if not self.dry_run or self.ledger is None:
            return []
        filled = self.ledger.mark(symbol, price)
        return [
            {
                "orderId": o.order_id,
                "symbol": o.symbol,
                "side": o.side,
                "filledPrice": o.filled_price,
                "size": o.size,
            }
            for o in filled
        ]

    def paper_summary(self, prices: dict[str, float] | None = None) -> dict:
        """Return the current paper-trading account summary.

        Args:
            prices: optional ``{symbol: mark_price}`` mapping used to
                    compute unrealised PnL.
        """
        if not self.dry_run or self.ledger is None:
            return {}
        return self.ledger.summary(prices)
