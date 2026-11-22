"""治理事件日志设施：审计事件的装配、序列化、读取与归档确认。

收拢原先散落在各业务服务里的审计条目装配逻辑，以及 audit_log 服务的
归档序列化逻辑。审计归档与审计事件日志共用同一套字段语义：

    记录编号 ↔ record_id      操作主体 ↔ subject
    操作类型 ↔ operation      操作对象 ↔ target
    操作时间 ↔ operated_at    影响范围 ↔ scope
    审计摘要 ↔ summary        归档时间 → archived_at

归档确认具备幂等性：同一事件进入待归档列表后，无论归档中断重试还是
并发重放，重复确认只产生一份有效档案；档案文件缺失或损坏（中断残留）
时按条目当前字段重建，条目的操作时间与分类口径保持不变。只有字段
足以完整还原治理操作的审计条目才能确认为有效档案。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "audit")

# 能完整还原一次治理操作所必需的条目字段
GOVERNANCE_REQUIRED_FIELDS = ["记录编号", "操作主体", "操作类型", "操作对象", "操作时间"]

# 归档记录字段 ↔ 审计事件字段的对应关系（序列化/还原共用）
_FIELD_MAP = [
    ("record_id", "记录编号"),
    ("subject", "操作主体"),
    ("operation", "操作类型"),
    ("target", "操作对象"),
    ("operated_at", "操作时间"),
    ("scope", "影响范围"),
    ("summary", "审计摘要"),
]

# 归档确认串行化：并发重放下同一事件只会被首次确认一次
_ARCHIVE_LOCK = threading.RLock()


def append_audit_entry(entity: str, action: str, target_id: str,
                       detail: str) -> dict[str, Any]:
    """装配并追加一条审计事件日志，各业务服务共用同一装配口径。"""
    audit_rows = store.rows("audit_log")
    next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
    entry = {
        "id": next_id,
        "status": "已记录",
        "pending": False,
        "abnormal": False,
        "记录编号": f"AUDI-{next_id:04d}",
        "操作主体": entity,
        "操作类型": action,
        "操作对象": target_id,
        "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "影响范围": detail,
        "审计摘要": detail,
        "归档状态": "已记录",
    }
    audit_rows.append(entry)
    return entry


def can_restore_governance_event(entry: dict[str, Any]) -> bool:
    """判断条目字段是否足以完整还原治理操作（成为有效档案的前提）。"""
    return all(
        str(entry.get(field) or "").strip()
        for field in GOVERNANCE_REQUIRED_FIELDS
    )


def serialize_governance_event(entry: dict[str, Any]) -> dict[str, Any]:
    """把审计事件序列化为归档记录，archived_at 为本次归档时间。"""
    record = {key: str(entry.get(field, "")) for key, field in _FIELD_MAP}
    record["archived_at"] = datetime.now(timezone.utc).isoformat()
    return record


def restore_governance_event(record: dict[str, Any]) -> dict[str, str]:
    """把归档记录还原为治理事件字段，与审计事件日志同口径。"""
    return {field: str(record.get(key, "")) for key, field in _FIELD_MAP}


def archive_path(record_id: str) -> str:
    return os.path.join(ARCHIVE_DIR, f"audit_{record_id}.json")


def read_archive(record_id: str) -> dict[str, Any] | None:
    """读取归档记录；文件缺失或损坏（归档中断残留）时返回 None。"""
    path = archive_path(record_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def is_valid_archive(record: Any) -> bool:
    """归档记录是否完整到可以还原治理操作。"""
    if not isinstance(record, dict):
        return False
    restored = restore_governance_event(record)
    return all(restored.get(field, "").strip() for field in GOVERNANCE_REQUIRED_FIELDS)


def archives_same_event(entry: dict[str, Any], record: dict[str, Any]) -> bool:
    """归档记录是否与条目描述同一治理事件（归档时间本身不参与比对）。"""
    if not is_valid_archive(record):
        return False
    restored = restore_governance_event(record)
    return all(
        restored[field] == str(entry.get(field, ""))
        for _, field in _FIELD_MAP
    )


def write_archive(record: dict[str, Any]) -> None:
    """原子写入归档文件：先写临时文件再改名，中断不会留下半截档案。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = archive_path(str(record.get("record_id", "")))
    fd, tmp_path = tempfile.mkstemp(dir=ARCHIVE_DIR, prefix=".audit_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def confirm_archive(entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """确认一条审计事件的归档，返回 (归档记录, 是否本次首次确认)。

    幂等：同一事件重复确认（含归档中断后的重放、并发重放）只确认一次；
    档案文件缺失或损坏时按条目当前字段重建，条目字段本身不被改写，
    仅推进归档状态（status/pending/归档状态）。
    """
    with _ARCHIVE_LOCK:
        first_confirm = entry.get("status") != "已归档"
        record_id = str(entry.get("记录编号", ""))
        existing = read_archive(record_id)
        if existing is not None and archives_same_event(entry, existing):
            record = existing
        else:
            record = serialize_governance_event(entry)
            write_archive(record)
        entry["status"] = "已归档"
        entry["pending"] = False
        entry["归档状态"] = "已归档"
        return record, first_confirm
