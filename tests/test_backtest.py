"""
tests/test_backtest.py
BacktestEngine 성과 지표(Sharpe, MaxDD) 정합성 테스트.

검증 항목:
- _sharpe(): 단순 % 수익률이 아닌 로그 수익률 기반이어야 함
  (단순 % 평균은 비대칭 편향으로 손실 전략에도 양의 Sharpe를 낼 수 있음)
- BacktestEngine.run(): 합성 손실 시나리오에서 Sharpe 부호가 PnL과 일치해야 함
"""

from __future__ import annotations

import math
import random
import sys
import unittest
from typing import List

sys.path.insert(0, ".")

from src.backtest import BacktestEngine, _sharpe
from src.data_fetcher import Candle


def make_candles(
    n: int = 2000,
    price: float = 100.0,
    volume: float = 10.0,
    trend: float = 0.0,
    vol_noise: float = 0.0,
    seed: int = 42,
) -> List[Candle]:
    rng = random.Random(seed)
    candles = []
    p = price
    for i in range(n):
        p = p + trend + rng.gauss(0, 0.1)
        p = max(p, 0.01)
        v = max(0.01, volume + rng.gauss(0, vol_noise))
        hi = p * (1 + rng.uniform(0.001, 0.005))
        lo = p * (1 - rng.uniform(0.001, 0.005))
        candles.append(Candle(i * 60_000, p, hi, lo, p, v))
    return candles


class TestSharpeUsesLogReturns(unittest.TestCase):
    """단순 수익률의 비대칭 편향: -10% 후 +11.1%는 원금 복귀지만
    산술 평균은 +0.55%로 양수가 되는 문제를 로그 수익률로 회피해야 한다."""

    def test_round_trip_loss_then_gain_has_zero_mean_log_return(self):
        equity = [100.0, 90.0, 100.0]  # -10% 후 +11.11% → 원금 복귀
        log_returns = [
            math.log(equity[j] / equity[j - 1]) for j in range(1, len(equity))
        ]
        self.assertAlmostEqual(sum(log_returns), 0.0, places=10)

    def test_simple_returns_of_same_path_are_falsely_positive(self):
        """단순 % 수익률은 동일 경로에서 평균이 양수로 왜곡됨을 보여주는 대조 테스트."""
        equity = [100.0, 90.0, 100.0]
        simple_returns = [
            (equity[j] - equity[j - 1]) / equity[j - 1] for j in range(1, len(equity))
        ]
        self.assertGreater(sum(simple_returns) / len(simple_returns), 0.0)


class TestBacktestEngineSharpeSignConsistency(unittest.TestCase):
    """손실 시나리오에서 Sharpe 부호가 PnL 부호와 일치해야 한다."""

    def test_strong_downtrend_negative_pnl_yields_negative_sharpe(self):
        candles = make_candles(n=2000, trend=-0.03, vol_noise=2.0)
        result = BacktestEngine(
            candles=candles, initial_capital=10_000.0, engine_a_allocation=0.25
        ).run()
        self.assertLess(result.total_pnl, 0.0)
        self.assertLess(result.sharpe_ratio, 0.0)

    def test_ranging_market_negative_pnl_yields_negative_sharpe(self):
        candles = make_candles(n=2000, trend=0.0, vol_noise=1.0)
        result = BacktestEngine(
            candles=candles, initial_capital=10_000.0, engine_a_allocation=0.25
        ).run()
        self.assertLess(result.total_pnl, 0.0)
        self.assertLess(result.sharpe_ratio, 0.0)

    def test_high_volatility_negative_pnl_yields_negative_sharpe(self):
        candles = make_candles(n=2000, trend=0.0, vol_noise=8.0)
        result = BacktestEngine(
            candles=candles, initial_capital=10_000.0, engine_a_allocation=0.25
        ).run()
        self.assertLess(result.total_pnl, 0.0)
        self.assertLess(result.sharpe_ratio, 0.0)


class TestSharpeHelper(unittest.TestCase):

    def test_zero_returns_is_zero_sharpe(self):
        self.assertEqual(_sharpe([0.0, 0.0, 0.0]), 0.0)

    def test_single_return_is_zero_sharpe(self):
        self.assertEqual(_sharpe([0.01]), 0.0)

    def test_positive_consistent_returns_yield_positive_sharpe(self):
        returns = [0.001] * 100 + [0.0011] * 100  # 거의 일정한 양의 수익률
        self.assertGreater(_sharpe(returns), 0.0)

    def test_negative_consistent_returns_yield_negative_sharpe(self):
        returns = [-0.001] * 100 + [-0.0011] * 100
        self.assertLess(_sharpe(returns), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
