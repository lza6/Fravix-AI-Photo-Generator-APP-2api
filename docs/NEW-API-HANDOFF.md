# Fravix → new-api 渠道对接交接书（可直接交给另一个会话来落地）

> 本文是给 **new-api（本仓库 `new-api-Max`）实现者**的作业单。上游协议已完成逆向并实测通过，
> 协议细节见 [`PROTOCOL.md`](PROTOCOL.md)，可执行的 Python 参考实现在仓库根 `fravix_client.py`。
> 本仓库是 **Python 侧交付**；Go 渠道代码由你（或另一个会话）按本文落地。

---

## 0. 决策已定（不要重新讨论）

| 项 | 结论 |
|----|------|
| 对外模型数 | **单模型 + style 参数**（不按风格拆模型） |
| 模型名 | `fravix-image`（图生图）、`fravix-video`（图生视频） |
| 风格选择 | 请求体可选字段 `style`（值 = `styles_catalog.json` 里风格的 `id`），缺省 `ai_lookbook` |
| 协议形态 | 与本仓库既有 2API 渠道（`relay/channel/aivm`）一致：**chat passthrough**，`DoRequest` 内做真实上游调用，结果以 chat completion 返回产出直链 |
| 渠道能力 | `abilities: ["default", "2api"]` |

---

## 1. 需要新增/修改的文件（本仓库 `new-api-Max`）

| 文件 | 动作 |
|------|------|
| `constant/channel.go` | 新增 `ChannelTypeFravix = 74`（70=OpenChat142、71=Zyra、72=AIVM、73=Duck 已占用，见文件 70-73 行）；在名称映射表（约 230 行）加 `ChannelTypeFravix: "Fravix"` |
| `relay/channel/fravix/adaptor.go` | 新建，仿 `relay/channel/aivm/adaptor.go`（107 行）：`Init/GetRequestURL/SetupRequestHeader/GetModelList/GetChannelName/Convert*` |
| `relay/channel/fravix/do_request.go` | 新建，仿 `aivm/do_request.go`（292 行）：签名 + 上传 + 生成 + 结果拼装 |
| `relay/channel/fravix/styles_gen.go` | 新建，由 `styles_catalog.json` 生成的 `map[string]Style`（125 条，只需 `ID/Name/PositivePrompt/NegativePrompt/IsVideo/Mode`） |
| `relay/relay_adaptor.go` | 在 `case constant.ChannelTypeAIVM: return &aivm.Adaptor{}`（第 159 行）旁加 `case constant.ChannelTypeFravix: return &fravix.Adaptor{}`，并加 import |
| `relay/channel/fravix/adaptor_test.go` | 至少覆盖：签名正确性、style 解析、is_video 路由、错误码映射 |
| 渠道配置字段 | 复用 `channel.key` 存 **`app_api_key`**；把 `app_public_key`、`app_bundle_id`、`app_workflow_id` 放 `channel.other`（JSON）或全局 env，建议 env 优先、渠道字段兜底 |

---

## 2. 对外 API 契约

### 2.1 `fravix-image`

```http
POST /v1/chat/completions
Content-Type: application/json

{
  "model": "fravix-image",
  "style": "platinum_blonde",              // 可选，缺省 ai_lookbook
  "image_url": "https://example.com/face.jpg",  // 必填（也可用 image_base64）
  "messages": [{"role":"user","content":"换成铂金短发"}],   // 可选，覆盖风格提示词
  "accept_nsfw": false                      // 可选
}
```

返回（与 AIVM 同构的 chat completion，正文里给直链）：

```json
{"id":"chatcmpl-...","object":"chat.completion","model":"fravix-image",
 "choices":[{"index":0,"message":{"role":"assistant",
   "content":"https://static.aperogroup.ai/ai-core-qwen-editing/.../output/xxx_0.jpeg"},
   "finish_reason":"stop"}],
 "usage":{...}}
```

`usage` 至少填 `completion_tokens>0`（本仓库 2API 币/配额逻辑依赖它，参考 AIVM 的落库行为）。

