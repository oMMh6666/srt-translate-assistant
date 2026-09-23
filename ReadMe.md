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
| 日志完整度 | `api_logs` 存**全量**请求与响应（跟旧脚本 legacy/utils 的 `format_full_request_payload` + `response.model_dump()` 一个量级）：1027 条字幕的剧集，单批请求约 41KB（含全文 33KB + 提示词 5.6KB），响应约 7KB；`Tmp/logpeek.py` 可列出/导出某批次的完整内容 |
| 提示词随任务走 | `prompt_files` 表存两条记录：**system_prompt**（反思翻译主提示词）与 **customer_prompt**（自定义样式/术语，嵌入前者的 ${custom_prompt}）；是任务的参数值，只进库不落 md 文件；「提示词管理」页维护全局模板供新建/修改任务时选用 |
| Gemini官方库调用 | `core/engines.py`：Gemini / OpenAI 兼容接口(暂不可用)，Translator 不感知具体 SDK。**Gemini 走官方 SDK `google-genai` 原生强结构**：`response_mime_type="application/json"` + `response_schema=genai.types.Schema`（字段带 description、required，并按声明顺序给 `property_ordering`），不使用 OpenAI 兼容接口；OpenAI 引擎只为 openai/deepseek/通义等兼容端点准备 |
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
core/    srt errors keys jobs context prompts translator engines models config
app/     FastAPI 服务
web/     前端（原生 JS，暗色；任务 / 提示词管理 / API Key 管理 三大主视图）
prompts/ 全局模板（reflect.md custom_prompt*.md，仅模板，每一项的任务提示词会保存在对应的任务里）
input/   浏览器上传的字幕暂存（创建任务即逐条入库，之后可删）
log/     任务库（每个任务一个 db）
```

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
- **实时跟随**（不用手动刷新页面）：停在哪个页签就刷哪个 —— 翻译记录每 2.5s 拉清单，「跟随最新」自动跳到最新一条并加载正文；字幕对照每完成一批就自动滚到那批第一条并短暂高亮（开关「跟随最新批次」在页签行右侧，默认开）。这些更新**不依赖 SSE 事件流**，服务重启过、任务在别的会话里启动也一样跟得上（底层另有 SSE 加速，事件由后端**广播**给每个连接，页面重连不会互相抢事件）
- 「下载 SRT / 下载 ASS」只在任务跑完（`DONE` / `PARTIAL`）且产物文件确实存在时可用，否则置灰并在鼠标悬停时说明原因（还剩几批 / 产物丢失）
- 页签选中态统一：顶栏与子页签同为「蓝边 + 蓝字 + 加粗」，子页签选中另有淡蓝底和底部蓝线

