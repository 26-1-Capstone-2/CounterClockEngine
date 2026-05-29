"""
verification0522.py
Algorithm behavior verification script for gps_api

Verification items:
  1. Cosine Blend Interval  — GPS update interval optimization based on distance/time urgency
  2. Sigmoid Activity Mult  — Smooth activity multiplier based on speed
  3. Significant Location   — Adjust update interval via movement detection
  4. Group GPS Interval     — Personalized interval per group participant
  5. Status determination   — on_time / leave_soon / hurry / late
"""

import sys
import math
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from gps_api.core.optimizer import (
    LocationPoint, Geofence,
    calculate_next_interval,
    smooth_activity_multiplier,
    INTERVAL_MIN_S, INTERVAL_MAX_S,
)
from gps_api.core.group import (
    Participant, Group,
    _compute_status, compute_gps_interval,
)
from gps_api.core.journey import Journey, compute_eta

# ── Common coordinates ────────────────────────────────────────────────
GANGNAM   = (37.4979, 127.0276)   # Gangnam Station
SAMSUNG   = (37.5088, 127.0632)   # Samsung Station (destination)
YEOKSAM   = (37.5008, 127.0364)   # Yeoksam Station (midpoint)
SEOCHO    = (37.4837, 127.0324)   # Seocho Station (far location)

SEP = "=" * 64


def header(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def subheader(title: str):
    print(f"\n  ── {title} {'─' * (55 - len(title))}")


def make_history(
    points: list[tuple],
    interval_sec: int = 10,
) -> list[LocationPoint]:
    base = datetime(2026, 5, 22, 9, 0, 0)
    return [
        LocationPoint(lat, lon, base + timedelta(seconds=i * interval_sec))
        for i, (lat, lon) in enumerate(points)
    ]


# ══════════════════════════════════════════════════════════════════════
# 1. Cosine Blend Interval — GPS interval based on distance/time urgency
# ══════════════════════════════════════════════════════════════════════
def verify_cosine_blend():
    header("1. Cosine Blend Interval  (distance + time urgency → GPS update interval)")

    dest_fence = [Geofence("samsung", SAMSUNG[0], SAMSUNG[1], 200)]

    scenarios = [
        {
            "label": "Before departure — far, plenty of time",
            "pos": SEOCHO,
            "history": make_history([SEOCHO] * 3),
            "eta_sec": 600,
            "appt_remain": 7200,
        },
        {
            "label": "In transit — midpoint, moderate time",
            "pos": GANGNAM,
            "history": make_history([SEOCHO, YEOKSAM, GANGNAM], interval_sec=30),
            "eta_sec": 300,
            "appt_remain": 1200,
        },
        {
            "label": "Almost arrived — near destination, time tight",
            "pos": YEOKSAM,
            "history": make_history([GANGNAM, YEOKSAM], interval_sec=10),
            "eta_sec": 180,
            "appt_remain": 200,
        },
    ]

    print(f"\n  {'Scenario':<30} {'Dist(m)':>7} {'u_dist':>7} {'u_time':>7} {'urgency':>8} {'interval':>9} {'mode':>8}")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*9} {'-'*8}")

    for s in scenarios:
        r = calculate_next_interval(
            s["pos"][0], s["pos"][1],
            s["history"], dest_fence,
            eta_sec=s["eta_sec"],
            appointment_remaining_sec=s["appt_remain"],
        )
        print(
            f"  {s['label']:<30} {r.distance_to_nearest_fence:>7.0f}"
            f"  {r.u_dist:>6.3f}  {r.u_time:>6.3f}  {r.urgency:>7.3f}"
            f"  {r.next_interval:>7}s  {r.gps_mode:>8}"
        )

    print(f"\n  → urgency 0 (relaxed) → {INTERVAL_MAX_S}s(LOW),  urgency 1 (tight) → {INTERVAL_MIN_S}s(HIGH)")


