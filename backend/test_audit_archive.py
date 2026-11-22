"""审计归档重构验收测试。

验收口径：
1. 资产发布、创建快照两类动作的归档内容与审计条目按原字段一一对应；
2. 归档中断（部分写入失败/档案损坏）后重放，已确认的治理记录与归档
   仍按原字段对应，条目的操作时间与分类口径不变；
3. 质量报告中的异常仍只计一次（重放与并发重放都不重复核算）；
4. 只有能完整还原治理操作的审计条目才能成为有效档案。
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))

from app.store import store
from app.services import service_for, audit_events

ARCHIVE_DIR = audit_events.ARCHIVE_DIR
RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1
        return False
    return True


def quality_count():
    return len(store.rows("quality_report"))


def abnormal_audit_count():
    return sum(1 for r in store.rows("audit_log") if r.get("abnormal"))


def latest_audit(**criteria):
    rows = [r for r in store.rows("audit_log")
            if all(str(r.get(k, "")) == v for k, v in criteria.items())]
    return max(rows, key=lambda r: int(r.get("id", 0))) if rows else None


def check_archive_matches(entry, record, subject, operation, target):
    """核对已确认治理记录与归档仍按原字段对应。"""
    check("record_id ↔ 记录编号", record["record_id"] == str(entry["记录编号"]))
    check("subject ↔ 操作主体",
          record["subject"] == subject == str(entry["操作主体"]))
    check("operation ↔ 操作类型",
          record["operation"] == operation == str(entry["操作类型"]))
    check("target ↔ 操作对象",
          record["target"] == target == str(entry["操作对象"]))
    check("operated_at ↔ 操作时间",
          record["operated_at"] == str(entry["操作时间"]))
    check("scope ↔ 影响范围", record["scope"] == str(entry["影响范围"]))
    check("summary ↔ 审计摘要", record["summary"] == str(entry["审计摘要"]))
    check("archived_at 已落盘", bool(str(record.get("archived_at", "")).strip()))


def test_serialize_restore_roundtrip():
    """治理事件的序列化与读取共用审计事件日志字段语义"""
    print("\n=== 测试1: 治理事件序列化/读取的字段语义 ===")
    entry = audit_events.append_audit_entry(
        entity="数据资产", action="发布", target_id="ASSE-9001",
        detail="序列化口径演练")
    record = audit_events.serialize_governance_event(entry)
    check("归档记录字段集沿用原口径",
          set(record.keys()) == {"record_id", "subject", "operation", "target",
                                 "operated_at", "scope", "summary", "archived_at"},
          f"实际字段: {sorted(record.keys())}")
    restored = audit_events.restore_governance_event(record)
    consistent = all(
        restored[field] == str(entry.get(field, ""))
        for field in ["记录编号", "操作主体", "操作类型", "操作对象",
                      "操作时间", "影响范围", "审计摘要"]
    )
    check("归档记录可完整还原治理事件", consistent)
    check("完整条目可还原治理操作", audit_events.can_restore_governance_event(entry))


def test_publish_archive_content():
    """资产发布动作：归档内容与审计条目逐字段对应"""
    print("\n=== 测试2: 资产发布动作的归档内容对照 ===")
    svc = service_for("asset_catalog")
    audit_svc = service_for("audit_log")

    entry, msg = svc.publish_asset(3)
    check("资产发布成功", entry is not None, msg)

    audit_entry = latest_audit(操作主体="数据资产", 操作类型="发布", 操作对象="ASSE-0003")
    check("发布审计条目已装配", audit_entry is not None)
    if not audit_entry:
        return
    check("装配字段口径不变",
          audit_entry["status"] == "已记录"
          and audit_entry["归档状态"] == "已记录"
          and audit_entry["影响范围"] == audit_entry["审计摘要"]
          and str(audit_entry["记录编号"]).startswith("AUDI-"))

    operated_at = audit_entry["操作时间"]
    before_quality = quality_count()
    archived, msg = audit_svc.run_action(audit_entry["id"], "提交归档")
    check("提交归档成功", archived is not None, msg)
    if not archived:
        return
    check("归档后状态推进口径不变",
          archived["status"] == "已归档" and archived["归档状态"] == "已归档"
          and archived["pending"] is False)
    check("操作时间保持原值", archived["操作时间"] == operated_at)

    path = audit_events.archive_path(str(audit_entry["记录编号"]))
    check("归档文件已生成", os.path.exists(path), path)
    record = audit_events.read_archive(str(audit_entry["记录编号"]))
    check("归档记录可读取且为有效档案",
          record is not None and audit_events.is_valid_archive(record))
    if record:
        check_archive_matches(audit_entry, record, "数据资产", "发布", "ASSE-0003")

    check("质量报告仅新增一份", quality_count() == before_quality + 1)
    report = store.rows("quality_report")[-1]
    check("质量报告异常分类口径不变",
          report["异常数"] == str(abnormal_audit_count())
          and report["准确性"] == f"审计异常:{abnormal_audit_count()}"
          and report["关联资产"] == "全量资产")


def test_snapshot_archive_content():
    """创建快照动作：归档内容与审计条目逐字段对应"""
    print("\n=== 测试3: 创建快照动作的归档内容对照 ===")
    snap_svc = service_for("version_snapshot")
    audit_svc = service_for("audit_log")

    snap_no = f"VERS-T{len(store.rows('version_snapshot')):04d}"
    entry, missing = snap_svc.create_entry({
        "快照编号": snap_no, "关联资产": "ASSE-0003", "快照时间": "2026-11-22",
        "数据量": "10MB", "快照类型": "手动快照", "保留天数": "30",
        "依赖快照": "", "快照状态": "创建中",
    })
    check("快照登记成功", entry is not None, str(missing))
    if not entry:
        return
    created, msg = snap_svc.create_snapshot(entry["id"])
    check("快照创建成功", created is not None, msg)

    audit_entry = latest_audit(操作主体="版本快照", 操作类型="创建", 操作对象=snap_no)
    check("快照审计条目已装配", audit_entry is not None)
    if not audit_entry:
        return
    archived, msg = audit_svc.archive_logs(audit_entry["id"])
    check("快照审计归档成功", archived is not None, msg)
    if not archived:
        return
    record = audit_events.read_archive(str(audit_entry["记录编号"]))
    check("归档记录可读取且为有效档案",
          record is not None and audit_events.is_valid_archive(record))
    if record:
        check_archive_matches(audit_entry, record, "版本快照", "创建", snap_no)


def test_interrupted_archive_replay():
    """归档文件部分写入失败后重放：只确认一次，档案按条目字段重建"""
    print("\n=== 测试4: 归档中断后重放 ===")
    audit_svc = service_for("audit_log")
    event = audit_events.append_audit_entry(
        entity="数据资产", action="发布", target_id="ASSE-9002",
        detail="归档中断重放演练")
    path = audit_events.archive_path(str(event["记录编号"]))
    if os.path.exists(path):
        # 清理上一轮演练留下的归档，模拟事件首次进入待归档列表
        os.unlink(path)

    # 模拟归档文件部分写入失败：目标路径留下半截档案，写入中断
    original_write = audit_events.write_archive

    def broken_write(record):
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"record_id": "%s", "subject": ' % record["record_id"])
        raise OSError("模拟归档中断")

    before_quality = quality_count()
    audit_events.write_archive = broken_write
    try:
        entry, msg = audit_svc.archive_logs(event["id"])
        check("归档中断返回失败", entry is None, msg)
    finally:
        audit_events.write_archive = original_write
    check("中断后条目未被确认",
          event["status"] != "已归档" and event["归档状态"] != "已归档")
    check("半截档案不被视为有效档案",
          audit_events.read_archive(str(event["记录编号"])) is None)
    check("中断未触发质量核算", quality_count() == before_quality)

    # 重放：按条目当前字段重建档案
    operated_at = event["操作时间"]
    entry, msg = audit_svc.archive_logs(event["id"])
    check("中断后重放归档成功", entry is not None, msg)
    record = audit_events.read_archive(str(event["记录编号"]))
    check("重放后档案完整有效",
          record is not None and audit_events.is_valid_archive(record))
    if record:
        check_archive_matches(event, record, "数据资产", "发布", "ASSE-9002")
    check("重放保持操作时间原值", event["操作时间"] == operated_at)
    check("质量报告只核算一次", quality_count() == before_quality + 1)

    # 再次重放已确认事件：档案与质量报告都不再变化
    archived_at = record["archived_at"] if record else ""
    entry, msg = audit_svc.archive_logs(event["id"])
    check("重复确认返回成功", entry is not None, msg)
    record2 = audit_events.read_archive(str(event["记录编号"]))
    check("重复确认不重写档案",
          record2 is not None and record2["archived_at"] == archived_at)
    check("重复确认不重复核算质量", quality_count() == before_quality + 1)


def test_concurrent_replay_single_archive():
    """并发重放同一事件：只确认一份档案，质量异常只计一次"""
    print("\n=== 测试5: 并发重放只确认一份档案 ===")
    audit_svc = service_for("audit_log")
    event = audit_events.append_audit_entry(
        entity="数据资产", action="发布", target_id="ASSE-9003",
        detail="并发重放演练")

    before_quality = quality_count()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: audit_svc.archive_logs(event["id"]), range(8)))
    succeeded = [e for e, _ in results if e is not None]
    check("并发归档全部返回成功", len(succeeded) == 8,
          f"成功 {len(succeeded)}/8")

    record = audit_events.read_archive(str(event["记录编号"]))
    check("并发后档案完整有效",
          record is not None and audit_events.is_valid_archive(record))
    if record:
        check_archive_matches(event, record, "数据资产", "发布", "ASSE-9003")
    check("同一事件只确认一次", event["status"] == "已归档")
    check("质量报告只核算一次", quality_count() == before_quality + 1,
          f"质量报告数={quality_count()}, 期望={before_quality + 1}")
    leftovers = [f for f in os.listdir(ARCHIVE_DIR) if f.startswith(".audit_")]
    check("无中断临时文件残留", not leftovers, str(leftovers))


def test_historical_entry_abnormal_counted_once():
    """历史条目保持原操作时间与分类口径，异常在质量报告中只计一次"""
    print("\n=== 测试6: 历史条目口径与异常只计一次 ===")
    audit_svc = service_for("audit_log")
    entry = store.find("audit_log", 2)  # AUDI-0002：已校验、abnormal
    check("历史异常条目存在", entry is not None and bool(entry.get("abnormal")))
    if not entry:
        return
    operated_at = entry["操作时间"]
    op_type = entry["操作类型"]
    op_subject = entry["操作主体"]

    before_quality = quality_count()
    archived, msg = audit_svc.archive_logs(2)
    check("历史条目归档成功", archived is not None, msg)
    if not archived:
        return
    check("操作时间保持原值", archived["操作时间"] == operated_at == "2026-09-02")
    check("分类口径保持原值",
          archived["操作类型"] == op_type and archived["操作主体"] == op_subject)
    record = audit_events.read_archive(str(archived["记录编号"]))
    check("档案 operated_at 与历史一致",
          record is not None and record["operated_at"] == "2026-09-02")

    check("质量报告新增一份", quality_count() == before_quality + 1)
    report = store.rows("quality_report")[-1]
    check("异常数只计一次", report["异常数"] == str(abnormal_audit_count()),
          f"异常数={report['异常数']}, 实际异常条目={abnormal_audit_count()}")

    archived2, _ = audit_svc.archive_logs(2)
    check("历史条目重放返回成功", archived2 is not None)
    check("重放后异常仍只计一次", quality_count() == before_quality + 1)


def test_incomplete_entry_rejected():
    """字段不足以完整还原治理操作的审计不能成为有效档案"""
    print("\n=== 测试7: 无法完整还原治理操作的条目不成为有效档案 ===")
    audit_svc = service_for("audit_log")
    rows = store.rows("audit_log")
    nid = max(int(r.get("id", 0)) for r in rows) + 1
    incomplete = {
        "id": nid, "status": "已记录", "pending": False, "abnormal": False,
        "记录编号": f"AUDI-{nid:04d}", "操作主体": "数据资产", "操作类型": "发布",
        "操作对象": "", "操作时间": "", "影响范围": "残缺条目", "审计摘要": "残缺条目",
        "归档状态": "已记录",
    }
    rows.append(incomplete)
    check("残缺条目不可还原治理操作",
          not audit_events.can_restore_governance_event(incomplete))

    before_quality = quality_count()
    entry, msg = audit_svc.archive_logs(nid)
    check("残缺条目归档被拒绝", entry is None, msg)
    check("未生成档案文件",
          not os.path.exists(audit_events.archive_path(incomplete["记录编号"])))
    check("条目状态未被推进", incomplete["status"] == "已记录")
    check("质量报告未受影响", quality_count() == before_quality)


def test_atomic_write_leaves_no_partial():
    """原子写入：中断不留半截档案，临时文件必清理"""
    print("\n=== 测试8: 原子写入不留下半截档案 ===")
    record = {
        "record_id": "AUDI-TMPTEST", "subject": "数据资产", "operation": "发布",
        "target": "ASSE-9999", "operated_at": "2026-11-22", "scope": "s",
        "summary": "s", "archived_at": "2026-11-22T00:00:00+00:00",
    }
    path = audit_events.archive_path(record["record_id"])
    if os.path.exists(path):
        os.unlink(path)

    original_replace = audit_events.os.replace

    def failing_replace(src, dst):
        raise OSError("模拟改名前中断")

    audit_events.os.replace = failing_replace
    try:
        try:
            audit_events.write_archive(record)
            check("写入中断应以错误暴露", False)
        except OSError:
            check("写入中断以错误暴露", True)
    finally:
        audit_events.os.replace = original_replace
    check("目标路径无半截档案", not os.path.exists(path))
    leftovers = [f for f in os.listdir(ARCHIVE_DIR) if f.startswith(".audit_")]
    check("临时文件已清理", not leftovers, str(leftovers))


if __name__ == "__main__":
    print("=" * 60)
    print("审计归档重构验收测试")
    print("=" * 60)

    test_serialize_restore_roundtrip()
    test_publish_archive_content()
    test_snapshot_archive_content()
    test_interrupted_archive_replay()
    test_concurrent_replay_single_archive()
    test_historical_entry_abnormal_counted_once()
    test_incomplete_entry_rejected()
    test_atomic_write_leaves_no_partial()

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
