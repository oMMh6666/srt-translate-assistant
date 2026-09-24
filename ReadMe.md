# Gemini 字幕翻译工作台

SRT 字幕「反思式翻译」工作台：Web UI + FastAPI + Gemini官方google-genai库调用(非OpenAI通用库)。

## 依赖安装（换机器时）

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动

```
双击 start.bat
# 或
.venv\Scripts\python.exe run.py
```

浏览器自动打开 http://127.0.0.1:8777/


## 初次使用

请先添加你的 google gemini api key，可添加多个，程序会自动轮换使用。

## 核心能力

| 能力 | 说明 |
|---|---|
| 字幕逐条入库 | `source_srt` 表：**一条字幕 = 一行记录**（id/序号/时间轴/原文齐全，只空 translation 一列，翻译完自动回填）。**保留原始字幕文件名（`INPUT_NAME`）**；续跑、展示、导出全部只依赖 log |
| 断点续传 | 批次进度全部落库，随时点「继续」接着跑；没有可用 Key 时任务自动停止（NO_KEY），补 Key 后继续即可 |
| API Key 池 | 最少用量优先使用；**429 返回体带 quotaValue → 该 Key 自动禁用**（已无额度），没有 quotaValue 的 429 与 503 不会自动禁用（503 只做瞬时抖动处理） |
| Key 管理 | 行内编辑（保存 / 取消）、「增加API KEY」可取消、每行勾选框 + 顶部全选，启用 / 禁用 / 删除 / 重置用量只作用于勾选的行（「重置冷却」已移除） |
| 上下文策略 | **完整全部字幕（整体参考）+ 上一批次已翻译内容（最近参考）** |
| 重试策略 | **无最大重试上限**：批次未完成就一直重试到完成为止（失败退避 2s→60s）；单次请求超过「请求超时」（默认 600s，可配）判定为服务端卡死，换 Key 重试；三种情况会停下：暂停、致命错误、**没有可用 Key**，进度一律落库 |
| 无可用 API Key 自动停止 | 启动时或批次取 Key 时发现没有可用 Key，任务立刻停止（状态 `NO_KEY`）并说明原因；顶栏「开始」按钮在无可用 Key 时置灰，启动接口也会直接拒绝 |
| 执行可见性 | 每次请求前后都打日志（Key 脱敏、输入字符数、响应耗时），等待响应期间每 30s 汇报一次，不会再出现「看起来卡住」；任务线程若意外崩溃会写入 `service.log` 并在界面提示 |
| 日志完整度 | `api_logs` 存**全量**请求与响应（跟旧脚本 legacy/utils 的 `format_full_request_payload` + `response.model_dump()` 一个量级）：1027 条字幕的剧集，单批请求约 41KB（含全文 33KB + 提示词 5.6KB），响应约 7KB；`.tests/logpeek.py` 可列出/导出某批次的完整内容 |
| 提示词随任务走 | `prompt_files` 表存两条记录：**system_prompt**（反思翻译主提示词）与 **customer_prompt**（自定义样式/术语，嵌入前者的 ${custom_prompt}）；是任务的参数值，只进库不落 md 文件；「提示词管理」页维护全局模板供新建/修改任务时选用 |
| Gemini官方库调用 | `core/engine.py`：**只保留 Gemini 官方 SDK `google-genai`**，走原生强结构：`response_mime_type="application/json"` + `response_schema=genai.types.Schema`（字段带 description、required，并按声明顺序给 `property_ordering`）。模块是无状态函数（`generate_json` / `to_gemini_schema` / `dump_response`），Translator 只认「给入参、拿 Generation」这一件事，不感知 SDK |
| 漏句校验 | 返回缺 ID 自动带纠错上下文重发 |
| 执行方式 | **严格串行**：每批次带上上一批译文做上下文（术语/人称/语气连贯）；全局同时只允许一个任务运行；创建任务后不会自动运行，必须点「开始」 |
| 删除任务 | 左侧任务列表每行右侧的「✕」= 删除**单个**任务（无需多选）：任务库整个移到 `log/.trash/<原名>.db`，不进系统回收站，弹窗会告知具体落点，需要时手动拖回 `log/` 即可恢复；运行中的任务要先暂停 |
| 产物 | output/*.cn.srt + 双语.ass（直接由 source_srt 表合成，不依赖外部文件） |
| 默认参数 | 唯一来源是 `core/config.py` 的 `DEFAULT_CONFIG`（引擎/模型/温度/批次大小/目标语言/超时等），**不读任何外部配置文件**；要改默认值直接改这个文件 |

## thinking level 与模型的对应

| 模型 | 合法等级 | 默认 |
|---|---|---|
| gemini-3.5-flash-lite / 3.5-flash / 3.6-flash | OFF, MINIMAL, LOW, MEDIUM, HIGH | MINIMAL |
| gemini-3.7-flash / 3.8-flash | OFF, LOW, MEDIUM, HIGH | LOW（不支持 MINIMAL） |

UI 模型下拉切换时会自动连动到该模型的默认等级，见 `core/models.py`。
推荐使用 Gemini 3.7/3.8，效果明显更优，但免费账号配额有限，一天只有20次。
程序自动轮换使用次数最少的api key。

## 目录结构

```
core/    srt errors keys jobs context prompts translator engine models config
         db events runner payload watchdog exporter
app/     FastAPI 服务：main.py（装配）+ deps.py（共享资源）+ routers/{jobs,keys,prompts}.py
web/     前端（原生 ES modules，暗色；任务 / 提示词管理 / API Key 管理 三大主视图）
         index.html · style.css · js/{main,api,state,events,log,util,splitter}
                        js/{view_tasks,view_keys,view_prompts}.js
prompts/ 全局模板（reflect.md custom_prompt*.md，仅模板，每一项的任务提示词会保存在对应的任务里）
input/   浏览器上传的字幕暂存（创建任务即逐条入库，之后可删）
log/     任务库（每个任务一个 db），删掉的任务进 log/.trash/
```

**目录命名约定**：不带点 = 跑项目用得着；`.` 前缀 = 与运行无关，本地自用、随时可删。

```
.tests/  冒烟脚本（**. 前缀但照样进版本库** —— 以前放 Tmp/ 被当草稿清掉过一次）
         四个套件：smoke_refactor / smoke_pipeline / smoke_ui / smoke_progress
.docs/   本地设计文档（重构方案、分层约定，不进仓库）
.tmp/    随手写的草稿 + 测试残留（gitignore，随时可删）
```

分层约定（越往下越底层）：

- `core` —— 业务规则，不认识 HTTP。分三类：`jobs/keys/srt/prompts/context` 是数据访问与素材，`translator/engine/watchdog/payload/exporter` 是翻译链路，`db/events/runner/errors/models/config` 是基础设施
- `app` —— 只做「解析参数 → 调 core → 组装响应」，业务规则一律不下沉到这层
- `web/js` —— 按职责拆：`api.js` 只管发请求、`state.js` 只管状态、`view_*.js` 各管一个视图、`events.js` 只管 SSE 解帧

## log 库的表结构

| 表 | 作用 |
|---|---|
| `api_params` | 任务参数键值对。含模型/温度/批次等，以及 `INPUT_NAME`（字幕文件名）、`SOURCE_SRT_LINES`（字幕条数）。|
| `source_srt` | **逐条字幕表**：`id`（字幕序号）、`pos`（顺序）、`time`（时间轴）、`text`（原文）、`translation`（待翻译列，翻完回填） |
| `batch_results` | 批次完成状态（权威表），含每批译文 JSON |
| `prompt_files` | 两条记录：`system_prompt`、`customer_prompt`，内容随任务走 |
| `api_logs` | 每次 API 调用的**全量**请求 / 响应日志（key 脱敏）。request = `model` + `system_instruction`（渲染后的完整提示词）+ `generate_config`（温度/top_p/最大输出/schema/thinking）+ `contents`（每一轮：全文上下文、上一批译文、本批任务、纠错轮次），与真正发出去的内容一致、不截断；response = `raw_response`（SDK 响应整体 dump，含 candidates / usage_metadata）+ `text`（模型原文）+ `parsed`（解析结果）。单条超过 2MB 才封顶并打 `__truncated__` 标记。界面「翻译记录」页签可浏览：清单只带长度，点开单条才取正文 |

## 界面速览

- 左栏与内容区之间的**分隔条可拖动**调宽（双击恢复 280px，宽度记忆在浏览器 localStorage）
- 底部「运行日志」区高度同样**可拖动**（双击恢复 200px，记忆在 localStorage）
- 任务列表按状态着底色：未开始=灰 / 进行中=蓝 / 暂停中=琥珀 / 已完成=绿 / 部分完成=浅蓝 / 无可用Key・出错=红
- 任务详情四个页签：字幕对照、运行参数、提示词、翻译记录（上半是 `api_logs` 清单，点一行下半显示该条完整请求/响应，只读可复制）
- **顶部进度条**两条路都会走：SSE 的 `batch` 事件一到就更新（**一批一跳，主路径**）；没有 SSE 时由 5s 定时器拿任务列表的进度兜底（慢一拍但不会卡住 —— 进度条按列表走，不看「状态是否变化」，否则运行中一直是 RUNNING 就永远推不动它）
- **实时跟随**（不用手动刷新页面）：统一定时器每 5s 走一轮 —— 任务列表（进度 / 状态 / 下载按钮）、字幕对照（SSE 缺失时发现完成批次数变了就整页重拉 + 跳到最新完成批次）、翻译记录（页签可见就拉清单，「跟随最新」自动跳到最新一条并加载正文）。这些更新**不依赖 SSE 事件流**，服务重启过、任务在别的会话里启动也一样跟得上（底层另有 SSE 加速，事件由后端**广播**给每个连接，页面重连不会互相抢事件）
- 「下载 SRT / 下载 ASS」只在任务跑完（`DONE` / `PARTIAL`）且产物文件确实存在时可用，否则置灰并在鼠标悬停时说明原因（还剩几批 / 产物丢失）
- 页签选中态统一：顶栏与子页签同为「蓝边 + 蓝字 + 加粗」，子页签选中另有淡蓝底和底部蓝线

## 事件流（SSE）协议

`GET /api/jobs/{id}/events`，服务端是**广播式订阅**（`core/events.py` 的 `EventBroker`）：
每个连接领一条自己的队列，`emit()` 发给全部订阅者，多个标签页 / 反复重连互不抢事件。

| 帧 | 事件名 | 载荷 | 触发时机 |
|---|---|---|---|
| `log` | 日志 | `{level, message}` | 每一步操作（请求前 / 收到响应 / 报错 / 换 Key …） |
| `batch` | 批次完成 | `{batch_index, translations, status, progress, message}` | 一批翻完，前端直接把译文填进对照表 |
| `status` | 状态变更 | `{status, progress, message}` | RUNNING / PAUSED / DONE / NO_KEY / ERROR |
| `export` | 产物导出 | `{message, ...}` | 写出 srt / ass |
| `end` | 流结束 | `{reason}` | `finished` / `stopped` / `crashed` / `closed`（任务没在跑时立即返回 `closed`） |

其余约定：

- 每个事件带**单调递增的 `id:`（seq）**，服务端留最近 200 条环形缓冲（`HISTORY_SIZE`）
- 断线重连走浏览器原生 `Last-Event-ID`（也支持 `?last=`）：服务端从缓冲补发那段事件，页面**不必整页重拉**
- 缓冲已被挤出（`last_seq < oldest_seq`）时先补一帧提示，告知界面可能落后于实际进度
- 空闲 15s 发一帧 `: heartbeat` 注释帧保活；首帧 `retry: 3000` 指定重连间隔
- 界面**不依赖** SSE 也能跟得上（另有 5s 轮询兜底），SSE 只是加速器；服务重启过、任务在别的会话里启动也一样更新

## 冒烟验收

不引入 pytest。改完跑一遍 `.tests/` 下的脚本（**脚本本身在版本库里**，
`.tmp/` 只放随手写的草稿）：

```
.venv\Scripts\python.exe .tests/run_all.py        # 四个套件一起跑
```

| 脚本 | 干什么 | 项数 |
|---|---|---|
| `.tests/smoke_refactor.py` | 分层是否守住 / 废弃设计是否清干净 / 核心契约是否成立 | 58 |
| `.tests/smoke_pipeline.py` | 建任务→跑批→暂停→续跑→导出→SSE 断线重连 | 51 |
| `.tests/smoke_ui.py` | 真起服务 + 无头 Chrome 点一遍界面 | 38 |
| `.tests/smoke_progress.py` | 进度条专项：SSE 正常 / 掐断 SSE 也要走到 100% | 15 |

三条铁律（踩过坑才定的）：

1. **一律临时目录** —— `Key 库 / log / input` 全部指到 tempdir，绝不碰 `api_keys/` 的真实 Key 和 `log/` 的真实任务
2. **一律假引擎** —— `.tests/_harness.py` 的 `FakeEngine` 换掉 `core.engine.generate_json`，一个字节都不发给真实 API
3. **自带起停服务** —— `.tests/fake_server.py` 是「假引擎 + 真服务」的子进程，跑完自己收干净

`smoke_progress.py` 专治「跑完了界面却不动」这类只有真跑一遍才看得出来的问题 ——
进度条那个「条件互斥导致兜底从不执行」的 bug 就是这么抓出来的。

