"""
Fravix / Apero 2API — R1 本地压测
用法: python stress_test.py [--quick]

覆盖:
  S1 网关 /health 吞吐与延迟
  S2 上游 /timestamp 延迟（签名前必需）
  S3 本地 RSA 签名吞吐（纯 CPU，无网络）
  S4 上游 presigned-link 直连延迟
  S5 全链路图片生成（网关 /api/v1/generate，顺序 5 次）
  S6 并发突发 c3 × 2 批（图片链）
  S7 全链路视频生成（workflow 轮询 1 次）
  S8 错误路径（网关 404/400、上游 401/ERR06/ERR05）
  S9 汇总网关侧 /stats
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import statistics
import time
from typing import Any, Dict, List

import os
from pathlib import Path

import requests

import fravix_client as F

GATEWAY = os.environ.get("FRAVIX_GATEWAY", "http://127.0.0.1:8955")
IMG = os.environ.get("FRAVIX_TEST_IMAGE", str(Path(__file__).with_name("media") / "test_face_real.jpg"))
RESULTS: List[Dict[str, Any]] = []


def pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))], 1)


def report(name: str, lat: List[float], ok: int, fail: int, note: str = "") -> None:
    row = {
        "scenario": name,
        "n": ok + fail,
        "ok": ok,
        "fail": fail,
        "p50_ms": pct(lat, 50),
        "p95_ms": pct(lat, 95),
        "max_ms": round(max(lat), 1) if lat else 0.0,
        "note": note,
    }
    RESULTS.append(row)
    print(
        f"[{name:<26}] n={row['n']:<3} ok={ok:<3} fail={fail:<2} "
        f"p50={row['p50_ms']:>9.1f} p95={row['p95_ms']:>9.1f} max={row['max_ms']:>9.1f}  {note}"
    )


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    try:
        out = fn(*a, **kw)
        return (time.perf_counter() - t0) * 1000, out, None
    except Exception as exc:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000, None, exc


def s1_health() -> None:
    lat, ok, fail = [], 0, 0
    for _ in range(20):
        ms, out, err = timed(lambda: requests.get(f"{GATEWAY}/health", timeout=10).json())
        lat.append(ms)
        ok, fail = (ok + 1, fail) if not err else (ok, fail + 1)
    report("S1 gateway /health", lat, ok, fail, "本地 FastAPI")


def s2_timestamp() -> None:
    c = F.FravixClient()
    lat, ok, fail = [], 0, 0
    for _ in range(10):
        ms, out, err = timed(c.server_timestamp)
        lat.append(ms)
        ok, fail = (ok + 1, fail) if not err else (ok, fail + 1)
    report("S2 upstream /timestamp", lat, ok, fail, "边缘层 412 风险已覆盖")


def s3_signature() -> None:
    c = F.FravixClient()
    ts = c.now_ms()
    lat, ok, fail = [], 0, 0
    t0 = time.perf_counter()
    for _ in range(200):
        ms, out, err = timed(c.sign, ts)
        lat.append(ms)
        ok, fail = (ok + 1, fail) if not err else (ok, fail + 1)
    total = (time.perf_counter() - t0) * 1000
    report("S3 local RSA sign", lat, ok, fail, f"200 次耗时 {total:.0f}ms → {200/(total/1000):.0f} op/s")


def s4_presigned() -> None:
    c = F.FravixClient()
    lat, ok, fail = [], 0, 0
    for _ in range(5):
        ms, out, err = timed(c.presigned_link)
        lat.append(ms)
        ok, fail = (ok + 1, fail) if not err else (ok, fail + 1)
    report("S4 presigned-link", lat, ok, fail, "S3 直传地址签发")


def _gen_once(style_id: str = "ai_lookbook") -> requests.Response:
    with open(IMG, "rb") as fh:
        return requests.post(
            f"{GATEWAY}/api/v1/generate",
            files={"image": ("face.jpg", fh.read(), "image/jpeg")},
            data={"style_id": style_id},
            timeout=300,
        )


def s5_sequential(n: int = 5) -> None:
    lat, ok, fail, urls = [], 0, 0, []
    for _ in range(n):
        ms, resp, err = timed(_gen_once)
        lat.append(ms)
        if err or resp is None or resp.status_code != 200:
            fail += 1
            print("   fail:", err or resp.text[:200])
        else:
            ok += 1
            urls.append(resp.json()["url"])
    report("S5 image chain seq", lat, ok, fail, f"含 S3 上传 11~14s；样例 {len(urls)} 条直链")


def s6_burst(concurrency: int = 3, batches: int = 2) -> None:
    lat, ok, fail = [], 0, 0
    for b in range(batches):
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = [pool.submit(_gen_once) for _ in range(concurrency)]
            for fut in cf.as_completed(futs):
                try:
                    r = fut.result()
                    ms = (time.perf_counter() - t0) * 1000
                    lat.append(ms)
                    if r.status_code == 200:
                        ok += 1
                    else:
                        fail += 1
                        print("   fail:", r.status_code, r.text[:160])
                except Exception as exc:  # noqa: BLE001
                    fail += 1
                    print("   fail:", exc)
    report(f"S6 burst c{concurrency}×{batches}", lat, ok, fail, "并发隔离性/上游限流观察")


def s7_video() -> None:
    lat, ok, fail = [], 0, 0
    ms, resp, err = timed(
        lambda: requests.post(
            f"{GATEWAY}/api/v1/generate",
            files={"image": ("face.jpg", open(IMG, "rb").read(), "image/jpeg")},
            data={"style_id": "retro_80s"},
            timeout=400,
        )
    )
    lat.append(ms)
    if err or resp.status_code != 200:
        fail += 1
        print("   fail:", err or resp.text[:200])
    else:
        ok += 1
        print("   video:", resp.json()["url"][:110])
    report("S7 video workflow", lat, ok, fail, "execute→轮询→mp4")


def s8_errors() -> None:
    checks = []

    r = requests.post(
        f"{GATEWAY}/api/v1/generate",
        files={"image": ("face.jpg", open(IMG, "rb").read(), "image/jpeg")},
        data={"style_id": "no_such_style"},
        timeout=30,
    )
    checks.append(("gateway 未知风格", r.status_code == 404, f"{r.status_code}"))

    r = requests.post(
        f"{GATEWAY}/api/v1/generate",
        files={"image": ("e.jpg", b"", "image/jpeg")},
        timeout=30,
    )
    checks.append(("gateway 空文件", r.status_code == 400, f"{r.status_code}"))

    r = requests.get(f"{F.BASE_URL}/api/v5.1/qwen-editing/presigned-link", headers=dict(F.FravixClient.PLACEHOLDER_HEADERS), timeout=30)
    checks.append(("upstream 无签名→401", r.status_code == 401, f"{r.status_code}/{r.json().get('errorCode')}"))

    c = F.FravixClient()
    h = dict(F.FravixClient.PLACEHOLDER_HEADERS)
    h.update(
        {
            "app-version": "2.1.1",
            "country-code": "US",
            "X-Api-Signature": "AAAA",
            "X-Api-Timestamp": str(c.now_ms()),
            "App-name": c.app_name,
            "X-Api-BundleId": c.bundle_id,
            "x-api-token": F.DEFAULT_API_TOKEN,
        }
    )
    r = requests.get(f"{F.BASE_URL}/api/v5.1/qwen-editing/presigned-link", headers=h, timeout=30)
    checks.append(("upstream 错签名→ERR06", r.json().get("errorCode") == "ERR06", f"{r.status_code}/{r.json().get('errorCode')}"))

    ts = c.server_timestamp() - 400_000
    h2 = dict(F.FravixClient.PLACEHOLDER_HEADERS)
    h2.update(
        {
            "app-version": "2.1.1",
            "country-code": "US",
            "X-Api-Signature": c.sign(ts),
            "X-Api-Timestamp": str(ts),
            "App-name": c.app_name,
            "X-Api-BundleId": c.bundle_id,
            "x-api-token": F.DEFAULT_API_TOKEN,
        }
    )
    r = requests.get(f"{F.BASE_URL}/api/v5.1/qwen-editing/presigned-link", headers=h2, timeout=30)
    checks.append(("upstream 旧时间戳→ERR05", r.json().get("errorCode") == "ERR05", f"{r.status_code}/{r.json().get('errorCode')}"))

    passed = sum(1 for _, okk, _ in checks if okk)
    for name, okk, info in checks:
        print(f"   {'PASS' if okk else 'FAIL'} {name:<22} {info}")
    report("S8 error paths", [0.0], passed, len(checks) - passed, f"{passed}/{len(checks)} 断言通过")


def s9_stats() -> None:
    s = requests.get(f"{GATEWAY}/stats", timeout=10).json()
    print("\n=== 网关侧统计 ===")
    print(json.dumps({k: s[k] for k in ("requests", "success_rate", "image", "video")}, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过 S5/S6/S7 的真实生成")
    args = ap.parse_args()

    t0 = time.time()
    s1_health()
    s2_timestamp()
    s3_signature()
    s4_presigned()
    if not args.quick:
        s5_sequential(5)
        s6_burst(3, 2)
        s7_video()
    s8_errors()
    s9_stats()

    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "duration_s": round(time.time() - t0, 1), "results": RESULTS}
    out_file = "benchmark_results_quick.json" if args.quick else "benchmark_results.json"
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n总耗时 {out['duration_s']}s → benchmark_results.json")


if __name__ == "__main__":
    main()
