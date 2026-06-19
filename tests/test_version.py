import os
import sys
import shutil
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from patrol_cli.storage import PatrolState
from patrol_cli.workflow import (
    create_template, update_template, delete_template,
    create_draft, create_draft_from_template,
    execute_draft, WorkflowError,
    snapshot_health_check, snapshot_patch,
    publish_version, list_versions, get_version, compare_versions,
    preview_restore_version, restore_version,
    precheck_import_conflicts, import_with_versions, export_with_versions
)
from patrol_cli.models import (
    TemplateVersion, VersionDiffItem, VersionCompareResult,
    VersionRestorePreview, ImportConflictItem, ImportConflictResult,
    STATUS_NAMES
)


def setup_test_dir():
    return tempfile.mkdtemp(prefix="patrol_ver_test_")


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


def test_version_publish():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-VER-001"

        tpl = create_template(
            state, name="派单模板", target_status="dispatched",
            handler="张工", remark="标准派单", source_type="daily"
        )

        ver = publish_version(state, tpl.template_id, "v1.0", published_by="管理员")
        assert ver.version_id
        assert ver.template_id == tpl.template_id
        assert ver.template_name == "派单模板"
        assert ver.version_name == "v1.0"
        assert ver.target_status == "dispatched"
        assert ver.handler == "张工"
        assert ver.remark == "标准派单"
        assert ver.source_type == "daily"
        assert ver.template_snapshot
        assert ver.template_snapshot["name"] == "派单模板"
        assert ver.published_at
        assert ver.published_by == "管理员"

        audit_logs = state.get_audit_logs(action="version_publish")
        assert len(audit_logs) == 1
        assert audit_logs[0].result == "success"
    finally:
        teardown_test_dir(test_dir)


def test_version_publish_duplicate_name():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        tpl = create_template(state, name="模板A", target_status="dispatched")

        publish_version(state, tpl.template_id, "v1.0")

        try:
            publish_version(state, tpl.template_id, "v1.0")
            assert False, "应该抛出异常"
        except WorkflowError as e:
            assert "已存在同名版本" in str(e)
    finally:
        teardown_test_dir(test_dir)


def test_version_list():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl1 = create_template(state, name="模板A", target_status="dispatched", handler="甲")
        tpl2 = create_template(state, name="模板B", target_status="closed", handler="乙")

        publish_version(state, tpl1.template_id, "v1.0")
        publish_version(state, tpl1.template_id, "v2.0")
        publish_version(state, tpl2.template_id, "v1.0")

        all_versions = list_versions(state)
        assert len(all_versions) == 3

        tpl1_versions = list_versions(state, template_id=tpl1.template_id)
        assert len(tpl1_versions) == 2

        tpl2_versions = list_versions(state, template_id=tpl2.template_id)
        assert len(tpl2_versions) == 1
    finally:
        teardown_test_dir(test_dir)


def test_version_compare():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="比较模板", target_status="dispatched",
            handler="张工", remark="版本1备注", source_type="daily"
        )

        ver1 = publish_version(state, tpl.template_id, "v1.0")

        update_template(state, tpl.template_id, handler="李工", remark="版本2备注", source_type="manual")
        ver2 = publish_version(state, tpl.template_id, "v2.0")

        result = compare_versions(state, ver1.version_id, ver2.version_id)

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


def test_version_compare_same():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(state, name="同版本模板", target_status="dispatched", handler="甲")
        ver1 = publish_version(state, tpl.template_id, "v1.0")
        ver2 = publish_version(state, tpl.template_id, "v1.0-copy")

        result = compare_versions(state, ver1.version_id, ver2.version_id)
        assert result.is_same
        assert len(result.diffs) == 0
    finally:
        teardown_test_dir(test_dir)


def test_version_restore_preview():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="恢复模板", target_status="dispatched",
            handler="张工", remark="原始备注", source_type="daily"
        )

        ver = publish_version(state, tpl.template_id, "v1.0")

        update_template(state, tpl.template_id, handler="李工", remark="新备注", source_type="manual")

        preview = preview_restore_version(state, ver.version_id)

        assert preview.version_name == "v1.0"
        assert preview.current_handler == "李工"
        assert preview.restore_handler == "张工"
        assert preview.current_remark == "新备注"
        assert preview.restore_remark == "原始备注"
        assert preview.current_source_type == "manual"
        assert preview.restore_source_type == "daily"
        assert len(preview.diffs) == 3
    finally:
        teardown_test_dir(test_dir)


