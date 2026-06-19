"""tests/test_engine_b.py — Engine B 헤지 전략 유닛 테스트.

커버 범위:
  - 펀딩비 수령액 계산 (notional 기반)
  - 72h APR 이력 추적
  - EXIT 조건 (APR 하락 / 미실현 손실)
  - 동시 청산 (exit_hedge_async)
  - 미실현 PnL 계산
"""

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.data_fetcher import FundingInfo, MarketData
from src.engine_b import (
    EXIT_LOSS_THRESHOLD,
    EXIT_MIN_APR,
    EngineB,
    HedgePosition,
)


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def make_auth():
    auth = MagicMock()
    auth.get_headers.return_value = {}
    auth.sign_order.side_effect = lambda p: {**p, "signature": "0xSIG"}
    return auth


def make_engine(dry_run=True) -> EngineB:
    engine = EngineB(auth=make_auth(), dry_run=dry_run)
    engine.allocated_capital = 10_000.0
    engine.symbol = "BTC-USDC"
    return engine


def make_market(price: float = 100.0) -> MarketData:
    md = MarketData(symbol="BTC-USDC")
    md.last_price = price
    return md


def make_funding(rate: float = 0.001, apr: float = 10.0, next_ms: int = 0) -> FundingInfo:
    """next_funding_time=0이면 현재 시각 기준 항상 체결."""
    if next_ms == 0:
        next_ms = int(time.time() * 1000) - 1  # 이미 지났음
    return FundingInfo(
        symbol="BTC-USDC",
        funding_rate=rate,
        funding_apr=apr,
        next_funding_time=next_ms,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 펀딩비 수령액 계산
# ═══════════════════════════════════════════════════════════════════════════════

class TestFundingFeeCalculation(unittest.TestCase):

    def test_notional_value_used(self):
        """receipt = funding_rate * size * price."""
        engine = make_engine()
        engine.position.edgex_short_size = 1.0
        engine.position.edgex_short_entry = 50_000.0

        funding = make_funding(rate=0.001, apr=10.0)
        current_price = 50_000.0
        engine.record_funding(funding, current_price)

        expected_receipt = 0.001 * 1.0 * 50_000.0  # = 50.0
        self.assertAlmostEqual(engine.position.total_funding_received, expected_receipt, places=4)

    def test_zero_price_prevents_receipt(self):
        """current_price=0이면 receipt=0."""
        engine = make_engine()
        engine.position.edgex_short_size = 1.0
        engine.position.edgex_short_entry = 50_000.0

        funding = make_funding(rate=0.001, apr=10.0)
        engine.record_funding(funding, current_price=0.0)

        self.assertAlmostEqual(engine.position.total_funding_received, 0.0, places=8)

    def test_funding_history_appended(self):
        """record_funding 호출 시 _funding_rate_history에 기록."""
        engine = make_engine()
        engine.position.edgex_short_size = 1.0

        self.assertEqual(len(engine._funding_rate_history), 0)

        funding = make_funding(rate=0.001, apr=10.0)
        engine.record_funding(funding, current_price=50_000.0)

        self.assertEqual(len(engine._funding_rate_history), 1)
        ts, apr = engine._funding_rate_history[0]
        self.assertAlmostEqual(apr, 10.0, places=1)

    def test_funding_history_multiple_calls(self):
        """여러 번 호출 시 이력이 누적된다."""
        engine = make_engine()
        engine.position.edgex_short_size = 1.0

        for i in range(5):
            funding = make_funding(rate=0.001, apr=float(i + 1))
            engine.record_funding(funding, current_price=50_000.0)

        self.assertEqual(len(engine._funding_rate_history), 5)


# ═══════════════════════════════════════════════════════════════════════════════
# EXIT 조건 (APR 하락 / 미실현 손실)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitConditions(unittest.IsolatedAsyncioTestCase):

    def _setup_position(self, engine: EngineB, size=1.0, entry=50_000.0):
        engine.position.edgex_short_size = size
        engine.position.edgex_short_entry = entry
        engine.position.binance_long_size = size
        engine.position.binance_long_entry = entry
        engine.position.symbol = "BTC-USDC"

    async def test_exit_on_low_apr(self):
        """72h 평균 APR < 3% → exit_hedge_async 호출."""
        engine = make_engine()
        self._setup_position(engine)

        # 72h 이내 이력 4개 추가 (APR < EXIT_MIN_APR)
        now = time.time()
        for i in range(4):
            engine._funding_rate_history.append((now - i * 3600, 2.0))  # 2% < 3%

        market = make_market(price=50_000.0)
        # exit_hedge_async를 mock으로 대체
        engine.exit_hedge_async = AsyncMock()
        await engine._check_exit_conditions(market)
        engine.exit_hedge_async.assert_called_once()

    async def test_no_exit_when_apr_sufficient(self):
        """APR >= 3% → 청산 안 함."""
        engine = make_engine()
        self._setup_position(engine)

        now = time.time()
        for i in range(4):
            engine._funding_rate_history.append((now - i * 3600, 10.0))  # 10% > 3%

        market = make_market(price=50_000.0)
        engine.exit_hedge_async = AsyncMock()
        await engine._check_exit_conditions(market)
        engine.exit_hedge_async.assert_not_called()

    async def test_exit_on_loss_threshold(self):
        """미실현 손실 > 2% → exit_hedge_async 호출.

        헤지 포지션에서 순손실은 총 펀딩 수령액이 음수(손실)일 때 발생합니다.
        short_pnl + long_pnl은 완전 헤지일 때 상쇄되므로,
        funding 손실이나 불균형 사이징으로 순 손실을 시뮬레이션합니다.
        """
        engine = make_engine()
        entry = 50_000.0
        # 숏 사이즈 > 롱 사이즈로 불균형 헤지 시뮬레이션
        # short_pnl = (50000 - 52000) * 1.0 = -2000
        # long_pnl = (52000 - 50000) * 0.9 = +1800
        # net = -2000 + 1800 + 0 = -200 → -200 < -50000 * 0.02 = -1000? NO
        # 더 큰 불균형이 필요. funding을 음수로 설정
        engine.position.edgex_short_size = 1.0
        engine.position.edgex_short_entry = entry
        engine.position.binance_long_size = 1.0
        engine.position.binance_long_entry = entry
        engine.position.symbol = "BTC-USDC"
        # 음수 펀딩 (손실 시뮬레이션)
        # cost_basis = 50000, threshold = 50000 * 0.02 = 1000
        # upnl = short_pnl + long_pnl + funding = 0 + 0 + (-2000) = -2000
        # -2000 < -1000 → EXIT 조건 충족
        engine.position.total_funding_received = -2000.0

        market = make_market(price=entry)  # 가격 변화 없음

        engine.exit_hedge_async = AsyncMock()
        # APR 이력 없음 (avg_apr=None → 조건1 스킵)
        await engine._check_exit_conditions(market)
        engine.exit_hedge_async.assert_called_once()

    async def test_no_exit_when_no_position(self):
        """포지션 없으면 체크 스킵."""
        engine = make_engine()
        # edgex_short_size = 0 (기본값)

        market = make_market(price=50_000.0)
        engine.exit_hedge_async = AsyncMock()
        await engine._check_exit_conditions(market)
        engine.exit_hedge_async.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 동시 청산
# ═══════════════════════════════════════════════════════════════════════════════

class TestSimultaneousExit(unittest.IsolatedAsyncioTestCase):

    async def test_exit_calls_both_exchanges(self):
        """exit_hedge_async가 edgeX + Binance 둘 다 청산 호출."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 1.0
        engine.position.edgex_short_entry = 50_000.0
        engine.position.binance_long_size = 1.0
        engine.position.binance_long_entry = 50_000.0
        engine.position.symbol = "BTC-USDC"

        close_edgex_calls = []
        close_binance_calls = []

        original_close_edgex = engine._close_edgex_short_async
        original_close_binance = engine._close_binance_long_async

        async def mock_close_edgex(*args, **kwargs):
            close_edgex_calls.append(args)
            return "DRY-CLOSE-EDGEX"

        async def mock_close_binance(*args, **kwargs):
            close_binance_calls.append(args)
            return {"orderId": "DRY-CLOSE-BINANCE"}

        engine._close_edgex_short_async = mock_close_edgex
        engine._close_binance_long_async = mock_close_binance

        market = make_market(price=50_000.0)
        await engine.exit_hedge_async(market)

        self.assertEqual(len(close_edgex_calls), 1)
        self.assertEqual(len(close_binance_calls), 1)

    async def test_exit_resets_position(self):
        """청산 후 HedgePosition이 초기화됨."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 1.0
        engine.position.edgex_short_entry = 50_000.0
        engine.position.binance_long_size = 1.0
        engine.position.binance_long_entry = 50_000.0
        engine.position.symbol = "BTC-USDC"

        market = make_market(price=50_000.0)
        await engine.exit_hedge_async(market)

        self.assertEqual(engine.position.edgex_short_size, 0.0)
        self.assertEqual(engine.position.binance_long_size, 0.0)
        self.assertEqual(engine.position.total_funding_received, 0.0)

    async def test_dry_run_exit_returns_dry_ids(self):
        """dry_run 모드에서 청산 시 DRY ID 반환."""
        engine = make_engine(dry_run=True)
        result = await engine._close_edgex_short_async("BTC-USDC", 50_000.0, 1.0)
        self.assertEqual(result, "DRY-CLOSE-EDGEX")

        result2 = await engine._close_binance_long_async("BTCUSDT", 1.0)
        self.assertIsNotNone(result2)
        self.assertIn("DRY-CLOSE-BINANCE", str(result2.get("orderId", "")))


# ═══════════════════════════════════════════════════════════════════════════════
# 미실현 PnL 계산
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnrealizedPnl(unittest.TestCase):

    def _setup_engine(self, size=1.0, short_entry=100.0, long_entry=100.0, funding=0.0):
        engine = make_engine()
        engine.position.edgex_short_size = size
        engine.position.edgex_short_entry = short_entry
        engine.position.binance_long_size = size
        engine.position.binance_long_entry = long_entry
        engine.position.total_funding_received = funding
        return engine

    def test_neutral_position_zero_pnl(self):
        """같은 진입가/현재가면 PnL = 0 + 수령 펀딩비."""
        engine = self._setup_engine(size=1.0, short_entry=100.0, long_entry=100.0, funding=5.0)
        pnl = engine._calculate_unrealized_pnl(current_price=100.0)
        # short_pnl = (100 - 100) * 1 = 0
        # long_pnl = (100 - 100) * 1 = 0
        # total = 0 + 0 + 5.0 = 5.0
        self.assertAlmostEqual(pnl, 5.0, places=6)

    def test_short_profit_when_price_falls(self):
        """가격 하락 시 숏 수익 + 롱 손실 상쇄 검증."""
        engine = self._setup_engine(size=1.0, short_entry=100.0, long_entry=100.0, funding=0.0)
        # 가격 90으로 하락
        # short_pnl = (100 - 90) * 1 = 10
        # long_pnl = (90 - 100) * 1 = -10
        # total = 10 + (-10) + 0 = 0
        pnl = engine._calculate_unrealized_pnl(current_price=90.0)
        self.assertAlmostEqual(pnl, 0.0, places=6)

    def test_no_position_zero_pnl(self):
        """포지션 없으면 PnL=0."""
        engine = make_engine()
        # edgex_short_size = 0
        pnl = engine._calculate_unrealized_pnl(current_price=100.0)
        self.assertEqual(pnl, 0.0)

    def test_price_rise_creates_short_loss(self):
        """가격 상승 시 숏 손실 (헤지 불완전할 경우)."""
        engine = self._setup_engine(size=1.0, short_entry=100.0, long_entry=100.0, funding=0.0)
        # 가격 110으로 상승
        # short_pnl = (100 - 110) * 1 = -10
        # long_pnl = (110 - 100) * 1 = 10
        # total = -10 + 10 + 0 = 0
        pnl = engine._calculate_unrealized_pnl(current_price=110.0)
        self.assertAlmostEqual(pnl, 0.0, places=6)


# ═══════════════════════════════════════════════════════════════════════════════
# 72h APR 이력 추적
# ═══════════════════════════════════════════════════════════════════════════════

class Test72hAprTracking(unittest.TestCase):

    def test_returns_none_when_fewer_than_3(self):
        """3개 미만이면 None 반환."""
        engine = make_engine()
        now = time.time()
        engine._funding_rate_history = [(now, 10.0), (now - 3600, 10.0)]
        result = engine._calculate_72h_avg_apr()
        self.assertIsNone(result)

    def test_returns_average_when_enough_data(self):
        """3개 이상이면 평균 반환."""
        engine = make_engine()
        now = time.time()
        engine._funding_rate_history = [
            (now - 3600, 10.0),
            (now - 7200, 20.0),
            (now - 10800, 30.0),
        ]
        result = engine._calculate_72h_avg_apr()
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 20.0, places=4)

    def test_filters_out_old_entries(self):
        """72h 초과 항목은 제외."""
        engine = make_engine()
        now = time.time()
        engine._funding_rate_history = [
            (now - 3600, 10.0),      # 최근
            (now - 7200, 10.0),      # 최근
            (now - 10800, 10.0),     # 최근 (3h)
            (now - 80 * 3600, 100.0),  # 오래됨 (80h)
        ]
        result = engine._calculate_72h_avg_apr()
        # 오래된 항목(100.0) 제외, 3개 (10.0) 평균 = 10.0
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 10.0, places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# HedgePosition 새 필드 검증
# ═══════════════════════════════════════════════════════════════════════════════

