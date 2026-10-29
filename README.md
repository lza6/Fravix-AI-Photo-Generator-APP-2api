# Fravix — AI Photo Generator 2.1.1 · APK 逆向 + 纯协议 2API

> 目标：`com.hairai.hairstyletryon.haircolorchanger`（Fravix / AI Photo Generator 2.1.1）
> 上游：`https://cloth-change-core.aperogroup.ai`（Apero Group 白标 AI 引擎）
> 结论：**图生图（122 风格）+ 图生视频（9 风格）纯协议打通，无需设备、无需 Frida、无需重打包**
> 性质：安全研究方法论沉淀 / 协议互操作研究。**不含任何上游共享签名凭据**，凭据需自行从 Firebase Remote Config 拉取。

---

## TL;DR

Fravix 是 **Apero（aperogroup.ai）的白标皮肤**（Vision Lab AIP862 模板 + `com.apero.*` SDK）。
它的签名密钥**不在 APK 里**，而是启动时从 **Firebase Remote Config** 同步：

```
app_api_key / app_public_key / app_bundle_id / app_app_name / app_workflow_id
```

拿到这 5 个值后，请求鉴权只有一件事：

```
X-Api-Signature = base64( RSA_PKCS1v15( public_key, "{ts}@@@{api_key}@@@{rand}" ) )
```

于是：

| 能力 | 端点 | 实测 |
|------|------|------|
| 图生图 / 风格化（122 风格） | `presigned-link` → S3 `PUT` → `POST /api/v5.1/qwen-editing` | 12~19s，1120×1120 JPEG |
| 图生视频（9 风格） | `POST /api/v1/workflow/execute` + 轮询 | 16~39s，mp4 |
| 服务器时间戳 | `GET /timestamp` | 容差 ≈5 分钟 |

完整协议：[`docs/PROTOCOL.md`](docs/PROTOCOL.md) ｜ new-api 对接作业单：[`docs/NEW-API-HANDOFF.md`](docs/NEW-API-HANDOFF.md)

---

## 快速开始

```bash
pip install requests cryptography fastapi uvicorn python-multipart   # playwright 可选（浏览器验证）
# 可选：pip install playwright && playwright install chromium

# 1) 拉取本 App 的签名凭据（FIS + Firebase Remote Config）
python tools/fetch_remoteconfig.py
#    → 生成 config.local.json（已在 .gitignore 中）

# 2) 冒烟
python fravix_client.py media/test_face_real.jpg "short platinum blonde pixie cut"

# 3) 起网关 + Demo 页
python demo_server.py            # http://127.0.0.1:8955

# 4) 压测 / 浏览器路径验证
python stress_test.py            # 约 3 分钟，9 个场景
python stress_test.py --quick    # 只跑健康/签名/错误路径
python browser_test.py           # Playwright 截图 → browser_demo.png / browser_result.png
```

也可用环境变量代替 `config.local.json`：

```bash
export FRAVIX_API_KEY=...        # Remote Config 的 app_api_key
export FRAVIX_PUBLIC_KEY=...     # app_public_key（DER+Base64）
export FRAVIX_BUNDLE_ID=com.hairai.hairstyletryon.haircolorchanger
export FRAVIX_WORKFLOW_ID=b8c9535d-1d12-488f-8033-deeef515f332
```

Python 用法：

```python
from fravix_client import FravixClient
import requests

c = FravixClient()
r = c.generate(open("media/test_face_real.jpg", "rb").read(),
               prompt="short curly copper afro, studio lighting")
open("out.jpg", "wb").write(requests.get(r["url"]).content)

v = c.generate_video(open("media/test_face_real.jpg", "rb").read(),
                     prompt="cinematic 1980s retro couple portrait, vertical 9:16")
print(v["status"], v["resultFile"])
```

---

## 逆向路径

1. `unzip` APK → dex 字符串扫域名 → 命中 `cloth-change-core.aperogroup.ai`
2. `jadx` 反编译 25k 类 → 网络层 `com.apero.data.datasource.remote.*`
3. 鉴权核心：`SignatureInterceptor` + `SignatureParserImpl`
   - `payload = "{timestamp}@@@{keyId}@@@{random(0..999999)}"`
   - `Cipher.getInstance("RSA/None/PKCS1Padding")`（= 标准 PKCS#1 v1.5，非 OAEP）
4. 密钥不在 APK → 追溯到 `com.apero.data.datasource.local.preferences.remoteconfig.*`
   - APK `strings.xml` 有 `project_id` / `google_api_key` / `google_app_id`
   - FIS 取 `app_instance_id` → `firebaseremoteconfig.../firebase:fetch`
   - 一次返回 **205 条**配置（含 125 条风格表 `data_style`）
