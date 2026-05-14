#!/usr/bin/env python3
"""
전체 검증 실행 스크립트
1. 단위 테스트 (optimizer 로직)
2. 배터리 시뮬레이션 (일반 vs 최적화 비교)
3. 좌표 직접 입력 테스트
"""

import sys
import os
import unittest
from datetime import datetime, timedelta
from dotenv import load_dotenv, find_dotenv

sys.path.insert(0, ".")

# 현재 파일에서 위로 올라가며 .env 자동 탐색
load_dotenv(find_dotenv())


# ── 1. 단위 테스트 ─────────────────────────────────────────────
def run_unit_tests():
    print("\n" + "=" * 60)
    print("  [1/2] 단위 테스트: 계산 로직 정확성")
    print("=" * 60)
    loader = unittest.TestLoader()
    suite  = loader.discover(".", pattern="test_optimizer.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


# ── 2. 배터리 시뮬레이션 ───────────────────────────────────────
def run_simulation():
    print("\n" + "=" * 60)
    print("  [2/2] 배터리 시뮬레이션")
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


# ── 3. 좌표 직접 입력 테스트 ───────────────────────────────────
def run_interactive():
    from optimizer import (
        calculate_next_interval, LocationPoint, Geofence
    )

    print("\n" + "=" * 60)
    print("  좌표 직접 입력 테스트")
    print("=" * 60)

    # Geofence 설정
    print("\n[Geofence 설정]")
    print("  기본값: 삼성역 (37.5088, 127.0632, 반경 200m)")
    use_default = input("  기본값 사용? (Enter=yes / n=직접입력): ").strip().lower()

    geofences = []
    if use_default != "n":
        geofences = [
            Geofence("samsung_station", 37.5088, 127.0632, 200),
            Geofence("gangnam_station", 37.4979, 127.0276, 200),
        ]
        print("  → 출발지 -> 강남역 Geofence 사용")
    else:
        print("  Geofence를 입력하세요. 완료하면 빈 줄 Enter.")
        while True:
            raw = input("  [id lat lon radius]: ").strip()
            if not raw:
                break
            parts = raw.split()
            if len(parts) != 4:
                print("  형식 오류. 예) samsung 37.5088 127.0632 200")
                continue
            try:
                geofences.append(Geofence(parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
                print(f"  → {parts[0]} 추가됨")
            except ValueError:
                print("  숫자 형식 오류. 다시 입력하세요.")
        if not geofences:
            print("  Geofence 없이 진행합니다.")

    # 좌표 입력 루프
    history = []
    call_count = 0

    print("\n[좌표 입력]")
    print("  형식: lat lon   (예: 37.5012 127.0300)")
    print("  종료: q")
    print("-" * 60)
    print(f"  {'#':>3}  {'위도':>10}  {'경도':>11}  {'거리(m)':>8}  {'Activity':>10}  {'Mode':>8}  {'Interval':>10}")
    print("-" * 60)

    while True:
        raw = input("  좌표> ").strip()
        if raw.lower() == "q":
            break
        parts = raw.split()
        if len(parts) != 2:
            print("  형식 오류. 예) 37.5012 127.0300")
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            print("  숫자 형식 오류.")
            continue

        # 이력 업데이트 (최근 5개 유지)
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
            f"  {result.next_interval:>8}초"
        )

        # 상세 정보
        zones = ", ".join(result.entered_zones) if result.entered_zones else "없음"
        print(f"       base={result.base_interval}초"
              f"  × activity({result.activity_multiplier})"
              f"  × slc({result.slc_multiplier})"
              f"  → {result.next_interval}초"
              f"  | 진입존: {zones}")
        print()

    print("-" * 60)
    print(f"  총 {call_count}회 입력 완료")


# ── 4. 카카오 경로 기반 시뮬레이션 ───────────────────────────
def run_kakao_simulation():
    from kakao_route import fetch_route, resample_route, print_route_summary, print_departure_plan
    from battery_simulation import (
        simulate_normal_mode, simulate_optimized_mode, print_report
    )
    from optimizer import Geofence

    print("\n" + "=" * 60)
    print("  카카오 경로 기반 배터리(GPS 호출) 시뮬레이션")
    print("=" * 60)

    # API 키 (.env → 환경변수 → 직접 입력 순서)
    api_key = os.environ.get("KAKAO_API_KEY", "").strip()
    if api_key:
        print("  API 키: .env 에서 로드됨")
    else:
        print("  .env 파일에 KAKAO_API_KEY가 없습니다.")
        api_key = input("  카카오 REST API 키를 직접 입력하세요: ").strip()
    if not api_key:
        print("  API 키가 없어 취소합니다.")
        return

    # 출발지 입력
    print("\n[출발지] lat lon 입력  (예: 37.4979 127.0276)")
    while True:
        raw = input("  출발지> ").strip().split()
        if len(raw) == 2:
            try:
                origin = (float(raw[0]), float(raw[1]))
                break
            except ValueError:
                pass
        print("  형식 오류. 다시 입력하세요.")

    # 목적지 입력
    print("[목적지] lat lon 입력")
    while True:
        raw = input("  목적지> ").strip().split()
        if len(raw) == 2:
            try:
                dest = (float(raw[0]), float(raw[1]))
                break
            except ValueError:
                pass
        print("  형식 오류. 다시 입력하세요.")

    # Geofence: 목적지를 자동으로 Geofence로 등록 + 추가 입력 가능
    geofences = [Geofence("destination", dest[0], dest[1], 200)]
    print(f"\n  목적지가 Geofence(반경 200m)로 자동 등록됩니다.")
    raw = input("  추가 Geofence 입력? (Enter=건너뜀 / y=입력): ").strip().lower()
    if raw == "y":
        print("  [id lat lon radius] 형식. 완료하면 빈 줄 Enter.")
        while True:
            line = input("  > ").strip()
            if not line:
                break
            parts = line.split()
            if len(parts) == 4:
                try:
                    geofences.append(Geofence(parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
                    print(f"  → {parts[0]} 추가됨")
                except ValueError:
                    print("  숫자 형식 오류.")

    # 경로 요청
    print("\n  카카오 경로 조회 중...")
    try:
        coords, duration_sec, distance_m = fetch_route(
            origin[0], origin[1], dest[0], dest[1], api_key
        )
    except Exception as e:
        print(f"  [오류] {e}")
        return

    print("\n[경로 요약]")
    print_route_summary(origin, dest, duration_sec, distance_m, len(coords))

    # 출발 시간 안내
    print("\n[목표 도착 시각 입력]")
    print("  형식: YYYY-MM-DD HH:MM  (예: 2026-05-13 14:30)")
    print("  Enter 건너뜀: 도착 시간 안내 생략")
    raw_arrival = input("  도착 시각> ").strip()
    if raw_arrival:
        try:
            arrival_time = datetime.strptime(raw_arrival, "%Y-%m-%d %H:%M")
            departure_time = arrival_time - timedelta(seconds=duration_sec)
            print_departure_plan(origin, dest, arrival_time, departure_time, duration_sec, distance_m)
        except ValueError:
            print("  형식 오류로 도착 시간 안내를 건너뜁니다.")

    step_sec = 10
    route = resample_route(coords, duration_sec, step_sec=step_sec)
    print(f"  시뮬레이션 스텝: {len(route)}개 × {step_sec}초\n")

    # 시뮬레이션 실행
    h, rem = divmod(duration_sec, 3600)
    m, s   = divmod(rem, 60)
    time_str = f"{h}시간 {m}분" if h else f"{m}분 {s}초"
    scenario = (
        f"{origin[0]:.4f},{origin[1]:.4f} → "
        f"{dest[0]:.4f},{dest[1]:.4f}  "
        f"({distance_m/1000:.1f}km / {time_str})"
    )
    normal    = simulate_normal_mode(route,    step_sec=step_sec, geofences=geofences)
    optimized = simulate_optimized_mode(route, step_sec=step_sec, geofences=geofences)
    print_report(normal, optimized, scenario=scenario)


# ── 메인 메뉴 ─────────────────────────────────────────────────
def print_menu():
    print("\n" + "=" * 60)
    print("  CounterClock GPS Optimizer 검증 도구")
    print("=" * 60)
    print("  1. 단위 테스트 (계산 로직 정확성)")
    print("  2. 배터리 시뮬레이션 (일반 vs 최적화 비교)")
    print("  3. 좌표 직접 입력 테스트")
    print("  4. 카카오 경로 기반 시뮬레이션")
    print("  5. 전체 실행 (1 + 2)")
    print("  q. 종료")
    print("=" * 60)


if __name__ == "__main__":
    # 인자 없이 실행 시 메뉴, --all 인자 시 전체 자동 실행
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        results = {
            "단위 테스트":      run_unit_tests(),
            "배터리 시뮬레이션": run_simulation(),
        }
        print("\n" + "=" * 60)
        print("  최종 결과 요약")
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
        choice = input("  선택> ").strip().lower()

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
                "단위 테스트":      run_unit_tests(),
                "배터리 시뮬레이션": run_simulation(),
            }
            print("\n" + "=" * 60)
            print("  최종 결과 요약")
            print("=" * 60)
            for name, ok in results.items():
                status = "PASS" if ok else "FAIL"
                print(f"  [{status}]  {name}")
            print("=" * 60)
        elif choice == "q":
            print("  종료합니다.")
            break
        else:
            print("  잘못된 입력입니다.")
