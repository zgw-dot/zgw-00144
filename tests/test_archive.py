import os
import sys
import shutil
import json
import tempfile
from pathlib import Path

from patrol_cli.storage import PatrolState
from patrol_cli.workflow import (
    create_template, update_template, delete_template,
    create_draft, create_draft_from_template,
    execute_draft, WorkflowError,
    publish_version, list_archives, get_archive, compare_archives,
    preview_restore_archive, restore_archive,
    export_archives, import_archives, precheck_archive_import
)
from patrol_cli.models import (
    TemplateArchive, ArchiveDiffItem, ArchiveCompareResult,
    ArchiveRestorePreview, STATUS_NAMES
)


def setup_test_dir():
    return tempfile.mkdtemp(prefix="patrol_archive_test_")


def teardown_test_dir(test_dir):
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)


def create_sample_defect(state, defect_id, **kwargs):
    from patrol_cli.models import DefectRecord
    from datetime import datetime

    defaults = {
        "defect_id": defect_id,
        "building": "1号楼",
        "device_id": "EL-001",
        "device_category": "elevator",
        "defect_type": "门机故障",
        "severity": "high",
        "description": "电梯门机异响",
        "first_seen": datetime.now().isoformat(),
        "last_seen": datetime.now().isoformat(),
        "status": "pending",
        "source_rows": [],
        "review_remark": "",
        "handler": "",
        "status_history": []
    }
    defaults.update(kwargs)
    defect = DefectRecord(**defaults)
    state.add_defect(defect)
    state.save()
    return defect


def test_archive_auto_create_on_publish():
    """发布版本时自动创建档案"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-ARC-001"

        tpl = create_template(
            state, name="归档测试模板", target_status="dispatched",
            handler="张工", remark="标准派单", source_type="daily"
        )

        ver = publish_version(state, tpl.template_id, "v1.0", published_by="管理员")

        archives = list_archives(state, template_id=tpl.template_id)
        assert len(archives) == 1

        arc = archives[0]
        assert arc.archive_id
        assert arc.template_id == tpl.template_id
        assert arc.template_name == "归档测试模板"
        assert arc.version_name == "v1.0"
        assert arc.target_status == "dispatched"
        assert arc.handler == "张工"
        assert arc.remark == "标准派单"
        assert arc.source_type == "daily"
        assert arc.template_snapshot
        assert arc.template_snapshot["name"] == "归档测试模板"
        assert arc.published_at
        assert arc.archived_at
        assert arc.published_by == "管理员"

        arc_by_id = get_archive(state, arc.archive_id)
        assert arc_by_id.archive_id == arc.archive_id

        audit_logs = state.get_audit_logs(action="archive_create")
        assert len(audit_logs) >= 1
        assert any(log.result == "success" for log in audit_logs)
    finally:
        teardown_test_dir(test_dir)


def test_archive_survives_template_delete():
    """模板删除后，档案仍然存在且可查询"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="将被删除的模板", target_status="closed",
            handler="李工", remark="待删除", source_type="manual"
        )

        ver = publish_version(state, tpl.template_id, "v1.0")
        arc_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        delete_template(state, tpl.template_id)

        assert state.get_template(tpl.template_id) is None

        arc = get_archive(state, arc_id)
        assert arc is not None
        assert arc.template_name == "将被删除的模板"
        assert arc.target_status == "closed"
        assert arc.handler == "李工"
        assert arc.remark == "待删除"

        all_archives = list_archives(state)
        assert len(all_archives) >= 1

        by_name = list_archives(state, template_name="将被删除的模板")
        assert len(by_name) == 1
        assert by_name[0].archive_id == arc_id
    finally:
        teardown_test_dir(test_dir)


