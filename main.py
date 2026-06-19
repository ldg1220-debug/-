"""[단계 7] 오케스트레이터: 전체 시스템 초기화, Paper Trading 모드, 자동 재연결."""

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import NoReturn

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

from src.auth import EdgeXAuth
from src.data_fetcher import DataFetcher
from src.engine_a import EngineA
from src.engine_b import EngineB
from src.monitor import DriftLevel, Monitor
from src.optimizer import Optimizer
from src.risk_manager import RiskConfig, RiskManager
from src.state_manager import StateManager
from src.sweeper import Sweeper

MAX_RETRY_DELAY = 60  # 최대 재연결 대기(초)
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "10000.0"))
OPTIMIZER_CHECK_INTERVAL = int(os.getenv("OPTIMIZER_CHECK_INTERVAL", "3600"))  # 1시간
OPTIMIZER_BASELINE_CANDLES = 30 * 24 * 60   # 30일 × 1분봉
OPTIMIZER_RECENT_CANDLES   = 7  * 24 * 60   # 7일 × 1분봉


# --- Retry decorator for async tasks ---

async def run_with_retry(coro_factory, name: str) -> NoReturn:
    """코루틴 팩토리를 지수 백오프로 무한 재시도합니다."""
    delay = 2
    while True:
        try:
            logger.info("[%s] 태스크 시작", name)
            await coro_factory()
            delay = 2  # 성공 후 초기화
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] 오류 발생: %s — %.1f초 후 재시도", name, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)


# --- Optimizer scheduler ---

class OptimizerScheduler:
    """
    Background scheduler that watches for market drift and triggers
    GA parameter retraining.

    Maintains a rolling candle buffer (up to 30 days of 1-min candles).
    Every OPTIMIZER_CHECK_INTERVAL seconds it calls should_retrain(); if
    triggered it runs run_cycle() in a thread pool (to avoid blocking the
    event loop) and writes the winning genome to .env + strategy_config.json.
    """

    ENV_PATH    = os.getenv("OPTIMIZER_ENV_PATH",    ".env")
    CONFIG_PATH = os.getenv("OPTIMIZER_CONFIG_PATH", "config/strategy_config.json")

    def __init__(self, optimizer: Optimizer, retrain_interval_days: float = 7.0) -> None:
        self._optimizer = optimizer
        self._retrain_interval_days = retrain_interval_days
        self._candle_buffer: list = []
        self._last_retrain_ts: float = 0.0
        self._running: bool = False

    def feed_candle(self, candle) -> None:
        """Append a candle to the rolling buffer (called from _on_candle)."""
        self._candle_buffer.append(candle)
        if len(self._candle_buffer) > OPTIMIZER_BASELINE_CANDLES:
            self._candle_buffer.pop(0)

    async def run(self) -> None:
        self._running = True
        logger.info("OptimizerScheduler: 가동 (체크 주기=%ds)", OPTIMIZER_CHECK_INTERVAL)
        while self._running:
            await asyncio.sleep(OPTIMIZER_CHECK_INTERVAL)
            await self._check_and_retrain()

    async def _check_and_retrain(self) -> None:
        if len(self._candle_buffer) < 100:
            logger.debug("OptimizerScheduler: 캔들 부족 — 건너뜀")
            return

        recent   = self._candle_buffer[-OPTIMIZER_RECENT_CANDLES:]
        baseline = self._candle_buffer

        should, reason = self._optimizer.should_retrain(
            recent, baseline,
            last_retrain_ts=self._last_retrain_ts,
            retrain_interval_days=self._retrain_interval_days,
        )

        if not should:
            logger.debug("OptimizerScheduler: 재학습 불필요 — %s", reason)
            return

        logger.info("OptimizerScheduler: 재학습 트리거 — %s", reason)
        try:
            best = await asyncio.get_event_loop().run_in_executor(
                None, self._optimizer.run_cycle, recent
            )
            self._optimizer.apply_to_env(best, env_path=self.ENV_PATH)
            self._optimizer.apply_to_config(best, config_path=self.CONFIG_PATH)
            self._last_retrain_ts = time.time()
            logger.info(
                "OptimizerScheduler: 파라미터 업데이트 완료 (fitness=%.4f)\n%s",
                best.fitness, best.summary(),
            )
        except Exception as exc:
            logger.error("OptimizerScheduler: 재학습 실패 — %s", exc, exc_info=True)

    def stop(self) -> None:
        self._running = False


# --- System orchestrator ---

