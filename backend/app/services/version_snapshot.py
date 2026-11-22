"""version_snapshot 领域服务：版本快照管理，接收 asset_catalog 和 schema_registry 上游事件。

跨模块数据流：
    asset_catalog → version_snapshot: 资产归档触发快照创建
    asset_catalog → schema_registry → version_snapshot: Schema注册后创建版本快照
    version_snapshot → lineage_graph: 快照创建更新血缘信息
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services import audit_events
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots")


class VersionSnapshotService(BaseModuleService):
    MODULE = "version_snapshot"
    ENTITY = "数据快照"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["快照编号", "关联资产", "快照时间", "数据量", "快照类型", "保留天数", "依赖快照", "快照状态"]
    REQUIRED_FIELDS = ["快照编号", "关联资产", "快照时间"]
    STATUS_ORDER = ["创建中", "已完成", "已过期", "已删除"]
    ACTION_RULES = {"确认完成": "已完成", "标记过期": "已过期", "安全删除": "已删除"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def create_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["快照时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["快照状态"] = "已完成"

        # 写入快照文件
        self._write_snapshot(entry)

        # 更新血缘图谱
        self._update_lineage_snapshot(asset_id)

        # 记录审计
        audit_events.append_audit_entry(
            entity="版本快照",
            action="创建",
            target_id=str(entry.get("快照编号", "")),
            detail=f"快照创建: {asset_id}",
        )

        return entry, f"{self.ENTITY} {entry_id} 已创建"

    def list_snapshots_for_asset(self, asset_id: str) -> list[dict[str, Any]]:
        """列出某资产的所有版本快照。"""
        pattern = f"snapshot_{asset_id}_.json"
        results = []
        for fname in os.listdir(DATA_DIR):
            if fname.startswith(f"snapshot_{asset_id}_"):
                path = os.path.join(DATA_DIR, fname)
                with open(path, "r", encoding="utf-8") as f:
                    results.append(json.load(f))
        return sorted(results, key=lambda x: x.get("snapshot_time", ""), reverse=True)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认完成":
            return self.create_snapshot(entry_id)
        return super().run_action(entry_id, action)

    def _write_snapshot(self, entry: dict[str, Any]) -> None:
        asset_id = str(entry.get("关联资产", ""))
        snapshot_id = str(entry.get("快照编号", ""))
        filename = f"snapshot_{asset_id}_{snapshot_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "snapshot_time": str(entry.get("快照时间", "")),
            "data_volume": str(entry.get("数据量", "")),
            "snapshot_type": str(entry.get("快照类型", "")),
            "retention_days": str(entry.get("保留天数", "")),
            "dependent_snapshot": str(entry.get("依赖快照", "")),
            "status": str(entry.get("快照状态", "")),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_lineage_snapshot(self, asset_id: str) -> None:
        lineage_rows = store.rows("lineage_graph")
        for row in lineage_rows:
            if str(row.get("下游资产")) == asset_id:
                row["变更SHA"] = f"snap-{int(datetime.now(timezone.utc).timestamp())}"
                break