def test_archive_name_not_affected_by_template_rename():
    """模板改名后，档案中的模板名称保持不变（固化）"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="原名模板", target_status="dispatched",
            handler="张工"
        )

        ver = publish_version(state, tpl.template_id, "v1.0")
        arc_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        update_template(state, tpl.template_id, name="新名模板")

        arc = get_archive(state, arc_id)
        assert arc.template_name == "原名模板"

        tpl_reloaded = state.get_template(tpl.template_id)
        assert tpl_reloaded.name == "新名模板"
    finally:
        teardown_test_dir(test_dir)


def test_archive_compare():
    """比较两个档案的差异"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="比较测试模板", target_status="dispatched",
            handler="张工", remark="版本1备注", source_type="daily"
        )

        ver1 = publish_version(state, tpl.template_id, "v1.0")
        arc1_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        update_template(state, tpl.template_id, handler="李工", remark="版本2备注", source_type="manual")
        ver2 = publish_version(state, tpl.template_id, "v2.0")
        arc2_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        result = compare_archives(state, arc1_id, arc2_id)

        assert not result.is_same
        diff_fields = [d.field_name for d in result.diffs]
        assert "handler" in diff_fields
        assert "remark" in diff_fields
        assert "source_type" in diff_fields
        assert "target_status" not in diff_fields

        handler_diff = [d for d in result.diffs if d.field_name == "handler"][0]
        assert handler_diff.old_value == "张工"
        assert handler_diff.new_value == "李工"
    finally:
        teardown_test_dir(test_dir)


def test_archive_restore_preview_dry_run():
    """档案恢复预览（dry-run）"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="恢复预览模板", target_status="dispatched",
            handler="张工", remark="原始备注", source_type="daily"
        )

        ver = publish_version(state, tpl.template_id, "v1.0")
        arc_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        update_template(state, tpl.template_id, handler="李工", remark="新备注", source_type="manual")

        preview = preview_restore_archive(state, arc_id)

        assert preview.version_name == "v1.0"
        assert preview.template_exists is True
        assert preview.current_handler == "李工"
        assert preview.restore_handler == "张工"
        assert preview.current_remark == "新备注"
        assert preview.restore_remark == "原始备注"
        assert preview.current_source_type == "manual"
        assert preview.restore_source_type == "daily"
        assert len(preview.diffs) == 3
        assert preview.restore_action == "覆盖更新现有模板"
    finally:
        teardown_test_dir(test_dir)


def test_archive_restore_deleted_template():
    """从档案恢复已删除的模板（自动重建）"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="将被删再恢复的模板", target_status="dispatched",
            handler="王工", remark="原始备注", source_type="daily",
            description="测试描述"
        )
        tpl_id = tpl.template_id

        ver = publish_version(state, tpl_id, "v1.0", published_by="管理员")
        arc_id = list_archives(state, template_id=tpl_id)[0].archive_id

        delete_template(state, tpl_id)
        assert state.get_template(tpl_id) is None

        preview = preview_restore_archive(state, arc_id)
        assert preview.template_exists is False
        assert preview.restore_action == "新建模板（从档案恢复）"

        restored = restore_archive(state, arc_id, restored_by="恢复操作人")

        assert restored.template_id == tpl_id
        assert restored.name == "将被删再恢复的模板"
        assert restored.target_status == "dispatched"
        assert restored.handler == "王工"
        assert restored.remark == "原始备注"
        assert restored.source_type == "daily"
        assert restored.description == "测试描述"

        audit_logs = state.get_audit_logs(action="archive_restore")
        assert len(audit_logs) == 1
        assert audit_logs[0].result == "success"
        assert "新建" in audit_logs[0].detail
    finally:
        teardown_test_dir(test_dir)


def test_archive_restore_existing_template():
    """从档案恢复现有模板（覆盖更新）"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="覆盖恢复模板", target_status="dispatched",
            handler="张工", remark="原始备注"
        )

        ver = publish_version(state, tpl.template_id, "v1.0")
        arc_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        update_template(state, tpl.template_id, handler="李工", remark="新备注", source_type="manual")

        restored = restore_archive(state, arc_id)

        assert restored.handler == "张工"
        assert restored.remark == "原始备注"
        assert restored.target_status == "dispatched"

        audit_logs = state.get_audit_logs(action="archive_restore")
        assert len(audit_logs) == 1
        assert "覆盖更新" in audit_logs[0].detail
    finally:
        teardown_test_dir(test_dir)


def test_archive_export_import():
    """档案导出和导入"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl1 = create_template(
            state, name="导出模板A", target_status="dispatched",
            handler="甲", remark="备注A", source_type="daily"
        )
        tpl2 = create_template(
            state, name="导出模板B", target_status="closed",
            handler="乙", remark="备注B"
        )

        publish_version(state, tpl1.template_id, "v1.0")
        publish_version(state, tpl1.template_id, "v2.0")
        publish_version(state, tpl2.template_id, "v1.0")

        export_path = os.path.join(test_dir, "export_archives.json")
        count = export_archives(state, export_path)
        assert count == 3

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 3

        import_dir = setup_test_dir()
        try:
            state2 = PatrolState(data_dir=import_dir)

            precheck = precheck_archive_import(state2, export_path)
            assert not precheck["has_conflicts"]
            assert precheck["total_archives"] == 3

            result = import_archives(state2, export_path, conflict_strategy="skip")
            assert len(result["imported"]) == 3
            assert len(result["errors"]) == 0
            assert result["audit_id"]

            archives = list_archives(state2)
            assert len(archives) == 3

            audit_logs = state2.get_audit_logs(action="archive_import")
            assert len(audit_logs) == 1
        finally:
            teardown_test_dir(import_dir)
    finally:
        teardown_test_dir(test_dir)


