"""端到端测试：草稿执行链路的模板溯源完整性"""
import os
import sys
import shutil
import json
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from patrol_cli.storage import PatrolState
from patrol_cli.workflow import (
    create_template, update_template, delete_template,
    create_draft, create_draft_from_template,
    execute_draft, WorkflowError,
    snapshot_health_check, snapshot_patch
)
from patrol_cli.exporter import (
    export_draft_csv, export_draft_list_csv,
    _resolve_template_fields, _classify_snapshot,
    export_health_check_csv, export_health_check_json
)
from patrol_cli.cli import _resolve_draft_template_info, _classify_snapshot as cli_classify_snapshot


def setup_test_dir():
    test_dir = tempfile.mkdtemp(prefix="patrol_test_")
    return test_dir


def teardown_test_dir(test_dir):
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)


def create_sample_defect(state, defect_id, **kwargs):
    from patrol_cli.models import DefectRecord, SourceRow
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


@pytest.fixture
def template_draft_env():
    test_dir = tempfile.mkdtemp(prefix="patrol_test_")
    state = PatrolState(data_dir=test_dir)
    state.batch_id = "BATCH-TEST-001"

    defect1 = create_sample_defect(state, "DEF-TEST-001")
    defect2 = create_sample_defect(state, "DEF-TEST-002", building="2号楼", device_id="FA-001")

    template = create_template(
        state,
        name="派单模板-A",
        target_status="dispatched",
        handler="张工",
        remark="标准派单处理",
        description="用于日常巡检缺陷派单"
    )

    draft = create_draft_from_template(
        state,
        template_id=template.template_id,
        source="DEF-TEST-001,DEF-TEST-002",
        source_type="ids",
        name="草稿-派单测试"
    )

    execute_draft(state, draft.draft_id)

    yield {
        "test_dir": test_dir,
        "state": state,
        "template": template,
        "draft": draft,
    }

    teardown_test_dir(test_dir)


def test_1_template_draft_execute():
    """测试1: 模板建草稿并执行，执行回显要包含模板信息，快照完整度=complete"""
    print("\n" + "=" * 60)
    print("测试1: 模板建草稿并执行")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-001"

        defect1 = create_sample_defect(state, "DEF-TEST-001")
        defect2 = create_sample_defect(state, "DEF-TEST-002", building="2号楼", device_id="FA-001")

        template = create_template(
            state,
            name="派单模板-A",
            target_status="dispatched",
            handler="张工",
            remark="标准派单处理",
            description="用于日常巡检缺陷派单"
        )
        print(f"  创建模板: {template.name} ({template.template_id})")

        draft = create_draft_from_template(
            state,
            template_id=template.template_id,
            source="DEF-TEST-001,DEF-TEST-002",
            source_type="ids",
            name="草稿-派单测试"
        )
        print(f"  从模板创建草稿: {draft.name} ({draft.draft_id})")
        assert draft.template_id == template.template_id
        assert draft.template_snapshot
        assert draft.template_snapshot["name"] == "派单模板-A"

        result = execute_draft(state, draft.draft_id)
        assert result.success_count == 2

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  模板信息解析: has_template={tpl_info['has_template']}, "
              f"name={tpl_info['template_name']}, snapshot_status={tpl_info['snapshot_status']}")
        assert tpl_info["has_template"] is True
        assert tpl_info["template_name"] == "派单模板-A"
        assert tpl_info["has_snapshot"] is True
        assert tpl_info["template_exists"] is True
        assert tpl_info["snapshot_status"] == "complete"
        assert tpl_info["missing_fields"] == []
        assert tpl_info["snapshot_target_status"] == "已派单"
        assert tpl_info["snapshot_handler"] == "张工"

        tpl_export = _resolve_template_fields(state, draft)
        assert tpl_export["snapshot_status"] == "complete"
        assert tpl_export["snapshot_completeness_label"] == "完整快照"
        assert "完整快照" in tpl_export["template_note"]
        assert "不受后续变更影响" in tpl_export["template_note"]

        print("  [OK] 测试1通过")
    finally:
        pass


def test_2_modify_template_and_review_old(template_draft_env):
    """测试2: 修改模板后回看旧记录，旧草稿应仍显示原始模板快照（不反查新模板）"""
    print("\n" + "=" * 60)
    print("测试2: 修改模板后回看旧记录")
    print("=" * 60)

    try:
        state = template_draft_env["state"]
        drafts = state.list_drafts()
        old_draft = drafts[0]
        old_template_id = old_draft.template_id
        print(f"  修改前旧草稿显示模板: {old_draft.template_snapshot['name']}")

        updated_template = update_template(
            state,
            template_id=old_template_id,
            name="派单模板-A-已改名",
            handler="李工"
        )
        print(f"  模板已改名: {updated_template.name}")

        tpl_info = _resolve_draft_template_info(state, old_draft)
        print(f"  旧草稿解析出的模板名: {tpl_info['template_name']}")
        assert tpl_info["template_name"] == "派单模板-A", "旧草稿应显示快照中的原始名称，而不是新名称"
        assert tpl_info["template_exists"] is True
        assert tpl_info["snapshot_status"] == "complete"
        assert tpl_info["snapshot_handler"] == "张工", "旧草稿处理人快照应为张工，不是李工"

        tpl_export = _resolve_template_fields(state, old_draft)
        assert tpl_export["template_name"] == "派单模板-A"

        print("  [OK] 测试2通过")
    finally:
        pass


