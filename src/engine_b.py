"""[단계 4] 엔진 B: 펀딩비 APR 순위 기반 최적 페어 선정 + asyncio 동시 헷지 진입."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import aiohttp
import requests
from dotenv import load_dotenv

from .auth import EdgeXAuth
from .data_fetcher import FundingInfo, MarketData, fetch_all_funding_aprs

load_dotenv()
logger = logging.getLogger(__name__)

EDGEX_API_URL = os.getenv("EDGEX_API_URL", "https://testnet-api.edgex.exchange")
ENGINE_B_ALLOCATION = float(os.getenv("ENGINE_B_ALLOCATION", "0.75"))
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

BINANCE_SPOT_BASE = (
    "https://testnet.binance.vision/api/v3"
    if BINANCE_TESTNET
    else "https://api.binance.com/api/v3"
)
BINANCE_FAPI = (
    "https://testnet.binancefuture.com/fapi/v1"
    if BINANCE_TESTNET
    else "https://fapi.binance.com/fapi/v1"
)

MIN_APR_THRESHOLD = 0.05  # 연 5% 이상인 페어만 선정

EXIT_MIN_APR = float(os.getenv("EXIT_MIN_APR", "3.0"))          # 연 3% 미만 시 청산
EXIT_LOSS_THRESHOLD = float(os.getenv("EXIT_LOSS_THRESHOLD", "0.02"))  # 총 미실현손실 2% 초과 시 청산


@dataclass
class HedgePosition:
    symbol: str = ""
    edgex_short_size: float = 0.0
    edgex_short_entry: float = 0.0
    edgex_order_id: str = ""
    binance_long_size: float = 0.0
    binance_long_entry: float = 0.0
    binance_order_id: str = ""
    total_funding_received: float = 0.0
    last_rebalance_time: float = 0.0
    entry_basis: float = 0.0   # 진입 시 (edgex_entry - binance_entry) / binance_entry
    entry_time: float = 0.0    # 진입 Unix timestamp


class EngineB:
    def __init__(self, auth: EdgeXAuth, dry_run: bool = False):
        self.auth = auth
        self.dry_run = dry_run
        self.symbol: str = ""
        self.position = HedgePosition()
        self.allocated_capital: float = 0.0

        self._binance_api_key = os.getenv("BINANCE_API_KEY", "")
        self._binance_secret = os.getenv("BINANCE_SECRET_KEY", "")

        self._edgex_session = requests.Session()
        self._edgex_session.headers.update(auth.get_headers())

        self._funding_rate_history: list[tuple[float, float]] = []

    # --- Pair selection ---

    def select_best_pair(self) -> dict | None:
        """펀딩비 APR 순위에서 상위 페어 중 edgeX 상장 여부를 고려해 선정합니다."""
        rankings = fetch_all_funding_aprs(top_n=10)
        for item in rankings:
            if item["apr"] < MIN_APR_THRESHOLD * 100:
                continue
            # 바이낸스 심볼 "BTCUSDT" → edgeX 심볼 "BTC-USDC"
            raw = item["symbol"]
            if raw.endswith("USDT"):
                edgex_sym = raw[:-4] + "-USDC"
            elif raw.endswith("USDC"):
                edgex_sym = raw[:-4] + "-USDC"
            else:
                continue
            logger.info("최적 페어 선정: %s (APR=%.2f%%)", edgex_sym, item["apr"])
            return {**item, "edgex_symbol": edgex_sym}
        return None

    # --- edgeX short (async) ---

    async def _open_edgex_short_async(
        self, symbol: str, price: float, size: float
    ) -> str | None:
        if self.dry_run:
            fake_id = f"DRY-SHORT-{symbol}-{int(size*1e4)}"
            logger.info("[DRY-RUN] edgeX 숏: %s %.6f @ %.2f", symbol, size, price)
            return fake_id

        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": symbol,
            "side": "SELL",
            "price": "0",
            "size": str(round(size, 6)),
            "type": "MARKET",
        }
        signed = self.auth.sign_order(payload)
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{EDGEX_API_URL}/api/v1/order",
                    json=signed,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    oid = data.get("orderId")
                    logger.info("edgeX 숏 진입: %s %.6f @ %.2f (id=%s)", symbol, size, price, oid)
                    return oid
            except Exception as exc:
                logger.error("edgeX 숏 주문 실패: %s", exc)
                return None

    # --- Binance spot long (async) ---

    async def _open_binance_long_async(
        self, binance_symbol: str, size: float, price: float = 0.0
    ) -> dict | None:
        if self.dry_run:
            logger.info("[DRY-RUN] Binance 롱: %s %.6f @ %.2f", binance_symbol, size, price)
            return {"orderId": f"DRY-LONG-{binance_symbol}", "fills": [{"price": str(price)}]}

        import hashlib
        import hmac
        import urllib.parse

        timestamp = int(time.time() * 1000)
        params = {
            "symbol": binance_symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": round(size, 6),
            "timestamp": timestamp,
        }
        query = urllib.parse.urlencode(params)
        sig = hmac.new(
            self._binance_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = sig

        headers = {"X-MBX-APIKEY": self._binance_api_key}
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.post(
                    f"{BINANCE_SPOT_BASE}/order",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    avg_price = float(
                        (data.get("fills") or [{}])[0].get("price", 0)
                    )
                    logger.info("Binance 롱 진입: %s %.6f @ %.2f", binance_symbol, size, avg_price)
                    return data
            except Exception as exc:
                logger.error("Binance 롱 주문 실패: %s", exc)
                return None

    # --- edgeX short close (async) ---

    async def _close_edgex_short_async(self, symbol, price, size) -> str | None:
        """edgeX 숏 청산 (BUY 반대매매). dry_run 지원."""
        if self.dry_run:
            logger.info("[DRY-RUN] edgeX 숏 청산: %s %.6f @ %.2f", symbol, size, price)
            return "DRY-CLOSE-EDGEX"

        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": symbol,
            "side": "BUY",
            "price": "0",
            "size": str(round(size, 6)),
            "type": "MARKET",
            "reduceOnly": True,
        }
        signed = self.auth.sign_order(payload)
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{EDGEX_API_URL}/api/v1/order",
                    json=signed,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    oid = data.get("orderId")
                    logger.info("edgeX 숏 청산: %s %.6f @ %.2f (id=%s)", symbol, size, price, oid)
                    return oid
            except Exception as exc:
                logger.error("edgeX 숏 청산 실패: %s", exc)
                return None

    # --- Binance long close (async) ---

    async def _close_binance_long_async(self, binance_symbol, size) -> dict | None:
        """Binance 현물 SELL 청산. dry_run 지원."""
        if self.dry_run:
            logger.info("[DRY-RUN] Binance 롱 청산: %s %.6f", binance_symbol, size)
            return {"orderId": "DRY-CLOSE-BINANCE"}

        import hashlib
        import hmac
        import urllib.parse

        timestamp = int(time.time() * 1000)
        params = {
            "symbol": binance_symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": round(size, 6),
            "timestamp": timestamp,
        }
        query = urllib.parse.urlencode(params)
        sig = hmac.new(
            self._binance_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = sig

        headers = {"X-MBX-APIKEY": self._binance_api_key}
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.post(
                    f"{BINANCE_SPOT_BASE}/order",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    logger.info("Binance 롱 청산: %s %.6f", binance_symbol, size)
                    return data
            except Exception as exc:
                logger.error("Binance 롱 청산 실패: %s", exc)
                return None

    # --- Simultaneous hedge entry (asyncio) ---

    async def enter_hedge_async(self, symbol: str, price: float, size: float) -> None:
        """edgeX 숏 + Binance 롱을 asyncio.gather로 동시에 실행합니다."""
        binance_symbol = symbol.replace("-USDC", "USDT").replace("-", "")
        self.symbol = symbol

        edgex_task = self._open_edgex_short_async(symbol, price, size)
        binance_task = self._open_binance_long_async(binance_symbol, size, price)

        edgex_result, binance_result = await asyncio.gather(
            edgex_task, binance_task, return_exceptions=True
        )

        if isinstance(edgex_result, Exception):
            logger.error("edgeX 진입 실패: %s", edgex_result)
        else:
            self.position.edgex_short_size = size
            self.position.edgex_short_entry = price
            self.position.edgex_order_id = edgex_result or ""

        if isinstance(binance_result, Exception):
            logger.error("Binance 진입 실패: %s", binance_result)
        else:
            fills = (binance_result or {}).get("fills", [{}])
            avg = float(fills[0].get("price", price)) if fills else price
            self.position.binance_long_size = size
            self.position.binance_long_entry = avg
            self.position.binance_order_id = str((binance_result or {}).get("orderId", ""))

        # entry_basis 및 entry_time 설정
        if self.position.binance_long_entry > 0:
            self.position.entry_basis = (
                (self.position.edgex_short_entry - self.position.binance_long_entry)
                / self.position.binance_long_entry
            )
        self.position.entry_time = time.time()
        self.position.symbol = symbol

    def enter_hedge(self, market: MarketData) -> None:
        """동기 진입 래퍼 (이미 실행 중인 루프가 없을 때 사용)."""
        pair = self.select_best_pair() or {"edgex_symbol": self.symbol or "BTC-USDC"}
        symbol = pair["edgex_symbol"]
        price = market.last_price
        if self.allocated_capital <= 0 or price <= 0:
            return
        size = self.allocated_capital / price
        asyncio.run(self.enter_hedge_async(symbol, price, size))

    # --- Simultaneous hedge exit (asyncio) ---

    async def exit_hedge_async(self, market: MarketData) -> None:
        """edgeX 숏 + Binance 롱을 asyncio.gather로 동시 청산 후 position 초기화."""
        symbol = self.position.symbol or self.symbol
        binance_symbol = symbol.replace("-USDC", "USDT").replace("-", "")
        logger.warning("🚪 [엔진B EXIT] 시작: %s %.6f", symbol, self.position.edgex_short_size)
        results = await asyncio.gather(
            self._close_edgex_short_async(symbol, market.last_price, self.position.edgex_short_size),
            self._close_binance_long_async(binance_symbol, self.position.binance_long_size),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error("청산 실패 [%s]: %s", ["edgeX", "Binance"][i], r)
        self.position = HedgePosition()
        logger.info("✅ [엔진B EXIT] 포지션 초기화 완료")

    # --- Rebalance ---

    def is_delta_neutral(self, tolerance: float = 0.01) -> bool:
        s = self.position.edgex_short_size
        if s == 0:
            return True
        return abs(s - self.position.binance_long_size) / s <= tolerance

    async def rebalance(self, market: MarketData) -> None:
        now = time.time()
        if now - self.position.last_rebalance_time < 300:
            return
        diff = self.position.edgex_short_size - self.position.binance_long_size
        if abs(diff) < 1e-6:
            return
        if diff > 0:
            binance_sym = self.symbol.replace("-USDC", "USDT")
            await self._open_binance_long_async(binance_sym, diff, market.last_price)
        else:
            await self._open_edgex_short_async(self.symbol, market.last_price, -diff)
        self.position.last_rebalance_time = now

    # --- Funding tracking ---

    def record_funding(self, funding: FundingInfo, current_price: float) -> None:
        now_ms = int(time.time() * 1000)
        if funding.next_funding_time > 0 and funding.next_funding_time <= now_ms:
            notional_value = self.position.edgex_short_size * current_price
            receipt = funding.funding_rate * notional_value
            self.position.total_funding_received += receipt
            self._funding_rate_history.append((time.time(), funding.funding_apr))
            logger.info(
                "펀딩비 수령: %.6f USDC | APR=%.2f%% | 누적=%.4f",
                receipt,
                funding.funding_apr,
                self.position.total_funding_received,
            )

    def _calculate_72h_avg_apr(self) -> float | None:
        """최근 72시간 평균 APR을 계산합니다. 3개 미만이면 None 반환."""
        cutoff = time.time() - 72 * 3600
        recent = [apr for ts, apr in self._funding_rate_history if ts >= cutoff]
        if len(recent) < 3:
            return None
        return sum(recent) / len(recent)

    # --- Unrealized PnL ---

    def _calculate_unrealized_pnl(self, current_price: float) -> float:
        """헤지 포지션 총 미실현 PnL (숏 + 롱 합산)."""
        if self.position.edgex_short_size == 0:
            return 0.0
        short_pnl = (self.position.edgex_short_entry - current_price) * self.position.edgex_short_size
        long_pnl = (current_price - self.position.binance_long_entry) * self.position.binance_long_size
        return short_pnl + long_pnl + self.position.total_funding_received

    # --- EXIT conditions ---

    async def _check_exit_conditions(self, market: MarketData) -> None:
        """EXIT 조건 체크: APR 하락 또는 미실현 손실 초과."""
        if self.position.edgex_short_size == 0:
            return
        # 조건1: 72h 평균 APR < EXIT_MIN_APR
        avg_apr = self._calculate_72h_avg_apr()
        if avg_apr is not None and avg_apr < EXIT_MIN_APR:
            logger.warning("🚪 72h APR=%.2f%% < %.1f%% → EXIT", avg_apr, EXIT_MIN_APR)
            await self.exit_hedge_async(market)
            return
        # 조건2: 총 미실현 손실 > 초기 투입 자본의 EXIT_LOSS_THRESHOLD
        upnl = self._calculate_unrealized_pnl(market.last_price)
        cost_basis = self.position.edgex_short_entry * self.position.edgex_short_size
        if cost_basis > 0 and upnl < -cost_basis * EXIT_LOSS_THRESHOLD:
            logger.warning("🚪 미실현 손실 %.2f USDC (%.2f%%) → EXIT", upnl, -upnl / cost_basis * 100)
            await self.exit_hedge_async(market)

    # --- Main tick ---

    async def on_market_update(self, market: MarketData) -> None:
        await self._check_exit_conditions(market)
        if market.funding:
            self.record_funding(market.funding, market.last_price)
        if self.position.edgex_short_size > 0 and not self.is_delta_neutral():
            await self.rebalance(market)

    def set_capital(self, total_capital: float) -> None:
        self.allocated_capital = total_capital * ENGINE_B_ALLOCATION
