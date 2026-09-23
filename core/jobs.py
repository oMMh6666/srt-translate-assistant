"""任务（Job）存储：一个 log 库 = 一个 srt 的一次翻译任务记录。

设计要点
- 一任务一 db，放 log/。
- `source_srt` 是逐条字幕表：字幕文件里的每一条字幕就是表里的一行记录，
  原文/序号/时间轴齐全，只有 translation 一列留空等待填充。
  续跑、展示、导出全部只依赖这张表，外部 srt 不见了也不影响。
- `batch_results` 是「批次是否完成」的权威表，含每批译文 JSON。
- `prompt_files` 存两条记录：system_prompt（反思翻译主提示词）
  与 customer_prompt（自定义样式 / 术语用语）。二者都是任务的参数值，
  随任务走、可在任务里改，不往磁盘上另存 md 副本。

本模块只做「一个任务库的读写」与「log/ 目录下的任务枚举」，
不碰线程、不碰 HTTP —— 那是 runner / app 层的事。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core import db
from core import srt as S

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "log"

STATUS_OK = "OK"
STATUS_ERROR = "ERROR"
STATUS_PENDING = "PENDING"
STATUS_NO_KEY = "NO_KEY"  # 没有可用 Key（全部禁用 / 配额耗尽）-> 任务自动停止

PARAM_STATUS = "JOB_STATUS"
PARAM_TOTAL_BATCHES = "TOTAL_BATCHES"
PARAM_INPUT_NAME = "INPUT_NAME"  # 只记字幕文件名：浏览器选文件拿不到真实路径

# 删除任务时的去处（软删除，不进系统回收站，直接移到 log/.trash）
TRASH_DIR_NAME = ".trash"

# prompt_files 里的两条记录名（任务自带的提示词 = 任务参数值）
PROMPT_SYSTEM = "system_prompt"
PROMPT_CUSTOMER = "customer_prompt"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    param_name TEXT UNIQUE NOT NULL,
    param_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    batch_index INTEGER,
    status TEXT NOT NULL,
    key_id INTEGER,
    api_key TEXT,
    request_payload TEXT,
    response_payload TEXT
);
CREATE TABLE IF NOT EXISTS batch_results (
    batch_index INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    key_id INTEGER,
    attempts INTEGER DEFAULT 0,
    translations TEXT,
    error TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS source_srt (
    id INTEGER PRIMARY KEY,
    pos INTEGER NOT NULL,
    time TEXT NOT NULL,
    text TEXT NOT NULL,
    translation TEXT
);
CREATE TABLE IF NOT EXISTS prompt_files (
    name TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db.init_schema(self.db_path, _SCHEMA)

    # ---------------------------------------------------------------- 目录约定
    @property
    def job_dir(self) -> Path:
        """任务附属目录（产物目录的父级）。扁平库则在其同名子目录。"""
        if self.db_path.parent.name == "log" and self.db_path.stem:
            return self.db_path.parent / self.db_path.stem
        return self.db_path.parent

    @property
    def output_dir(self) -> Path:
        return self.job_dir / "output"

    # ------------------------------------------------------------------ 参数
    def save_params(self, params: dict) -> None:
        with db.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO api_params (param_name, param_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(param_name) DO UPDATE SET
                    param_value=excluded.param_value,
                    updated_at=excluded.updated_at
                """,
                [(k, str(v), _now()) for k, v in params.items()],
            )
            conn.commit()

    def get_params(self) -> dict:
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT param_name, param_value FROM api_params"
            ).fetchall()
        return dict(rows)

    def get_param(self, name: str, default=None):
        return self.get_params().get(name, default)

    def set_param(self, name: str, value) -> None:
        self.save_params({name: value})

    @property
    def status(self) -> str:
        return self.get_param(PARAM_STATUS, STATUS_PENDING)

    @status.setter
    def status(self, value: str) -> None:
        self.set_param(PARAM_STATUS, value)

    # ------------------------------------------------------------------ 日志
    def add_log(
        self,
        batch_index: int,
        status: str,
        key_id: int | None = None,
        api_key: str | None = None,
        request_payload=None,
        response_payload=None,
    ) -> None:
        masked = (
            f"{api_key[:8]}...{api_key[-4:]}"
            if api_key and len(api_key) > 12
            else (api_key or "N/A")
        )
        req = (
            json.dumps(request_payload, ensure_ascii=False)
            if isinstance(request_payload, (dict, list))
            else str(request_payload)
        )
        resp = (
            json.dumps(response_payload, ensure_ascii=False)
            if isinstance(response_payload, (dict, list))
            else str(response_payload)
        )
        with db.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO api_logs
                (timestamp, batch_index, status, key_id, api_key, request_payload, response_payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now(), batch_index, status, key_id, masked, req, resp),
            )
            conn.commit()

    def api_log_entries(self) -> list[dict]:
        """api_logs 清单（不含正文，只给正文长度）。

        单条请求正文经常几万字符，几百条一起回前端会把浏览器拖死，
        所以清单只回元数据，正文在点开某一行时由 api_log_detail 单独取。
        """
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, batch_index, status, key_id, api_key,
                       LENGTH(request_payload), LENGTH(response_payload)
                FROM api_logs
                ORDER BY id DESC
                """
            ).fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "batch_index": r[2],
                "status": r[3],
                "key_id": r[4],
                "api_key": r[5],
                "request_len": r[6] or 0,
                "response_len": r[7] or 0,
            }
            for r in rows
        ]

    def api_log_detail(self, log_id: int) -> dict | None:
        """取一条 api_log 的完整正文（界面「翻译记录」下半部分只读展示用）。"""
        with db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, timestamp, batch_index, status, key_id, api_key,
                       request_payload, response_payload
                FROM api_logs WHERE id = ?
                """,
                (int(log_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "timestamp": row[1],
            "batch_index": row[2],
            "status": row[3],
            "key_id": row[4],
            "api_key": row[5],
            "request_payload": row[6] or "",
            "response_payload": row[7] or "",
        }

    # ------------------------------------------------------------ 批次结果
    def save_batch_result(
        self,
        batch_index: int,
        status: str,
        translations: dict[str, str] | None = None,
        key_id: int | None = None,
        attempts: int = 0,
        error: str | None = None,
    ) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO batch_results
                (batch_index, status, key_id, attempts, translations, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_index) DO UPDATE SET
                    status=excluded.status,
                    key_id=excluded.key_id,
                    attempts=excluded.attempts,
                    translations=excluded.translations,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    batch_index,
                    status,
                    key_id,
                    attempts,
                    json.dumps(translations or {}, ensure_ascii=False),
                    error,
                    _now(),
                ),
            )
            conn.commit()

    def batch_statuses(self) -> dict[int, str]:
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT batch_index, status FROM batch_results"
            ).fetchall()
        return dict(rows)

    def completed_batches(self) -> dict[int, dict[str, str]]:
        """已完成批次 -> {batch_index: {sub_id: translation}}"""
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT batch_index, translations FROM batch_results WHERE status=?",
                (STATUS_OK,),
            ).fetchall()
        out: dict[int, dict[str, str]] = {}
        for idx, raw in rows:
            try:
                out[idx] = json.loads(raw)
            except Exception:
                out[idx] = {}
        return out

    def translations_map(self) -> dict[str, str]:
        """每个字幕 id 当前的译文（来自已完成批次）。"""
        merged: dict[str, str] = {}
        for batch in self.completed_batches().values():
            merged.update(batch)
        return merged

    def progress(self, total_batches: int | None = None) -> dict:
        total = int(total_batches or self.get_param(PARAM_TOTAL_BATCHES, 0) or 0)
        statuses = self.batch_statuses()
        ok = sum(1 for s in statuses.values() if s == STATUS_OK)
        error = sum(1 for s in statuses.values() if s == STATUS_ERROR)
        return {
            "total": total,
            "ok": ok,
            "error": error,
            "pending": max(total - ok - error, 0),
            "percent": round(ok / total * 100, 1) if total else 0.0,
        }

    # -------------------------------------------------------------- 字幕
    def save_subtitles(self, subtitles: list[dict]) -> int:
        """逐条写入字幕：id / 序号 / 时间轴 / 原文齐全，只留 translation 待填。

        已有译文不会被覆盖 —— 续跑时重导一次字幕不会把翻好的内容抹掉。
        """
        rows = [
            (int(s["id"]), i + 1, str(s["time"]), str(s["text"]))
            for i, s in enumerate(subtitles)
            if str(s["id"]).isdigit()
        ]
        if not rows:
            return 0
        with db.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO source_srt (id, pos, time, text)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pos=excluded.pos, time=excluded.time, text=excluded.text
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def count_subtitles(self) -> int:
        with db.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM source_srt").fetchone()[0]

    def has_subtitles(self) -> bool:
        return self.count_subtitles() > 0

    def subtitle_rows(self) -> list[dict]:
        """逐条字幕（含已回填的译文），主界面列表直接读它。"""
        with db.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, pos, time, text, translation FROM source_srt ORDER BY pos"
            ).fetchall()
        return [
            {
                "id": r[0],
                "pos": r[1],
                "time": r[2],
                "text": r[3],
                "translation": r[4] or "",
            }
            for r in rows
        ]

    def subtitles(self) -> list[dict]:
        """翻译任务需要的字幕三元组（不含译文）。"""
        return [
            {"id": r["id"], "time": r["time"], "text": r["text"]}
            for r in self.subtitle_rows()
        ]

    def apply_translations(self, translations: dict[str, str]) -> None:
        """批次完成后回填译文（只补这一列，不动画面其余内容）。"""
        rows = [
            (v, int(k)) for k, v in (translations or {}).items() if str(k).isdigit()
        ]
        if not rows:
            return
        with db.connect(self.db_path) as conn:
            conn.executemany(
                "UPDATE source_srt SET translation=? WHERE id=?", rows
            )
            conn.commit()

    # ------------------------------------------------------------ 提示词资产
    def save_prompt(self, name: str, content: str) -> None:
        with db.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO prompt_files (name, content, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    content=excluded.content, updated_at=excluded.updated_at
                """,
                (name, content, _now()),
            )
            conn.commit()

    def get_prompt(self, name: str) -> str | None:
        with db.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT content FROM prompt_files WHERE name=?", (name,)
            ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ 元信息
    def source_meta(self) -> dict:
        """字幕来源（文件名 + 已入表行数）。没有 path：不再记录任何路径。"""
        return {
            "filename": self.get_param(PARAM_INPUT_NAME, ""),
            "lines": self.count_subtitles(),
        }

    def source_stem(self) -> str:
        """产物文件名主干：优先用记录在案的字幕文件名。"""
        name = self.get_param(PARAM_INPUT_NAME, "")
        return Path(name).stem if name else self.db_path.stem


# -------------------------------------------------------------------- 工厂


def create_job(
    input_srt: str | Path,
    params: dict,
    prompts: dict[str, str],
    log_dir: str | Path | None = None,
) -> JobStore:
    """新建任务：生成 log/<字幕名>_<时间戳>.db。

    一次性完成三件事
    1. api_params 记字幕文件名（INPUT_NAME）与创建时间
    2. source_srt 逐条写入字幕，只空着 translation 一列
    3. prompt_files 存 system_prompt / customer_prompt 两条记录

    之后这个 log 就是自足的：原 srt 删了也照样能续跑。
    """
    log_dir = Path(log_dir or DEFAULT_LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    src = Path(input_srt)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_path = log_dir / f"{src.stem}_{stamp}.db"

    store = JobStore(db_path)
    store.save_subtitles(S.parse_srt(src))
    store.save_params(
        {
            **params,
            PARAM_INPUT_NAME: src.name,
            "CREATED_AT": _now(),
            PARAM_STATUS: STATUS_PENDING,
        }
    )
    store.save_prompt(PROMPT_SYSTEM, prompts.get(PROMPT_SYSTEM, ""))
    store.save_prompt(PROMPT_CUSTOMER, prompts.get(PROMPT_CUSTOMER, ""))
    return store


def _iter_job_dbs(log_dir: Path) -> list[Path]:
    """log/ 下的任务库：扁平的 <任务名>.db（一任务一库）。"""
    # 已删除的任务（移到 .trash）不再出现在列表里
    return [p for p in sorted(log_dir.glob("*.db")) if TRASH_DIR_NAME not in p.parts]


def scan_log_dir(log_dir: str | Path | None = None) -> list[dict]:
    """扫描 log/ 下所有任务库。"""
    log_dir = Path(log_dir or DEFAULT_LOG_DIR)
    if not log_dir.exists():
        return []

    out: list[dict] = []
    for db in _iter_job_dbs(log_dir):
        try:
            store = JobStore(db)
            params = store.get_params()
            meta = store.source_meta()
        except Exception:
            continue
        out.append(
            {
                "db_path": str(db),
                "name": db.stem,
                "source_name": meta["filename"],
                "source_lines": meta["lines"],
                "has_source": meta["lines"] > 0,
                "model": params.get("MODEL_NAME", ""),
                "status": params.get(PARAM_STATUS, STATUS_PENDING),
                "created_at": params.get("CREATED_AT", ""),
                "progress": store.progress(),
            }
        )
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def trash_job(db_path: str | Path, log_dir: str | Path | None = None) -> Path:
    """删除任务：把任务库整个挪进 log/.trash/（不走系统回收站，可直接找回）。

    返回落点路径，界面拿它提示用户「任务被放到哪里了」。
    """
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"任务库不存在：{src}")

    base = Path(log_dir) if log_dir else src.parent
    trash = base / TRASH_DIR_NAME
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 扁平库（log/<任务名>.db）：连 WAL / SHM 伴生文件一起挪，不留残缺副本
    dest = trash / src.name
    if dest.exists():
        dest = trash / f"{src.stem}_{stamp}{src.suffix}"

    src.replace(dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            side.replace(Path(str(dest) + suffix))

    # 同名附属目录（产物 log/<任务名>/output）也一并挪走
    folder = src.parent / src.stem
    if folder.is_dir():
        dest_folder = trash / folder.name
        if dest_folder.exists():
            dest_folder = trash / f"{folder.name}_{stamp}"
        folder.replace(dest_folder)

    return dest
