# Fravix 2.1.1 — 上游协议规格

目标 App：`com.hairai.hairstyletryon.haircolorchanger`（Apero / Vision Lab 白标 AIP862 皮肤）
上游网关：`https://cloth-change-core.aperogroup.ai`

> 本文件只描述协议；签名凭据需自行用 `tools/fetch_remoteconfig.py` 从 Firebase Remote Config 拉取。

---

## 1. 凭据来源（关键设计）

App 启动时从 **Firebase Remote Config** 同步 5 个值并缓存进 SharedPreferences：

| RC key | 用途 |
|--------|------|
| `app_api_key` | 签名 keyId |
| `app_public_key` | RSA 公钥（DER + Base64），本地加密签名串 |
| `app_bundle_id` | 请求头 `X-Api-BundleId`，**与 api_key 绑定** |
| `app_app_name` | 请求头 `App-name` |
| `app_workflow_id` | 图生视频 workflow 标识 |

同一份 RC 还下发 `data_style`（125 条风格，含 positivePrompt/negativePrompt/is_video）、
`data_category`（17 条）、`data_banner`、全套广告位与开关，共 205 条。

获取方式（`tools/fetch_remoteconfig.py` 已实现）：

```
POST https://firebaseinstallations.googleapis.com/v1/projects/<PROJECT>/installations
     header: x-goog-api-key=<公开 client key>, X-Android-Package, X-Android-Cert
     body  : {"appId":..., "authVersion":"FIS_v2", "sdkVersion":"a:17.0.0"}
  → app_instance_id (fid)

POST https://firebaseremoteconfig.googleapis.com/v1/projects/<PROJECT>/namespaces/firebase:fetch?key=<公开 client key>
     body  : {"app_instance_id":fid, "app_id":..., "language_code":"en-US",
              "platform_version":"android 13", "time_zone":"UTC", "package_name":<pkg>}
  → {"entries": {...205 项...}}
```

`data_style` 的值可能是 `json\n[ ... ]` 形式，需先剥掉前缀再 `json.loads`。

---

## 2. 鉴权：RSA-PKCS1 签名

```text
payload   = f"{timestamp_ms}@@@{app_api_key}@@@{randint(0, 999999)}"
signature = base64( RSA_PKCS1v15_encrypt(app_public_key, payload) )
```

APK 对应实现：`com.apero.signature.SignatureParserImpl#encryptSignature`
（`Cipher.getInstance("RSA/None/PKCS1Padding", "BC")`，等价于标准 PKCS#1 v1.5 加密，**不是** OAEP）。

### 2.1 请求头

| Header | 必需 | 说明 |
|--------|------|------|
| `X-Api-Signature` | ✅ | 上述 Base64 |
| `X-Api-Timestamp` | ✅ | 与 payload 内 timestamp 完全一致（毫秒） |
| `X-Api-BundleId` | ✅ | 必须与 api_key 绑定的包名一致（换值 → `401 ERR15`） |
| `app-version` / `country-code` / `App-name` / `x-api-token` / `User-Agent` | ❌ | App 会带；实测删除仍 200，建议保留 |

### 2.2 时间戳事实（实测）

- `GET /timestamp` → `{"statusCode":200,"data":{"timestamp":1793284667375,...}}`
- 容差：`-270s` 通过、`-300s` 拒绝 → **窗口约 5 分钟**
- 因此可以缓存 server offset（建议 TTL 60s），同一 timestamp 可复用多次签名
- 签名本身无状态（payload 内自带随机数），单批请求可共用一个 ts

### 2.3 边缘层坑（必须知道）

`/timestamp` **也必须**携带 `x-api-signature: x`、`x-api-timestamp: x`、`x-api-bundleid: x`、`App-name: x`
四个占位头（值可任意），否则 Cloudflare 前置直接

```
HTTP 412  {"error": "Precondition Failed"}
```

这是最容易卡住第一小时的坑。

---

## 3. 图片链路（122 个风格）

```
① GET  /api/v5.1/qwen-editing/presigned-link
   → {"data":{"url":"https://s3.ap-southeast-1.amazonaws.com/cdn-prod-static-aperovn/ai-core-qwen-editing/<yyyyMMdd>/<uuid>/input.jpg?X-Amz-...","path":"ai-core-qwen-editing/<yyyyMMdd>/<uuid>/input.jpg"}}

② PUT  {url}
   Content-Type: image/jpg        # 与 presigned URL 内 Content-Type=image%2Fjpg 一致，否则 403
   body: 原始字节
   注意：PUT 不带任何签名头（APK 里 NOT_ADD_HEADER_METHODS = ["PUT"]）

③ POST /api/v5.1/qwen-editing
   {"files":["<path>"],"mode":"IMAGE_EDITING","positivePrompt":"<风格提示词>","acceptNSFW":false}
   → 201 {"statusCode":200,"data":{"url":"https://static.aperogroup.ai/.../output/xxx_0.jpeg","path":"..."}}
```