def test_3_restart_and_query(template_draft_env):
    """测试3: 重启（重新加载）后查询，模板溯源信息保持一致"""
    print("\n" + "=" * 60)
    print("测试3: 重启后查询")
    print("=" * 60)

    try:
        test_dir = template_draft_env["test_dir"]
        state = template_draft_env["state"]
        drafts = state.list_drafts()
        old_draft = drafts[0]
        update_template(
            state,
            template_id=old_draft.template_id,
            name="派单模板-A-已改名",
            handler="李工"
        )

        state2 = PatrolState(data_dir=test_dir)
        drafts = state2.list_drafts()
        assert len(drafts) == 1

        draft = drafts[0]
        tpl_info = _resolve_draft_template_info(state2, draft)
        print(f"  重启后解析模板: name={tpl_info['template_name']}, "
              f"snapshot_status={tpl_info['snapshot_status']}")
        assert tpl_info["template_name"] == "派单模板-A"
        assert tpl_info["has_snapshot"] is True
        assert tpl_info["snapshot_status"] == "complete"

        templates = state2.list_templates()
        assert len(templates) == 1
        assert templates[0].name == "派单模板-A-已改名"

        print("  [OK] 测试3通过")
    finally:
        pass


def test_4_export_consistency(template_draft_env):
    """测试4: 导出核对，CSV 中模板字段应与快照一致"""
    print("\n" + "=" * 60)
    print("测试4: 导出核对")
    print("=" * 60)

    try:
        test_dir = template_draft_env["test_dir"]
        state = template_draft_env["state"]
        drafts = state.list_drafts()
        old_draft = drafts[0]
        update_template(
            state,
            template_id=old_draft.template_id,
            name="派单模板-A-已改名",
            handler="李工"
        )
        draft = drafts[0]

        csv_path = os.path.join(test_dir, "draft_export.csv")
        count = export_draft_csv(state, draft.draft_id, csv_path)
        print(f"  导出 draft-csv: {count} 行 -> {csv_path}")

        list_csv_path = os.path.join(test_dir, "draft_list_export.csv")
        count2 = export_draft_list_csv(state, list_csv_path)
        print(f"  导出 draft-list-csv: {count2} 行 -> {list_csv_path}")

        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            print(f"  CSV 列名: {reader.fieldnames}")
            assert "模板溯源备注" in reader.fieldnames
            assert "快照完整度" in reader.fieldnames
            assert rows[0]["模板名称"] == "派单模板-A"
            assert rows[0]["模板ID"] == draft.template_id
            assert rows[0]["快照完整度"] == "完整快照"
            assert "完整快照" in rows[0]["模板溯源备注"]
            assert "不受后续变更影响" in rows[0]["模板溯源备注"]
            print(f"  单草稿导出 - 模板名: {rows[0]['模板名称']}, "
                  f"完整度: {rows[0]['快照完整度']}, 备注: {rows[0]['模板溯源备注']}")

        with open(list_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "模板溯源备注" in reader.fieldnames
            assert "快照完整度" in reader.fieldnames
            assert rows[0]["模板名称"] == "派单模板-A"
            assert rows[0]["快照完整度"] == "完整快照"
            print(f"  草稿列表导出 - 模板名: {rows[0]['模板名称']}, "
                  f"完整度: {rows[0]['快照完整度']}, 备注: {rows[0]['模板溯源备注']}")

        print("  [OK] 测试4通过")
    finally:
        pass


def test_5_delete_template_and_trace(template_draft_env):
    """测试5: 删除模板后，旧草稿仍能通过快照溯源"""
    print("\n" + "=" * 60)
    print("测试5: 删除模板后溯源")
    print("=" * 60)

    try:
        test_dir = template_draft_env["test_dir"]
        state = template_draft_env["state"]
        drafts = state.list_drafts()
        draft = drafts[0]
        template_id = draft.template_id

        delete_template(state, template_id)
        print(f"  已删除模板: {template_id}")

        assert state.get_template(template_id) is None

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  删除后解析: name={tpl_info['template_name']}, "
              f"exists={tpl_info['template_exists']}, snapshot_status={tpl_info['snapshot_status']}, "
              f"note={tpl_info['note']}")
        assert tpl_info["template_name"] == "派单模板-A"
        assert tpl_info["template_exists"] is False
        assert tpl_info["snapshot_status"] == "complete"
        assert "已删除" in tpl_info["note"]

        csv_path = os.path.join(test_dir, "draft_after_delete.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert rows[0]["模板名称"] == "派单模板-A"
            assert rows[0]["快照完整度"] == "完整快照"
            assert "已删除" in rows[0]["模板溯源备注"]
            assert "完整快照" in rows[0]["模板溯源备注"]
            print(f"  删除后CSV导出 - 模板名: {rows[0]['模板名称']}, "
                  f"完整度: {rows[0]['快照完整度']}, 备注: {rows[0]['模板溯源备注']}")

        print("  [OK] 测试5通过")
    finally:
        pass


def test_6_non_template_draft():
    """测试6: 非模板草稿执行链路，应明确显示未使用模板"""
    print("\n" + "=" * 60)
    print("测试6: 非模板草稿执行链路")
    print("=" * 60)

    test_dir2 = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir2)
        state.batch_id = "BATCH-TEST-002"

        defect1 = create_sample_defect(state, "DEF-NO-TPL-001")
        defect2 = create_sample_defect(state, "DEF-NO-TPL-002", building="2号楼")

        draft = create_draft(
            state,
            source="DEF-NO-TPL-001,DEF-NO-TPL-002",
            source_type="ids",
            target_status="closed",
            name="手动创建草稿-无模板",
            handler="王工",
            remark="手动关闭"
        )
        assert draft.template_id == ""
        assert not draft.template_snapshot

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["has_template"] is False
        assert tpl_info["template_id"] == ""
        assert tpl_info["snapshot_status"] == "missing"

        result = execute_draft(state, draft.draft_id)
        assert result.success_count == 2

        tpl_info_after = _resolve_draft_template_info(state, draft)
        assert tpl_info_after["has_template"] is False

        csv_path = os.path.join(test_dir2, "non_template_draft.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert rows[0]["模板名称"] == "未使用模板"
            assert rows[0]["模板ID"] == ""
            assert rows[0]["模板溯源备注"] == "手动创建"
            assert rows[0]["快照完整度"] == "非模板草稿"

        list_csv = os.path.join(test_dir2, "non_template_list.csv")
        export_draft_list_csv(state, list_csv)
        with open(list_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert rows[0]["模板名称"] == "未使用模板"
            assert rows[0]["模板溯源备注"] == "手动创建"
            assert rows[0]["快照完整度"] == "非模板草稿"

        print("  [OK] 测试6通过")
    finally:
        teardown_test_dir(test_dir2)


def test_7_old_data_missing_snapshot():
    """测试7: 老数据有 template_id 但无 template_snapshot 的情况"""
    print("\n" + "=" * 60)
    print("测试7: 老数据无模板快照")
    print("=" * 60)

    test_dir3 = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir3)
        state.batch_id = "BATCH-TEST-003"

        defect1 = create_sample_defect(state, "DEF-OLD-001")

        template = create_template(
            state,
            name="老版本模板",
            target_status="false_positive",
            handler="赵工"
        )

        draft = create_draft(
            state,
            source="DEF-OLD-001",
            source_type="ids",
            target_status="false_positive",
            name="老数据模拟草稿"
        )
        draft.template_id = template.template_id
        draft.template_snapshot = {}
        state.update_draft(draft)
        state.save_drafts()

        print(f"  模拟老草稿: template_id={draft.template_id}, snapshot=空")

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  解析结果: name={tpl_info['template_name']}, "
              f"snapshot_status={tpl_info['snapshot_status']}, note={tpl_info['note']}")
        assert tpl_info["snapshot_status"] == "missing"
        assert tpl_info["has_snapshot"] is False
        assert "老数据" in tpl_info["note"]
        assert tpl_info["template_name"] == template.template_id, "无快照时应显示模板ID而非当前模板名"

        tpl_export = _resolve_template_fields(state, draft)
        assert tpl_export["snapshot_status"] == "missing"
        assert tpl_export["snapshot_completeness_label"] == "老数据无快照"
        assert "老数据" in tpl_export["template_note"]

        delete_template(state, template.template_id)
        state2 = PatrolState(data_dir=test_dir3)
        draft_reloaded = state2.get_draft(draft.draft_id)
        tpl_info2 = _resolve_draft_template_info(state2, draft_reloaded)
        print(f"  删除模板+重启后: name={tpl_info2['template_name']}, note={tpl_info2['note']}")
        assert "老数据，模板已删除且无快照" in tpl_info2["note"]
        assert tpl_info2["snapshot_status"] == "missing"

        execute_draft(state2, draft.draft_id)
        csv_path = os.path.join(test_dir3, "old_data.csv")
        export_draft_csv(state2, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            print(f"  老数据CSV: name='{rows[0]['模板名称']}', "
                  f"完整度='{rows[0]['快照完整度']}', note='{rows[0]['模板溯源备注']}'")
            assert "老数据" in rows[0]["模板溯源备注"]
            assert rows[0]["快照完整度"] == "老数据无快照"

        print("  [OK] 测试7通过")
    finally:
        teardown_test_dir(test_dir3)


def test_8_incomplete_snapshot_detail_list_export():
    """测试8: 残缺快照——snapshot 存在但缺关键字段，详情/列表/导出都要提示"""
    print("\n" + "=" * 60)
    print("测试8: 残缺快照详情/列表/导出")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-008"

        defect1 = create_sample_defect(state, "DEF-INCOMPLETE-001")

        template = create_template(
            state,
            name="残缺测试模板",
            target_status="dispatched",
            handler="钱工",
            remark="备注内容"
        )

        draft = create_draft_from_template(
            state,
            template_id=template.template_id,
            source="DEF-INCOMPLETE-001",
            source_type="ids",
            name="草稿-残缺快照模拟"
        )

        draft.template_snapshot = {"name": "残缺测试模板"}
        state.update_draft(draft)
        state.save_drafts()

        print(f"  模拟残缺快照: 只保留 name，缺 target_status 和 handler")

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  解析: snapshot_status={tpl_info['snapshot_status']}, "
              f"missing_fields={tpl_info['missing_fields']}, note={tpl_info['note']}")
        assert tpl_info["snapshot_status"] == "incomplete"
        assert "目标状态" in tpl_info["missing_fields"]
        assert "处理人" in tpl_info["missing_fields"]
        assert tpl_info["has_snapshot"] is True
        assert tpl_info["template_name"] == "残缺测试模板"
        assert "残缺快照" in tpl_info["note"]
        assert "缺目标状态,处理人" in tpl_info["note"]
        assert tpl_info["snapshot_target_status"] == "(缺失)"
        assert tpl_info["snapshot_handler"] == "(缺失)"

        tpl_export = _resolve_template_fields(state, draft)
        assert tpl_export["snapshot_status"] == "incomplete"
        assert "缺:目标状态,处理人" in tpl_export["snapshot_completeness_label"]
        assert "残缺快照" in tpl_export["template_note"]
        assert "缺目标状态,处理人" in tpl_export["template_note"]

        execute_draft(state, draft.draft_id)

        csv_path = os.path.join(test_dir, "incomplete_draft.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            print(f"  残缺CSV: 完整度='{rows[0]['快照完整度']}', 备注='{rows[0]['模板溯源备注']}'")
            assert "字段残缺" in rows[0]["快照完整度"]
            assert "缺:目标状态" in rows[0]["快照完整度"]
            assert "残缺快照" in rows[0]["模板溯源备注"]

        list_csv_path = os.path.join(test_dir, "incomplete_list.csv")
        export_draft_list_csv(state, list_csv_path)
        with open(list_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "字段残缺" in rows[0]["快照完整度"]
            assert "残缺快照" in rows[0]["模板溯源备注"]

        print("  [OK] 测试8通过")
    finally:
        teardown_test_dir(test_dir)


def test_9_incomplete_snapshot_with_deleted_template():
    """测试9: 残缺快照+模板已删除"""
    print("\n" + "=" * 60)
    print("测试9: 残缺快照+模板已删除")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-009"

        defect1 = create_sample_defect(state, "DEF-DEL-INCOMPLETE-001")

        template = create_template(
            state,
            name="将被删除的残缺模板",
            target_status="false_positive",
            handler="孙工"
        )

        draft = create_draft_from_template(
            state,
            template_id=template.template_id,
            source="DEF-DEL-INCOMPLETE-001",
            source_type="ids"
        )

        draft.template_snapshot = {"name": "将被删除的残缺模板"}
        state.update_draft(draft)
        state.save_drafts()

        delete_template(state, template.template_id)

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  解析: snapshot_status={tpl_info['snapshot_status']}, "
              f"exists={tpl_info['template_exists']}, note={tpl_info['note']}")
        assert tpl_info["snapshot_status"] == "incomplete"
        assert tpl_info["template_exists"] is False
        assert "残缺快照" in tpl_info["note"]
        assert "模板已删除" in tpl_info["note"]

        tpl_export = _resolve_template_fields(state, draft)
        assert "残缺快照" in tpl_export["template_note"]
        assert "模板已删除" in tpl_export["template_note"]

        execute_draft(state, draft.draft_id)
        csv_path = os.path.join(test_dir, "del_incomplete.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "残缺快照" in rows[0]["模板溯源备注"]
            assert "模板已删除" in rows[0]["模板溯源备注"]

        print("  [OK] 测试9通过")
    finally:
        teardown_test_dir(test_dir)


def test_10_template_delete_then_history_review():
    """测试10: 模板删除后历史回看——旧草稿仍按快照展示，不拿新模板反推"""
    print("\n" + "=" * 60)
    print("测试10: 模板删除后历史回看")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-010"

        defect1 = create_sample_defect(state, "DEF-DEL-HIST-001")

        template = create_template(
            state,
            name="历史模板-B",
            target_status="dispatched",
            handler="周工",
            remark="历史备注"
        )

        draft = create_draft_from_template(
            state,
            template_id=template.template_id,
            source="DEF-DEL-HIST-001",
            source_type="ids"
        )
        execute_draft(state, draft.draft_id)

        tpl_info_before = _resolve_draft_template_info(state, draft)
        name_before = tpl_info_before["template_name"]
        handler_before = tpl_info_before["snapshot_handler"]

        delete_template(state, template.template_id)

        new_template = create_template(
            state,
            name="新模板-C",
            target_status="dispatched",
            handler="吴工",
            remark="新备注"
        )

        tpl_info_after = _resolve_draft_template_info(state, draft)
        print(f"  删除后解析: name={tpl_info_after['template_name']}, "
              f"handler={tpl_info_after['snapshot_handler']}, "
              f"exists={tpl_info_after['template_exists']}")
        assert tpl_info_after["template_name"] == name_before, "仍应显示快照中的原始名称"
        assert tpl_info_after["snapshot_handler"] == handler_before, "仍应显示快照中的原始处理人"
        assert tpl_info_after["snapshot_status"] == "complete"
        assert tpl_info_after["template_exists"] is False

        print("  [OK] 测试10通过")
    finally:
        teardown_test_dir(test_dir)


def test_11_restart_consistency_for_all_tiers():
    """测试11: 重启后所有三档快照状态查询一致"""
    print("\n" + "=" * 60)
    print("测试11: 重启后三档快照查询一致性")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-011"

        defect1 = create_sample_defect(state, "DEF-RESTART-001")
        defect2 = create_sample_defect(state, "DEF-RESTART-002", building="2号楼")
        defect3 = create_sample_defect(state, "DEF-RESTART-003", building="3号楼")

        tpl_complete = create_template(
            state, name="完整模板", target_status="dispatched", handler="甲"
        )
        tpl_incomplete = create_template(
            state, name="残缺模板", target_status="closed", handler="乙"
        )
        tpl_missing = create_template(
            state, name="缺失模板", target_status="false_positive", handler="丙"
        )

        draft_complete = create_draft_from_template(
            state, template_id=tpl_complete.template_id,
            source="DEF-RESTART-001", source_type="ids"
        )
        execute_draft(state, draft_complete.draft_id)

        draft_incomplete = create_draft_from_template(
            state, template_id=tpl_incomplete.template_id,
            source="DEF-RESTART-002", source_type="ids"
        )
        draft_incomplete.template_snapshot = {"name": "残缺模板"}
        state.update_draft(draft_incomplete)
        state.save_drafts()
        execute_draft(state, draft_incomplete.draft_id)

        draft_missing = create_draft(
            state, source="DEF-RESTART-003", source_type="ids",
            target_status="false_positive", name="老数据草稿"
        )
        draft_missing.template_id = tpl_missing.template_id
        draft_missing.template_snapshot = {}
        state.update_draft(draft_missing)
        state.save_drafts()
        execute_draft(state, draft_missing.draft_id)

        info_before_complete = _resolve_draft_template_info(state, draft_complete)
        info_before_incomplete = _resolve_draft_template_info(state, draft_incomplete)
        info_before_missing = _resolve_draft_template_info(state, draft_missing)

        export_before_complete = _resolve_template_fields(state, draft_complete)
        export_before_incomplete = _resolve_template_fields(state, draft_incomplete)
        export_before_missing = _resolve_template_fields(state, draft_missing)

        csv_complete_path = os.path.join(test_dir, "restart_complete.csv")
        csv_incomplete_path = os.path.join(test_dir, "restart_incomplete.csv")
        csv_missing_path = os.path.join(test_dir, "restart_missing.csv")
        export_draft_csv(state, draft_complete.draft_id, csv_complete_path)
        export_draft_csv(state, draft_incomplete.draft_id, csv_incomplete_path)
        export_draft_csv(state, draft_missing.draft_id, csv_missing_path)

        state2 = PatrolState(data_dir=test_dir)

        draft_complete_r = state2.get_draft(draft_complete.draft_id)
        draft_incomplete_r = state2.get_draft(draft_incomplete.draft_id)
        draft_missing_r = state2.get_draft(draft_missing.draft_id)

        info_after_complete = _resolve_draft_template_info(state2, draft_complete_r)
        info_after_incomplete = _resolve_draft_template_info(state2, draft_incomplete_r)
        info_after_missing = _resolve_draft_template_info(state2, draft_missing_r)

        export_after_complete = _resolve_template_fields(state2, draft_complete_r)
        export_after_incomplete = _resolve_template_fields(state2, draft_incomplete_r)
        export_after_missing = _resolve_template_fields(state2, draft_missing_r)

        assert info_after_complete["snapshot_status"] == "complete"
        assert info_after_complete["template_name"] == info_before_complete["template_name"]
        assert info_after_complete["snapshot_handler"] == info_before_complete["snapshot_handler"]

        assert info_after_incomplete["snapshot_status"] == "incomplete"
        assert info_after_incomplete["missing_fields"] == info_before_incomplete["missing_fields"]
        assert info_after_incomplete["template_name"] == info_before_incomplete["template_name"]

        assert info_after_missing["snapshot_status"] == "missing"
        assert info_after_missing["note"] == info_before_missing["note"]

        assert export_after_complete["template_note"] == export_before_complete["template_note"]
        assert export_after_incomplete["template_note"] == export_before_incomplete["template_note"]
        assert export_after_missing["template_note"] == export_before_missing["template_note"]

        csv_complete_path2 = os.path.join(test_dir, "restart_complete_v2.csv")
        csv_incomplete_path2 = os.path.join(test_dir, "restart_incomplete_v2.csv")
        csv_missing_path2 = os.path.join(test_dir, "restart_missing_v2.csv")
        export_draft_csv(state2, draft_complete_r.draft_id, csv_complete_path2)
        export_draft_csv(state2, draft_incomplete_r.draft_id, csv_incomplete_path2)
        export_draft_csv(state2, draft_missing_r.draft_id, csv_missing_path2)

        import csv as csv_mod
        for path1, path2 in [
            (csv_complete_path, csv_complete_path2),
            (csv_incomplete_path, csv_incomplete_path2),
            (csv_missing_path, csv_missing_path2),
        ]:
            with open(path1, "r", encoding="utf-8-sig") as f1, open(path2, "r", encoding="utf-8-sig") as f2:
                rows1 = list(csv_mod.DictReader(f1))
                rows2 = list(csv_mod.DictReader(f2))
                assert rows1[0]["模板名称"] == rows2[0]["模板名称"]
                assert rows1[0]["快照完整度"] == rows2[0]["快照完整度"]
                assert rows1[0]["模板溯源备注"] == rows2[0]["模板溯源备注"]

        print("  [OK] 测试11通过")
    finally:
        teardown_test_dir(test_dir)


def test_12_classify_snapshot_unit():
    """测试12: _classify_snapshot 单元测试"""
    print("\n" + "=" * 60)
    print("测试12: _classify_snapshot 单元测试")
    print("=" * 60)

    ss, mf = _classify_snapshot({})
    assert ss == "missing"
    assert len(mf) == 3

    ss, mf = _classify_snapshot(None)
    assert ss == "missing"

    ss, mf = _classify_snapshot({"name": "A", "target_status": "closed", "handler": "X"})
    assert ss == "complete"
    assert mf == []

    ss, mf = _classify_snapshot({"name": "A", "target_status": "closed"})
    assert ss == "incomplete"
    assert "处理人" in mf

    ss, mf = _classify_snapshot({"name": "A"})
    assert ss == "incomplete"
    assert "目标状态" in mf
    assert "处理人" in mf

    ss, mf = _classify_snapshot({"target_status": "closed"})
    assert ss == "incomplete"
    assert "模板名称" in mf
    assert "处理人" in mf

    assert cli_classify_snapshot({}) == ("missing", ["模板名称", "目标状态", "处理人"])
    assert cli_classify_snapshot({"name": "A", "target_status": "X", "handler": "Y"}) == ("complete", [])

    print("  [OK] 测试12通过")


def test_13_snapshot_health_check():
    """测试13: 快照体检——完整/残缺/缺失三档分类，不改任何状态"""
    print("\n" + "=" * 60)
    print("测试13: 快照体检")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-013"

        defect1 = create_sample_defect(state, "DEF-HC-001")
        defect2 = create_sample_defect(state, "DEF-HC-002", building="2号楼")
        defect3 = create_sample_defect(state, "DEF-HC-003", building="3号楼")
        defect4 = create_sample_defect(state, "DEF-HC-004", building="4号楼")

        tpl_complete = create_template(
            state, name="完整模板-HC", target_status="dispatched", handler="甲"
        )
        tpl_missing = create_template(
            state, name="缺失模板-HC", target_status="closed", handler="乙"
        )

        draft_complete = create_draft_from_template(
            state, template_id=tpl_complete.template_id,
            source="DEF-HC-001", source_type="ids"
        )
        execute_draft(state, draft_complete.draft_id)

        draft_incomplete = create_draft_from_template(
            state, template_id=tpl_complete.template_id,
            source="DEF-HC-002", source_type="ids"
        )
        draft_incomplete.template_snapshot = {"name": "完整模板-HC"}
        state.update_draft(draft_incomplete)
        state.save_drafts()

        draft_missing = create_draft(
            state, source="DEF-HC-003", source_type="ids",
            target_status="closed", name="老数据草稿"
        )
        draft_missing.template_id = tpl_missing.template_id
        draft_missing.template_snapshot = {}
        state.update_draft(draft_missing)
        state.save_drafts()

        draft_no_tpl = create_draft(
            state, source="DEF-HC-004", source_type="ids",
            target_status="false_positive", name="非模板草稿"
        )

        results = snapshot_health_check(state)
        id_to_r = {r["draft_id"]: r for r in results}

        r_complete = id_to_r[draft_complete.draft_id]
        print(f"  完整快照草稿: status={r_complete['snapshot_status']}, "
              f"sealed={r_complete['sealed']}, can_patch={r_complete['can_patch']}")
        assert r_complete["snapshot_status"] == "complete"
        assert r_complete["sealed"] is False
        assert r_complete["can_patch"] is True
        assert r_complete["missing_fields"] == []

        r_incomplete = id_to_r[draft_incomplete.draft_id]
        print(f"  残缺快照草稿: status={r_incomplete['snapshot_status']}, "
              f"missing={r_incomplete['missing_fields']}, can_patch={r_incomplete['can_patch']}")
        assert r_incomplete["snapshot_status"] == "incomplete"
        assert "目标状态" in r_incomplete["missing_fields"]
        assert "处理人" in r_incomplete["missing_fields"]
        assert r_incomplete["can_patch"] is True

        r_missing = id_to_r[draft_missing.draft_id]
        print(f"  老数据草稿: status={r_missing['snapshot_status']}, "
              f"can_patch={r_missing['can_patch']}, risk={r_missing['risk_reason']}")
        assert r_missing["snapshot_status"] == "missing"
        assert r_missing["can_patch"] is True

        r_no_tpl = id_to_r[draft_no_tpl.draft_id]
        print(f"  非模板草稿: can_patch={r_no_tpl['can_patch']}, reason={r_no_tpl['cannot_patch_reason']}")
        assert r_no_tpl["can_patch"] is False
        assert "非模板草稿" in r_no_tpl["cannot_patch_reason"]

        before_statuses = {d.draft_id: d.status for d in state.drafts.values()}
        before_sealed = {d.draft_id: d.snapshot_sealed_at for d in state.drafts.values()}

        reloaded = PatrolState(data_dir=test_dir)
        for draft_after in reloaded.drafts.values():
            assert draft_after.status == before_statuses[draft_after.draft_id], "体检不应改变草稿状态"
            assert draft_after.snapshot_sealed_at == before_sealed[draft_after.draft_id], "体检不应改变封存状态"

        print("  [OK] 测试13通过")
    finally:
        teardown_test_dir(test_dir)


def test_14_snapshot_patch_and_seal():
    """测试14: 补档——封存模板快照为只读副本，补档后不可再补"""
    print("\n" + "=" * 60)
    print("测试14: 补档封存")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-014"

        defect1 = create_sample_defect(state, "DEF-PATCH-001")
        defect2 = create_sample_defect(state, "DEF-PATCH-002", building="2号楼")

        tpl = create_template(
            state, name="补档模板", target_status="dispatched", handler="丙"
        )

        draft_complete = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-PATCH-001", source_type="ids"
        )

        draft_missing = create_draft(
            state, source="DEF-PATCH-002", source_type="ids",
            target_status="dispatched", name="老数据草稿-待补档"
        )
        draft_missing.template_id = tpl.template_id
        draft_missing.template_snapshot = {}
        state.update_draft(draft_missing)
        state.save_drafts()

        result = snapshot_patch(state, [draft_complete.draft_id, draft_missing.draft_id])
        print(f"  补档结果: patched={result['patched']}, errors={result['errors']}, "
              f"audit_id={result['audit_id']}")
        assert len(result["patched"]) == 2
        assert result["errors"] == []
        assert result["audit_id"]

        draft_complete_r = state.get_draft(draft_complete.draft_id)
        draft_missing_r = state.get_draft(draft_missing.draft_id)
        assert draft_complete_r.snapshot_sealed_at != ""
        assert draft_missing_r.snapshot_sealed_at != ""
        assert draft_missing_r.template_snapshot["name"] == "补档模板"
        assert draft_missing_r.template_snapshot["handler"] == "丙"
        assert draft_missing_r.template_snapshot["target_status"] == "dispatched"

        health = snapshot_health_check(state)
        id_to_h = {h["draft_id"]: h for h in health}
        assert id_to_h[draft_complete.draft_id]["sealed"] is True
        assert id_to_h[draft_missing.draft_id]["sealed"] is True
        assert id_to_h[draft_complete.draft_id]["can_patch"] is False
        assert "已封存" in id_to_h[draft_complete.draft_id]["cannot_patch_reason"]
        assert id_to_h[draft_missing.draft_id]["can_patch"] is False

        result2 = snapshot_patch(state, [draft_complete.draft_id])
        assert len(result2["patched"]) == 0
        assert len(result2["errors"]) > 0
        assert result2["audit_id"]

        audit_logs = state.get_audit_logs(action="snapshot_patch")
        assert len(audit_logs) == 2
        assert audit_logs[0].result == "failed"

        print("  [OK] 测试14通过")
    finally:
        teardown_test_dir(test_dir)


def test_15_patch_batch_conflict():
    """测试15: 批次校验——来源冲突/重复/字段对不上整批失败并记审计日志"""
    print("\n" + "=" * 60)
    print("测试15: 批次校验冲突")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-015"

        defect1 = create_sample_defect(state, "DEF-CONFLICT-001")
        defect2 = create_sample_defect(state, "DEF-CONFLICT-002", building="2号楼")

        tpl = create_template(
            state, name="冲突模板", target_status="dispatched", handler="丁"
        )

        draft_incomplete = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-CONFLICT-001", source_type="ids"
        )
        draft_incomplete.template_snapshot = {"name": "冲突模板", "target_status": "closed"}
        state.update_draft(draft_incomplete)
        state.save_drafts()

        draft_no_tpl = create_draft(
            state, source="DEF-CONFLICT-002", source_type="ids",
            target_status="false_positive", name="非模板草稿"
        )

        result = snapshot_patch(state, [draft_no_tpl.draft_id])
        assert len(result["patched"]) == 0
        assert len(result["errors"]) > 0
        assert any("非模板草稿" in e for e in result["errors"])
        print(f"  非模板草稿拦截: {result['errors']}")

        result2 = snapshot_patch(state, [draft_incomplete.draft_id])
        assert len(result2["patched"]) == 0
        assert len(result2["errors"]) > 0
        assert any("来源冲突" in e or "不一致" in e for e in result2["errors"])
        print(f"  来源冲突拦截: {result2['errors']}")

        result3 = snapshot_patch(state, [draft_incomplete.draft_id, draft_incomplete.draft_id])
        assert len(result3["patched"]) == 0
        assert any("重复" in e for e in result3["errors"])
        print(f"  重复ID拦截: {result3['errors']}")

        audit_failed = state.get_audit_logs(action="snapshot_patch")
        failed_logs = [l for l in audit_failed if l.result == "failed"]
        assert len(failed_logs) >= 3
        print(f"  审计日志记录: {len(failed_logs)} 条失败记录")

        print("  [OK] 测试15通过")
    finally:
        teardown_test_dir(test_dir)


