"""
tests/test_optimizer.py
유전 알고리즘 최적화 시스템 단위 테스트 및 통합 검증.

- Genome: 범위 클램핑, 랜덤 생성, .env 직렬화
- GA 연산자: 교차(crossover), 돌연변이(mutation), 토너먼트 선택
- 적합도 함수: 합성 캔들로 정상 실행 검증
- 표류 감지(Drift detection): 통계적으로 다른 시장 감지
- Optimizer.run_cycle(): 세대 진행에 따른 fitness 개선 검증
- Optimizer.apply_to_env(): .env 파일 쓰기/갱신
- Optimizer.apply_to_config(): config/strategy_config.json 쓰기/갱신
- Optimizer.load_from_config(): JSON 파라미터 복원
- Optimizer.should_retrain(): 일정/표류 기반 재학습 트리거
- 통합: 드리프트 캔들 → Drift 감지 → GA → fitness 수렴 전체 파이프라인
"""

from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from typing import List
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from src.optimizer import (
    DRIFT_Z_THRESHOLD,
    GENE_BOUNDS,
    GA_MUTATION_RATE,
    Genome,
    MarketProfile,
    Optimizer,
    detect_drift,
    evaluate_fitness,
    mutate,
    tournament_select,
    uniform_crossover,
)
import json

try:
    from src.backtest import Candle
except ImportError:
    from dataclasses import dataclass

    @dataclass
    class Candle:
        open_time: int
        open: float
        high: float
        low: float
        close: float
        volume: float


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_candles(
    n: int = 300,
    price: float = 100.0,
    volume: float = 10.0,
    trend: float = 0.0,
    vol_noise: float = 0.0,
) -> List[Candle]:
    """Synthetic OHLCV candles for fast testing."""
    rng = random.Random(42)
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


def mock_fitness_fn(genome: Genome, candles, capital=10_000.0) -> float:
    """빠른 가짜 fitness: box_lookback이 클수록 높은 점수."""
    return float(genome.box_lookback) / 200.0