def test_archive_import_conflict_precheck():
    """档案导入冲突预检"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="冲突模板", target_status="dispatched",
            handler="甲", source_type="daily"
        )
        publish_version(state, tpl.template_id, "v1.0")

        export_path = os.path.join(test_dir, "conflict_export.json")
        export_archives(state, export_path)

        precheck = precheck_archive_import(state, export_path)
        assert precheck["has_conflicts"]
        assert len(precheck["conflicts"]) >= 1
        assert any("冲突模板" in c["name"] for c in precheck["conflicts"])
    finally:
        teardown_test_dir(test_dir)


def test_archive_import_conflict_strategies():
    """档案导入冲突处理策略"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="策略模板", target_status="dispatched",
            handler="甲", source_type="daily"
        )
        publish_version(state, tpl.template_id, "v1.0")

        export_path = os.path.join(test_dir, "strategy_export.json")
        export_archives(state, export_path)

        skip_dir = setup_test_dir()
        try:
            state_skip = PatrolState(data_dir=skip_dir)
            result = import_archives(state_skip, export_path, conflict_strategy="skip")
            assert len(result["imported"]) == 1

            result2 = import_archives(state_skip, export_path, conflict_strategy="skip")
            assert len(result2["skipped"]) >= 1
        finally:
            teardown_test_dir(skip_dir)

        overwrite_dir = setup_test_dir()
        try:
            state_ow = PatrolState(data_dir=overwrite_dir)
            import_archives(state_ow, export_path, conflict_strategy="overwrite")

            result2 = import_archives(state_ow, export_path, conflict_strategy="overwrite")
            assert any("覆盖" in t for t in result2["imported"])
        finally:
            teardown_test_dir(overwrite_dir)

        save_as_dir = setup_test_dir()
        try:
            state_sa = PatrolState(data_dir=save_as_dir)
            import_archives(state_sa, export_path, conflict_strategy="save_as")

            result2 = import_archives(state_sa, export_path, conflict_strategy="save_as")
            assert len(result2["saved_as"]) >= 1
        finally:
            teardown_test_dir(save_as_dir)

        abort_dir = setup_test_dir()
        try:
            state_ab = PatrolState(data_dir=abort_dir)
            import_archives(state_ab, export_path, conflict_strategy="skip")

            result2 = import_archives(state_ab, export_path, conflict_strategy="abort")
            assert len(result2["errors"]) > 0
            assert any("冲突中止" in e for e in result2["errors"])
        finally:
            teardown_test_dir(abort_dir)
    finally:
        teardown_test_dir(test_dir)


