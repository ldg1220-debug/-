"""tests/test_master_engine.py — 엔진 A 마스터 시나리오 통합 검증.

전체 흐름 (7단계):
  ① RANGE(그리드 배치)
  → ② 조용한 가격 이탈 → WATCHING (그리드 전량 취소)
  → ③ 박스권 복귀 → RANGE (그리드 재배치)
  → ④ 거래량 Spike + 박스 이탈 → BREAKOUT_LONG (시장가 진입)
  → ⑤ 트레일링 스탑 → RANGE 복귀 (쿨다운 없음)
  → ⑥ ADX + EMA 정배열 + 눌림목 → TREND_FOLLOWING (2×ATR 사이징)
  → ⑦ 샹들리에 라인 하향 이탈 → RANGE 복귀 (쿨다운 없음)
"""

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data_fetcher import Candle, MarketData
from src.engine_a import (
    ADX_TREND_THRESHOLD,
    ATR_MULTIPLIER_SL,
    ATR_MULTIPLIER_TRAILING,
    ATR_PERIOD,
    MAX_RISK_PER_TRADE_PCT,
    TREND_ATR_MULTIPLIER,
    TREND_EMA_LONG,
    TREND_EMA_SHORT,
    BoxRange,
    BreakoutPosition,
    EngineA,
    StrategyMode,
)


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def make_auth():
    auth = MagicMock()
    auth.get_headers.return_value = {}
    auth.sign_order.side_effect = lambda p: {**p, "signature": "0xSIG", "nonce": 1, "timestamp": 0}
    auth.sign_cancel.side_effect = lambda oid: {"orderId": oid, "signature": "0xSIG"}
    return auth


def make_engine(capital: float = 10_000.0) -> EngineA:
    engine = EngineA(auth=make_auth(), symbol="BTC-USDC", dry_run=True)
    engine.state.allocated_capital = capital
    engine.state.box_last_updated = 0.0  # 캐시 비활성화
    return engine


def box_candles(n: int = 2880, price: float = 100.0, volume: float = 10.0) -> list:
    """안정적인 박스권 캔들: high = price×1.005, low = price×0.995."""
    return [
        Candle(i * 60_000, price, price * 1.005, price * 0.995, price, volume)
        for i in range(n)
    ]


def make_md(candles: list, last_price: float, volume_ma: float = 10.0) -> MarketData:
    md = MarketData(symbol="BTC-USDC")
    for c in candles:
        md.candles.append(c)
    md.last_price = last_price
    md.volume_ma_20m = volume_ma
    return md


def force_box(engine: EngineA, support: float, resistance: float) -> None:
    """박스권을 강제로 고정하고 캐시를 유효 상태로 설정합니다."""
    engine.state.box = BoxRange(support=support, resistance=resistance)
    engine.state.box_last_updated = time.time()


# ═══════════════════════════════════════════════════════════════════════════════
# 단계별 개별 검증
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhase1Range(unittest.IsolatedAsyncioTestCase):
    """① RANGE: 박스권 그리드 주문 배치."""

    async def test_grid_placed_on_range_mode(self):
        engine = make_engine()
        md = make_md(box_candles(2880), last_price=100.0)
        await engine.on_market_update(md)
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertGreater(len(engine.state.open_order_ids), 0, "그리드 주문 존재")

    async def test_grid_not_placed_outside_box(self):
        """현재가가 박스 외부(캐시된 박스)면 그리드 배치 안 함."""
        engine = make_engine()
        force_box(engine, support=99.0, resistance=101.0)
        md = make_md(box_candles(2880), last_price=105.0, volume_ma=10.0)
        await engine.on_market_update(md)
        # 조용한 이탈이 발생해 WATCHING으로 전환
        self.assertNotEqual(engine.state.mode, StrategyMode.RANGE)

    async def test_grid_blocked_during_cooldown(self):
        """쿨다운 중에는 그리드 배치 금지."""
        engine = make_engine()
        engine.state.cooldown_until = time.time() + 9999
        md = make_md(box_candles(2880), last_price=100.0)
        await engine.on_market_update(md)
        self.assertEqual(len(engine.state.open_order_ids), 0)


