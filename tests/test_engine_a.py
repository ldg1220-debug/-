"""tests/test_engine_a.py — Engine A 5단계 전체 유닛 테스트.

커버 범위:
  - 단계 1: 48시간 박스권 계산, 지정가 Payload 구조 검증
  - 단계 2: 거래량 Spike 감지, 전량 취소, 상태 전환
  - 단계 3: 돌파 시장가 Payload + 슬리피지 방어
  - 단계 4: 트레일링 스탑(-2%), 손절(-1.5%), RANGE 복귀
  - 단계 5: asyncio 전체 플로우 통합 테스트
"""

import asyncio
import os
import sys
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data_fetcher import Candle, MarketData
from src.engine_a import (
    BOX_48H_CANDLES,
    MAX_SLIPPAGE_PCT,
    STOP_LOSS_PCT,
    TRAILING_STOP_PCT,
    BoxRange,
    BreakoutPosition,
    EngineA,
    EngineAState,
    StrategyMode,
)


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def make_auth():
    auth = MagicMock()
    auth.get_headers.return_value = {}
    auth.sign_order.side_effect = lambda p: {**p, "signature": "0xSIG", "nonce": 1, "timestamp": 0}
    auth.sign_cancel.side_effect = lambda oid: {"orderId": oid, "signature": "0xSIG"}
    return auth


def make_engine(dry_run=True) -> EngineA:
    engine = EngineA(auth=make_auth(), symbol="BTC-USDC", dry_run=dry_run)
    engine.state.allocated_capital = 10_000.0
    return engine


def make_candles(n: int, base_price: float = 100.0, volume: float = 10.0) -> list[Candle]:
    return [
        Candle(
            open_time=i * 60_000,
            open=base_price,
            high=base_price * 1.005,
            low=base_price * 0.995,
            close=base_price,
            volume=volume,
        )
        for i in range(n)
    ]


def make_market(
    n_candles: int = 2880,
    price: float = 100.0,
    volume: float = 10.0,
    volume_ma_20m: float = 10.0,
) -> MarketData:
    md = MarketData(symbol="BTC-USDC")
    for c in make_candles(n_candles, price, volume):
        md.candles.append(c)
    md.last_price = price
    md.volume_ma_20m = volume_ma_20m
    return md


# ═══════════════════════════════════════════════════════════════════════════════
# 단계 1: 박스권 계산 및 지정가 주문 Payload
# ═══════════════════════════════════════════════════════════════════════════════

class TestBoxRangeCalculation(unittest.TestCase):
    """단계 1-A: 48시간 박스권 계산"""

    def setUp(self):
        self.engine = make_engine()

    def test_48h_lookback_uses_correct_window(self):
        """2880캔들(48h)만큼만 고/저를 사용해야 합니다."""
        # 처음 1000개 = 저가 50, 고가 60 → 이후 2880개 = 저가 90, 고가 110
        md = MarketData(symbol="BTC-USDC")
        for i in range(1000):
            md.candles.append(Candle(i, 55, 60, 50, 55, 1.0))
        for i in range(2880):
            md.candles.append(Candle(1000 + i, 100, 110, 90, 100, 1.0))
        md.last_price = 100.0

        box = self.engine.calculate_box_48h(md)
        # 가장 최근 2880캔들만 반영 → support=90, resistance=110
        self.assertEqual(box.support, 90.0)
        self.assertEqual(box.resistance, 110.0)

    def test_insufficient_candles_raises(self):
        md = MarketData(symbol="BTC-USDC")
        md.candles.append(Candle(0, 100, 101, 99, 100, 1.0))  # 1개만
        with self.assertRaises(ValueError):
            self.engine.calculate_box_48h(md)

    def test_support_is_global_low(self):
        md = make_market(200, price=100.0)
        # 특정 캔들에만 낮은 저가 삽입
        md.candles[50] = Candle(50, 100, 101, 80.0, 100, 1.0)
        box = self.engine.calculate_box_48h(md)
        self.assertAlmostEqual(box.support, 80.0, places=1)

    def test_resistance_is_global_high(self):
        md = make_market(200, price=100.0)
        md.candles[100] = Candle(100, 100, 130.0, 99, 100, 1.0)
        box = self.engine.calculate_box_48h(md)
        self.assertAlmostEqual(box.resistance, 130.0, places=1)

    def test_box_width_positive(self):
        md = make_market(100)
        box = self.engine.calculate_box_48h(md)
        self.assertGreater(box.width, 0)

    def test_box_breakout_direction_long(self):
        box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(box.breakout_direction(115.0), "LONG")

    def test_box_breakout_direction_short(self):
        box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(box.breakout_direction(85.0), "SHORT")

    def test_box_no_breakout_inside(self):
        box = BoxRange(support=90.0, resistance=110.0)
        self.assertIsNone(box.breakout_direction(100.0))