def test_16_patch_immutability():
    """测试16: 补档后不可变——模板改名/删除/覆盖导入后，快照仍一致"""
    print("\n" + "=" * 60)
    print("测试16: 补档后不可变")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-016"

        defect1 = create_sample_defect(state, "DEF-IMMU-001")
        defect2 = create_sample_defect(state, "DEF-IMMU-002", building="2号楼")

        tpl = create_template(
            state, name="不可变模板", target_status="dispatched",
            handler="戊", remark="原始备注"
        )

        draft1 = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-IMMU-001", source_type="ids"
        )
        execute_draft(state, draft1.draft_id)

        draft2 = create_draft(
            state, source="DEF-IMMU-002", source_type="ids",
            target_status="dispatched", name="老数据草稿"
        )
        draft2.template_id = tpl.template_id
        draft2.template_snapshot = {}
        state.update_draft(draft2)
        state.save_drafts()

        result = snapshot_patch(state, [draft1.draft_id, draft2.draft_id])
        assert len(result["patched"]) == 2

        snap1_before = state.get_draft(draft1.draft_id).template_snapshot.copy()
        snap2_before = state.get_draft(draft2.draft_id).template_snapshot.copy()

        update_template(
            state, template_id=tpl.template_id,
            name="不可变模板-已改名", handler="己"
        )

        delete_template(state, tpl.template_id)

        draft1_r = state.get_draft(draft1.draft_id)
        draft2_r = state.get_draft(draft2.draft_id)
        assert draft1_r.template_snapshot == snap1_before, "模板改名+删除后，已封存快照不应变"
        assert draft2_r.template_snapshot == snap2_before, "模板改名+删除后，已封存快照不应变"
        assert draft1_r.template_snapshot["name"] == "不可变模板"
        assert draft1_r.template_snapshot["handler"] == "戊"

        tpl_info = _resolve_draft_template_info(state, draft1_r)
        assert "已封存" in tpl_info["note"]
        assert tpl_info["snapshot_handler"] == "戊"

        tpl_export = _resolve_template_fields(state, draft1_r)
        assert "已封存" in tpl_export["template_note"]
        assert "不可变" in tpl_export["template_note"]
        assert tpl_export["snapshot_completeness_label"] == "完整快照(已封存)"

        print("  [OK] 测试16通过")
    finally:
        teardown_test_dir(test_dir)