class TradingSystem:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        if dry_run:
            logger.warning("=== PAPER TRADING (DRY-RUN) 모드 — 실제 주문 없음 ===")

        # 상태 저장소 & 리스크 관리자 (가장 먼저 초기화)
        self.state_mgr = StateManager()
        self.risk_cfg = RiskConfig()
        self.risk_mgr = RiskManager(self.state_mgr, self.risk_cfg)

        # 단계 1: 인증
        self.auth_a = EdgeXAuth()
        self.auth_b = EdgeXAuth()  # 별도 지갑: PRIVATE_KEY_B env 설정 시 분리 가능

        # 단계 2: 데이터 수집기
        self.symbol = os.getenv("SYMBOL", "BTC-USDC")
        self.fetcher = DataFetcher(symbol=self.symbol)

        # 단계 3: 엔진 A (저장된 상태 복원)
        self.engine_a = EngineA(auth=self.auth_a, symbol=self.symbol, dry_run=dry_run)
        self.engine_a.set_capital(TOTAL_CAPITAL)
        saved_a = self.state_mgr.load_engine_a_state()
        if saved_a:
            self.engine_a.state = saved_a
            logger.info("엔진 A 상태 복원 완료 (mode=%s)", saved_a.mode.name)

        # 단계 4: 엔진 B (저장된 포지션 복원)
        self.engine_b = EngineB(auth=self.auth_b, dry_run=dry_run)
        self.engine_b.symbol = self.symbol
        self.engine_b.set_capital(TOTAL_CAPITAL)
        saved_b = self.state_mgr.load_engine_b_position()
        if saved_b:
            self.engine_b.position = saved_b
            logger.info("엔진 B 포지션 복원 완료 (short=%.4f)", saved_b.edgex_short_size)

        # 단계 5: 스위퍼
        self.sweeper = Sweeper(self.engine_a, self.engine_b, dry_run=dry_run)
        self.sweeper.set_initial_capital(TOTAL_CAPITAL * 0.25)
        saved_sweep = self.state_mgr.load_sweep_history()
        if saved_sweep:
            self.sweeper.state.history = saved_sweep
            self.sweeper.state.total_swept = sum(r.amount_usdc for r in saved_sweep)

        # 단계 6: 모니터 (engine_a 참조 전달 → 박스 동적 확대)
        self.monitor = Monitor(engine_a=self.engine_a)

        # 단계 8: 유전 알고리즘 자가 학습 옵티마이저
        _opt = Optimizer(seed=int(os.getenv("OPTIMIZER_SEED", "0")) or None)
        # 기존 config.json에 저장된 최적 파라미터가 있으면 기본값으로 로드
        _saved_genome = Optimizer.load_from_config()
        if _saved_genome is not None:
            logger.info(
                "Optimizer: 저장된 파라미터 로드 (fitness=%.4f)", _saved_genome.fitness
            )
        self.opt_scheduler = OptimizerScheduler(_opt)

        # 이벤트 연결
        self.fetcher.on_candle_update(self._on_candle)
        self.monitor.on_drift(self._on_drift)

    # --- Event handlers ---

    async def _on_candle(self, market) -> None:
        # 리스크 체크 후 엔진 실행
        if not self.risk_mgr.is_in_cooldown():
            await self.engine_a.on_market_update(market)
            await self.engine_b.on_market_update(market)
        else:
            logger.warning("리스크 쿨다운 중 — 엔진 일시 정지 (일일 손실=%.2f)", self.risk_mgr.get_daily_loss())
        await self.monitor.on_market_update(market)

        # 옵티마이저 캔들 버퍼에 공급 (최신 캔들이 있을 때)
        if market.candles:
            self.opt_scheduler.feed_candle(market.candles[-1])

        # 상태 주기적 저장 (매 캔들마다)
        try:
            self.state_mgr.save_engine_a_state(self.engine_a.state)
            self.state_mgr.save_engine_b_position(self.engine_b.position)
            self.state_mgr.save_sweep_history(self.sweeper.state.history)
        except Exception as exc:
            logger.warning("상태 저장 실패: %s", exc)

    def _on_drift(self, event) -> None:
        if event.level == DriftLevel.CRITICAL:
            logger.critical("CRITICAL DRIFT — 엔진 A 박스 취소 및 포지션 재평가 권고")

    # --- Initialization ---

    async def _initialize(self) -> None:
        logger.info("시스템 초기화 중...")

        # 잔고 확인
        try:
            usdc_a = self.auth_a.get_usdc_balance()
            logger.info("엔진 A 지갑 USDC 잔고: %.2f", usdc_a)
        except Exception as exc:
            logger.warning("잔고 조회 실패 (testnet RPC 문제일 수 있음): %s", exc)

        # 엔진 B 최적 페어 탐색 및 헷지 진입
        try:
            pair = self.engine_b.select_best_pair()
            if pair:
                logger.info("엔진 B 최적 페어: %s (APR %.2f%%)", pair["edgex_symbol"], pair["apr"])
        except Exception as exc:
            logger.warning("펀딩비 APR 조회 실패: %s", exc)

        logger.info("초기화 완료")

    # --- Main run loop ---

    async def run(self) -> None:
        await self._initialize()

        tasks = [
            asyncio.create_task(
                run_with_retry(lambda: self.fetcher.start(), "DataFetcher")
            ),
            asyncio.create_task(
                run_with_retry(lambda: self.sweeper.run(), "Sweeper")
            ),
            asyncio.create_task(
                run_with_retry(
                    lambda: self.monitor.run_periodic_calibration(), "Monitor-Calibration"
                )
            ),
            asyncio.create_task(
                run_with_retry(
                    lambda: self.risk_mgr.reset_daily_at_midnight(), "RiskManager-Reset"
                )
            ),
            asyncio.create_task(
                run_with_retry(lambda: self.opt_scheduler.run(), "OptimizerScheduler")
            ),
        ]

        logger.info("=== edgeX 하이브리드 트레이딩 시스템 가동 ===")
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("시스템 종료 요청")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.fetcher.stop()
            self.sweeper.stop()
            self.monitor.stop()
            self.opt_scheduler.stop()
            logger.info("시스템 정상 종료")


# --- CLI entry point ---

def parse_args():
    parser = argparse.ArgumentParser(description="edgeX 하이브리드 트레이딩 시스템")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Paper Trading 모드 (실제 주문 전송 없음)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="로그 레벨",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    system = TradingSystem(dry_run=args.dry_run)
    try:
        asyncio.run(system.run())
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트 — 종료")


if __name__ == "__main__":
    main()
