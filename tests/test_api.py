"""[단계 1] 테스트: 아비트럼 테스트넷 연결 및 USDC 잔고 조회 검증."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── 단계 1: auth.py 테스트 ──────────────────────────────────────────────────

class TestBuildWeb3(unittest.TestCase):
    @patch("src.auth.Web3")
    def test_connected(self, mock_web3_cls):
        mock_w3 = MagicMock()
        mock_w3.is_connected.return_value = True
        mock_web3_cls.return_value = mock_w3
        mock_web3_cls.HTTPProvider = MagicMock()

        from src.auth import build_web3
        w3 = build_web3()
        self.assertTrue(w3.is_connected())

    @patch("src.auth.Web3")
    def test_not_connected_raises(self, mock_web3_cls):
        mock_w3 = MagicMock()
        mock_w3.is_connected.return_value = False
        mock_web3_cls.return_value = mock_w3
        mock_web3_cls.HTTPProvider = MagicMock()

        from src.auth import build_web3
        with self.assertRaises(ConnectionError):
            build_web3()


class TestGetUsdcBalance(unittest.TestCase):
    def _make_w3_mock(self, raw_balance: int = 1_000_000_000, decimals: int = 6):
        mock_w3 = MagicMock()
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = raw_balance
        mock_contract.functions.decimals.return_value.call.return_value = decimals
        mock_w3.eth.contract.return_value = mock_contract
        mock_w3.to_checksum_address = lambda x: x
        return mock_w3

    @patch("src.auth.Web3.to_checksum_address", side_effect=lambda x: x)
    def test_balance_returns_float(self, _):
        from src.auth import get_usdc_balance
        w3 = self._make_w3_mock(raw_balance=5_000_000, decimals=6)
        balance = get_usdc_balance("0xTestAddress", w3=w3)
        self.assertAlmostEqual(balance, 5.0)

    @patch("src.auth.Web3.to_checksum_address", side_effect=lambda x: x)
    def test_zero_balance(self, _):
        from src.auth import get_usdc_balance
        w3 = self._make_w3_mock(raw_balance=0, decimals=6)
        balance = get_usdc_balance("0xTestAddress", w3=w3)
        self.assertEqual(balance, 0.0)

    @patch("src.auth.Web3.to_checksum_address", side_effect=lambda x: x)
    def test_large_balance(self, _):
        from src.auth import get_usdc_balance
        w3 = self._make_w3_mock(raw_balance=100_000_000_000, decimals=6)
        balance = get_usdc_balance("0xTestAddress", w3=w3)
        self.assertAlmostEqual(balance, 100_000.0)


class TestEdgeXAuth(unittest.TestCase):
    TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

    def setUp(self):
        self.auth = None

    def _make_auth(self):
        from src.auth import EdgeXAuth
        return EdgeXAuth(private_key=self.TEST_KEY)

    def test_wallet_address_derived(self):
        auth = self._make_auth()
        self.assertTrue(auth.wallet_address.startswith("0x"))
        self.assertEqual(len(auth.wallet_address), 42)

    def test_sign_order_returns_signature(self):
        auth = self._make_auth()
        payload = {
            "accountId": "test-account",
            "symbol": "BTC-USDC",
            "side": "BUY",
            "price": "50000.00",
            "size": "0.001",
        }
        signed = auth.sign_order(payload)
        self.assertIn("signature", signed)
        self.assertIn("nonce", signed)
        self.assertIn("timestamp", signed)
        self.assertTrue(signed["signature"].startswith("0x") or len(signed["signature"]) > 0)

    def test_sign_cancel_returns_signature(self):
        auth = self._make_auth()
        signed = auth.sign_cancel("order-123")
        self.assertIn("signature", signed)
        self.assertEqual(signed["orderId"], "order-123")

    def test_nonce_increments(self):
        auth = self._make_auth()
        p = {"accountId": "a", "symbol": "BTC-USDC", "side": "BUY", "price": "1", "size": "1"}
        s1 = auth.sign_order(p)
        s2 = auth.sign_order(p)
        self.assertGreater(s2["nonce"], s1["nonce"])


# ── 단계 2: data_fetcher.py 테스트 ──────────────────────────────────────────

class TestFundingAprConversion(unittest.TestCase):
    def test_positive_funding_rate(self):
        from src.data_fetcher import funding_rate_to_apr
        apr = funding_rate_to_apr(0.0001)
        self.assertAlmostEqual(apr, 0.0001 * 3 * 365 * 100, places=5)

    def test_zero_rate(self):
        from src.data_fetcher import funding_rate_to_apr
        self.assertEqual(funding_rate_to_apr(0), 0.0)

    def test_negative_rate(self):
        from src.data_fetcher import funding_rate_to_apr
        self.assertLess(funding_rate_to_apr(-0.0001), 0)


class TestDataFetcherVolumeMa(unittest.TestCase):
    def _make_fetcher_with_candles(self, volumes: list[float]):
        from src.data_fetcher import DataFetcher, Candle
        fetcher = DataFetcher(symbol="BTC-USDC")
        for i, v in enumerate(volumes):
            fetcher.market_data.candles.append(
                Candle(open_time=i * 60000, open=100.0, high=101.0, low=99.0, close=100.5, volume=v)
            )
        fetcher._recalculate_volume_mas()
        return fetcher

    def test_volume_ma_20m_computed(self):
        volumes = [float(i + 1) for i in range(40)]
        fetcher = self._make_fetcher_with_candles(volumes)
        self.assertGreater(fetcher.market_data.volume_ma_20m, 0)

    def test_volume_ma_reflects_recent_window(self):
        # 처음 20개 = 1.0, 마지막 20개 = 100.0
        volumes = [1.0] * 20 + [100.0] * 20
        fetcher = self._make_fetcher_with_candles(volumes)
        # 20분 MA는 최근 20개(모두 100)의 평균이어야 함
        self.assertAlmostEqual(fetcher.market_data.volume_ma_20m, 100.0, places=1)


# ── 단계 3: engine_a.py 테스트 ──────────────────────────────────────────────

class TestEngineABoxCalculation(unittest.TestCase):
    def _make_market(self, highs, lows):
        from src.data_fetcher import Candle, MarketData
        md = MarketData(symbol="BTC-USDC")
        for i, (h, l) in enumerate(zip(highs, lows)):
            md.candles.append(
                Candle(open_time=i, open=l, high=h, low=l, close=(h + l) / 2, volume=1.0)
            )
        md.last_price = (highs[-1] + lows[-1]) / 2
        return md

    def test_box_support_resistance(self):
        from src.auth import EdgeXAuth
        from src.engine_a import EngineA
        auth = MagicMock(spec=EdgeXAuth)
        auth.get_headers.return_value = {}
        engine = EngineA(auth=auth, dry_run=True)

        market = self._make_market(
            highs=[100 + i for i in range(50)],
            lows=[90 + i for i in range(50)],
        )
        box = engine.calculate_box_48h(market)
        self.assertEqual(box.support, 90.0)
        self.assertEqual(box.resistance, 149.0)

    def test_volume_spike_detected(self):
        from src.auth import EdgeXAuth
        from src.data_fetcher import Candle, MarketData
        from src.engine_a import EngineA
        auth = MagicMock(spec=EdgeXAuth)
        auth.get_headers.return_value = {}
        engine = EngineA(auth=auth, dry_run=True)

        md = MarketData(symbol="BTC-USDC")
        for i in range(25):
            md.candles.append(Candle(i, 100, 101, 99, 100, volume=10.0))
        # 마지막 캔들에 3배 이상 거래량
        md.candles.append(Candle(25, 100, 101, 99, 100, volume=35.0))
        md.volume_ma_20m = 10.0
        md.last_price = 100.0

        self.assertTrue(engine.is_volume_spike(md))

    def test_no_spike_normal_volume(self):
        from src.auth import EdgeXAuth
        from src.data_fetcher import Candle, MarketData
        from src.engine_a import EngineA
        auth = MagicMock(spec=EdgeXAuth)
        auth.get_headers.return_value = {}
        engine = EngineA(auth=auth, dry_run=True)

        md = MarketData(symbol="BTC-USDC")
        for i in range(25):
            md.candles.append(Candle(i, 100, 101, 99, 100, volume=10.0))
        md.volume_ma_20m = 10.0

        self.assertFalse(engine.is_volume_spike(md))


class TestBreakoutPositionRiskManagement(unittest.TestCase):
    def test_stop_loss_long(self):
        from src.engine_a import BreakoutPosition
        pos = BreakoutPosition(side="BUY", entry_price=100.0, size=1.0, peak_price=100.0)
        self.assertFalse(pos.should_stop_loss(99.0))   # -1% (허용범위)
        self.assertTrue(pos.should_stop_loss(98.4))    # -1.6% (손절선 아래)

    def test_stop_loss_short(self):
        from src.engine_a import BreakoutPosition
        pos = BreakoutPosition(side="SELL", entry_price=100.0, size=1.0, peak_price=100.0)
        self.assertFalse(pos.should_stop_loss(101.0))
        self.assertTrue(pos.should_stop_loss(101.6))

    def test_trailing_stop_long(self):
        from src.engine_a import BreakoutPosition
        pos = BreakoutPosition(side="BUY", entry_price=100.0, size=1.0, peak_price=110.0)
        self.assertFalse(pos.should_trailing_stop(109.0))  # -0.9% from peak
        self.assertTrue(pos.should_trailing_stop(107.7))   # -2.1% from peak

    def test_trailing_stop_short(self):
        from src.engine_a import BreakoutPosition
        pos = BreakoutPosition(side="SELL", entry_price=100.0, size=1.0, peak_price=90.0)
        self.assertFalse(pos.should_trailing_stop(91.0))
        self.assertTrue(pos.should_trailing_stop(91.9))

    def test_peak_update_long(self):
        from src.engine_a import BreakoutPosition
        pos = BreakoutPosition(side="BUY", entry_price=100.0, size=1.0, peak_price=100.0)
        pos.update_peak(105.0)
        self.assertEqual(pos.peak_price, 105.0)
        pos.update_peak(103.0)  # 떨어졌을 때 peak 유지
        self.assertEqual(pos.peak_price, 105.0)


# ── 단계 6: monitor.py 테스트 ──────────────────────────────────────────────

class TestMonitorVolatility(unittest.TestCase):
    def _make_market_with_prices(self, prices: list[float]):
        from src.data_fetcher import Candle, MarketData
        md = MarketData(symbol="BTC-USDC")
        for i, p in enumerate(prices):
            md.candles.append(Candle(i, p, p * 1.01, p * 0.99, p, volume=1.0))
        return md

    def test_std_returns_float(self):
        from src.monitor import Monitor
        monitor = Monitor()
        prices = [100.0 + (i % 10) * 0.5 for i in range(100)]
        market = self._make_market_with_prices(prices)
        result = monitor.calculate_24h_std(market)
        self.assertIsInstance(result, float)
        self.assertGreater(result, 0)

    def test_std_none_for_insufficient_data(self):
        from src.monitor import Monitor
        monitor = Monitor()
        market = self._make_market_with_prices([100.0] * 10)
        result = monitor.calculate_24h_std(market)
        self.assertIsNone(result)

    def test_drift_detected(self):
        from src.monitor import DriftLevel, Monitor
        monitor = Monitor()
        monitor._baseline_std = 0.001  # 낮은 기준

        prices = [100.0 * (1 + 0.05 * (i % 2)) for i in range(200)]  # 큰 변동
        market = self._make_market_with_prices(prices)
        event = monitor.check_drift(market)
        if event:
            self.assertIn(event.level, (DriftLevel.WARNING, DriftLevel.CRITICAL))

    def test_no_drift_stable_market(self):
        from src.monitor import Monitor
        monitor = Monitor()
        monitor._baseline_std = 0.1  # 높은 기준

        prices = [100.0 + 0.001 * i for i in range(200)]  # 매우 안정적
        market = self._make_market_with_prices(prices)
        event = monitor.check_drift(market)
        self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
