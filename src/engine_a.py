"""엔진 A — RANGE ↔ BREAKOUT 하이브리드 전략 (5단계 완전판)

단계 1: 48시간 박스권 계산 + 지정가 주문 Payload 생성/로그
단계 2: 거래량 Spike 감시 → cancel_all → BREAKOUT_LONG/SHORT 전환
단계 3: 시장가 돌파 진입 + 슬리피지 방어
단계 4: 트레일링 스탑(-2%) + 손절(-1.5%) + RANGE 복귀
단계 5: asyncio.Lock 기반 상태 동시성 보호
"""

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

# ── 설정 상수 ────────────────────────────────────────────────────────────────
EDGEX_API_URL = os.getenv("EDGEX_API_URL", "https://testnet-api.edgex.exchange")
ENGINE_A_ALLOCATION = float(os.getenv("ENGINE_A_ALLOCATION", "0.25"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3.0"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.005"))  # 0.5% 허용

BOX_48H_CANDLES = 48 * 60          # 48시간 × 1분봉 = 2880캔들
TRAILING_STOP_PCT = 0.02            # 고점 대비 -2% → 익절
STOP_LOSS_PCT = 0.015               # 진입가 대비 -1.5% → 손절


# ── 데이터 모델 ──────────────────────────────────────────────────────────────

class StrategyMode(Enum):
    RANGE = auto()
    BREAKOUT_LONG = auto()
    BREAKOUT_SHORT = auto()


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
        """박스권 돌파 방향을 반환합니다. 내부면 None."""
        if price >= self.resistance:
            return "LONG"
        if price <= self.support:
            return "SHORT"
        return None


@dataclass
class BreakoutPosition:
    """단계 3/4: 돌파 진입 포지션 추적 데이터."""
    side: str           # "BUY" | "SELL"
    entry_price: float
    size: float
    peak_price: float   # 롱: 갱신 고점 / 숏: 갱신 저점
    order_id: str = ""

    def update_peak(self, price: float) -> None:
        """단계 4: 실시간 고점(또는 저점)을 계속 갱신합니다."""
        if self.side == "BUY":
            self.peak_price = max(self.peak_price, price)
        else:
            self.peak_price = min(self.peak_price, price)

    def should_stop_loss(self, price: float) -> bool:
        """단계 4: 진입가 대비 -1.5% 이상 손실이면 True."""
        if self.side == "BUY":
            return price <= self.entry_price * (1 - STOP_LOSS_PCT)
        return price >= self.entry_price * (1 + STOP_LOSS_PCT)

    def should_trailing_stop(self, price: float) -> bool:
        """단계 4: 고점(저점) 대비 -2% 이상 밀리면 True."""
        if self.side == "BUY":
            return price <= self.peak_price * (1 - TRAILING_STOP_PCT)
        return price >= self.peak_price * (1 + TRAILING_STOP_PCT)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size