def test_archive_restart_persistence():
    """档案跨重启持久化"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="重启模板", target_status="dispatched",
            handler="张工", remark="重启测试"
        )
        ver = publish_version(state, tpl.template_id, "v1.0", published_by="管理员")
        arc_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        state2 = PatrolState(data_dir=test_dir)

        archives = list_archives(state2, template_id=tpl.template_id)
        assert len(archives) == 1
        assert archives[0].version_name == "v1.0"
        assert archives[0].handler == "张工"
        assert archives[0].template_snapshot["handler"] == "张工"
        assert archives[0].published_by == "管理员"

        arc = get_archive(state2, arc_id)
        assert arc is not None
        assert arc.archive_id == arc_id

        audit_logs = state2.get_audit_logs(action="archive_create")
        assert len(audit_logs) >= 1
    finally:
        teardown_test_dir(test_dir)


def test_archive_draft_snapshot_independence():
    """草稿快照独立于模板和档案，绑定各自产生时的数据"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-ARC-DRAFT"

        defect1 = create_sample_defect(state, "DEF-ARC-001")
        defect2 = create_sample_defect(state, "DEF-ARC-002", building="2号楼")

        tpl = create_template(
            state, name="独立模板", target_status="dispatched",
            handler="张工", remark="原始"
        )

        ver1 = publish_version(state, tpl.template_id, "v1.0")
        arc1_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        draft = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-ARC-001", source_type="ids"
        )
        execute_draft(state, draft.draft_id)

        update_template(state, tpl.template_id, handler="李工", remark="修改后")
        ver2 = publish_version(state, tpl.template_id, "v2.0")
        arc2_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        restored = restore_archive(state, arc1_id)
        assert restored.handler == "张工"

        draft_reloaded = state.get_draft(draft.draft_id)
        assert draft_reloaded.template_snapshot["handler"] == "张工"
        assert draft_reloaded.execution.success_count == 1

        draft2 = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-ARC-002", source_type="ids"
        )
        assert draft2.template_snapshot["handler"] == "张工"

        arc1 = get_archive(state, arc1_id)
        arc2 = get_archive(state, arc2_id)
        assert arc1.handler == "张工"
        assert arc2.handler == "李工"
    finally:
        teardown_test_dir(test_dir)


def test_archive_export_after_template_delete():
    """模板删除后仍可导出档案"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl1 = create_template(state, name="改名模板", target_status="dispatched", handler="甲")
        tpl2 = create_template(state, name="删除模板", target_status="closed", handler="乙")

        publish_version(state, tpl1.template_id, "v1.0")
        publish_version(state, tpl2.template_id, "v1.0")

        update_template(state, tpl1.template_id, name="改名后模板")
        delete_template(state, tpl2.template_id)

        all_archives = list_archives(state)
        assert len(all_archives) == 2

        export_path = os.path.join(test_dir, "post_delete_export.json")
        count = export_archives(state, export_path)
        assert count == 2

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 2

        arc_names = [arc["template_name"] for arc in data.values()]
        assert "改名模板" in arc_names
        assert "删除模板" in arc_names
    finally:
        teardown_test_dir(test_dir)


def test_archive_audit_logging_complete():
    """档案操作完整审计日志记录"""
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(state, name="审计模板", target_status="dispatched", handler="甲")
        ver = publish_version(state, tpl.template_id, "v1.0")
        arc_id = list_archives(state, template_id=tpl.template_id)[0].archive_id

        update_template(state, tpl.template_id, handler="乙")
        restore_archive(state, arc_id)

        export_path = os.path.join(test_dir, "audit_export.json")
        export_archives(state, export_path)

        import_dir = setup_test_dir()
        try:
            state2 = PatrolState(data_dir=import_dir)
            import_archives(state2, export_path, conflict_strategy="skip")

            audit_logs = state2.get_audit_logs()
            import_logs = [l for l in audit_logs if l.action == "archive_import"]
            assert len(import_logs) == 1
            assert import_logs[0].result == "success"
        finally:
            teardown_test_dir(import_dir)

        all_audit = state.get_audit_logs()
        create_logs = [l for l in all_audit if l.action == "archive_create"]
        restore_logs = [l for l in all_audit if l.action == "archive_restore"]
        export_logs = [l for l in all_audit if l.action == "archive_export"]

        assert len(create_logs) >= 1
        assert len(restore_logs) == 1
        assert len(export_logs) == 1
        assert restore_logs[0].result == "success"
        assert export_logs[0].result == "success"
    finally:
        teardown_test_dir(test_dir)


if __name__ == "__main__":
    import traceback

    tests = [
        test_archive_auto_create_on_publish,
        test_archive_survives_template_delete,
        test_archive_name_not_affected_by_template_rename,
        test_archive_compare,
        test_archive_restore_preview_dry_run,
        test_archive_restore_deleted_template,
        test_archive_restore_existing_template,
        test_archive_export_import,
        test_archive_import_conflict_precheck,
        test_archive_import_conflict_strategies,
        test_archive_restart_persistence,
        test_archive_draft_snapshot_independence,
        test_archive_export_after_template_delete,
        test_archive_audit_logging_complete,
    ]

    print("开始模板档案馆模块测试")
    print("=" * 60)

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  [OK] {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 60)
    if failed == 0:
        print(f"[OK] 全部 {passed} 个测试通过！")
    else:
        print(f"[FAIL] {failed} 个测试失败, {passed} 个通过")
        sys.exit(1)
