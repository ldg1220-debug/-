"""
tests/test_data_fetcher.py
DataFetcher 실시간 거래량 큐 및 스파이크 감지 단위 테스트.

검증 항목:
- _accumulate_tick_volume(): 분 경계 누적 및 큐 push 로직
- get_recent_average_volume(): 큐 평균 / REST MA 폴백
- is_volume_spike(): 스파이크 판정 (True/False) 및 경계값
- get_current_market_data(): 정상 응답, Timeout, 네트워크 오류, KeyError 처리
"""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from src.data_fetcher import (
    VOLUME_MA_MINUTES,
    VOLUME_SPIKE_MULTIPLIER,
    DataFetcher,
)


def make_fetcher() -> DataFetcher:
    return DataFetcher(symbol="BTC-USDC")


# ═══════════════════════════════════════════════════════════════════════════════
# _accumulate_tick_volume()  — 분봉 누적 및 큐 관리
# ═══════════════════════════════════════════════════════════════════════════════

class TestAccumulateTickVolume(unittest.TestCase):

    def test_first_tick_initializes_minute_ts(self):
        """첫 번째 틱이 _current_minute_ts를 현재 분 단위로 초기화해야 한다."""
        f = make_fetcher()
        self.assertEqual(f._current_minute_ts, 0)
        now_min = (int(time.time()) // 60) * 60
        f._accumulate_tick_volume(5.0)
        self.assertEqual(f._current_minute_ts, now_min)

    def test_same_minute_accumulates(self):
        """같은 분 안의 틱들은 합산되어야 한다."""
        f = make_fetcher()
        now_min = (int(time.time()) // 60) * 60
        f._current_minute_ts = now_min
        f._accumulate_tick_volume(3.0)
        f._accumulate_tick_volume(7.0)
        self.assertAlmostEqual(f._current_minute_volume, 10.0)

    def test_minute_boundary_pushes_to_queue(self):
        """분 경계를 넘으면 이전 분봉 거래량이 큐에 추가되어야 한다."""
        f = make_fetcher()
        old_min = (int(time.time()) // 60 - 2) * 60  # 2분 전
        f._current_minute_ts = old_min
        f._current_minute_volume = 42.0

        # 현재 분으로 새 틱 주입 → 경계 초과
        f._accumulate_tick_volume(1.0)

        self.assertEqual(len(f._minute_volume_queue), 1)
        self.assertAlmostEqual(f._minute_volume_queue[0], 42.0)

    def test_minute_boundary_resets_current_volume(self):
        """분 경계 초과 후 current_minute_volume이 새 틱 수량만 남아야 한다."""
        f = make_fetcher()
        f._current_minute_ts = (int(time.time()) // 60 - 1) * 60
        f._current_minute_volume = 100.0
        f._accumulate_tick_volume(2.5)
        self.assertAlmostEqual(f._current_minute_volume, 2.5)

    def test_queue_maxlen_is_volume_ma_minutes(self):
        """큐 최대 길이는 VOLUME_MA_MINUTES와 같아야 한다."""
        f = make_fetcher()
        self.assertEqual(f._minute_volume_queue.maxlen, VOLUME_MA_MINUTES)

    def test_queue_overflow_drops_oldest(self):
        """큐가 가득 찼을 때 가장 오래된 항목이 제거되어야 한다."""
        f = make_fetcher()
        # VOLUME_MA_MINUTES + 5개를 직접 채움
        for i in range(VOLUME_MA_MINUTES + 5):
            f._minute_volume_queue.append(float(i))
        self.assertEqual(len(f._minute_volume_queue), VOLUME_MA_MINUTES)
        # 가장 오래된 값(0..4)이 제거되고 5부터 시작해야 함
        self.assertAlmostEqual(f._minute_volume_queue[0], 5.0)

    def test_multiple_minute_boundaries(self):
        """여러 분 경계를 연속으로 넘겨도 큐에 순서대로 쌓여야 한다."""
        f = make_fetcher()
        base_min = (int(time.time()) // 60 - 5) * 60
        for step in range(4):
            f._current_minute_ts = base_min + step * 60
            f._current_minute_volume = float(step * 10)
            # 다음 분 틱 주입
            with patch("time.time", return_value=float(base_min + (step + 1) * 60 + 1)):
                f._accumulate_tick_volume(0.1)
        self.assertGreaterEqual(len(f._minute_volume_queue), 3)


# ═══════════════════════════════════════════════════════════════════════════════
# get_recent_average_volume()  — 평균 계산 및 폴백
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetRecentAverageVolume(unittest.TestCase):

    def test_empty_queue_falls_back_to_rest_ma(self):
        """큐가 비어 있으면 REST 폴 기반 volume_ma_20m을 반환해야 한다."""
        f = make_fetcher()
        f.market_data.volume_ma_20m = 55.5
        self.assertAlmostEqual(f.get_recent_average_volume(), 55.5)

    def test_queue_average_correct(self):
        """큐에 값이 있으면 산술 평균을 반환해야 한다."""
        f = make_fetcher()
        for v in [10.0, 20.0, 30.0]:
            f._minute_volume_queue.append(v)
        self.assertAlmostEqual(f.get_recent_average_volume(), 20.0)

    def test_single_entry_queue(self):
        """큐에 항목이 1개일 때도 정상 반환해야 한다."""
        f = make_fetcher()
        f._minute_volume_queue.append(99.0)
        self.assertAlmostEqual(f.get_recent_average_volume(), 99.0)

    def test_full_queue_average(self):
        """VOLUME_MA_MINUTES개 항목의 평균을 정확히 계산해야 한다."""
        f = make_fetcher()
        values = [float(i) for i in range(1, VOLUME_MA_MINUTES + 1)]
        for v in values:
            f._minute_volume_queue.append(v)
        expected = sum(values) / len(values)
        self.assertAlmostEqual(f.get_recent_average_volume(), expected, places=5)

    def test_queue_takes_priority_over_rest_ma(self):
        """큐에 데이터가 있으면 REST MA(volume_ma_20m)를 무시해야 한다."""
        f = make_fetcher()
        f.market_data.volume_ma_20m = 999.0  # 큰 값 세팅
        f._minute_volume_queue.append(10.0)
        self.assertAlmostEqual(f.get_recent_average_volume(), 10.0)


# ═══════════════════════════════════════════════════════════════════════════════
# is_volume_spike()  — 스파이크 판정
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsVolumeSpike(unittest.TestCase):

    def _setup_avg(self, f: DataFetcher, avg: float) -> None:
        """큐 평균을 avg로 설정하는 헬퍼."""
        f._minute_volume_queue.append(avg)

    def test_no_spike_below_threshold(self):
        """현재 분 거래량 < avg × multiplier → False."""
        f = make_fetcher()
        self._setup_avg(f, 100.0)
        f._current_minute_volume = 250.0   # 2.5배 (< 3.0배)
        self.assertFalse(f.is_volume_spike(multiplier=3.0))

    def test_spike_above_threshold(self):
        """현재 분 거래량 >= avg × multiplier → True."""
        f = make_fetcher()
        self._setup_avg(f, 100.0)
        f._current_minute_volume = 300.0   # 정확히 3.0배
        self.assertTrue(f.is_volume_spike(multiplier=3.0))

    def test_spike_strictly_above(self):
        """3배 초과(301)도 True여야 한다."""
        f = make_fetcher()
        self._setup_avg(f, 100.0)
        f._current_minute_volume = 301.0
        self.assertTrue(f.is_volume_spike(multiplier=3.0))

    def test_no_spike_when_avg_zero(self):
        """평균이 0이면 스파이크 판정 불가 → False (ZeroDivision 방지)."""
        f = make_fetcher()
        f.market_data.volume_ma_20m = 0.0
        f._current_minute_volume = 9999.0
        self.assertFalse(f.is_volume_spike())

    def test_custom_multiplier(self):
        """커스텀 배수(2.0)로도 정상 판정해야 한다."""
        f = make_fetcher()
        self._setup_avg(f, 100.0)
        f._current_minute_volume = 200.0
        self.assertTrue(f.is_volume_spike(multiplier=2.0))
        f._current_minute_volume = 199.9
        self.assertFalse(f.is_volume_spike(multiplier=2.0))

    def test_uses_env_default_multiplier(self):
        """기본 인자(VOLUME_SPIKE_MULTIPLIER 환경변수)가 적용되어야 한다."""
        f = make_fetcher()
        self._setup_avg(f, 10.0)
        # VOLUME_SPIKE_MULTIPLIER=3.0 기본값: 30.0 이상이면 True
        f._current_minute_volume = 10.0 * VOLUME_SPIKE_MULTIPLIER
        self.assertTrue(f.is_volume_spike())

    def test_queue_empty_uses_rest_ma_for_avg(self):
        """큐가 비어 있을 때 REST MA 기반 평균으로 스파이크 판정해야 한다."""
        f = make_fetcher()
        f.market_data.volume_ma_20m = 50.0
        f._current_minute_volume = 200.0   # 4배 (> 3배)
        self.assertTrue(f.is_volume_spike(multiplier=3.0))


# ═══════════════════════════════════════════════════════════════════════════════
# get_current_market_data()  — REST 스냅샷
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetCurrentMarketData(unittest.TestCase):

    def _mock_response(self, json_data: dict, status: int = 200):
        mock = MagicMock()
        mock.status_code = status
        mock.json.return_value = json_data
        mock.raise_for_status = MagicMock()
        return mock

    def test_successful_response_returns_dict(self):
        """정상 응답 시 필수 키를 가진 dict를 반환해야 한다."""
        f = make_fetcher()
        with patch("requests.get") as mock_get:
            mock_get.return_value = self._mock_response(
                {"lastPrice": "95000.5", "volume24h": "12345.67"}
            )
            result = f.get_current_market_data()

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["current_price"], 95000.5)
        self.assertAlmostEqual(result["volume_24h"], 12345.67)
        self.assertEqual(result["symbol"], "BTC-USDC")
        self.assertIn("timestamp", result)

    def test_price_is_float_not_string(self):
        """API가 문자열로 반환해도 float으로 변환되어야 한다."""
        f = make_fetcher()
        with patch("requests.get") as mock_get:
            mock_get.return_value = self._mock_response(
                {"lastPrice": "100000.0", "volume24h": "500.0"}
            )
            result = f.get_current_market_data()

        self.assertIsInstance(result["current_price"], float)
        self.assertIsInstance(result["volume_24h"], float)

    def test_timeout_returns_none(self):
        """Timeout 발생 시 None을 반환해야 한다 (봇 중단 없음)."""
        import requests as req
        f = make_fetcher()
        with patch("requests.get", side_effect=req.exceptions.Timeout):
            result = f.get_current_market_data()
        self.assertIsNone(result)

    def test_request_exception_returns_none(self):
        """일반 네트워크 오류 시 None을 반환해야 한다."""
        import requests as req
        f = make_fetcher()
        with patch("requests.get", side_effect=req.exceptions.ConnectionError("refused")):
            result = f.get_current_market_data()
        self.assertIsNone(result)

    def test_key_error_returns_none(self):
        """거래소 응답 포맷이 바뀌어 KeyError가 발생해도 None을 반환해야 한다."""
        f = make_fetcher()
        with patch("requests.get") as mock_get:
            # 필수 키가 없는 응답
            mock_get.return_value = self._mock_response({"price": "100", "vol": "10"})
            result = f.get_current_market_data()
        self.assertIsNone(result)

    def test_timeout_parameter_is_5_seconds(self):
        """requests.get 호출 시 timeout=5가 전달되어야 한다."""
        f = make_fetcher()
        with patch("requests.get") as mock_get:
            mock_get.return_value = self._mock_response(
                {"lastPrice": "1.0", "volume24h": "1.0"}
            )
            f.get_current_market_data()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs.get("timeout"), 5)


# ═══════════════════════════════════════════════════════════════════════════════
# 통합 시나리오: 틱 누적 → 스파이크 감지 전체 흐름
# ═══════════════════════════════════════════════════════════════════════════════

class TestVolumeIntegration(unittest.TestCase):

    def test_tick_accumulation_then_spike_detected(self):
        """20분 안정 베이스라인 쌓은 뒤 3배 급등 틱이 오면 스파이크 감지."""
        f = make_fetcher()
        # 안정 베이스라인: 20분 × 분당 10.0
        for _ in range(VOLUME_MA_MINUTES):
            f._minute_volume_queue.append(10.0)

        # 현재 분봉에 30.0 누적 (평균 10의 정확히 3배)
        f._current_minute_volume = 30.0
        self.assertTrue(f.is_volume_spike(multiplier=3.0))

    def test_no_spike_before_threshold(self):
        """평균의 2.99배는 스파이크가 아니어야 한다."""
        f = make_fetcher()
        for _ in range(VOLUME_MA_MINUTES):
            f._minute_volume_queue.append(10.0)
        f._current_minute_volume = 29.9
        self.assertFalse(f.is_volume_spike(multiplier=3.0))

    def test_realtime_accumulate_across_minute_boundary(self):
        """분 경계 전후에 걸친 틱 스트림이 올바르게 분리되어야 한다."""
        f = make_fetcher()
        old_min = (int(time.time()) // 60 - 1) * 60
        now_min = old_min + 60

        # 이전 분: 누적 50
        f._current_minute_ts = old_min
        f._current_minute_volume = 50.0

        # 새 분의 첫 틱 → 이전 분 50이 큐에 들어가고, 현재는 7.0
        with patch("time.time", return_value=float(now_min + 5)):
            f._accumulate_tick_volume(7.0)

        self.assertAlmostEqual(list(f._minute_volume_queue)[-1], 50.0)
        self.assertAlmostEqual(f._current_minute_volume, 7.0)

    def test_average_volume_updates_after_spike_passes(self):
        """스파이크 분봉이 큐에 편입된 뒤 평균에 반영되어야 한다."""
        f = make_fetcher()
        # 19분은 10.0
        for _ in range(19):
            f._minute_volume_queue.append(10.0)
        # 1분은 스파이크: 100.0
        f._minute_volume_queue.append(100.0)

        avg = f.get_recent_average_volume()
        # 평균 = (19*10 + 100) / 20 = 290/20 = 14.5
        self.assertAlmostEqual(avg, 14.5, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
