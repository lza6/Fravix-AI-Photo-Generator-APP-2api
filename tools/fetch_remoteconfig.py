"""
从 Firebase Remote Config 拉取 Fravix(Apero 白标) 的签名凭据 → config.local.json

用法:
    python tools/fetch_remoteconfig.py                # 自动拉取并写 config.local.json
    python tools/fetch_remoteconfig.py --dump-all     # 额外导出全部 205 条 RC entries

原理（逆向自 com.apero.data.datasource.local.preferences.remoteconfig.*）:
    App 的 api_key / public_key / bundle_id / workflow_id 不在 APK 里，
    启动时从 Firebase Remote Config 同步并缓存到 SharedPreferences。
    只要用 APK 内的公开 firebase client 配置走
      FIS installations  → app_instance_id
      remoteconfig fetch  → entries
    就能拿到与 App 完全一致的配置。

注意: 这里使用的 google_api_key 是 Firebase 客户端公开标识（APK strings.xml 内可见），
      不构成服务端凭据；拉取到的 app_api_key/app_public_key 属于该白标 App 的
      共享签名凭据，请勿公开提交（本仓库 .gitignore 已排除 config.local.json）。
"""

from __future__ import annotations

import argparse
import json
import random
import string
import urllib.request
from pathlib import Path

PROJECT = "aip862-ai-hair-changer"
GOOGLE_API_KEY = "AIzaSyBZZDuG71TR_FsIvNSlAu5TlvBz2GYXvMM"  # 公开 client key（APK strings.xml）
APP_ID = "1:1096519110136:android:bcb66291e035a6d3b0da0d"
PACKAGE = "com.hairai.hairstyletryon.haircolorchanger"
CERT_SHA1 = "38918a453d07199354f8b19af05ec6562ced5788"

OUT = Path(__file__).resolve().parent.parent / "config.local.json"


def _post(url: str, body: dict, extra: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "X-Android-Package": PACKAGE, "X-Android-Cert": CERT_SHA1}
    headers.update(extra or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_installation_id() -> str:
    try:
        r = _post(
            f"https://firebaseinstallations.googleapis.com/v1/projects/{PROJECT}/installations",
            {"appId": APP_ID, "authVersion": "FIS_v2", "sdkVersion": "a:17.0.0"},
            {"x-goog-api-key": GOOGLE_API_KEY},
        )
        fid = r["fid"]
        print(f"[FIS] installation id = {fid}")
        return fid
    except Exception as exc:  # noqa: BLE001
        fid = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(22))
        print(f"[FIS] 失败({exc})，退回随机 app_instance_id = {fid}")
        return fid


def fetch_remote_config(fid: str) -> dict:
    return _post(
        f"https://firebaseremoteconfig.googleapis.com/v1/projects/{PROJECT}/namespaces/firebase:fetch?key={GOOGLE_API_KEY}",
        {
            "app_instance_id": fid,
            "app_id": APP_ID,
            "language_code": "en-US",
            "platform_version": "android 13",
            "time_zone": "UTC",
            "package_name": PACKAGE,
        },
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-all", action="store_true", help="同时导出 remoteconfig.json 全量快照")
    args = ap.parse_args()

    entries = fetch_remote_config(fetch_installation_id()).get("entries", {})
    print(f"[RC] entries = {len(entries)}")

    cfg = {
        "api_key": entries.get("app_api_key", ""),
        "public_key": entries.get("app_public_key", ""),
        "bundle_id": entries.get("app_bundle_id", PACKAGE),
        "app_name": entries.get("app_app_name", ""),
        "workflow_id": entries.get("app_workflow_id", ""),
    }
    if not cfg["api_key"] or not cfg["public_key"]:
        print("[RC] 缺少 app_api_key / app_public_key —— 上游可能已改为其它下发方式")
        return 1

    OUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[OK] 写入 {OUT}")
    print(f"     api_key    = {cfg['api_key'][:8]}…{cfg['api_key'][-4:]}")
    print(f"     bundle_id  = {cfg['bundle_id']}")
    print(f"     workflow_id= {cfg['workflow_id']}")

    styles = entries.get("data_style", "")
    if styles:
        raw = styles[5:] if styles.startswith("json\n") else styles
        try:
            print(f"[RC] 风格数 = {len(json.loads(raw))}")
        except Exception:  # noqa: BLE001
            pass

    if args.dump_all:
        dump = OUT.parent / "remoteconfig.json"
        dump.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[OK] 全量快照 → {dump}（含密钥，勿提交）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
