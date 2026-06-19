"""端到端测试：草稿执行链路的模板溯源完整性"""
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
    execute_draft, WorkflowError
)
from patrol_cli.exporter import export_draft_csv, export_draft_list_csv
from patrol_cli.cli import _resolve_draft_template_info


def setup_test_dir():
    """创建独立测试目录"""
    test_dir = tempfile.mkdtemp(prefix="patrol_test_")
    return test_dir


def teardown_test_dir(test_dir):
    """清理测试目录"""
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)


def create_sample_defect(state, defect_id, **kwargs):
    """创建一个测试用缺陷"""
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


def test_1_template_draft_execute():
    """测试1: 模板建草稿并执行，执行回显要包含模板信息"""
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
        assert draft.template_id == template.template_id, "草稿应关联模板ID"
        assert draft.template_snapshot, "草稿应保存模板快照"
        assert draft.template_snapshot["name"] == "派单模板-A", "快照名称应正确"
        print(f"  模板ID已关联: {draft.template_id}")
        print(f"  模板快照已保存: 是 (name={draft.template_snapshot['name']})")

        result = execute_draft(state, draft.draft_id)
        print(f"  执行结果: success_count={result.success_count}")
        assert result.success_count == 2, "应成功执行2条"

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  模板信息解析: has_template={tpl_info['has_template']}, "
              f"name={tpl_info['template_name']}, has_snapshot={tpl_info['has_snapshot']}")
        assert tpl_info["has_template"] is True
        assert tpl_info["template_name"] == "派单模板-A"
        assert tpl_info["has_snapshot"] is True
        assert tpl_info["template_exists"] is True

        print("  [OK] 测试1通过")
        return test_dir, state
    finally:
        pass


def test_2_modify_template_and_review_old(test_dir, state):
    """测试2: 修改模板后回看旧记录，旧草稿应仍显示原始模板快照"""
    print("\n" + "=" * 60)
    print("测试2: 修改模板后回看旧记录")
    print("=" * 60)

    try:
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
        assert tpl_info["template_exists"] is True, "模板仍存在"
        print(f"  快照目标状态: {tpl_info['snapshot_target_status']}")
        print(f"  快照处理人: {tpl_info['snapshot_handler']}")
        assert tpl_info["snapshot_handler"] == "张工", "旧草稿处理人快照应为张工，不是李工"

        print("  [OK] 测试2通过")
    finally:
        pass


def test_3_restart_and_query(test_dir):
    """测试3: 重启（重新加载）后查询，模板溯源信息保持一致"""
    print("\n" + "=" * 60)
    print("测试3: 重启后查询")
    print("=" * 60)

    try:
        state2 = PatrolState(data_dir=test_dir)
        drafts = state2.list_drafts()
        assert len(drafts) == 1, "重启后应仍有1条草稿"

        draft = drafts[0]
        tpl_info = _resolve_draft_template_info(state2, draft)
        print(f"  重启后解析模板: name={tpl_info['template_name']}, "
              f"id={tpl_info['template_id']}, has_snapshot={tpl_info['has_snapshot']}")
        assert tpl_info["template_name"] == "派单模板-A"
        assert tpl_info["has_snapshot"] is True

        templates = state2.list_templates()
        assert len(templates) == 1
        assert templates[0].name == "派单模板-A-已改名"
        print(f"  当前模板名: {templates[0].name} (不受旧草稿影响)")

        print("  [OK] 测试3通过")
        return state2
    finally:
        pass


