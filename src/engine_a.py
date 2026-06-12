"""엔진 A — RANGE ↔ BREAKOUT 하이브리드 전략 (매매 로직 개선판)

매매 개선 사항:
  - RANGE: 그리드 주문 (N레벨 × 양방향, 현재가 기준 균등 배치)
  - 손절 후 쿨다운 (연속 fakeout 방지)
  - ATR 기반 동적 스탑 (변동성 연동) + 데이터 부족 시 고정 % 폴백
  - 박스권 30분 캐싱
  - GridOrder 체결 추적
  - 조용한 가격 이탈 방어 (WATCHING 모드)
  - 리스크 기반 포지션 사이징

기존 5단계 구조 유지:
  단계 1: 48h 박스권 + 그리드 지정가 Payload
  단계 2: 거래량 Spike → cancel_all → BREAKOUT 전환
  단계 3: 시장가 진입 + worstAcceptablePrice 슬리피지 방어
  단계 4: ATR 트레일링 스탑 + ATR 손절 + 쿨다운 + RANGE 복귀
  단계 5: asyncio.Lock 동시성 보호 + mock_edgex_order 경계 분리
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto

import pandas as pd
import requests
from dotenv import load_dotenv

from .auth import EdgeXAuth
from .data_fetcher import MarketData

load_dotenv()
logger = logging.getLogger(__name__)

# ── 설정 상수 ────────────────────────────────────────────────────────────────
EDGEX_API_URL = os.getenv("EDGEX_API_URL", "https://testnet-api.edgex.exchange")
ENGINE_A_ALLOCATION = float(os.getenv("ENGINE_A_ALLOCATION", "0.25"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3.0"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.005"))

BOX_48H_CANDLES = 48 * 60          # 2880개 1분봉
BOX_CACHE_SECONDS = int(os.getenv("BOX_CACHE_SECONDS", "1800"))  # 30분 캐싱

# 그리드 설정
N_GRID_LEVELS = int(os.getenv("N_GRID_LEVELS", "3"))  # 양방향 각 N단계

# 쿨다운 (손절 후 재진입 방지)
STOP_LOSS_COOLDOWN_SECONDS = int(os.getenv("STOP_LOSS_COOLDOWN_SECONDS", "300"))  # 5분

# ATR 기반 동적 스탑
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULTIPLIER_TRAILING = float(os.getenv("ATR_MULTIPLIER_TRAILING", "1.5"))
ATR_MULTIPLIER_SL = float(os.getenv("ATR_MULTIPLIER_SL", "1.0"))

# 고정 % 폴백 (ATR 데이터 부족 시)
TRAILING_STOP_PCT = 0.02
STOP_LOSS_PCT = 0.015

# 리스크 기반 포지션 사이징
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.02"))


# ── 데이터 모델 ──────────────────────────────────────────────────────────────

class StrategyMode(Enum):
    RANGE = auto()
    BREAKOUT_LONG = auto()
    BREAKOUT_SHORT = auto()
    WATCHING = auto()


@dataclass
class GridOrder:
    order_id: str
    side: str           # "BUY" | "SELL"
    price: float
    size: float
    level: int          # 0-indexed
    grid_step: float    # TP 주문 간격
    is_tp: bool = False
    status: str = "OPEN"   # OPEN | FILLED | CANCELLED
    tp_order_id: str = ""


@dataclass
class BoxRange:
    support: float
    resistance: float

    @property
    def width(self) -> float:
        return self.resistance - self.support

    @property
    def mid(self) -> float:
        return (self.support + self.resistance) / 2

    def is_inside(self, price: float) -> bool:
        return self.support < price < self.resistance

    def breakout_direction(self, price: float) -> str | None:
        if price >= self.resistance:
            return "LONG"
        if price <= self.support:
            return "SHORT"
        return None


@dataclass
class BreakoutPosition:
    """돌파 진입 포지션 추적.

    trailing_stop_distance / stop_loss_distance: ATR 기반 절대 거리.
    0이면 고정 % 폴백을 사용합니다.
    """
    side: str
    entry_price: float
    size: float
    peak_price: float
    order_id: str = ""
    trailing_stop_distance: float = 0.0   # ATR × ATR_MULTIPLIER_TRAILING
    stop_loss_distance: float = 0.0        # ATR × ATR_MULTIPLIER_SL

    def update_peak(self, price: float) -> None:
        if self.side == "BUY":
            self.peak_price = max(self.peak_price, price)
        else:
            self.peak_price = min(self.peak_price, price)

    def should_stop_loss(self, price: float) -> bool:
        """손절 조건: ATR 기반 거리 우선, 폴백은 고정 %."""
        if self.stop_loss_distance > 0:
            if self.side == "BUY":
                return price <= self.entry_price - self.stop_loss_distance
            return price >= self.entry_price + self.stop_loss_distance
        # 폴백
        if self.side == "BUY":
            return price <= self.entry_price * (1 - STOP_LOSS_PCT)
        return price >= self.entry_price * (1 + STOP_LOSS_PCT)

    def should_trailing_stop(self, price: float) -> bool:
        """트레일링 스탑 조건: ATR 기반 거리 우선, 폴백은 고정 %."""
        if self.trailing_stop_distance > 0:
            if self.side == "BUY":
                return price <= self.peak_price - self.trailing_stop_distance
            return price >= self.peak_price + self.trailing_stop_distance
        # 폴백
        if self.side == "BUY":
            return price <= self.peak_price * (1 - TRAILING_STOP_PCT)
        return price >= self.peak_price * (1 + TRAILING_STOP_PCT)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size

    def effective_stop_loss_pct(self) -> float:
        """실제 적용 중인 손절 % (로깅용)."""
        if self.stop_loss_distance > 0 and self.entry_price > 0:
            return self.stop_loss_distance / self.entry_price * 100
        return STOP_LOSS_PCT * 100

    def effective_trailing_pct(self) -> float:
        """실제 적용 중인 트레일링 % (로깅용)."""
        if self.trailing_stop_distance > 0 and self.peak_price > 0:
            return self.trailing_stop_distance / self.peak_price * 100
        return TRAILING_STOP_PCT * 100


@dataclass
class EngineAState:
    mode: StrategyMode = StrategyMode.RANGE
    box: BoxRange | None = None
    open_order_ids: list = field(default_factory=list)
    allocated_capital: float = 0.0
    realized_pnl: float = 0.0
    position: BreakoutPosition | None = None
    cooldown_until: float = 0.0       # Unix timestamp; 0 = 쿨다운 없음
    box_last_updated: float = 0.0     # 박스권 캐싱 타임스탬프
    grid_orders: list = field(default_factory=list)  # list[GridOrder]


# ── 메인 엔진 ────────────────────────────────────────────────────────────────

class EngineA:
    def __init__(
        self,
        auth: EdgeXAuth,
        symbol: str = "BTC-USDC",
        dry_run: bool = False,
    ):
        self.auth = auth
        self.symbol = symbol
        self.dry_run = dry_run
        self.state = EngineAState()
        self._state_lock = asyncio.Lock()
        self._session = requests.Session()
        self._session.headers.update(auth.get_headers())

    # ── ATR 계산 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_atr(market: MarketData) -> float | None:
        """최근 ATR_PERIOD 캔들로 ATR을 계산합니다.

        Returns:
            ATR 값(float), 데이터 부족이면 None
        """
        candles = list(market.candles)
        if len(candles) < ATR_PERIOD + 1:
            return None
        df = pd.DataFrame([c.__dict__ for c in candles[-(ATR_PERIOD * 3):]])
        df["prev_close"] = df["close"].shift(1)
        df["tr"] = (
            df[["high", "prev_close"]].max(axis=1)
            - df[["low", "prev_close"]].min(axis=1)
        )
        atr = float(df["tr"].rolling(window=ATR_PERIOD).mean().iloc[-1])
        return atr if atr > 0 else None

    # ── 단계 1: 48시간 박스권 + 그리드 주문 ──────────────────────────────────

    def calculate_box_48h(self, market: MarketData) -> BoxRange:
        """48시간 최고가/최저가로 저항선/지지선을 계산합니다. 30분 캐싱 적용."""
        # 캐시 유효 여부 확인
        if (
            self.state.box is not None
            and time.time() - self.state.box_last_updated < BOX_CACHE_SECONDS
        ):
            logger.debug("[박스권] 캐시 반환 (%.0f초 전 계산)", time.time() - self.state.box_last_updated)
            return self.state.box

        # 재계산
        candles = list(market.candles)[-BOX_48H_CANDLES:]
        if len(candles) < 2:
            raise ValueError(f"박스권 계산에 필요한 캔들 부족: {len(candles)}개 (최소 2)")
        box = BoxRange(
            support=min(c.low for c in candles),
            resistance=max(c.high for c in candles),
        )
        self.state.box = box
        self.state.box_last_updated = time.time()
        logger.debug(
            "[박스권] support=%.2f  resistance=%.2f  width=%.4f  (캔들 %d개)",
            box.support, box.resistance, box.width, len(candles),
        )
        return box

    def invalidate_box_cache(self) -> None:
        """박스권 캐시를 무효화합니다. 다음 calculate_box_48h 호출 시 재계산."""
        self.state.box_last_updated = 0.0
        logger.debug("[박스권] 캐시 무효화")

    def _build_grid_levels(
        self, box: BoxRange, capital: float, price: float
    ) -> list:
        """현재가 기준으로 균등한 그리드 주문 레벨 목록을 생성합니다.

        BUY: 현재가 아래 → 지지선 방향으로 N단계
        SELL: 현재가 위  → 저항선 방향으로 N단계

        Returns:
            list[GridOrder]
        """
        levels: list = []
        per_level_capital = capital / (N_GRID_LEVELS * 2)

        # BUY 레벨 (현재가 ~ 지지선 사이 균등 분할)
        buy_range = price - box.support
        buy_step = buy_range / (N_GRID_LEVELS + 1) if buy_range > 0 else 0
        if buy_range > 0:
            for i in range(1, N_GRID_LEVELS + 1):
                lp = round(price - buy_step * i, 2)
                if lp > box.support:
                    size = per_level_capital / lp
                    levels.append(GridOrder(
                        order_id="",
                        side="BUY",
                        price=lp,
                        size=size,
                        level=i - 1,
                        grid_step=buy_step,
                    ))

        # SELL 레벨 (현재가 ~ 저항선 사이 균등 분할)
        sell_range = box.resistance - price
        sell_step = sell_range / (N_GRID_LEVELS + 1) if sell_range > 0 else 0
        if sell_range > 0:
            for i in range(1, N_GRID_LEVELS + 1):
                lp = round(price + sell_step * i, 2)
                if lp < box.resistance:
                    size = per_level_capital / lp
                    levels.append(GridOrder(
                        order_id="",
                        side="SELL",
                        price=lp,
                        size=size,
                        level=i - 1,
                        grid_step=sell_step,
                    ))

        return levels

    def _build_limit_payload(self, side: str, price: float, size: float) -> dict:
        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": self.symbol,
            "side": side,
            "price": str(round(price, 2)),
            "size": str(round(size, 6)),
            "type": "LIMIT",
            "timeInForce": "GTC",
        }
        logger.debug(
            "[Payload][LIMIT] side=%s price=%s size=%s",
            payload["side"], payload["price"], payload["size"],
        )
        return payload

    def place_range_orders(self, market: MarketData) -> None:
        """그리드 지정가 주문을 배치합니다.

        현재가 기준으로 BUY N단계 + SELL N단계 균등 배치.
        쿨다운 중이거나 자본/가격 조건 미충족 시 배치하지 않습니다.
        """
        if self._is_in_cooldown():
            remaining = self.state.cooldown_until - time.time()
            logger.debug("쿨다운 중 — 주문 배치 보류 (남은 시간: %.0f초)", remaining)
            return

        box = self.calculate_box_48h(market)
        self.state.box = box

        price = market.last_price
        capital = self.state.allocated_capital
        if capital <= 0 or price <= 0:
            return

        if not box.is_inside(price):
            logger.warning(
                "현재가(%.2f)가 박스권 외부 — 그리드 배치 스킵 (support=%.2f, resistance=%.2f)",
                price, box.support, box.resistance,
            )
            return

        grid = self._build_grid_levels(box, capital, price)
        placed = 0
        for order in grid:
            payload = self._build_limit_payload(order.side, order.price, order.size)
            oid = self._send_order(payload)
            if oid:
                order.order_id = oid
                self.state.grid_orders.append(order)
                self.state.open_order_ids.append(oid)
                placed += 1

        logger.info(
            "✅ [그리드 RANGE] %d개 주문 배치 | support=%.2f resistance=%.2f | ATR=%s",
            placed,
            box.support,
            box.resistance,
            f"{self._calculate_atr(market):.4f}" if self._calculate_atr(market) else "N/A",
        )

    # ── 그리드 체결 추적 ──────────────────────────────────────────────────────

    def _check_grid_fills(self, market: MarketData) -> None:
        """그리드 주문 체결 여부를 확인합니다."""
        if self.dry_run:
            self._simulate_fills(market)
        else:
            for order in list(self.state.grid_orders):
                if order.status == "OPEN" and not order.is_tp:
                    status = self._query_order_status(order.order_id)
                    if status == "FILLED":
                        order.status = "FILLED"
                        if order.order_id in self.state.open_order_ids:
                            self.state.open_order_ids.remove(order.order_id)
                        self._place_grid_tp_order(order)

    def _simulate_fills(self, market: MarketData) -> None:
        """드라이런 시 가격 기반 체결 시뮬레이션."""
        price = market.last_price
        for order in list(self.state.grid_orders):
            if order.status != "OPEN":
                continue
            filled = False
            if order.side == "BUY" and price <= order.price:
                filled = True
            elif order.side == "SELL" and price >= order.price:
                filled = True

            if filled:
                order.status = "FILLED"
                if order.order_id in self.state.open_order_ids:
                    self.state.open_order_ids.remove(order.order_id)
                logger.info(
                    "[시뮬] %s 체결: %s @ %.2f (현재가=%.2f)",
                    order.order_id, order.side, order.price, price,
                )
                self._place_grid_tp_order(order)

    def _place_grid_tp_order(self, filled_order: GridOrder) -> None:
        """체결된 그리드 주문에 대한 TP 주문을 배치합니다."""
        if filled_order.side == "BUY":
            tp_side = "SELL"
            tp_price = round(filled_order.price + filled_order.grid_step, 2)
        else:
            tp_side = "BUY"
            tp_price = round(filled_order.price - filled_order.grid_step, 2)

        payload = self._build_limit_payload(tp_side, tp_price, filled_order.size)
        tp_oid = self._send_order(payload)
        if tp_oid:
            tp_order = GridOrder(
                order_id=tp_oid,
                side=tp_side,
                price=tp_price,
                size=filled_order.size,
                level=filled_order.level,
                grid_step=filled_order.grid_step,
                is_tp=True,
            )
            self.state.grid_orders.append(tp_order)
            self.state.open_order_ids.append(tp_oid)
            filled_order.tp_order_id = tp_oid
            logger.info(
                "[TP] %s TP 주문 배치: %s @ %.2f (원 주문: %s)",
                filled_order.order_id, tp_side, tp_price, tp_oid,
            )

    def _query_order_status(self, order_id: str) -> str | None:
        """edgeX API에서 주문 상태를 조회합니다.
        TODO: 실제 API 엔드포인트로 교체 필요.
        """
        try:
            resp = self._session.get(
                f"{EDGEX_API_URL}/api/v1/order/{order_id}", timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("status")
        except Exception as exc:
            logger.error("주문 상태 조회 실패 %s: %s", order_id, exc)
            return None

    # ── 단계 2: 거래량 Spike 감시 + 비상 전량 취소 + 상태 전환 ──────────────

    def is_volume_spike(self, market: MarketData) -> bool:
        candles = list(market.candles)
        if len(candles) < 2 or market.volume_ma_20m == 0:
            return False
        current_vol = candles[-1].volume
        threshold = market.volume_ma_20m * VOLUME_SPIKE_MULTIPLIER
        if current_vol >= threshold:
            logger.warning(
                "🔥 거래량 Spike: %.2f ≥ MA(%.2f) × %.1f = %.2f",
                current_vol, market.volume_ma_20m, VOLUME_SPIKE_MULTIPLIER, threshold,
            )
            return True
        return False

    def _determine_breakout_direction(self, market: MarketData) -> str:
        price = market.last_price
        box = self.state.box
        if box:
            direction = box.breakout_direction(price)
            if direction:
                return direction
        candles = list(market.candles)
        if len(candles) >= 2 and candles[-1].close > candles[-2].close:
            return "LONG"
        return "SHORT"

    def cancel_all_orders(self) -> int:
        cancelled = 0
        for oid in list(self.state.open_order_ids):
            if self._cancel_order(oid):
                self.state.open_order_ids.remove(oid)
                cancelled += 1

        # grid_orders 상태도 CANCELLED로 업데이트
        for order in self.state.grid_orders:
            if order.status == "OPEN":
                order.status = "CANCELLED"

        logger.info("🗑️  전량 취소 완료: %d건", cancelled)
        return cancelled

    def _handle_silent_breakout(self, market: MarketData) -> None:
        """거래량 없는 조용한 가격 이탈 처리."""
        if self.state.open_order_ids:
            self.cancel_all_orders()
        self.invalidate_box_cache()
        self.state.mode = StrategyMode.WATCHING
        logger.warning(
            "🔕 조용한 가격 이탈 감지 → 그리드 취소 + WATCHING 모드 전환 (price=%.2f)",
            market.last_price,
        )

    def _execute_emergency_switch(self, direction: str, market: MarketData) -> None:
        self.cancel_all_orders()
        self.state.grid_orders.clear()
        self.state.open_order_ids.clear()
        self.state.mode = (
            StrategyMode.BREAKOUT_LONG if direction == "LONG"
            else StrategyMode.BREAKOUT_SHORT
        )
        logger.warning("⚡ 상태 전환: RANGE → %s", self.state.mode.name)
        self._place_breakout_market_order(market)

    # ── 단계 3: 돌파 시장가 진입 + 슬리피지 방어 + ATR 스탑 계산 ───────────

    def _build_market_payload(
        self, side: str, size: float, ref_price: float
    ) -> dict:
        worst = (
            ref_price * (1 + MAX_SLIPPAGE_PCT)
            if side == "BUY"
            else ref_price * (1 - MAX_SLIPPAGE_PCT)
        )
        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": self.symbol,
            "side": side,
            "price": "0",
            "size": str(round(size, 6)),
            "type": "MARKET",
            "timeInForce": "IOC",
            "slippageTolerance": str(MAX_SLIPPAGE_PCT),
            "worstAcceptablePrice": str(round(worst, 2)),
        }
        logger.info(
            "[Payload][MARKET] side=%s size=%s worstPrice=%s slippage=%.2f%%",
            side, payload["size"], payload["worstAcceptablePrice"],
            MAX_SLIPPAGE_PCT * 100,
        )
        return payload

    def _check_slippage(self, ref_price: float, current_price: float) -> bool:
        if ref_price <= 0:
            return True
        deviation = abs(current_price - ref_price) / ref_price
        if deviation > MAX_SLIPPAGE_PCT:
            logger.warning(
                "⚠️  슬리피지 초과: ref=%.2f current=%.2f deviation=%.3f%%",
                ref_price, current_price, deviation * 100,
            )
            return False
        return True

    def _place_breakout_market_order(self, market: MarketData) -> None:
        """ATR 기반 스탑 거리를 계산하고 시장가 돌파 진입 주문을 실행합니다."""
        price = market.last_price
        capital = self.state.allocated_capital
        if capital <= 0 or price <= 0:
            return

        side = "BUY" if self.state.mode == StrategyMode.BREAKOUT_LONG else "SELL"

        # ── ATR 기반 동적 스탑 계산 + 리스크 기반 사이징 ────────────────────
        atr = self._calculate_atr(market)
        max_risk_usdc = capital * MAX_RISK_PER_TRADE_PCT

        if atr:
            sl_dist = round(atr * ATR_MULTIPLIER_SL, 4)
            trailing_dist = round(atr * ATR_MULTIPLIER_TRAILING, 4)
            size = max_risk_usdc / sl_dist
            size = min(size, capital / price)
            logger.info(
                "📐 ATR=%.4f → 트레일링=%.4f(%.2f%%)  손절=%.4f(%.2f%%)  size=%.6f",
                atr,
                trailing_dist, trailing_dist / price * 100,
                sl_dist, sl_dist / price * 100,
                size,
            )
        else:
            # 데이터 부족 → 고정 % 폴백
            trailing_dist = 0.0
            sl_dist = 0.0
            sl_dist_fallback = price * STOP_LOSS_PCT
            size = max_risk_usdc / sl_dist_fallback
            size = min(size, capital / price)
            logger.warning(
                "ATR 계산 불가 (캔들 %d개) → 고정 %% 스탑 사용 (trailing=%.1f%% sl=%.1f%%) size=%.6f",
                len(list(market.candles)),
                TRAILING_STOP_PCT * 100,
                STOP_LOSS_PCT * 100,
                size,
            )

        payload = self._build_market_payload(side, size, price)
        order_id = self._send_order(payload)

        if order_id:
            self.state.position = BreakoutPosition(
                side=side,
                entry_price=price,
                size=size,
                peak_price=price,
                order_id=order_id,
                trailing_stop_distance=trailing_dist,
                stop_loss_distance=sl_dist,
            )
            logger.info(
                "🚀 [%s] 진입: %s %.6f @ %.2f | trailing=%.4f  sl=%.4f",
                self.state.mode.name, side, size, price,
                trailing_dist, sl_dist,
            )

    # ── 단계 4: 트레일링 스탑 / 손절 + 쿨다운 + RANGE 복귀 ─────────────────

    def _is_in_cooldown(self) -> bool:
        return time.time() < self.state.cooldown_until

    def _start_cooldown(self) -> None:
        self.state.cooldown_until = time.time() + STOP_LOSS_COOLDOWN_SECONDS
        logger.info(
            "⏳ 쿨다운 시작: %d초 (%.1f분) — 다음 RANGE 진입 차단",
            STOP_LOSS_COOLDOWN_SECONDS,
            STOP_LOSS_COOLDOWN_SECONDS / 60,
        )

    def _check_exit_conditions(self, market: MarketData) -> None:
        pos = self.state.position
        if pos is None:
            return

        price = market.last_price
        pos.update_peak(price)

        if pos.should_stop_loss(price):
            logger.warning(
                "🛑 [손절] entry=%.2f current=%.2f  효과 스탑=%.2f%%",
                pos.entry_price, price, pos.effective_stop_loss_pct(),
            )
            self._close_position(market, "STOP_LOSS")

        elif pos.should_trailing_stop(price):
            logger.info(
                "💰 [트레일링 스탑] peak=%.2f current=%.2f  효과 스탑=%.2f%%",
                pos.peak_price, price, pos.effective_trailing_pct(),
            )
            self._close_position(market, "TRAILING_STOP")

    def _close_position(self, market: MarketData, reason: str) -> None:
        """포지션을 시장가로 전량 청산합니다.

        STOP_LOSS: 쿨다운 시작 (N분간 RANGE 재진입 차단)
        TRAILING_STOP: 즉시 RANGE 복귀
        """
        pos = self.state.position
        if pos is None:
            return

        close_side = "SELL" if pos.side == "BUY" else "BUY"
        payload = self._build_market_payload(close_side, pos.size, market.last_price)
        self._send_order(payload)

        pnl = pos.unrealized_pnl(market.last_price)
        self.state.realized_pnl += pnl

        logger.info(
            "✅ [%s] 청산 | PnL=%.4f USDC | 누적=%.4f USDC",
            reason, pnl, self.state.realized_pnl,
        )

        # 쿨다운: 손절 시만 적용
        if reason == "STOP_LOSS":
            self._start_cooldown()

        self.state.position = None
        self.state.mode = StrategyMode.RANGE
        self.state.grid_orders.clear()
        self.state.open_order_ids.clear()
        logger.info("🔄 RANGE 복귀%s", " (쿨다운 중)" if self._is_in_cooldown() else "")

    # ── 단계 5: 메인 비동기 Tick ──────────────────────────────────────────────

    async def on_market_update(self, market: MarketData) -> None:
        async with self._state_lock:
            self._check_exit_conditions(market)

            if self.state.mode in (StrategyMode.RANGE, StrategyMode.WATCHING):
                self._check_grid_fills(market)

            mode = self.state.mode
            spike = self.is_volume_spike(market) if mode == StrategyMode.RANGE else False

            # 조용한 이탈 방어
            if mode == StrategyMode.RANGE and self.state.box:
                if not self.state.box.is_inside(market.last_price) and not spike:
                    self._handle_silent_breakout(market)
                    return

            # WATCHING 모드: 박스 복귀 시 RANGE 재개
            if mode == StrategyMode.WATCHING:
                if self.state.box and self.state.box.is_inside(market.last_price):
                    logger.info("📦 가격 박스권 복귀 → RANGE 재개")
                    self.state.mode = StrategyMode.RANGE
                    mode = StrategyMode.RANGE
                else:
                    return

            if mode == StrategyMode.RANGE and spike:
                direction = self._determine_breakout_direction(market)
                self._execute_emergency_switch(direction, market)
            elif mode == StrategyMode.RANGE and not self.state.open_order_ids:
                self.place_range_orders(market)

    # ── edgeX API 경계 (mock / 실제 교체 포인트) ─────────────────────────────

    def mock_edgex_order(self, payload: dict) -> str:
        """[Mock] edgeX API 규격 확정 전 사용.
        TODO: 실제 POST /api/v1/order 로직으로 교체하세요.
        """
        fake_id = f"MOCK-{payload['side']}-{payload.get('price','0')}-{payload['size']}"
        logger.debug("[MOCK] 주문: %s → id=%s", payload, fake_id)
        return fake_id

    def mock_edgex_cancel(self, order_id: str) -> bool:
        """[Mock] edgeX API 규격 확정 전 사용.
        TODO: 실제 DELETE /api/v1/order/{id} 로직으로 교체하세요.
        """
        logger.debug("[MOCK] 취소: %s", order_id)
        return True

    def _send_order(self, payload: dict) -> str | None:
        if self.dry_run:
            return self.mock_edgex_order(payload)
        signed = self.auth.sign_order(payload)
        try:
            resp = self._session.post(
                f"{EDGEX_API_URL}/api/v1/order", json=signed, timeout=10
            )
            resp.raise_for_status()
            return resp.json().get("orderId")
        except Exception as exc:
            logger.error("주문 전송 실패: %s | payload=%s", exc, payload)
            return None

    def _cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            return self.mock_edgex_cancel(order_id)
        signed = self.auth.sign_cancel(order_id)
        try:
            resp = self._session.delete(
                f"{EDGEX_API_URL}/api/v1/order/{order_id}", json=signed, timeout=10
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("취소 실패 %s: %s", order_id, exc)
            return False

    # ── 유틸리티 ─────────────────────────────────────────────────────────────

    def set_capital(self, total_capital: float) -> None:
        self.state.allocated_capital = total_capital * ENGINE_A_ALLOCATION

    def get_realized_pnl(self) -> float:
        return self.state.realized_pnl

    def get_status(self) -> dict:
        pos = self.state.position
        return {
            "mode": self.state.mode.name,
            "cooldown_remaining": max(0.0, self.state.cooldown_until - time.time()),
            "grid_levels": N_GRID_LEVELS,
            "box": {"support": self.state.box.support, "resistance": self.state.box.resistance}
            if self.state.box else None,
            "open_orders": len(self.state.open_order_ids),
            "position": {
                "side": pos.side,
                "entry": pos.entry_price,
                "peak": pos.peak_price,
                "size": pos.size,
                "trailing_dist": pos.trailing_stop_distance,
                "sl_dist": pos.stop_loss_distance,
            } if pos else None,
            "realized_pnl": self.state.realized_pnl,
        }
