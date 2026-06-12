"""tests/test_engine_a.py — Engine A 매매 로직 개선판 전체 유닛 테스트.

커버 범위:
  - 그리드 주문 (N레벨 × 양방향, 현재가 기준 균등 배치)
  - 손절 후 쿨다운 (연속 fakeout 방지)
  - ATR 기반 동적 스탑 + 고정 % 폴백
  - asyncio 전체 플로우 통합 (RANGE → Spike → Breakout → Exit → RANGE)
"""

import asyncio
import os
import sys
import time
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data_fetcher import Candle, MarketData
from src.engine_a import (
    ATR_MULTIPLIER_SL,
    ATR_MULTIPLIER_TRAILING,
    ATR_PERIOD,
    BOX_48H_CANDLES,
    MAX_SLIPPAGE_PCT,
    N_GRID_LEVELS,
    STOP_LOSS_COOLDOWN_SECONDS,
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


def make_candles(
    n: int,
    base_price: float = 100.0,
    volume: float = 10.0,
    high_mult: float = 1.005,
    low_mult: float = 0.995,
) -> list[Candle]:
    return [
        Candle(
            open_time=i * 60_000,
            open=base_price,
            high=base_price * high_mult,
            low=base_price * low_mult,
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
# 박스권 계산
# ═══════════════════════════════════════════════════════════════════════════════

class TestBoxRangeCalculation(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()

    def test_48h_lookback_ignores_older_candles(self):
        """2880개 이전 캔들은 박스권 계산에서 제외되어야 합니다."""
        md = MarketData(symbol="BTC-USDC")
        for i in range(1000):
            md.candles.append(Candle(i, 55, 60, 50, 55, 1.0))
        for i in range(2880):
            md.candles.append(Candle(1000 + i, 100, 110, 90, 100, 1.0))
        md.last_price = 100.0

        box = self.engine.calculate_box_48h(md)
        self.assertEqual(box.support, 90.0)
        self.assertEqual(box.resistance, 110.0)

    def test_insufficient_candles_raises(self):
        md = MarketData(symbol="BTC-USDC")
        md.candles.append(Candle(0, 100, 101, 99, 100, 1.0))
        with self.assertRaises(ValueError):
            self.engine.calculate_box_48h(md)

    def test_support_is_minimum_low(self):
        md = make_market(200, price=100.0)
        md.candles[50] = Candle(50, 100, 101, 75.0, 100, 1.0)
        box = self.engine.calculate_box_48h(md)
        self.assertAlmostEqual(box.support, 75.0, places=1)

    def test_resistance_is_maximum_high(self):
        md = make_market(200, price=100.0)
        md.candles[100] = Candle(100, 100, 135.0, 99, 100, 1.0)
        box = self.engine.calculate_box_48h(md)
        self.assertAlmostEqual(box.resistance, 135.0, places=1)

    def test_box_breakout_long(self):
        box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(box.breakout_direction(115.0), "LONG")

    def test_box_breakout_short(self):
        box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(box.breakout_direction(85.0), "SHORT")

    def test_box_inside_returns_none(self):
        box = BoxRange(support=90.0, resistance=110.0)
        self.assertIsNone(box.breakout_direction(100.0))


# ═══════════════════════════════════════════════════════════════════════════════
# 그리드 주문
# ═══════════════════════════════════════════════════════════════════════════════

class TestGridOrders(unittest.TestCase):
    """RANGE 모드 그리드 주문: N레벨 × 양방향"""

    def setUp(self):
        self.engine = make_engine()

    def test_grid_levels_count(self):
        """_build_grid_levels가 BUY N개 + SELL N개를 반환해야 합니다."""
        box = BoxRange(support=99.0, resistance=101.0)
        levels = self.engine._build_grid_levels(box, 10_000.0, 100.0)
        buys = [l for l in levels if l[0] == "BUY"]
        sells = [l for l in levels if l[0] == "SELL"]
        self.assertEqual(len(buys), N_GRID_LEVELS)
        self.assertEqual(len(sells), N_GRID_LEVELS)

    def test_buy_levels_below_current_price(self):
        box = BoxRange(support=99.0, resistance=101.0)
        levels = self.engine._build_grid_levels(box, 10_000.0, 100.0)
        for side, price, _ in levels:
            if side == "BUY":
                self.assertLess(price, 100.0)

    def test_sell_levels_above_current_price(self):
        box = BoxRange(support=99.0, resistance=101.0)
        levels = self.engine._build_grid_levels(box, 10_000.0, 100.0)
        for side, price, _ in levels:
            if side == "SELL":
                self.assertGreater(price, 100.0)

    def test_buy_levels_above_support(self):
        box = BoxRange(support=99.0, resistance=101.0)
        levels = self.engine._build_grid_levels(box, 10_000.0, 100.0)
        for side, price, _ in levels:
            if side == "BUY":
                self.assertGreater(price, box.support)

    def test_sell_levels_below_resistance(self):
        box = BoxRange(support=99.0, resistance=101.0)
        levels = self.engine._build_grid_levels(box, 10_000.0, 100.0)
        for side, price, _ in levels:
            if side == "SELL":
                self.assertLess(price, box.resistance)

    def test_capital_distributed_evenly(self):
        """각 레벨의 notional(가격 × 수량)이 균등해야 합니다."""
        box = BoxRange(support=99.0, resistance=101.0)
        levels = self.engine._build_grid_levels(box, 10_000.0, 100.0)
        per_level_capital = 10_000.0 / (N_GRID_LEVELS * 2)
        for side, price, size in levels:
            notional = price * size
            self.assertAlmostEqual(notional, per_level_capital, delta=0.1)

    def test_place_range_orders_places_grid(self):
        """place_range_orders가 2×N_GRID_LEVELS 주문을 배치해야 합니다."""
        md = make_market(2880, price=100.0)
        self.engine.place_range_orders(md)
        self.assertEqual(len(self.engine.state.open_order_ids), N_GRID_LEVELS * 2)

    def test_place_range_orders_skipped_outside_box(self):
        """현재가가 박스권 밖이면 주문을 배치하지 않아야 합니다."""
        md = make_market(200, price=100.0)  # 캔들은 99.5~100.5 박스권
        md.last_price = 200.0  # 현재가를 박스권 밖으로 설정
        self.engine.place_range_orders(md)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)

    def test_limit_payload_structure(self):
        payload = self.engine._build_limit_payload("BUY", 99.5, 0.1)
        for key in ["accountId", "symbol", "side", "price", "size", "type", "timeInForce"]:
            self.assertIn(key, payload)
        self.assertEqual(payload["type"], "LIMIT")
        self.assertEqual(payload["timeInForce"], "GTC")


# ═══════════════════════════════════════════════════════════════════════════════
# ATR 기반 동적 스탑
# ═══════════════════════════════════════════════════════════════════════════════

class TestAtrBasedStops(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()

    def _make_volatile_market(self, n=50, price=100.0, high_mult=1.02, low_mult=0.98) -> MarketData:
        md = MarketData(symbol="BTC-USDC")
        for c in make_candles(n, price, volume=10.0, high_mult=high_mult, low_mult=low_mult):
            md.candles.append(c)
        md.last_price = price
        md.volume_ma_20m = 10.0
        return md

    def test_atr_returns_float_for_sufficient_data(self):
        md = self._make_volatile_market(n=60)
        atr = EngineA._calculate_atr(md)
        self.assertIsNotNone(atr)
        self.assertIsInstance(atr, float)
        self.assertGreater(atr, 0)

    def test_atr_returns_none_for_insufficient_data(self):
        md = self._make_volatile_market(n=5)
        atr = EngineA._calculate_atr(md)
        self.assertIsNone(atr)

    def test_atr_reflects_volatility(self):
        """변동성이 큰 시장이 작은 시장보다 ATR이 높아야 합니다."""
        low_vol = self._make_volatile_market(60, high_mult=1.001, low_mult=0.999)
        high_vol = self._make_volatile_market(60, high_mult=1.03, low_mult=0.97)
        atr_low = EngineA._calculate_atr(low_vol)
        atr_high = EngineA._calculate_atr(high_vol)
        self.assertGreater(atr_high, atr_low)

    def test_position_stores_atr_distances(self):
        """돌파 진입 시 ATR 기반 스탑 거리가 포지션에 저장되어야 합니다."""
        md = self._make_volatile_market(n=60, price=100.0, high_mult=1.02, low_mult=0.98)
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine._place_breakout_market_order(md)

        pos = self.engine.state.position
        self.assertIsNotNone(pos)
        atr = EngineA._calculate_atr(md)
        if atr:
            self.assertAlmostEqual(pos.trailing_stop_distance, atr * ATR_MULTIPLIER_TRAILING, places=3)
            self.assertAlmostEqual(pos.stop_loss_distance, atr * ATR_MULTIPLIER_SL, places=3)

    def test_position_uses_fixed_fallback_when_no_atr(self):
        """데이터 부족 시 trailing_stop_distance=0 (고정 % 폴백)이어야 합니다."""
        md = self._make_volatile_market(n=5)  # ATR 계산 불가
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine._place_breakout_market_order(md)

        pos = self.engine.state.position
        self.assertIsNotNone(pos)
        self.assertEqual(pos.trailing_stop_distance, 0.0)
        self.assertEqual(pos.stop_loss_distance, 0.0)

    def test_atr_stop_triggers_correctly_for_long(self):
        """ATR 기반 손절: entry - atr*1.0 이하이면 발동해야 합니다."""
        atr = 1.5
        pos = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=100.0,
            trailing_stop_distance=atr * ATR_MULTIPLIER_TRAILING,
            stop_loss_distance=atr * ATR_MULTIPLIER_SL,
        )
        # entry(100) - atr*SL(1.5) = 98.5 → 이하이면 손절
        self.assertFalse(pos.should_stop_loss(98.6))
        self.assertTrue(pos.should_stop_loss(98.4))

    def test_atr_trailing_triggers_for_long(self):
        atr = 1.5
        pos = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=110.0,
            trailing_stop_distance=atr * ATR_MULTIPLIER_TRAILING,  # 2.25
            stop_loss_distance=atr * ATR_MULTIPLIER_SL,
        )
        # peak(110) - trailing(2.25) = 107.75 → 이하이면 발동
        self.assertFalse(pos.should_trailing_stop(107.8))
        self.assertTrue(pos.should_trailing_stop(107.7))

    def test_fixed_pct_fallback_when_distance_is_zero(self):
        """stop_loss_distance=0이면 고정 % 로직을 사용해야 합니다."""
        pos = BreakoutPosition("BUY", entry_price=100.0, size=1.0, peak_price=100.0)
        self.assertFalse(pos.should_stop_loss(99.0))
        self.assertTrue(pos.should_stop_loss(98.4))


# ═══════════════════════════════════════════════════════════════════════════════
# 손절 후 쿨다운
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopLossCooldown(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()
        # BREAKOUT_LONG 포지션 직접 주입
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine.state.position = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=100.0
        )

    def test_stop_loss_activates_cooldown(self):
        """손절 청산 후 쿨다운이 활성화되어야 합니다."""
        md = make_market(50, price=98.4)
        self.engine._check_exit_conditions(md)
        self.assertTrue(self.engine._is_in_cooldown())

    def test_cooldown_until_is_set_correctly(self):
        md = make_market(50, price=98.4)
        before = time.time()
        self.engine._check_exit_conditions(md)
        after = time.time()
        expected_min = before + STOP_LOSS_COOLDOWN_SECONDS
        expected_max = after + STOP_LOSS_COOLDOWN_SECONDS
        self.assertGreaterEqual(self.engine.state.cooldown_until, expected_min)
        self.assertLessEqual(self.engine.state.cooldown_until, expected_max)

    def test_trailing_stop_does_not_activate_cooldown(self):
        """트레일링 스탑(익절)은 쿨다운을 활성화하지 않아야 합니다."""
        self.engine.state.position = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=110.0
        )
        md = make_market(50, price=107.7)  # 트레일링 스탑 발동
        self.engine._check_exit_conditions(md)
        self.assertFalse(self.engine._is_in_cooldown())

    def test_place_range_orders_blocked_during_cooldown(self):
        """쿨다운 중에는 그리드 주문이 배치되지 않아야 합니다."""
        self.engine.state.cooldown_until = time.time() + 3600  # 1시간 쿨다운
        md = make_market(2880, price=100.0)
        self.engine.place_range_orders(md)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)

    def test_place_range_orders_allowed_after_cooldown(self):
        """쿨다운이 끝나면 정상적으로 그리드 주문이 배치되어야 합니다."""
        self.engine.state.cooldown_until = time.time() - 1  # 이미 만료
        md = make_market(2880, price=100.0)
        self.engine.place_range_orders(md)
        self.assertGreater(len(self.engine.state.open_order_ids), 0)

    def test_is_in_cooldown_false_by_default(self):
        engine = make_engine()
        self.assertFalse(engine._is_in_cooldown())


