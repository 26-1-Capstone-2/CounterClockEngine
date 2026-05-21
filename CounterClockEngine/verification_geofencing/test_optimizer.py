"""
단위 테스트: next_interval 계산 로직 정확성 검증
- Cosine Blend Interval (거리 + 시간 긴급도)
- Activity Recognition
- Significant Location Change
- 통합 계산 (계산 예시 시나리오 포함)
"""

import sys
import math
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from optimizer import (
    haversine, estimate_activity,
    detect_significant_change, calculate_next_interval,
    classify_gps_mode, LocationPoint, Geofence,
    SLC_THRESHOLD_M, INTERVAL_MIN_S, INTERVAL_MAX_S, D_MAX_M, TIME_ALPHA,
    _cosine_blend_interval,
)


# ── 헬퍼: 위치 이력 생성 ──────────────────────────────────────
def make_history(points: list[tuple], interval_sec: int = 10) -> list[LocationPoint]:
    base_time = datetime(2026, 5, 11, 9, 0, 0)
    return [
        LocationPoint(lat, lon, base_time + timedelta(seconds=i * interval_sec))
        for i, (lat, lon) in enumerate(points)
    ]

GANGNAM  = (37.5012, 127.0276)
SAMSUNG_CENTER = (37.5088, 127.0456)
SAMSUNG_RADIUS = 200

SAMSUNG_FENCE = Geofence(
    id="samsung_station",
    lat=SAMSUNG_CENTER[0],
    lon=SAMSUNG_CENTER[1],
    radius=SAMSUNG_RADIUS,
)


# ══════════════════════════════════════════════════════════════
# 1. Haversine 거리 계산
# ══════════════════════════════════════════════════════════════
class TestHaversine(unittest.TestCase):

    def test_same_point_is_zero(self):
        self.assertAlmostEqual(haversine(37.5, 127.0, 37.5, 127.0), 0.0, places=2)

    def test_gangnam_to_samsung_approx_360m(self):
        p1 = (37.5000, 127.0000)
        p2 = (37.5030, 127.0020)
        dist = haversine(*p1, *p2)
        self.assertGreater(dist, 300)
        self.assertLess(dist, 450)

    def test_symmetry(self):
        d1 = haversine(37.5, 127.0, 37.6, 127.1)
        d2 = haversine(37.6, 127.1, 37.5, 127.0)
        self.assertAlmostEqual(d1, d2, places=5)

    def test_1km_north(self):
        dist = haversine(37.5, 127.0, 37.509, 127.0)
        self.assertGreater(dist, 900)
        self.assertLess(dist, 1100)


# ══════════════════════════════════════════════════════════════
# 2. Cosine Blend Interval
# ══════════════════════════════════════════════════════════════
class TestCosineBlend(unittest.TestCase):

    def test_urgency_zero_gives_max_interval(self):
        """긴급도=0 (아주 멀리, 시간 충분) → INTERVAL_MAX_S"""
        raw, u_dist, u_time, urgency = _cosine_blend_interval(
            distance_to_fence=D_MAX_M,
            eta_sec=10.0,
            appointment_remaining_sec=3600.0,
        )
        self.assertAlmostEqual(urgency, 0.0, places=1)
        self.assertAlmostEqual(raw, INTERVAL_MAX_S, delta=5)

    def test_urgency_one_gives_min_interval(self):
        """긴급도=1 (바로 앞, ETA=남은시간) → INTERVAL_MIN_S"""
        raw, u_dist, u_time, urgency = _cosine_blend_interval(
            distance_to_fence=0.0,
            eta_sec=60.0,
            appointment_remaining_sec=60.0,
        )
        self.assertAlmostEqual(urgency, 1.0, places=1)
        self.assertAlmostEqual(raw, INTERVAL_MIN_S, delta=1)

    def test_urgency_in_valid_range(self):
        """긴급도는 항상 [0, 1] 범위"""
        cases = [
            (0.0, 0.0, 0.0),
            (500.0, 300.0, 600.0),
            (D_MAX_M * 2, 10.0, 3600.0),   # 거리 초과
            (100.0, 9999.0, 60.0),          # eta > appointment
        ]
        for d, eta, appt in cases:
            _, _, _, urgency = _cosine_blend_interval(d, eta, appt)
            self.assertGreaterEqual(urgency, 0.0, msg=f"case {d},{eta},{appt}")
            self.assertLessEqual(urgency, 1.0, msg=f"case {d},{eta},{appt}")

    def test_interval_monotone_with_distance(self):
        """거리가 멀어질수록 interval이 길어짐"""
        distances = [50, 200, 500, 1000, 2000]
        intervals = [_cosine_blend_interval(d, None, None)[0] for d in distances]
        for i in range(len(intervals) - 1):
            self.assertLessEqual(intervals[i], intervals[i + 1],
                msg=f"interval should increase: d={distances[i]}→{distances[i+1]}")

    def test_time_urgency_without_info_mirrors_distance(self):
        """시간 정보 없으면 u_time == u_dist"""
        _, u_dist, u_time, _ = _cosine_blend_interval(800.0, None, None)
        self.assertAlmostEqual(u_dist, u_time, places=6)

    def test_time_urgency_high_eta_ratio(self):
        """eta가 약속 시간 전체와 같으면 u_time=1 (매우 촉박)"""
        _, _, u_time, _ = _cosine_blend_interval(1000.0, 600.0, 600.0)
        self.assertAlmostEqual(u_time, 1.0, places=3)

    def test_time_urgency_low_eta_ratio(self):
        """eta가 약속 시간의 10%면 u_time이 낮음 (여유 있음)"""
        _, _, u_time, _ = _cosine_blend_interval(1000.0, 60.0, 600.0)
        self.assertAlmostEqual(u_time, 0.1, places=3)

    def test_alpha_weight_balance(self):
        """alpha=1.0 이면 urgency == u_dist"""
        _, u_dist, _, _ = _cosine_blend_interval(500.0, None, None, alpha=1.0)
        _, _, _, urgency = _cosine_blend_interval(500.0, None, None, alpha=1.0)
        self.assertAlmostEqual(urgency, u_dist, places=5)


