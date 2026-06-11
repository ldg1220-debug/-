"""[단계 6] 데이터 이탈(Drift) 감지기: 24h 변동성 vs 30일 기준, 텔레그램 알림."""

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

import pandas as pd
import requests
from dotenv import load_dotenv

from .data_fetcher import MarketData

load_dotenv()
logger = logging.getLogger(__name__)

ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
DRIFT_MULTIPLIER = float(os.getenv("DRIFT_MULTIPLIER", "2.0"))
BOX_EXPANSION_FACTOR = float(os.getenv("BOX_EXPANSION_FACTOR", "1.5"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


class DriftLevel(Enum):
    NORMAL = auto()
    WARNING = auto()
    CRITICAL = auto()


@dataclass
class DriftEvent:
    level: DriftLevel
    current_std: float
    baseline_std: float
    ratio: float
    message: str


def send_telegram(message: str) -> bool:
    """텔레그램 봇으로 알림 메시지를 전송합니다. 토큰/챗ID 미설정 시 스킵."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as exc:
        logger.warning("텔레그램 전송 실패: %s", exc)
        return False


class Monitor:
    def __init__(self, engine_a=None):
        self.engine_a = engine_a
        self._baseline_std: float | None = None
        self._drift_callbacks: list[Callable] = []
        self._running = False
        self._recalibrate_flag = False

    def on_drift(self, cb: Callable) -> None:
        self._drift_callbacks.append(cb)

    # --- Volatility calculations ---

    @staticmethod
    def calculate_24h_std(market: MarketData) -> float | None:
        """최근 24시간(1440개 1분봉)의 수익률 표준편차를 반환합니다."""
        candles = list(market.candles)
        window = min(1440, len(candles))
        if window < 30:
            return None
        df = pd.DataFrame([c.__dict__ for c in candles[-window:]])
        returns = df["close"].pct_change().dropna()
        return float(returns.std())

    @staticmethod
    def calculate_30d_baseline(market: MarketData) -> float | None:
        """전체 캔들(최대 43200 1분봉 = 30일)의 수익률 표준편차를 반환합니다."""
        candles = list(market.candles)
        if len(candles) < 60:
            return None
        df = pd.DataFrame([c.__dict__ for c in candles])
        returns = df["close"].pct_change().dropna()
        return float(returns.std())

    @staticmethod
    def calculate_atr(market: MarketData, period: int = ATR_PERIOD) -> float | None:
        candles = list(market.candles)
        if len(candles) < period + 1:
            return None
        df = pd.DataFrame([c.__dict__ for c in candles])
        df["prev_close"] = df["close"].shift(1)
        df["tr"] = (
            df[["high", "prev_close"]].max(axis=1) - df[["low", "prev_close"]].min(axis=1)
        )
        return float(df["tr"].rolling(window=period).mean().iloc[-1])

    def calibrate_baseline(self, market: MarketData) -> None:
        std = self.calculate_30d_baseline(market)
        if std:
            self._baseline_std = std
            logger.info("기준 30일 변동성(std) 설정: %.6f", std)

    # --- Dynamic box expansion ---

    def _expand_engine_a_box(self, ratio: float) -> None:
        if self.engine_a is None or self.engine_a.state.box is None:
            return
        box = self.engine_a.state.box
        mid = (box.support + box.resistance) / 2
        half_width = (box.width / 2) * ratio * BOX_EXPANSION_FACTOR
        from .engine_a import BoxRange
        self.engine_a.state.box = BoxRange(
            support=mid - half_width,
            resistance=mid + half_width,
        )
        logger.info(
            "박스권 동적 확대: support=%.2f resistance=%.2f",
            self.engine_a.state.box.support,
            self.engine_a.state.box.resistance,
        )

    # --- Drift check ---

    def check_drift(self, market: MarketData) -> DriftEvent | None:
        current_std = self.calculate_24h_std(market)
        if current_std is None:
            return None

        if self._baseline_std is None or self._recalibrate_flag:
            self.calibrate_baseline(market)
            self._recalibrate_flag = False
            return None

        ratio = current_std / self._baseline_std

        if ratio >= DRIFT_MULTIPLIER * 1.5:
            level = DriftLevel.CRITICAL
            msg = (
                f"🚨 *CRITICAL DRIFT*\n"
                f"24h std: `{current_std:.6f}`\n"
                f"30d baseline: `{self._baseline_std:.6f}`\n"
                f"배율: `{ratio:.2f}x` — 포지션 즉시 점검 권고"
            )
        elif ratio >= DRIFT_MULTIPLIER:
            level = DriftLevel.WARNING
            msg = (
                f"⚠️ *WARNING DRIFT*\n"
                f"24h std: `{current_std:.6f}`\n"
                f"30d baseline: `{self._baseline_std:.6f}`\n"
                f"배율: `{ratio:.2f}x` — 박스권 폭 확대 적용"
            )
        else:
            return None

        return DriftEvent(
            level=level,
            current_std=current_std,
            baseline_std=self._baseline_std,
            ratio=ratio,
            message=msg,
        )

    # --- Main tick ---

    async def on_market_update(self, market: MarketData) -> None:
        event = self.check_drift(market)
        if event is None:
            return

        logger.warning(event.message)

        # 박스권 동적 확대 (WARNING 이상)
        if event.level in (DriftLevel.WARNING, DriftLevel.CRITICAL):
            self._expand_engine_a_box(event.ratio)

        # 텔레그램 알림 (비동기 전송)
        await asyncio.get_event_loop().run_in_executor(
            None, send_telegram, event.message
        )

        # 등록된 외부 콜백 실행
        for cb in self._drift_callbacks:
            result = cb(event)
            if asyncio.iscoroutine(result):
                await result

    async def run_periodic_calibration(self, interval_sec: int = 86400) -> None:
        """24시간마다 기준 변동성을 재보정합니다."""
        self._running = True
        while self._running:
            await asyncio.sleep(interval_sec)
            self._recalibrate_flag = True
            logger.info("기준 변동성 재보정 예약 (다음 market update 시 적용)")

    def stop(self) -> None:
        self._running = False
