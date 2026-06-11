"""[단계 3] 엔진 A: 박스권 횡보 매매 + 거래량 돌파 전환 + 트레일링 스탑 / 손절."""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum, auto

import requests
from dotenv import load_dotenv

from .auth import EdgeXAuth
from .data_fetcher import MarketData

load_dotenv()
logger = logging.getLogger(__name__)

EDGEX_API_URL = os.getenv("EDGEX_API_URL", "https://testnet-api.edgex.exchange")
BOX_LOOKBACK = int(os.getenv("BOX_LOOKBACK", "50"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3.0"))
ENGINE_A_ALLOCATION = float(os.getenv("ENGINE_A_ALLOCATION", "0.25"))

TRAILING_STOP_PCT = 0.02   # 고점 대비 -2% → 익절
STOP_LOSS_PCT = 0.015      # 진입가 대비 -1.5% → 손절


class StrategyMode(Enum):
    BOX = auto()
    BREAKOUT = auto()


@dataclass
class BoxRange:
    support: float
    resistance: float

    @property
    def width(self) -> float:
        return self.resistance - self.support

    def is_inside(self, price: float) -> bool:
        return self.support < price < self.resistance


@dataclass
class BreakoutPosition:
    side: str           # "BUY" | "SELL"
    entry_price: float
    size: float
    peak_price: float   # trailing stop 기준 고점(롱) 또는 저점(숏)
    order_id: str = ""

    def should_stop_loss(self, current_price: float) -> bool:
        if self.side == "BUY":
            return current_price <= self.entry_price * (1 - STOP_LOSS_PCT)
        return current_price >= self.entry_price * (1 + STOP_LOSS_PCT)

    def update_peak(self, current_price: float) -> None:
        if self.side == "BUY":
            self.peak_price = max(self.peak_price, current_price)
        else:
            self.peak_price = min(self.peak_price, current_price)

    def should_trailing_stop(self, current_price: float) -> bool:
        if self.side == "BUY":
            return current_price <= self.peak_price * (1 - TRAILING_STOP_PCT)
        return current_price >= self.peak_price * (1 + TRAILING_STOP_PCT)


@dataclass
class EngineAState:
    mode: StrategyMode = StrategyMode.BOX
    box: BoxRange | None = None
    open_order_ids: list[str] = field(default_factory=list)
    allocated_capital: float = 0.0
    realized_pnl: float = 0.0
    position: BreakoutPosition | None = None


class EngineA:
    def __init__(self, auth: EdgeXAuth, symbol: str = "BTC-USDC", dry_run: bool = False):
        self.auth = auth
        self.symbol = symbol
        self.dry_run = dry_run
        self.state = EngineAState()
        self._session = requests.Session()
        self._session.headers.update(auth.get_headers())

    # --- Box calculation ---

    def calculate_box(self, market: MarketData) -> BoxRange:
        candles = list(market.candles)[-BOX_LOOKBACK:]
        if not candles:
            raise ValueError("캔들 데이터가 부족합니다")
        return BoxRange(
            support=min(c.low for c in candles),
            resistance=max(c.high for c in candles),
        )

    # --- Volume spike detection (20분 평균의 3배) ---

    def is_volume_spike(self, market: MarketData) -> bool:
        candles = list(market.candles)
        if len(candles) < 2 or market.volume_ma_20m == 0:
            return False
        return candles[-1].volume >= market.volume_ma_20m * VOLUME_SPIKE_MULTIPLIER

    # --- Order helpers ---

    def _place_order(self, side: str, order_type: str, price: float, size: float) -> str | None:
        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": self.symbol,
            "side": side,
            "price": str(round(price, 2)) if order_type == "LIMIT" else "0",
            "size": str(round(size, 6)),
            "type": order_type,
            "timeInForce": "GTC" if order_type == "LIMIT" else "IOC",
        }
        if self.dry_run:
            fake_id = f"DRY-{side}-{int(price)}-{int(size*1e4)}"
            logger.info("[DRY-RUN] 주문: %s %s %s @ %.2f (id=%s)", order_type, side, size, price, fake_id)
            return fake_id

        signed = self.auth.sign_order(payload)
        try:
            resp = self._session.post(
                f"{EDGEX_API_URL}/api/v1/order",
                json=signed,
                timeout=10,
            )
            resp.raise_for_status()
            order_id = resp.json().get("orderId")
            logger.info("주문: %s %s %.6f @ %.2f (id=%s)", order_type, side, size, price, order_id)
            return order_id
        except Exception as exc:
            logger.error("주문 실패: %s", exc)
            return None

    def _cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            logger.info("[DRY-RUN] 취소: %s", order_id)
            return True
        signed = self.auth.sign_cancel(order_id)
        try:
            resp = self._session.delete(
                f"{EDGEX_API_URL}/api/v1/order/{order_id}",
                json=signed,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("취소 실패 %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> None:
        for oid in list(self.state.open_order_ids):
            if self._cancel_order(oid):
                self.state.open_order_ids.remove(oid)
        logger.info("모든 주문 취소 완료")

    # --- Box range strategy ---

    def place_box_orders(self, market: MarketData) -> None:
        box = self.calculate_box(market)
        self.state.box = box
        price = market.last_price
        capital = self.state.allocated_capital
        if capital <= 0 or price <= 0:
            return

        order_size = (capital * 0.5) / price

        buy_id = self._place_order("BUY", "LIMIT", box.support * 1.001, order_size)
        sell_id = self._place_order("SELL", "LIMIT", box.resistance * 0.999, order_size)

        for oid in [buy_id, sell_id]:
            if oid:
                self.state.open_order_ids.append(oid)

        logger.info("박스권 주문: support=%.2f resistance=%.2f", box.support, box.resistance)

    # --- Breakout strategy ---

    def place_breakout_order(self, market: MarketData) -> None:
        price = market.last_price
        capital = self.state.allocated_capital
        if capital <= 0 or price <= 0:
            return

        candles = list(market.candles)
        if len(candles) < 2:
            return

        side = "BUY" if price > candles[-2].close else "SELL"
        size = capital / price

        oid = self._place_order(side, "MARKET", price, size)
        if oid:
            self.state.open_order_ids.append(oid)
            self.state.position = BreakoutPosition(
                side=side,
                entry_price=price,
                size=size,
                peak_price=price,
                order_id=oid,
            )
        logger.info("돌파 진입: %s @ %.2f", side, price)

    def _close_position(self, market: MarketData, reason: str) -> None:
        pos = self.state.position
        if pos is None:
            return
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        oid = self._place_order(close_side, "MARKET", market.last_price, pos.size)

        pnl = (
            (market.last_price - pos.entry_price) * pos.size
            if pos.side == "BUY"
            else (pos.entry_price - market.last_price) * pos.size
        )
        self.state.realized_pnl += pnl
        logger.info("포지션 청산 [%s]: pnl=%.4f USDC (누적=%.4f)", reason, pnl, self.state.realized_pnl)
        self.state.position = None

    # --- Risk management tick ---

    def _check_risk(self, market: MarketData) -> None:
        pos = self.state.position
        if pos is None:
            return
        price = market.last_price
        pos.update_peak(price)

        if pos.should_stop_loss(price):
            logger.warning("손절 실행: 진입가=%.2f 현재=%.2f", pos.entry_price, price)
            self._close_position(market, "STOP_LOSS")
        elif pos.should_trailing_stop(price):
            logger.info("트레일링 스탑: peak=%.2f 현재=%.2f", pos.peak_price, price)
            self._close_position(market, "TRAILING_STOP")

    # --- Main tick ---

    async def on_market_update(self, market: MarketData) -> None:
        self._check_risk(market)

        if self.is_volume_spike(market) and self.state.mode == StrategyMode.BOX:
            spike_vol = list(market.candles)[-1].volume if market.candles else 0
            logger.warning(
                "거래량 급증 (vol=%.2f, 20m_ma=%.2f) → 돌파 전략 전환",
                spike_vol,
                market.volume_ma_20m,
            )
            self.cancel_all_orders()
            self.state.mode = StrategyMode.BREAKOUT
            self.place_breakout_order(market)

        elif self.state.mode == StrategyMode.BOX and not self.state.open_order_ids:
            self.place_box_orders(market)

    def set_capital(self, total_capital: float) -> None:
        self.state.allocated_capital = total_capital * ENGINE_A_ALLOCATION

    def get_realized_pnl(self) -> float:
        return self.state.realized_pnl