class TestHedgePositionFields(unittest.TestCase):

    def test_entry_basis_default_zero(self):
        pos = HedgePosition()
        self.assertEqual(pos.entry_basis, 0.0)

    def test_entry_time_default_zero(self):
        pos = HedgePosition()
        self.assertEqual(pos.entry_time, 0.0)

    async def _enter_hedge(self, engine):
        await engine.enter_hedge_async("BTC-USDC", 50_000.0, 0.1)

    def test_entry_time_set_after_enter(self):
        """enter_hedge_async 후 entry_time이 설정됨."""
        engine = make_engine(dry_run=True)
        before = time.time()
        asyncio.run(engine.enter_hedge_async("BTC-USDC", 50_000.0, 0.1))
        after = time.time()
        self.assertGreaterEqual(engine.position.entry_time, before)
        self.assertLessEqual(engine.position.entry_time, after)

    def test_entry_basis_set_after_enter(self):
        """enter_hedge_async 후 entry_basis가 설정됨 (양쪽 모두 동일 가격으로 진입하므로 0)."""
        engine = make_engine(dry_run=True)
        asyncio.run(engine.enter_hedge_async("BTC-USDC", 50_000.0, 0.1))
        self.assertEqual(engine.position.entry_basis, 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# dry-run 모드 binance_long_entry 회귀 테스트
# (이전 버그: _open_binance_long_async가 dry-run에서 price="0"을 반환해
#  binance_long_entry가 항상 0이 되어 PnL/entry_basis 계산이 완전히 깨짐)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDryRunBinanceEntryPrice(unittest.TestCase):

    def test_binance_long_entry_uses_actual_price_not_zero(self):
        """dry-run에서도 binance_long_entry가 실제 진입가로 설정되어야 한다."""
        engine = make_engine(dry_run=True)
        asyncio.run(engine.enter_hedge_async("BTC-USDC", 50_000.0, 0.1))
        self.assertAlmostEqual(engine.position.binance_long_entry, 50_000.0)

    def test_unrealized_pnl_is_near_zero_when_price_unchanged(self):
        """진입가와 동일한 가격에서는 헤지 PnL이 0에 가까워야 한다 (델타 뉴트럴)."""
        engine = make_engine(dry_run=True)
        asyncio.run(engine.enter_hedge_async("BTC-USDC", 50_000.0, 0.1))
        upnl = engine._calculate_unrealized_pnl(50_000.0)
        self.assertAlmostEqual(upnl, 0.0)

    def test_unrealized_pnl_cancels_out_after_price_move(self):
        """가격이 변동해도 숏+롱 합산 PnL은 0에 가까워야 한다 (방향성 리스크 없음)."""
        engine = make_engine(dry_run=True)
        asyncio.run(engine.enter_hedge_async("BTC-USDC", 50_000.0, 0.1))
        upnl = engine._calculate_unrealized_pnl(48_000.0)  # 4% 하락
        self.assertAlmostEqual(upnl, 0.0, places=6)

    def test_rebalance_passes_market_price_to_binance_open(self):
        """rebalance()가 _open_binance_long_async에 실제 시세를 전달해야 한다."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 1.0
        engine.position.binance_long_size = 0.8  # 델타 불일치 → 리밸런스 필요
        engine.position.last_rebalance_time = 0.0
        market = make_market(price=55_000.0)
        with patch.object(
            engine, "_open_binance_long_async", new=AsyncMock(return_value=None)
        ) as mock_open:
            asyncio.run(engine.rebalance(market))
        mock_open.assert_called_once()
        args, kwargs = mock_open.call_args
        called_price = args[2] if len(args) > 2 else kwargs.get("price")
        self.assertAlmostEqual(called_price, 55_000.0)


class TestRebalanceUpdatesPositionSize(unittest.TestCase):
    """rebalance()가 주문 체결 후 position 사이즈를 갱신해야 한다.
    갱신하지 않으면 델타 불일치가 영원히 해소되지 않아 쿨다운(300초)마다
    동일한 보정 주문이 무한 반복된다."""

    def test_binance_size_increases_after_rebalance(self):
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 0.125
        engine.position.binance_long_size = 0.0625
        engine.position.last_rebalance_time = 0.0
        market = make_market(price=60_000.0)
        asyncio.run(engine.rebalance(market))
        self.assertAlmostEqual(engine.position.binance_long_size, 0.125)
        self.assertTrue(engine.is_delta_neutral())

    def test_edgex_size_increases_after_rebalance(self):
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 0.0625
        engine.position.binance_long_size = 0.125
        engine.position.last_rebalance_time = 0.0
        market = make_market(price=60_000.0)
        asyncio.run(engine.rebalance(market))
        self.assertAlmostEqual(engine.position.edgex_short_size, 0.125)
        self.assertTrue(engine.is_delta_neutral())

    def test_no_repeated_rebalance_once_neutral(self):
        """리밸런스 후 델타가 0에 가까워지면 더 이상 보정 주문을 내지 않아야 한다."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 0.125
        engine.position.binance_long_size = 0.0625
        engine.position.last_rebalance_time = 0.0
        market = make_market(price=60_000.0)
        asyncio.run(engine.rebalance(market))

        call_count = 0
        orig = engine._open_binance_long_async

        async def counting(*a, **kw):
            nonlocal call_count
            call_count += 1
            return await orig(*a, **kw)

        engine._open_binance_long_async = counting
        engine.position.last_rebalance_time = 0.0
        asyncio.run(engine.rebalance(market))
        self.assertEqual(call_count, 0)

    def test_position_unchanged_when_order_fails(self):
        """주문이 실패(None 반환)하면 position 사이즈를 갱신하지 않아야 한다."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 0.125
        engine.position.binance_long_size = 0.0625
        engine.position.last_rebalance_time = 0.0
        market = make_market(price=60_000.0)
        with patch.object(
            engine, "_open_binance_long_async", new=AsyncMock(return_value=None)
        ):
            asyncio.run(engine.rebalance(market))
        self.assertAlmostEqual(engine.position.binance_long_size, 0.0625)


# ═══════════════════════════════════════════════════════════════════════════════
# on_market_update 통합 검증
# ═══════════════════════════════════════════════════════════════════════════════

class TestOnMarketUpdate(unittest.IsolatedAsyncioTestCase):

    async def test_funding_recorded_on_market_update(self):
        """on_market_update에서 펀딩 정보 기록."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 1.0
        engine.position.edgex_short_entry = 50_000.0

        market = make_market(price=50_000.0)
        market.funding = make_funding(rate=0.001, apr=10.0)

        await engine.on_market_update(market)

        self.assertGreater(engine.position.total_funding_received, 0)

    async def test_no_funding_recorded_when_none(self):
        """market.funding=None이면 펀딩 기록 없음."""
        engine = make_engine(dry_run=True)
        engine.position.edgex_short_size = 1.0

        market = make_market(price=50_000.0)
        market.funding = None

        await engine.on_market_update(market)
        self.assertEqual(engine.position.total_funding_received, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