# ══════════════════════════════════════════════════════════════════════
# 2. Sigmoid Activity Multiplier — smooth multiplier based on speed
# ══════════════════════════════════════════════════════════════════════
def verify_sigmoid_activity():
    header("2. Sigmoid Activity Multiplier  (speed → multiplier mapping)")

    speeds = [
        (0.0,  "Completely stationary"),
        (0.3,  "Very slow movement"),
        (0.5,  "Boundary (stationary→walking)"),
        (1.4,  "Walking"),
        (4.0,  "Fast walk / bicycle"),
        (8.0,  "Boundary (walking→vehicle)"),
        (12.0, "Bus"),
        (30.0, "High-speed vehicle"),
    ]

    print(f"\n  {'Speed(m/s)':>10}  {'State':>25}  {'multiplier':>12}  {'Meaning'}")
    print(f"  {'-'*10}  {'-'*25}  {'-'*12}  {'-'*20}")

    for spd, label in speeds:
        mult = smooth_activity_multiplier(spd)
        if spd < 0.5:
            meaning = "GPS interval ×3 (save battery)"
        elif spd < 8.0:
            meaning = "Keep base interval"
        else:
            meaning = "GPS interval ×0.3 (fast update)"
        print(f"  {spd:>10.1f}  {label:>25}  {mult:>12.4f}  {meaning}")

    print("\n  → Smooth transition near boundaries using sigmoid instead of discrete classification")


# ══════════════════════════════════════════════════════════════════════
# 3. Significant Location Change — movement detection
# ══════════════════════════════════════════════════════════════════════
def verify_slc():
    header("3. Significant Location Change  (movement detection → multiplier adjustment)")

    dest_fence = [Geofence("samsung", SAMSUNG[0], SAMSUNG[1], 200)]

    cases = [
        {
            "label": "Stationary (no SLC) → slc_mult=2.0 applied",
            "history": make_history([GANGNAM] * 5),
            "pos": GANGNAM,
        },
        {
            "label": "Moving (SLC detected)   → slc_mult=1.0",
            "history": make_history([SEOCHO, GANGNAM], interval_sec=60),
            "pos": GANGNAM,
        },
    ]

    print(f"\n  {'Scenario':<38} {'Dist moved(m)':>10} {'SLC':>5} {'slc_mult':>9} {'interval':>9}")
    print(f"  {'-'*38} {'-'*10} {'-'*5} {'-'*9} {'-'*9}")

    for c in cases:
        r = calculate_next_interval(c["pos"][0], c["pos"][1], c["history"], dest_fence)
        print(
            f"  {c['label']:<38} {r.moved_distance:>10.0f}"
            f"  {'O' if r.is_significant_change else 'X':>5}"
            f"  {r.slc_multiplier:>9.1f}"
            f"  {r.next_interval:>7}s"
        )

    print("\n  → Double interval when stationary to save battery")


# ══════════════════════════════════════════════════════════════════════
# 4. Group GPS Interval — personalized GPS interval per participant
# ══════════════════════════════════════════════════════════════════════
def verify_group_gps():
    header("4. Group GPS Interval  (personalized GPS update interval per participant)")

    appointment_time = datetime.now() + timedelta(minutes=25)
    destination = SAMSUNG
    dest_fence = [Geofence("samsung", SAMSUNG[0], SAMSUNG[1], 200)]
    appt_remain = (appointment_time - datetime.now()).total_seconds()

    # Scenarios with movement history: recent location flow + ETA per participant
    participants = [
        {
            "name": "Alice  (bus, far)",
            # Moving quickly toward Gangnam from Seocho (~30 m/s)
            "history": make_history([
                (37.4650, 127.0200), (37.4720, 127.0230),
                (37.4800, 127.0250), (37.4900, 127.0270),
            ], interval_sec=10),
            "pos": (37.4900, 127.0270),
            "eta_sec": 900,
        },
        {
            "name": "Bob    (walking, mid)",
            # Walking slowly near Yeoksam (~1.4 m/s)
            "history": make_history([
                (37.5000, 127.0350), (37.5003, 127.0360),
                (37.5006, 127.0370), (37.5008, 127.0380),
            ], interval_sec=10),
            "pos": (37.5008, 127.0380),
            "eta_sec": 300,
        },
        {
            "name": "Charlie (stationary, nearby)",
            # Stopped right in front of Samsung Station
            "history": make_history([(37.5075, 127.0600)] * 4, interval_sec=10),
            "pos": (37.5075, 127.0600),
            "eta_sec": 30,
        },
    ]

    print(f"\n  Time until appointment: {int(appt_remain / 60)} min  /  Destination: Samsung Station {destination}")
    print(f"\n  {'Participant':<24} {'Movement':>10} {'Dist(m)':>8} {'ETA':>7} {'interval':>10} {'mode':>8}")
    print(f"  {'-'*24} {'-'*10} {'-'*8} {'-'*7} {'-'*10} {'-'*8}")

    from gps_api.core.optimizer import haversine as hav, estimate_activity

    for p in participants:
        r = calculate_next_interval(
            p["pos"][0], p["pos"][1],
            p["history"], dest_fence,
            eta_sec=p["eta_sec"],
            appointment_remaining_sec=appt_remain,
        )
        dist = hav(p["pos"][0], p["pos"][1], SAMSUNG[0], SAMSUNG[1])
        print(
            f"  {p['name']:<24} {r.activity:>10}  {dist:>7.0f}m"
            f"  {int(p['eta_sec']):>5}s  {r.next_interval:>8}s  {r.gps_mode:>8}"
        )

    print("\n  → Different GPS intervals assigned per participant based on speed, distance, and ETA")