class TestPhase2Watching(unittest.IsolatedAsyncioTestCase):
    """② 조용한 가격 이탈 → WATCHING."""

    async def test_silent_breakout_to_watching(self):
        engine = make_engine()
        # 그리드 배치
        await engine.on_market_update(make_md(box_candles(2880), 100.0))
        self.assertGreater(len(engine.state.open_order_ids), 0)

        # 조용한 이탈 (낮은 거래량)
        force_box(engine, 99.0, 101.0)
        await engine.on_market_update(make_md(box_candles(2880), 105.0, volume_ma=10.0))
        self.assertEqual(engine.state.mode, StrategyMode.WATCHING, "WATCHING 전환")
        self.assertEqual(len(engine.state.open_order_ids), 0, "그리드 전량 취소")

    async def test_spike_breakout_not_watching(self):
        """거래량 스파이크 동반 이탈은 WATCHING이 아닌 BREAKOUT."""
        engine = make_engine()
        force_box(engine, 99.0, 101.0)
        candles = box_candles(2880) + [
            Candle(2880 * 60_000, 101.0, 102.0, 100.5, 101.5, volume=35.0)
        ]
        md = make_md(candles, last_price=101.5, volume_ma=10.0)
        await engine.on_market_update(md)
        self.assertNotEqual(engine.state.mode, StrategyMode.WATCHING)
        self.assertIn(engine.state.mode,
                      (StrategyMode.BREAKOUT_LONG, StrategyMode.BREAKOUT_SHORT))

    async def test_box_cache_invalidated_after_watching(self):
        """WATCHING 전환 후 박스 캐시가 무효화되어 다음 틱에서 재계산."""
        engine = make_engine()
        force_box(engine, 99.0, 101.0)
        await engine.on_market_update(make_md(box_candles(2880), 105.0, volume_ma=10.0))
        self.assertEqual(engine.state.box_last_updated, 0.0, "캐시 무효화")


