#!/usr/bin/env python3
"""
Full verification runner script
1. Unit tests (optimizer logic)
2. Battery simulation (normal vs optimized comparison)
3. Direct coordinate input test
"""

import sys
import os
import unittest
from datetime import datetime, timedelta
from dotenv import load_dotenv, find_dotenv

sys.path.insert(0, ".")

# Auto-search for .env file by traversing up from current file
load_dotenv(find_dotenv())


# ── 1. Unit tests ──────────────────────────────────────────────
def run_unit_tests():
    print("\n" + "=" * 60)
    print("  [1/2] Unit Tests: Calculation Logic Accuracy")
    print("=" * 60)
    loader = unittest.TestLoader()
    suite  = loader.discover(".", pattern="test_optimizer.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


# ── 2. Battery simulation ──────────────────────────────────────
def run_simulation():
    print("\n" + "=" * 60)
    print("  [2/2] Battery Simulation")
    print("=" * 60)
    import random
    from battery_simulation import (
        generate_route, simulate_normal_mode,
        simulate_optimized_mode, print_report
    )
    random.seed(42)
    route     = generate_route()
    normal    = simulate_normal_mode(route)
    optimized = simulate_optimized_mode(route)
    saving_battery, saving_calls = print_report(normal, optimized)
    return saving_battery >= 40.0 and saving_calls >= 60.0


# ── 3. Direct coordinate input test ───────────────────────────
def run_interactive():
    from optimizer import (
        calculate_next_interval, LocationPoint, Geofence
    )

    print("\n" + "=" * 60)
    print("  Direct Coordinate Input Test")
    print("=" * 60)

    # Geofence configuration
    print("\n[Geofence Setup]")
    print("  Default: Samsung Station (37.5088, 127.0632, radius 200m)")
    use_default = input("  Use default? (Enter=yes / n=manual input): ").strip().lower()

    geofences = []
    if use_default != "n":
        geofences = [
            Geofence("samsung_station", 37.5088, 127.0632, 200),
            Geofence("gangnam_station", 37.4979, 127.0276, 200),
        ]
        print("  → Using Gangnam Station Geofence as origin")
    else:
        print("  Enter geofences. Press Enter on empty line when done.")
        while True:
            raw = input("  [id lat lon radius]: ").strip()
            if not raw:
                break
            parts = raw.split()
            if len(parts) != 4:
                print("  Format error. e.g. samsung 37.5088 127.0632 200")
                continue
            try:
                geofences.append(Geofence(parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
                print(f"  → {parts[0]} added")
            except ValueError:
                print("  Number format error. Please try again.")
        if not geofences:
            print("  Proceeding without geofences.")

    # Coordinate input loop
    history = []
    call_count = 0

    print("\n[Coordinate Input]")
    print("  Format: lat lon   (e.g. 37.5012 127.0300)")
    print("  Quit: q")
    print("-" * 60)
    print(f"  {'#':>3}  {'Lat':>10}  {'Lon':>11}  {'Dist(m)':>8}  {'Activity':>10}  {'Mode':>8}  {'Interval':>10}")
    print("-" * 60)

    while True:
        raw = input("  coord> ").strip()
        if raw.lower() == "q":
            break
        parts = raw.split()
        if len(parts) != 2:
            print("  Format error. e.g. 37.5012 127.0300")
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            print("  Number format error.")
            continue

        # Update history (keep last 5)
        history.append(LocationPoint(lat, lon, datetime.now()))
        if len(history) > 5:
            history.pop(0)

        result = calculate_next_interval(lat, lon, history, geofences)
        call_count += 1

        dist_str = f"{result.distance_to_nearest_fence:.0f}" if geofences else "N/A"
        print(
            f"  {call_count:>3}  {lat:>10.4f}  {lon:>11.4f}"
            f"  {dist_str:>8}"
            f"  {result.activity:>10}"
            f"  {result.gps_mode:>8}"
            f"  {result.next_interval:>8}s"
        )

        # Detailed info
        zones = ", ".join(result.entered_zones) if result.entered_zones else "None"
        print(f"       urgency={result.urgency}"
              f"  × activity({result.activity_multiplier:.3f})"
              f"  × slc({result.slc_multiplier})"
              f"  → {result.next_interval}s"
              f"  | entered zones: {zones}")
        print()

    print("-" * 60)
    print(f"  Total {call_count} inputs completed")


# ── 4. Kakao route-based simulation ───────────────────────────
def run_kakao_simulation():
    from kakao_route import fetch_route, resample_route, print_route_summary, print_departure_plan
    from battery_simulation import (
        simulate_normal_mode, simulate_optimized_mode, print_report
    )
    from optimizer import Geofence

    print("\n" + "=" * 60)
    print("  Kakao Route-based Battery (GPS call) Simulation")
    print("=" * 60)

    # API key (priority: .env → env var → manual input)
    api_key = os.environ.get("KAKAO_API_KEY", "").strip()
    if api_key:
        print("  API key: loaded from .env")
    else:
        print("  No KAKAO_API_KEY in .env file.")
        api_key = input("  Enter Kakao REST API key directly: ").strip()
    if not api_key:
        print("  No API key. Cancelling.")
        return

    # Origin input
    print("\n[Origin] Enter lat lon  (e.g. 37.4979 127.0276)")
    while True:
        raw = input("  origin> ").strip().split()
        if len(raw) == 2:
            try:
                origin = (float(raw[0]), float(raw[1]))
                break
            except ValueError:
                pass
        print("  Format error. Please try again.")

    # Destination input
    print("[Destination] Enter lat lon")
    while True:
        raw = input("  destination> ").strip().split()
        if len(raw) == 2:
            try:
                dest = (float(raw[0]), float(raw[1]))
                break
            except ValueError:
                pass
        print("  Format error. Please try again.")

    # Geofence: auto-register destination as geofence + allow additional input
    geofences = [Geofence("destination", dest[0], dest[1], 200)]
    print(f"\n  Destination will be auto-registered as Geofence (radius 200m).")
    raw = input("  Add more geofences? (Enter=skip / y=input): ").strip().lower()
    if raw == "y":
        print("  Format: [id lat lon radius]. Press Enter on empty line when done.")
        while True:
            line = input("  > ").strip()
            if not line:
                break
            parts = line.split()
            if len(parts) == 4:
                try:
                    geofences.append(Geofence(parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
                    print(f"  → {parts[0]} added")
                except ValueError:
                    print("  Number format error.")

    # Fetch route
    print("\n  Fetching Kakao route...")
    try:
        coords, duration_sec, distance_m = fetch_route(
            origin[0], origin[1], dest[0], dest[1], api_key
        )
    except Exception as e:
        print(f"  [Error] {e}")
        return

    print("\n[Route Summary]")
    print_route_summary(origin, dest, duration_sec, distance_m, len(coords))

    # Departure time guide
    print("\n[Target Arrival Time Input]")
    print("  Format: YYYY-MM-DD HH:MM  (e.g. 2026-05-13 14:30)")
    print("  Press Enter to skip arrival time guidance")
    raw_arrival = input("  arrival time> ").strip()
    if raw_arrival:
        try:
            arrival_time = datetime.strptime(raw_arrival, "%Y-%m-%d %H:%M")
            departure_time = arrival_time - timedelta(seconds=duration_sec)
            print_departure_plan(origin, dest, arrival_time, departure_time, duration_sec, distance_m)
        except ValueError:
            print("  Format error. Skipping arrival time guidance.")

    step_sec = 10
    route = resample_route(coords, duration_sec, step_sec=step_sec)
    print(f"  Simulation steps: {len(route)} × {step_sec}s\n")

    # Run simulation
    h, rem = divmod(duration_sec, 3600)
    m, s   = divmod(rem, 60)
    time_str = f"{h}h {m}m" if h else f"{m}m {s}s"
    scenario = (
        f"{origin[0]:.4f},{origin[1]:.4f} → "
        f"{dest[0]:.4f},{dest[1]:.4f}  "
        f"({distance_m/1000:.1f}km / {time_str})"
    )
    normal    = simulate_normal_mode(route,    step_sec=step_sec, geofences=geofences)
    optimized = simulate_optimized_mode(route, step_sec=step_sec, geofences=geofences)
    print_report(normal, optimized, scenario=scenario)


# ── Main menu ──────────────────────────────────────────────────
def print_menu():
    print("\n" + "=" * 60)
    print("  CounterClock GPS Optimizer Verification Tool")
    print("=" * 60)
    print("  1. Unit tests (calculation logic accuracy)")
    print("  2. Battery simulation (normal vs optimized comparison)")
    print("  3. Direct coordinate input test")
    print("  4. Kakao route-based simulation")
    print("  5. Run all (1 + 2)")
    print("  q. Quit")
    print("=" * 60)


if __name__ == "__main__":
    # Run menu if no args, run all if --all arg provided
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        results = {
            "Unit tests":        run_unit_tests(),
            "Battery simulation": run_simulation(),
        }
        print("\n" + "=" * 60)
        print("  Final Results Summary")
        print("=" * 60)
        all_pass = True
        for name, ok in results.items():
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}]  {name}")
            if not ok:
                all_pass = False
        print("=" * 60)
        sys.exit(0 if all_pass else 1)

    while True:
        print_menu()
        choice = input("  choice> ").strip().lower()

        if choice == "1":
            run_unit_tests()
        elif choice == "2":
            run_simulation()
        elif choice == "3":
            run_interactive()
        elif choice == "4":
            run_kakao_simulation()
        elif choice == "5":
            results = {
                "Unit tests":        run_unit_tests(),
                "Battery simulation": run_simulation(),
            }
            print("\n" + "=" * 60)
            print("  Final Results Summary")
            print("=" * 60)
            for name, ok in results.items():
                status = "PASS" if ok else "FAIL"
                print(f"  [{status}]  {name}")
            print("=" * 60)
        elif choice == "q":
            print("  Exiting.")
            break
        else:
            print("  Invalid input.")