# ══════════════════════════════════════════════════════════════
# 3. Activity Recognition
# ══════════════════════════════════════════════════════════════
class TestActivityRecognition(unittest.TestCase):

    def test_single_point_is_unknown(self):
        history = make_history([GANGNAM])
        self.assertEqual(estimate_activity(history), "unknown")

    def test_no_movement_is_stationary(self):
        history = make_history([GANGNAM] * 5)
        self.assertEqual(estimate_activity(history), "stationary")

    def test_slow_movement_is_walking(self):
        points = [(37.5012 + i * 0.00009, 127.0300) for i in range(5)]
        history = make_history(points, interval_sec=10)
        self.assertEqual(estimate_activity(history), "walking")

    def test_fast_movement_is_vehicle(self):
        points = [(37.5012 + i * 0.0009, 127.0300) for i in range(5)]
        history = make_history(points, interval_sec=10)
        self.assertEqual(estimate_activity(history), "vehicle")

    def test_bus_scenario_is_vehicle(self):
        points = [
            (37.4980, 127.0210), (37.4990, 127.0240),
            (37.5000, 127.0270), (37.5008, 127.0290),
            (37.5012, 127.0300),
        ]
        history = make_history(points, interval_sec=10)
        self.assertEqual(estimate_activity(history), "vehicle")


# ══════════════════════════════════════════════════════════════
# 4. Significant Location Change
# ══════════════════════════════════════════════════════════════
class TestSLC(unittest.TestCase):

    def test_single_point_no_change(self):
        history = make_history([GANGNAM])
        is_sig, dist = detect_significant_change(history)
        self.assertFalse(is_sig)
        self.assertEqual(dist, 0.0)

    def test_no_movement_is_not_significant(self):
        history = make_history([GANGNAM] * 5)
        is_sig, dist = detect_significant_change(history)
        self.assertFalse(is_sig)
        self.assertLess(dist, SLC_THRESHOLD_M)

    def test_200m_movement_is_not_significant(self):
        points = [GANGNAM, (37.5012 + 0.0018, 127.0300)]
        history = make_history(points)
        is_sig, _ = detect_significant_change(history)
        self.assertFalse(is_sig)

    def test_800m_movement_is_significant(self):
        points = [GANGNAM, (37.5012 + 0.0072, 127.0300)]
        history = make_history(points)
        is_sig, dist = detect_significant_change(history)
        self.assertTrue(is_sig)
        self.assertGreater(dist, SLC_THRESHOLD_M)

    def test_bus_scenario_820m_is_significant(self):
        points = [
            (37.4980, 127.0210), (37.4990, 127.0240),
            (37.5000, 127.0270), (37.5008, 127.0290),
            (37.5012, 127.0300),
        ]
        history = make_history(points, interval_sec=10)
        is_sig, dist = detect_significant_change(history)
        self.assertTrue(is_sig)
        self.assertGreater(dist, 500)

    def test_custom_threshold(self):
        points = [GANGNAM, (37.5012 + 0.0018, 127.0300)]
        history = make_history(points)
        is_sig, _ = detect_significant_change(history, threshold=100)
        self.assertTrue(is_sig)