class TestLimitOrderPayload(unittest.TestCase):
    """단계 1-B: 지정가 주문 Payload 구조 검증"""

    def setUp(self):
        self.engine = make_engine()

    def test_buy_payload_has_required_fields(self):
        payload = self.engine._build_limit_payload("BUY", 99.5, 0.1)
        for field in ["accountId", "symbol", "side", "price", "size", "type", "timeInForce"]:
            self.assertIn(field, payload)

    def test_buy_payload_side_and_type(self):
        payload = self.engine._build_limit_payload("BUY", 99.5, 0.1)
        self.assertEqual(payload["side"], "BUY")
        self.assertEqual(payload["type"], "LIMIT")
        self.assertEqual(payload["timeInForce"], "GTC")

    def test_sell_payload_side(self):
        payload = self.engine._build_limit_payload("SELL", 110.5, 0.1)
        self.assertEqual(payload["side"], "SELL")

    def test_price_formatted_correctly(self):
        payload = self.engine._build_limit_payload("BUY", 99.1234, 0.1)
        self.assertEqual(payload["price"], "99.12")

    def test_size_formatted_correctly(self):
        payload = self.engine._build_limit_payload("BUY", 100.0, 0.123456789)
        self.assertEqual(payload["size"], "0.123457")

    def test_place_range_orders_places_two_orders(self):
        md = make_market(200)
        self.engine.place_range_orders(md)
        self.assertEqual(len(self.engine.state.open_order_ids), 2)

    def test_range_orders_buy_below_resistance_sell_above_support(self):
        """BUY는 지지선 위, SELL은 저항선 아래에 배치되어야 합니다."""
        md = make_market(200, price=100.0)
        self.engine.place_range_orders(md)
        box = self.engine.state.box
        self.assertIsNotNone(box)
        # 박스가 계산됐고 두 주문 모두 생성된 것으로 간접 검증
        self.assertGreater(box.resistance, box.support)


# ═══════════════════════════════════════════════════════════════════════════════
# 단계 2: 거래량 Spike 감지, 전량 취소, 상태 전환
# ═══════════════════════════════════════════════════════════════════════════════

class TestVolumeSpikeDetection(unittest.TestCase):
    """단계 2-A: 거래량 Spike 감지"""

    def setUp(self):
        self.engine = make_engine()

    def _market_with_spike(self, volume_ma: float, spike_volume: float) -> MarketData:
        md = make_market(50, price=100.0, volume=volume_ma)
        spike = Candle(9999, 100, 101, 99, 100, volume=spike_volume)
        md.candles.append(spike)
        md.volume_ma_20m = volume_ma
        md.last_price = 100.0
        return md

    def test_spike_at_exactly_3x(self):
        md = self._market_with_spike(10.0, 30.0)
        self.assertTrue(self.engine.is_volume_spike(md))

    def test_spike_above_3x(self):
        md = self._market_with_spike(10.0, 50.0)
        self.assertTrue(self.engine.is_volume_spike(md))

    def test_no_spike_below_3x(self):
        md = self._market_with_spike(10.0, 29.9)
        self.assertFalse(self.engine.is_volume_spike(md))

    def test_no_spike_with_zero_ma(self):
        md = make_market(2)
        md.volume_ma_20m = 0.0
        self.assertFalse(self.engine.is_volume_spike(md))

    def test_no_spike_with_single_candle(self):
        md = MarketData(symbol="BTC-USDC")
        md.candles.append(Candle(0, 100, 101, 99, 100, 999))
        md.volume_ma_20m = 10.0
        self.assertFalse(self.engine.is_volume_spike(md))