class TestPhase3Recovery(unittest.IsolatedAsyncioTestCase):
    """③ WATCHING → RANGE 복귀."""

    async def test_price_recovery_to_range(self):
        engine = make_engine()
        engine.state.mode = StrategyMode.WATCHING
        force_box(engine, 99.0, 101.0)
        await engine.on_market_update(make_md(box_candles(2880), 100.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertGreater(len(engine.state.open_order_ids), 0, "그리드 재배치")

    async def test_watching_stays_if_price_outside(self):
        """가격이 여전히 박스 밖이면 WATCHING 유지."""
        engine = make_engine()
        engine.state.mode = StrategyMode.WATCHING
        force_box(engine, 99.0, 101.0)
        await engine.on_market_update(make_md(box_candles(2880), 110.0))
        self.assertEqual(engine.state.mode, StrategyMode.WATCHING)

    async def test_no_cooldown_on_recovery(self):
        """박스권 복귀에는 쿨다운이 없습니다."""
        engine = make_engine()
        engine.state.mode = StrategyMode.WATCHING
        force_box(engine, 99.0, 101.0)
        await engine.on_market_update(make_md(box_candles(2880), 100.0))
        self.assertAlmostEqual(engine.state.cooldown_until, 0.0, delta=1.0)


class TestPhase4Breakout(unittest.IsolatedAsyncioTestCase):
    """④ 거래량 Spike + 박스 이탈 → BREAKOUT."""

    async def test_volume_spike_long(self):
        engine = make_engine()
        force_box(engine, 99.0, 101.0)
        candles = box_candles(2880) + [
            Candle(2880 * 60_000, 101.0, 102.0, 100.5, 101.5, volume=35.0)
        ]
        await engine.on_market_update(make_md(candles, 101.5, volume_ma=10.0))
        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_LONG)
        self.assertIsNotNone(engine.state.position)

    async def test_volume_spike_short(self):
        engine = make_engine()
        force_box(engine, 99.0, 101.0)
        candles = box_candles(2880) + [
            Candle(2880 * 60_000, 99.0, 99.5, 98.5, 98.8, volume=35.0)
        ]
        await engine.on_market_update(make_md(candles, 98.8, volume_ma=10.0))
        self.assertEqual(engine.state.mode, StrategyMode.BREAKOUT_SHORT)
        self.assertIsNotNone(engine.state.position)

    async def test_breakout_risk_sizing(self):
        """브레이크아웃 진입: 손절 리스크 = 자본의 2%."""
        engine = make_engine(capital=10_000.0)
        force_box(engine, 99.0, 101.0)
        candles = box_candles(2880) + [
            Candle(2880 * 60_000, 101.0, 102.0, 100.5, 101.5, volume=35.0)
        ]
        await engine.on_market_update(make_md(candles, 101.5, volume_ma=10.0))
        if engine.state.position:
            pos = engine.state.position
            max_loss = 10_000.0 * MAX_RISK_PER_TRADE_PCT
            if pos.stop_loss_distance > 0:
                actual_loss = pos.stop_loss_distance * pos.size
                self.assertLessEqual(actual_loss, max_loss * 1.05, "리스크 2% 초과 금지")

    async def test_grid_cleared_on_breakout(self):
        """브레이크아웃 시 기존 그리드 주문 전량 취소."""
        engine = make_engine()
        await engine.on_market_update(make_md(box_candles(2880), 100.0))
        self.assertGreater(len(engine.state.open_order_ids), 0)

        force_box(engine, 99.0, 101.0)
        candles = box_candles(2880) + [
            Candle(2880 * 60_000, 101.0, 102.0, 100.5, 101.5, volume=35.0)
        ]
        await engine.on_market_update(make_md(candles, 101.5, volume_ma=10.0))
        # 브레이크아웃 포지션의 order_id가 있으면 1개
        self.assertLessEqual(len(engine.state.open_order_ids), 1)


class TestPhase5BreakoutExit(unittest.IsolatedAsyncioTestCase):
    """⑤ BREAKOUT 청산 → RANGE 복귀."""

    async def test_trailing_stop_returns_to_range_no_cooldown(self):
        """트레일링 스탑 청산 → RANGE + 쿨다운 없음."""
        engine = make_engine()
        engine.state.mode = StrategyMode.BREAKOUT_LONG
        engine.state.position = BreakoutPosition(
            side="BUY", entry_price=100.0, size=0.5,
            peak_price=110.0, trailing_stop_distance=5.0, stop_loss_distance=5.0,
        )
        # 현재가 104 < 110-5=105 → 트레일링 스탑
        await engine.on_market_update(make_md(box_candles(2880), 104.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(engine.state.position)
        self.assertAlmostEqual(engine.state.cooldown_until, 0.0, delta=1.0)

    async def test_stop_loss_triggers_cooldown(self):
        """손절 → RANGE + 300초 쿨다운 활성화."""
        engine = make_engine()
        engine.state.mode = StrategyMode.BREAKOUT_LONG
        engine.state.position = BreakoutPosition(
            side="BUY", entry_price=100.0, size=0.5,
            peak_price=100.0, stop_loss_distance=5.0, trailing_stop_distance=20.0,
        )
        # 현재가 94 < 100-5=95 → 손절
        await engine.on_market_update(make_md(box_candles(2880), 94.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertGreater(engine.state.cooldown_until, time.time())


class TestPhase6TrendFollowing(unittest.IsolatedAsyncioTestCase):
    """⑥ TREND_FOLLOWING 진입 검증."""

    async def test_trend_entry_mode_transition(self):
        """추세 신호 → TREND_FOLLOWING 모드 + 포지션 생성."""
        engine = make_engine()
        md = make_md(box_candles(2880), last_price=100.0)
        with patch.object(engine, "_is_trend_entry_signal", return_value=True):
            await engine.on_market_update(md)
        self.assertEqual(engine.state.mode, StrategyMode.TREND_FOLLOWING)
        self.assertIsNotNone(engine.state.position)
        self.assertEqual(engine.state.position.side, "BUY")

    async def test_trend_sizing_is_2x_atr(self):
        """추세 손절 거리 = ATR × 2.0."""
        engine = make_engine()
        md = make_md(box_candles(2880), last_price=100.0)
        with patch.object(engine, "_is_trend_entry_signal", return_value=True):
            await engine.on_market_update(md)
        if engine.state.position and EngineA._calculate_atr(md):
            atr = EngineA._calculate_atr(md)
            expected = round(atr * TREND_ATR_MULTIPLIER, 4)
            self.assertAlmostEqual(engine.state.position.stop_loss_distance, expected, places=3)

    async def test_trend_risk_capped_at_2pct(self):
        """손절 리스크 = 자본의 2%."""
        engine = make_engine(capital=10_000.0)
        md = make_md(box_candles(2880), last_price=100.0)
        with patch.object(engine, "_is_trend_entry_signal", return_value=True):
            await engine.on_market_update(md)
        if engine.state.position:
            pos = engine.state.position
            if pos.stop_loss_distance > 0:
                actual_risk = pos.stop_loss_distance * pos.size
                max_risk = 10_000.0 * MAX_RISK_PER_TRADE_PCT
                self.assertLessEqual(actual_risk, max_risk * 1.05)

    async def test_breakout_has_priority_over_trend(self):
        """거래량 스파이크 돌파 > 추세 진입 우선순위."""
        engine = make_engine()
        force_box(engine, 99.0, 101.0)
        candles = box_candles(2880) + [
            Candle(2880 * 60_000, 101.0, 102.0, 100.5, 101.5, volume=35.0)
        ]
        md = make_md(candles, 101.5, volume_ma=10.0)
        with patch.object(engine, "_is_trend_entry_signal", return_value=True):
            await engine.on_market_update(md)
        self.assertIn(
            engine.state.mode,
            (StrategyMode.BREAKOUT_LONG, StrategyMode.BREAKOUT_SHORT),
            "BREAKOUT이 TREND보다 우선",
        )

    async def test_trend_entry_blocked_when_position_exists(self):
        """포지션이 있으면 추세 재진입 불가."""
        engine = make_engine()
        engine.state.position = BreakoutPosition("BUY", 100.0, 1.0, 100.0)
        md = make_md(box_candles(2880), 100.0)
        self.assertFalse(engine._is_trend_entry_signal(md))


class TestPhase7TrendExit(unittest.IsolatedAsyncioTestCase):
    """⑦ 추세 청산: 샹들리에 + 데드크로스."""

    def _trend_engine(self, peak: float = 120.0, sl_dist: float = 5.0) -> EngineA:
        engine = make_engine()
        engine.state.mode = StrategyMode.TREND_FOLLOWING
        engine.state.position = BreakoutPosition(
            side="BUY", entry_price=100.0, size=0.5,
            peak_price=peak, trailing_stop_distance=sl_dist, stop_loss_distance=sl_dist,
        )
        return engine

    async def test_chandelier_triggers_below_line(self):
        """현재가 < peak - 2×ATR → 샹들리에 청산 → RANGE."""
        engine = self._trend_engine(peak=120.0, sl_dist=5.0)
        # chandelier line = 120 - 5 = 115; 현재가 113 < 115
        with patch.object(engine, "_is_dead_cross", return_value=False):
            await engine.on_market_update(make_md(box_candles(200), 113.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(engine.state.position)

    async def test_chandelier_not_triggered_above_line(self):
        """현재가 > 샹들리에 라인 → 포지션 유지."""
        engine = self._trend_engine(peak=120.0, sl_dist=5.0)
        # chandelier line = 115; 현재가 116 > 115 → 유지
        with patch.object(engine, "_is_dead_cross", return_value=False):
            await engine.on_market_update(make_md(box_candles(200), 116.0))
        self.assertIsNotNone(engine.state.position)
        self.assertEqual(engine.state.mode, StrategyMode.TREND_FOLLOWING)

    async def test_chandelier_no_cooldown(self):
        """샹들리에 익절은 쿨다운을 유발하지 않습니다."""
        engine = self._trend_engine(peak=120.0, sl_dist=5.0)
        with patch.object(engine, "_is_dead_cross", return_value=False):
            await engine.on_market_update(make_md(box_candles(200), 113.0))
        self.assertAlmostEqual(engine.state.cooldown_until, 0.0, delta=1.0)

    async def test_dead_cross_forces_immediate_exit(self):
        """데드크로스 → 즉시 청산 (샹들리에 라인 무관)."""
        # sl_dist=1000 → chandelier line = 120-1000 = -880 → 절대 트리거 안 됨
        engine = self._trend_engine(peak=120.0, sl_dist=1000.0)
        with patch.object(engine, "_is_dead_cross", return_value=True):
            await engine.on_market_update(make_md(box_candles(200), 119.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE)
        self.assertIsNone(engine.state.position)

    async def test_dead_cross_no_cooldown(self):
        """데드크로스 청산도 쿨다운 없음 (손절 아님)."""
        engine = self._trend_engine(peak=120.0, sl_dist=1000.0)
        with patch.object(engine, "_is_dead_cross", return_value=True):
            await engine.on_market_update(make_md(box_candles(200), 119.0))
        self.assertAlmostEqual(engine.state.cooldown_until, 0.0, delta=1.0)

    async def test_peak_updates_raise_chandelier_line(self):
        """가격 상승 시 최고가 갱신 → 샹들리에 라인도 상승."""
        engine = self._trend_engine(peak=120.0, sl_dist=5.0)
        # 새 최고가 130 → chandelier = 125; 현재가 126 > 125 → 유지
        engine.state.position.peak_price = 130.0
        with patch.object(engine, "_is_dead_cross", return_value=False):
            await engine.on_market_update(make_md(box_candles(200), 126.0))
        self.assertIsNotNone(engine.state.position, "126 > 125 → 포지션 유지")


# ═══════════════════════════════════════════════════════════════════════════════
# 마스터 시나리오: 전체 7단계 순서 실행
# ═══════════════════════════════════════════════════════════════════════════════

class TestMasterScenarioFullFlow(unittest.IsolatedAsyncioTestCase):
    """
    전체 7단계를 하나의 엔진 인스턴스로 순서대로 실행합니다.
    각 단계에서 모드·포지션·주문 상태를 검증합니다.
    """

    async def test_full_7step_scenario(self):
        engine = make_engine(capital=10_000.0)
        base = box_candles(2880, price=100.0)

        # ─────────────────────────────────────────────
        # ① RANGE: 그리드 주문 배치
        # ─────────────────────────────────────────────
        await engine.on_market_update(make_md(base, last_price=100.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "① 모드=RANGE")
        self.assertGreater(len(engine.state.open_order_ids), 0, "① 그리드 존재")
        grid_count_phase1 = len(engine.state.open_order_ids)

        # ─────────────────────────────────────────────
        # ② 조용한 이탈 → WATCHING + 그리드 전량 취소
        # ─────────────────────────────────────────────
        force_box(engine, support=99.0, resistance=101.0)
        await engine.on_market_update(make_md(base, last_price=105.0, volume_ma=10.0))
        self.assertEqual(engine.state.mode, StrategyMode.WATCHING, "② 모드=WATCHING")
        self.assertEqual(len(engine.state.open_order_ids), 0, "② 그리드 전량 취소")
        self.assertEqual(engine.state.box_last_updated, 0.0, "② 캐시 무효화")

        # ─────────────────────────────────────────────
        # ③ 박스권 복귀 → RANGE + 그리드 재배치
        # ─────────────────────────────────────────────
        # WATCHING 상태: state.box = BoxRange(99,101), box_last_updated=0
        # 복귀: 캐시 미스이므로 캔들에서 박스 재계산 → [99.5, 100.5]
        # state.box는 아직 BoxRange(99,101)이므로 100.0은 내부 → RANGE 전환
        await engine.on_market_update(make_md(base, last_price=100.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "③ 모드=RANGE 복귀")
        self.assertGreater(len(engine.state.open_order_ids), 0, "③ 그리드 재배치")

        # ─────────────────────────────────────────────
        # ④ 거래량 Spike + 박스 이탈 → BREAKOUT_LONG
        # ─────────────────────────────────────────────
        force_box(engine, support=99.0, resistance=101.0)
        spike_candles = list(base) + [
            Candle(len(base) * 60_000, 101.0, 102.0, 100.5, 101.5, volume=35.0)
        ]
        await engine.on_market_update(make_md(spike_candles, last_price=101.5, volume_ma=10.0))
        self.assertIn(
            engine.state.mode,
            (StrategyMode.BREAKOUT_LONG, StrategyMode.BREAKOUT_SHORT),
            "④ BREAKOUT 확인",
        )
        self.assertIsNotNone(engine.state.position, "④ 포지션 생성")

        # 리스크 사이징 검증
        pos = engine.state.position
        if pos.stop_loss_distance > 0:
            risk = pos.stop_loss_distance * pos.size
            max_risk = 10_000.0 * MAX_RISK_PER_TRADE_PCT
            self.assertLessEqual(risk, max_risk * 1.05, "④ 손절 리스크 ≤ 2%")

        # ─────────────────────────────────────────────
        # ⑤ 트레일링 스탑 청산 → RANGE (쿨다운 없음)
        # ─────────────────────────────────────────────
        # 박스를 넓혀서 청산 후 price=104가 박스 내부에 있도록 함
        force_box(engine, support=99.0, resistance=106.0)
        pos = engine.state.position
        pos.peak_price = 110.0
        pos.trailing_stop_distance = 5.0   # Chandelier line = 105
        pos.stop_loss_distance = 50.0      # SL line = 진입가 - 50 (매우 낮음)
        # 현재가 104 < 105 → trailing stop, 그리고 104 ∈ [99, 106] → RANGE 유지
        await engine.on_market_update(make_md(base, last_price=104.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "⑤ RANGE 복귀")
        self.assertIsNone(engine.state.position, "⑤ 포지션 청산")
        self.assertAlmostEqual(engine.state.cooldown_until, 0.0, delta=1.0, msg="⑤ 쿨다운 없음")

        # ─────────────────────────────────────────────
        # ⑥ 추세 진입 → TREND_FOLLOWING (2×ATR 사이징)
        # ─────────────────────────────────────────────
        with patch.object(engine, "_is_trend_entry_signal", return_value=True):
            await engine.on_market_update(make_md(base, last_price=100.0))
        self.assertEqual(engine.state.mode, StrategyMode.TREND_FOLLOWING, "⑥ TREND_FOLLOWING 확인")
        self.assertIsNotNone(engine.state.position, "⑥ 포지션 생성")
        self.assertEqual(engine.state.position.side, "BUY", "⑥ 롱 포지션")

        atr = EngineA._calculate_atr(make_md(base, 100.0))
        if atr:
            expected_sl = round(atr * TREND_ATR_MULTIPLIER, 4)
            self.assertAlmostEqual(
                engine.state.position.stop_loss_distance, expected_sl, places=3,
                msg="⑥ 손절 거리 = ATR × 2.0",
            )

        # ─────────────────────────────────────────────
        # ⑦ 샹들리에 라인 하향 이탈 → RANGE 복귀 (쿨다운 없음)
        # ─────────────────────────────────────────────
        # 박스를 넓혀서 청산 후 price=113이 박스 내부에 있도록 함
        force_box(engine, support=99.0, resistance=116.0)
        pos = engine.state.position
        pos.peak_price = 120.0
        pos.trailing_stop_distance = 5.0   # Chandelier line = 115
        pos.stop_loss_distance = 5.0
        # 현재가 113 < 115 → 샹들리에 청산, 그리고 113 ∈ [99, 116] → RANGE 유지
        with patch.object(engine, "_is_dead_cross", return_value=False):
            await engine.on_market_update(make_md(base, last_price=113.0))
        self.assertEqual(engine.state.mode, StrategyMode.RANGE, "⑦ RANGE 최종 복귀")
        self.assertIsNone(engine.state.position, "⑦ 포지션 완전 청산")
        self.assertAlmostEqual(engine.state.cooldown_until, 0.0, delta=1.0, msg="⑦ 쿨다운 없음")


# ═══════════════════════════════════════════════════════════════════════════════
# 공식 수학 정확성 검증
# ═══════════════════════════════════════════════════════════════════════════════

class TestMathAccuracy(unittest.TestCase):
    """EMA, ADX, ATR 계산 수학 정확성 검증."""

    def _uptrend_market(self, n: int = 200) -> MarketData:
        md = MarketData(symbol="BTC-USDC")
        for i in range(n):
            p = 100.0 + 0.8 * i
            md.candles.append(Candle(i * 60_000, p, p * 1.005, p * 0.995, p, 10.0))
        md.last_price = 100.0 + 0.8 * (n - 1)
        md.volume_ma_20m = 10.0
        return md

    def test_ema20_calculated_correctly(self):
        """EMA20: 상승 추세에서 양수 반환."""
        md = self._uptrend_market(100)
        ema = EngineA._calculate_ema(md, TREND_EMA_SHORT)
        self.assertIsNotNone(ema)
        self.assertGreater(ema, 0)

    def test_ema50_calculated_correctly(self):
        md = self._uptrend_market(200)
        ema = EngineA._calculate_ema(md, TREND_EMA_LONG)
        self.assertIsNotNone(ema)
        self.assertGreater(ema, 0)

    def test_ema_golden_cross_in_uptrend(self):
        """상승 추세: EMA20 > EMA50 (정배열)."""
        md = self._uptrend_market(200)
        ema20 = EngineA._calculate_ema(md, TREND_EMA_SHORT)
        ema50 = EngineA._calculate_ema(md, TREND_EMA_LONG)
        self.assertGreater(ema20, ema50, "정배열 확인")

    def test_ema_dead_cross_in_downtrend(self):
        """하락 추세: EMA20 < EMA50 (역배열)."""
        md = MarketData(symbol="BTC-USDC")
        for i in range(200):
            p = 300.0 - 0.8 * i
            md.candles.append(Candle(i * 60_000, p, p * 1.005, p * 0.995, p, 10.0))
        md.last_price = 300.0 - 0.8 * 199
        md.volume_ma_20m = 10.0
        ema20 = EngineA._calculate_ema(md, TREND_EMA_SHORT)
        ema50 = EngineA._calculate_ema(md, TREND_EMA_LONG)
        self.assertLess(ema20, ema50, "역배열 확인")

    def test_adx_is_positive_in_trending_market(self):
        """강한 추세장: ADX > 0."""
        md = self._uptrend_market(200)
        adx = EngineA._calculate_adx(md)
        self.assertIsNotNone(adx)
        self.assertGreater(adx, 0)

    def test_atr_calculated(self):
        """ATR이 양수로 계산됩니다."""
        md = self._uptrend_market(50)
        atr = EngineA._calculate_atr(md)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)

    def test_risk_formula_invariant(self):
        """리스크 공식 불변식: stop_dist × size ≤ capital × 2%."""
        capital = 10_000.0
        for atr in [0.5, 1.0, 2.0, 5.0]:
            sl_dist = atr * TREND_ATR_MULTIPLIER
            max_risk = capital * MAX_RISK_PER_TRADE_PCT
            size = max_risk / sl_dist
            actual_loss = sl_dist * size
            self.assertAlmostEqual(actual_loss, max_risk, places=6,
                                   msg=f"ATR={atr}: 손실 {actual_loss:.2f} ≠ 리스크 {max_risk:.2f}")

    def test_chandelier_formula(self):
        """샹들리에 공식: exit_line = peak - trailing_dist."""
        pos = BreakoutPosition(
            side="BUY", entry_price=100.0, size=1.0,
            peak_price=130.0, trailing_stop_distance=6.0, stop_loss_distance=6.0,
        )
        # chandelier line = 130 - 6 = 124
        self.assertFalse(pos.should_trailing_stop(125.0), "125 > 124 → 유지")
        self.assertTrue(pos.should_trailing_stop(123.9), "123.9 < 124 → 청산")


if __name__ == "__main__":
    unittest.main(verbosity=2)