# ══════════════════════════════════════════════════════════════
# 5. 통합 계산 (핵심 시나리오)
# ══════════════════════════════════════════════════════════════
class TestIntegration(unittest.TestCase):

    def _make_bus_history(self):
        """버스 시나리오: 삼성역 존 방향으로 빠르게 이동 (경계 ~200m 앞 도착)"""
        points = [
            (37.5012, 127.0276), (37.5030, 127.0310),
            (37.5048, 127.0344), (37.5062, 127.0378),
            (37.5073, 127.0415),
        ]
        return make_history(points, interval_sec=10)

    def _make_stationary_history(self):
        """정지 시나리오: 존 경계까지 약 200m, 이동 없음"""
        near_fence = (37.5073, 127.0415)
        return make_history([near_fence] * 5)

    def test_bus_scenario_activity_is_vehicle(self):
        result = calculate_next_interval(
            37.5073, 127.0415, self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(result.activity, "vehicle")

    def test_bus_scenario_activity_multiplier(self):
        result = calculate_next_interval(
            37.5073, 127.0415, self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertAlmostEqual(result.activity_multiplier, 0.3)

    def test_bus_scenario_slc_is_significant(self):
        result = calculate_next_interval(
            37.5073, 127.0415, self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertTrue(result.is_significant_change)
        self.assertAlmostEqual(result.slc_multiplier, 1.0)

    def test_bus_scenario_high_urgency_near_fence(self):
        """버스 + 경계 근접 → 긴급도 높고(>0.8), interval 짧음(<10s)"""
        result = calculate_next_interval(
            37.5073, 127.0415, self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertGreater(result.urgency, 0.8)
        self.assertLess(result.next_interval, 10)
        self.assertEqual(result.gps_mode, "HIGH")

    def test_stationary_scenario_activity_is_stationary(self):
        result = calculate_next_interval(
            37.5073, 127.0415, self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(result.activity, "stationary")

    def test_stationary_scenario_slc_not_significant(self):
        result = calculate_next_interval(
            37.5073, 127.0415, self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertFalse(result.is_significant_change)
        self.assertAlmostEqual(result.slc_multiplier, 2.0)

    def test_stationary_scenario_interval_longer_than_bus(self):
        """정지 interval이 버스 interval보다 훨씬 김 (최소 5배)"""
        bus = calculate_next_interval(
            37.5073, 127.0415, self._make_bus_history(), [SAMSUNG_FENCE]
        )
        still = calculate_next_interval(
            37.5073, 127.0415, self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertGreater(still.next_interval, bus.next_interval * 5)

    def test_time_urgency_increases_frequency(self):
        """ETA가 약속 시간에 촉박할수록 interval이 줄어듦 (multiplier 누적 방지를 위해 vehicle 히스토리 사용)"""
        # vehicle 히스토리: activity_multiplier=0.3, SLC significant → slc_mult=1.0
        # → 총 배율이 작아 300s 캡에 걸리지 않음
        points = [(37.5012 + i * 0.0009, 127.0276) for i in range(6)]
        history = make_history(points, interval_sec=10)
        fence_mid = Geofence("mid", 37.52, 127.03, 100)

        # 여유 있음: eta 20분, 약속 3시간
        result_relaxed = calculate_next_interval(
            *GANGNAM, history, [fence_mid],
            eta_sec=1200, appointment_remaining_sec=10800,
        )
        # 촉박: eta 20분, 약속 22분
        result_urgent = calculate_next_interval(
            *GANGNAM, history, [fence_mid],
            eta_sec=1200, appointment_remaining_sec=1320,
        )
        self.assertGreater(result_relaxed.next_interval, result_urgent.next_interval)
        self.assertLess(result_relaxed.u_time, result_urgent.u_time)

    def test_max_interval_cap(self):
        """최대 300초 이상 넘지 않음"""
        far_fence = Geofence("far", 37.5012 + 0.1, 127.0300, 100)
        result = calculate_next_interval(
            *GANGNAM, self._make_stationary_history(), [far_fence]
        )
        self.assertLessEqual(result.next_interval, INTERVAL_MAX_S)

    def test_inside_geofence_detected(self):
        inside_point = (SAMSUNG_CENTER[0], SAMSUNG_CENTER[1])
        history = make_history([inside_point] * 3)
        result = calculate_next_interval(*inside_point, history, [SAMSUNG_FENCE])
        self.assertIn("samsung_station", result.entered_zones)

    def test_multiple_geofences(self):
        fence_far  = Geofence("far",  37.6000, 127.0300, 100)
        fence_near = Geofence("near", 37.5020, 127.0300, 100)
        result = calculate_next_interval(
            *GANGNAM, make_history([GANGNAM] * 3), [fence_far, fence_near],
        )
        self.assertLess(result.distance_to_nearest_fence, 500)

    def test_gps_mode_classification(self):
        self.assertEqual(classify_gps_mode(1),  "HIGH")
        self.assertEqual(classify_gps_mode(9),  "HIGH")
        self.assertEqual(classify_gps_mode(10), "BALANCED")
        self.assertEqual(classify_gps_mode(29), "BALANCED")
        self.assertEqual(classify_gps_mode(30), "LOW")
        self.assertEqual(classify_gps_mode(60), "LOW")

    def test_result_has_cosine_debug_fields(self):
        """OptimizationResult에 u_dist, u_time, urgency 포함"""
        result = calculate_next_interval(
            *GANGNAM, make_history([GANGNAM] * 2), [SAMSUNG_FENCE]
        )
        self.assertTrue(hasattr(result, "u_dist"))
        self.assertTrue(hasattr(result, "u_time"))
        self.assertTrue(hasattr(result, "urgency"))
        self.assertGreaterEqual(result.urgency, 0.0)
        self.assertLessEqual(result.urgency, 1.0)


# ══════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [TestHaversine, TestCosineBlend, TestActivityRecognition,
                TestSLC, TestIntegration]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
