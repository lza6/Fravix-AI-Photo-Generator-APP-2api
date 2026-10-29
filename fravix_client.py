"""
Fravix (AI Photo Generator 2.1.1) — 纯协议客户端 / pure-protocol client

Target : https://play.google.com/store/apps/details?id=com.hairai.hairstyletryon.haircolorchanger
Upstream: https://cloth-change-core.aperogroup.ai   (Apero Group white-label AI engine)

凭据 / credentials
------------------
Apero 的签名凭据不在 APK 内，由 Firebase Remote Config 下发：
    app_api_key, app_public_key, app_bundle_id, app_app_name, app_workflow_id
先用 `python tools/fetch_remoteconfig.py` 自行拉取（会写出 config.local.json），
或手工通过环境变量注入：

    FRAVIX_API_KEY / FRAVIX_PUBLIC_KEY / FRAVIX_BUNDLE_ID
    FRAVIX_APP_NAME / FRAVIX_WORKFLOW_ID

协议细节见 docs/PROTOCOL.md。
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://cloth-change-core.aperogroup.ai"
OKHTTP_UA = "okhttp/4.12.0"
DEFAULT_API_TOKEN = "not_get_api_token"

# 公开标识（APK 内可见，非机密）
BUNDLE_ID = "com.hairai.hairstyletryon.haircolorchanger"
APP_NAME = "AI_AIP862_com.hairai.hairstyletryon.haircolorchanger_Android_Vision_Lab"
APP_VERSION = "2.1.1"
WORKFLOW_ID = "b8c9535d-1d12-488f-8033-deeef515f332"

CONFIG_FILE = Path(__file__).with_name("config.local.json")


class FravixError(RuntimeError):
    pass


def load_config() -> Dict[str, str]:
    """环境变量优先，其次 config.local.json（由 tools/fetch_remoteconfig.py 生成）。"""
    cfg = {
        "api_key": os.environ.get("FRAVIX_API_KEY", ""),
        "public_key": os.environ.get("FRAVIX_PUBLIC_KEY", ""),
        "bundle_id": os.environ.get("FRAVIX_BUNDLE_ID", BUNDLE_ID),
        "app_name": os.environ.get("FRAVIX_APP_NAME", APP_NAME),
        "workflow_id": os.environ.get("FRAVIX_WORKFLOW_ID", WORKFLOW_ID),
    }
    if CONFIG_FILE.exists():
        local = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for k, v in local.items():
            if not cfg.get(k):
                cfg[k] = v
    if not cfg["api_key"] or not cfg["public_key"]:
        raise FravixError(
            "缺少签名凭据：先运行 `python tools/fetch_remoteconfig.py`，"
            "或设置 FRAVIX_API_KEY / FRAVIX_PUBLIC_KEY 环境变量（见 README）。"
        )
    return cfg


class FravixClient:
    # 边缘层要求 /timestamp 也带这套头（值可为占位符），否则 Cloudflare 前置 412
    PLACEHOLDER_HEADERS = {
        "User-Agent": OKHTTP_UA,
        "accept": "application/json",
        "content-type": "application/json; charset=UTF-8",
        "x-api-signature": "x",
        "x-api-timestamp": "x",
        "x-api-bundleid": "x",
        "App-name": "x",
    }

    def __init__(
        self,
        base_url: str = BASE_URL,
        config: Optional[Dict[str, str]] = None,
        country_code: str = "US",
        timeout: int = 120,
        session: Optional[requests.Session] = None,
    ) -> None:
        cfg = config or load_config()
        self.base_url = base_url.rstrip("/")
        self.api_key = cfg["api_key"]
        self.public_key = cfg["public_key"]
        self.bundle_id = cfg.get("bundle_id") or BUNDLE_ID
        self.app_name = cfg.get("app_name") or APP_NAME
        self.workflow_id = cfg.get("workflow_id") or WORKFLOW_ID
        self.app_version = APP_VERSION
        self.country_code = country_code
        self.timeout = timeout
        self.session = session or requests.Session()
        self._server_offset_ms: Optional[int] = None
        self._pub = serialization.load_der_public_key(base64.b64decode(self.public_key))

    # ---------------- auth ----------------
    def server_timestamp(self) -> int:
        r = self.session.get(f"{self.base_url}/timestamp", headers=self.PLACEHOLDER_HEADERS, timeout=30)
        if r.status_code == 412:
            raise FravixError("HTTP 412 on /timestamp: 边缘层拒绝，检查 PLACEHOLDER_HEADERS")
        r.raise_for_status()
        ts = int(r.json()["data"]["timestamp"])
        self._server_offset_ms = ts - int(time.time() * 1000)
        return ts

    def now_ms(self) -> int:
        if self._server_offset_ms is None:
            try:
                return self.server_timestamp()
            except Exception:  # noqa: BLE001
                return int(time.time() * 1000)
        return int(time.time() * 1000) + self._server_offset_ms

    def sign(self, ts: int) -> str:
        payload = f"{ts}@@@{self.api_key}@@@{int(random.random() * 1000000)}".encode()
        return base64.b64encode(self._pub.encrypt(payload, padding.PKCS1v15())).decode()

    def headers(self) -> Dict[str, str]:
        ts = self.now_ms()
        return {
            "User-Agent": OKHTTP_UA,
            "app-version": self.app_version,
            "country-code": self.country_code,
            "X-Api-Signature": self.sign(ts),
            "X-Api-Timestamp": str(ts),
            "App-name": self.app_name,
            "X-Api-BundleId": self.bundle_id,
            "x-api-token": DEFAULT_API_TOKEN,
            "Accept": "application/json",
        }

    # ---------------- http ----------------
    def _get(self, path: str) -> Any:
        r = self.session.get(f"{self.base_url}{path}", headers=self.headers(), timeout=self.timeout)
        return self._unwrap(r)

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        r = self.session.post(
            f"{self.base_url}{path}", json=payload, headers=self.headers(), timeout=self.timeout
        )
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: requests.Response) -> Any:
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            raise FravixError(f"HTTP {r.status_code} non-json: {r.text[:300]}")
        if r.status_code >= 400:
            raise FravixError(f"HTTP {r.status_code}: {str(body)[:400]}")
        code = body.get("statusCode")
        if code is not None and int(code) != 200:
            raise FravixError(f"statusCode={code} message={body.get('message')!r}")
        return body.get("data", body)

    # ---------------- image chain ----------------
    def presigned_link(self) -> Dict[str, str]:
        return self._get("/api/v5.1/qwen-editing/presigned-link")

    def upload(self, upload_url: str, content: bytes, content_type: str = "image/jpg") -> None:
        # PUT 不签名（APK: NetworkConstant.NOT_ADD_HEADER_METHODS = ["PUT"]）
        # Content-Type 必须与 presigned URL 里签的 image%2Fjpg 一致，否则 403
        r = self.session.put(
            upload_url, data=content, headers={"User-Agent": OKHTTP_UA, "Content-Type": content_type}, timeout=self.timeout
        )
        if r.status_code >= 400:
            raise FravixError(f"upload HTTP {r.status_code}: {r.text[:200]}")

    def upload_and_get_path(self, image_bytes: bytes, content_type: str = "image/jpg") -> str:
        link = self.presigned_link()
        self.upload(link["url"], image_bytes, content_type)
        return link["path"]

    def qwen_editing(
        self,
        files: List[str],
        mode: str = "IMAGE_EDITING",
        positive_prompt: Optional[str] = None,
        accept_nsfw: bool = False,
    ) -> Dict[str, str]:
        payload: Dict[str, Any] = {"files": files, "mode": mode, "acceptNSFW": accept_nsfw}
        if positive_prompt is not None:
            payload["positivePrompt"] = positive_prompt
        return self._post("/api/v5.1/qwen-editing", payload)

    def generate(self, image_bytes: bytes, prompt: Optional[str] = None, accept_nsfw: bool = False) -> Dict[str, str]:
        """图生图 / 风格化，同步返回 {url, path}。"""
        path = self.upload_and_get_path(image_bytes)
        return self.qwen_editing([path], positive_prompt=prompt, accept_nsfw=accept_nsfw)

    # ---------------- video chain ----------------
    def execute_workflow(self, workflow_id: str, wf_input: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/api/v1/workflow/execute", {"workflowId": workflow_id, "input": wf_input})

    def workflow_run(self, run_id: str) -> Dict[str, Any]:
        return self._get(f"/api/v1/workflow/execute/{run_id}")

    def generate_video(
        self,
        image_bytes: bytes,
        prompt: str,
        negative_prompt: Optional[str] = None,
        workflow_id: Optional[str] = None,
        timeout_s: int = 240,
        interval_s: float = 3.0,
    ) -> Dict[str, Any]:
        """图生视频。input 里绝不能带 mode，带上会立即 ERR103。"""
        path = self.upload_and_get_path(image_bytes)
        wf_input: Dict[str, Any] = {"files": [path], "positivePrompt": prompt}
        if negative_prompt:
            wf_input["negativePrompt"] = negative_prompt
        started = self.execute_workflow(workflow_id or self.workflow_id, wf_input)
        run_id = started["runId"]
        deadline = time.time() + timeout_s
        last: Dict[str, Any] = started
        while time.time() < deadline:
            last = self.workflow_run(run_id)
            if str(last.get("status", "")).lower() in ("success", "failed", "error", "canceled"):
                break
            time.sleep(interval_s)
        last["runId"] = run_id
        last["resultFile"] = (last.get("output") or {}).get("resultFile")
        return last


if __name__ == "__main__":
    import sys

    client = FravixClient()
    print("server ts:", client.server_timestamp())
    print("presigned:", json.dumps(client.presigned_link(), ensure_ascii=False)[:160])
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as fh:
            raw = fh.read()
        prompt = sys.argv[2] if len(sys.argv) > 2 else "short platinum blonde pixie cut, studio lighting"
        print("generate:", client.generate(raw, prompt))
