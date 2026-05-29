"""
Unit tests: next_interval calculation logic accuracy verification
- Cosine Blend Interval (distance + time urgency)
- Activity Recognition
- Significant Location Change
- Integration calculation (including example calculation scenarios)
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


# ── Helper: generate location history ────────────────────────
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
# 1. Haversine distance calculation
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
        """urgency=0 (very far, plenty of time) → INTERVAL_MAX_S"""
        raw, u_dist, u_time, urgency = _cosine_blend_interval(
            distance_to_fence=D_MAX_M,
            eta_sec=10.0,
            appointment_remaining_sec=3600.0,
        )
        self.assertAlmostEqual(urgency, 0.0, places=1)
        self.assertAlmostEqual(raw, INTERVAL_MAX_S, delta=5)

    def test_urgency_one_gives_min_interval(self):
        """urgency=1 (right in front, ETA=remaining time) → INTERVAL_MIN_S"""
        raw, u_dist, u_time, urgency = _cosine_blend_interval(
            distance_to_fence=0.0,
            eta_sec=60.0,
            appointment_remaining_sec=60.0,
        )
        self.assertAlmostEqual(urgency, 1.0, places=1)
        self.assertAlmostEqual(raw, INTERVAL_MIN_S, delta=1)

    def test_urgency_in_valid_range(self):
        """urgency is always in [0, 1] range"""
        cases = [
            (0.0, 0.0, 0.0),
            (500.0, 300.0, 600.0),
            (D_MAX_M * 2, 10.0, 3600.0),   # distance exceeds max
            (100.0, 9999.0, 60.0),          # eta > appointment
        ]
        for d, eta, appt in cases:
            _, _, _, urgency = _cosine_blend_interval(d, eta, appt)
            self.assertGreaterEqual(urgency, 0.0, msg=f"case {d},{eta},{appt}")
            self.assertLessEqual(urgency, 1.0, msg=f"case {d},{eta},{appt}")

    def test_interval_monotone_with_distance(self):
        """interval increases as distance grows"""
        distances = [50, 200, 500, 1000, 2000]
        intervals = [_cosine_blend_interval(d, None, None)[0] for d in distances]
        for i in range(len(intervals) - 1):
            self.assertLessEqual(intervals[i], intervals[i + 1],
                msg=f"interval should increase: d={distances[i]}→{distances[i+1]}")

    def test_time_urgency_without_info_mirrors_distance(self):
        """u_time == u_dist when no time info available"""
        _, u_dist, u_time, _ = _cosine_blend_interval(800.0, None, None)
        self.assertAlmostEqual(u_dist, u_time, places=6)

    def test_time_urgency_high_eta_ratio(self):
        """u_time=1 when eta equals full appointment time (very tight)"""
        _, _, u_time, _ = _cosine_blend_interval(1000.0, 600.0, 600.0)
        self.assertAlmostEqual(u_time, 1.0, places=3)

    def test_time_urgency_low_eta_ratio(self):
        """u_time is low when eta is 10% of appointment time (plenty of time)"""
        _, _, u_time, _ = _cosine_blend_interval(1000.0, 60.0, 600.0)
        self.assertAlmostEqual(u_time, 0.1, places=3)

    def test_alpha_weight_balance(self):
        """urgency == u_dist when alpha=1.0"""
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
# 5. Integration calculation (key scenarios)
# ══════════════════════════════════════════════════════════════
class TestIntegration(unittest.TestCase):

    def _make_bus_history(self):
        """Bus scenario: moving quickly toward Samsung Station zone (arrives ~200m before boundary)"""
        points = [
            (37.5012, 127.0276), (37.5030, 127.0310),
            (37.5048, 127.0344), (37.5062, 127.0378),
            (37.5073, 127.0415),
        ]
        return make_history(points, interval_sec=10)

    def _make_stationary_history(self):
        """Stationary scenario: ~200m from zone boundary, no movement"""
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
        """Bus + near boundary → high urgency (>0.8), short interval (<10s)"""
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
        """Stationary interval is much longer than bus interval (at least 5x)"""
        bus = calculate_next_interval(
            37.5073, 127.0415, self._make_bus_history(), [SAMSUNG_FENCE]
        )
        still = calculate_next_interval(
            37.5073, 127.0415, self._make_stationary_history(), [SAMSUNG_FENCE]
        )
        self.assertGreater(still.next_interval, bus.next_interval * 5)

    def test_time_urgency_increases_frequency(self):
        """interval decreases as ETA gets tight relative to appointment time (using vehicle history to avoid multiplier accumulation)"""
        # vehicle history: activity_multiplier=0.3, SLC significant → slc_mult=1.0
        # → total multiplier is small, won't hit 300s cap
        points = [(37.5012 + i * 0.0009, 127.0276) for i in range(6)]
        history = make_history(points, interval_sec=10)
        fence_mid = Geofence("mid", 37.52, 127.03, 100)

        # Relaxed: eta 20min, appointment 3hr
        result_relaxed = calculate_next_interval(
            *GANGNAM, history, [fence_mid],
            eta_sec=1200, appointment_remaining_sec=10800,
        )
        # Tight: eta 20min, appointment 22min
        result_urgent = calculate_next_interval(
            *GANGNAM, history, [fence_mid],
            eta_sec=1200, appointment_remaining_sec=1320,
        )
        self.assertGreater(result_relaxed.next_interval, result_urgent.next_interval)
        self.assertLess(result_relaxed.u_time, result_urgent.u_time)

    def test_max_interval_cap(self):
        """interval never exceeds 300 seconds"""
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
        """OptimizationResult includes u_dist, u_time, urgency fields"""
        result = calculate_next_interval(
            *GANGNAM, make_history([GANGNAM] * 2), [SAMSUNG_FENCE]
        )
        self.assertTrue(hasattr(result, "u_dist"))
        self.assertTrue(hasattr(result, "u_time"))
        self.assertTrue(hasattr(result, "urgency"))
        self.assertGreaterEqual(result.urgency, 0.0)
        self.assertLessEqual(result.urgency, 1.0)


# ══════════════════════════════════════════════════════════════
# Run
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
