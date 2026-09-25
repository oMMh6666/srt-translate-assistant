# Gemini API 字幕翻译工作台

SRT 字幕「反思式翻译」工作台：Web UI + FastAPI，通过 Gemini 官方 `google-genai` 库调用（非 OpenAI 兼容接口）。服务于白嫖怪们，免费账户的额度KEY已经足够使用了。

## 安装

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

浏览器自动打开 <http://127.0.0.1:8778/>。端口设置在 `core/config.py` 里，修改 `PORT` 值即可。

## 初次使用

先到「API Key 管理」添加你的 Google Gemini API Key。可添加多个，程序自动轮换使用次数最少的那个。

## 核心能力

| 能力 | 说明 |
|---|---|
| 字幕逐条入库 | `source_srt` 表：一条字幕 = 一行记录（序号 / 时间轴 / 原文齐全，只空 translation 一列，翻完自动回填）。保留原始字幕文件名；续跑、展示、导出都只依赖任务库 |
| 断点续传 | 批次进度全部落库，随时点「继续」接着跑；没有可用 Key 时任务自动停止（NO_KEY），重新启用 Key 后继续即可（太平洋时间0点重置RPD） |
| API Key 池 | 最少用量优先使用；**429 返回体带 quotaValue → 该 Key 自动禁用**（已无额度），没有 quotaValue 的 429 与 503 不会自动禁用（503 只做瞬时抖动处理） |
| Key 管理 | 行内编辑、每行勾选框 + 顶部全选，启用 / 禁用 / 删除 / 重置用量只作用于勾选的行。**被禁用的 Key 在列表里带删除线** |
| 上下文策略 | 完整全部字幕（整体参考）+ 上一批次已翻译内容（最近参考） |
| 重试策略 | 无最大重试上限：批次未完成就一直重试（失败退避 2s→60s）；单次请求超过「请求超时」（默认 600s，可配）判定为服务端卡死，换 Key 重试。三种情况会停下：暂停、致命错误、没有可用 Key |
| 无可用 Key 自动停止 | 启动或取 Key 时发现没有可用 Key，任务立刻停止并说明原因；顶栏「开始」按钮同时置灰 |
| 执行可见性 | 每次请求前后都打日志（Key 脱敏、输入字符数、响应耗时），等待响应期间每 30s 汇报一次，不会出现「看起来卡住」；任务线程意外崩溃会写入 `service.log` 并在界面提示 |
| 日志完整度 | `api_logs` 存**全量**请求与响应：1027 条字幕的剧集，单批请求约 41KB、响应约 7KB。界面「翻译记录」页签点开单条即可查看完整内容 |
| 提示词随任务走 | 每个任务存两条提示词：反思翻译主提示词 + 自定义样式 / 术语（嵌入前者的 `${custom_prompt}`）。「提示词管理」页维护全局模板，供新建或修改任务时选用 |
| 官方 SDK 强结构化 | 用 Gemini 官方 SDK 的原生强结构输出（`response_mime_type=application/json` + `response_schema`），字段带 description 与 required，按声明顺序给 property_ordering |
| 漏句校验 | 返回缺 ID 自动带纠错上下文重发，使用官方强schema在使用3.7,3.8模型时基本不会出现这种情况，使用3.5-flash-lite在极端情况下ID会抽风变成不相干的值 |
| 执行方式 | 严格串行：每批次带上上一批译文做上下文（术语 / 人称 / 语气连贯）；全局同时只允许一个任务运行；创建任务后不会自动运行，必须点「开始」 |
| 删除任务 | 任务列表每行右侧的「✕」删除单个任务：任务库整体移到 `log/.trash/<原名>.db`，弹窗会告知具体落点，需要时手动拖回 `log/` 即可恢复；运行中的任务要先暂停 |
| 产物 | `output/*.cn.srt` + 双语 `.ass`（直接由字幕表合成，不依赖外部文件） |
| 默认参数 | 唯一来源是 `core/config.py` 的 `DEFAULT_CONFIG`（模型 / 温度 / 批次大小 / 目标语言 / 超时等），不读外部配置文件；要改默认值直接改这个文件，创建任务后的参数保存在任务里，可单独修改，会覆盖默认参数 |

## thinking level 与模型的对应

| 模型 | 合法等级 | 默认 |
|---|---|---|
| gemini-3.5-flash-lite / 3.5-flash / 3.6-flash | OFF, MINIMAL, LOW, MEDIUM, HIGH | MINIMAL |
| gemini-3.7-flash / 3.8-flash | OFF, LOW, MEDIUM, HIGH | LOW（不支持 MINIMAL） |

界面切换模型时会自动连动到该模型的默认等级。
**推荐使用 Gemini 3.7 / 3.8，效果明显更优**，但免费账号配额有限（20 RPD）。
**3.5-flash-lite 模型**免费账号配额多（500 RPD），但效果略逊一筹，提示词的遵循效果差一点，但普通够用。
程序会自动轮换使用次数最少的 Key。

## 目录结构

```
core/    srt errors keys jobs context prompts translator engine models config
         db events runner payload watchdog exporter
app/     FastAPI 服务：main.py（装配）+ deps.py（共享资源）+ routers/{jobs,keys,prompts}.py
web/     前端（原生 ES modules，暗色；任务 / 提示词管理 / API Key 管理 三大主视图）
         index.html · style.css · js/{main,api,state,events,log,util,splitter}
                        js/{view_tasks,view_keys,view_prompts}.js
prompts/ 全局模板（reflect.md custom_prompt*.md，仅模板；每个任务的提示词保存在该任务库里）
input/   浏览器上传的字幕暂存（创建任务即逐条入库，之后可删）
log/     任务库（每个任务一个 db），删掉的任务进 log/.trash/
```

分层约定（越往下越底层）：

- `core` —— 业务规则，不认识 HTTP。分三类：`jobs/keys/srt/prompts/context` 是数据访问与素材，`translator/engine/watchdog/payload/exporter` 是翻译链路，`db/events/runner/errors/models/config` 是基础设施
- `app` —— 只做「解析参数 → 调 core → 组装响应」，业务规则一律不下沉到这层
- `web/js` —— 按职责拆：`api.js` 只管发请求、`state.js` 只管状态、`view_*.js` 各管一个视图、`events.js` 只管 SSE 解帧

## 界面速览

- 左栏与内容区之间的**分隔条可拖动**调宽（双击恢复 280px，宽度记在浏览器 localStorage）
- 底部「运行日志」区高度同样可拖动（双击恢复 200px）
- 任务列表按状态着底色：未开始=灰 / 进行中=蓝 / 暂停中=琥珀 / 已完成=绿 / 部分完成=浅蓝 / 无可用 Key・出错=红
- 任务详情四个页签：字幕对照、运行参数、提示词、翻译记录（上半是调用清单，点一行下半显示该条完整请求 / 响应，只读可复制）
- **顶部进度条**两条路都会走：SSE 的 `batch` 事件一到就更新（一批一跳，主路径）；没有 SSE 时由 5s 定时器拿任务列表的进度兜底（慢一拍但不会卡住）
- **实时跟随**，不用手动刷新页面：统一定时器每 5s 走一轮 —— 任务列表（进度 / 状态 / 下载按钮）、字幕对照、翻译记录。这些更新**不依赖 SSE**，服务重启过、任务在别的会话里启动也一样跟得上
- 「下载 SRT / 下载 ASS」只在任务跑完（`DONE` / `PARTIAL`）且产物文件确实存在时可用，否则置灰并在鼠标悬停时说明原因（还剩几批 / 产物丢失）