def test_17_patch_restart_consistency():
    """测试17: 补档后重启——体检结果、导出结果一致"""
    print("\n" + "=" * 60)
    print("测试17: 补档后重启一致性")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-017"

        defect1 = create_sample_defect(state, "DEF-RESTART-PATCH-001")
        defect2 = create_sample_defect(state, "DEF-RESTART-PATCH-002", building="2号楼")

        tpl = create_template(
            state, name="重启模板", target_status="closed", handler="庚"
        )

        draft1 = create_draft_from_template(
            state, template_id=tpl.template_id,
            source="DEF-RESTART-PATCH-001", source_type="ids"
        )
        execute_draft(state, draft1.draft_id)

        draft2 = create_draft(
            state, source="DEF-RESTART-PATCH-002", source_type="ids",
            target_status="closed", name="老数据待补档"
        )
        draft2.template_id = tpl.template_id
        draft2.template_snapshot = {}
        state.update_draft(draft2)
        state.save_drafts()
        execute_draft(state, draft2.draft_id)

        result = snapshot_patch(state, [draft1.draft_id, draft2.draft_id])
        assert len(result["patched"]) == 2

        health_before = snapshot_health_check(state)
        csv_path_before = os.path.join(test_dir, "health_before.csv")
        json_path_before = os.path.join(test_dir, "health_before.json")
        export_health_check_csv(health_before, csv_path_before)
        export_health_check_json(health_before, json_path_before)

        draft_csv_before = os.path.join(test_dir, "draft_before.csv")
        export_draft_csv(state, draft1.draft_id, draft_csv_before)

        state2 = PatrolState(data_dir=test_dir)

        health_after = snapshot_health_check(state2)
        csv_path_after = os.path.join(test_dir, "health_after.csv")
        json_path_after = os.path.join(test_dir, "health_after.json")
        export_health_check_csv(health_after, csv_path_after)
        export_health_check_json(health_after, json_path_after)

        draft_csv_after = os.path.join(test_dir, "draft_after.csv")
        export_draft_csv(state2, draft1.draft_id, draft_csv_after)

        import json as json_mod
        with open(json_path_before, "r", encoding="utf-8") as f:
            data_before = json_mod.load(f)
        with open(json_path_after, "r", encoding="utf-8") as f:
            data_after = json_mod.load(f)
        assert len(data_before) == len(data_after)
        for b, a in zip(data_before, data_after):
            assert b["draft_id"] == a["draft_id"]
            assert b["snapshot_status"] == a["snapshot_status"]
            assert b["sealed"] == a["sealed"]
            assert b["can_patch"] == a["can_patch"]
            assert b["missing_fields"] == a["missing_fields"]

        import csv as csv_mod
        with open(draft_csv_before, "r", encoding="utf-8-sig") as f:
            rows_before = list(csv_mod.DictReader(f))
        with open(draft_csv_after, "r", encoding="utf-8-sig") as f:
            rows_after = list(csv_mod.DictReader(f))
        assert rows_before[0]["模板名称"] == rows_after[0]["模板名称"]
        assert rows_before[0]["快照完整度"] == rows_after[0]["快照完整度"]
        assert rows_before[0]["模板溯源备注"] == rows_after[0]["模板溯源备注"]

        audit_reloaded = state2.get_audit_logs(action="snapshot_patch")
        assert len(audit_reloaded) >= 1
        success_logs = [l for l in audit_reloaded if l.result == "success"]
        assert len(success_logs) >= 1

        print("  [OK] 测试17通过")
    finally:
        teardown_test_dir(test_dir)


