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
from src.sweeper import Sweeper

MAX_RETRY_DELAY = 60  # 최대 재연결 대기(초)
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "10000.0"))


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


# --- System orchestrator ---

class TradingSystem:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        if dry_run:
            logger.warning("=== PAPER TRADING (DRY-RUN) 모드 — 실제 주문 없음 ===")

        # 단계 1: 인증
        self.auth_a = EdgeXAuth()
        self.auth_b = EdgeXAuth()  # 별도 지갑 키를 사용할 경우 PRIVATE_KEY_B 등 env 분리 가능

        # 단계 2: 데이터 수집기
        self.symbol = os.getenv("SYMBOL", "BTC-USDC")
        self.fetcher = DataFetcher(symbol=self.symbol)

        # 단계 3: 엔진 A
        self.engine_a = EngineA(auth=self.auth_a, symbol=self.symbol, dry_run=dry_run)
        self.engine_a.set_capital(TOTAL_CAPITAL)

        # 단계 4: 엔진 B
        self.engine_b = EngineB(auth=self.auth_b, dry_run=dry_run)
        self.engine_b.symbol = self.symbol
        self.engine_b.set_capital(TOTAL_CAPITAL)

        # 단계 5: 스위퍼
        self.sweeper = Sweeper(self.engine_a, self.engine_b, dry_run=dry_run)
        self.sweeper.set_initial_capital(TOTAL_CAPITAL * 0.25)

        # 단계 6: 모니터 (engine_a 참조 전달 → 박스 동적 확대)
        self.monitor = Monitor(engine_a=self.engine_a)

        # 이벤트 연결
        self.fetcher.on_candle_update(self._on_candle)
        self.monitor.on_drift(self._on_drift)

    # --- Event handlers ---

    async def _on_candle(self, market) -> None:
        await self.engine_a.on_market_update(market)
        await self.engine_b.on_market_update(market)
        await self.monitor.on_market_update(market)

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