# ══════════════════════════════════════════════════════════════════════
# 5. Status determination — on_time / leave_soon / hurry / late based on ETA vs appointment time
# ══════════════════════════════════════════════════════════════════════
def verify_status():
    header("5. Status Determination  (appointment time - ETA → on_time / leave_soon / hurry / late)")

    now = datetime.now()
    appointment_time = now + timedelta(minutes=20)

    cases = [
        {"label": "Relaxed   (ETA 5min, appt 20min away)",  "eta": 300,  "dist": 1000},
        {"label": "Leave soon (ETA 15min, appt 20min away)", "eta": 900,  "dist": 3000},
        {"label": "Hurry      (ETA 22min, appt 20min away)", "eta": 1320, "dist": 4500},
        {"label": "Late       (appointment already passed)", "eta": 300,  "dist": 500,
         "appt": now - timedelta(minutes=5)},
        {"label": "Arrived    (50m away)",                   "eta": 10,   "dist": 50},
    ]

    print(f"\n  {'Scenario':<40} {'Status':>12}")
    print(f"  {'-'*40} {'-'*12}")

    for c in cases:
        appt = c.get("appt", appointment_time)
        status = _compute_status(c["eta"], c["dist"], appt)
        print(f"  {c['label']:<40} {status:>12}")

    print("\n  → margin = time remaining until appointment - ETA")
    print("     ≥ 600s → on_time  /  0~600s → leave_soon  /  <0 → hurry  /  passed → late")


# ══════════════════════════════════════════════════════════════════════
# 6. Journey ETA calculation — personal journey (Haversine fallback without Kakao)
# ══════════════════════════════════════════════════════════════════════
def verify_journey_eta():
    header("6. Journey ETA  (Haversine fallback without Kakao API)")

    goal_time = datetime.now() + timedelta(minutes=30)

    journey = Journey(
        journey_id="j001",
        member_id="david",
        title="Samsung Station Appointment",
        origin_lat=GANGNAM[0],
        origin_lon=GANGNAM[1],
        origin_name="Gangnam Station",
        dest_lat=SAMSUNG[0],
        dest_lon=SAMSUNG[1],
        dest_name="Samsung Station",
        current_lat=GANGNAM[0],
        current_lon=GANGNAM[1],
        goal_time=goal_time,
    )

    journey = compute_eta(journey)

    print(f"\n  Origin      : Gangnam Station {GANGNAM}")
    print(f"  Destination : Samsung Station {SAMSUNG}")
    print(f"  Appointment : {goal_time.strftime('%H:%M')}")
    print(f"\n  Distance    : {journey.distance_m:.0f}m")
    print(f"  ETA         : {journey.eta_sec:.0f}s ({journey.eta_sec/60:.1f}min)")
    print(f"  Alarm time  : {journey.alarm_time[:19] if journey.alarm_time else 'N/A'}")
    print(f"  Status      : {journey.status}")
    print("\n  → ETA can be calculated using Haversine straight-line distance without Kakao API key")


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"\n{'#' * 64}")
    print(f"  CounterClock gps_api Algorithm Verification  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#' * 64}")

    verify_cosine_blend()
    verify_sigmoid_activity()
    verify_slc()
    verify_group_gps()
    verify_status()
    verify_journey_eta()

    print(f"\n{'#' * 64}")
    print("  Verification complete")
    print(f"{'#' * 64}\n")
