"""[단계 2] 실시간 시세, 거래량, 펀딩비 APR 수집 모듈."""

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd
import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

EDGEX_WS_URL = os.getenv("EDGEX_WS_URL", "wss://testnet-ws.edgex.exchange")
EDGEX_API_URL = os.getenv("EDGEX_API_URL", "https://testnet-api.edgex.exchange")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

BINANCE_BASE = (
    "https://testnet.binance.vision/api/v3"
    if BINANCE_TESTNET
    else "https://api.binance.com/api/v3"
)
BINANCE_FAPI = (
    "https://testnet.binancefuture.com/fapi/v1"
    if BINANCE_TESTNET
    else "https://fapi.binance.com/fapi/v1"
)

VOLUME_MA_MINUTES = 20  # 20분 평균 거래량 윈도우
FUNDING_PERIODS_PER_YEAR = 3 * 365  # 8시간마다 펀딩 → 연 1095회


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FundingInfo:
    symbol: str
    funding_rate: float       # 8시간 단위 펀딩률
    funding_apr: float        # 연환산 펀딩 APR
    next_funding_time: int    # Unix ms


@dataclass
class MarketData:
    symbol: str
    last_price: float = 0.0
    candles: deque = field(default_factory=lambda: deque(maxlen=500))
    volume_ma_20m: float = 0.0    # 최근 20분 평균 거래량
    volume_ma_200: float = 0.0    # 200캔들 이동평균 (엔진 A spike 감지용)
    funding: FundingInfo | None = None


# --- Funding APR helpers ---

def funding_rate_to_apr(rate_8h: float) -> float:
    """8시간 펀딩률을 연환산 APR(%)로 변환합니다."""
    return rate_8h * FUNDING_PERIODS_PER_YEAR * 100


def fetch_all_funding_aprs(top_n: int = 20) -> list[dict]:
    """바이낸스 무기한 선물 전 페어의 펀딩비 APR 순위를 반환합니다.

    Returns:
        [{"symbol": str, "fundingRate": float, "apr": float}, ...] 내림차순
    """
    url = f"{BINANCE_FAPI}/premiumIndex"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = []
    for item in data:
        rate = float(item.get("lastFundingRate", 0))
        result.append(
            {
                "symbol": item["symbol"],
                "fundingRate": rate,
                "apr": funding_rate_to_apr(rate),
            }
        )

    result.sort(key=lambda x: x["apr"], reverse=True)
    return result[:top_n]


class DataFetcher:
    def __init__(self, symbol: str = "BTC-USDC"):
        self.symbol = symbol
        self.binance_symbol = symbol.replace("-", "")
        self.market_data = MarketData(symbol=symbol)
        self._price_callbacks: list[Callable] = []
        self._candle_callbacks: list[Callable] = []
        self._running = False

    # --- Callback registration ---

    def on_price_update(self, cb: Callable) -> None:
        self._price_callbacks.append(cb)

    def on_candle_update(self, cb: Callable) -> None:
        self._candle_callbacks.append(cb)

    # --- REST helpers ---

    def fetch_binance_klines(self, interval: str = "1m", limit: int = 300) -> list[Candle]:
        url = f"{BINANCE_BASE}/klines"
        resp = requests.get(
            url,
            params={"symbol": self.binance_symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        candles = [
            Candle(
                open_time=row[0],
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in resp.json()
        ]
        return candles

    def fetch_binance_funding(self) -> FundingInfo:
        url = f"{BINANCE_FAPI}/premiumIndex"
        resp = requests.get(url, params={"symbol": self.binance_symbol}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get("lastFundingRate", 0))
        return FundingInfo(
            symbol=self.symbol,
            funding_rate=rate,
            funding_apr=funding_rate_to_apr(rate),
            next_funding_time=int(data.get("nextFundingTime", 0)),
        )

    def fetch_edgex_price(self) -> float:
        url = f"{EDGEX_API_URL}/api/v1/market/ticker"
        resp = requests.get(url, params={"symbol": self.symbol}, timeout=10)
        resp.raise_for_status()
        return float(resp.json().get("lastPrice", 0))

    def _recalculate_volume_mas(self) -> None:
        candles = list(self.market_data.candles)
        if not candles:
            return
        df = pd.DataFrame([c.__dict__ for c in candles])
        # 20분 MA (최근 20개 1분봉 = 20분)
        self.market_data.volume_ma_20m = float(
            df["volume"].rolling(window=VOLUME_MA_MINUTES, min_periods=1).mean().iloc[-1]
        )
        # 200캔들 MA
        self.market_data.volume_ma_200 = float(
            df["volume"].rolling(window=200, min_periods=1).mean().iloc[-1]
        )

    def get_volume_dataframe(self) -> pd.DataFrame:
        candles = list(self.market_data.candles)
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame([c.__dict__ for c in candles])
        df["vol_ma_20"] = df["volume"].rolling(window=VOLUME_MA_MINUTES, min_periods=1).mean()
        df["vol_ma_200"] = df["volume"].rolling(window=200, min_periods=1).mean()
        return df

    # --- Async streaming ---

    async def _stream_edgex_ws(self) -> None:
        edgex_symbol = self.symbol.replace("-", "_").lower()
        uri = f"{EDGEX_WS_URL}/stream?streams={edgex_symbol}@trade"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(uri, ping_interval=20) as ws:
                    backoff = 1
                    async for raw in ws:
                        msg = json.loads(raw)
                        price = float(msg.get("p", 0) or msg.get("price", 0))
                        if price:
                            self.market_data.last_price = price
                            for cb in self._price_callbacks:
                                await _maybe_await(cb, price)
            except Exception:
                if self._running:
                    await asyncio.sleep(min(backoff, 30))
                    backoff *= 2

    async def _poll_candles(self, interval_sec: int = 60) -> None:
        while self._running:
            try:
                candles = self.fetch_binance_klines()
                self.market_data.candles.clear()
                self.market_data.candles.extend(candles)
                self._recalculate_volume_mas()
                for cb in self._candle_callbacks:
                    await _maybe_await(cb, self.market_data)
            except Exception:
                pass
            await asyncio.sleep(interval_sec)

    async def _poll_funding(self, interval_sec: int = 300) -> None:
        while self._running:
            try:
                self.market_data.funding = self.fetch_binance_funding()
            except Exception:
                pass
            await asyncio.sleep(interval_sec)

    async def start(self) -> None:
        self._running = True
        try:
            candles = self.fetch_binance_klines()
            self.market_data.candles.extend(candles)
            self._recalculate_volume_mas()
        except Exception:
            pass

        await asyncio.gather(
            self._stream_edgex_ws(),
            self._poll_candles(),
            self._poll_funding(),
        )

    def stop(self) -> None:
        self._running = False


async def _maybe_await(cb, *args):
    result = cb(*args)
    if asyncio.iscoroutine(result):
        await result