# ═══════════════════════════════════════════════════════════════════════════════
# 거래량 Spike + 상태 전환
# ═══════════════════════════════════════════════════════════════════════════════

class TestVolumeSpikeSwitch(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()

    def test_spike_at_3x(self):
        md = make_market(50, volume_ma_20m=10.0)
        md.candles.append(Candle(9999, 100, 101, 99, 100, volume=30.0))
        md.volume_ma_20m = 10.0
        self.assertTrue(self.engine.is_volume_spike(md))

    def test_no_spike_below_3x(self):
        md = make_market(50, volume_ma_20m=10.0)
        md.candles.append(Candle(9999, 100, 101, 99, 100, volume=29.9))
        md.volume_ma_20m = 10.0
        self.assertFalse(self.engine.is_volume_spike(md))

    def test_direction_long_above_resistance(self):
        md = make_market(50, price=115.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(self.engine._determine_breakout_direction(md), "LONG")

    def test_direction_short_below_support(self):
        md = make_market(50, price=85.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.assertEqual(self.engine._determine_breakout_direction(md), "SHORT")

    def test_emergency_switch_cancels_all_orders(self):
        md = make_market(200, price=115.0)
        self.engine.state.open_order_ids = ["oid1", "oid2", "oid3"]
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._execute_emergency_switch("LONG", md)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)

    def test_emergency_switch_sets_mode_breakout_long(self):
        md = make_market(200, price=115.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._execute_emergency_switch("LONG", md)
        self.assertEqual(self.engine.state.mode, StrategyMode.BREAKOUT_LONG)

    def test_emergency_switch_sets_mode_breakout_short(self):
        md = make_market(200, price=85.0)
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._execute_emergency_switch("SHORT", md)
        self.assertEqual(self.engine.state.mode, StrategyMode.BREAKOUT_SHORT)


# ═══════════════════════════════════════════════════════════════════════════════
# 시장가 주문 + 슬리피지 방어
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarketOrderPayload(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()

    def test_market_payload_has_slippage_tolerance(self):
        payload = self.engine._build_market_payload("BUY", 0.1, 100.0)
        self.assertIn("slippageTolerance", payload)
        self.assertEqual(float(payload["slippageTolerance"]), MAX_SLIPPAGE_PCT)

    def test_worst_price_buy(self):
        payload = self.engine._build_market_payload("BUY", 0.1, 100.0)
        self.assertAlmostEqual(
            float(payload["worstAcceptablePrice"]),
            100.0 * (1 + MAX_SLIPPAGE_PCT), places=2
        )

    def test_worst_price_sell(self):
        payload = self.engine._build_market_payload("SELL", 0.1, 100.0)
        self.assertAlmostEqual(
            float(payload["worstAcceptablePrice"]),
            100.0 * (1 - MAX_SLIPPAGE_PCT), places=2
        )

    def test_breakout_entry_creates_position(self):
        md = make_market(60, price=112.0)
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._place_breakout_market_order(md)
        self.assertIsNotNone(self.engine.state.position)
        self.assertEqual(self.engine.state.position.side, "BUY")

    def test_breakout_short_entry(self):
        md = make_market(60, price=88.0)
        self.engine.state.mode = StrategyMode.BREAKOUT_SHORT
        self.engine.state.box = BoxRange(support=90.0, resistance=110.0)
        self.engine._place_breakout_market_order(md)
        self.assertIsNotNone(self.engine.state.position)
        self.assertEqual(self.engine.state.position.side, "SELL")

    def test_slippage_check_within_tolerance(self):
        self.assertTrue(self.engine._check_slippage(100.0, 100.4))

    def test_slippage_check_outside_tolerance(self):
        self.assertFalse(self.engine._check_slippage(100.0, 101.0))


# ═══════════════════════════════════════════════════════════════════════════════
# 포지션 리스크 (BreakoutPosition 메서드)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBreakoutPositionRisk(unittest.TestCase):

    def test_atr_stop_loss_long(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 100.0, stop_loss_distance=1.5)
        self.assertFalse(pos.should_stop_loss(98.6))
        self.assertTrue(pos.should_stop_loss(98.4))

    def test_atr_trailing_stop_long(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 110.0, trailing_stop_distance=2.25)
        self.assertFalse(pos.should_trailing_stop(107.8))
        self.assertTrue(pos.should_trailing_stop(107.7))

    def test_fixed_pct_stop_loss_long(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 100.0)  # distance=0 → fixed %
        self.assertFalse(pos.should_stop_loss(99.0))
        self.assertTrue(pos.should_stop_loss(98.4))

    def test_fixed_pct_trailing_stop_long(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 110.0)  # distance=0 → fixed %
        self.assertFalse(pos.should_trailing_stop(108.5))
        self.assertTrue(pos.should_trailing_stop(107.7))

    def test_peak_update_long(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 100.0)
        pos.update_peak(115.0)
        self.assertEqual(pos.peak_price, 115.0)
        pos.update_peak(110.0)
        self.assertEqual(pos.peak_price, 115.0)

    def test_peak_update_short(self):
        pos = BreakoutPosition("SELL", 100.0, 1.0, 100.0)
        pos.update_peak(85.0)
        self.assertEqual(pos.peak_price, 85.0)
        pos.update_peak(90.0)
        self.assertEqual(pos.peak_price, 85.0)

    def test_unrealized_pnl_long(self):
        pos = BreakoutPosition("BUY", 100.0, 2.0, 100.0)
        self.assertAlmostEqual(pos.unrealized_pnl(110.0), 20.0)

    def test_unrealized_pnl_short(self):
        pos = BreakoutPosition("SELL", 100.0, 2.0, 100.0)
        self.assertAlmostEqual(pos.unrealized_pnl(90.0), 20.0)

    def test_effective_stop_pct_atr_based(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 100.0, stop_loss_distance=1.5)
        self.assertAlmostEqual(pos.effective_stop_loss_pct(), 1.5, places=1)

    def test_effective_stop_pct_fallback(self):
        pos = BreakoutPosition("BUY", 100.0, 1.0, 100.0)  # distance=0
        self.assertAlmostEqual(pos.effective_stop_loss_pct(), STOP_LOSS_PCT * 100, places=2)


# ═══════════════════════════════════════════════════════════════════════════════
# 청산 및 RANGE 복귀
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitAndRangeReset(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()
        self.engine.state.mode = StrategyMode.BREAKOUT_LONG
        self.engine.state.position = BreakoutPosition(
            "BUY", entry_price=100.0, size=1.0, peak_price=100.0
        )

    def _check_exit(self, price: float):
        md = make_market(50, price=price)
        self.engine._check_exit_conditions(md)

    def test_stop_loss_resets_to_range(self):
        self._check_exit(98.4)
        self.assertEqual(self.engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(self.engine.state.position)

    def test_trailing_stop_resets_to_range(self):
        self.engine.state.position.peak_price = 115.0
        self._check_exit(112.7)
        self.assertEqual(self.engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(self.engine.state.position)

    def test_stop_loss_records_negative_pnl(self):
        self._check_exit(98.4)
        self.assertLess(self.engine.state.realized_pnl, 0)

    def test_trailing_stop_records_positive_pnl(self):
        self.engine.state.position = BreakoutPosition(
            "BUY", 100.0, 1.0, 120.0
        )
        self._check_exit(117.6)
        self.assertGreater(self.engine.state.realized_pnl, 0)

    def test_stop_loss_clears_open_orders(self):
        self.engine.state.open_order_ids = ["x1", "x2"]
        self._check_exit(98.4)
        self.assertEqual(len(self.engine.state.open_order_ids), 0)

    def test_stop_loss_priority_over_trailing(self):
        """손절과 트레일링이 동시 충족 시 손절이 우선 실행되어야 합니다."""
        self.engine.state.position = BreakoutPosition(
            "BUY", 100.0, 1.0, 100.0
        )
        self._check_exit(98.3)  # 손절(-1.7%) + 트레일링(0%) 동시
        self.assertIsNone(self.engine.state.position)
        self.assertEqual(self.engine.state.mode, StrategyMode.RANGE)


# ═══════════════════════════════════════════════════════════════════════════════
# asyncio 전체 플로우 통합 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullFlowAsync(unittest.IsolatedAsyncioTestCase):

    def _market(self, n=2880, price=100.0, volume_ma=10.0) -> MarketData:
        return make_market(n, price=price, volume_ma_20m=volume_ma)

    def _add_spike(self, md: MarketData, price: float, volume: float) -> MarketData:
        md.candles.append(Candle(9_999_999, price, price*1.01, price*0.99, price, volume))
        md.last_price = price
        return md

    async def test_range_places_grid_orders(self):
        """RANGE 모드: 그리드 주문 N_GRID_LEVELS×2 개 배치."""
        engine = make_engine()
        md = self._market()
        await engine.on_market_update(md)
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertEqual(len(engine.state.open_order_ids), N_GRID_LEVELS * 2)

    async def test_volume_spike_breakout_long(self):
        """거래량 Spike + 저항선 돌파 → 전량 취소 + BREAKOUT_LONG + 포지션 생성."""
        engine = make_engine()
        await engine.on_market_update(self._market())
        box = engine.state.box

        spike_price = box.resistance * 1.01
        md = self._market(price=spike_price)
        self._add_spike(md, spike_price, 50.0)  # 5× volume

        await engine.on_market_update(md)

        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_LONG)
        self.assertEqual(len(engine.state.open_order_ids), 0, "미체결 주문 전량 취소")
        self.assertIsNotNone(engine.state.position)
        self.assertEqual(engine.state.position.side, "BUY")

    async def test_volume_spike_breakout_short(self):
        """거래량 Spike + 지지선 이탈 → BREAKOUT_SHORT + 포지션 생성."""
        engine = make_engine()
        await engine.on_market_update(self._market())
        box = engine.state.box

        spike_price = box.support * 0.99
        md = self._market(price=spike_price)
        self._add_spike(md, spike_price, 50.0)

        await engine.on_market_update(md)

        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_SHORT)
        self.assertIsNotNone(engine.state.position)
        self.assertEqual(engine.state.position.side, "SELL")

    async def test_full_flow_trailing_stop_and_range_grid_reset(self):
        """핵심: RANGE → Spike → BREAKOUT_LONG → 가격 상승 → 트레일링 스탑 → RANGE + 그리드 즉시 재배치."""
        engine = make_engine()

        # 1. RANGE 진입 + 그리드 배치
        await engine.on_market_update(self._market())
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)

        # 2. 거래량 Spike → 돌파
        box = engine.state.box
        entry = box.resistance * 1.005
        md_spike = self._market(price=entry)
        self._add_spike(md_spike, entry, 50.0)
        await engine.on_market_update(md_spike)
        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_LONG)
        pos = engine.state.position
        self.assertIsNotNone(pos)

        # 3. 가격 상승 → 고점 갱신 (트레일링 스탑이 아직 발동하지 않도록)
        for p in [entry * 1.02, entry * 1.04, entry * 1.06, entry * 1.08]:
            md_up = self._market(n=60, price=p)
            await engine.on_market_update(md_up)
        peak = engine.state.position.peak_price
        self.assertAlmostEqual(peak, entry * 1.08, delta=0.1)

        # 4. 고점 대비 트레일링 스탑 발동
        trailing_trigger = peak - (pos.trailing_stop_distance or peak * TRAILING_STOP_PCT) - 0.01
        md_drop = self._market(n=60, price=trailing_trigger)
        await engine.on_market_update(md_drop)

        # 검증: 청산 + RANGE 복귀 + 그리드 즉시 재배치 (쿨다운 없음)
        self.assertIsNone(engine.state.position, "포지션 청산")
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "RANGE 복귀")
        self.assertGreater(engine.state.realized_pnl, 0, "익절 PnL 양수")
        self.assertEqual(
            len(engine.state.open_order_ids), N_GRID_LEVELS * 2,
            "트레일링 스탑 후 즉시 그리드 재배치 (쿨다운 없음)"
        )

    async def test_full_flow_stop_loss_triggers_cooldown(self):
        """Fakeout → 손절 → 쿨다운 → 그리드 주문 차단."""
        engine = make_engine()

        entry = 100.0
        engine.state.mode = StrategyMode.BREAKOUT_LONG
        engine.state.position = BreakoutPosition(
            "BUY", entry_price=entry, size=1.0, peak_price=entry
        )

        stop_price = entry * (1 - STOP_LOSS_PCT - 0.005)
        md = self._market(n=60, price=stop_price)
        await engine.on_market_update(md)

        self.assertIsNone(engine.state.position, "포지션 청산")
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "RANGE 복귀")
        self.assertLess(engine.state.realized_pnl, 0, "손절 PnL 음수")
        self.assertTrue(engine._is_in_cooldown(), "쿨다운 활성화")
        self.assertEqual(
            len(engine.state.open_order_ids), 0,
            "쿨다운 중 그리드 주문 차단"
        )

    async def test_lock_prevents_concurrent_double_entry(self):
        """asyncio.Lock: 동시 호출 시 포지션이 1개만 생성되어야 합니다."""
        engine = make_engine()
        await engine.on_market_update(self._market())

        box = engine.state.box
        spike_price = box.resistance * 1.01
        md_spike = self._market(price=spike_price)
        self._add_spike(md_spike, spike_price, 50.0)

        await asyncio.gather(
            engine.on_market_update(md_spike),
            engine.on_market_update(md_spike),
        )

        # 포지션 최대 1개
        if engine.state.position is not None:
            self.assertIsInstance(engine.state.position, BreakoutPosition)
        self.assertIn(
            engine.state.mode,
            (StrategyMode.BREAKOUT_LONG, StrategyMode.BREAKOUT_SHORT, StrategyMode.RANGE),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
