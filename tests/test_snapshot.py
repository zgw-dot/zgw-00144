import os
import json
import shutil
import tempfile
import csv

from patrol_cli.storage import PatrolState
from patrol_cli.workflow import (
    create_template, update_template, delete_template,
    create_draft, create_draft_from_template,
    execute_draft, WorkflowError,
    snapshot_health_check, snapshot_patch,
)
from patrol_cli.exporter import (
    export_health_check_csv, export_health_check_json,
    export_draft_csv, export_draft_list_csv,
    _resolve_template_fields, _classify_snapshot,
)
from patrol_cli.cli import _resolve_draft_template_info, _classify_snapshot as cli_classify_snapshot
from patrol_cli.models import DefectRecord, AuditLogEntry


def _setup_state(tmp_path):
    state = PatrolState(data_dir=str(tmp_path))
    state.batch_id = "BATCH-SNAP-TEST"
    return state


def _add_defect(state, defect_id, **kwargs):
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


class TestClassifySnapshot:
    def test_missing_empty_dict(self):
        ss, mf = _classify_snapshot({})
        assert ss == "missing"
        assert len(mf) == 3

    def test_missing_none(self):
        ss, mf = _classify_snapshot(None)
        assert ss == "missing"

    def test_complete(self):
        ss, mf = _classify_snapshot({"name": "A", "target_status": "closed", "handler": "X"})
        assert ss == "complete"
        assert mf == []

    def test_incomplete_one_field(self):
        ss, mf = _classify_snapshot({"name": "A", "target_status": "closed"})
        assert ss == "incomplete"
        assert "处理人" in mf

    def test_incomplete_two_fields(self):
        ss, mf = _classify_snapshot({"name": "A"})
        assert ss == "incomplete"
        assert "目标状态" in mf
        assert "处理人" in mf

    def test_incomplete_name_and_handler_missing(self):
        ss, mf = _classify_snapshot({"target_status": "closed"})
        assert ss == "incomplete"
        assert "模板名称" in mf
        assert "处理人" in mf

    def test_cli_classify_matches(self):
        assert cli_classify_snapshot({}) == ("missing", ["模板名称", "目标状态", "处理人"])
        assert cli_classify_snapshot({"name": "A", "target_status": "X", "handler": "Y"}) == ("complete", [])


