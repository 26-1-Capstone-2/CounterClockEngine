"""
단위 테스트: next_interval 계산 로직 정확성 검증
- 거리 기반 Adaptive Interval
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
    haversine, get_base_interval, estimate_activity,
    detect_significant_change, calculate_next_interval,
    classify_gps_mode, LocationPoint, Geofence,
    SLC_THRESHOLD_M, MAX_INTERVAL_S,
)


# ── 헬퍼: 위치 이력 생성 ──────────────────────────────────────
def make_history(points: list[tuple], interval_sec: int = 10) -> list[LocationPoint]:
    """(lat, lon) 리스트로 LocationPoint 이력 생성"""
    base_time = datetime(2026, 5, 11, 9, 0, 0)
    return [
        LocationPoint(lat, lon, base_time + timedelta(seconds=i * interval_sec))
        for i, (lat, lon) in enumerate(points)
    ]

GANGNAM  = (37.5012, 127.0276)   # 강남역 (현재 위치)
SAMSUNG_CENTER = (37.5088, 127.0456)  # 삼성역 방향 존 (Geofence 중심, 약 360m)
SAMSUNG_RADIUS = 200             # Geofence 반경 (m)

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
        # 위도 0.003도 ≈ 333m, 경도 0.002도 ≈ 185m → 합산 약 380m
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
        """위도 약 0.009도 ≈ 1km"""
        dist = haversine(37.5, 127.0, 37.509, 127.0)
        self.assertGreater(dist, 900)
        self.assertLess(dist, 1100)


# ══════════════════════════════════════════════════════════════
# 2. 거리 기반 Adaptive Interval
# ══════════════════════════════════════════════════════════════
class TestBaseInterval(unittest.TestCase):

    def test_inside_zone_is_3s(self):
        """존 내부 (거리 음수) → 3초"""
        self.assertEqual(get_base_interval(-50), 3)

    def test_200m_boundary_is_3s(self):
        self.assertEqual(get_base_interval(200), 3)

    def test_201m_is_10s(self):
        self.assertEqual(get_base_interval(201), 10)

    def test_500m_boundary_is_10s(self):
        self.assertEqual(get_base_interval(500), 10)

    def test_501m_is_30s(self):
        self.assertEqual(get_base_interval(501), 30)

    def test_1000m_boundary_is_30s(self):
        self.assertEqual(get_base_interval(1000), 30)

    def test_1001m_is_60s(self):
        self.assertEqual(get_base_interval(1001), 60)

    def test_5km_is_60s(self):
        self.assertEqual(get_base_interval(5000), 60)


# ══════════════════════════════════════════════════════════════
# 3. Activity Recognition
# ══════════════════════════════════════════════════════════════
class TestActivityRecognition(unittest.TestCase):

    def test_single_point_is_unknown(self):
        history = make_history([GANGNAM])
        self.assertEqual(estimate_activity(history), "unknown")

    def test_no_movement_is_stationary(self):
        """같은 위치 반복 → 정지"""
        history = make_history([GANGNAM] * 5)
        self.assertEqual(estimate_activity(history), "stationary")

    def test_slow_movement_is_walking(self):
        """약 1 m/s 이동 → 도보"""
        # 10초마다 약 10m 이동 (위도 0.00009도 ≈ 10m)
        points = [(37.5012 + i * 0.00009, 127.0300) for i in range(5)]
        history = make_history(points, interval_sec=10)
        self.assertEqual(estimate_activity(history), "walking")

    def test_fast_movement_is_vehicle(self):
        """약 10 m/s 이동 → 차량"""
        # 10초마다 약 100m 이동 (위도 0.0009도 ≈ 100m)
        points = [(37.5012 + i * 0.0009, 127.0300) for i in range(5)]
        history = make_history(points, interval_sec=10)
        self.assertEqual(estimate_activity(history), "vehicle")

    def test_bus_scenario_is_vehicle(self):
        """버스 시나리오: 강남→삼성 구간 이동 → 차량"""
        points = [
            (37.4980, 127.0210),
            (37.4990, 127.0240),
            (37.5000, 127.0270),
            (37.5008, 127.0290),
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
        """이동 없음 → False"""
        history = make_history([GANGNAM] * 5)
        is_sig, dist = detect_significant_change(history)
        self.assertFalse(is_sig)
        self.assertLess(dist, SLC_THRESHOLD_M)

    def test_200m_movement_is_not_significant(self):
        """200m 이동 → False (기준 500m)"""
        points = [GANGNAM, (37.5012 + 0.0018, 127.0300)]  # 약 200m
        history = make_history(points)
        is_sig, dist = detect_significant_change(history)
        self.assertFalse(is_sig)

    def test_800m_movement_is_significant(self):
        """800m 이동 → True"""
        points = [GANGNAM, (37.5012 + 0.0072, 127.0300)]  # 약 800m
        history = make_history(points)
        is_sig, dist = detect_significant_change(history)
        self.assertTrue(is_sig)
        self.assertGreater(dist, SLC_THRESHOLD_M)

    def test_bus_scenario_820m_is_significant(self):
        """버스 시나리오: t-4에서 t-0까지 약 820m → True"""
        points = [
            (37.4980, 127.0210),
            (37.4990, 127.0240),
            (37.5000, 127.0270),
            (37.5008, 127.0290),
            (37.5012, 127.0300),
        ]
        history = make_history(points, interval_sec=10)
        is_sig, dist = detect_significant_change(history)
        self.assertTrue(is_sig)
        self.assertGreater(dist, 500)

    def test_custom_threshold(self):
        """커스텀 임계값 적용"""
        points = [GANGNAM, (37.5012 + 0.0018, 127.0300)]  # 약 200m
        history = make_history(points)
        is_sig, _ = detect_significant_change(history, threshold=100)
        self.assertTrue(is_sig)


# ══════════════════════════════════════════════════════════════
# 5. 통합 계산 (핵심 시나리오)
# ══════════════════════════════════════════════════════════════
class TestIntegration(unittest.TestCase):

    def _make_bus_history(self):
        """버스 시나리오: 삼성역 존 방향으로 빠르게 이동 (경계 160m 앞 도착)"""
        # SAMSUNG_CENTER=(37.5088, 127.0456), radius=200m
        # 목적지: 경계까지 ~160m → 존 중심까지 ~360m
        # 10초마다 약 100m 이동 (9~10 m/s = 차량)
        points = [
            (37.5012, 127.0276),   # 출발 (존까지 ~2km)
            (37.5030, 127.0310),
            (37.5048, 127.0344),
            (37.5062, 127.0378),
            (37.5073, 127.0415),   # 도착 (존 경계까지 약 160m)
        ]
        return make_history(points, interval_sec=10)

    def _make_stationary_history(self):
        """정지 시나리오: 존 경계까지 약 160m, 이동 없음"""
        near_fence = (37.5073, 127.0415)
        return make_history([near_fence] * 5)

    def test_bus_scenario_base_interval(self):
        """버스: 경계 ~160m → base=3초"""
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(result.base_interval, 3)

    def test_bus_scenario_activity_is_vehicle(self):
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(result.activity, "vehicle")

    def test_bus_scenario_activity_multiplier(self):
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertAlmostEqual(result.activity_multiplier, 0.3)

    def test_bus_scenario_slc_is_significant(self):
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertTrue(result.is_significant_change)
        self.assertAlmostEqual(result.slc_multiplier, 1.0)

    def test_bus_scenario_final_interval_is_1s(self):
        """버스: 3초 × 0.3 × 1.0 = 0.9 ≈ 1초"""
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_bus_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(result.next_interval, 1)
        self.assertEqual(result.gps_mode, "HIGH")

    def test_stationary_scenario_activity_is_stationary(self):
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(result.activity, "stationary")

    def test_stationary_scenario_slc_not_significant(self):
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertFalse(result.is_significant_change)
        self.assertAlmostEqual(result.slc_multiplier, 2.0)

    def test_stationary_scenario_final_interval_is_18s(self):
        """정지: base 3초 × 3.0 × 2.0 = 18초 (존 경계 160m, 정지, SLC 미감지)"""
        result = calculate_next_interval(
            37.5073, 127.0415,
            self._make_stationary_history(),
            [SAMSUNG_FENCE],
        )
        # 경계까지 거리가 200m 이내여야 base=3초
        self.assertLessEqual(result.distance_to_nearest_fence, 200)
        self.assertEqual(result.base_interval, 3)
        self.assertEqual(result.next_interval, 18)

    def test_bus_vs_stationary_ratio(self):
        """정지(18초) / 버스(1초) = 18배 차이"""
        bus = calculate_next_interval(
            37.5073, 127.0415,
            self._make_bus_history(), [SAMSUNG_FENCE]
        )
        still = calculate_next_interval(
            37.5073, 127.0415,
            self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertEqual(bus.next_interval, 1)
        self.assertEqual(still.next_interval, 18)
        self.assertEqual(still.next_interval / bus.next_interval, 18)

    # ── 경계 케이스 ──
    def test_max_interval_cap(self):
        """최대 300초 이상 넘지 않음"""
        # 10km 이상 거리 + 정지
        far_fence = Geofence("far", 37.5012 + 0.1, 127.0300, 100)
        result = calculate_next_interval(
            *GANGNAM, self._make_stationary_history(), [far_fence]
        )
        self.assertLessEqual(result.next_interval, MAX_INTERVAL_S)

    def test_inside_geofence_detected(self):
        """존 내부 진입 감지"""
        inside_point = (SAMSUNG_CENTER[0], SAMSUNG_CENTER[1])  # 존 중심
        history = make_history([inside_point] * 3)
        result = calculate_next_interval(
            *inside_point, history, [SAMSUNG_FENCE]
        )
        self.assertIn("samsung_station", result.entered_zones)

    def test_multiple_geofences(self):
        """복수 Geofence 중 가장 가까운 거리 사용"""
        fence_far  = Geofence("far",  37.6000, 127.0300, 100)
        fence_near = Geofence("near", 37.5020, 127.0300, 100)
        result = calculate_next_interval(
            *GANGNAM,
            make_history([GANGNAM] * 3),
            [fence_far, fence_near],
        )
        self.assertLess(result.distance_to_nearest_fence, 500)

    def test_gps_mode_classification(self):
        self.assertEqual(classify_gps_mode(1),  "HIGH")
        self.assertEqual(classify_gps_mode(9),  "HIGH")
        self.assertEqual(classify_gps_mode(10), "BALANCED")
        self.assertEqual(classify_gps_mode(29), "BALANCED")
        self.assertEqual(classify_gps_mode(30), "LOW")
        self.assertEqual(classify_gps_mode(60), "LOW")


# ══════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [TestHaversine, TestBaseInterval, TestActivityRecognition,
                TestSLC, TestIntegration]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