@dataclass
class EngineAState:
    mode: StrategyMode = StrategyMode.RANGE
    box: BoxRange | None = None
    open_order_ids: list[str] = field(default_factory=list)
    allocated_capital: float = 0.0
    realized_pnl: float = 0.0
    position: BreakoutPosition | None = None


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
        # 단계 5: 상태 전환 시 경쟁 조건 방지용 비동기 락
        self._state_lock = asyncio.Lock()
        self._session = requests.Session()
        self._session.headers.update(auth.get_headers())

    # ── 단계 1: 48시간 박스권 계산 및 지정가 주문 Payload ────────────────────

    def calculate_box_48h(self, market: MarketData) -> BoxRange:
        """48시간 최고가/최저가로 저항선(resistance)/지지선(support)을 계산합니다.

        data_fetcher.py로부터 최대 2880개(48h)의 1분봉을 사용하며,
        데이터가 부족하면 가용한 전체 캔들을 사용합니다.
        """
        candles = list(market.candles)[-BOX_48H_CANDLES:]
        if len(candles) < 2:
            raise ValueError(f"박스권 계산에 필요한 캔들 부족: {len(candles)}개 (최소 2)")
        box = BoxRange(
            support=min(c.low for c in candles),
            resistance=max(c.high for c in candles),
        )
        logger.debug(
            "[박스권] support=%.2f  resistance=%.2f  width=%.2f  (캔들 %d개 / 48h)",
            box.support,
            box.resistance,
            box.width,
            len(candles),
        )
        return box

    def _build_limit_payload(self, side: str, price: float, size: float) -> dict:
        """단계 1: 지정가 주문 Payload를 생성합니다 (API 전송 전 로그 검증용)."""
        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": self.symbol,
            "side": side,
            "price": str(round(price, 2)),
            "size": str(round(size, 6)),
            "type": "LIMIT",
            "timeInForce": "GTC",
        }
        logger.info(
            "[Payload][LIMIT] side=%s price=%s size=%s",
            payload["side"],
            payload["price"],
            payload["size"],
        )
        return payload

    def place_range_orders(self, market: MarketData) -> None:
        """단계 1: 지지선 근처 롱 + 저항선 근처 숏 지정가 주문을 배치합니다."""
        box = self.calculate_box_48h(market)
        self.state.box = box

        price = market.last_price
        capital = self.state.allocated_capital
        if capital <= 0 or price <= 0:
            logger.warning("자본 또는 가격 0 — 박스권 주문 스킵")
            return

        order_size = (capital * 0.5) / price  # 자본의 50%씩 양방향

        # 하단 0.1% 안쪽 롱 / 상단 0.1% 안쪽 숏
        buy_price = box.support * 1.001
        sell_price = box.resistance * 0.999

        buy_payload = self._build_limit_payload("BUY", buy_price, order_size)
        sell_payload = self._build_limit_payload("SELL", sell_price, order_size)

        buy_id = self._send_order(buy_payload)
        sell_id = self._send_order(sell_payload)

        for oid in [buy_id, sell_id]:
            if oid:
                self.state.open_order_ids.append(oid)

        logger.info(
            "✅ [RANGE] 주문 배치 완료 | BUY@%.2f SELL@%.2f | size=%.6f",
            buy_price,
            sell_price,
            order_size,
        )

    # ── 단계 2: 거래량 Spike 감시 + 비상 전량 취소 + 상태 전환 ──────────────

    def is_volume_spike(self, market: MarketData) -> bool:
        """현재 거래량이 20분 평균의 VOLUME_SPIKE_MULTIPLIER배 이상이면 True."""
        candles = list(market.candles)
        if len(candles) < 2 or market.volume_ma_20m == 0:
            return False
        current_vol = candles[-1].volume
        threshold = market.volume_ma_20m * VOLUME_SPIKE_MULTIPLIER
        if current_vol >= threshold:
            logger.warning(
                "🔥 거래량 Spike: %.2f ≥ MA(%.2f) × %.1f = %.2f",
                current_vol,
                market.volume_ma_20m,
                VOLUME_SPIKE_MULTIPLIER,
                threshold,
            )
            return True
        return False

    def _determine_breakout_direction(self, market: MarketData) -> str:
        """단계 2: 돌파 방향을 결정합니다 — 'LONG' | 'SHORT'.

        우선순위:
          1. 현재가가 저항선 돌파 → LONG
          2. 현재가가 지지선 이탈 → SHORT
          3. 박스권 내부 Spike → 직전 종가 대비 모멘텀
        """
        price = market.last_price
        box = self.state.box

        if box:
            direction = box.breakout_direction(price)
            if direction:
                logger.info("박스권 돌파 방향: %s (price=%.2f)", direction, price)
                return direction

        # 박스권 내부 spike → 모멘텀 판단
        candles = list(market.candles)
        if len(candles) >= 2 and candles[-1].close > candles[-2].close:
            return "LONG"
        return "SHORT"

    def cancel_all_orders(self) -> int:
        """단계 2: 모든 미체결 지정가 주문을 전량 취소합니다.

        Returns:
            취소 성공한 주문 수
        """
        cancelled = 0
        for oid in list(self.state.open_order_ids):
            if self._cancel_order(oid):
                self.state.open_order_ids.remove(oid)
                cancelled += 1
        logger.info("🗑️  전량 취소 완료: %d건", cancelled)
        return cancelled

    def _execute_emergency_switch(self, direction: str, market: MarketData) -> None:
        """단계 2/3: 비상 함수 — 취소 → 상태 전환 → 돌파 진입을 원자적으로 수행합니다."""
        # 2a: 전량 취소
        self.cancel_all_orders()

        # 2b: 상태 즉시 전환
        if direction == "LONG":
            self.state.mode = StrategyMode.BREAKOUT_LONG
        else:
            self.state.mode = StrategyMode.BREAKOUT_SHORT
        logger.warning(
            "⚡ 상태 전환 완료: RANGE → %s", self.state.mode.name
        )

        # 3: 돌파 진입
        self._place_breakout_market_order(market)

    # ── 단계 3: 돌파 시장가 진입 + 슬리피지 방어 ────────────────────────────

    def _build_market_payload(
        self, side: str, size: float, ref_price: float
    ) -> dict:
        """단계 3: 시장가 주문 Payload — worstAcceptablePrice로 슬리피지를 제한합니다."""
        if side == "BUY":
            worst_price = ref_price * (1 + MAX_SLIPPAGE_PCT)
        else:
            worst_price = ref_price * (1 - MAX_SLIPPAGE_PCT)

        payload = {
            "accountId": os.getenv("EDGEX_ACCOUNT_ID", ""),
            "symbol": self.symbol,
            "side": side,
            "price": "0",
            "size": str(round(size, 6)),
            "type": "MARKET",
            "timeInForce": "IOC",
            "slippageTolerance": str(MAX_SLIPPAGE_PCT),
            "worstAcceptablePrice": str(round(worst_price, 2)),
        }
        logger.info(
            "[Payload][MARKET] side=%s size=%s worstPrice=%s slippage=%.2f%%",
            side,
            payload["size"],
            payload["worstAcceptablePrice"],
            MAX_SLIPPAGE_PCT * 100,
        )
        return payload

    def _check_slippage(self, ref_price: float, current_price: float) -> bool:
        """단계 3: 현재가가 참조가 대비 허용 슬리피지 이내인지 사전 검증합니다."""
        if ref_price <= 0:
            return True
        deviation = abs(current_price - ref_price) / ref_price
        if deviation > MAX_SLIPPAGE_PCT:
            logger.warning(
                "⚠️  슬리피지 초과 → 주문 보류: ref=%.2f current=%.2f deviation=%.3f%%",
                ref_price,
                current_price,
                deviation * 100,
            )
            return False
        return True

    def _place_breakout_market_order(self, market: MarketData) -> None:
        """단계 3: 시장가 돌파 진입 주문을 실행합니다.

        슬리피지 보호는 payload의 worstAcceptablePrice 필드를 통해
        거래소(edgeX) 수준에서 처리합니다. 박스 경계 기준 사전 체크는
        돌파 진입에 부적합하므로 사용하지 않습니다.
        _check_slippage()는 재진입 시나리오 등 별도 맥락에서 활용하세요.
        """
        price = market.last_price
        capital = self.state.allocated_capital
        if capital <= 0 or price <= 0:
            logger.error("돌파 진입 불가: capital=%.2f price=%.2f", capital, price)
            return

        side = (
            "BUY"
            if self.state.mode == StrategyMode.BREAKOUT_LONG
            else "SELL"
        )
        size = capital / price

        # worstAcceptablePrice가 거래소 수준 슬리피지 방어선 역할
        payload = self._build_market_payload(side, size, price)
        order_id = self._send_order(payload)

        if order_id:
            self.state.position = BreakoutPosition(
                side=side,
                entry_price=price,
                size=size,
                peak_price=price,
                order_id=order_id,
            )
            logger.info(
                "🚀 [%s] 돌파 진입: %s %.6f @ %.2f (id=%s)",
                self.state.mode.name,
                side,
                size,
                price,
                order_id,
            )

    # ── 단계 4: 트레일링 스탑 / 손절 / RANGE 복귀 ───────────────────────────

    def _check_exit_conditions(self, market: MarketData) -> None:
        """단계 4: 매 캔들마다 포지션 종료 조건을 검사합니다."""
        pos = self.state.position
        if pos is None:
            return

        price = market.last_price

        # 4-1: 고점(저점) 갱신
        pos.update_peak(price)

        # 4-2: 손절이 트레일링보다 우선 (최악 케이스)
        if pos.should_stop_loss(price):
            logger.warning(
                "🛑 [손절] entry=%.2f current=%.2f loss=%.2f%%",
                pos.entry_price,
                price,
                abs(price - pos.entry_price) / pos.entry_price * 100,
            )
            self._close_position(market, "STOP_LOSS")

        elif pos.should_trailing_stop(price):
            logger.info(
                "💰 [트레일링 스탑] peak=%.2f current=%.2f drawdown=%.2f%%",
                pos.peak_price,
                price,
                abs(price - pos.peak_price) / pos.peak_price * 100,
            )
            self._close_position(market, "TRAILING_STOP")

    def _close_position(self, market: MarketData, reason: str) -> None:
        """단계 4: 포지션을 시장가로 전량 청산하고 RANGE 모드로 복귀합니다."""
        pos = self.state.position
        if pos is None:
            return

        close_side = "SELL" if pos.side == "BUY" else "BUY"
        price = market.last_price

        payload = self._build_market_payload(close_side, pos.size, price)
        self._send_order(payload)

        pnl = pos.unrealized_pnl(price)
        self.state.realized_pnl += pnl

        logger.info(
            "✅ [%s] 포지션 종료 | PnL=%.4f USDC | 누적=%.4f USDC",
            reason,
            pnl,
            self.state.realized_pnl,
        )

        # 단계 4: 상태 초기화 → RANGE 복귀
        self.state.position = None
        self.state.mode = StrategyMode.RANGE
        self.state.open_order_ids.clear()
        logger.info("🔄 RANGE 모드 복귀 완료")

    # ── 주문 전송 / 취소 내부 헬퍼 ───────────────────────────────────────────

    # ── edgeX API 경계 함수 ──────────────────────────────────────────────────
    # 아래 두 함수만 실제 edgeX API 규격에 맞게 교체하면 됩니다.
    # 내부 로직(상태 머신, 리스크 관리)은 이 경계 밖에서 독립적으로 동작합니다.

    def mock_edgex_order(self, payload: dict) -> str:
        """[Mock] 실제 API 규격 확정 전 사용하는 가상 주문 전송 함수.

        TODO: edgeX API 규격 확정 후 아래 실제 전송 로직으로 교체하세요.
        교체 포인트: self._session.post(EDGEX_API_URL + "/api/v1/order", ...)
        """
        fake_id = f"MOCK-{payload['side']}-{payload.get('price','0')}-{payload['size']}"
        logger.info("[MOCK] 주문: %s → id=%s", payload, fake_id)
        return fake_id

    def mock_edgex_cancel(self, order_id: str) -> bool:
        """[Mock] 실제 API 규격 확정 전 사용하는 가상 취소 함수.

        TODO: edgeX API 규격 확정 후 self._session.delete(...)로 교체하세요.
        """
        logger.info("[MOCK] 취소: %s", order_id)
        return True

    def _send_order(self, payload: dict) -> str | None:
        """payload를 edgeX API로 전송합니다.

        dry_run=True → mock_edgex_order() 사용 (실제 API 호출 없음)
        dry_run=False → 실제 서명 후 REST 전송
        """
        if self.dry_run:
            return self.mock_edgex_order(payload)

        # ── 실제 API 경계 (edgeX 규격 확정 후 수정) ──────────────────────────
        signed = self.auth.sign_order(payload)
        try:
            resp = self._session.post(
                f"{EDGEX_API_URL}/api/v1/order",
                json=signed,
                timeout=10,
            )
            resp.raise_for_status()
            order_id = resp.json().get("orderId")
            logger.info("주문 전송 OK: id=%s", order_id)
            return order_id
        except Exception as exc:
            logger.error("주문 전송 실패: %s | payload=%s", exc, payload)
            return None

    def _cancel_order(self, order_id: str) -> bool:
        """주문을 취소합니다.

        dry_run=True → mock_edgex_cancel() 사용
        dry_run=False → 실제 서명 후 REST DELETE
        """
        if self.dry_run:
            return self.mock_edgex_cancel(order_id)

        # ── 실제 API 경계 ─────────────────────────────────────────────────────
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

    # ── 단계 5: 메인 비동기 Tick (asyncio.Lock으로 상태 동시성 보호) ──────────

    async def on_market_update(self, market: MarketData) -> None:
        """매 캔들 업데이트마다 호출됩니다.

        asyncio.Lock으로 상태 전환 도중 중복 진입을 방지합니다.
        처리 순서:
          1. 포지션 종료 조건 검사 (항상 최우선)
          2. RANGE 모드: 거래량 Spike 여부 감시
          3. RANGE 모드: 미체결 주문 없으면 박스권 주문 배치
        """
        async with self._state_lock:
            # 1. 항상 먼저 리스크 체크
            self._check_exit_conditions(market)

            mode = self.state.mode

            # 2. RANGE 모드에서 Spike 감지
            if mode == StrategyMode.RANGE and self.is_volume_spike(market):
                direction = self._determine_breakout_direction(market)
                self._execute_emergency_switch(direction, market)

            # 3. RANGE 모드 & 미체결 주문 없음 → 박스권 주문 배치
            elif mode == StrategyMode.RANGE and not self.state.open_order_ids:
                self.place_range_orders(market)

    # ── 유틸리티 ─────────────────────────────────────────────────────────────

    def set_capital(self, total_capital: float) -> None:
        self.state.allocated_capital = total_capital * ENGINE_A_ALLOCATION

    def get_realized_pnl(self) -> float:
        return self.state.realized_pnl

    def get_status(self) -> dict:
        pos = self.state.position
        return {
            "mode": self.state.mode.name,
            "box": {
                "support": self.state.box.support,
                "resistance": self.state.box.resistance,
            }
            if self.state.box
            else None,
            "open_orders": len(self.state.open_order_ids),
            "position": {
                "side": pos.side,
                "entry": pos.entry_price,
                "peak": pos.peak_price,
                "size": pos.size,
                "unrealized_pnl": pos.unrealized_pnl(
                    self.state.allocated_capital / pos.size if pos.size else 0
                ),
            }
            if pos
            else None,
            "realized_pnl": self.state.realized_pnl,
        }
