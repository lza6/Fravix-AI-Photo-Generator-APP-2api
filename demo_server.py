"""
Fravix / Apero AI Photo Generator — 本地 2API demo 网关
端口: 8955

设计目标:
  * 把 App 的三步上传链（presigned-link → S3 PUT → qwen-editing）封装成一次 multipart 调用
  * 按风格 is_video 自动路由: 图片走 /api/v5.1/qwen-editing，视频走 /api/v1/workflow/execute 轮询
  * 暴露 /stats 让压测脚本与浏览器都能看实时成功率/耗时

运行: python demo_server.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response

from fravix_client import FravixClient, FravixError

ROOT = Path(__file__).parent
CATALOG = json.loads((ROOT / "styles_catalog.json").read_text(encoding="utf-8"))
STYLES: List[Dict[str, Any]] = CATALOG["styles"]
STYLE_BY_ID = {s["id"]: s for s in STYLES}

app = FastAPI(title="Fravix AI Photo Generator 2API Demo", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_client = FravixClient()
_stats = {
    "started_at": time.time(),
    "requests": 0,
    "image_ok": 0,
    "image_fail": 0,
    "video_ok": 0,
    "video_fail": 0,
    "latency_ms": {"image": deque(maxlen=200), "video": deque(maxlen=200)},
    "errors": deque(maxlen=30),
    "recent": deque(maxlen=20),
}


def _record(kind: str, ok: bool, ms: float, detail: Optional[str] = None) -> None:
    _stats["requests"] += 1
    _stats[f"{kind}_{'ok' if ok else 'fail'}"] += 1
    _stats["latency_ms"][kind].append(ms)
    if not ok and detail:
        _stats["errors"].append({"t": time.time(), "kind": kind, "error": detail[:300]})
    _stats["recent"].append({"t": time.time(), "kind": kind, "ok": ok, "ms": round(ms, 1)})


def _pct(values: Deque[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return round(ordered[idx], 1)


@app.on_event("startup")
async def _warm() -> None:
    """预热: 拿一次服务器时间戳，后续签名不必每次联网取。"""
    try:
        await asyncio.to_thread(_client.server_timestamp)
        print(f"[warmup] server ts offset = {_client._server_offset_ms} ms")
    except Exception as exc:  # noqa: BLE001
        print(f"[warmup] failed: {exc} (签名将回退系统时间)")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((ROOT / "demo.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "base_url": _client.base_url,
        "bundle": _client.bundle_id,
        "app_version": _client.app_version,
        "styles": len(STYLES),
        "video_styles": sum(1 for s in STYLES if s.get("is_video")),
        "uptime_s": round(time.time() - _stats["started_at"], 1),
    }


@app.get("/styles")
async def styles(video: Optional[bool] = None, category: Optional[str] = None) -> Dict[str, Any]:
    items = STYLES
    if video is not None:
        items = [s for s in items if bool(s.get("is_video")) is video]
    if category:
        items = [s for s in items if s.get("categoryCode") == category]
    return {
        "count": len(items),
        "categories": sorted({s.get("categoryCode") for s in STYLES}),
        "items": [
            {
                "id": s["id"],
                "name": s.get("style_name"),
                "thumbnail": s.get("thumbnail"),
                "category": s.get("categoryCode"),
                "is_video": bool(s.get("is_video")),
                "mode": s.get("mode"),
            }
            for s in items
        ],
    }


@app.get("/stats")
async def stats() -> Dict[str, Any]:
    lat = _stats["latency_ms"]
    total = _stats["requests"]
    ok = _stats["image_ok"] + _stats["video_ok"]
    return {
        "uptime_s": round(time.time() - _stats["started_at"], 1),
        "requests": total,
        "success_rate": round(ok / total * 100, 2) if total else 0.0,
        "image": {
            "ok": _stats["image_ok"],
            "fail": _stats["image_fail"],
            "p50_ms": _pct(lat["image"], 50),
            "p95_ms": _pct(lat["image"], 95),
        },
        "video": {
            "ok": _stats["video_ok"],
            "fail": _stats["video_fail"],
            "p50_ms": _pct(lat["video"], 50),
            "p95_ms": _pct(lat["video"], 95),
        },
        "errors": list(_stats["errors"])[-10:],
        "recent": list(_stats["recent"]),
    }


async def _run_image(image_bytes: bytes, prompt: Optional[str], accept_nsfw: bool, content_type: str) -> Dict[str, Any]:
    def _work() -> Dict[str, Any]:
        path = _client.upload_and_get_path(image_bytes, content_type)
        return _client.qwen_editing([path], positive_prompt=prompt, accept_nsfw=accept_nsfw)

    t0 = time.time()
    try:
        data = await asyncio.to_thread(_work)
    except (FravixError, requests.RequestException) as exc:
        _record("image", False, (time.time() - t0) * 1000, str(exc))
        raise HTTPException(status_code=502, detail=f"upstream: {exc}") from exc
    _record("image", True, (time.time() - t0) * 1000)
    return {"type": "image", "url": data.get("url"), "path": data.get("path"), "elapsed_ms": round((time.time() - t0) * 1000)}


async def _run_video(
    image_bytes: bytes, prompt: str, negative: Optional[str], content_type: str, timeout_s: int
) -> Dict[str, Any]:
    def _work() -> Dict[str, Any]:
        return _client.generate_video(image_bytes, prompt, negative, timeout_s=timeout_s)

    t0 = time.time()
    try:
        run = await asyncio.to_thread(_work)
    except (FravixError, requests.RequestException) as exc:
        _record("video", False, (time.time() - t0) * 1000, str(exc))
        raise HTTPException(status_code=502, detail=f"upstream: {exc}") from exc
    ok = str(run.get("status", "")).lower() == "success" and bool(run.get("resultFile"))
    if not ok:
        _record("video", False, (time.time() - t0) * 1000, json.dumps(run)[:300])
        raise HTTPException(status_code=502, detail=f"workflow {run.get('status')} {run.get('errorCode')}")
    _record("video", True, (time.time() - t0) * 1000)
    return {
        "type": "video",
        "url": run["resultFile"],
        "run_id": run.get("runId"),
        "elapsed_ms": round((time.time() - t0) * 1000),
    }


@app.post("/api/v1/generate")
async def generate(
    image: UploadFile = File(...),
    style_id: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    accept_nsfw: bool = Form(False),
    timeout_s: int = Form(240),
    force: Optional[str] = Form(None, description="image|video 强制路由，默认按风格 is_video"),
) -> Dict[str, Any]:
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="image > 12MB")
    content_type = image.content_type or "image/jpeg"
    if "png" in content_type:
        ctype = "image/png"
    else:
        ctype = "image/jpg"  # presigned URL 里签的是 image%2Fjpg

    style = STYLE_BY_ID.get(style_id) if style_id else None
    if style_id and style is None:
        raise HTTPException(status_code=404, detail=f"unknown style_id {style_id}")

    final_prompt = prompt or (style or {}).get("positivePrompt")
    negative = (style or {}).get("negativePrompt")
    nsfw = bool(accept_nsfw or (style or {}).get("accept_nsfw", False))
    route = force or ("video" if (style or {}).get("is_video") else "image")
    request_id = uuid.uuid4().hex[:12]

    if route == "video":
        if not final_prompt:
            raise HTTPException(status_code=400, detail="video route requires prompt/style")
        result = await _run_video(raw, final_prompt, negative, ctype, timeout_s)
    else:
        result = await _run_image(raw, final_prompt, nsfw, ctype)

    result.update({"request_id": request_id, "style_id": style_id, "route": route, "prompt": (final_prompt or "")[:200]})
    return result


@app.post("/api/v1/generate/sync")
async def generate_sync(
    image: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    accept_nsfw: bool = Form(False),
) -> Dict[str, Any]:
    """只走 qwen-editing 图片链（便于压测隔离掉视频轮询开销）。"""
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image")
    ctype = "image/png" if "png" in (image.content_type or "") else "image/jpg"
    return await _run_image(raw, prompt, accept_nsfw, ctype)


@app.get("/media")
async def media(url: str = Query(..., description="上游产出直链")) -> Response:
    """CDN 直链代理（demo 内联展示用；避免个别网络环境直连 CDN 失败）。"""
    host_allow = ("static.aperogroup.ai", "sr-cdn.aperogroup.ai", "s3.amazonaws.com")
    if not any(h in url for h in host_allow):
        raise HTTPException(status_code=400, detail="host not allowed")
    r = await asyncio.to_thread(requests.get, url, timeout=90)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"cdn {r.status_code}")
    return Response(content=r.content, media_type=r.headers.get("Content-Type", "application/octet-stream"))


@app.get("/debug/signature")
async def debug_signature() -> Dict[str, Any]:
    """暴露一次签名构造过程，便于排查签名相关问题。"""
    ts = _client.now_ms()
    sig = _client.sign(ts)
    return {
        "api_key_masked": _client.api_key[:6] + "..." + _client.api_key[-4:],
        "timestamp": ts,
        "signature_len": len(sig),
        "signature": base64.b64encode(base64.b64decode(sig)).decode()[:64] + "...",
        "headers": {k: (v[:40] + "..." if len(v) > 40 else v) for k, v in _client.headers().items()},
    }


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8955"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
