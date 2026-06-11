"""[단계 5] 자금 스위프: 엔진 A 수익금 → 엔진 B 온체인 이체 (가스비 최적화)."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import geth_poa_middleware

load_dotenv()
logger = logging.getLogger(__name__)

SWEEP_THRESHOLD = float(os.getenv("SWEEP_THRESHOLD", "100.0"))
SWEEP_INITIAL_PROFIT_PCT = float(os.getenv("SWEEP_INITIAL_PROFIT_PCT", "0.10"))
SWEEP_INTERVAL_SECONDS = int(os.getenv("SWEEP_INTERVAL_SECONDS", "3600"))
WEEKLY_SWEEP_DOW = int(os.getenv("WEEKLY_SWEEP_DOW", "6"))  # 0=월, 6=일

ARB_RPC = os.getenv("ARB_RPC", "https://sepolia-rollup.arbitrum.io/rpc")
USDC_ADDRESS = os.getenv("USDC_CONTRACT", "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d")

# GAS_PRICE_MULTIPLIER: 시장가 대비 배수 (1.1 = 10% 프리미엄으로 빠른 처리)
GAS_PRICE_MULTIPLIER = float(os.getenv("GAS_PRICE_MULTIPLIER", "1.1"))

USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass
class SweepRecord:
    timestamp: float
    amount_usdc: float
    tx_hash: str
    gas_used: int = 0
    gas_price_gwei: float = 0.0


@dataclass
class SweeperState:
    total_swept: float = 0.0
    history: list[SweepRecord] = field(default_factory=list)
    initial_capital_a: float = 0.0


class Sweeper:
    def __init__(self, engine_a, engine_b, dry_run: bool = False):
        self.engine_a = engine_a
        self.engine_b = engine_b
        self.dry_run = dry_run
        self.state = SweeperState()
        self._running = False

        self._private_key = os.getenv("PRIVATE_KEY", "")
        self._wallet = Account.from_key(self._private_key) if self._private_key else None

        self._w3 = Web3(Web3.HTTPProvider(ARB_RPC, request_kwargs={"timeout": 30}))
        self._w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        self._usdc = self._w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI
        )

    def set_initial_capital(self, capital_a: float) -> None:
        self.state.initial_capital_a = capital_a

    # --- Condition checks ---

    def _profit_threshold_exceeded(self) -> bool:
        pnl = self.engine_a.get_realized_pnl()
        return pnl >= SWEEP_THRESHOLD

    def _profit_pct_exceeded(self) -> bool:
        if self.state.initial_capital_a <= 0:
            return False
        pnl = self.engine_a.get_realized_pnl()
        return pnl / self.state.initial_capital_a >= SWEEP_INITIAL_PROFIT_PCT

    def _is_weekly_sweep_time(self) -> bool:
        from datetime import datetime
        now = datetime.utcnow()
        return now.weekday() == WEEKLY_SWEEP_DOW and now.hour == 0

    def should_sweep(self) -> bool:
        return (
            self._profit_threshold_exceeded()
            or self._profit_pct_exceeded()
            or self._is_weekly_sweep_time()
        )

    # --- Gas estimation ---

    def _get_optimal_gas_price(self) -> int:
        base_gwei = self._w3.eth.gas_price
        return int(base_gwei * GAS_PRICE_MULTIPLIER)

    def _estimate_gas(self, to_address: str, amount_raw: int) -> int:
        try:
            return self._usdc.functions.transfer(
                Web3.to_checksum_address(to_address), amount_raw
            ).estimate_gas({"from": self._wallet.address if self._wallet else "0x0"})
        except Exception:
            return 100_000  # 기본값

    # --- On-chain transfer ---

    def _transfer_usdc(self, amount_usdc: float, to_address: str) -> SweepRecord | None:
        if not self._wallet:
            logger.error("개인 키 없음 — 온체인 이체 불가")
            return None

        amount_raw = int(amount_usdc * 1e6)
        gas_price = self._get_optimal_gas_price()
        gas_limit = self._estimate_gas(to_address, amount_raw)

        if self.dry_run:
            logger.info(
                "[DRY-RUN] USDC %.2f → %s | gas=%d @ %.2f gwei",
                amount_usdc,
                to_address,
                gas_limit,
                gas_price / 1e9,
            )
            return SweepRecord(
                timestamp=time.time(),
                amount_usdc=amount_usdc,
                tx_hash="DRY-RUN",
                gas_used=gas_limit,
                gas_price_gwei=gas_price / 1e9,
            )

        nonce = self._w3.eth.get_transaction_count(self._wallet.address)
        tx = self._usdc.functions.transfer(
            Web3.to_checksum_address(to_address), amount_raw
        ).build_transaction(
            {
                "from": self._wallet.address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
            }
        )

        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        record = SweepRecord(
            timestamp=time.time(),
            amount_usdc=amount_usdc,
            tx_hash=tx_hash.hex(),
            gas_used=receipt.gasUsed,
            gas_price_gwei=gas_price / 1e9,
        )
        logger.info(
            "온체인 이체: %.2f USDC → %s | tx=%s | gas=%d @ %.2f gwei",
            amount_usdc,
            to_address,
            tx_hash.hex(),
            receipt.gasUsed,
            gas_price / 1e9,
        )
        return record

    def sweep(self) -> bool:
        if not self.should_sweep():
            return False

        pnl = self.engine_a.get_realized_pnl()
        if pnl <= 0:
            return False

        to_address = self.engine_b.auth.wallet_address
        record = self._transfer_usdc(pnl, to_address)

        if record:
            self.state.history.append(record)
            self.state.total_swept += pnl
            self.engine_a.state.realized_pnl = 0.0
            self.engine_b.allocated_capital += pnl
            logger.info("스위프 완료: +%.2f USDC | 누적=%.2f USDC", pnl, self.state.total_swept)
            return True
        return False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                self.sweep()
            except Exception as exc:
                logger.error("스위프 오류: %s", exc)

    def stop(self) -> None:
        self._running = False