5. 黑盒验证：签名 → 三步上传 → `qwen-editing` 出图 → `workflow` 出视频

### 鉴权实测事实

| Header | 必需 | 备注 |
|--------|------|------|
| `X-Api-Signature` | ✅ | RSA PKCS1v15 |
| `X-Api-Timestamp` | ✅ | 与签名内 ts 一致；容差 ≈5min（-270s ok / -300s ERR05） |
| `X-Api-BundleId` | ✅ | **与 api_key 绑定**，换值 → `401 ERR15` |
| `App-name` / `app-version` / `country-code` / `x-api-token` | ❌ | 删掉仍 200 |

错误码：`412` 边缘层缺占位头 → `ERR15` 未授权 → `ERR06` 签名错 → `ERR05` 时间戳过期 → `ERR103` 视频入参非法 → `ERR120` NSFW。

---

## 交付物

```
fravix_client.py           纯协议客户端（签名 / 三步上传 / 生图 / 生视频）
demo_server.py             FastAPI 2API 网关 :8955（按 is_video 自动路由，含 /stats,/styles,/media）
demo.html                  浏览器 Demo（上传 → 风格库 → 出图/出片 → 实时统计）
stress_test.py             9 场景压测 → benchmark_results.json
browser_test.py            Playwright 真实浏览器路径验证
tools/fetch_remoteconfig.py  FIS + Firebase RC 取凭据 → config.local.json
styles_catalog.json        125 条风格（含 positivePrompt/negativePrompt/is_video/mode）+ 17 分类
benchmark_results.json     压测原始数据
media/                     样例输入/输出、视频、浏览器截图
docs/PROTOCOL.md           协议规格
docs/NEW-API-HANDOFF.md    new-api Go 渠道对接作业单（含代码骨架 + 验收清单）
```

---

## 实测数据（2026-10-29，本机直连，27 次真实生成，成功率 100%）

| 场景 | n | p50 | p95 | max |
|------|---|-----|-----|-----|
| 网关 `/health` | 20 | 3.5ms | 8.2ms | 13.5ms |
| 上游 `/timestamp` | 10 | 568ms | 1923ms | 1923ms |
| 本地 RSA 签名 | 200 | <0.1ms | 0.1ms | 5.1ms（≈27.6k op/s） |
| `presigned-link` | 5 | 858ms | 4075ms | 4075ms |
| 全链路图片 | 5 | **17.0s** | 18.6s | 18.6s |
| 并发 c3×2 | 6 | 18.1s | 21.0s | 21.0s |
| 图生视频 | 1 | **39.5s** | — | — |
| 错误路径断言 | 5 | — | — | **5/5 PASS** |

> 上表为 R1 首轮（网关 :8955）。**公开仓库复现跑**（网关 :18555，203.9s，同样 27 次真实生成、成功率 100%）：
> 图片 p50 **14.9s** / p95 18.2s，c3 突发 6/6（max 17.2s），视频 **36.1s**，错误路径 5/5。

- 身份保持：同脸 + `curly_afro` → 雀斑/五官/脸型全保留，仅换发型与背景（见 `media/out_real_curly.jpg`）
- 意外能力：喂一张**空白纯色图** + 提示词也能产出完整人像（等效纯文生图）
- **未观察到服务端配额**：连续 50+ 次生成无 429；App 的"每日 1 次免费"是客户端 SharedPreferences 计数
- Playwright：125 风格渲染 → 选风格 → 上传 → 出图 22.3s → 统计 100%，控制台零 error

---

## 已知限制 / 风险

1. **凭据是白标共享资源**：上游随时可能轮换 Remote Config → 届时重跑 `tools/fetch_remoteconfig.py` 即可
2. **无服务端配额**：生产使用必须自建限流，避免触发上游风控（ERR120 / 封 key）
3. 视频风格只有 9 个，且路由必须用 `is_video`（6 条视频风格的 `mode` 仍写 `IMAGE_EDITING`）
4. 未打通：RC `AppSettings` 里的第二条后端线（`ghibli.dailymorningupdate.com` 文生图 / `api.imagix.dailymorningupdate.com` 文生视频），被 Cloudflare 1010 阻挡
5. 未验证：NSFW 拦截（ERR120）具体行为、`/api/v1/{venture}/{appCode}/report` 上报端点

---

## 免责声明

本项目仅用于 **安全研究、协议互操作性与自有系统集成可行性验证**。
仓库内不包含任何上游共享密钥、不包含 APK 原始文件与反编译产物。
使用前请自行评估目标服务条款与所在地区法律；由此产生的任何后果由使用者自行承担。
生成内容的责任由使用者承担，上游内容审核（ERR120）行为不受本项目控制。
