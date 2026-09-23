"""产物导出：字幕 + 译文 -> output/*.cn.srt 与双语 .ass。

字幕已内嵌在任务库里，导出不依赖任何外部 srt 文件。
产物路径写回 api_params（OUTPUT_SRT / OUTPUT_ASS），界面据此判断「下载」能不能点。
"""

from __future__ import annotations

from dataclasses import dataclass

from core import srt as S

PARAM_OUTPUT_SRT = "OUTPUT_SRT"
PARAM_OUTPUT_ASS = "OUTPUT_ASS"


@dataclass(frozen=True)
class ExportResult:
    srt_path: str
    ass_path: str
    missing: list[str]  # 没有译文、沿用原文的字幕 id


def export_outputs(store, subtitles: list[dict]) -> ExportResult:
    """把当前所有译文导出成 SRT 与双语 ASS。"""
    out_dir = store.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    translations = store.translations_map()
    store.apply_translations(translations)

    stem = store.source_stem()
    srt_out = out_dir / f"{stem}.cn.srt"
    missing = S.write_srt(srt_out, subtitles, translations)

    ass_out = out_dir / f"{stem}.cn.ass"
    orig, trans = S.blocks_from_subtitles(subtitles, translations)
    S.write_ass_from_blocks(orig, trans, ass_out)

    store.save_params(
        {PARAM_OUTPUT_SRT: str(srt_out), PARAM_OUTPUT_ASS: str(ass_out)}
    )
    return ExportResult(srt_path=str(srt_out), ass_path=str(ass_out), missing=missing)
