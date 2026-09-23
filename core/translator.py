"""翻译流程编排：分批 -> 严格串行 -> 单批次重试到完成 -> 落库 -> 导出。

相对旧脚本修掉的问题
- `5_Resume_From_Log.py` 里 retry_count 从不递增 -> 失败批次会无限重试死循环
- 503 与 429 一视同仁 -> 现在按错误分类决定「换 Key / 短冷却 / 冷却到次日」
- 漏句重发时复用同一个 Key -> 现在每次重试都重新取 Key
- 上下文模式过多 -> 固定为「完整全部字幕 + 上一批次译文」这一种正确做法
- 字幕依赖外部文件 -> 原始字幕内嵌在任务库里，只靠 log 即可续跑

重试策略：**批次未完成就一直重试到完成为止**，没有「最大重试次数」。
只有三种情况会让当前批次停下来：暂停、致命错误（重试也没用）、
没有可用 Key（全部禁用或配额耗尽 -> 任务自动停止，进度全部保留）。

Key 失效判定只有一个硬信号：429 返回体里带 quotaValue -> 自动禁用该 Key；
不带 quotaValue 的 429 不会自动禁用（可能只是瞬时抖动 / 分钟级限流）。

本类只做编排：怎么发请求在 engine.py，日志封包在 payload.py，
上下文在 context.py，产物在 exporter.py，Key 调度在 keys.py。
外部依赖全部注入（store / pool / cfg / emit / sleep），方便单测。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from core import context as C
from core import engine as E
from core import payload as PL
from core import prompts as P
from core.errors import ErrorKind, classify
from core.exporter import export_outputs
from core.jobs import (
    PARAM_TOTAL_BATCHES,
    PROMPT_CUSTOMER,
    PROMPT_SYSTEM,
    STATUS_ERROR,
    STATUS_NO_KEY,
    STATUS_OK,
)
from core.keys import mask_key
from core.models import normalize_thinking_level
from core.watchdog import RequestTimeout, run_with_timeout

# 强结构输出用的 Schema（引擎无关描述，由 engine 转成 genai.types.Schema）
# `description` 会原样带给模型，与 reflect.md 里的字段用词保持一致。
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

# 任务状态（写进 api_params 的 JOB_STATUS，也是 status 事件的 status 字段）
STATUS_RUNNING = "RUNNING"
STATUS_PAUSED = "PAUSED"
STATUS_DONE = "DONE"
STATUS_PARTIAL = "PARTIAL"


class BatchOutcome(str, Enum):
    """单个批次的结束方式。"""

    OK = "OK"
    PAUSED = "PAUSED"  # 被用户暂停
    NO_KEY = "NO_KEY"  # 没有可用 Key
    FATAL = "FATAL"  # 致命错误，重试也没用


@dataclass(frozen=True)
class RunResult:
    outcome: str  # DONE / PARTIAL / PAUSED / NO_KEY / ERROR
    progress: dict


def chunk_list(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


class Translator:
    def __init__(
        self,
        store,
        pool,
        cfg: dict,
        emit: Callable[..., None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.pool = pool
        self.cfg = dict(cfg)
        self.emit = emit or (lambda *a, **kw: None)
        self._sleep = sleep
        self._stop = False

        self.model = self.cfg.get("MODEL_NAME", "gemini-3.5-flash-lite")
        self.batch_size = max(int(self.cfg.get("BATCH_SIZE", 20)), 1)
        self.retry_wait = float(self.cfg.get("RETRY_WAIT", 2))
        self.request_timeout = float(self.cfg.get("REQUEST_TIMEOUT", 600) or 600)
        self.target_language = self.cfg.get("TARGET_LANGUAGE", "简体中文")
        self.thinking_level = normalize_thinking_level(
            self.model, self.cfg.get("THINKING_LEVEL") or None
        )

    # ------------------------------------------------------------------ 控制
    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------ 主流程
    def run(self, resume: bool = True) -> RunResult:
        subtitles = self.store.subtitles()
        if not subtitles:
            name = self.store.source_meta()["filename"] or self.store.db_path
            return self._finish(
                STATUS_ERROR, f"任务里没有可用的字幕：{name}"
            )

        batches = list(chunk_list(subtitles, self.batch_size))
        self.store.set_param(PARAM_TOTAL_BATCHES, len(batches))

        system_prompt = P.render_system_prompt(
            self.store.get_prompt(PROMPT_SYSTEM) or "",
            self.store.get_prompt(PROMPT_CUSTOMER) or "",
            self.target_language,
        )

        completed = self.store.completed_batches() if resume else {}
        pending = [i for i in range(1, len(batches) + 1) if i not in completed]

        self._status(
            STATUS_RUNNING,
            f"共 {len(batches)} 批，待处理 {len(pending)} 批，严格串行执行。",
        )
        self._log(
            "info",
            f"模型 {self.model} · thinking {self.thinking_level} · "
            f"每批 {self.batch_size} 条 · 可用 Key {self._available_keys()} 个 · "
            f"单次请求超时 {int(self.request_timeout)}s",
        )

        # 没有可用 Key 就不开工：与其挂起空转，不如立刻停下来让用户去补 Key
        if not self._has_key():
            return self._finish(
                STATUS_NO_KEY,
                "没有可用 Key（全部已禁用或被配额耗尽自动停用），任务已自动停止。"
                "进度已保存 —— 到「API Key 管理」启用/新增 Key 后点「继续」即可接着跑。",
            )

        if not pending:
            self._export(subtitles)
            return self._finish(STATUS_DONE, "任务已完成。")

        # 严格串行：每批次都要带上「上一批次」的译文上下文，
        # 否则术语、人称、语气无法在批次之间保持一致。
        known = dict(completed)
        for idx in pending:
            if self._stop:
                return self._finish(STATUS_PAUSED, "已暂停，进度已保存。")

            outcome = self._run_batch(
                idx, batches[idx - 1], subtitles, system_prompt, known.get(idx - 1)
            )
            if outcome is BatchOutcome.PAUSED:
                return self._finish(STATUS_PAUSED, "已暂停，进度已保存。")
            if outcome is BatchOutcome.NO_KEY:
                return self._finish(
                    STATUS_NO_KEY,
                    "没有可用 Key（全部已禁用或被配额耗尽自动停用），任务已自动停止。"
                    "进度已保存 —— 到「API Key 管理」启用/新增 Key 后点「继续」即可接着跑。",
                )
            if outcome is BatchOutcome.FATAL:
                return self._finish(STATUS_ERROR, "遇到致命错误，已停止。")

            known[idx] = self.store.completed_batches().get(idx, {})

        self._export(subtitles)
        prog = self.store.progress(len(batches))
        outcome = (
            STATUS_DONE if prog["pending"] == 0 and prog["error"] == 0
            else STATUS_PARTIAL
        )
        return self._finish(outcome, f"完成 {prog['ok']}/{prog['total']} 批。")

    # ------------------------------------------------------------------ 单批次
    def _run_batch(
        self,
        idx: int,
        batch: list[dict],
        subtitles: list[dict],
        system_prompt: str,
        prev_map: dict | None,
    ) -> BatchOutcome:
        expected = {str(s["id"]) for s in batch}
        kv = {str(s["id"]): s["text"] for s in batch}

        turns = C.build_turns(
            C.full_context(subtitles), C.prev_context(prev_map), kv
        )

        attempt = 0
        while True:
            if self._stop:
                return BatchOutcome.PAUSED

            key = self._acquire_key()
            if key is None:
                return (
                    BatchOutcome.PAUSED if self._stop else BatchOutcome.NO_KEY
                )

            try:
                gen = self._request(idx, attempt + 1, key, system_prompt, turns)
                try:
                    parsed = json.loads(gen.text or "[]")
                except json.JSONDecodeError:
                    parsed = []

                returned = {
                    str(i.get("id"))
                    for i in parsed
                    if isinstance(i, dict) and "id" in i
                }
                missing = expected - returned

                if missing:
                    missing_sorted = sorted(
                        missing, key=lambda x: int(x) if x.isdigit() else x
                    )
                    self._log(
                        "warn",
                        f"批次 {idx} 漏句 {missing_sorted}，重发中（第 {attempt + 1} 次）。",
                        idx,
                    )
                    self.store.add_log(
                        idx, "ERROR_MISSING_KEYS", key.id, key.api_key,
                        self._req_payload(system_prompt, turns),
                        PL.cap_payload(
                            PL.response_payload(
                                gen.text, gen.raw, {"missing_keys": missing_sorted}
                            )
                        ),
                    )
                    turns += C.missing_feedback(missing_sorted, gen.text)
                    if len(turns) > C.MAX_TURNS:
                        turns = C.trim_turns(turns)
                        self._log(
                            "warn",
                            f"批次 {idx} 的纠错上下文已过长，已截断只保留最近几轮。",
                            idx,
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
                    PL.cap_payload(
                        PL.response_payload(gen.text, gen.raw, {"parsed": parsed})
                    ),
                )
                self.pool.mark_ok(key.id)
                self.emit(
                    "batch",
                    batch_index=idx,
                    status=STATUS_OK,
                    message=f"批次 {idx} 完成，共 {len(expected)} 条。",
                    translations=translations,
                    progress=self.store.progress(),
                )
                return BatchOutcome.OK

            except RequestTimeout:
                # SDK 卡死：当作服务端抖动，换 Key 重试（不让卡顿变成死等）
                self.store.add_log(
                    idx, "ERROR_TIMEOUT", key.id, key.api_key,
                    self._req_payload(system_prompt, turns),
                    {"error": "request timeout"},
                )
                self._log(
                    "warn",
                    f"批次 {idx} [ERROR_TIMEOUT] 请求超时，稍后换 Key 重试。",
                    idx,
                )
                attempt += 1
                self._sleep(min(max(self.retry_wait, 2.0), 60.0))
                continue

            except Exception as e:
                c = classify(e)
                self.store.add_log(
                    idx, c.label, key.id, key.api_key,
                    self._req_payload(system_prompt, turns),
                    PL.cap_payload(
                        PL.response_payload("", None, PL.error_payload(e, c))
                    ),
                )
                self._apply_key_feedback(key, c)
                self._log(
                    "error", f"批次 {idx} [{c.label}] {c.message[:160]}", idx
                )

                if c.kind == ErrorKind.FATAL:
                    self.store.save_batch_result(
                        idx, STATUS_ERROR, {}, key_id=key.id, error=c.message
                    )
                    return BatchOutcome.FATAL

                # 非致命错误：换 Key 继续，直到这一批次真正翻译完
                attempt += 1
                if c.disables_key:
                    # 日配额型 429 的 retry_after 是「到次日零点」，不能真睡那么久
                    self._sleep(min(max(self.retry_wait, 0.0), 5.0))
                else:
                    self._sleep(
                        min(c.retry_after, 60)
                        if c.retry_after
                        else self._backoff(attempt)
                    )

    # ------------------------------------------------------------------ 请求
    def _request(self, idx: int, attempt: int, key, system_prompt: str, turns: list[dict]) -> E.Generation:
        """发一次请求：请求前打日志、等待期间汇报耗时、收到响应打耗时。"""
        size = sum(len(t.get("text", "")) for t in turns) + len(system_prompt)
        self._log(
            "info",
            f"批次 {idx} 第 {attempt} 次请求 → Key {mask_key(key.api_key)}，"
            f"输入约 {size} 字符（全文 {len(turns[0]['text'])} 字符 + 本批）",
            idx,
        )
        started = time.time()
        gen = run_with_timeout(
            lambda: E.generate_json(
                api_key=key.api_key,
                model=self.model,
                system_prompt=system_prompt,
                turns=turns,
                schema=SUBTITLE_ARRAY_SCHEMA,
                temperature=float(self.cfg.get("TEMPERATURE", 0.7)),
                top_p=float(self.cfg.get("TOP_P", 0.95)),
                max_output_tokens=int(self.cfg.get("MAX_OUTPUT_TOKENS", 65536)),
                thinking_level=self.thinking_level,
            ),
            timeout=self.request_timeout,
            on_wait=lambda waited: self._log(
                "info", f"批次 {idx} 仍在等待模型响应…（已等待 {int(waited)}s）", idx
            ),
        )
        self._log(
            "info", f"批次 {idx} 收到响应，用时 {time.time() - started:.1f}s。", idx
        )
        return gen

    def _req_payload(self, system_prompt: str, turns: list[dict]) -> dict:
        return PL.cap_payload(
            PL.request_payload(
                model=self.model,
                system_prompt=system_prompt,
                turns=turns,
                schema=SUBTITLE_ARRAY_SCHEMA,
                temperature=float(self.cfg.get("TEMPERATURE", 0.7)),
                top_p=float(self.cfg.get("TOP_P", 0.95)),
                max_output_tokens=int(self.cfg.get("MAX_OUTPUT_TOKENS", 65536)),
                thinking_level=self.thinking_level,
            )
        )

    # ------------------------------------------------------------------ Key
    def _available_keys(self) -> int:
        try:
            return self.pool.available_count()
        except Exception:
            return -1

    def _has_key(self) -> bool:
        return self._available_keys() > 0

    def _acquire_key(self):
        """取一个可用 Key；没有就返回 None（调用方据此停止任务）。

        不挂起等待：空等既看不到进展也占着任务槽，不如停下来让人补 Key。
        """
        if self._stop:
            return None
        return self.pool.acquire()

    def _apply_key_feedback(self, key, c) -> None:
        """按错误分类惩罚（或不惩罚）这个 Key。"""
        if c.disables_key:
            self.pool.disable(key.id)
            self._log(
                "warn",
                f"Key {mask_key(key.api_key)} 配额已耗尽"
                f"（429 quotaValue={c.quota_value}），已自动禁用；换下一个 Key 继续。",
            )
        elif c.kind == ErrorKind.QUOTA_DAILY:
            self.pool.mark_quota_exhausted(key.id)
        elif c.kind == ErrorKind.RATE_LIMIT:
            self.pool.mark_rate_limit(key.id, c.retry_after)
        elif c.kind == ErrorKind.TRANSIENT:
            self.pool.mark_transient(key.id)

    def _backoff(self, attempt: int) -> float:
        """失败退避：2s → 4s → 8s … 上限 60s，避免把 Key 打得更惨。"""
        return min(self.retry_wait * (2 ** min(max(attempt - 1, 0), 5)), 60.0)

    # ------------------------------------------------------------------ 事件
    def _log(self, level: str, message: str, batch_index: int | None = None) -> None:
        self.emit("log", level=level, message=message, batch_index=batch_index)

    def _status(self, status: str, message: str) -> None:
        self.emit(
            "status", status=status, message=message, progress=self.store.progress()
        )

    def _finish(self, outcome: str, message: str) -> RunResult:
        """写状态 + 发终态事件 + 返回结果（run() 的唯一出口）。"""
        self.store.status = outcome
        progress = self.store.progress()
        self.emit("status", status=outcome, message=message, progress=progress)
        return RunResult(outcome=outcome, progress=progress)

    def _export(self, subtitles: list[dict]) -> None:
        result = export_outputs(self.store, subtitles)
        srt_name = Path(result.srt_path).name
        self.emit(
            "export",
            message=f"已导出：{srt_name}"
            + (f"（{len(result.missing)} 条沿用原文）" if result.missing else ""),
            srt=result.srt_path,
            ass=result.ass_path,
            missing=len(result.missing),
        )
