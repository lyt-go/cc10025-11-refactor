"""asset_catalog领域服务：资产编目、发布、归档和数据资产管理。

跨模块数据流：
    asset_catalog → tier_policy → cold_storage → hot_prefetch: 编目→分层→冷存→预热
    asset_catalog → lineage_graph → change_notice: 编目→血缘→变更通知
    asset_catalog → schema_registry → version_snapshot: 编目→Schema→快照
    asset_catalog → label_tag → access_gate: 编目→标签→访问网关
    asset_catalog → retention_rule → reclaim_job: 编目→保留→回收
    asset_catalog → cost_tracker: 编目→成本追踪
    asset_catalog → quality_report: 编目→质量报告
    asset_catalog → compliance_check → audit_log: 编目→合规检查→审计日志
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from app.services import audit_events
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class AssetCatalogService(BaseModuleService):
    MODULE = "asset_catalog"
    ENTITY = "数据资产"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["资产编号", "资产名称", "所属域", "资产类型", "负责人", "创建时间", "最近更新", "资产描述"]
    REQUIRED_FIELDS = ["资产编号", "资产名称", "所属域"]
    STATUS_ORDER = ["草稿", "待审核", "已发布", "已归档"]
    ACTION_RULES = {"提交审核": "待审核", "确认发布": "已发布", "标记归档": "已归档"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._snapshot_root = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots")
        os.makedirs(self._snapshot_root, exist_ok=True)

    def publish_asset(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发布资产并写入asset数据产物，触发下游消费链。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] != "已发布":
            return None, f"{self.ENTITY} {entry_id} 必须先审核通过才能发布"

        asset_id = str(entry.get("资产编号", f"ASSET-{entry_id}"))
        asset_name = str(entry.get("资产名称", ""))
        domain = str(entry.get("所属域", ""))

        # 写入数据产物
        asset_record = {
            "asset_id": asset_id,
            "asset_name": asset_name,
            "asset_type": str(entry.get("资产类型", "")),
            "owner": str(entry.get("负责人", "")),
            "domain": domain,
            "description": str(entry.get("资产描述", "")),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "version": 1,
            "status": "已发布",
        }
        self._write_json(f"{asset_id}.json", asset_record)

        # 更新内存状态
        entry["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["status"] = "已发布"
        entry["pending"] = False

        # 触发下游模块: tier_policy（创建分层策略种子）
        self._seed_tier_policy(asset_id, domain)

        # 触发下游模块: cost_tracker（创建成本追踪种子）
        self._seed_cost_tracker(asset_id, domain)

        # 触发下游模块: lineage_graph（注册资产血缘节点）
        self._seed_lineage_node(asset_id, asset_name, domain)

        # 记录审计日志
        audit_events.append_audit_entry(
            entity="数据资产",
            action="发布",
            target_id=asset_id,
            detail=f"资产 {asset_name} 发布，所属域 {domain}",
        )

        return entry, f"{self.ENTITY} {entry_id} 已发布并触发下游消费链"

    def archive_asset(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """归档资产：创建版本快照并更新归档状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        entry["status"] = "已归档"
        entry["pending"] = False
        entry["最近更新"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 创建归档快照
        self._create_snapshot(entry)

        # 停用关联的访问策略
        self._revoke_access_gates(str(entry.get("资产编号", "")))

        # 记录审计
        audit_events.append_audit_entry(
            entity="数据资产",
            action="归档",
            target_id=str(entry.get("资产编号", "")),
            detail=f"资产 {entry.get('资产名称', '')} 已归档",
        )

        return entry, f"{self.ENTITY} {entry_id} 已归档"

    def get_asset_snapshot(self, asset_id: str) -> dict[str, Any] | None:
        """读取资产数据产物。"""
        return self._read_json(f"{asset_id}.json")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """重载：发布操作触发跨模块下游消费，归档操作触发快照创建。"""
        if action == "确认发布":
            return self.publish_asset(entry_id)
        if action == "标记归档":
            return self.archive_asset(entry_id)
        return super().run_action(entry_id, action)

    # ---------- 文件 I/O ----------
    def _read_json(self, filename: str) -> dict[str, Any] | None:
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, filename: str, data: dict[str, Any]) -> None:
        path = os.path.join(DATA_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    # ---------- 跨模块副作用 ----------
    def _seed_tier_policy(self, asset_id: str, domain: str) -> None:
        """为已发布资产生成分层策略种子记录。"""
        tier_rows = store.rows("tier_policy")
        next_id = max((int(r.get("id", 0)) for r in tier_rows), default=0) + 1
        policy = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "策略编号": f"TIER-{next_id:04d}",
            "目标资产": asset_id,
            "当前层级": "HOT",
            "迁移条件": "访问频次<5次/天",
            "目标层级": "COLD",
            "冷却天数": "90",
            "访问频次阈值": "5",
            "策略版本": "v1",
        }
        tier_rows.append(policy)

    def _seed_cost_tracker(self, asset_id: str, domain: str) -> None:
        """为已发布资产创建成本追踪种子。"""
        cost_rows = store.rows("cost_tracker")
        next_id = max((int(r.get("id", 0)) for r in cost_rows), default=0) + 1
        record = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "记录编号": f"COST-{next_id:04d}",
            "关联资产": asset_id,
            "存储成本": "0.00",
            "计算成本": "0.00",
            "传输成本": "0.00",
            "统计周期": datetime.now(timezone.utc).strftime("%Y-%m"),
            "趋势方向": "持平",
            "异常标记": "无",
        }
        cost_rows.append(record)

    def _seed_lineage_node(self, asset_id: str, asset_name: str, domain: str) -> None:
        """注册资产为血缘图谱节点。"""
        lineage_rows = store.rows("lineage_graph")
        next_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        record = {
            "id": next_id,
            "status": "已注册",
            "pending": False,
            "abnormal": False,
            "记录编号": f"LINE-{next_id:04d}",
            "上游资产": "SOURCE",
            "下游资产": asset_id,
            "依赖类型": "数仓入湖",
            "加工逻辑": f"资产编目自动注册:{asset_name}",
            "生效时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "变更SHA": f"ac-{int(time.time())}",
            "血缘状态": "已生效",
        }
        lineage_rows.append(record)

    def _create_snapshot(self, entry: dict[str, Any]) -> None:
        """创建归档资产的版本快照。"""
        snap_rows = store.rows("version_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        snap = {
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"VERS-{next_id:04d}",
            "关联资产": str(entry.get("资产编号", "")),
            "快照时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "数据量": "0MB",
            "快照类型": "归档快照",
            "保留天数": "365",
            "依赖快照": "",
            "快照状态": "已完成",
        }
        snap_rows.append(snap)

    def _revoke_access_gates(self, asset_id: str) -> None:
        """归档时撤销关联的访问策略。"""
        access_rows = store.rows("access_gate")
        for row in access_rows:
            if str(row.get("目标资产")) == asset_id and row.get("status") != "已撤销":
                row["status"] = "已撤销"
                row["pending"] = False