"""[단계 2] 실시간 시세, 거래량, 펀딩비 APR 수집 모듈."""

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd
import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EDGEX_WS_URL  = os.getenv("EDGEX_WS_URL",  "wss://testnet-ws.edgex.exchange")
EDGEX_API_URL = os.getenv("EDGEX_API_URL",  "https://testnet-api.edgex.exchange")
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

VOLUME_MA_MINUTES       = int(os.getenv("VOLUME_MA_MINUTES", "20"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3.0"))
FUNDING_PERIODS_PER_YEAR = 3 * 365   # 8시간마다 펀딩 → 연 1095회


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
    """실시간 시세·캔들·펀딩비 수집기.

    WebSocket 틱 스트림에서 분당 거래량을 누적하여 최대 VOLUME_MA_MINUTES개의
    완성된 분봉 거래량을 메모리 큐에 유지합니다. REST 폴(60초)과 틱 누적이
    병행되어, 분 경계 전에도 is_volume_spike()로 실시간 스파이크를 감지합니다.
    """

    def __init__(self, symbol: str = "BTC-USDC"):
        self.symbol = symbol
        self.binance_symbol = symbol.replace("-", "")
        self.market_data = MarketData(symbol=symbol)
        self._price_callbacks: list[Callable] = []
        self._candle_callbacks: list[Callable] = []
        self._running = False

        # ── 실시간 분당 거래량 큐 ─────────────────────────────────────────────
        # 완성된 1분봉 거래량을 순서대로 저장 (최대 VOLUME_MA_MINUTES개)
        self._minute_volume_queue: deque[float] = deque(maxlen=VOLUME_MA_MINUTES)
        # 현재 진행 중인 1분봉에 누적된 거래량
        self._current_minute_volume: float = 0.0
        # 현재 분봉의 시작 Unix 타임스탬프 (초, 60 단위로 내림)
        self._current_minute_ts: int = 0

    # ── REST 스냅샷 ───────────────────────────────────────────────────────────

    def get_current_market_data(self) -> Optional[dict]:
        """edgeX V2 REST API에서 현재 가격·24h 거래량 스냅샷을 가져옵니다.

        거래소 서버 장애나 포맷 변경에도 봇이 멈추지 않도록 3종의 예외를
        분리 처리합니다. 실패 시 None을 반환하여 호출부가 다음 틱에서 재시도합니다.

        Returns:
            {"symbol", "current_price", "volume_24h", "timestamp"} 또는 None
        """
        url = f"{EDGEX_API_URL}/v2/ticker"
        try:
            # timeout=5: 거래소 서버가 응답하지 않을 때 무한 대기 방지
            resp = requests.get(url, params={"symbol": self.symbol}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return {
                "symbol": self.symbol,
                # float() 변환: API는 숫자를 "95250.5" 형태의 문자열로 전송
                "current_price": float(data["lastPrice"]),
                "volume_24h":    float(data["volume24h"]),
                "timestamp":     int(time.time()),
            }
        except requests.exceptions.Timeout:
            logger.warning("[%s] edgeX API 응답 시간 초과 — 다음 틱에서 재시도", self.symbol)
        except requests.exceptions.RequestException as e:
            logger.warning("[%s] edgeX API 네트워크 오류: %s", self.symbol, e)
        except KeyError as e:
            logger.warning("[%s] edgeX API 응답 포맷 변경 (KeyError: %s)", self.symbol, e)
        return None

    # ── 실시간 분당 거래량 큐 ─────────────────────────────────────────────────

    def _accumulate_tick_volume(self, qty: float) -> None:
        """WebSocket 틱 1건의 체결 수량을 현재 분봉에 누적합니다.

        분 경계(xx:xx:00)를 넘으면 완성된 분봉 거래량을 큐에 추가하고
        새 분봉 카운터를 0부터 시작합니다.
        """
        now_min = (int(time.time()) // 60) * 60  # 현재 시각을 60초 단위로 내림

        # 첫 틱: 현재 분봉 타임스탬프 초기화
        if self._current_minute_ts == 0:
            self._current_minute_ts = now_min

        # 분 경계 초과 → 지난 분봉을 큐에 저장하고 리셋
        if now_min > self._current_minute_ts:
            self._minute_volume_queue.append(self._current_minute_volume)
            self._current_minute_volume = 0.0
            self._current_minute_ts = now_min
            logger.debug(
                "[%s] 분봉 완성 → 큐 길이=%d", self.symbol, len(self._minute_volume_queue)
            )

        self._current_minute_volume += qty

    def get_recent_average_volume(self) -> float:
        """최근 VOLUME_MA_MINUTES분 분당 평균 거래량을 반환합니다.

        WebSocket 틱 큐에 데이터가 쌓이기 전(봇 초기 기동 시)에는
        REST 폴로 계산한 volume_ma_20m을 폴백으로 사용합니다.
        """
        if self._minute_volume_queue:
            return sum(self._minute_volume_queue) / len(self._minute_volume_queue)
        # 폴백: REST 폴 기반 MA
        return self.market_data.volume_ma_20m

    def is_volume_spike(self, multiplier: float = VOLUME_SPIKE_MULTIPLIER) -> bool:
        """현재 분봉 거래량이 최근 20분 평균의 multiplier배를 초과하면 True.

        Args:
            multiplier: 스파이크 판정 배수 (기본 3.0, .env의 VOLUME_SPIKE_MULTIPLIER)

        Returns:
            True  → 거래량 급등 → 엔진 A 돌파 매매 신호
            False → 정상 범위 → 박스권 그리드 유지
        """
        avg = self.get_recent_average_volume()
        if avg <= 0:
            return False
        return self._current_minute_volume >= avg * multiplier

    # ── Callback registration ─────────────────────────────────────────────────

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
                        # 가격: edgeX WS는 "p" 또는 "price" 필드로 전달
                        price = float(msg.get("p", 0) or msg.get("price", 0))
                        if price:
                            self.market_data.last_price = price
                            for cb in self._price_callbacks:
                                await _maybe_await(cb, price)

                        # 체결 수량: "q" 또는 "quantity" 필드
                        qty_raw = msg.get("q", 0) or msg.get("quantity", 0)
                        if qty_raw:
                            self._accumulate_tick_volume(float(qty_raw))

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
