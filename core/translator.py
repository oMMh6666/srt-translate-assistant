"""翻译引擎：合并旧 3_/4_/5_ 三套脚本为单一实现，参数全部外部注入。

相对旧脚本修掉的问题
- `5_Resume_From_Log.py` 里 retry_count 从不递增 -> 失败批次会无限重试死循环
- 503 与 429 一视同仁 -> 现在按错误分类决定「换 Key / 短冷却 / 冷却到次日」
- 漏句重发时复用同一个 Key -> 现在每次重试都重新取 Key
- 上下文模式过多 -> 固定为「完整全部字幕 + 上一批次译文」这一种正确做法
- 写死 Gemini SDK -> 改为依赖 BaseEngine 抽象，可换引擎
- 字幕依赖外部文件 -> 现在原始字幕内嵌在任务库里，只靠 log 即可续跑

重试策略：**批次未完成就一直重试到完成为止**，没有「最大重试次数」。
只有三种情况会让当前批次停下来：暂停、致命错误（重试也没用）、
没有可用 Key（全部禁用或配额耗尽 -> 任务自动停止，进度全部保留）。

Key 失效判定只有一个硬信号：429 返回体里带 quotaValue -> 自动禁用该 Key；
不带 quotaValue 的 429 不会自动禁用（可能只是瞬时抖动 / 分钟级限流）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

from core import context as C
from core import prompts as P
from core import srt as S
from core.engines import GeminiEngine, create_engine
from core.errors import ErrorKind, classify
from core.jobs import (
    PARAM_TOTAL_BATCHES,
    PROMPT_CUSTOMER,
    PROMPT_SYSTEM,
    STATUS_ERROR,
    STATUS_FALLBACK,
    STATUS_NO_KEY,
    STATUS_OK,
)
from core.models import normalize_thinking_level

# 通用 JSON Schema（引擎无关，各引擎自行转换）
# 强结构输出用的 Schema。
# Gemini 走官方 SDK 的 response_schema（types.Schema），OpenAI 兼容接口走 json_schema；
# 两边共用这一份描述，`description` 会原样带给模型，和 system_prompt 里的字段保持一致。
SUBTITLE_ARRAY_SCHEMA: dict = {
    "type": "array",
    "description": "逐条字幕的翻译结果，每个元素对应一条字幕",
    "items": {
        "type": "object",
        "required": ["id", "initial_translation", "reflection", "native_translation"],
        "properties": {
            "id": {"type": "string", "description": "srt 字幕 ID"},
            "initial_translation": {"type": "string", "description": "初步直译结果"},
            "reflection": {"type": "string", "description": "关于语境、流畅度及语法结构的反思与修改意见"},
            "native_translation": {"type": "string", "description": "润色后的地道终译"},
        },
    },
}

def chunk_list(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


PREV_NONE = "无（本批次为第一批）"


def prev_context_items(prev_map: dict | None) -> str:
    """把上一批次的译文组装成上下文文本，用于保持术语/人称/语气连贯。"""
    if not prev_map:
        return PREV_NONE
    items = [
        {"id": str(k), "native_translation": v}
        for k, v in prev_map.items()
    ]
    return json.dumps(items, ensure_ascii=False, indent=2)


class RequestTimeout(Exception):
    """单次请求超过 REQUEST_TIMEOUT 仍未返回 —— 视为抖动，换 Key 重试。"""


def mask_key(key: str) -> str:
    return f"{key[:8]}...{key[-4:]}" if key and len(key) > 12 else (key or "N/A")


# 单条日志的封顶字符数（2MB）。正常一部剧的全文也就几十 KB，
# 这里只是防止异常输入把 db 撑爆，触发时才截断并打标记。
LOG_MAX_CHARS = 2_000_000


def request_payload(
    *,
    model: str,
    system_prompt: str,
    turns: list[dict],
    schema: dict,
    temperature: float,
    top_p: float,
    max_output_tokens: int,
    thinking_level: str | None = None,
) -> dict:
    """构建**全量**请求包（对齐旧脚本 legacy/utils.format_full_request_payload）。

    日志里必须能看到真正发出去的东西：完整 system_instruction + 生成参数 +
    每一轮 contents（全文上下文 / 上一批译文 / 本批任务 / 纠错轮次），不截断。
    """
    return {
        "model": model,
        "system_instruction": system_prompt,
        "generate_config": {
            "temperature": temperature,
            "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": schema,
            "thinking_config": (
                {"thinking_level": thinking_level} if thinking_level else None
            ),
        },
        "contents": [
            {
                "role": t.get("role", "user"),
                "parts": [{"text": t.get("text", "")}],
            }
            for t in turns
        ],
    }


def cap_payload(payload: dict) -> dict:
    """超过封顶才截断，并在包里留标记。"""
    try:
        size = len(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return payload
    if size <= LOG_MAX_CHARS:
        return payload
    out = dict(payload)
    out["contents"] = [
        {
            "role": c.get("role", "user"),
            "parts": [
                {
                    "text": (
                        p.get("text", "")[: LOG_MAX_CHARS // 4]
                        + "\n…[日志封顶，已截断]"
                    )
                }
                for p in c.get("parts", [])
            ],
        }
        for c in payload.get("contents", [])
    ]
    out["__truncated__"] = True
    return out


class Translator:
    def __init__(
        self,
        store,
        pool,
        cfg: dict,
        engine=None,
        on_event: Callable[[dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.pool = pool
        self.cfg = dict(cfg)
        self.engine = engine
        self.on_event = on_event or (lambda e: None)
        self._sleep = sleep
        self._stop = False

        self.engine_name = (self.cfg.get("ENGINE") or "gemini").lower()
        self.model = self.cfg.get("MODEL_NAME", "gemini-3.5-flash-lite")
        self.batch_size = max(int(self.cfg.get("BATCH_SIZE", 20)), 1)
        self.retry_wait = float(self.cfg.get("RETRY_WAIT", 2))
        self.request_timeout = float(self.cfg.get("REQUEST_TIMEOUT", 600) or 600)
        self.target_language = self.cfg.get("TARGET_LANGUAGE", "简体中文")
        self.thinking_level = normalize_thinking_level(
            self.model, self.cfg.get("THINKING_LEVEL") or None
        )

    # ------------------------------------------------------------------ 工具
    def emit(self, **kw) -> None:
        try:
            self.on_event(kw)
        except Exception:
            pass

    # ------------------------------------------------------------- 日志素材
    def _req_payload(self, system_prompt: str, turns: list[dict]) -> dict:
        """本次请求的全量包（与真正发出去的内容一致）。"""
        return cap_payload(
            request_payload(
                model=self.model,
                system_prompt=system_prompt,
                turns=turns,
                schema=SUBTITLE_ARRAY_SCHEMA,
                temperature=float(self.cfg.get("TEMPERATURE", 0.7)),
                top_p=float(self.cfg.get("TOP_P", 0.95)),
                max_output_tokens=int(self.cfg.get("MAX_OUTPUT_TOKENS", 65536)),
                thinking_level=self.thinking_level
                if self.engine_name == "gemini"
                else None,
            )
        )

    def _raw_response(self) -> dict | None:
        """引擎留下的原始响应（candidates / usage_metadata ...），没有就 None。"""
        try:
            return getattr(self.get_engine(), "last_raw_response", None)
        except Exception:
            return None

    def _resp_payload(self, raw: str, extra: dict | None = None) -> dict:
        """完整响应：SDK 原始响应 + 文本 + 解析结果。"""
        payload: dict = {"text": raw}
        raw_resp = self._raw_response()
        if raw_resp is not None:
            payload["raw_response"] = raw_resp
        if extra:
            payload.update(extra)
        return cap_payload(payload)

    def stop(self) -> None:
        self._stop = True

    def get_engine(self):
        if self.engine is None:
            opts = {}
            if self.cfg.get("BASE_URL"):
                opts["base_url"] = self.cfg["BASE_URL"]
            self.engine = create_engine(self.engine_name, **opts)
        return self.engine

    def _call(self, *, api_key, system_prompt, turns, schema):
        return self.get_engine().generate_json(
            api_key=api_key,
            model=self.model,
            system_prompt=system_prompt,
            turns=turns,
            schema=schema,
            temperature=float(self.cfg.get("TEMPERATURE", 0.7)),
            top_p=float(self.cfg.get("TOP_P", 0.95)),
            max_output_tokens=int(self.cfg.get("MAX_OUTPUT_TOKENS", 65536)),
            thinking_level=self.thinking_level
            if self.engine_name == "gemini"
            else None,
        )

    def _call_with_watchdog(
        self,
        *,
        idx: int,
        attempt: int,
        api_key: str,
        system_prompt: str,
        turns: list[dict],
        schema: dict,
    ) -> str:
        """发起请求并在等待期间持续汇报耗时。

        以前 SDK 卡住时界面会「静默」到像是死掉了 —— 这里做了两件事：
        1. 等待期间每 30s 打一条日志，明确告诉用户还在等模型
        2. 超过 REQUEST_TIMEOUT 抛 RequestTimeout，走正常的换 Key 重试逻辑
        """
        started = time.time()
        size = sum(len(t.get("text", "")) for t in turns) + len(system_prompt)
        self.emit(
            type="log",
            level="info",
            batch_index=idx,
            message=(
                f"批次 {idx} 第 {attempt} 次请求 → Key {mask_key(api_key)}，"
                f"输入约 {size} 字符（全文 {len(turns[0]['text'])} 字符 + 本批）"
            ),
        )

        box: dict = {}
        th = threading.Thread(target=self._run_call, args=(box, system_prompt, turns, schema, api_key), daemon=True)
        th.start()

        # join 的粒度决定超时的检出灵敏度：太粗会让「已经超时」迟迟不被发现
        step = min(1.0, max(self.request_timeout / 10, 0.05))
        next_report = 30.0
        while th.is_alive():
            th.join(step)
            if not th.is_alive():
                break
            waited = time.time() - started
            if waited >= next_report:
                self.emit(
                    type="log",
                    level="info",
                    batch_index=idx,
                    message=f"批次 {idx} 仍在等待模型响应…（已等待 {int(waited)}s）",
                )
                next_report += 30.0
            if waited >= self.request_timeout:
                self.emit(
                    type="log",
                    level="warn",
                    batch_index=idx,
                    message=(
                        f"批次 {idx} 请求超过 {int(self.request_timeout)}s 没有响应，"
                        "判定为服务端卡住，换 Key 重试。"
                    ),
                )
                raise RequestTimeout(
                    f"请求超过 {int(self.request_timeout)} 秒未返回（Key {mask_key(api_key)}）"
                )

        if "error" in box:
            raise box["error"]
        elapsed = time.time() - started
        self.emit(
            type="log",
            level="info",
            batch_index=idx,
            message=f"批次 {idx} 收到响应，用时 {elapsed:.1f}s。",
        )
        return box.get("value", "")

    def _run_call(self, box: dict, system_prompt: str, turns: list[dict], schema: dict, api_key: str) -> None:
        try:
            box["value"] = self._call(
                api_key=api_key,
                system_prompt=system_prompt,
                turns=turns,
                schema=schema,
            )
        except BaseException as e:  # noqa: BLE001 - 需要把引擎异常原样送回主线程
            box["error"] = e

    def context_text(self, subtitles: list[dict]) -> str:
        """整体参考 = 完整全部字幕原文。"""
        return C.full_context(subtitles)

    # ------------------------------------------------------------------ 主流程
    def run(self, resume: bool = True) -> dict:
        # 字幕优先来自任务库内嵌内容；旧库首次打开时会自动补嵌一次
        subtitles = self.store.ensure_subtitles()
        if not subtitles:
            self.store.status = STATUS_ERROR
            self.emit(
                type="status",
                status=STATUS_ERROR,
                message="任务里没有可用的字幕："
                f"{self.store.source_meta()['filename'] or self.store.db_path}",
            )
            return {"ok": False, "reason": "missing_input"}

        batches = list(chunk_list(subtitles, self.batch_size))
        self.store.save_params({PARAM_TOTAL_BATCHES: len(batches)})

        system_tpl = self.store.get_prompt(PROMPT_SYSTEM) or ""
        customer = self.store.get_prompt(PROMPT_CUSTOMER) or ""
        system_prompt = P.render_system_prompt(
            system_tpl, customer, self.target_language
        )

        completed = self.store.completed_batches() if resume else {}
        pending = [i for i in range(1, len(batches) + 1) if i not in completed]

        self.store.status = "RUNNING"
        self.emit(
            type="status",
            status="RUNNING",
            message=f"共 {len(batches)} 批，待处理 {len(pending)} 批，严格串行执行。",
        )

        try:
            avail = self.pool.available_count()
        except Exception:
            avail = -1
        self.emit(
            type="log",
            level="info",
            message=(
                f"模型 {self.model} · thinking {self.thinking_level or '未设置'} · "
                f"每批 {self.batch_size} 条 · 可用 Key {avail} 个 · "
                f"单次请求超时 {int(self.request_timeout)}s"
            ),
        )

        # 没有可用 Key 就不开工：与其挂起空转，不如立刻停下来让用户去补 Key
        if not self._has_key():
            return self._stop_for_no_key()

        if not pending:
            self._export(subtitles)
            self.store.status = "DONE"
            self.emit(type="done", status="DONE", message="任务已完成。")
            return {"ok": True, "done": len(batches)}

        # 严格串行：每批次都要带上「上一批次」的译文上下文，
        # 否则术语、人称、语气无法在批次之间保持一致。
        known = dict(completed)
        for idx in pending:
            if self._stop:
                self.store.status = "PAUSED"
                self.emit(type="status", status="PAUSED", message="已暂停。")
                return {"ok": True, "paused": True}

            prev_items = prev_context_items(known.get(idx - 1))
            outcome = self._run_batch(
                idx, batches[idx - 1], subtitles, system_prompt, prev_items
            )
            if outcome == "WAIT":
                # 走到这里只可能是被用户暂停（没 Key 的情况单独返回 NO_KEY）
                self.store.status = "PAUSED"
                self.emit(type="status", status="PAUSED", message="已暂停，进度已保存。")
                return {"ok": True, "paused": True}

            if outcome == "NO_KEY":
                return self._stop_for_no_key()
            if outcome == "FATAL":
                self.store.status = STATUS_ERROR
                self.emit(
                    type="status", status=STATUS_ERROR, message="遇到致命错误，已停止。"
                )
                return {"ok": False, "reason": "fatal"}

            known[idx] = self.store.completed_batches().get(idx, {})

        self._export(subtitles)
        prog = self.store.progress(len(batches))
        status = "DONE" if prog["pending"] == 0 and prog["error"] == 0 else "PARTIAL"
        self.store.status = status
        self.emit(
            type="done", status=status, message=f"完成 {prog['ok']}/{prog['total']} 批。"
        )
        return {"ok": True, "progress": prog}

    # ------------------------------------------------------------------ 单批次
    def _run_batch(
        self,
        idx: int,
        batch: list[dict],
        subtitles: list[dict],
        system_prompt: str,
        prev_items: str = PREV_NONE,
    ) -> str:
        expected = {str(s["id"]) for s in batch}
        kv = {str(s["id"]): s["text"] for s in batch}

        ctx_text = self.context_text(subtitles)

        turns: list[dict] = [
            {
                "role": "user",
                "text": (
                    "<full_context>\n以下是整部视频的完整字幕原文，仅供你理解整体视频背景、上下文语境：\n"
                    f"{ctx_text}\n</full_context>"
                ),
            },
            {
                "role": "user",
                "text": (
                    "<previous_translation_context>\n以下是上一批次的翻译结果，"
                    "仅供参考（用于保持术语统一、人称一致及连贯语气）：\n"
                    f"{prev_items}\n</previous_translation_context>"
                ),
            },
            {
                "role": "user",
                "text": (
                    "<current_task>\n请严格对以下指定范围的字幕条目进行**直译+反思+润色终译**。"
                    f"必须且仅返回本次任务 {len(batch)} 条 ID 的 JSON 结果：\n"
                    f"{json.dumps(kv, ensure_ascii=False, indent=2)}\n</current_task>"
                ),
            },
        ]

        attempt = 0
        while True:
            if self._stop:
                return "WAIT"

            key = self._acquire_key()
            if key is None:
                return "WAIT" if self._stop else "NO_KEY"

            try:
                raw = self._call_with_watchdog(
                    idx=idx,
                    attempt=attempt + 1,
                    api_key=key.api_key,
                    system_prompt=system_prompt,
                    turns=turns,
                    schema=SUBTITLE_ARRAY_SCHEMA,
                )
                try:
                    parsed = json.loads(raw or "[]")
                except json.JSONDecodeError:
                    parsed = []

                returned = {
                    str(i.get("id")) for i in parsed if isinstance(i, dict) and "id" in i
                }
                missing = expected - returned

                if missing:
                    missing_sorted = sorted(
                        missing, key=lambda x: int(x) if x.isdigit() else x
                    )
                    self.emit(
                        type="log",
                        level="warn",
                        batch_index=idx,
                        message=f"批次 {idx} 漏句 {missing_sorted}，重发中（第 {attempt + 1} 次）。",
                    )
                    self.store.add_log(
                        idx, "ERROR_MISSING_KEYS", key.id, key.api_key,
                        self._req_payload(system_prompt, turns),
                        self._resp_payload(raw, {"missing_keys": missing_sorted}),
                    )
                    turns.append({"role": "model", "text": raw})
                    turns.append(
                        {
                            "role": "user",
                            "text": (
                                f"错误：你返回的 JSON 缺失了以下 ID 的字幕条目：{missing_sorted}。"
                                "请绝对保证输入的每一个 ID 都有对应的 JSON 节点，严禁擅自合并或省略！"
                            ),
                        }
                    )
                    # 反复漏句时上下文会不断膨胀 -> 只保留最近的纠错轮次，
                    # 否则请求越来越大、越来越慢，看起来像卡死。
                    if len(turns) > 9:
                        turns = turns[:3] + turns[-4:]
                        self.emit(
                            type="log",
                            level="warn",
                            batch_index=idx,
                            message=f"批次 {idx} 的纠错上下文已过长，已截断只保留最近几轮。",
                        )
                    attempt += 1
                    self._sleep(self._backoff(attempt))
                    continue

                translations = {
                    str(i["id"]): i.get("native_translation", "") for i in parsed
                }
                self.store.save_batch_result(
                    idx, STATUS_OK, translations, key_id=key.id, attempts=attempt + 1
                )
                self.store.apply_translations(translations)
                self.store.add_log(
                    idx, "OK", key.id, key.api_key,
                    self._req_payload(system_prompt, turns),
                    self._resp_payload(raw, {"parsed": parsed}),
                )
                self.pool.mark_ok(key.id)
                self.emit(
                    type="batch",
                    batch_index=idx,
                    status=STATUS_OK,
                    message=f"批次 {idx} 完成，共 {len(expected)} 条。",
                    translations=translations,
                )
                return "OK"

            except RequestTimeout:
                # SDK 卡死：原样当作服务端抖动，换 Key 重试（不让卡顿变成死等）
                self.store.add_log(
                    idx, "ERROR_TIMEOUT", key.id, key.api_key,
                    self._req_payload(system_prompt, turns),
                    {"error": "request timeout"},
                )
                self.emit(
                    type="log",
                    level="warn",
                    batch_index=idx,
                    message=f"批次 {idx} [ERROR_TIMEOUT] 请求超时，稍后换 Key 重试。",
                )
                attempt += 1
                self._sleep(min(max(self.retry_wait, 2.0), 60.0))
                continue

            except Exception as e:
                c = classify(e)
                self.store.add_log(
                    idx, c.label, key.id, key.api_key,
                    self._req_payload(system_prompt, turns),
                    {
                        "error_type": type(e).__name__,
                        "error": c.message,
                        "error_detail": str(e)[:4000],
                    },
                )
                # 429 且返回体带 quotaValue -> 该 Key 的额度确实被吃满，直接停用
                disabled = c.disables_key
                if disabled:
                    self.pool.mark_disabled(key.id)
                elif c.kind == ErrorKind.QUOTA_DAILY:
                    self.pool.mark_quota_exhausted(key.id)
                elif c.kind == ErrorKind.RATE_LIMIT:
                    self.pool.mark_rate_limit(key.id, c.retry_after)
                elif c.kind == ErrorKind.TRANSIENT:
                    self.pool.mark_transient(key.id)

                self.emit(
                    type="log",
                    level="error",
                    batch_index=idx,
                    message=f"批次 {idx} [{c.label}] {c.message[:160]}",
                )

                if disabled:
                    self.emit(
                        type="log",
                        level="warn",
                        batch_index=idx,
                        message=(
                            f"Key {mask_key(key.api_key)} 配额已耗尽"
                            f"（429 quotaValue={c.quota_value}），已自动禁用；"
                            "换下一个 Key 继续。"
                        ),
                    )

                if c.kind == ErrorKind.FATAL:
                    self.store.save_batch_result(
                        idx, STATUS_ERROR, {}, key_id=key.id, error=c.message
                    )
                    return "FATAL"

                # 非致命错误：换 Key 继续，直到这一批次真正翻译完
                attempt += 1
                if disabled:
                    # 日配额型 429 的 retry_after 是「到次日零点」，不能真睡那么久
                    self._sleep(min(max(self.retry_wait, 0.0), 5.0))
                else:
                    self._sleep(
                        min(c.retry_after, 60)
                        if c.retry_after
                        else self._backoff(attempt)
                    )

    # ------------------------------------------------------------------ 取 Key
    def _has_key(self) -> bool:
        """当前是否还有可用 Key（未禁用且不在冷却中）。"""
        try:
            return self.pool.available_count() > 0
        except Exception:
            return False

    def _stop_for_no_key(self) -> dict:
        """没有可用 Key -> 任务自动停止（进度已落库，补 Key 后点「继续」即可）。"""
        self.store.status = STATUS_NO_KEY
        self.emit(
            type="status",
            status=STATUS_NO_KEY,
            message=(
                "没有可用 Key（全部已禁用或被配额耗尽自动停用），任务已自动停止。"
                "进度已保存 —— 到「API Key 管理」启用/新增 Key 后点「继续」即可接着跑。"
            ),
        )
        return {"ok": True, "no_key": True}

    def _acquire_key(self):
        """取一个可用 Key；没有就返回 None（调用方据此停止任务）。

        不再挂起等待：空等既看不到进展也占着任务槽，不如停下来让人补 Key。
        """
        if self._stop:
            return None
        return self.pool.acquire()

    def _backoff(self, attempt: int) -> float:
        """失败退避：2s → 4s → 8s … 上限 60s，避免把 Key 打得更惨。"""
        return min(self.retry_wait * (2 ** min(max(attempt - 1, 0), 5)), 60.0)

    # ------------------------------------------------------------------ 导出
    def _export(self, subtitles: list[dict]) -> None:
        stem = self.store.source_stem()
        out_dir = self.store.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        translations = self.store.translations_map()
        self.store.apply_translations(translations)
        srt_out = out_dir / f"{stem}.cn.srt"
        missing = S.write_srt(srt_out, subtitles, translations)

        ass_out = out_dir / f"{stem}.cn.ass"
        orig, trans = S.blocks_from_subtitles(subtitles, translations)
        S.write_ass_from_blocks(orig, trans, ass_out)

        self.store.save_params(
            {"OUTPUT_SRT": str(srt_out), "OUTPUT_ASS": str(ass_out)}
        )
        self.emit(
            type="export",
            message=f"已导出：{srt_out.name} / {ass_out.name}"
            + (f"（{len(missing)} 条沿用原文）" if missing else ""),
            srt=str(srt_out),
            ass=str(ass_out),
        )