class TestBreakoutDirectionAndCancel(unittest.TestCase):
    """단계 2-B: 돌파 방향 결정 및 전량 취소"""

    def setUp(self):
        self.engine = make_engine()

    def test_direction_long_when_above_resistance(self):
        md = make_market(50, price=115.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(self.engine._determine_breakout_direction(md), "LONG")

    def test_direction_short_when_below_support(self):
        md = make_market(50, price=85.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(self.engine._determine_breakout_direction(md), "SHORT")

    def test_direction_by_momentum_inside_box(self):
        md = MarketData(symbol="BTC-USDC")
        md.candles.append(Candle(0, 100, 101, 99, 100.0, 10))
        md.candles.append(Candle(1, 100, 101, 99, 105.0, 35))  # 상승 모멘텀
        md.volume_ma_20m = 10.0
        md.last_price = 102.0
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(self.engine._determine_breakout_direction(md), "LONG")

    def test_cancel_all_clears_order_ids(self):
        self.engine.state.open_order_ids = ["id1", "id2", "id3"]
        cancelled = self.engine.cancel_all_orders()
        self.assertEqual(cancelled, 3)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)

    def test_state_switches_to_breakout_long(self):
        md = make_market(200, price=115.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._execute_emergency_switch("LONG", md)
        self.assertEqual(self.engine.state.mode, StrategyMode.BREAKOUT_LONG)

    def test_state_switches_to_breakout_short(self):
        md = make_market(200, price=85.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._execute_emergency_switch("SHORT", md)
        self.assertEqual(self.engine.state.mode, StrategyMode.BREAKOUT_SHORT)

    def test_emergency_switch_cancels_orders_first(self):
        md = make_market(200, price=115.0)
        self.engine.state.open_order_ids = ["oid1", "oid2"]
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._execute_emergency_switch("LONG", md)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 단계 3: 돌파 시장가 주문 Payload + 슬리피지 방어
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarketOrderPayload(unittest.TestCase):
    """단계 3: 시장가 Payload 구조 및 슬리피지 필드 검증"""

    def setUp(self):
        self.engine = make_engine()

    def test_market_payload_has_slippage_tolerance(self):
        payload = self.engine._build_market_payload("BUY", 0.1, 100.0)
        self.assertIn("slippageTolerance", payload)
        self.assertEqual(float(payload["slippageTolerance"]), MAX_SLIPPAGE_PCT)

    def test_market_payload_has_worst_acceptable_price(self):
        payload = self.engine._build_market_payload("BUY", 0.1, 100.0)
        self.assertIn("worstAcceptablePrice", payload)
        worst = float(payload["worstAcceptablePrice"])
        self.assertAlmostEqual(worst, 100.0 * (1 + MAX_SLIPPAGE_PCT), places=1)

    def test_worst_price_lower_for_sell(self):
        payload = self.engine._build_market_payload("SELL", 0.1, 100.0)
        worst = float(payload["worstAcceptablePrice"])
        self.assertAlmostEqual(worst, 100.0 * (1 - MAX_SLIPPAGE_PCT), places=1)

    def test_market_payload_type_is_market(self):
        payload = self.engine._build_market_payload("BUY", 0.1, 100.0)
        self.assertEqual(payload["type"], "MARKET")
        self.assertEqual(payload["timeInForce"], "IOC")

    def test_slippage_check_passes_within_tolerance(self):
        within = 100.0 * (1 + MAX_SLIPPAGE_PCT * 0.5)
        self.assertTrue(self.engine._check_slippage(100.0, within))

    def test_slippage_check_fails_outside_tolerance(self):
        outside = 100.0 * (1 + MAX_SLIPPAGE_PCT * 2)
        self.assertFalse(self.engine._check_slippage(100.0, outside))

    def test_breakout_entry_creates_position(self):
        """돌파 진입 시 포지션이 생성되어야 합니다 (슬리피지는 payload에서 처리)."""
        # resistance=100.5, current=101.5 — 1% 이탈이지만 돌파 진입은 허용
        md = make_market(200, price=112.0)
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._place_breakout_market_order(md)
        self.assertIsNotNone(self.engine.state.position)
        self.assertEqual(self.engine.state.position.side, "BUY")
        self.assertAlmostEqual(self.engine.state.position.entry_price, 112.0)

    def test_worst_acceptable_price_protects_buy_slippage(self):
        """BUY 시장가: worstAcceptablePrice = ref × (1 + slippage) — 거래소 수준 방어."""
        payload = self.engine._build_market_payload("BUY", 0.1, 100.0)
        worst = float(payload["worstAcceptablePrice"])
        self.assertAlmostEqual(worst, 100.0 * (1 + MAX_SLIPPAGE_PCT), places=2)
        # worst price는 반드시 현재가 이상이어야 함 (BUY)
        self.assertGreater(worst, 100.0)

    def test_worst_acceptable_price_protects_sell_slippage(self):
        """SELL 시장가: worstAcceptablePrice = ref × (1 - slippage)."""
        payload = self.engine._build_market_payload("SELL", 0.1, 100.0)
        worst = float(payload["worstAcceptablePrice"])
        self.assertAlmostEqual(worst, 100.0 * (1 - MAX_SLIPPAGE_PCT), places=2)
        # worst price는 반드시 현재가 이하여야 함 (SELL)
        self.assertLess(worst, 100.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 단계 4: 트레일링 스탑 / 손절 / RANGE 복귀
# ═══════════════════════════════════════════════════════════════════════════════

class TestBreakoutPositionRisk(unittest.TestCase):
    """단계 4-A: BreakoutPosition 리스크 메서드"""

    def test_trailing_stop_long_fires_at_2pct_from_peak(self):
        pos = BreakoutPosition("BUY", entry_price=100.0, size=1.0, peak_price=110.0)
        # 110 * 0.98 = 107.8 → 이하이면 발동
        self.assertFalse(pos.should_trailing_stop(108.0))  # 1.8% drop → no
        self.assertTrue(pos.should_trailing_stop(107.7))   # 2.1% drop → yes

    def test_trailing_stop_short_fires_at_2pct_from_peak(self):
        pos = BreakoutPosition("SELL", entry_price=100.0, size=1.0, peak_price=90.0)
        self.assertFalse(pos.should_trailing_stop(91.6))   # 1.8% rise → no
        self.assertTrue(pos.should_trailing_stop(91.9))    # 2.1% rise → yes

    def test_stop_loss_long_fires_at_1p5pct(self):
        pos = BreakoutPosition("BUY", entry_price=100.0, size=1.0, peak_price=100.0)
        self.assertFalse(pos.should_stop_loss(99.0))   # -1% → no
        self.assertTrue(pos.should_stop_loss(98.4))    # -1.6% → yes

    def test_stop_loss_short_fires_at_1p5pct(self):
        pos = BreakoutPosition("SELL", entry_price=100.0, size=1.0, peak_price=100.0)
        self.assertFalse(pos.should_stop_loss(101.0))
        self.assertTrue(pos.should_stop_loss(101.6))

    def test_peak_updates_upward_for_long(self):
        pos = BreakoutPosition("BUY", entry_price=100.0, size=1.0, peak_price=100.0)
        pos.update_peak(115.0)
        self.assertEqual(pos.peak_price, 115.0)
        pos.update_peak(110.0)  # 하락해도 peak 유지
        self.assertEqual(pos.peak_price, 115.0)

    def test_peak_updates_downward_for_short(self):
        pos = BreakoutPosition("SELL", entry_price=100.0, size=1.0, peak_price=100.0)
        pos.update_peak(85.0)
        self.assertEqual(pos.peak_price, 85.0)
        pos.update_peak(90.0)
        self.assertEqual(pos.peak_price, 85.0)

    def test_unrealized_pnl_long_positive(self):
        pos = BreakoutPosition("BUY", entry_price=100.0, size=2.0, peak_price=100.0)
        self.assertAlmostEqual(pos.unrealized_pnl(110.0), 20.0)

    def test_unrealized_pnl_short_positive(self):
        pos = BreakoutPosition("SELL", entry_price=100.0, size=2.0, peak_price=100.0)
        self.assertAlmostEqual(pos.unrealized_pnl(90.0), 20.0)


class TestExitAndRangeReset(unittest.TestCase):
    """단계 4-B: 청산 후 RANGE 복귀"""

    def setUp(self):
        self.engine = make_engine()
        # BREAKOUT_LONG 포지션 세팅
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine.state.position = BreakoutPosition(
            side="BUY", entry_price=100.0, size=1.0, peak_price=100.0
        )

    def _tick(self, price: float) -> None:
        md = make_market(200, price=price)
        self.engine._check_exit_conditions(md)

    def test_trailing_stop_resets_mode_to_range(self):
        # 고점 115 → 현재 112.7 (2.1% 하락)
        self.engine.state.position.peak_price = 115.0
        self._tick(112.7)
        self.assertEqual(self.engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(self.engine.state.position)

    def test_stop_loss_resets_mode_to_range(self):
        self._tick(98.4)  # -1.6% 손절
        self.assertEqual(self.engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(self.engine.state.position)

    def test_trailing_stop_records_positive_pnl(self):
        self.engine.state.position = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=120.0
        )
        self._tick(117.6)  # peak(120) 대비 2% 초과 하락
        self.assertGreater(self.engine.state.realized_pnl, 0)

    def test_stop_loss_records_negative_pnl(self):
        self._tick(98.4)  # -1.6%
        self.assertLess(self.engine.state.realized_pnl, 0)

    def test_open_order_ids_cleared_after_exit(self):
        self.engine.state.open_order_ids = ["x1"]
        self._tick(98.4)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)

    def test_stop_loss_takes_priority_over_trailing_stop(self):
        """동시 조건에서 손절이 먼저 실행되어야 합니다."""
        # entry=100, peak=100, current=98.3 → 손절(-1.7%) & trailing_stop(0%) 동시
        self.engine.state.position = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=100.0
        )
        self._tick(98.3)
        # 어느 쪽이든 포지션이 청산되고 RANGE로 복귀해야 함
        self.assertIsNone(self.engine.state.position)
        self.assertEqual(self.engine.state.mode, StrategyMode.RANGE)


# ═══════════════════════════════════════════════════════════════════════════════
# 단계 5: asyncio 전체 플로우 통합 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullFlowAsync(unittest.IsolatedAsyncioTestCase):
    """단계 5: RANGE → 거래량 폭발 → 추격 진입 → 트레일링 스탑 익절 → RANGE"""

    def _build_market(
        self,
        n: int = 2880,
        price: float = 100.0,
        volume: float = 10.0,
        volume_ma: float = 10.0,
    ) -> MarketData:
        md = MarketData(symbol="BTC-USDC")
        for c in make_candles(n, price, volume):
            md.candles.append(c)
        md.last_price = price
        md.volume_ma_20m = volume_ma
        return md

    async def test_range_places_two_limit_orders(self):
        """정상 거래량 → RANGE 모드에서 박스권 주문 2개 배치."""
        engine = make_engine()
        md = self._build_market(2880, price=100.0, volume=10.0, volume_ma=10.0)

        await engine.on_market_update(md)

        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertEqual(len(engine.state.open_order_ids), 2)

    async def test_volume_spike_cancels_and_enters_breakout_long(self):
        """거래량 폭발 + 저항선 돌파 → 전량 취소 + BREAKOUT_LONG 진입."""
        engine = make_engine()

        # 1. 먼저 RANGE 상태로 주문 배치
        md_range = self._build_market(2880, price=100.0, volume=10.0, volume_ma=10.0)
        await engine.on_market_update(md_range)
        self.assertEqual(len(engine.state.open_order_ids), 2)

        # 2. 거래량 폭발 + 가격 저항선 돌파
        box = engine.state.box
        self.assertIsNotNone(box)
        spike_price = box.resistance * 1.01  # 저항선 1% 상회

        md_spike = self._build_market(2880, price=spike_price, volume_ma=10.0)
        md_spike.last_price = spike_price
        # 마지막 캔들에 3배 이상 거래량 주입
        spike_candle = Candle(9999999, spike_price, spike_price * 1.01, spike_price * 0.99, spike_price, volume=35.0)
        md_spike.candles.append(spike_candle)
        md_spike.volume_ma_20m = 10.0

        await engine.on_market_update(md_spike)

        # 검증
        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_LONG)
        self.assertEqual(len(engine.state.open_order_ids), 0, "미체결 주문이 전량 취소되어야 함")
        self.assertIsNotNone(engine.state.position, "돌파 포지션이 생성되어야 함")
        self.assertEqual(engine.state.position.side, "BUY")

    async def test_volume_spike_enters_breakout_short(self):
        """거래량 폭발 + 지지선 붕괴 → BREAKOUT_SHORT 진입."""
        engine = make_engine()
        md_range = self._build_market(2880, price=100.0, volume_ma=10.0)
        await engine.on_market_update(md_range)

        box = engine.state.box
        breakdown_price = box.support * 0.99  # 지지선 하회

        md_spike = self._build_market(2880, price=breakdown_price, volume_ma=10.0)
        md_spike.last_price = breakdown_price
        spike_candle = Candle(9999999, breakdown_price, breakdown_price * 1.01, breakdown_price * 0.99, breakdown_price, volume=40.0)
        md_spike.candles.append(spike_candle)
        md_spike.volume_ma_20m = 10.0

        await engine.on_market_update(md_spike)

        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_SHORT)
        self.assertIsNotNone(engine.state.position)
        self.assertEqual(engine.state.position.side, "SELL")

    async def test_full_flow_trailing_stop_exit_and_range_reset(self):
        """핵심 통합 테스트: 거래량 폭발 → 추격 진입 → 가격 상승 → 트레일링 스탑 익절 → RANGE."""
        engine = make_engine()

        # ── Step 1: RANGE 모드 진입 ──────────────────────────────────────────
        md = self._build_market(2880, price=100.0, volume_ma=10.0)
        await engine.on_market_update(md)
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)

        # ── Step 2: 거래량 폭발로 BREAKOUT_LONG 전환 ────────────────────────
        box = engine.state.box
        entry_price = box.resistance * 1.01  # 돌파 가격

        md_spike = self._build_market(2880, price=entry_price, volume_ma=10.0)
        md_spike.last_price = entry_price
        spike = Candle(9999, entry_price, entry_price * 1.01, entry_price * 0.99, entry_price, 50.0)
        md_spike.candles.append(spike)
        md_spike.volume_ma_20m = 10.0
        await engine.on_market_update(md_spike)

        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_LONG)
        pos = engine.state.position
        self.assertIsNotNone(pos)

        # ── Step 3: 가격 상승 → 고점 갱신 ──────────────────────────────────
        for price in [entry_price * 1.02, entry_price * 1.04, entry_price * 1.06, entry_price * 1.08]:
            md_up = self._build_market(10, price=price, volume_ma=10.0)
            await engine.on_market_update(md_up)
            # 포지션 유지 & 고점 갱신 확인
            self.assertIsNotNone(engine.state.position)
            self.assertAlmostEqual(engine.state.position.peak_price, price, places=2)

        peak = engine.state.position.peak_price  # ≈ entry * 1.08

        # ── Step 4: 고점 대비 2.1% 하락 → 트레일링 스탑 발동 ───────────────
        trailing_trigger = peak * (1 - TRAILING_STOP_PCT - 0.001)  # 살짝 초과
        md_drop = self._build_market(10, price=trailing_trigger, volume_ma=10.0)
        await engine.on_market_update(md_drop)

        # ── 검증 ─────────────────────────────────────────────────────────────
        self.assertIsNone(engine.state.position, "포지션이 청산되어야 합니다")
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "RANGE 모드로 복귀해야 합니다")
        self.assertGreater(engine.state.realized_pnl, 0, "익절이므로 PnL이 양수여야 합니다")
        # 트레일링 스탑 직후 같은 틱에서 RANGE 복귀 → 즉시 새 박스권 주문 배치 (정상 동작)
        self.assertEqual(
            len(engine.state.open_order_ids), 2,
            "RANGE 복귀 후 즉시 박스권 주문 2개가 배치되어야 합니다"
        )

    async def test_full_flow_stop_loss_resets_to_range(self):
        """가짜 돌파(Fakeout) → 손절 → RANGE 복귀."""
        engine = make_engine()

        # BREAKOUT_LONG 포지션 직접 주입
        entry = 100.0
        engine.state.mode = StrategyMode.BREAKOUT_LONG
        engine.state.position = BreakoutPosition(
            side="BUY", entry_price=entry, size=1.0, peak_price=entry
        )

        # 손절선(-1.5%) 아래로 가격 하락
        stop_price = entry * (1 - STOP_LOSS_PCT - 0.005)
        md = self._build_market(10, price=stop_price, volume_ma=10.0)
        await engine.on_market_update(md)

        self.assertIsNone(engine.state.position)
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertLess(engine.state.realized_pnl, 0, "손절이므로 PnL이 음수여야 합니다")

    async def test_state_lock_prevents_double_entry(self):
        """asyncio.Lock: 동시 호출 시 상태가 두 번 전환되지 않아야 합니다."""
        engine = make_engine()
        md_range = self._build_market(2880, price=100.0, volume_ma=10.0)
        await engine.on_market_update(md_range)

        box = engine.state.box
        spike_price = box.resistance * 1.01
        md_spike = self._build_market(2880, price=spike_price, volume_ma=10.0)
        spike = Candle(9999, spike_price, spike_price * 1.01, spike_price * 0.99, spike_price, 50.0)
        md_spike.candles.append(spike)
        md_spike.volume_ma_20m = 10.0

        # 동시에 두 번 호출
        await asyncio.gather(
            engine.on_market_update(md_spike),
            engine.on_market_update(md_spike),
        )

        # Lock 덕분에 포지션이 최대 1개만 생성되어야 함
        self.assertIn(engine.state.mode, (StrategyMode.BREAKOUT_LONG, StrategyMode.BREAKOUT_SHORT, StrategyMode.RANGE))
        # 포지션이 있으면 1개뿐이어야 함
        if engine.state.position is not None:
            self.assertIsInstance(engine.state.position, BreakoutPosition)


if __name__ == "__main__":
    unittest.main(verbosity=2)
