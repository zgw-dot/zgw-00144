"""导入校验和回滚回归测试"""

import os
import json
import tempfile
import shutil
from pathlib import Path

import pytest

from patrol_cli.config import load_rules
from patrol_cli.storage import PatrolState
from patrol_cli.merger import import_and_merge
from patrol_cli.importer import validate_and_transform_rows, read_csv
from patrol_cli.exporter import export_csv

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def config():
    return load_rules("examples/rules.yaml")


@pytest.fixture
def temp_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return str(data_dir)


@pytest.fixture
def clean_state(temp_data_dir):
    return PatrolState(data_dir=temp_data_dir)


@pytest.fixture
def state_with_data(config, temp_data_dir):
    state = PatrolState(data_dir=temp_data_dir)
    import_and_merge("examples/sample_data.csv", config, state, "BATCH-TEST-001")
    return state


def write_csv(path, rows):
    header = "楼栋,设备编号,设备类别,缺陷类型,严重等级,缺陷描述,巡检时间,巡检员,具体位置"
    lines = [header] + rows
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")


class TestConfigValidation:
    def test_invalid_device_category_rejected(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "invalid_category.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,invalid_cat,门机故障,critical,测试无效设备类别,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, clean_state, "BATCH-TEST")

        assert "设备类别不在配置中" in str(excinfo.value)
        assert "invalid_cat" in str(excinfo.value)

    def test_invalid_severity_rejected(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "invalid_severity.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,elevator,门机故障,urgent,测试无效严重等级,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, clean_state, "BATCH-TEST")

        assert "严重等级不在配置中" in str(excinfo.value)
        assert "urgent" in str(excinfo.value)

    def test_invalid_defect_type_rejected(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "invalid_defect_type.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,elevator,未知故障,critical,测试无效缺陷类型,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, clean_state, "BATCH-TEST")

        assert "缺陷类型不在" in str(excinfo.value)
        assert "elevator" in str(excinfo.value)
        assert "未知故障" in str(excinfo.value)

    def test_valid_data_passes(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "valid_data.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,elevator,门机故障,critical,有效数据测试,2025-06-15 08:30:00,张三,1单元",
        ])

        result = import_and_merge(str(csv_path), config, clean_state, "BATCH-TEST")
        assert result.new_defects == 1
        assert len(clean_state.defects) == 1


class TestPartialImportRollback:
    def test_missing_building_rollback(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()
        defects_before = dict(state_with_data.defects)
        imported_before = list(state_with_data.imported_files)
        undo_before = list(state_with_data.undo_stack)
        batch_before = state_with_data.batch_id

        csv_path = tmp_path / "missing_building.csv"
        write_csv(csv_path, [
            ",EL-999,elevator,门机故障,critical,缺楼栋,2025-06-15 08:30:00,张三,1单元",
            "2号楼,EL-001,elevator,按钮失灵,medium,有效行,2025-06-18 10:00:00,李四,2单元",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        assert ("必填字段缺失: building" in str(excinfo.value) or
                "楼栋信息缺失" in str(excinfo.value))
        assert "第2行" in str(excinfo.value)

        stats_after = state_with_data.stats()
        assert stats_after == stats_before
        assert state_with_data.defects == defects_before
        assert state_with_data.imported_files == imported_before
        assert state_with_data.undo_stack == undo_before
        assert state_with_data.batch_id == batch_before
        assert csv_path.name not in state_with_data.imported_files

    def test_bad_time_format_rollback(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()
        defects_before = dict(state_with_data.defects)
        imported_before = list(state_with_data.imported_files)
        undo_before = list(state_with_data.undo_stack)
        batch_before = state_with_data.batch_id

        csv_path = tmp_path / "bad_time.csv"
        write_csv(csv_path, [
            "2号楼,EL-001,elevator,按钮失灵,medium,有效行,2025-06-18 10:00:00,李四,2单元",
            "1号楼,EL-999,elevator,门机故障,critical,坏时间,not-a-date,张三,1单元",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        assert "时间格式无法解析" in str(excinfo.value) or "巡检时间缺失或无效" in str(excinfo.value)
        assert "第3行" in str(excinfo.value)

        stats_after = state_with_data.stats()
        assert stats_after == stats_before
        assert state_with_data.defects == defects_before
        assert state_with_data.imported_files == imported_before
        assert state_with_data.undo_stack == undo_before
        assert state_with_data.batch_id == batch_before

    def test_mixed_invalid_config_values_rollback(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()
        defects_before = dict(state_with_data.defects)
        imported_before = list(state_with_data.imported_files)
        undo_before = list(state_with_data.undo_stack)
        batch_before = state_with_data.batch_id

        csv_path = tmp_path / "mixed_errors.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,elevator,门机故障,critical,有效行1,2025-06-18 08:30:00,张三,1单元",
            "2号楼,EL-002,invalid_cat,门机故障,critical,无效设备类别,2025-06-18 09:00:00,李四,2单元",
            "3号楼,EL-003,elevator,门机故障,bad_sev,无效严重等级,2025-06-18 10:00:00,王五,3单元",
            "4号楼,EL-004,elevator,门机故障,critical,有效行2,2025-06-18 11:00:00,赵六,4单元",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        assert "设备类别不在配置中" in str(excinfo.value)
        assert "严重等级不在配置中" in str(excinfo.value)
        assert "行不合法" in str(excinfo.value)

        stats_after = state_with_data.stats()
        assert stats_after == stats_before
        assert state_with_data.defects == defects_before
        assert state_with_data.imported_files == imported_before
        assert state_with_data.undo_stack == undo_before
        assert state_with_data.batch_id == batch_before

    def test_all_invalid_rows_rollback(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()
        defects_before = dict(state_with_data.defects)
        imported_before = list(state_with_data.imported_files)
        undo_before = list(state_with_data.undo_stack)

        csv_path = tmp_path / "all_invalid.csv"
        write_csv(csv_path, [
            ",EL-999,elevator,门机故障,critical,缺楼栋1,2025-06-15 08:30:00,张三,1单元",
            ",EL-002,elevator,按钮失灵,medium,缺楼栋2,2025-06-18 10:00:00,李四,2单元",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        stats_after = state_with_data.stats()
        assert stats_after == stats_before
        assert state_with_data.defects == defects_before
        assert state_with_data.imported_files == imported_before
        assert state_with_data.undo_stack == undo_before


class TestStateUnchangedAfterFailure:
    def test_stats_unchanged_after_failed_import(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()

        csv_path = tmp_path / "bad.csv"
        write_csv(csv_path, [
            ",EL-999,elevator,门机故障,critical,缺楼栋,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        stats_after = state_with_data.stats()
        assert stats_after["total"] == stats_before["total"]
        assert stats_after["by_status"] == stats_before["by_status"]
        assert stats_after["by_building"] == stats_before["by_building"]
        assert stats_after["imported_files"] == stats_before["imported_files"]
        assert stats_after["undo_stack_size"] == stats_before["undo_stack_size"]

    def test_list_unchanged_after_failed_import(self, config, state_with_data, tmp_path):
        defects_before = state_with_data.list_defects()

        csv_path = tmp_path / "bad.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,bad_cat,门机故障,critical,坏类别,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        defects_after = state_with_data.list_defects()
        assert len(defects_after) == len(defects_before)
        assert [d.defect_id for d in defects_after] == [d.defect_id for d in defects_before]

    def test_export_unchanged_after_failed_import(self, config, state_with_data, tmp_path):
        export_before = tmp_path / "export_before.csv"
        count_before = export_csv(state_with_data, str(export_before))

        csv_path = tmp_path / "bad.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,elevator,门机故障,bad_sev,坏等级,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-TEST-001")

        export_after = tmp_path / "export_after.csv"
        count_after = export_csv(state_with_data, str(export_after))

        assert count_before == count_after

        with open(export_before, "r", encoding="utf-8-sig") as f:
            content_before = f.read()
        with open(export_after, "r", encoding="utf-8-sig") as f:
            content_after = f.read()

        assert content_before == content_after

    def test_disk_files_unchanged_after_failed_import(self, config, temp_data_dir, tmp_path):
        state = PatrolState(data_dir=temp_data_dir)
        import_and_merge("examples/sample_data.csv", config, state, "BATCH-TEST-001")

        defects_file = Path(temp_data_dir) / "defects.json"
        undo_file = Path(temp_data_dir) / "undo_stack.json"
        meta_file = Path(temp_data_dir) / "meta.json"

        mtime_defects_before = defects_file.stat().st_mtime if defects_file.exists() else 0
        mtime_undo_before = undo_file.stat().st_mtime if undo_file.exists() else 0
        mtime_meta_before = meta_file.stat().st_mtime if meta_file.exists() else 0

        with open(defects_file, "r", encoding="utf-8") as f:
            defects_content_before = f.read()
        with open(undo_file, "r", encoding="utf-8") as f:
            undo_content_before = f.read()
        with open(meta_file, "r", encoding="utf-8") as f:
            meta_content_before = f.read()

        csv_path = tmp_path / "bad.csv"
        write_csv(csv_path, [
            "1号楼,EL-999,elevator,未知故障,critical,坏缺陷类型,2025-06-15 08:30:00,张三,1单元",
        ])

        import time
        time.sleep(0.1)

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state, "BATCH-TEST-001")

        with open(defects_file, "r", encoding="utf-8") as f:
            defects_content_after = f.read()
        with open(undo_file, "r", encoding="utf-8") as f:
            undo_content_after = f.read()
        with open(meta_file, "r", encoding="utf-8") as f:
            meta_content_after = f.read()

        assert defects_content_before == defects_content_after
        assert undo_content_before == undo_content_after
        assert meta_content_before == meta_content_after

    def test_batch_id_not_created_on_failure(self, config, clean_state, tmp_path):
        assert clean_state.batch_id == ""

        csv_path = tmp_path / "bad.csv"
        write_csv(csv_path, [
            ",EL-999,elevator,门机故障,critical,缺楼栋,2025-06-15 08:30:00,张三,1单元",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, clean_state, "BATCH-NEW-001")

        assert clean_state.batch_id == ""

        meta_file = Path(clean_state.data_dir) / "meta.json"
        if meta_file.exists():
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            assert meta.get("batch_id", "") == ""


class TestValidateAndTransformRows:
    def test_rejects_invalid_device_category(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,bad_category,门机故障,critical,描述,2025-06-15 08:30:00,张三,位置",
        ])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "test.csv")

        assert len(valid) == 0
        assert len(invalid) == 1
        assert any("设备类别不在配置中" in e for e in invalid[0]["errors"])

    def test_rejects_invalid_severity(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,bad_sev,描述,2025-06-15 08:30:00,张三,位置",
        ])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "test.csv")

        assert len(valid) == 0
        assert len(invalid) == 1
        assert any("严重等级不在配置中" in e for e in invalid[0]["errors"])

    def test_rejects_invalid_defect_type(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,bad_type,critical,描述,2025-06-15 08:30:00,张三,位置",
        ])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "test.csv")

        assert len(valid) == 0
        assert len(invalid) == 1
        assert any("缺陷类型不在" in e for e in invalid[0]["errors"])

    def test_accepts_valid_config_values(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,描述,2025-06-15 08:30:00,张三,位置",
        ])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "test.csv")

        assert len(valid) == 1
        assert len(invalid) == 0

    def test_reports_multiple_errors_per_row(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            ",EL-001,bad_cat,bad_type,bad_sev,描述,2025-06-15 08:30:00,张三,位置",
        ])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "test.csv")

        assert len(valid) == 0
        assert len(invalid) == 1
        errors = invalid[0]["errors"]
        assert any(("必填字段缺失: building" in e or "楼栋信息缺失" in e) for e in errors)
        assert any("设备类别不在配置中" in e for e in errors)
        assert any("严重等级不在配置中" in e for e in errors)


class TestEdgeCases:
    def test_empty_csv_returns_no_errors(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "empty.csv"
        write_csv(csv_path, [])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "empty.csv")
        assert len(valid) == 0
        assert len(invalid) == 0

    def test_defect_type_checked_only_if_category_valid(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,bad_cat,bad_type,critical,描述,2025-06-15 08:30:00,张三,位置",
        ])
        rows, _ = read_csv(str(csv_path))
        valid, invalid = validate_and_transform_rows(rows, config, "test.csv")

        assert len(invalid) == 1
        errors = invalid[0]["errors"]
        assert any("设备类别不在配置中" in e for e in errors)

    def test_error_message_includes_line_numbers(self, config, tmp_path):
        csv_path = tmp_path / "test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,有效,2025-06-15 08:30:00,张三,位置",
            ",EL-002,elevator,门机故障,critical,第3行坏,2025-06-15 08:30:00,张三,位置",
            "1号楼,EL-003,elevator,门机故障,critical,第4行坏,not-a-time,张三,位置",
        ])

        with pytest.raises(ValueError) as excinfo:
            state = PatrolState(data_dir=str(tmp_path / "data"))
            import_and_merge(str(csv_path), config, state, "BATCH-TEST")

        assert "第3行" in str(excinfo.value)
        assert "第4行" in str(excinfo.value)