def test_4_export_consistency(test_dir, state):
    """测试4: 导出核对，CSV 中模板字段应与快照一致"""
    print("\n" + "=" * 60)
    print("测试4: 导出核对")
    print("=" * 60)

    try:
        drafts = state.list_drafts()
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
            assert "模板溯源备注" in reader.fieldnames, "应包含模板溯源备注列"
            assert rows[0]["模板名称"] == "派单模板-A", "导出的模板名应为快照原始名"
            assert rows[0]["模板ID"] == draft.template_id
            print(f"  单草稿导出 - 模板名: {rows[0]['模板名称']}, 备注: {rows[0]['模板溯源备注']}")

        with open(list_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "模板溯源备注" in reader.fieldnames
            assert rows[0]["模板名称"] == "派单模板-A"
            print(f"  草稿列表导出 - 模板名: {rows[0]['模板名称']}, 备注: {rows[0]['模板溯源备注']}")

        print("  [OK] 测试4通过")
    finally:
        pass


def test_5_delete_template_and_trace(test_dir, state):
    """测试5: 删除模板后，旧草稿仍能通过快照溯源"""
    print("\n" + "=" * 60)
    print("测试5: 删除模板后溯源")
    print("=" * 60)

    try:
        drafts = state.list_drafts()
        draft = drafts[0]
        template_id = draft.template_id

        delete_template(state, template_id)
        print(f"  已删除模板: {template_id}")

        assert state.get_template(template_id) is None, "模板应已删除"

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  删除后解析: name={tpl_info['template_name']}, "
              f"exists={tpl_info['template_exists']}, note={tpl_info['note']}")
        assert tpl_info["template_name"] == "派单模板-A", "仍应通过快照得到模板名"
        assert tpl_info["template_exists"] is False
        assert "已删除" in tpl_info["note"]

        csv_path = os.path.join(test_dir, "draft_after_delete.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert rows[0]["模板名称"] == "派单模板-A"
            assert "已删除" in rows[0]["模板溯源备注"]
            print(f"  删除后CSV导出 - 模板名: {rows[0]['模板名称']}, 备注: {rows[0]['模板溯源备注']}")

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
        print(f"  手动创建草稿: {draft.name} ({draft.draft_id})")
        assert draft.template_id == "", "非模板草稿 template_id 应为空"
        assert not draft.template_snapshot, "非模板草稿不应有快照"

        tpl_info = _resolve_draft_template_info(state, draft)
        print(f"  模板信息解析: has_template={tpl_info['has_template']}, "
              f"name='{tpl_info['template_name']}'")
        assert tpl_info["has_template"] is False
        assert tpl_info["template_id"] == ""

        result = execute_draft(state, draft.draft_id)
        print(f"  执行结果: success_count={result.success_count}")
        assert result.success_count == 2

        tpl_info_after = _resolve_draft_template_info(state, draft)
        assert tpl_info_after["has_template"] is False

        csv_path = os.path.join(test_dir2, "non_template_draft.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        import csv
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            print(f"  CSV: 模板名='{rows[0]['模板名称']}', 模板溯源备注='{rows[0]['模板溯源备注']}'")
            assert rows[0]["模板名称"] == "未使用模板"
            assert rows[0]["模板ID"] == ""
            assert rows[0]["模板溯源备注"] == "手动创建"

        list_csv = os.path.join(test_dir2, "non_template_list.csv")
        export_draft_list_csv(state, list_csv)
        with open(list_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert rows[0]["模板名称"] == "未使用模板"
            assert rows[0]["模板溯源备注"] == "手动创建"

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
              f"has_snapshot={tpl_info['has_snapshot']}, note={tpl_info['note']}")
        assert tpl_info["template_name"] == "老版本模板"
        assert tpl_info["has_snapshot"] is False
        assert "老数据" in tpl_info["note"]

        delete_template(state, template.template_id)
        state2 = PatrolState(data_dir=test_dir3)
        draft_reloaded = state2.get_draft(draft.draft_id)
        tpl_info2 = _resolve_draft_template_info(state2, draft_reloaded)
        print(f"  删除模板+重启后: name={tpl_info2['template_name']}, note={tpl_info2['note']}")
        assert tpl_info2["template_name"] == template.template_id
        assert "老数据，模板已删除且无快照" in tpl_info2["note"]

        csv_path = os.path.join(test_dir3, "old_data.csv")
        export_draft_csv(state2, draft.draft_id, csv_path)
        import csv
        execute_draft(state2, draft.draft_id)
        export_draft_csv(state2, draft.draft_id, csv_path)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            print(f"  老数据CSV: name='{rows[0]['模板名称']}', note='{rows[0]['模板溯源备注']}'")
            assert "老数据" in rows[0]["模板溯源备注"]

        print("  [OK] 测试7通过")
    finally:
        teardown_test_dir(test_dir3)


if __name__ == "__main__":
    import traceback
    error_file = Path(__file__).parent / "test_error.log"
    print("开始端到端测试：草稿执行-模板溯源链路")
    print("=" * 60)

    test_dir = None
    state = None
    try:
        test_dir, state = test_1_template_draft_execute()
        test_2_modify_template_and_review_old(test_dir, state)
        state = test_3_restart_and_query(test_dir)
        test_4_export_consistency(test_dir, state)
        test_5_delete_template_and_trace(test_dir, state)
        test_6_non_template_draft()
        test_7_old_data_missing_snapshot()

        print("\n" + "=" * 60)
        print("[OK] 全部端到端测试通过！")
        print("=" * 60)
    except AssertionError as e:
        msg = "\n[FAIL] 断言失败!\n" + traceback.format_exc()
        print(msg)
        with open(error_file, "w", encoding="utf-8") as f:
            f.write(msg)
        sys.exit(1)
    except Exception as e:
        msg = f"\n[FAIL] 测试异常: {e}\n" + traceback.format_exc()
        print(msg)
        with open(error_file, "w", encoding="utf-8") as f:
            f.write(msg)
        sys.exit(1)
    finally:
        if test_dir:
            teardown_test_dir(test_dir)