class TestHealthCheck:
    def test_complete_snapshot(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-HC-001")
        tpl = create_template(state, name="完整模板", target_status="dispatched", handler="甲")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-HC-001", source_type="ids")
        execute_draft(state, draft.draft_id)

        results = snapshot_health_check(state)
        id_to_r = {r["draft_id"]: r for r in results}
        r = id_to_r[draft.draft_id]

        assert r["snapshot_status"] == "complete"
        assert r["sealed"] is False
        assert r["can_patch"] is True
        assert r["missing_fields"] == []

    def test_incomplete_snapshot(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-HC-002")
        tpl = create_template(state, name="残缺模板", target_status="dispatched", handler="乙")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-HC-002", source_type="ids")
        draft.template_snapshot = {"name": "残缺模板"}
        state.update_draft(draft)
        state.save_drafts()

        results = snapshot_health_check(state)
        id_to_r = {r["draft_id"]: r for r in results}
        r = id_to_r[draft.draft_id]

        assert r["snapshot_status"] == "incomplete"
        assert "目标状态" in r["missing_fields"]
        assert "处理人" in r["missing_fields"]
        assert r["can_patch"] is True

    def test_missing_snapshot_old_data(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-HC-003")
        tpl = create_template(state, name="缺失模板", target_status="closed", handler="丙")
        draft = create_draft(state, source="DEF-HC-003", source_type="ids", target_status="closed", name="老数据草稿")
        draft.template_id = tpl.template_id
        draft.template_snapshot = {}
        state.update_draft(draft)
        state.save_drafts()

        results = snapshot_health_check(state)
        id_to_r = {r["draft_id"]: r for r in results}
        r = id_to_r[draft.draft_id]

        assert r["snapshot_status"] == "missing"
        assert r["can_patch"] is True

    def test_non_template_draft(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-HC-004")
        draft = create_draft(state, source="DEF-HC-004", source_type="ids", target_status="false_positive", name="非模板草稿")

        results = snapshot_health_check(state)
        id_to_r = {r["draft_id"]: r for r in results}
        r = id_to_r[draft.draft_id]

        assert r["can_patch"] is False
        assert "非模板草稿" in r["cannot_patch_reason"]

    def test_template_deleted_cannot_patch(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-HC-DEL")
        tpl = create_template(state, name="已删模板", target_status="dispatched", handler="丁")
        draft = create_draft(state, source="DEF-HC-DEL", source_type="ids", target_status="dispatched", name="老数据-已删模板")
        draft.template_id = tpl.template_id
        draft.template_snapshot = {}
        state.update_draft(draft)
        state.save_drafts()
        delete_template(state, tpl.template_id)

        results = snapshot_health_check(state)
        id_to_r = {r["draft_id"]: r for r in results}
        r = id_to_r[draft.draft_id]

        assert r["can_patch"] is False
        assert "已删除" in r["cannot_patch_reason"]

    def test_health_check_no_side_effects(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-NOSIDE")
        tpl = create_template(state, name="无副作用模板", target_status="dispatched", handler="戊")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-NOSIDE", source_type="ids")

        before_status = draft.status
        before_sealed = draft.snapshot_sealed_at
        before_snap = draft.template_snapshot.copy()

        snapshot_health_check(state)

        draft_after = state.get_draft(draft.draft_id)
        assert draft_after.status == before_status
        assert draft_after.snapshot_sealed_at == before_sealed
        assert draft_after.template_snapshot == before_snap


class TestPatchAndSeal:
    def test_patch_complete_and_missing(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-P01")
        _add_defect(state, "DEF-P02", building="2号楼")
        tpl = create_template(state, name="补档模板", target_status="dispatched", handler="甲")

        draft_complete = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-P01", source_type="ids")
        draft_missing = create_draft(state, source="DEF-P02", source_type="ids", target_status="dispatched", name="老数据待补档")
        draft_missing.template_id = tpl.template_id
        draft_missing.template_snapshot = {}
        state.update_draft(draft_missing)
        state.save_drafts()

        result = snapshot_patch(state, [draft_complete.draft_id, draft_missing.draft_id])
        assert len(result["patched"]) == 2
        assert result["errors"] == []
        assert result["audit_id"]

        dc = state.get_draft(draft_complete.draft_id)
        dm = state.get_draft(draft_missing.draft_id)
        assert dc.snapshot_sealed_at != ""
        assert dm.snapshot_sealed_at != ""
        assert dm.template_snapshot["name"] == "补档模板"
        assert dm.template_snapshot["handler"] == "甲"

    def test_sealed_after_patch(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-SEAL")
        tpl = create_template(state, name="封存模板", target_status="closed", handler="乙")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-SEAL", source_type="ids")

        snapshot_patch(state, [draft.draft_id])

        health = snapshot_health_check(state)
        id_to_h = {h["draft_id"]: h for h in health}
        assert id_to_h[draft.draft_id]["sealed"] is True
        assert id_to_h[draft.draft_id]["can_patch"] is False
        assert "已封存" in id_to_h[draft.draft_id]["cannot_patch_reason"]

    def test_patch_already_sealed_fails(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-RESEAL")
        tpl = create_template(state, name="重复补档模板", target_status="dispatched", handler="丙")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-RESEAL", source_type="ids")

        result1 = snapshot_patch(state, [draft.draft_id])
        assert len(result1["patched"]) == 1

        result2 = snapshot_patch(state, [draft.draft_id])
        assert len(result2["patched"]) == 0
        assert len(result2["errors"]) > 0

        audit_logs = state.get_audit_logs(action="snapshot_patch")
        failed_logs = [l for l in audit_logs if l.result == "failed"]
        assert len(failed_logs) >= 1

    def test_sealed_snapshot_immutable_after_template_change(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-IMMU")
        tpl = create_template(state, name="不可变模板", target_status="dispatched", handler="丁", remark="原始备注")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-IMMU", source_type="ids")
        execute_draft(state, draft.draft_id)

        snapshot_patch(state, [draft.draft_id])
        snap_before = state.get_draft(draft.draft_id).template_snapshot.copy()

        update_template(state, template_id=tpl.template_id, name="不可变模板-已改名", handler="戊")
        delete_template(state, tpl.template_id)

        snap_after = state.get_draft(draft.draft_id).template_snapshot
        assert snap_after == snap_before
        assert snap_after["name"] == "不可变模板"
        assert snap_after["handler"] == "丁"

    def test_sealed_reflects_in_resolve_info(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-INFO")
        tpl = create_template(state, name="信息模板", target_status="dispatched", handler="己")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-INFO", source_type="ids")
        execute_draft(state, draft.draft_id)

        snapshot_patch(state, [draft.draft_id])

        tpl_info = _resolve_draft_template_info(state, state.get_draft(draft.draft_id))
        assert "已封存" in tpl_info["note"]

        tpl_export = _resolve_template_fields(state, state.get_draft(draft.draft_id))
        assert "已封存" in tpl_export["template_note"]
        assert "不可变" in tpl_export["template_note"]


class TestConflictInterception:
    def test_duplicate_draft_id(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-DUP")
        tpl = create_template(state, name="重复ID模板", target_status="dispatched", handler="庚")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-DUP", source_type="ids")

        result = snapshot_patch(state, [draft.draft_id, draft.draft_id])
        assert len(result["patched"]) == 0
        assert any("重复" in e for e in result["errors"])

    def test_non_template_draft_intercepted(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-NOTPL")
        draft = create_draft(state, source="DEF-NOTPL", source_type="ids", target_status="closed", name="非模板草稿")

        result = snapshot_patch(state, [draft.draft_id])
        assert len(result["patched"]) == 0
        assert any("非模板草稿" in e for e in result["errors"])

    def test_source_conflict_incomplete(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-CONFLICT")
        tpl = create_template(state, name="冲突模板", target_status="dispatched", handler="辛")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-CONFLICT", source_type="ids")
        draft.template_snapshot = {"name": "冲突模板", "target_status": "closed"}
        state.update_draft(draft)
        state.save_drafts()

        result = snapshot_patch(state, [draft.draft_id])
        assert len(result["patched"]) == 0
        assert any("来源冲突" in e or "不一致" in e for e in result["errors"])

    def test_template_deleted_incomplete_intercepted(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-DELINC")
        tpl = create_template(state, name="已删残缺模板", target_status="dispatched", handler="壬")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-DELINC", source_type="ids")
        draft.template_snapshot = {"name": "已删残缺模板"}
        state.update_draft(draft)
        state.save_drafts()
        delete_template(state, tpl.template_id)

        result = snapshot_patch(state, [draft.draft_id])
        assert len(result["patched"]) == 0
        assert any("模板已删除" in e or "无法补齐" in e for e in result["errors"])

    def test_template_deleted_missing_intercepted(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-DELMIS")
        tpl = create_template(state, name="已删无快照模板", target_status="closed", handler="癸")
        draft = create_draft(state, source="DEF-DELMIS", source_type="ids", target_status="closed", name="老数据")
        draft.template_id = tpl.template_id
        draft.template_snapshot = {}
        state.update_draft(draft)
        state.save_drafts()
        delete_template(state, tpl.template_id)

        result = snapshot_patch(state, [draft.draft_id])
        assert len(result["patched"]) == 0
        assert any("已删除" in e or "无法补档" in e for e in result["errors"])

    def test_batch_atomic_all_or_nothing(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-ATOMIC-1")
        _add_defect(state, "DEF-ATOMIC-2")
        tpl = create_template(state, name="原子模板", target_status="dispatched", handler="甲")
        draft_ok = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-ATOMIC-1", source_type="ids")
        draft_bad = create_draft(state, source="DEF-ATOMIC-2", source_type="ids", target_status="closed", name="非模板")

        result = snapshot_patch(state, [draft_ok.draft_id, draft_bad.draft_id])
        assert len(result["patched"]) == 0
        assert len(result["errors"]) > 0

        draft_ok_check = state.get_draft(draft_ok.draft_id)
        assert draft_ok_check.snapshot_sealed_at == ""

    def test_all_conflicts_logged_in_audit(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-LOG-1")
        draft = create_draft(state, source="DEF-LOG-1", source_type="ids", target_status="closed", name="非模板草稿")

        snapshot_patch(state, [draft.draft_id])

        audit_logs = state.get_audit_logs(action="snapshot_patch")
        failed = [l for l in audit_logs if l.result == "failed"]
        assert len(failed) >= 1


class TestExport:
    def test_health_check_csv_export(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-EXP-CSV")
        tpl = create_template(state, name="导出CSV模板", target_status="dispatched", handler="甲")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-EXP-CSV", source_type="ids")

        health = snapshot_health_check(state)
        csv_path = str(tmp_path / "health.csv")
        count = export_health_check_csv(health, csv_path)
        assert count >= 1

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            id_to_row = {r["草稿ID"]: r for r in rows}
            assert draft.draft_id in id_to_row
            assert id_to_row[draft.draft_id]["快照状态"] == "完整快照"

    def test_health_check_json_export(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-EXP-JSON")
        tpl = create_template(state, name="导出JSON模板", target_status="dispatched", handler="乙")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-EXP-JSON", source_type="ids")

        health = snapshot_health_check(state)
        json_path = str(tmp_path / "health.json")
        count = export_health_check_json(health, json_path)
        assert count >= 1

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            id_to_d = {d["draft_id"]: d for d in data}
            assert draft.draft_id in id_to_d
            assert id_to_d[draft.draft_id]["snapshot_status"] == "complete"

    def test_draft_csv_export_with_snapshot_info(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-DRFT-CSV")
        tpl = create_template(state, name="草稿导出模板", target_status="dispatched", handler="丙")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-DRFT-CSV", source_type="ids")
        execute_draft(state, draft.draft_id)

        csv_path = str(tmp_path / "draft.csv")
        export_draft_csv(state, draft.draft_id, csv_path)

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "模板溯源备注" in reader.fieldnames
            assert "快照完整度" in reader.fieldnames
            assert rows[0]["模板名称"] == "草稿导出模板"
            assert rows[0]["快照完整度"] == "完整快照"

    def test_draft_list_csv_export(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-LIST-CSV")
        tpl = create_template(state, name="列表导出模板", target_status="dispatched", handler="丁")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-LIST-CSV", source_type="ids")
        execute_draft(state, draft.draft_id)

        csv_path = str(tmp_path / "draft_list.csv")
        export_draft_list_csv(state, csv_path)

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "模板溯源备注" in reader.fieldnames
            assert "快照完整度" in reader.fieldnames


class TestRestartConsistency:
    def test_health_check_persists_after_restart(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-RST-1")
        _add_defect(state, "DEF-RST-2", building="2号楼")
        tpl = create_template(state, name="重启模板", target_status="closed", handler="庚")

        draft1 = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-RST-1", source_type="ids")
        execute_draft(state, draft1.draft_id)

        draft2 = create_draft(state, source="DEF-RST-2", source_type="ids", target_status="closed", name="老数据待补档")
        draft2.template_id = tpl.template_id
        draft2.template_snapshot = {}
        state.update_draft(draft2)
        state.save_drafts()
        execute_draft(state, draft2.draft_id)

        snapshot_patch(state, [draft1.draft_id, draft2.draft_id])

        health_before = snapshot_health_check(state)
        json_path_before = str(tmp_path / "health_before.json")
        export_health_check_json(health_before, json_path_before)

        draft_csv_before = str(tmp_path / "draft_before.csv")
        export_draft_csv(state, draft1.draft_id, draft_csv_before)

        state2 = PatrolState(data_dir=str(tmp_path))

        health_after = snapshot_health_check(state2)
        json_path_after = str(tmp_path / "health_after.json")
        export_health_check_json(health_after, json_path_after)

        draft_csv_after = str(tmp_path / "draft_after.csv")
        export_draft_csv(state2, draft1.draft_id, draft_csv_after)

        with open(json_path_before, "r", encoding="utf-8") as f:
            data_before = json.load(f)
        with open(json_path_after, "r", encoding="utf-8") as f:
            data_after = json.load(f)
        assert len(data_before) == len(data_after)
        for b, a in zip(data_before, data_after):
            assert b["draft_id"] == a["draft_id"]
            assert b["snapshot_status"] == a["snapshot_status"]
            assert b["sealed"] == a["sealed"]
            assert b["can_patch"] == a["can_patch"]

        with open(draft_csv_before, "r", encoding="utf-8-sig") as f:
            rows_before = list(csv.DictReader(f))
        with open(draft_csv_after, "r", encoding="utf-8-sig") as f:
            rows_after = list(csv.DictReader(f))
        assert rows_before[0]["模板名称"] == rows_after[0]["模板名称"]
        assert rows_before[0]["快照完整度"] == rows_after[0]["快照完整度"]
        assert rows_before[0]["模板溯源备注"] == rows_after[0]["模板溯源备注"]

    def test_audit_logs_persist_after_restart(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-AUDIT-RST")
        tpl = create_template(state, name="审计重启模板", target_status="dispatched", handler="辛")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-AUDIT-RST", source_type="ids")

        snapshot_patch(state, [draft.draft_id])

        state2 = PatrolState(data_dir=str(tmp_path))
        audit_reloaded = state2.get_audit_logs(action="snapshot_patch")
        success_logs = [l for l in audit_reloaded if l.result == "success"]
        assert len(success_logs) >= 1

    def test_sealed_snapshot_survives_template_delete_and_restart(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-DEL-RST")
        tpl = create_template(state, name="删后重启模板", target_status="dispatched", handler="壬")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-DEL-RST", source_type="ids")
        execute_draft(state, draft.draft_id)

        snapshot_patch(state, [draft.draft_id])
        snap_before = state.get_draft(draft.draft_id).template_snapshot.copy()

        update_template(state, template_id=tpl.template_id, name="删后重启模板-改名", handler="癸")
        delete_template(state, tpl.template_id)

        state2 = PatrolState(data_dir=str(tmp_path))
        draft_r = state2.get_draft(draft.draft_id)
        assert draft_r.template_snapshot == snap_before
        assert draft_r.template_snapshot["name"] == "删后重启模板"
        assert draft_r.template_snapshot["handler"] == "壬"

        tpl_info = _resolve_draft_template_info(state2, draft_r)
        assert tpl_info["snapshot_status"] == "complete"
        assert "已封存" in tpl_info["note"]


class TestDraftTemplateTrace:
    def test_template_draft_execute_trace(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-TRACE-1")
        _add_defect(state, "DEF-TRACE-2", building="2号楼")
        tpl = create_template(state, name="派单模板-A", target_status="dispatched", handler="张工", remark="标准派单处理")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-TRACE-1,DEF-TRACE-2", source_type="ids", name="草稿-派单测试")

        result = execute_draft(state, draft.draft_id)
        assert result.success_count == 2

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["has_template"] is True
        assert tpl_info["template_name"] == "派单模板-A"
        assert tpl_info["has_snapshot"] is True
        assert tpl_info["snapshot_status"] == "complete"
        assert tpl_info["snapshot_handler"] == "张工"

    def test_modify_template_old_draft_shows_snapshot(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-MOD")
        tpl = create_template(state, name="派单模板-B", target_status="dispatched", handler="李工")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-MOD", source_type="ids")
        execute_draft(state, draft.draft_id)

        update_template(state, template_id=tpl.template_id, name="派单模板-B-已改名", handler="王工")

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["template_name"] == "派单模板-B"
        assert tpl_info["snapshot_handler"] == "李工"

    def test_delete_template_old_draft_still_traces(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-DEL-TRACE")
        tpl = create_template(state, name="历史模板-C", target_status="dispatched", handler="周工")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-DEL-TRACE", source_type="ids")
        execute_draft(state, draft.draft_id)

        delete_template(state, tpl.template_id)

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["template_name"] == "历史模板-C"
        assert tpl_info["template_exists"] is False
        assert tpl_info["snapshot_status"] == "complete"

    def test_non_template_draft(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-NOTPL-TRACE")
        draft = create_draft(state, source="DEF-NOTPL-TRACE", source_type="ids", target_status="closed", name="手动草稿", handler="赵工")

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["has_template"] is False
        assert tpl_info["snapshot_status"] == "missing"

    def test_old_data_no_snapshot(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-OLD-TRACE")
        tpl = create_template(state, name="老版本模板", target_status="false_positive", handler="钱工")
        draft = create_draft(state, source="DEF-OLD-TRACE", source_type="ids", target_status="false_positive", name="老数据模拟草稿")
        draft.template_id = tpl.template_id
        draft.template_snapshot = {}
        state.update_draft(draft)
        state.save_drafts()

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["snapshot_status"] == "missing"
        assert "老数据" in tpl_info["note"]

    def test_incomplete_snapshot_with_delete(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-INCDEL")
        tpl = create_template(state, name="残缺+删除模板", target_status="dispatched", handler="孙工")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-INCDEL", source_type="ids")
        draft.template_snapshot = {"name": "残缺+删除模板"}
        state.update_draft(draft)
        state.save_drafts()

        delete_template(state, tpl.template_id)

        tpl_info = _resolve_draft_template_info(state, draft)
        assert tpl_info["snapshot_status"] == "incomplete"
        assert tpl_info["template_exists"] is False
        assert "残缺快照" in tpl_info["note"]
        assert "模板已删除" in tpl_info["note"]

    def test_incomplete_snapshot_export(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-INCEXP")
        tpl = create_template(state, name="残缺导出模板", target_status="dispatched", handler="钱工")
        draft = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-INCEXP", source_type="ids")
        draft.template_snapshot = {"name": "残缺导出模板"}
        state.update_draft(draft)
        state.save_drafts()
        execute_draft(state, draft.draft_id)

        csv_path = str(tmp_path / "inc_draft.csv")
        export_draft_csv(state, draft.draft_id, csv_path)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert "字段残缺" in rows[0]["快照完整度"]
            assert "残缺快照" in rows[0]["模板溯源备注"]


class TestMixedBatchPrecheck:
    def test_mixed_batch_identifies_patchable_and_blocked(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-MIX-1")
        _add_defect(state, "DEF-MIX-2", building="2号楼")
        _add_defect(state, "DEF-MIX-3", building="3号楼")

        tpl_exists = create_template(state, name="存在模板", target_status="dispatched", handler="辛")
        tpl_deleted = create_template(state, name="已删模板", target_status="false_positive", handler="壬")

        draft_executed = create_draft_from_template(state, template_id=tpl_exists.template_id, source="DEF-MIX-1", source_type="ids")
        execute_draft(state, draft_executed.draft_id)

        draft_pending = create_draft_from_template(state, template_id=tpl_exists.template_id, source="DEF-MIX-2", source_type="ids")

        draft_old = create_draft(state, source="DEF-MIX-3", source_type="ids", target_status="false_positive", name="老数据-模板已删")
        draft_old.template_id = tpl_deleted.template_id
        draft_old.template_snapshot = {}
        state.update_draft(draft_old)
        state.save_drafts()
        delete_template(state, tpl_deleted.template_id)

        health = snapshot_health_check(state)
        id_to_h = {h["draft_id"]: h for h in health}

        assert id_to_h[draft_executed.draft_id]["can_patch"] is True
        assert id_to_h[draft_pending.draft_id]["can_patch"] is True
        assert id_to_h[draft_old.draft_id]["can_patch"] is False
        assert "已删除" in id_to_h[draft_old.draft_id]["cannot_patch_reason"]

    def test_patch_only_patchable_in_mixed(self, tmp_path):
        state = _setup_state(tmp_path)
        _add_defect(state, "DEF-MIX-P1")
        _add_defect(state, "DEF-MIX-P2", building="2号楼")

        tpl = create_template(state, name="混合可补模板", target_status="dispatched", handler="辛")
        d1 = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-MIX-P1", source_type="ids")
        d2 = create_draft_from_template(state, template_id=tpl.template_id, source="DEF-MIX-P2", source_type="ids")

        result = snapshot_patch(state, [d1.draft_id, d2.draft_id])
        assert len(result["patched"]) == 2
        assert result["errors"] == []

        health = snapshot_health_check(state)
        id_to_h = {h["draft_id"]: h for h in health}
        assert id_to_h[d1.draft_id]["sealed"] is True
        assert id_to_h[d2.draft_id]["sealed"] is True