### 2.2 `fravix-video`

同结构，`"model":"fravix-video"`，`content` 返回 mp4 直链。上游是异步 workflow，**必须**在 `DoRequest` 内轮询到 `status=success`（实测 16~39s），并把 HTTP client 超时设置 ≥ 300s。

### 2.3 风格解析优先级

1. 请求体 `style` 字段
2. `messages` 文本中 `#style=<id>`（便于纯文本客户端）
3. 缺省 `ai_lookbook`

`is_video=true` 的风格必须走视频链，**即使用户选了 `fravix-image`** —— 建议：`fravix-image` + 视频风格 → 返回 400 并提示改用 `fravix-video`（比静默改路由更可控）。

---

## 3. Go 实现骨架（关键代码，可直接抄）

### 3.1 签名（`crypto/rsa` + `crypto/x509`）

```go
func sign(ts int64, apiKey string, pubDERBase64 string) (string, error) {
    der, err := base64.StdEncoding.DecodeString(pubDERBase64)   // DER SubjectPublicKeyInfo
    if err != nil { return "", err }
    pubAny, err := x509.ParsePKIXPublicKey(der)
    if err != nil { return "", err }
    pub, ok := pubAny.(*rsa.PublicKey)
    if !ok { return "", errors.New("not rsa public key") }
    payload := fmt.Sprintf("%d@@@%s@@@%d", ts, apiKey, mrand.Intn(1000000))
    ct, err := rsa.EncryptPKCS1v15(rand.Reader, pub, []byte(payload))   // 必须 PKCS1v15，不是 OAEP
    if err != nil { return "", err }
    return base64.StdEncoding.EncodeToString(ct), nil
}
```

### 3.2 时间戳缓存（性能关键）

`/timestamp` 实测 p50 568ms / p95 1923ms，每次都取会把图片链拖慢 0.5s+：

```go
var (
    tsMu     sync.RWMutex
    tsOffset int64        // server - local，毫秒
    tsAt     time.Time
)

func serverTS(cl *http.Client) int64 {
    tsMu.RLock()
    if time.Since(tsAt) < 60*time.Second {
        off := tsOffset; tsMu.RUnlock()
        return time.Now().UnixMilli() + off
    }
    tsMu.RUnlock()
    // 取 /timestamp —— 必须带 4 个占位头，否则边缘 412
    req, _ := http.NewRequest(http.MethodGet, baseURL+"/timestamp", nil)
    req.Header.Set("x-api-signature", "x")
    req.Header.Set("x-api-timestamp", "x")
    req.Header.Set("x-api-bundleid", "x")
    req.Header.Set("App-name", "x")
    req.Header.Set("User-Agent", "okhttp/4.12.0")
    req.Header.Set("accept", "application/json")
    req.Header.Set("content-type", "application/json; charset=UTF-8")
    // ... 解析 data.timestamp，写入 tsOffset/tsAt
    // 失败时回退 time.Now().UnixMilli()
}
```

> 容差约 5 分钟，60s 刷新足够安全。

### 3.3 请求头

```go
h := http.Header{}
h.Set("X-Api-Signature", sig)
h.Set("X-Api-Timestamp", strconv.FormatInt(ts, 10))
h.Set("X-Api-BundleId", bundleID)   // 必须与 api_key 绑定，换值 → 401 ERR15
h.Set("App-name", appName)
h.Set("app-version", "2.1.1")
h.Set("country-code", "US")
h.Set("x-api-token", "not_get_api_token")
h.Set("User-Agent", "okhttp/4.12.0")
h.Set("Accept", "application/json")
```

### 3.4 图片链（三步）

```go
// 1) presigned
link := GET /api/v5.1/qwen-editing/presigned-link      // {url, path}
// 2) PUT —— 注意：不带签名头；Content-Type 必须 image/jpg
req, _ := http.NewRequest(http.MethodPut, link.URL, bytes.NewReader(imgBytes))
req.Header.Set("Content-Type", "image/jpg")
req.Header.Set("User-Agent", "okhttp/4.12.0")
// 3) 提交
POST /api/v5.1/qwen-editing
{"files":[link.Path],"mode":"IMAGE_EDITING","positivePrompt":prompt,"acceptNSFW":false}
→ data.url
```

