"""compliance_check 领域服务：合规检查管理，检查数据资产合规性并生成审计日志和质量报告。

跨模块数据流：
    asset_catalog → compliance_check: 资产变更触发合规扫描
    compliance_check → audit_log: 检查结果写入审计日志
    compliance_check → quality_report: 合规结果纳入质量报告
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services import audit_events
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "quality")


class ComplianceCheckService(BaseModuleService):
    MODULE = "compliance_check"
    ENTITY = "合规规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "检查维度", "合规标准", "扫描范围", "阻断阈值", "修复建议", "检查频率", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "检查维度", "合规标准"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def run_check(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """执行合规检查：扫描关联资产并生成检查结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        dimension = str(entry.get("检查维度", ""))
        standard = str(entry.get("合规标准", ""))
        scope = str(entry.get("扫描范围", ""))

        # 模拟合规扫描：统计关联的资产模块中的pending和abnormal状态
        checked_count = 0
        violations = 0
        for module_name in store.module_names():
            for row in store.rows(module_name):
                if scope and str(row.get("所属域", "") + str(row.get("关联资产", ""))).find(scope[:3]) >= 0:
                    checked_count += 1
                    if row.get("abnormal"):
                        violations += 1
                checked_count += 1
                if row.get("abnormal"):
                    violations += 1

        passed = violations == 0

        # 写入合规检查结果
        result = {
            "rule_id": str(entry.get("规则编号", "")),
            "dimension": dimension,
            "standard": standard,
            "scope": scope,
            "checked_count": checked_count,
            "violations": violations,
            "passed": passed,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_check_result(str(entry.get("规则编号", "")), result)

        # 写入审计日志
        audit_events.append_audit_entry(
            entity="合规检查",
            action="执行扫描",
            target_id=str(entry.get("规则编号", "")),
            detail=f"合规检查 {dimension}: {violations} / {checked_count} 违规",
        )

        # 更新质量报告
        self._update_quality_report(dimension, checked_count, violations)

        message = f"合规检查完成：{checked_count}项，{violations}违规{'，通过' if passed else '，不通过'}"
        return entry, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "标记生效":
            return self.run_check(entry_id)
        return super().run_action(entry_id, action)

    def _write_check_result(self, rule_id: str, result: dict[str, Any]) -> None:
        filename = f"compliance_check_{rule_id}.json"
        path = os.path.join(DATA_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    def _update_quality_report(self, dimension: str, total: int, violations: int) -> None:
        quality_rows = store.rows("quality_report")
        next_id = max((int(r.get("id", 0)) for r in quality_rows), default=0) + 1
        completeness = "高" if total > 0 and violations / total < 0.1 else "中" if violations > 0 else "高"
        report = {
            "id": next_id,
            "status": "已生成",
            "pending": False,
            "abnormal": violations > 0,
            "报告编号": f"QUAL-{next_id:04d}",
            "关联资产": "全量资产",
            "检核时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完整性": completeness,
            "准确性": f"合规率:{1 - violations/max(total,1):.1%}",
            "及时性": "实时",
            "异常数": str(violations),
            "趋势方向": "恶化" if violations > 0 else "稳定",
        }
        quality_rows.append(report)