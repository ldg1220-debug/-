"""Genetic Algorithm optimizer for Engine A / B trading parameters.

Retraining cycle (typically every weekend or on drift detection):
  1. Compare recent 7-day market profile vs 30-day baseline (drift check)
  2. If drift detected or schedule due → run GA on recent candle data
  3. Write winning parameter set to .env

Usage:
    from src.optimizer import Optimizer
    opt = Optimizer(seed=42)
    best = opt.run_cycle(recent_candles)
    opt.apply_to_env(best, env_path=".env")
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── GA hyper-parameters ──────────────────────────────────────────────────────

GA_POPULATION = int(os.getenv("GA_POPULATION", "24"))
GA_GENERATIONS = int(os.getenv("GA_GENERATIONS", "15"))
GA_ELITISM = int(os.getenv("GA_ELITISM", "2"))
GA_MUTATION_RATE = float(os.getenv("GA_MUTATION_RATE", "0.10"))
GA_TOURNAMENT_K = int(os.getenv("GA_TOURNAMENT_K", "3"))

# Drift detection
DRIFT_Z_THRESHOLD = float(os.getenv("DRIFT_Z_THRESHOLD", "2.0"))

# Fitness weights
FITNESS_SHARPE_WEIGHT = float(os.getenv("FITNESS_SHARPE_WEIGHT", "1.0"))
FITNESS_DRAWDOWN_PENALTY = float(os.getenv("FITNESS_DRAWDOWN_PENALTY", "2.0"))
FITNESS_MIN_TRADES = int(os.getenv("FITNESS_MIN_TRADES", "3"))

# Gene definitions: name → (min, max, is_integer)
GENE_BOUNDS: dict[str, tuple] = {
    "volume_spike_multiplier": (1.5,  6.0,  False),
    "box_lookback":            (20,   200,  True),
    "trailing_stop_pct":       (0.005, 0.05, False),
    "stop_loss_pct":           (0.005, 0.04, False),
    "adx_trend_threshold":     (15.0, 40.0,  False),
    "trend_atr_multiplier":    (1.0,  4.0,   False),
    "exit_min_apr":            (1.0,  10.0,  False),
    "exit_loss_threshold":     (0.005, 0.08, False),
}


# ─── Genome ───────────────────────────────────────────────────────────────────

@dataclass
class Genome:
    """One individual in the GA population (= one complete parameter set)."""

    volume_spike_multiplier: float = 3.0
    box_lookback:            int   = 50
    trailing_stop_pct:       float = 0.020
    stop_loss_pct:           float = 0.015
    adx_trend_threshold:     float = 25.0
    trend_atr_multiplier:    float = 2.0
    exit_min_apr:            float = 3.0
    exit_loss_threshold:     float = 0.02

    # Set by the optimizer after evaluation — not a gene
    fitness: float = field(default=-999.0, compare=False)

    @classmethod
    def random(cls, rng: random.Random) -> "Genome":
        """Sample a genome uniformly at random within gene bounds."""
        kwargs: dict = {}
        for name, (lo, hi, is_int) in GENE_BOUNDS.items():
            v = rng.uniform(lo, hi)
            kwargs[name] = int(round(v)) if is_int else v
        return cls(**kwargs)

    def clamp(self) -> "Genome":
        """Clamp every gene to its defined bounds (in-place, returns self)."""
        for name, (lo, hi, is_int) in GENE_BOUNDS.items():
            v = getattr(self, name)
            v = max(lo, min(hi, v))
            setattr(self, name, int(round(v)) if is_int else v)
        return self

    def to_env_dict(self) -> dict[str, str]:
        """Return {ENV_VAR_NAME: str_value} ready for .env serialization."""
        return {
            "VOLUME_SPIKE_MULTIPLIER": str(round(self.volume_spike_multiplier, 4)),
            "BOX_LOOKBACK":            str(self.box_lookback),
            "TRAILING_STOP_PCT":       str(round(self.trailing_stop_pct, 5)),
            "STOP_LOSS_PCT":           str(round(self.stop_loss_pct, 5)),
            "ADX_TREND_THRESHOLD":     str(round(self.adx_trend_threshold, 2)),
            "TREND_ATR_MULTIPLIER":    str(round(self.trend_atr_multiplier, 3)),
            "EXIT_MIN_APR":            str(round(self.exit_min_apr, 2)),
            "EXIT_LOSS_THRESHOLD":     str(round(self.exit_loss_threshold, 4)),
        }

    def summary(self) -> str:
        d = self.to_env_dict()
        rows = "\n".join(f"  {k:30s} = {v}" for k, v in sorted(d.items()))
        return f"Genome (fitness={self.fitness:.4f}):\n{rows}"


# ─── Fitness evaluation ───────────────────────────────────────────────────────

def evaluate_fitness(genome: Genome, candles: list, initial_capital: float = 10_000.0) -> float:
    """
    Run BacktestEngine with genome parameters and return a scalar fitness.

    Fitness = Sharpe × w_sharpe  −  MaxDrawdown × w_dd
    Genomes with < FITNESS_MIN_TRADES trades are heavily penalised.

    The function temporarily monkey-patches the module-level constants used by
    BacktestEngine helpers (_is_volume_spike, _calc_box, etc.) so no changes
    to BacktestEngine are required.
    """
    try:
        import src.backtest as _bt
    except ModuleNotFoundError:
        from . import backtest as _bt  # type: ignore[no-redef]

    saved = {
        "BOX_LOOKBACK":              _bt.BOX_LOOKBACK,
        "VOLUME_SPIKE_MULTIPLIER":   _bt.VOLUME_SPIKE_MULTIPLIER,
        "TRAILING_STOP_PCT":         _bt.TRAILING_STOP_PCT,
        "STOP_LOSS_PCT":             _bt.STOP_LOSS_PCT,
    }
    _bt.BOX_LOOKBACK             = genome.box_lookback
    _bt.VOLUME_SPIKE_MULTIPLIER  = genome.volume_spike_multiplier
    _bt.TRAILING_STOP_PCT        = genome.trailing_stop_pct
    _bt.STOP_LOSS_PCT            = genome.stop_loss_pct

    try:
        result = _bt.BacktestEngine(candles=candles, initial_capital=initial_capital).run()
    finally:
        for k, v in saved.items():
            setattr(_bt, k, v)

    if result.total_trades < FITNESS_MIN_TRADES:
        return -500.0

    return (
        FITNESS_SHARPE_WEIGHT * result.sharpe_ratio
        - FITNESS_DRAWDOWN_PENALTY * result.max_drawdown_pct
    )


# ─── GA operators ─────────────────────────────────────────────────────────────

def tournament_select(population: List[Genome], k: int, rng: random.Random) -> Genome:
    """Return the fittest individual from a random sample of k."""
    contestants = rng.sample(population, min(k, len(population)))
    return max(contestants, key=lambda g: g.fitness)


def uniform_crossover(
    parent_a: Genome, parent_b: Genome, rng: random.Random
) -> Tuple[Genome, Genome]:
    """Produce two offspring via 50/50 uniform gene swap."""
    child_a, child_b = copy.copy(parent_a), copy.copy(parent_b)
    for name in GENE_BOUNDS:
        if rng.random() < 0.5:
            setattr(child_a, name, getattr(parent_b, name))
            setattr(child_b, name, getattr(parent_a, name))
    child_a.fitness = child_b.fitness = -999.0
    return child_a, child_b


def mutate(genome: Genome, rng: random.Random, rate: float = GA_MUTATION_RATE) -> Genome:
    """Gaussian perturbation of each gene with probability `rate`."""
    g = copy.copy(genome)
    for name, (lo, hi, is_int) in GENE_BOUNDS.items():
        if rng.random() < rate:
            sigma = (hi - lo) * 0.10  # σ = 10 % of range
            v = getattr(g, name) + rng.gauss(0.0, sigma)
            v = max(lo, min(hi, v))
            setattr(g, name, int(round(v)) if is_int else v)
    g.fitness = -999.0
    return g


# ─── Market profile & drift detection ────────────────────────────────────────

@dataclass
class MarketProfile:
    """Compact summary statistics used to detect regime change."""

    vol_mean: float   # mean candle volume
    vol_std: float    # std of candle volume
    range_mean: float # mean relative (high-low)/close per candle
    range_std: float  # std of above

    @classmethod
    def from_candles(cls, candles: list) -> Optional["MarketProfile"]:
        if len(candles) < 10:
            return None
        volumes = [c.volume for c in candles]
        ranges = [
            (c.high - c.low) / c.close if c.close else 0.0
            for c in candles
        ]
        try:
            return cls(
                vol_mean=statistics.mean(volumes),
                vol_std=statistics.stdev(volumes),
                range_mean=statistics.mean(ranges),
                range_std=statistics.stdev(ranges),
            )
        except statistics.StatisticsError:
            return None


def detect_drift(
    recent: MarketProfile,
    baseline: MarketProfile,
    z_threshold: float = DRIFT_Z_THRESHOLD,
) -> Tuple[bool, float]:
    """
    Compare recent vs baseline market profile using z-scores.

    Returns (is_drift: bool, drift_score: float).
    drift_score = Σ |z_i| across monitored metrics.
    """
    score = 0.0
    pairs = [
        (recent.vol_mean,   baseline.vol_mean,   baseline.vol_std),
        (recent.range_mean, baseline.range_mean, baseline.range_std),
    ]
    for val, mu, sigma in pairs:
        if sigma > 1e-12:
            score += abs((val - mu) / sigma)
    return score > z_threshold, score


# ─── Optimizer ────────────────────────────────────────────────────────────────

class Optimizer:
    """
    Genetic Algorithm optimizer for trading engine parameters.

    Attributes
    ----------
    generation_log : list[dict]
        Per-generation statistics ({gen, best_fitness, mean_fitness, best_genome})
        populated after every run_cycle call.
    """

    def __init__(
        self,
        population_size: int = GA_POPULATION,
        generations: int = GA_GENERATIONS,
        elitism: int = GA_ELITISM,
        mutation_rate: float = GA_MUTATION_RATE,
        tournament_k: int = GA_TOURNAMENT_K,
        initial_capital: float = 10_000.0,
        seed: Optional[int] = None,
        fitness_fn: Optional[Callable] = None,
    ) -> None:
        self.population_size = population_size
        self.generations = generations
        self.elitism = max(0, elitism)
        self.mutation_rate = mutation_rate
        self.tournament_k = tournament_k
        self.initial_capital = initial_capital
        self.rng = random.Random(seed)
        self._fitness_fn = fitness_fn or evaluate_fitness
        self.generation_log: List[dict] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def run_cycle(
        self,
        candles: list,
        progress_callback: Optional[Callable[[int, float], None]] = None,
    ) -> Genome:
        """
        Run a full GA optimisation cycle on `candles`.

        Args:
            candles:           list of Candle objects (from BacktestDataLoader)
            progress_callback: optional fn(generation_index, best_fitness)

        Returns:
            Best Genome found across all generations.
        """
        if not candles:
            raise ValueError("Candle list is empty — cannot run optimizer.")

        logger.info(
            "Optimizer: GA start — pop=%d gen=%d candles=%d",
            self.population_size, self.generations, len(candles),
        )
        self.generation_log = []

        population = [Genome.random(self.rng) for _ in range(self.population_size)]

        for gen in range(self.generations):
            # ── Evaluate unevaluated genomes ──────────────────────────────
            for g in population:
                if g.fitness == -999.0:
                    g.fitness = self._fitness_fn(g, candles, self.initial_capital)

            population.sort(key=lambda g: g.fitness, reverse=True)
            best = population[0]
            mean_f = sum(g.fitness for g in population) / len(population)

            self.generation_log.append({
                "gen": gen,
                "best_fitness": best.fitness,
                "mean_fitness": mean_f,
                "best_genome": asdict(best),
            })

            if progress_callback:
                progress_callback(gen, best.fitness)

            logger.debug(
                "Gen %02d/%02d | best=%.4f mean=%.4f",
                gen + 1, self.generations, best.fitness, mean_f,
            )

            if gen == self.generations - 1:
                break  # last generation — skip offspring step

            # ── Produce next generation ───────────────────────────────────
            next_pop: List[Genome] = list(population[: self.elitism])  # elites

            while len(next_pop) < self.population_size:
                p1 = tournament_select(population, self.tournament_k, self.rng)
                p2 = tournament_select(population, self.tournament_k, self.rng)
                c1, c2 = uniform_crossover(p1, p2, self.rng)
                c1 = mutate(c1, self.rng, self.mutation_rate).clamp()
                c2 = mutate(c2, self.rng, self.mutation_rate).clamp()
                next_pop.extend([c1, c2])

            population = next_pop[: self.population_size]

        best = max(population, key=lambda g: g.fitness)
        logger.info("Optimizer: done — best_fitness=%.4f", best.fitness)
        return best

    def apply_to_env(self, genome: Genome, env_path: str = ".env") -> None:
        """
        Merge genome parameters into `env_path`.

        Existing lines not related to the genome are preserved.
        Missing keys are appended at the end.
        """
        updates = genome.to_env_dict()
        lines: List[str] = []

        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()

        found: set[str] = set()
        new_lines: List[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}\n")
                    found.add(key)
                    continue
            new_lines.append(line)

        for key, val in updates.items():
            if key not in found:
                new_lines.append(f"{key}={val}\n")

        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(new_lines)

        logger.info("Optimizer: %d params written to '%s'", len(updates), env_path)

    def should_retrain(
        self,
        recent_candles: list,
        baseline_candles: list,
        last_retrain_ts: float = 0.0,
        retrain_interval_days: float = 7.0,
    ) -> Tuple[bool, str]:
        """
        Decide whether a retraining cycle is warranted.

        Triggers when drift score exceeds DRIFT_Z_THRESHOLD **or** when
        `retrain_interval_days` have elapsed since `last_retrain_ts`.

        Returns (should: bool, reason: str).
        """
        now = time.time()
        elapsed_days = (now - last_retrain_ts) / 86_400.0

        recent = MarketProfile.from_candles(recent_candles)
        baseline = MarketProfile.from_candles(baseline_candles)

        if recent is None or baseline is None:
            if elapsed_days >= retrain_interval_days:
                return True, "scheduled (too few candles for drift check)"
            return False, "insufficient data"

        is_drift, score = detect_drift(recent, baseline)
        if is_drift:
            return True, f"drift detected (score={score:.2f} > {DRIFT_Z_THRESHOLD})"

        if elapsed_days >= retrain_interval_days:
            return True, f"scheduled (every {retrain_interval_days:.0f}d, last={elapsed_days:.1f}d ago)"

        return False, f"stable (drift={score:.2f}, days_since={elapsed_days:.1f})"

    def apply_to_config(
        self,
        genome: Genome,
        config_path: str = "config/strategy_config.json",
    ) -> None:
        """
        Write genome parameters to a JSON config file.

        The JSON structure is:
            {
              "ga_metadata": {"fitness": ..., "updated_at": "..."},
              "parameters": { <all genome genes as floats/ints> }
            }

        Existing keys not covered by the genome are preserved.
        """
        import datetime

        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)

        # Load existing config (if any)
        existing: dict = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            except (json.JSONDecodeError, OSError):
                existing = {}

        # Build gene dict (exclude fitness field)
        gene_data = {
            name: getattr(genome, name)
            for name in GENE_BOUNDS
        }

        existing["parameters"] = {**existing.get("parameters", {}), **gene_data}
        existing["ga_metadata"] = {
            "fitness": genome.fitness,
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        }

        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2)

        logger.info(
            "Optimizer: strategy config written to '%s' (fitness=%.4f)",
            config_path, genome.fitness,
        )

    @staticmethod
    def load_from_config(config_path: str = "config/strategy_config.json") -> Optional[Genome]:
        """
        Load a Genome from a JSON config file produced by apply_to_config().

        Returns None if the file does not exist or is malformed.
        Unknown keys in the JSON are silently ignored.
        """
        if not os.path.exists(config_path):
            return None
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            params = data.get("parameters", {})
            # Build kwargs, coercing types from GENE_BOUNDS
            kwargs: dict = {}
            for name, (lo, hi, is_int) in GENE_BOUNDS.items():
                if name in params:
                    v = params[name]
                    kwargs[name] = int(round(v)) if is_int else float(v)
            fitness = data.get("ga_metadata", {}).get("fitness", -999.0)
            g = Genome(**kwargs)
            g.fitness = float(fitness)
            return g.clamp()
        except Exception as exc:
            logger.warning("Optimizer: failed to load config from '%s': %s", config_path, exc)
            return None
