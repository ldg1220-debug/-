"""Global risk guard: halts trading when configurable limits are exceeded."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DAILY_LOSS_KEY = "risk_daily_loss"


@dataclass
class RiskConfig:
    max_daily_loss_usdc: float = 500.0
    max_drawdown_pct: float = 0.10
    max_position_size_usdc: float = 5000.0
    cooldown_after_loss_sec: float = 1800.0


class RiskManager:
    def __init__(self, state_manager, config: RiskConfig | None = None) -> None:
        self._sm = state_manager
        self.config = config or RiskConfig()

        # Restore daily loss from persisted trades so a restart doesn't reset it.
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        self._daily_loss: float = abs(min(0.0, self._sm.get_daily_pnl(today)))

        self._cooldown_until: float = 0.0  # epoch seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_order(self, side: str, size: float, price: float) -> bool:
        """Return True if the order is within risk limits; False (and log) otherwise."""
        import time

        # 1. Cooldown check
        if self.is_in_cooldown():
            remaining = self._cooldown_until - time.time()
            logger.warning(
                "ORDER BLOCKED — in cooldown (%.0f s remaining)", remaining
            )
            return False

        # 2. Daily loss limit
        if self._daily_loss >= self.config.max_daily_loss_usdc:
            logger.warning(
                "ORDER BLOCKED — daily loss limit reached "
                "(loss=%.2f, limit=%.2f USDC)",
                self._daily_loss,
                self.config.max_daily_loss_usdc,
            )
            return False

        # 3. Position size limit
        notional = size * price
        if notional > self.config.max_position_size_usdc:
            logger.warning(
                "ORDER BLOCKED — position size %.2f USDC exceeds limit %.2f USDC",
                notional,
                self.config.max_position_size_usdc,
            )
            return False

        return True

    def record_loss(self, amount: float) -> None:
        """Add *amount* (positive number) to today's loss tracker.

        Triggers a cooldown if the daily loss limit is hit.
        """
        import time

        if amount <= 0:
            return

        self._daily_loss += amount
        logger.info(
            "Loss recorded: %.4f USDC | daily total: %.4f USDC (limit %.2f)",
            amount,
            self._daily_loss,
            self.config.max_daily_loss_usdc,
        )

        if self._daily_loss >= self.config.max_daily_loss_usdc:
            self._cooldown_until = time.time() + self.config.cooldown_after_loss_sec
            logger.warning(
                "Daily loss limit breached — trading halted for %.0f s",
                self.config.cooldown_after_loss_sec,
            )

    def is_in_cooldown(self) -> bool:
        """Return True while the cooldown window is active."""
        import time
        return time.time() < self._cooldown_until

    def get_daily_loss(self) -> float:
        """Return the accumulated loss for today (positive = loss)."""
        return self._daily_loss

    # ------------------------------------------------------------------
    # Async midnight reset task
    # ------------------------------------------------------------------

    async def reset_daily_at_midnight(self) -> None:
        """Async task: runs forever, resetting daily loss at UTC 00:00."""
        while True:
            seconds_until_midnight = self._seconds_until_utc_midnight()
            logger.debug(
                "Next daily loss reset in %.0f s", seconds_until_midnight
            )
            await asyncio.sleep(seconds_until_midnight)
            self._daily_loss = 0.0
            self._cooldown_until = 0.0
            logger.info("Daily loss counter reset at UTC midnight.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seconds_until_utc_midnight(self) -> float:
        now = datetime.now(tz=timezone.utc)
        tomorrow_midnight = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # If it's exactly midnight add a full day to avoid an immediate re-fire.
        delta = (tomorrow_midnight - now).total_seconds()
        if delta <= 0:
            delta += 86400.0
        return delta