def test_18_mixed_batch_precheck():
    """测试18: 混合批次预检——已执行/未执行/老数据混合，预检一次说清"""
    print("\n" + "=" * 60)
    print("测试18: 混合批次预检")
    print("=" * 60)

    test_dir = setup_test_dir()
    try:
        state = PatrolState(data_dir=test_dir)
        state.batch_id = "BATCH-TEST-018"

        defect1 = create_sample_defect(state, "DEF-MIX-001")
        defect2 = create_sample_defect(state, "DEF-MIX-002", building="2号楼")
        defect3 = create_sample_defect(state, "DEF-MIX-003", building="3号楼")

        tpl_exists = create_template(
            state, name="存在模板", target_status="dispatched", handler="辛"
        )
        tpl_deleted = create_template(
            state, name="已删模板", target_status="false_positive", handler="壬"
        )

        draft_executed = create_draft_from_template(
            state, template_id=tpl_exists.template_id,
            source="DEF-MIX-001", source_type="ids"
        )
        execute_draft(state, draft_executed.draft_id)

        draft_pending = create_draft_from_template(
            state, template_id=tpl_exists.template_id,
            source="DEF-MIX-002", source_type="ids"
        )

        draft_old = create_draft(
            state, source="DEF-MIX-003", source_type="ids",
            target_status="false_positive", name="老数据-模板已删"
        )
        draft_old.template_id = tpl_deleted.template_id
        draft_old.template_snapshot = {}
        state.update_draft(draft_old)
        state.save_drafts()

        delete_template(state, tpl_deleted.template_id)

        health = snapshot_health_check(state)
        id_to_h = {h["draft_id"]: h for h in health}

        h_executed = id_to_h[draft_executed.draft_id]
        h_pending = id_to_h[draft_pending.draft_id]
        h_old = id_to_h[draft_old.draft_id]

        print(f"  已执行草稿: can_patch={h_executed['can_patch']}, "
              f"snapshot_status={h_executed['snapshot_status']}")
        print(f"  未执行草稿: can_patch={h_pending['can_patch']}, "
              f"snapshot_status={h_pending['snapshot_status']}")
        print(f"  老数据草稿: can_patch={h_old['can_patch']}, "
              f"reason={h_old['cannot_patch_reason']}, risk={h_old['risk_reason']}")

        assert h_executed["can_patch"] is True
        assert h_executed["snapshot_status"] == "complete"
        assert h_pending["can_patch"] is True
        assert h_pending["snapshot_status"] == "complete"
        assert h_old["can_patch"] is False
        assert h_old["snapshot_status"] == "missing"
        assert "已删除" in h_old["cannot_patch_reason"]

        csv_path = os.path.join(test_dir, "mixed_health.csv")
        count = export_health_check_csv(health, csv_path)
        assert count == 3

        json_path = os.path.join(test_dir, "mixed_health.json")
        count2 = export_health_check_json(health, json_path)
        assert count2 == 3

        import csv as csv_mod
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            rows = list(csv_mod.DictReader(f))
        id_to_row = {r["草稿ID"]: r for r in rows}
        assert id_to_row[draft_old.draft_id]["可否补档"] == "否"
        assert "已删除" in id_to_row[draft_old.draft_id]["不可补档原因"]

        patch_result = snapshot_patch(state, [draft_executed.draft_id, draft_pending.draft_id])
        assert len(patch_result["patched"]) == 2
        assert patch_result["errors"] == []

        print("  [OK] 测试18通过")
    finally:
        teardown_test_dir(test_dir)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
