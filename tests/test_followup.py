import json
import csv
import os
import re
from datetime import datetime, timedelta

from click.testing import CliRunner

from patrol_cli.storage import PatrolState
from patrol_cli.config import load_rules
from patrol_cli.models import DefectRecord
from patrol_cli.cli import cli
from patrol_cli.followup import (
    FollowUpError,
    preview_create_followup, create_followup_plan,
    dispatch_followup_plan,
    complete_followup_item, complete_followup_plan,
    cancel_followup_plan,
    get_followup_plan_detail, list_followup_plans,
    export_followup_plans_json, export_followup_plans_csv,
    import_followup_plans_json,
)


TEST_CONFIG = "examples/rules.yaml"


def _setup_state(tmp_path):
    state = PatrolState(data_dir=str(tmp_path))
    state.batch_id = "BATCH-FUP-TEST"
    return state


def _load_config():
    return load_rules(TEST_CONFIG)


def _add_defect(state, defect_id, **kwargs):
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


class TestCreateFollowUp:
    def test_create_by_ids(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-001")
        _add_defect(state, "DEF-FUP-002", building="2号楼")

        plan = create_followup_plan(
            state, config, "测试计划",
            defect_ids=["DEF-FUP-001", "DEF-FUP-002"],
            handler="张工",
            remark="测试备注"
        )

        assert plan.plan_id.startswith("FUP-")
        assert plan.name == "测试计划"
        assert plan.handler == "张工"
        assert plan.remark == "测试备注"
        assert plan.status == "pending"
        assert len(plan.items) == 2
        assert plan.items[0].defect_snapshot["defect_id"] == "DEF-FUP-001"
        assert plan.items[0].item_status == "pending"
        assert plan.deadline != ""

    def test_create_by_building(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-B1", building="A栋")
        _add_defect(state, "DEF-FUP-B2", building="A栋")
        _add_defect(state, "DEF-FUP-B3", building="B栋")

        plan = create_followup_plan(
            state, config, "A栋回访",
            building="A栋"
        )

        assert len(plan.items) == 2
        defect_ids = [item.defect_id for item in plan.items]
        assert "DEF-FUP-B1" in defect_ids
        assert "DEF-FUP-B2" in defect_ids

    def test_create_by_status(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-S1", status="pending")
        _add_defect(state, "DEF-FUP-S2", status="pending")
        _add_defect(state, "DEF-FUP-S3", status="closed")

        plan = create_followup_plan(
            state, config, "待处理回访",
            status="pending"
        )

        assert len(plan.items) == 2

    def test_dry_run_preview(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-DRY1")
        _add_defect(state, "DEF-FUP-DRY2", building="2号楼")

        preview = preview_create_followup(
            state, config, "预览测试",
            defect_ids=["DEF-FUP-DRY1", "DEF-FUP-DRY2"]
        )

        assert preview.can_create is True
        assert preview.total_count == 2
        assert len(preview.items) == 2
        assert len(preview.conflicts) == 0
        assert len(state.followup_plans) == 0
        assert preview.status_fingerprint == {
            "DEF-FUP-DRY1": "pending",
            "DEF-FUP-DRY2": "pending",
        }

    def test_deadline_from_severity(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-SEV", severity="critical")

        plan = create_followup_plan(
            state, config, "严重等级测试",
            defect_ids=["DEF-FUP-SEV"]
        )

        assert plan.deadline != ""
        deadline_dt = datetime.fromisoformat(plan.deadline)
        expected = datetime.now() + timedelta(hours=4)
        assert abs((deadline_dt - expected).total_seconds()) < 60

    def test_deadline_override_hours(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-OVR", severity="critical")

        plan = create_followup_plan(
            state, config, "覆盖时限测试",
            defect_ids=["DEF-FUP-OVR"],
            deadline_override_hours=10
        )

        deadline_dt = datetime.fromisoformat(plan.deadline)
        expected = datetime.now() + timedelta(hours=10)
        assert abs((deadline_dt - expected).total_seconds()) < 60

    def test_deadline_override_direct(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-DIR")
        custom_deadline = "2025-12-31T23:59:59"

        plan = create_followup_plan(
            state, config, "直接截止时间",
            defect_ids=["DEF-FUP-DIR"],
            deadline_override=custom_deadline
        )

        assert plan.deadline == custom_deadline

    def test_create_empty_name_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FUP-NAME")

        try:
            create_followup_plan(state, config, "   ", defect_ids=["DEF-FUP-NAME"])
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "名称" in str(e)

    def test_create_no_defects_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()

        try:
            create_followup_plan(state, config, "空计划", building="不存在的楼")
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "未找到" in str(e)


class TestConflictInterception:
    def test_duplicate_defect_ids(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DUP-1")

        try:
            create_followup_plan(
                state, config, "重复测试",
                defect_ids=["DEF-DUP-1", "DEF-DUP-1"]
            )
            assert False, "应该抛出冲突异常"
        except FollowUpError as e:
            assert "重复" in str(e)
        assert len(state.followup_plans) == 0

    def test_defect_not_found(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-EXIST")

        try:
            create_followup_plan(
                state, config, "不存在测试",
                defect_ids=["DEF-EXIST", "DEF-NOTEXIST"]
            )
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "不存在" in str(e)
        assert len(state.followup_plans) == 0

    def test_same_defect_in_another_active_plan(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CONFLICT-1")
        _add_defect(state, "DEF-CONFLICT-2")

        create_followup_plan(
            state, config, "计划A",
            defect_ids=["DEF-CONFLICT-1"]
        )

        try:
            create_followup_plan(
                state, config, "计划B",
                defect_ids=["DEF-CONFLICT-1", "DEF-CONFLICT-2"]
            )
            assert False, "应该抛出冲突异常"
        except FollowUpError as e:
            assert "未完成计划" in str(e)
        assert len(state.followup_plans) == 1

    def test_completed_plan_no_conflict(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-NOCONF")

        plan1 = create_followup_plan(
            state, config, "已完成计划",
            defect_ids=["DEF-NOCONF"]
        )
        dispatch_followup_plan(state, plan1.plan_id)
        complete_followup_plan(state, plan1.plan_id)

        plan2 = create_followup_plan(
            state, config, "新计划",
            defect_ids=["DEF-NOCONF"]
        )

        assert plan2 is not None
        assert len(state.followup_plans) == 2

    def test_cancelled_plan_no_conflict(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CANCONF")

        plan1 = create_followup_plan(
            state, config, "已撤销计划",
            defect_ids=["DEF-CANCONF"]
        )
        cancel_followup_plan(state, plan1.plan_id, reason="测试撤销")

        plan2 = create_followup_plan(
            state, config, "新计划2",
            defect_ids=["DEF-CANCONF"]
        )

        assert plan2 is not None
        assert len(state.followup_plans) == 2

    def test_atomic_all_or_nothing(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-ATOM-OK")
        _add_defect(state, "DEF-ATOM-EXIST")

        create_followup_plan(
            state, config, "已有计划",
            defect_ids=["DEF-ATOM-EXIST"]
        )

        before_count = len(state.followup_plans)

        try:
            create_followup_plan(
                state, config, "原子测试",
                defect_ids=["DEF-ATOM-OK", "DEF-ATOM-EXIST"]
            )
            assert False, "应该整批失败"
        except FollowUpError:
            pass

        assert len(state.followup_plans) == before_count


class TestStatusDriftInterception:
    def test_status_drift_after_preview_rejects_create(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DRIFT-1", status="pending")
        _add_defect(state, "DEF-DRIFT-2", status="pending")

        preview = preview_create_followup(
            state, config, "状态漂移测试",
            defect_ids=["DEF-DRIFT-1", "DEF-DRIFT-2"]
        )
        assert preview.can_create is True
        fingerprint = preview.status_fingerprint

        defect = state.get_defect("DEF-DRIFT-1")
        defect.status = "closed"
        state.save()

        try:
            create_followup_plan(
                state, config, "状态漂移测试",
                defect_ids=["DEF-DRIFT-1", "DEF-DRIFT-2"],
                status_fingerprint=fingerprint,
            )
            assert False, "应该因状态漂移而整批失败"
        except FollowUpError as e:
            assert "状态已变更" in str(e)

        assert len(state.followup_plans) == 0

    def test_no_fingerprint_allows_create_despite_change(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-NOFP-1", status="pending")

        defect = state.get_defect("DEF-NOFP-1")
        defect.status = "closed"
        state.save()

        plan = create_followup_plan(
            state, config, "无指纹测试",
            defect_ids=["DEF-NOFP-1"],
        )
        assert plan is not None
        assert len(state.followup_plans) == 1

    def test_drift_with_other_conflicts_reports_all(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-MIX-DRIFT", status="pending")

        preview = preview_create_followup(
            state, config, "混合冲突测试",
            defect_ids=["DEF-MIX-DRIFT", "DEF-MIX-NONEXIST"]
        )
        assert not preview.can_create

        defect = state.get_defect("DEF-MIX-DRIFT")
        defect.status = "closed"
        state.save()

        try:
            create_followup_plan(
                state, config, "混合冲突测试",
                defect_ids=["DEF-MIX-DRIFT", "DEF-MIX-NONEXIST"],
                status_fingerprint=preview.status_fingerprint,
            )
            assert False, "应该整批失败"
        except FollowUpError as e:
            msg = str(e)
            assert "不存在" in msg
            assert "状态已变更" in msg

    def test_preview_fingerprint_matches_current_state(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-FP-OK", status="pending")

        preview = preview_create_followup(
            state, config, "指纹一致测试",
            defect_ids=["DEF-FP-OK"]
        )

        plan = create_followup_plan(
            state, config, "指纹一致测试",
            defect_ids=["DEF-FP-OK"],
            status_fingerprint=preview.status_fingerprint,
        )
        assert plan is not None

    def test_multiple_defects_partial_drift_rejects_all(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-PDRIFT-1", status="pending")
        _add_defect(state, "DEF-PDRIFT-2", status="pending")

        preview = preview_create_followup(
            state, config, "部分漂移测试",
            defect_ids=["DEF-PDRIFT-1", "DEF-PDRIFT-2"]
        )
        fingerprint = preview.status_fingerprint

        defect = state.get_defect("DEF-PDRIFT-2")
        defect.status = "dispatched"
        state.save()

        try:
            create_followup_plan(
                state, config, "部分漂移测试",
                defect_ids=["DEF-PDRIFT-1", "DEF-PDRIFT-2"],
                status_fingerprint=fingerprint,
            )
            assert False, "只要有一个漂移就应整批失败"
        except FollowUpError as e:
            assert "DEF-PDRIFT-2" in str(e)
            assert "状态已变更" in str(e)

        assert len(state.followup_plans) == 0


class TestConflictSummary:
    def test_all_conflict_types_reported_at_once(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-COMB-1", status="pending")
        _add_defect(state, "DEF-COMB-2", status="pending")

        create_followup_plan(
            state, config, "已有计划",
            defect_ids=["DEF-COMB-1"]
        )

        try:
            create_followup_plan(
                state, config, "组合冲突",
                defect_ids=["DEF-COMB-1", "DEF-COMB-1", "DEF-COMB-NONEXIST"]
            )
            assert False, "应抛出冲突"
        except FollowUpError as e:
            msg = str(e)
            assert "重复" in msg
            assert "不存在" in msg
            assert "未完成计划" in msg

    def test_duplicate_and_not_found_together(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()

        try:
            create_followup_plan(
                state, config, "双冲突",
                defect_ids=["DEF-NON-A", "DEF-NON-A"]
            )
            assert False
        except FollowUpError as e:
            msg = str(e)
            assert "重复" in msg
            assert "不存在" in msg


class TestDispatch:
    def test_dispatch_pending_plan(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DISP-1")

        plan = create_followup_plan(
            state, config, "待签收计划",
            defect_ids=["DEF-DISP-1"]
        )

        dispatched = dispatch_followup_plan(
            state, plan.plan_id,
            handler="李工",
            dispatched_by="管理员"
        )

        assert dispatched.status == "dispatched"
        assert dispatched.handler == "李工"
        assert dispatched.dispatched_at != ""
        assert dispatched.dispatched_by == "管理员"

    def test_dispatch_already_dispatched_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DISP-2")

        plan = create_followup_plan(
            state, config, "已签收计划",
            defect_ids=["DEF-DISP-2"]
        )
        dispatch_followup_plan(state, plan.plan_id)

        try:
            dispatch_followup_plan(state, plan.plan_id)
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "已签收" in str(e)

    def test_dispatch_completed_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DISP-3")

        plan = create_followup_plan(
            state, config, "已完成计划",
            defect_ids=["DEF-DISP-3"]
        )
        dispatch_followup_plan(state, plan.plan_id)
        complete_followup_plan(state, plan.plan_id)

        try:
            dispatch_followup_plan(state, plan.plan_id)
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "已完成" in str(e)

    def test_dispatch_cancelled_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DISP-4")

        plan = create_followup_plan(
            state, config, "已撤销计划",
            defect_ids=["DEF-DISP-4"]
        )
        cancel_followup_plan(state, plan.plan_id)

        try:
            dispatch_followup_plan(state, plan.plan_id)
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "已撤销" in str(e)


class TestComplete:
    def test_complete_single_item(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-COMP-1")
        _add_defect(state, "DEF-COMP-2")

        plan = create_followup_plan(
            state, config, "单条完成测试",
            defect_ids=["DEF-COMP-1", "DEF-COMP-2"]
        )
        dispatch_followup_plan(state, plan.plan_id)

        result = complete_followup_item(
            state, plan.plan_id, "DEF-COMP-1",
            result="已整改",
            result_remark="现场检查合格",
            result_by="王工"
        )

        item1 = next(i for i in result.items if i.defect_id == "DEF-COMP-1")
        item2 = next(i for i in result.items if i.defect_id == "DEF-COMP-2")

        assert item1.item_status == "completed"
        assert item1.result == "已整改"
        assert item1.result_remark == "现场检查合格"
        assert item1.result_by == "王工"
        assert item1.result_at != ""

        assert item2.item_status == "pending"
        assert result.status == "dispatched"

    def test_complete_all_items_auto_plan_complete(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-COMP-ALL1")
        _add_defect(state, "DEF-COMP-ALL2")

        plan = create_followup_plan(
            state, config, "全部完成测试",
            defect_ids=["DEF-COMP-ALL1", "DEF-COMP-ALL2"]
        )
        dispatch_followup_plan(state, plan.plan_id)

        complete_followup_item(state, plan.plan_id, "DEF-COMP-ALL1")
        result = complete_followup_item(state, plan.plan_id, "DEF-COMP-ALL2")

        assert result.status == "completed"
        assert result.completed_at != ""

    def test_complete_entire_plan(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-BATCH-1")
        _add_defect(state, "DEF-BATCH-2")

        plan = create_followup_plan(
            state, config, "批量完成测试",
            defect_ids=["DEF-BATCH-1", "DEF-BATCH-2"]
        )
        dispatch_followup_plan(state, plan.plan_id)

        results = {
            "DEF-BATCH-1": {"result": "整改完成", "result_remark": "已修复"},
            "DEF-BATCH-2": {"result": "无需处理", "result_remark": "误报"},
        }
        result = complete_followup_plan(
            state, plan.plan_id,
            results=results,
            result_by="赵工"
        )

        assert result.status == "completed"
        for item in result.items:
            assert item.item_status == "completed"
            assert item.result_by == "赵工"

    def test_complete_already_completed_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-COMP-DUP1")
        _add_defect(state, "DEF-COMP-DUP2")

        plan = create_followup_plan(
            state, config, "重复完成测试",
            defect_ids=["DEF-COMP-DUP1", "DEF-COMP-DUP2"]
        )
        dispatch_followup_plan(state, plan.plan_id)
        complete_followup_item(state, plan.plan_id, "DEF-COMP-DUP1")

        try:
            complete_followup_item(state, plan.plan_id, "DEF-COMP-DUP1")
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "已完成回访" in str(e)


class TestCancel:
    def test_cancel_pending_plan(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CANCEL-1")

        plan = create_followup_plan(
            state, config, "待撤销计划",
            defect_ids=["DEF-CANCEL-1"]
        )

        result = cancel_followup_plan(
            state, plan.plan_id,
            reason="计划调整"
        )

        assert result.status == "cancelled"
        assert result.cancel_reason == "计划调整"
        assert result.cancelled_at != ""
        for item in result.items:
            assert item.item_status == "cancelled"

    def test_cancel_dispatched_plan(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CANCEL-2")

        plan = create_followup_plan(
            state, config, "已签收计划",
            defect_ids=["DEF-CANCEL-2"]
        )
        dispatch_followup_plan(state, plan.plan_id)

        result = cancel_followup_plan(state, plan.plan_id, reason="取消")
        assert result.status == "cancelled"

    def test_cancel_completed_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CANCEL-3")

        plan = create_followup_plan(
            state, config, "已完成计划",
            defect_ids=["DEF-CANCEL-3"]
        )
        dispatch_followup_plan(state, plan.plan_id)
        complete_followup_plan(state, plan.plan_id)

        try:
            cancel_followup_plan(state, plan.plan_id)
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "已完成" in str(e)

    def test_cancel_already_cancelled_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CANCEL-4")

        plan = create_followup_plan(
            state, config, "已撤销计划",
            defect_ids=["DEF-CANCEL-4"]
        )
        cancel_followup_plan(state, plan.plan_id)

        try:
            cancel_followup_plan(state, plan.plan_id)
            assert False, "应该抛出异常"
        except FollowUpError as e:
            assert "已撤销" in str(e)


class TestListAndDetail:
    def test_list_all_plans(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-LIST-1")
        _add_defect(state, "DEF-LIST-2")

        create_followup_plan(state, config, "计划1", defect_ids=["DEF-LIST-1"])
        create_followup_plan(state, config, "计划2", defect_ids=["DEF-LIST-2"])

        plans = list_followup_plans(state)
        assert len(plans) == 2

    def test_list_by_status(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-LIST-S1")
        _add_defect(state, "DEF-LIST-S2")

        plan1 = create_followup_plan(state, config, "待签收", defect_ids=["DEF-LIST-S1"])
        plan2 = create_followup_plan(state, config, "已签收", defect_ids=["DEF-LIST-S2"])
        dispatch_followup_plan(state, plan2.plan_id)

        pending = list_followup_plans(state, status="pending")
        dispatched = list_followup_plans(state, status="dispatched")

        assert len(pending) == 1
        assert pending[0]["plan_id"] == plan1.plan_id
        assert len(dispatched) == 1
        assert dispatched[0]["plan_id"] == plan2.plan_id

    def test_get_detail(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DET-1")
        _add_defect(state, "DEF-DET-2")

        plan = create_followup_plan(
            state, config, "详情测试",
            defect_ids=["DEF-DET-1", "DEF-DET-2"],
            handler="陈工",
            remark="测试详情"
        )

        detail = get_followup_plan_detail(state, plan.plan_id)

        assert detail["plan_id"] == plan.plan_id
        assert detail["name"] == "详情测试"
        assert detail["handler"] == "陈工"
        assert detail["remark"] == "测试详情"
        assert detail["total_items"] == 2
        assert detail["pending_items"] == 2
        assert detail["completed_items"] == 0
        assert len(detail["items"]) == 2

        item = detail["items"][0]
        assert "snapshot_status" in item
        assert "current_status" in item
        assert "snapshot_building" in item
        assert "snapshot_description" in item

    def test_detail_shows_snapshot_vs_current(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-CHG", status="pending")

        plan = create_followup_plan(
            state, config, "状态变化测试",
            defect_ids=["DEF-CHG"]
        )

        defect = state.get_defect("DEF-CHG")
        defect.status = "closed"
        state.save()

        detail = get_followup_plan_detail(state, plan.plan_id)
        item = detail["items"][0]

        assert item["snapshot_status"] == "pending"
        assert item["current_status"] == "closed"


class TestExport:
    def test_export_json(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-EXP-J1")
        _add_defect(state, "DEF-EXP-J2")

        plan = create_followup_plan(
            state, config, "JSON导出测试",
            defect_ids=["DEF-EXP-J1", "DEF-EXP-J2"],
            handler="周工"
        )

        output = str(tmp_path / "followup.json")
        count = export_followup_plans_json(state, output)

        assert count == 1
        assert os.path.exists(output)

        with open(output, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert plan.plan_id in data
        assert data[plan.plan_id]["name"] == "JSON导出测试"
        assert len(data[plan.plan_id]["items"]) == 2

    def test_export_csv(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-EXP-C1")
        _add_defect(state, "DEF-EXP-C2")

        plan = create_followup_plan(
            state, config, "CSV导出测试",
            defect_ids=["DEF-EXP-C1", "DEF-EXP-C2"]
        )
        dispatch_followup_plan(state, plan.plan_id)
        complete_followup_item(
            state, plan.plan_id, "DEF-EXP-C1",
            result="已修复", result_remark="合格", result_by="测试员"
        )

        output = str(tmp_path / "followup.csv")
        count = export_followup_plans_csv(state, output)

        assert count == 2
        assert os.path.exists(output)

        with open(output, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert "计划ID" in reader.fieldnames
        assert "计划名称" in reader.fieldnames
        assert "缺陷编号" in reader.fieldnames
        assert "回访状态" in reader.fieldnames
        assert "回访结果" in reader.fieldnames

        row1 = next(r for r in rows if r["缺陷编号"] == "DEF-EXP-C1")
        assert row1["回访状态"] == "已完成"
        assert row1["回访结果"] == "已修复"

        row2 = next(r for r in rows if r["缺陷编号"] == "DEF-EXP-C2")
        assert row2["回访状态"] == "待回访"

    def test_export_specific_ids(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-EXP-S1")
        _add_defect(state, "DEF-EXP-S2")

        plan1 = create_followup_plan(state, config, "导出1", defect_ids=["DEF-EXP-S1"])
        create_followup_plan(state, config, "导出2", defect_ids=["DEF-EXP-S2"])

        output = str(tmp_path / "selected.json")
        count = export_followup_plans_json(state, output, plan_ids=[plan1.plan_id])

        assert count == 1
        with open(output, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 1
        assert plan1.plan_id in data


class TestImport:
    def test_import_json(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-IMP-1")

        plan = create_followup_plan(
            state, config, "原始计划",
            defect_ids=["DEF-IMP-1"],
            handler="导入测试员"
        )

        export_file = str(tmp_path / "export.json")
        export_followup_plans_json(state, export_file)

        state2 = PatrolState(data_dir=str(tmp_path / "new_dir"))
        state2.batch_id = "BATCH-IMP"
        _add_defect(state2, "DEF-IMP-1")

        result = import_followup_plans_json(state2, export_file)

        assert result["imported_count"] == 1
        assert "原始计划" in str(result["imported"])
        assert len(state2.followup_plans) == 1

        imported_plan = list(state2.followup_plans.values())[0]
        assert imported_plan.name == "原始计划"
        assert imported_plan.handler == "导入测试员"
        assert len(imported_plan.items) == 1

    def test_import_skip_existing(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-IMP-SKIP")

        plan = create_followup_plan(
            state, config, "已存在计划",
            defect_ids=["DEF-IMP-SKIP"]
        )

        export_file = str(tmp_path / "export.json")
        export_followup_plans_json(state, export_file)

        state2 = PatrolState(data_dir=str(tmp_path / "new_dir2"))
        state2.batch_id = "BATCH-IMP2"
        _add_defect(state2, "DEF-IMP-SKIP")
        state2.add_followup_plan(plan)
        state2.save_followup_plans()

        result = import_followup_plans_json(state2, export_file, overwrite=False)

        assert result["skipped_count"] == 1
        assert result["imported_count"] == 0

    def test_import_overwrite(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-IMP-OVR")

        plan = create_followup_plan(
            state, config, "原始名称",
            defect_ids=["DEF-IMP-OVR"]
        )

        export_file = str(tmp_path / "export.json")
        export_followup_plans_json(state, export_file)

        plan.name = "已修改"
        state.add_followup_plan(plan)
        state.save_followup_plans()

        result = import_followup_plans_json(state, export_file, overwrite=True)

        assert result["imported_count"] == 1
        updated = state.get_followup_plan(plan.plan_id)
        assert updated.name == "原始名称"


class TestRestartPersistence:
    def test_plans_persist_after_reload(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-RST-1")
        _add_defect(state, "DEF-RST-2")

        plan = create_followup_plan(
            state, config, "重启测试计划",
            defect_ids=["DEF-RST-1", "DEF-RST-2"],
            handler="重启测试员",
            remark="重启备注"
        )
        dispatch_followup_plan(state, plan.plan_id)
        complete_followup_item(
            state, plan.plan_id, "DEF-RST-1",
            result="已完成", result_remark="测试", result_by="测试员"
        )

        state2 = PatrolState(data_dir=str(tmp_path))

        reloaded = state2.get_followup_plan(plan.plan_id)
        assert reloaded is not None
        assert reloaded.name == "重启测试计划"
        assert reloaded.handler == "重启测试员"
        assert reloaded.status == "dispatched"
        assert len(reloaded.items) == 2

        item1 = next(i for i in reloaded.items if i.defect_id == "DEF-RST-1")
        assert item1.item_status == "completed"
        assert item1.result == "已完成"

        item2 = next(i for i in reloaded.items if i.defect_id == "DEF-RST-2")
        assert item2.item_status == "pending"

    def test_list_works_after_reload(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-LST-RST1")
        _add_defect(state, "DEF-LST-RST2")

        create_followup_plan(state, config, "列表重启1", defect_ids=["DEF-LST-RST1"])
        create_followup_plan(state, config, "列表重启2", defect_ids=["DEF-LST-RST2"])

        state2 = PatrolState(data_dir=str(tmp_path))
        plans = list_followup_plans(state2)

        assert len(plans) == 2

    def test_detail_works_after_reload(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-DET-RST")

        plan = create_followup_plan(
            state, config, "详情重启",
            defect_ids=["DEF-DET-RST"],
            handler="详情测试"
        )

        state2 = PatrolState(data_dir=str(tmp_path))
        detail = get_followup_plan_detail(state2, plan.plan_id)

        assert detail["name"] == "详情重启"
        assert detail["handler"] == "详情测试"
        assert detail["total_items"] == 1

    def test_query_after_restart_with_status_drift(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-RST-DRIFT", status="pending")

        plan = create_followup_plan(
            state, config, "重启漂移查询",
            defect_ids=["DEF-RST-DRIFT"]
        )

        defect = state.get_defect("DEF-RST-DRIFT")
        defect.status = "closed"
        state.save()

        state2 = PatrolState(data_dir=str(tmp_path))
        detail = get_followup_plan_detail(state2, plan.plan_id)
        item = detail["items"][0]

        assert item["snapshot_status"] == "pending"
        assert item["current_status"] == "closed"


class TestJsonCsvRoundTrip:
    def test_json_round_trip(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-RT-J1")
        _add_defect(state, "DEF-RT-J2", building="2号楼")

        plan = create_followup_plan(
            state, config, "往返测试",
            defect_ids=["DEF-RT-J1", "DEF-RT-J2"],
            handler="往返员",
            remark="往返备注"
        )
        dispatch_followup_plan(state, plan.plan_id)
        complete_followup_item(
            state, plan.plan_id, "DEF-RT-J1",
            result="已修复", result_by="测试员"
        )

        output = str(tmp_path / "roundtrip.json")
        export_followup_plans_json(state, output)

        state2 = PatrolState(data_dir=str(tmp_path / "rt_new"))
        state2.batch_id = "BATCH-RT"
        _add_defect(state2, "DEF-RT-J1")
        _add_defect(state2, "DEF-RT-J2", building="2号楼")

        result = import_followup_plans_json(state2, output)
        assert result["imported_count"] == 1
        assert result["error_count"] == 0

        imported = state2.get_followup_plan(plan.plan_id)
        assert imported.name == "往返测试"
        assert imported.handler == "往返员"
        assert imported.remark == "往返备注"
        assert imported.status == "dispatched"
        assert len(imported.items) == 2

        item1 = next(i for i in imported.items if i.defect_id == "DEF-RT-J1")
        assert item1.item_status == "completed"
        assert item1.result == "已修复"

        item2 = next(i for i in imported.items if i.defect_id == "DEF-RT-J2")
        assert item2.item_status == "pending"

    def test_csv_export_reimport_via_json(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-RT-C1")
        _add_defect(state, "DEF-RT-C2")

        plan = create_followup_plan(
            state, config, "CSV往返测试",
            defect_ids=["DEF-RT-C1", "DEF-RT-C2"],
            handler="CSV员"
        )

        json_output = str(tmp_path / "csv_rt.json")
        export_followup_plans_json(state, json_output)

        csv_output = str(tmp_path / "csv_rt.csv")
        row_count = export_followup_plans_csv(state, csv_output)
        assert row_count == 2

        with open(csv_output, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert any(r["缺陷编号"] == "DEF-RT-C1" for r in rows)
        assert any(r["缺陷编号"] == "DEF-RT-C2" for r in rows)

        state2 = PatrolState(data_dir=str(tmp_path / "csv_rt_new"))
        state2.batch_id = "BATCH-CSV-RT"
        _add_defect(state2, "DEF-RT-C1")
        _add_defect(state2, "DEF-RT-C2")

        result = import_followup_plans_json(state2, json_output)
        assert result["imported_count"] == 1

        imported = state2.get_followup_plan(plan.plan_id)
        assert imported.name == "CSV往返测试"
        assert len(imported.items) == 2

    def test_multiple_plans_round_trip(self, tmp_path):
        state = _setup_state(tmp_path)
        config = _load_config()
        _add_defect(state, "DEF-MRT-1")
        _add_defect(state, "DEF-MRT-2")
        _add_defect(state, "DEF-MRT-3")

        plan1 = create_followup_plan(state, config, "多计划1", defect_ids=["DEF-MRT-1"])
        plan2 = create_followup_plan(state, config, "多计划2", defect_ids=["DEF-MRT-2", "DEF-MRT-3"])

        output = str(tmp_path / "multi_rt.json")
        count = export_followup_plans_json(state, output)
        assert count == 2

        state2 = PatrolState(data_dir=str(tmp_path / "multi_rt_new"))
        state2.batch_id = "BATCH-MRT"
        _add_defect(state2, "DEF-MRT-1")
        _add_defect(state2, "DEF-MRT-2")
        _add_defect(state2, "DEF-MRT-3")

        result = import_followup_plans_json(state2, output)
        assert result["imported_count"] == 2

        plans = list_followup_plans(state2)
        assert len(plans) == 2


def _parse_fingerprint_from_output(output):
    """从 CLI 输出中解析状态指纹 JSON"""
    match = re.search(r"状态指纹:\s*(\{.*\})", output)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


class TestCliFollowupCreateFlow:
    """CLI 层回访计划创建链路测试

    重点验证：先 dry-run 看预览，再隔一次命令正式 create 的完整链路。
    两次调用通过状态指纹（JSON 字符串）传递，不靠内存透传 preview 对象。
    """

    def _invoke(self, data_dir, args):
        """调用 CLI 命令的辅助方法"""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", TEST_CONFIG, "--data-dir", str(data_dir)] + args,
            catch_exceptions=False
        )
        return result

    def test_cli_dry_run_preview_returns_fingerprint(self, tmp_path):
        """CLI dry-run 预览应该输出状态指纹"""
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CLI-DRY1", status="pending")
        _add_defect(state, "DEF-CLI-DRY2", status="pending")

        result = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "CLI预览测试",
            "--ids", "DEF-CLI-DRY1,DEF-CLI-DRY2",
            "--dry-run"
        ])

        assert result.exit_code == 0
        assert "回访计划预览" in result.output
        assert "无冲突，可创建" in result.output
        assert "状态指纹:" in result.output

        fingerprint = _parse_fingerprint_from_output(result.output)
        assert fingerprint is not None
        assert fingerprint["DEF-CLI-DRY1"] == "pending"
        assert fingerprint["DEF-CLI-DRY2"] == "pending"

        assert len(state.followup_plans) == 0

    def test_cli_create_succeeds_with_no_drift(self, tmp_path):
        """不漂移对照用例：dry-run 预览后直接 create，应该成功创建"""
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CLI-OK1", status="pending")
        _add_defect(state, "DEF-CLI-OK2", status="pending")

        result_dry = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "CLI正常创建测试",
            "--ids", "DEF-CLI-OK1,DEF-CLI-OK2",
            "--handler", "测试员",
            "--remark", "对照用例",
            "--dry-run"
        ])
        assert result_dry.exit_code == 0

        fingerprint = _parse_fingerprint_from_output(result_dry.output)
        assert fingerprint is not None
        assert len(fingerprint) == 2

        result_create = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "CLI正常创建测试",
            "--ids", "DEF-CLI-OK1,DEF-CLI-OK2",
            "--handler", "测试员",
            "--remark", "对照用例",
            "--status-fingerprint", json.dumps(fingerprint, ensure_ascii=False)
        ])

        assert result_create.exit_code == 0
        assert "回访计划创建成功" in result_create.output
        assert "包含缺陷: 2 条" in result_create.output
        assert "测试员" in result_create.output
        assert "对照用例" in result_create.output

        state2 = PatrolState(data_dir=str(tmp_path))
        assert len(state2.followup_plans) == 1
        plan = list(state2.followup_plans.values())[0]
        assert plan.name == "CLI正常创建测试"
        assert len(plan.items) == 2
        assert plan.handler == "测试员"
        assert plan.remark == "对照用例"

    def test_cli_create_fails_on_status_drift_between_calls(self, tmp_path):
        """状态漂移用例：dry-run 预览后改缺陷状态，再 create 应该整批失败

        验证点：
        1. 第一次调用 dry-run 预览成功，得到指纹
        2. 中间修改其中一条缺陷状态
        3. 第二次正式 create 带指纹调用，整批失败
        4. 报错信息包含状态漂移描述
        5. 没有留下任何回访计划（原子性）
        """
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CLI-DRIFT1", status="pending")
        _add_defect(state, "DEF-CLI-DRIFT2", status="pending")

        result_dry = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "CLI漂移测试",
            "--ids", "DEF-CLI-DRIFT1,DEF-CLI-DRIFT2",
            "--dry-run"
        ])
        assert result_dry.exit_code == 0
        assert "无冲突，可创建" in result_dry.output

        fingerprint = _parse_fingerprint_from_output(result_dry.output)
        assert fingerprint is not None
        assert fingerprint["DEF-CLI-DRIFT1"] == "pending"
        assert fingerprint["DEF-CLI-DRIFT2"] == "pending"

        defect = state.get_defect("DEF-CLI-DRIFT1")
        defect.status = "closed"
        state.save()

        result_create = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "CLI漂移测试",
            "--ids", "DEF-CLI-DRIFT1,DEF-CLI-DRIFT2",
            "--status-fingerprint", json.dumps(fingerprint, ensure_ascii=False)
        ])

        assert result_create.exit_code != 0
        assert "状态已变更" in result_create.output
        assert "DEF-CLI-DRIFT1" in result_create.output
        assert "预览时" in result_create.output
        assert "当前" in result_create.output
        assert "待派单" in result_create.output or "pending" in result_create.output
        assert "已关闭" in result_create.output or "closed" in result_create.output

        state2 = PatrolState(data_dir=str(tmp_path))
        assert len(state2.followup_plans) == 0

    def test_cli_create_no_fingerprint_uses_live_state(self, tmp_path):
        """不传指纹时，create 使用当前状态（不检查漂移）"""
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CLI-NOFP", status="pending")

        result_dry = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "无指纹测试",
            "--ids", "DEF-CLI-NOFP",
            "--dry-run"
        ])
        assert result_dry.exit_code == 0

        defect = state.get_defect("DEF-CLI-NOFP")
        defect.status = "closed"
        state.save()

        result_create = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "无指纹测试",
            "--ids", "DEF-CLI-NOFP",
        ])

        assert result_create.exit_code == 0
        assert "回访计划创建成功" in result_create.output

        state2 = PatrolState(data_dir=str(tmp_path))
        assert len(state2.followup_plans) == 1

    def test_cli_create_multiple_defects_partial_drift_rejects_all(self, tmp_path):
        """部分缺陷漂移：只要有一个漂移，整批都不创建"""
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CLI-PDRIFT-A", status="pending")
        _add_defect(state, "DEF-CLI-PDRIFT-B", status="pending")
        _add_defect(state, "DEF-CLI-PDRIFT-C", status="pending")

        result_dry = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "部分漂移测试",
            "--ids", "DEF-CLI-PDRIFT-A,DEF-CLI-PDRIFT-B,DEF-CLI-PDRIFT-C",
            "--dry-run"
        ])
        assert result_dry.exit_code == 0

        fingerprint = _parse_fingerprint_from_output(result_dry.output)
        assert fingerprint is not None

        defect = state.get_defect("DEF-CLI-PDRIFT-B")
        defect.status = "dispatched"
        state.save()

        result_create = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "部分漂移测试",
            "--ids", "DEF-CLI-PDRIFT-A,DEF-CLI-PDRIFT-B,DEF-CLI-PDRIFT-C",
            "--status-fingerprint", json.dumps(fingerprint, ensure_ascii=False)
        ])

        assert result_create.exit_code != 0
        assert "状态已变更" in result_create.output
        assert "DEF-CLI-PDRIFT-B" in result_create.output

        state2 = PatrolState(data_dir=str(tmp_path))
        assert len(state2.followup_plans) == 0

    def test_cli_create_with_building_filter(self, tmp_path):
        """按楼栋筛选的 CLI 创建链路测试"""
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CLI-BLD-A1", building="A栋", status="pending")
        _add_defect(state, "DEF-CLI-BLD-A2", building="A栋", status="pending")
        _add_defect(state, "DEF-CLI-BLD-B1", building="B栋", status="pending")

        result_dry = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "楼栋筛选测试",
            "--building", "A栋",
            "--dry-run"
        ])
        assert result_dry.exit_code == 0
        assert "将包含缺陷: 2 条" in result_dry.output

        fingerprint = _parse_fingerprint_from_output(result_dry.output)
        assert fingerprint is not None
        assert len(fingerprint) == 2

        result_create = self._invoke(tmp_path, [
            "followup", "create",
            "--name", "楼栋筛选测试",
            "--building", "A栋",
            "--status-fingerprint", json.dumps(fingerprint, ensure_ascii=False)
        ])

        assert result_create.exit_code == 0
        assert "回访计划创建成功" in result_create.output

        state2 = PatrolState(data_dir=str(tmp_path))
        assert len(state2.followup_plans) == 1
        plan = list(state2.followup_plans.values())[0]
        assert len(plan.items) == 2