# ═══════════════════════════════════════════════════════════════════════════════
# Genome 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenome(unittest.TestCase):

    def test_default_genome_within_bounds(self):
        """기본값 Genome이 모든 유전자 범위 내에 있어야 한다."""
        g = Genome()
        for name, (lo, hi, _) in GENE_BOUNDS.items():
            val = getattr(g, name)
            self.assertGreaterEqual(val, lo, f"{name} < lo")
            self.assertLessEqual(val, hi, f"{name} > hi")

    def test_random_genome_within_bounds(self):
        """랜덤 생성 Genome이 항상 범위 내에 있어야 한다."""
        rng = random.Random(0)
        for _ in range(50):
            g = Genome.random(rng)
            for name, (lo, hi, _) in GENE_BOUNDS.items():
                val = getattr(g, name)
                self.assertGreaterEqual(val, lo, f"{name} < lo")
                self.assertLessEqual(val, hi, f"{name} > hi")

    def test_integer_genes_are_integers(self):
        """is_integer=True 유전자는 int 타입이어야 한다."""
        rng = random.Random(1)
        for _ in range(20):
            g = Genome.random(rng)
            for name, (_, _, is_int) in GENE_BOUNDS.items():
                if is_int:
                    self.assertIsInstance(
                        getattr(g, name), int, f"{name} should be int"
                    )

    def test_clamp_restores_bounds(self):
        """범위를 벗어난 유전자가 clamp()로 경계 내로 복원되어야 한다."""
        g = Genome()
        g.box_lookback = 9999   # 위 범위 초과
        g.trailing_stop_pct = -1.0  # 아래 범위 초과
        g.clamp()
        self.assertEqual(g.box_lookback, 200)        # hi = 200
        self.assertAlmostEqual(g.trailing_stop_pct, 0.005)  # lo = 0.005

    def test_to_env_dict_keys(self):
        """to_env_dict()가 필요한 환경변수 키를 모두 반환해야 한다."""
        d = Genome().to_env_dict()
        expected = {
            "VOLUME_SPIKE_MULTIPLIER", "BOX_LOOKBACK",
            "TRAILING_STOP_PCT", "STOP_LOSS_PCT",
            "ADX_TREND_THRESHOLD", "TREND_ATR_MULTIPLIER",
            "EXIT_MIN_APR", "EXIT_LOSS_THRESHOLD",
        }
        self.assertEqual(set(d.keys()), expected)

    def test_to_env_dict_values_are_strings(self):
        """to_env_dict() 값이 모두 str 타입이어야 한다."""
        for v in Genome().to_env_dict().values():
            self.assertIsInstance(v, str)

    def test_fitness_not_a_gene(self):
        """fitness 필드는 GENE_BOUNDS에 포함되지 않아야 한다."""
        self.assertNotIn("fitness", GENE_BOUNDS)

    def test_summary_contains_fitness(self):
        """summary() 출력에 fitness 값이 포함되어야 한다."""
        g = Genome(fitness=1.234)
        s = g.summary()
        self.assertIn("1.234", s)
        self.assertIn("fitness", s.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# GA 연산자 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestGAOperators(unittest.TestCase):

    def setUp(self):
        self.rng = random.Random(7)

    def _pop(self, n=10):
        pop = [Genome.random(self.rng) for _ in range(n)]
        for i, g in enumerate(pop):
            g.fitness = float(i)  # 마지막이 가장 높음
        return pop

    def test_tournament_selects_best(self):
        """토너먼트 선택은 표본 중 가장 높은 fitness를 반환해야 한다."""
        pop = self._pop(20)
        rng = random.Random(99)
        # 많은 시도에서 최상위 개체가 종종 선택되어야 함
        selected = [tournament_select(pop, k=5, rng=rng) for _ in range(50)]
        max_fitness = max(g.fitness for g in selected)
        self.assertGreater(max_fitness, 10.0)  # pop은 0..19 범위

    def test_crossover_genes_from_parents(self):
        """교차 후 offspring의 유전자는 반드시 두 부모 중 하나에서 왔어야 한다."""
        rng = random.Random(5)
        p1 = Genome.random(rng)
        p2 = Genome.random(rng)
        c1, c2 = uniform_crossover(p1, p2, rng)
        for name in GENE_BOUNDS:
            v1 = getattr(p1, name)
            v2 = getattr(p2, name)
            vc1 = getattr(c1, name)
            vc2 = getattr(c2, name)
            # child의 각 유전자는 부모 중 하나와 같아야 함
            self.assertIn(vc1, (v1, v2), f"{name}: c1={vc1} not in ({v1},{v2})")
            self.assertIn(vc2, (v1, v2), f"{name}: c2={vc2} not in ({v1},{v2})")

    def test_crossover_resets_fitness(self):
        """교차로 생성된 offspring의 fitness는 -999.0으로 초기화되어야 한다."""
        rng = random.Random(3)
        p1 = Genome.random(rng); p1.fitness = 5.0
        p2 = Genome.random(rng); p2.fitness = 3.0
        c1, c2 = uniform_crossover(p1, p2, rng)
        self.assertEqual(c1.fitness, -999.0)
        self.assertEqual(c2.fitness, -999.0)

    def test_mutation_changes_at_least_one_gene_with_high_rate(self):
        """돌연변이율 1.0에서 반드시 최소 하나의 유전자가 변경되어야 한다."""
        rng = random.Random(11)
        original = Genome()  # 고정 기본값
        mutated = mutate(original, rng, rate=1.0)
        # 적어도 하나의 유전자가 달라야 함
        changed = any(
            getattr(mutated, n) != getattr(original, n)
            for n in GENE_BOUNDS
        )
        self.assertTrue(changed, "rate=1.0인데 변경된 유전자가 없음")

    def test_mutation_respects_bounds(self):
        """돌연변이 후에도 모든 유전자가 범위 내에 있어야 한다."""
        rng = random.Random(22)
        for _ in range(100):
            g = Genome.random(rng)
            m = mutate(g, rng, rate=1.0).clamp()
            for name, (lo, hi, _) in GENE_BOUNDS.items():
                self.assertGreaterEqual(getattr(m, name), lo)
                self.assertLessEqual(getattr(m, name), hi)

    def test_mutation_resets_fitness(self):
        """돌연변이 후 fitness는 -999.0으로 초기화되어야 한다."""
        rng = random.Random(0)
        g = Genome.random(rng)
        g.fitness = 42.0
        m = mutate(g, rng, rate=0.5)
        self.assertEqual(m.fitness, -999.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 적합도 함수 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateFitness(unittest.TestCase):

    def test_fitness_returns_float(self):
        """evaluate_fitness()가 float을 반환해야 한다."""
        candles = make_candles(200, price=100.0, volume=10.0)
        f = evaluate_fitness(Genome(), candles)
        self.assertIsInstance(f, float)

    def test_fitness_penalizes_no_trades(self):
        """거래가 거의 없는 설정(큰 박스 룩백)은 페널티 점수를 받아야 한다."""
        # box_lookback=200 → 200개 이하 캔들에서 박스가 계산되지 않아 거래 0
        candles = make_candles(150, price=100.0, volume=10.0)
        g = Genome()
        g.box_lookback = 200  # 캔들 수보다 큰 룩백
        f = evaluate_fitness(g, candles)
        self.assertLessEqual(f, -100.0, "거래 없음 → 페널티 점수 < -100")

    def test_fitness_restores_module_constants(self):
        """evaluate_fitness 호출 후 backtest 모듈 상수가 복원되어야 한다."""
        import src.backtest as bt
        original_lookback = bt.BOX_LOOKBACK
        original_spike = bt.VOLUME_SPIKE_MULTIPLIER

        g = Genome(box_lookback=77, volume_spike_multiplier=4.5)
        candles = make_candles(200)
        evaluate_fitness(g, candles)

        self.assertEqual(bt.BOX_LOOKBACK, original_lookback)
        self.assertAlmostEqual(bt.VOLUME_SPIKE_MULTIPLIER, original_spike)

    def test_fitness_restores_on_exception(self):
        """예외 발생 시에도 모듈 상수가 복원되어야 한다."""
        import src.backtest as bt
        original = bt.BOX_LOOKBACK

        with patch.object(bt, "BacktestEngine") as MockEngine:
            MockEngine.return_value.run.side_effect = RuntimeError("boom")
            g = Genome()
            with self.assertRaises(RuntimeError):
                evaluate_fitness(g, make_candles(50))

        self.assertEqual(bt.BOX_LOOKBACK, original)

    def test_two_genomes_different_fitness(self):
        """서로 다른 Genome은 다른 fitness를 가질 수 있어야 한다."""
        # 거래량 노이즈를 높여 volume spike가 발생하도록 설정 (volume ≥ MA×spike_mult)
        candles = make_candles(600, price=100.0, volume=10.0, vol_noise=8.0)
        g1 = Genome(box_lookback=30, volume_spike_multiplier=2.0, trailing_stop_pct=0.01)
        g2 = Genome(box_lookback=40, volume_spike_multiplier=2.0, trailing_stop_pct=0.04)
        f1 = evaluate_fitness(g1, candles)
        f2 = evaluate_fitness(g2, candles)
        # 적어도 하나는 유효한 (페널티가 아닌) 점수를 가져야 함
        self.assertTrue(
            f1 > -200.0 or f2 > -200.0,
            f"둘 다 거래 미달 페널티: f1={f1:.2f}, f2={f2:.2f}",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 표류 감지 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestDriftDetection(unittest.TestCase):

    def _profile(self, vol_mean, vol_std, range_mean=0.003, range_std=0.001):
        return MarketProfile(vol_mean, vol_std, range_mean, range_std)

    def test_no_drift_on_identical_profiles(self):
        """동일한 프로파일에서는 drift가 감지되지 않아야 한다."""
        p = self._profile(100.0, 10.0)
        is_drift, score = detect_drift(p, p)
        self.assertFalse(is_drift)
        self.assertAlmostEqual(score, 0.0)

    def test_drift_detected_on_volume_spike(self):
        """거래량이 3σ 이상 급등하면 drift가 감지되어야 한다."""
        baseline = self._profile(100.0, 10.0)
        recent = self._profile(200.0, 10.0)  # z_vol = (200-100)/10 = 10
        is_drift, score = detect_drift(recent, baseline)
        self.assertTrue(is_drift)
        self.assertGreater(score, DRIFT_Z_THRESHOLD)

    def test_no_drift_on_slight_change(self):
        """미미한 변화(z < threshold)는 drift로 판정하지 않아야 한다."""
        baseline = self._profile(100.0, 20.0)
        recent = self._profile(105.0, 20.0)   # z_vol = 0.25
        is_drift, _ = detect_drift(recent, baseline, z_threshold=2.0)
        self.assertFalse(is_drift)

    def test_drift_on_combined_range_and_volume(self):
        """거래량과 가격범위가 동시에 변하면 합산 z-score로 drift 감지."""
        baseline = MarketProfile(100.0, 10.0, 0.003, 0.001)
        recent = MarketProfile(120.0, 10.0, 0.006, 0.001)
        # z_vol ≈ 2.0, z_range ≈ 3.0 → 합계 5.0 > threshold
        is_drift, score = detect_drift(recent, baseline, z_threshold=2.0)
        self.assertTrue(is_drift)
        self.assertGreater(score, 4.0)

    def test_market_profile_from_candles(self):
        """실제 캔들 리스트에서 MarketProfile을 정상 생성해야 한다."""
        candles = make_candles(100, price=100.0, volume=10.0)
        profile = MarketProfile.from_candles(candles)
        self.assertIsNotNone(profile)
        self.assertGreater(profile.vol_mean, 0)
        self.assertGreater(profile.range_mean, 0)

    def test_market_profile_returns_none_on_few_candles(self):
        """캔들이 10개 미만이면 None을 반환해야 한다."""
        self.assertIsNone(MarketProfile.from_candles(make_candles(5)))

    def test_drift_score_is_symmetric_sign(self):
        """vol_mean이 낮아져도 drift_score는 양수여야 한다 (절대값)."""
        baseline = self._profile(100.0, 10.0)
        recent = self._profile(10.0, 10.0)   # 큰 음의 z
        _, score = detect_drift(recent, baseline)
        self.assertGreater(score, 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Optimizer.run_cycle() 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptimizerRunCycle(unittest.TestCase):

    def _fast_opt(self, pop=6, gens=4, seed=42):
        """빠른 테스트용 소형 Optimizer (가짜 fitness 함수 주입)."""
        return Optimizer(
            population_size=pop,
            generations=gens,
            elitism=1,
            mutation_rate=0.3,
            tournament_k=2,
            seed=seed,
            fitness_fn=mock_fitness_fn,
        )

    def test_returns_genome(self):
        """run_cycle()은 Genome 인스턴스를 반환해야 한다."""
        opt = self._fast_opt()
        result = opt.run_cycle(make_candles(50))
        self.assertIsInstance(result, Genome)

    def test_generation_log_populated(self):
        """세대 로그가 정확히 generations 개 항목을 포함해야 한다."""
        gens = 5
        opt = self._fast_opt(gens=gens)
        opt.run_cycle(make_candles(50))
        self.assertEqual(len(opt.generation_log), gens)

    def test_generation_log_keys(self):
        """세대 로그 각 항목은 필수 키를 가져야 한다."""
        opt = self._fast_opt()
        opt.run_cycle(make_candles(50))
        for entry in opt.generation_log:
            self.assertIn("gen", entry)
            self.assertIn("best_fitness", entry)
            self.assertIn("mean_fitness", entry)
            self.assertIn("best_genome", entry)

    def test_best_fitness_non_decreasing(self):
        """세대가 진행될수록 최상 fitness는 감소하지 않아야 한다."""
        opt = self._fast_opt(pop=10, gens=6)
        opt.run_cycle(make_candles(50))
        bests = [e["best_fitness"] for e in opt.generation_log]
        for i in range(1, len(bests)):
            self.assertGreaterEqual(
                bests[i], bests[i - 1] - 1e-9,
                f"Gen {i} fitness {bests[i]:.4f} < Gen {i-1} {bests[i-1]:.4f}",
            )

    def test_best_genome_within_bounds(self):
        """최종 최우수 Genome의 모든 유전자가 범위 내에 있어야 한다."""
        opt = self._fast_opt(pop=8, gens=3)
        best = opt.run_cycle(make_candles(50))
        for name, (lo, hi, _) in GENE_BOUNDS.items():
            val = getattr(best, name)
            self.assertGreaterEqual(val, lo, f"{name} < lo after cycle")
            self.assertLessEqual(val, hi, f"{name} > hi after cycle")

    def test_progress_callback_called(self):
        """progress_callback이 generations 횟수만큼 호출되어야 한다."""
        calls = []
        opt = self._fast_opt(gens=4)
        opt.run_cycle(make_candles(50), progress_callback=lambda g, f: calls.append(g))
        self.assertEqual(len(calls), 4)

    def test_empty_candles_raises(self):
        """빈 캔들 리스트는 ValueError를 발생시켜야 한다."""
        opt = self._fast_opt()
        with self.assertRaises(ValueError):
            opt.run_cycle([])

    def test_mock_fitness_picks_highest_box_lookback(self):
        """mock_fitness_fn은 box_lookback 최대화를 선호 → 최종 값이 크야 한다."""
        opt = self._fast_opt(pop=12, gens=8)
        best = opt.run_cycle(make_candles(50))
        # 완벽히 수렴하진 않지만 평균값(110)보다 높아야 함
        self.assertGreater(best.box_lookback, 110)


# ═══════════════════════════════════════════════════════════════════════════════
# Optimizer.apply_to_env() 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplyToEnv(unittest.TestCase):

    def _temp_env(self, content: str = "") -> str:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            return f.name

    def test_creates_file_when_missing(self):
        """존재하지 않는 경로에 .env를 새로 생성해야 한다."""
        path = tempfile.mktemp(suffix=".env")
        self.assertFalse(os.path.exists(path))
        try:
            Optimizer().apply_to_env(Genome(), env_path=path)
            self.assertTrue(os.path.exists(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_all_keys_written(self):
        """모든 Genome 파라미터 키가 .env에 존재해야 한다."""
        path = self._temp_env()
        try:
            Optimizer().apply_to_env(Genome(), env_path=path)
            with open(path) as f:
                content = f.read()
            for key in Genome().to_env_dict():
                self.assertIn(key, content, f"{key} not found in .env")
        finally:
            os.unlink(path)

    def test_existing_keys_are_updated(self):
        """기존에 있던 키는 새 값으로 덮어써야 한다."""
        path = self._temp_env("BOX_LOOKBACK=50\nOTHER_KEY=abc\n")
        try:
            g = Genome(box_lookback=99)
            Optimizer().apply_to_env(g, env_path=path)
            with open(path) as f:
                lines = f.readlines()
            val_line = next(l for l in lines if l.startswith("BOX_LOOKBACK="))
            self.assertIn("99", val_line)
        finally:
            os.unlink(path)

    def test_unrelated_keys_preserved(self):
        """Genome과 관계없는 기존 키는 그대로 보존해야 한다."""
        path = self._temp_env("PRIVATE_KEY=0xdeadbeef\nBINANCE_API_KEY=abc123\n")
        try:
            Optimizer().apply_to_env(Genome(), env_path=path)
            with open(path) as f:
                content = f.read()
            self.assertIn("PRIVATE_KEY=0xdeadbeef", content)
            self.assertIn("BINANCE_API_KEY=abc123", content)
        finally:
            os.unlink(path)

    def test_comments_preserved(self):
        """# 주석 줄은 그대로 보존해야 한다."""
        path = self._temp_env("# Trading config\nBOX_LOOKBACK=50\n")
        try:
            Optimizer().apply_to_env(Genome(), env_path=path)
            with open(path) as f:
                content = f.read()
            self.assertIn("# Trading config", content)
        finally:
            os.unlink(path)

    def test_values_match_genome(self):
        """작성된 값이 Genome의 to_env_dict()와 일치해야 한다."""
        path = self._temp_env()
        try:
            g = Genome(
                volume_spike_multiplier=2.5,
                box_lookback=77,
                trailing_stop_pct=0.03,
            )
            Optimizer().apply_to_env(g, env_path=path)
            env_dict = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env_dict[k] = v
            expected = g.to_env_dict()
            for key, val in expected.items():
                self.assertEqual(env_dict[key], val, f"{key} mismatch")
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
# Optimizer.should_retrain() 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestShouldRetrain(unittest.TestCase):

    def setUp(self):
        self.opt = Optimizer(fitness_fn=mock_fitness_fn)
        # 정상 (안정적) 베이스라인 캔들
        self.stable = make_candles(500, price=100.0, volume=10.0, vol_noise=1.0)
        # 거래량이 3배인 최근 캔들 → drift 유발
        self.volatile = make_candles(200, price=100.0, volume=30.0, vol_noise=2.0)

    def test_scheduled_retraining_when_overdue(self):
        """마지막 재학습이 충분히 오래됐으면 True를 반환해야 한다."""
        old_ts = time.time() - 8 * 86_400  # 8일 전
        should, reason = self.opt.should_retrain(
            self.stable, self.stable, last_retrain_ts=old_ts, retrain_interval_days=7.0
        )
        self.assertTrue(should)
        self.assertIn("scheduled", reason)

    def test_no_retrain_when_recent(self):
        """최근 재학습 + 안정 시장이면 False를 반환해야 한다."""
        recent_ts = time.time() - 1 * 86_400  # 1일 전
        should, reason = self.opt.should_retrain(
            self.stable, self.stable, last_retrain_ts=recent_ts, retrain_interval_days=7.0
        )
        self.assertFalse(should)
        self.assertIn("stable", reason)

    def test_drift_triggers_retraining(self):
        """시장 표류 감지 시 일정과 무관하게 True를 반환해야 한다."""
        recent_ts = time.time() - 1 * 86_400  # 1일 전 (아직 일정이 아님)
        should, reason = self.opt.should_retrain(
            self.volatile, self.stable, last_retrain_ts=recent_ts, retrain_interval_days=7.0
        )
        self.assertTrue(should)
        self.assertIn("drift", reason)

    def test_insufficient_candles_falls_back_to_schedule(self):
        """캔들 부족 시 일정 기준으로 판단해야 한다."""
        few = make_candles(5)  # < 10 → profile None
        old_ts = 0.0
        should, reason = self.opt.should_retrain(
            few, few, last_retrain_ts=old_ts, retrain_interval_days=7.0
        )
        self.assertTrue(should)

    def test_reason_is_string(self):
        """should_retrain()의 두 번째 반환값은 str이어야 한다."""
        _, reason = self.opt.should_retrain(self.stable, self.stable)
        self.assertIsInstance(reason, str)


# ═══════════════════════════════════════════════════════════════════════════════
# 통합 시나리오: 실제 fitness 함수 + 소형 GA
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):

    def test_real_fitness_run_cycle(self):
        """실제 BacktestEngine을 사용하여 소형 GA 사이클이 완료되어야 한다."""
        candles = make_candles(500, price=100.0, volume=10.0, trend=0.005)
        opt = Optimizer(
            population_size=4,
            generations=3,
            elitism=1,
            seed=7,
            # 실제 evaluate_fitness 사용 (fitness_fn 미지정)
        )
        best = opt.run_cycle(candles)
        self.assertIsInstance(best, Genome)
        self.assertNotEqual(best.fitness, -999.0)
        # 3세대 로그
        self.assertEqual(len(opt.generation_log), 3)

    def test_end_to_end_with_env_write(self):
        """GA 실행 → 최우수 Genome → .env 쓰기 전체 파이프라인."""
        candles = make_candles(300, price=100.0, volume=8.0)
        opt = Optimizer(
            population_size=4, generations=2, seed=0,
            fitness_fn=mock_fitness_fn,
        )
        best = opt.run_cycle(candles)

        path = tempfile.mktemp(suffix=".env")
        try:
            opt.apply_to_env(best, env_path=path)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                content = f.read()
            self.assertIn("BOX_LOOKBACK", content)
            self.assertIn("VOLUME_SPIKE_MULTIPLIER", content)
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════════
# strategy_config.json 읽기/쓰기 테스트
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigJson(unittest.TestCase):

    def _temp_config_dir(self):
        d = tempfile.mkdtemp()
        return d, os.path.join(d, "strategy_config.json")

    def test_apply_to_config_creates_file(self):
        """apply_to_config()가 JSON 파일을 생성해야 한다."""
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(Genome(), config_path=path)
            self.assertTrue(os.path.exists(path))
        finally:
            import shutil; shutil.rmtree(d)

    def test_apply_to_config_valid_json(self):
        """생성된 파일은 유효한 JSON이어야 한다."""
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(Genome(), config_path=path)
            with open(path) as f:
                data = json.load(f)
            self.assertIn("parameters", data)
            self.assertIn("ga_metadata", data)
        finally:
            import shutil; shutil.rmtree(d)

    def test_config_contains_all_genes(self):
        """config.json의 parameters 섹션에 모든 유전자 키가 있어야 한다."""
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(Genome(), config_path=path)
            with open(path) as f:
                params = json.load(f)["parameters"]
            for name in GENE_BOUNDS:
                self.assertIn(name, params, f"{name} missing in config")
        finally:
            import shutil; shutil.rmtree(d)

    def test_config_values_match_genome(self):
        """config에 기록된 수치가 Genome 값과 정확히 일치해야 한다."""
        g = Genome(box_lookback=88, volume_spike_multiplier=2.7, trailing_stop_pct=0.025)
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(g, config_path=path)
            with open(path) as f:
                params = json.load(f)["parameters"]
            self.assertEqual(params["box_lookback"], 88)
            self.assertAlmostEqual(params["volume_spike_multiplier"], 2.7, places=4)
            self.assertAlmostEqual(params["trailing_stop_pct"], 0.025, places=5)
        finally:
            import shutil; shutil.rmtree(d)

    def test_config_metadata_has_fitness_and_timestamp(self):
        """ga_metadata에 fitness와 updated_at 필드가 있어야 한다."""
        g = Genome(); g.fitness = 3.14
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(g, config_path=path)
            with open(path) as f:
                meta = json.load(f)["ga_metadata"]
            self.assertAlmostEqual(meta["fitness"], 3.14, places=4)
            self.assertIn("updated_at", meta)
        finally:
            import shutil; shutil.rmtree(d)

    def test_config_preserves_extra_keys(self):
        """기존 JSON에 있던 다른 키는 덮어쓰지 않아야 한다."""
        d, path = self._temp_config_dir()
        try:
            with open(path, "w") as f:
                json.dump({"custom_key": "custom_value"}, f)
            Optimizer().apply_to_config(Genome(), config_path=path)
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data.get("custom_key"), "custom_value")
        finally:
            import shutil; shutil.rmtree(d)

    def test_load_from_config_returns_genome(self):
        """저장 후 load_from_config()로 Genome을 복원할 수 있어야 한다."""
        g = Genome(box_lookback=77, trailing_stop_pct=0.03)
        g.fitness = 1.5
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(g, config_path=path)
            restored = Optimizer.load_from_config(config_path=path)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.box_lookback, 77)
            self.assertAlmostEqual(restored.trailing_stop_pct, 0.03, places=5)
            self.assertAlmostEqual(restored.fitness, 1.5, places=3)
        finally:
            import shutil; shutil.rmtree(d)

    def test_load_from_config_returns_none_when_missing(self):
        """파일이 없으면 None을 반환해야 한다."""
        self.assertIsNone(Optimizer.load_from_config("/nonexistent/path.json"))

    def test_load_from_config_returns_none_on_corrupt_json(self):
        """JSON이 손상된 경우 None을 반환해야 한다."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json{{")
            path = f.name
        try:
            self.assertIsNone(Optimizer.load_from_config(config_path=path))
        finally:
            os.unlink(path)

    def test_roundtrip_preserves_all_genes(self):
        """저장 → 복원 후 모든 유전자 값이 동일해야 한다 (정수 포함)."""
        rng = random.Random(42)
        original = Genome.random(rng)
        d, path = self._temp_config_dir()
        try:
            Optimizer().apply_to_config(original, config_path=path)
            restored = Optimizer.load_from_config(config_path=path)
            for name, (_, _, is_int) in GENE_BOUNDS.items():
                ov = getattr(original, name)
                rv = getattr(restored, name)
                if is_int:
                    self.assertEqual(ov, rv, f"{name}: {ov} ≠ {rv}")
                else:
                    self.assertAlmostEqual(ov, rv, places=4, msg=f"{name}: {ov} ≠ {rv}")
        finally:
            import shutil; shutil.rmtree(d)


# ═══════════════════════════════════════════════════════════════════════════════
# 전체 진화 파이프라인: Drift 감지 → GA 수렴 검증
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullEvolutionPipeline(unittest.TestCase):
    """
    명세서의 핵심 검증 시나리오:
    (A) 인위적으로 거래량·변동성을 3배 이상 증폭한 데이터 → should_retrain() Drift 감지
    (B) run_cycle() 실행 → 세대를 거듭할수록 best_fitness가 단조 증가
    (C) 최종 최적 파라미터가 .env와 config.json에 영구 기록됨
    """

    def _stable_candles(self, n=500):
        """안정적인 베이스라인 캔들 (낮은 거래량/변동성)."""
        return make_candles(n, price=100.0, volume=10.0, vol_noise=1.0)

    def _volatile_candles(self, n=300):
        """거래량 3배 + 변동성 급등 시뮬레이션 (Drift 유발)."""
        return make_candles(n, price=100.0, volume=35.0, vol_noise=5.0)

    def test_A_drift_detected_on_3x_volume(self):
        """거래량 3배 이상 급등 데이터에서 Drift가 정확히 감지되어야 한다."""
        stable = self._stable_candles()
        volatile = self._volatile_candles()

        opt = Optimizer(fitness_fn=mock_fitness_fn)
        should, reason = opt.should_retrain(
            recent_candles=volatile,
            baseline_candles=stable,
            last_retrain_ts=time.time() - 1 * 86_400,  # 1일 전 (7일 미도달)
            retrain_interval_days=7.0,
        )
        self.assertTrue(should, f"3배 거래량인데 Drift 미감지: {reason}")
        self.assertIn("drift", reason)

    def test_B_fitness_converges_over_generations(self):
        """GA 세대가 진행될수록 best_fitness가 단조 증가(≥ 이전 세대)해야 한다."""
        candles = make_candles(400, price=100.0, volume=10.0, vol_noise=3.0)
        opt = Optimizer(
            population_size=8,
            generations=6,
            elitism=2,
            mutation_rate=0.2,
            seed=99,
            fitness_fn=mock_fitness_fn,
        )
        opt.run_cycle(candles)
        bests = [e["best_fitness"] for e in opt.generation_log]

        # 단조 비감소: 엘리트 보존으로 보장됨
        for i in range(1, len(bests)):
            self.assertGreaterEqual(
                bests[i], bests[i - 1] - 1e-9,
                f"Gen {i} fitness {bests[i]:.4f} < Gen {i-1} {bests[i-1]:.4f}",
            )

        # 마지막 세대 fitness가 초기 세대보다 높아야 함 (수렴 검증)
        self.assertGreater(bests[-1], bests[0], "마지막 세대가 첫 세대보다 높아야 함")

    def test_C_full_pipeline_env_and_config(self):
        """Drift 감지 → GA → .env + config.json 파일 기록 전체 파이프라인."""
        candles = make_candles(300, price=100.0, volume=10.0, vol_noise=3.0)
        opt = Optimizer(
            population_size=4,
            generations=3,
            seed=7,
            fitness_fn=mock_fitness_fn,
        )

        env_path = tempfile.mktemp(suffix=".env")
        config_dir = tempfile.mkdtemp()
        config_path = os.path.join(config_dir, "strategy_config.json")

        try:
            # GA 실행
            best = opt.run_cycle(candles)

            # 영구 기록
            opt.apply_to_env(best, env_path=env_path)
            opt.apply_to_config(best, config_path=config_path)

            # .env 검증
            self.assertTrue(os.path.exists(env_path))
            with open(env_path) as f:
                env_content = f.read()
            self.assertIn("BOX_LOOKBACK", env_content)
            self.assertIn("VOLUME_SPIKE_MULTIPLIER", env_content)

            # config.json 검증
            self.assertTrue(os.path.exists(config_path))
            with open(config_path) as f:
                config = json.load(f)
            self.assertIn("parameters", config)
            self.assertIn("ga_metadata", config)
            self.assertAlmostEqual(
                config["ga_metadata"]["fitness"], best.fitness, places=3
            )

            # 복원 검증
            restored = Optimizer.load_from_config(config_path=config_path)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.box_lookback, best.box_lookback)

        finally:
            if os.path.exists(env_path):
                os.unlink(env_path)
            import shutil; shutil.rmtree(config_dir)

    def test_D_stable_market_no_early_retrain(self):
        """안정 시장이고 7일 미경과 시 재학습 트리거가 켜지지 않아야 한다."""
        stable = self._stable_candles()
        opt = Optimizer(fitness_fn=mock_fitness_fn)
        recent_ts = time.time() - 1 * 86_400  # 1일 전
        should, reason = opt.should_retrain(
            recent_candles=stable,
            baseline_candles=stable,
            last_retrain_ts=recent_ts,
            retrain_interval_days=7.0,
        )
        self.assertFalse(should, f"안정 시장인데 재학습 트리거: {reason}")
        self.assertIn("stable", reason)

    def test_E_generation_log_fitness_range(self):
        """mock_fitness_fn 기준 최종 best_fitness가 0.5 이상 수렴해야 한다."""
        candles = make_candles(200, price=100.0, volume=8.0)
        opt = Optimizer(
            population_size=10,
            generations=8,
            elitism=2,
            seed=1,
            fitness_fn=mock_fitness_fn,
        )
        best = opt.run_cycle(candles)
        # mock_fitness = box_lookback/200 → 범위 [0.1, 1.0]
        self.assertGreaterEqual(best.fitness, 0.5,
            f"8세대 수렴 후 fitness={best.fitness:.3f} < 0.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