def test_version_restore():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="恢复模板", target_status="dispatched",
            handler="张工", remark="原始备注"
        )

        ver = publish_version(state, tpl.template_id, "v1.0")

        update_template(state, tpl.template_id, handler="李工", remark="新备注")

        restored = restore_version(state, ver.version_id)

        assert restored.handler == "张工"
        assert restored.remark == "原始备注"
        assert restored.target_status == "dispatched"

        audit_logs = state.get_audit_logs(action="version_restore")
        assert len(audit_logs) == 1
        assert audit_logs[0].result == "success"
    finally:
        teardown_test_dir(test_dir)


def test_version_restore_deleted_template():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(state, name="将被删的模板", target_status="dispatched")
        ver = publish_version(state, tpl.template_id, "v1.0")

        ver_id = ver.version_id
        delete_template(state, tpl.template_id)

        try:
            restore_version(state, ver_id)
            assert False, "应该抛出异常"
        except WorkflowError as e:
            assert "版本不存在" in str(e) or "已删除" in str(e)

        tpl2 = create_template(state, name="另一个模板", target_status="dispatched", handler="甲")
        ver2 = publish_version(state, tpl2.template_id, "v1.0")

        delete_template(state, tpl2.template_id)

        try:
            restore_version(state, ver2.version_id)
            assert False, "应该抛出异常"
        except WorkflowError as e:
            assert "版本不存在" in str(e) or "已删除" in str(e)
    finally:
        teardown_test_dir(test_dir)


def test_version_draft_independence():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-VER-DRAFT"

        defect1 = create_sample_defect(state, "DEF-VER-001")
        defect2 = create_sample_defect(state, "DEF-VER-002", building="2号楼")

        tpl = create_template(
            state, name="独立模板", target_status="dispatched",
            handler="张工", remark="原始"
        )

        ver1 = publish_version(state, tpl.template_id, "v1.0")

        draft = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-VER-001", source_type="ids"
        )
        execute_draft(state, draft.draft_id)

        update_template(state, tpl.template_id, handler="李工", remark="修改后")
        ver2 = publish_version(state, tpl.template_id, "v2.0")

        restored = restore_version(state, ver1.version_id)
        assert restored.handler == "张工"

        draft_r = state.get_draft(draft.draft_id)
        assert draft_r.template_snapshot["handler"] == "张工"
        assert draft_r.execution.success_count == 1

        draft2 = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-VER-002", source_type="ids"
        )
        assert draft2.template_snapshot["handler"] == "张工"
    finally:
        teardown_test_dir(test_dir)


def test_version_rename_consistency():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(state, name="原名模板", target_status="dispatched")
        ver = publish_version(state, tpl.template_id, "v1.0")
        assert ver.template_name == "原名模板"

        update_template(state, tpl.template_id, name="新名模板")

        ver_reloaded = state.get_version(ver.version_id)
        assert ver_reloaded.template_name == "新名模板"

        versions = list_versions(state, template_id=tpl.template_id)
        assert versions[0].template_name == "新名模板"
    finally:
        teardown_test_dir(test_dir)


def test_version_delete_consistency():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(state, name="将被删模板", target_status="dispatched")
        publish_version(state, tpl.template_id, "v1.0")
        publish_version(state, tpl.template_id, "v2.0")

        assert len(list_versions(state, template_id=tpl.template_id)) == 2

        delete_template(state, tpl.template_id)

        assert len(list_versions(state, template_id=tpl.template_id)) == 0
    finally:
        teardown_test_dir(test_dir)


def test_version_restart_consistency():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="重启模板", target_status="dispatched",
            handler="张工", remark="重启测试"
        )
        ver = publish_version(state, tpl.template_id, "v1.0", published_by="管理员")

        state2 = PatrolState(data_dir=test_dir)

        versions = list_versions(state2, template_id=tpl.template_id)
        assert len(versions) == 1
        assert versions[0].version_name == "v1.0"
        assert versions[0].handler == "张工"
        assert versions[0].template_snapshot["handler"] == "张工"
        assert versions[0].published_by == "管理员"

        audit_logs = state2.get_audit_logs(action="version_publish")
        assert len(audit_logs) == 1
    finally:
        teardown_test_dir(test_dir)


def test_version_export_import():
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

        export_path = os.path.join(test_dir, "export_with_versions.json")
        count = export_with_versions(state, export_path)
        assert count == 2

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "templates" in data
        assert "versions" in data
        assert len(data["templates"]) == 2
        assert len(data["versions"]) == 3

        import_dir = setup_test_dir()
        try:
            state2 = PatrolState(data_dir=import_dir)

            precheck = precheck_import_conflicts(state2, export_path)
            assert not precheck.has_conflicts

            result = import_with_versions(state2, export_path, conflict_strategy="skip")
            assert len(result["imported_templates"]) == 2
            assert len(result["imported_versions"]) == 3
            assert len(result["errors"]) == 0

            versions = list_versions(state2)
            assert len(versions) == 3
        finally:
            teardown_test_dir(import_dir)
    finally:
        teardown_test_dir(test_dir)


