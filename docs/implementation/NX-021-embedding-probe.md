# NX-021 第一步：Embedding 端点实测报告

- 实测时间：2026-10-09（本机开发机，Python 3.12.10，标准库 `urllib`，无第三方 SDK）。
- 配置来源：负责人主 checkout 的私有 `.env`，经 `nexloop_eios.embedding_profile.load_embedding_profile(env_file=...)` 在进程内读取；key 未打印、未记录、未写入任何文件，`.env` 未复制进 worktree。
- 实测配置（公开部分）：`EMBEDDING_MODEL=doubao-embedding-vision-251215`，`EMBEDDING_MODE=multimodal`，端点主机 `ark.cn-beijing.volces.com`，路径 `/api/v3/embeddings/multimodal`。`.env` 中 `EMBEDDING_DIMENSION` 当前为空（`validation_mode=dimension_unmeasured`）。
- 输入：只用合成短语（“关注防水功能”“预算两千元以内”“在意是否防水”“价格不超过2000块”“喜欢户外跑步”“经常去越野跑”“合成短语第N号：偏好轻量款”、重复的“防水 ”），无任何真实客户数据。
- 探测脚本放在会话 scratchpad，不入库；可复测入口改为第二步提交的显式 opt-in 测试（见文末）。

## 结论

| 项 | 实测结果 |
|---|---|
| 纯中文文本 | **可用**。`input=[{"type":"text","text":"关注防水功能"}]` → HTTP 200，`usage.prompt_tokens_details.image_tokens=0` |
| 请求体 | `POST {"model":…, "input":[{"type":"text","text":…}], "dimensions"?:1024\|2048, "encoding_format"?:"float"\|"base64"}`，`Authorization: Bearer <key>` |
| 返回体 | `{"data":{"embedding":[float…],"object":"embedding"},"model":…,"object":"list","usage":{…},"created":…,"id":…}`；`data` 是**单个对象**，不是列表 |
| 默认维度 | **2048**（不传 `dimensions`） |
| 可选维度 | 只接受 `dimensions=1024` 或 `2048`；256 / 512 / 100 / 3072 → 400 `InvalidParameter`（param=`dimensions`） |
| 1024 与 2048 关系 | 1024 向量与 2048 向量前 1024 维余弦 0.9997（嵌套式降维）；两者 L2 范数约 1.0（0.9995 / 1.0015），可直接用余弦距离 |
| 确定性 | 同一文本两次请求逐元素完全一致（max_abs_diff=0.0） |
| 批量 | **无批量语义**：`input` 内多个 text 项被**融合成一个向量**（两条短语 → 一个 2048 维向量，tokens 累加）。批量上限因此为 1 条文本/请求，批量必须由调用方逐条并发/串行 |
| 单条耗时 | 短语 n=8：min 0.174s / median 0.223s / max 0.268s；首个请求 0.365s（含 TLS 建连）；约 6000 字符 0.603s；约 24000 字符 2.585s（仍 200，未见显式长度错误，是否截断未验证） |
| 编码 | `encoding_format=base64` → `embedding` 为字符串 |
| 语义区分（余弦，2048 维） | 防水/在意是否防水 0.7307；预算两千元以内/价格不超过2000块 0.7788；喜欢户外跑步/经常去越野跑 0.6491；无关对 0.32–0.39 |

### 错误形态（均为 JSON `{"error":{"code","message","param","type"}}`，message 内带 Request id）

| 场景 | HTTP | code / type |
|---|---|---|
| `input` 为字符串（非数组） | 400 | `InvalidParameter` / BadRequest（“could not parse the JSON body”） |
| 空文本 | 400 | `MissingParameter`，param=`input[0].text` |
| 缺 `input` | 400 | `MissingParameter`，param=`input` |
| 不支持的 `dimensions` | 400 | `InvalidParameter`，param=`dimensions` |
| 不存在的 model | 404 | `InvalidEndpointOrModel.NotFound` / Not Found |
| 无效 key | 401 | `AuthenticationError` / Unauthorized |
| 传输层 | — | 本机到端点 TLS 握手间歇 `SSL: UNEXPECTED_EOF_WHILE_READING`：第一轮第 2 个请求即失败（首次失败保留）；第二轮全部 27 次尝试中 3 次传输错误，有界重试后均成功。HTTP 层未见 429 |

## 建议值与索引决定

- **建议 `EMBEDDING_DIMENSION=1024`，请求时显式传 `dimensions=1024`。** 依据：pgvector `vector` 类型的 HNSW/IVFFlat 索引上限 2000 维，默认 2048 维只能用 `halfvec` 建 ANN 索引；1024 是 2048 的嵌套前缀（余弦 0.9997），存储与检索成本减半，且无需 `halfvec`。若负责人坚持 2048，索引列需改为 `halfvec(2048)`，属于新的配置版本，不能在已有索引上静默切换。
- 负责人需要在私有 `.env` 中自行填写 `EMBEDDING_DIMENSION=1024`（本线不改负责人的 `.env`）。`.env.example` 仅在注释中给出实测值，值保持空白（公开模板全空测试约束）。
- 召回实现把维度同时固定在三处：配置 `EMBEDDING_DIMENSION`、请求参数 `dimensions`、索引表的 profile 记录；任一处与返回向量长度不一致时 fail closed。
- 适配器不得在 `input` 中放多条文本来“批量”，否则得到的是一个融合向量；适配器按条调用并对传输错误做有界重试（不对 4xx 重试）。
- 纯文本可用，因此第二步实现向量 + FTS + pg_trgm 三路，不需要负责人换模型或端点。

## 可复测方式

第二步提交后，真实端点调用只在显式 opt-in 时执行（CI 默认不调用、不读 `.env`）：

```bash
NEXLOOP_REAL_EMBEDDING_ENV_FILE=/path/to/private/.env PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_recall_real_embedding.py
```