### 3.5 视频链

```go
POST /api/v1/workflow/execute
{"workflowId": wfID, "input": {"files":[path], "positivePrompt":p, "negativePrompt":np}}
// ⚠️ input 里绝不能带 "mode"，带上立即 ERR103
→ runId，然后 3s 轮询 GET /api/v1/workflow/execute/{runId}
   直到 status ∈ {success, failed}；success 取 output.resultFile
```

---

## 4. 错误码 → 网关行为

| 上游 | 建议网关返回 | 处理 |
|------|--------------|------|
| 412 | 502 | 内部实现 bug（缺占位头），打日志告警 |
| 401 ERR15 | **401** + "渠道凭据失效" | 重新拉 RC（`tools/fetch_remoteconfig.py`）后更新渠道 key；**不要自动重试** |
| 400 ERR06 | 502 | 签名实现 bug |
| 400 ERR05 | 重试 1 次 | 刷新 server timestamp |
| ERR103 | 400 | 视频入参错（自己带了 mode） |
| ERR120 | 400 + 内容安全文案 | 不透传上游原文 |
| 网络错误/超时 | 重试 1 次（视频只重试用 runId 续轮询，勿重建 run） | |

---

## 5. 容量与限流建议（实测依据）

| 指标 | 实测 |
|------|------|
| 图片 p50 / p95 | 15.7s / 19.6s（网关侧） |
| 视频 p50 / p95 | 37.5s / 39.5s |
| 并发 c3 | 6/6 成功，max 21s；未见限流 |
| presigned-link 抖动 | 0.5~4.1s（图片链方差主因） |
| 本地签名吞吐 | ≈27.6k op/s（可忽略） |
| 服务端配额 | **未观察到**；App 的"每日 1 次免费"是客户端 SharedPreferences 计数 |

建议：`fravix-image` 单实例并发 3~5，`fravix-video` 1~2；统一走本仓库"2API 小时容量推算"面板。
自建限流是必须的（上游随时可能因风控返回 ERR120 或轮换 key）。

---

## 6. 验收清单（交给实现者逐条打勾）

- [ ] 渠道类型 74 在渠道编辑页可选，名称显示 `Fravix`
- [ ] 模型 `fravix-image` 真实出图：输入 URL 人像 + `style=platinum_blonde` → 返回 1120×1120 jpeg 直链
- [ ] 模型 `fravix-video` 真实出片：`style=retro_80s` → 返回可播放 mp4
- [ ] `style` 缺省时落到 `ai_lookbook` 且不报错
- [ ] `fravix-image` + 视频风格 → 400 明确报错（不静默改路由）
- [ ] 未带 image_url → 400，不触发上游调用
- [ ] 渠道 key 换成错误值 → 401，且日志能区分 ERR15
- [ ] 视频轮询：上游 failed → 502/400，且不无限轮询（≤ 300s）
- [ ] 计费：一次成功调用扣费 1 次、日志 `quota` 正确落库
- [ ] 并发 3 张图连续 3 轮无失败、无 goroutine 泄漏
- [ ] 单测覆盖签名/路由/错误码映射

---

## 7. 卡片式备忘（最容易被忽略的 5 个坑）

1. `/timestamp` 不带 4 个占位头 → **412**（不是 401，会误导排查方向）
2. `PUT` 上传**不能**带签名头；`Content-Type` 必须是 `image/jpg`（不是 `image/jpeg`）
3. `X-Api-BundleId` 与 api_key 强绑定，换值 **401 ERR15**
4. 视频 `input` 禁带 `mode` → 否则 **ERR103**
5. 风格路由用 **`is_video`**，不要用 `mode`（6 条视频风格 mode 仍写 IMAGE_EDITING）