def test_import_conflict_precheck():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="冲突模板", target_status="dispatched",
            handler="甲", source_type="daily"
        )
        publish_version(state, tpl.template_id, "v1.0")

        export_path = os.path.join(test_dir, "conflict_export.json")
        export_with_versions(state, export_path)

        precheck = precheck_import_conflicts(state, export_path)
        assert precheck.has_conflicts
        assert len(precheck.conflicts) >= 1
        assert any(c.template_name == "冲突模板" for c in precheck.conflicts)
    finally:
        teardown_test_dir(test_dir)


def test_import_conflict_strategies():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(
            state, name="策略模板", target_status="dispatched",
            handler="甲", source_type="daily"
        )
        publish_version(state, tpl.template_id, "v1.0")

        export_path = os.path.join(test_dir, "strategy_export.json")
        export_with_versions(state, export_path)

        skip_dir = setup_test_dir()
        try:
            state_skip = PatrolState(data_dir=skip_dir)
            result = import_with_versions(state_skip, export_path, conflict_strategy="skip")
            assert len(result["imported_templates"]) == 1
            assert len(result["imported_versions"]) == 1

            result2 = import_with_versions(state_skip, export_path, conflict_strategy="skip")
            assert len(result2["skipped"]) >= 1
        finally:
            teardown_test_dir(skip_dir)

        overwrite_dir = setup_test_dir()
        try:
            state_ow = PatrolState(data_dir=overwrite_dir)
            import_with_versions(state_ow, export_path, conflict_strategy="overwrite")

            result2 = import_with_versions(state_ow, export_path, conflict_strategy="overwrite")
            assert any("覆盖" in t for t in result2["imported_templates"])
        finally:
            teardown_test_dir(overwrite_dir)

        save_as_dir = setup_test_dir()
        try:
            state_sa = PatrolState(data_dir=save_as_dir)
            import_with_versions(state_sa, export_path, conflict_strategy="save_as")

            result2 = import_with_versions(state_sa, export_path, conflict_strategy="save_as")
            assert len(result2["saved_as"]) >= 1
        finally:
            teardown_test_dir(save_as_dir)

        abort_dir = setup_test_dir()
        try:
            state_ab = PatrolState(data_dir=abort_dir)
            import_with_versions(state_ab, export_path, conflict_strategy="skip")

            result2 = import_with_versions(state_ab, export_path, conflict_strategy="abort")
            assert len(result2["errors"]) > 0
            assert any("冲突中止" in e for e in result2["errors"])
        finally:
            teardown_test_dir(abort_dir)
    finally:
        teardown_test_dir(test_dir)


def test_version_audit_logging():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl = create_template(state, name="审计模板", target_status="dispatched", handler="甲")
        ver = publish_version(state, tpl.template_id, "v1.0")

        update_template(state, tpl.template_id, handler="乙")
        restore_version(state, ver.version_id)

        audit_logs = state.get_audit_logs()
        publish_logs = [l for l in audit_logs if l.action == "version_publish"]
        restore_logs = [l for l in audit_logs if l.action == "version_restore"]
        assert len(publish_logs) == 1
        assert len(restore_logs) == 1
        assert restore_logs[0].result == "success"
    finally:
        teardown_test_dir(test_dir)


def test_version_export_after_rename_delete():
    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)

        tpl1 = create_template(state, name="改名模板", target_status="dispatched", handler="甲")
        tpl2 = create_template(state, name="删除模板", target_status="closed", handler="乙")

        publish_version(state, tpl1.template_id, "v1.0")
        publish_version(state, tpl2.template_id, "v1.0")

        update_template(state, tpl1.template_id, name="改名后模板")
        delete_template(state, tpl2.template_id)

        export_path = os.path.join(test_dir, "rename_delete_export.json")
        count = export_with_versions(state, export_path)
        assert count == 1

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["templates"]) == 1
        assert len(data["versions"]) == 1
        ver_list = list(data["versions"].values())
        assert ver_list[0]["template_name"] == "改名后模板"
    finally:
        teardown_test_dir(test_dir)


if __name__ == "__main__":
    import traceback

    tests = [
        test_version_publish,
        test_version_publish_duplicate_name,
        test_version_list,
        test_version_compare,
        test_version_compare_same,
        test_version_restore_preview,
        test_version_restore,
        test_version_restore_deleted_template,
        test_version_draft_independence,
        test_version_rename_consistency,
        test_version_delete_consistency,
        test_version_restart_consistency,
        test_version_export_import,
        test_import_conflict_precheck,
        test_import_conflict_strategies,
        test_version_audit_logging,
        test_version_export_after_rename_delete,
    ]

    print("开始版本库模块测试")
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