- 同步阻塞：实测 **12~19s**，输出固定 **1120×1120 JPEG**
- `files` 支持多图（数组），实测单图足够
- 输入可以是任意图；实测喂一张空白纯色图 + 提示词，也能产出完整人像（等效纯文生图）

---

## 4. 视频链路（9 个 `is_video` 风格）

```
① POST /api/v1/workflow/execute
   {"workflowId":"<app_workflow_id>",
    "input":{"files":["<path>"],"positivePrompt":"...","negativePrompt":"..."}}
   → 201 {"runId":"<uuid>","status":"queued"}

② GET  /api/v1/workflow/execute/{runId}
   → {"runId":..,"workflowId":..,"status":"running|success|failed","errorCode":null,
      "output":{"resultFile":"https://static.aperogroup.ai/.../xxx_0.mp4","resultStatus":"completed"}}
```

- 轮询间隔 3s；实测 **16~39s** 完成，产出 1~1.5MB mp4
- **坑**：`input` 里不能出现 `mode` 字段。带 `"mode":"VIDEO_EDITING"` → 立即 `status=failed` / `errorCode=ERR103`
- APK 端 `VideoInhouseInput` 的 `mode/steps/width/height/frameRate/frameNumber` 全部传 null

---

## 5. 风格表与路由

```json
{"id","style_name","thumbnail","thumbnail_be_af","categoryCode","group","positivePrompt",
 "negativePrompt","accept_nsfw","is_active","on_home","is_video","require_image","mode","order"}
```

快照分布：125 条 → `mode=IMAGE_EDITING` 122、`mode=VIDEO_EDITING` 3；`is_video=true` **9**。

路由判定来自 `com.apero.domain.model.GenerationType.fromStyle`：

> **`is_video == true` → workflow（视频）；否则 → qwen-editing（图片）**

⚠️ 不能用 `mode` 判定：有 6 条 `HAIR_VIDEO` 风格 `mode` 仍写着 `IMAGE_EDITING`。

完整风格表见仓库根 `styles_catalog.json`。

---

## 6. 错误码

| HTTP | errorCode | 含义 / 处理 |
|------|-----------|-------------|
| 412 | – | 边缘层：请求缺占位头 |
| 401 | ERR15 | 未授权（无签名 / bundleId 与 key 不匹配）→ 重新拉 RC，不要盲重试 |
| 400 | ERR06 | 签名无效（payload 拼装或填充方式错） |
| 400 | ERR05 | 时间戳超窗 → 刷新 server offset 重试 1 次 |
| 201→failed | ERR103 | workflow 入参非法（典型：多传 `input.mode`） |
| – | ERR120 | 内容安全拦截（NSFW） |
| 599 | – | 客户端自定义"网络超时"代码（非上游） |

---

## 7. 其它已定位但未一一打通的端点

| 端点 | 说明 |
|------|------|
| `POST /api/v1/{venture}/{appCode}/report` | 内容/问题上报（`ReportService`） |
| `POST https://n.apero.vn/api/integrity-token` | Play Integrity 校验；**本链路未强制**，无需通过 |
| `https://n.aperogroup.ai` | 同族 App 的 AI 网关（Fravix 内也存在该常量，但当前实际只用 cloth-change-core） |
| `ghibli.dailymorningupdate.com` / `api.imagix.dailymorningupdate.com` / `faceswap.dailymorningupdate.com` | RC `AppSettings` 中的第二条后端线（文生图 / 文生视频），被 Cloudflare 1010 阻挡，**未打通** |

---

## 8. 参考实现映射

`fravix_client.py` 是本文档的可执行版本：

| 方法 | 端点 |
|------|------|
| `server_timestamp()` | `GET /timestamp` |
| `sign(ts)` / `headers()` | RSA 签名与请求头 |
| `presigned_link()` | `GET /api/v5.1/qwen-editing/presigned-link` |
| `upload()` | `PUT {presigned}` |
| `qwen_editing()` | `POST /api/v5.1/qwen-editing` |
| `execute_workflow()` / `workflow_run()` | `POST/GET /api/v1/workflow/execute[/{runId}]` |
| `generate()` / `generate_video()` | 两条完整链路 |
