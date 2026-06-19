"""导入校验和回滚回归测试"""

import json
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


class TestPreviewImport:
    """预检导入（dry-run）测试"""

    def test_preview_does_not_modify_defects(self, config, state_with_data, tmp_path):
        defects_before = dict(state_with_data.defects)
        imported_before = list(state_with_data.imported_files)
        undo_before = list(state_with_data.undo_stack)
        batch_before = state_with_data.batch_id

        csv_path = tmp_path / "valid_preview.csv"
        write_csv(csv_path, [
            "3号楼,EL-999,elevator,门机故障,critical,预检测试,2025-06-20 08:30:00,张三,1单元",
        ])

        from patrol_cli.merger import preview_import
        result = preview_import(str(csv_path), config, state_with_data, "BATCH-PREVIEW")

        assert result.valid_rows == 1
        assert result.new_defects >= 1

        assert state_with_data.defects == defects_before
        assert state_with_data.imported_files == imported_before
        assert state_with_data.undo_stack == undo_before
        assert state_with_data.batch_id == batch_before

    def test_preview_shows_new_and_merged_details(self, config, state_with_data, tmp_path):
        csv_path = tmp_path / "mixed_preview.csv"
        write_csv(csv_path, [
            "3号楼,EL-NEW,elevator,门机故障,critical,新缺陷,2025-06-20 08:30:00,张三,1单元",
        ])

        from patrol_cli.merger import preview_import
        result = preview_import(str(csv_path), config, state_with_data, "BATCH-PREVIEW")

        assert len(result.new_defect_details) == result.new_defects
        assert len(result.merged_defect_details) == result.merged_defects

        if result.new_defect_details:
            new_d = result.new_defect_details[0]
            assert "defect_id" in new_d
            assert "building" in new_d
            assert "defect_type" in new_d

    def test_preview_with_invalid_rows_does_not_fail(self, config, state_with_data, tmp_path):
        csv_path = tmp_path / "preview_with_errors.csv"
        write_csv(csv_path, [
            "1号楼,EL-OK,elevator,门机故障,critical,有效行,2025-06-20 08:30:00,张三,位置",
            ",EL-BAD,elevator,门机故障,critical,缺楼栋,2025-06-20 08:30:00,李四,位置",
        ])

        from patrol_cli.merger import preview_import
        result = preview_import(str(csv_path), config, state_with_data, "BATCH-PREVIEW")

        assert result.total_rows == 2
        assert result.valid_rows == 1
        assert len(result.invalid_rows) == 1

    def test_preview_empty_csv(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "empty.csv"
        write_csv(csv_path, [])

        from patrol_cli.merger import preview_import
        result = preview_import(str(csv_path), config, clean_state, "BATCH-PREVIEW")

        assert result.total_rows == 0
        assert result.valid_rows == 0
        assert result.new_defects == 0
        assert result.merged_defects == 0

    def test_preview_does_not_write_disk_state(self, config, temp_data_dir, tmp_path):
        state = PatrolState(data_dir=temp_data_dir)
        import_and_merge("examples/sample_data.csv", config, state, "BATCH-TEST-001")

        defects_file = Path(temp_data_dir) / "defects.json"
        undo_file = Path(temp_data_dir) / "undo_stack.json"
        meta_file = Path(temp_data_dir) / "meta.json"

        mtime_defects_before = defects_file.stat().st_mtime
        mtime_undo_before = undo_file.stat().st_mtime
        mtime_meta_before = meta_file.stat().st_mtime

        with open(defects_file, "r", encoding="utf-8") as f:
            defects_before = f.read()
        with open(undo_file, "r", encoding="utf-8") as f:
            undo_before = f.read()
        with open(meta_file, "r", encoding="utf-8") as f:
            meta_before = f.read()

        csv_path = tmp_path / "preview.csv"
        write_csv(csv_path, [
            "5号楼,EL-PRE,elevator,按钮失灵,medium,预检不写盘,2025-06-25 10:00:00,王五,位置",
        ])

        import time
        time.sleep(0.1)

        from patrol_cli.merger import preview_import
        preview_import(str(csv_path), config, state, "BATCH-PREVIEW")

        with open(defects_file, "r", encoding="utf-8") as f:
            defects_after = f.read()
        with open(undo_file, "r", encoding="utf-8") as f:
            undo_after = f.read()
        with open(meta_file, "r", encoding="utf-8") as f:
            meta_after = f.read()

        assert defects_before == defects_after
        assert undo_before == undo_after
        assert meta_before == meta_after

        assert defects_file.stat().st_mtime == mtime_defects_before
        assert undo_file.stat().st_mtime == mtime_undo_before
        assert meta_file.stat().st_mtime == mtime_meta_before


class TestImportLogs:
    """导入日志测试"""

    def test_successful_import_creates_log(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "success.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,测试成功导入,2025-06-15 08:30:00,张三,1单元",
        ])

        logs_before = len(clean_state.import_logs)
        import_and_merge(str(csv_path), config, clean_state, "BATCH-LOG-001")
        logs_after = len(clean_state.import_logs)

        assert logs_after == logs_before + 1

        last_log = clean_state.get_last_import_log("import")
        assert last_log is not None
        assert last_log.filename == "success.csv"
        assert last_log.result == "success"
        assert last_log.log_type == "import"
        assert last_log.batch_id == "BATCH-LOG-001"
        assert last_log.total_rows == 1
        assert last_log.valid_rows == 1
        assert last_log.new_defects == 1
        assert last_log.error_summary == ""

    def test_failed_import_creates_log(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "failed.csv"
        write_csv(csv_path, [
            ",EL-001,elevator,门机故障,critical,缺楼栋,2025-06-15 08:30:00,张三,1单元",
        ])

        logs_before = len(clean_state.import_logs)

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, clean_state, "BATCH-LOG-002")

        logs_after = len(clean_state.import_logs)
        assert logs_after == logs_before + 1

        last_log = clean_state.get_last_import_log("import")
        assert last_log is not None
        assert last_log.filename == "failed.csv"
        assert last_log.result == "failed"
        assert last_log.invalid_rows == 1
        assert last_log.error_summary != ""

    def test_preview_creates_log(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "preview_log.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,预检日志测试,2025-06-15 08:30:00,张三,1单元",
        ])

        from patrol_cli.merger import preview_import
        logs_before = len(clean_state.import_logs)
        preview_import(str(csv_path), config, clean_state, "BATCH-PREVIEW-LOG")
        logs_after = len(clean_state.import_logs)

        assert logs_after == logs_before + 1

        last_log = clean_state.get_last_import_log("preview")
        assert last_log is not None
        assert last_log.log_type == "preview"
        assert last_log.filename == "preview_log.csv"

    def test_logs_persist_after_restart(self, config, temp_data_dir, tmp_path):
        state = PatrolState(data_dir=temp_data_dir)

        csv_path = tmp_path / "persist.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,持久化测试,2025-06-15 08:30:00,张三,1单元",
        ])

        import_and_merge(str(csv_path), config, state, "BATCH-PERSIST")
        log_count_before = len(state.import_logs)

        state2 = PatrolState(data_dir=temp_data_dir)
        log_count_after = len(state2.import_logs)

        assert log_count_after == log_count_before
        assert state2.get_last_import_log("import") is not None
        assert state2.get_last_import_log("import").filename == "persist.csv"

    def test_get_import_logs_reverse_order(self, config, clean_state, tmp_path):
        from patrol_cli.merger import preview_import

        for i in range(3):
            csv_path = tmp_path / f"log_{i}.csv"
            write_csv(csv_path, [
                f"{i+1}号楼,EL-00{i},elevator,门机故障,critical,日志顺序测试,2025-06-{15+i} 08:30:00,张三,位置",
            ])
            preview_import(str(csv_path), config, clean_state, f"BATCH-LOG-{i}")

        logs = clean_state.get_import_logs()
        assert len(logs) == 3

        timestamps = [l.timestamp for l in logs]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_import_logs_with_limit(self, config, clean_state, tmp_path):
        from patrol_cli.merger import preview_import

        for i in range(5):
            csv_path = tmp_path / f"limit_{i}.csv"
            write_csv(csv_path, [
                f"{i+1}号楼,EL-00{i},elevator,门机故障,critical,限制测试,2025-06-{15+i} 08:30:00,张三,位置",
            ])
            preview_import(str(csv_path), config, clean_state, f"BATCH-LIMIT-{i}")

        logs = clean_state.get_import_logs(limit=2)
        assert len(logs) == 2


class TestDuplicateFiles:
    """重复文件导入测试"""

    def test_duplicate_file_rejected(self, config, state_with_data, tmp_path):
        sample_name = Path("examples/sample_data.csv").name

        csv_path = tmp_path / sample_name
        write_csv(csv_path, [
            "9号楼,EL-DUP,elevator,门机故障,critical,重复文件测试,2025-06-30 08:30:00,张三,位置",
        ])

        with pytest.raises(ValueError) as excinfo:
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-DUP")

        assert "已经导入过了" in str(excinfo.value) or "重复" in str(excinfo.value).lower()

    def test_duplicate_file_no_state_change(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()
        defects_before = dict(state_with_data.defects)
        imported_before = list(state_with_data.imported_files)

        sample_name = Path("examples/sample_data.csv").name
        csv_path = tmp_path / sample_name
        write_csv(csv_path, [
            "9号楼,EL-DUP,elevator,门机故障,critical,重复测试,2025-06-30 08:30:00,张三,位置",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-DUP")

        stats_after = state_with_data.stats()
        assert stats_after == stats_before
        assert state_with_data.defects == defects_before
        assert state_with_data.imported_files == imported_before


class TestMixedValidInvalid:
    """坏行夹着有效行测试"""

    def test_preview_mixed_valid_invalid(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "mixed.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,有效1,2025-06-15 08:30:00,张三,位置",
            ",EL-002,elevator,门机故障,critical,缺楼栋,2025-06-15 08:30:00,李四,位置",
            "3号楼,EL-003,elevator,按钮失灵,medium,有效2,2025-06-16 10:00:00,王五,位置",
            "4号楼,EL-004,bad_cat,门机故障,critical,坏类别,2025-06-17 12:00:00,赵六,位置",
        ])

        from patrol_cli.merger import preview_import
        result = preview_import(str(csv_path), config, clean_state, "BATCH-MIXED")

        assert result.total_rows == 4
        assert result.valid_rows == 2
        assert len(result.invalid_rows) == 2

        line_numbers = [item["line"] for item in result.invalid_rows]
        assert 3 in line_numbers
        assert 5 in line_numbers

    def test_import_mixed_valid_invalid_rollback(self, config, state_with_data, tmp_path):
        stats_before = state_with_data.stats()
        defects_before = dict(state_with_data.defects)

        csv_path = tmp_path / "mixed_import.csv"
        write_csv(csv_path, [
            "5号楼,EL-OK,elevator,门机故障,critical,有效行,2025-06-20 08:30:00,张三,位置",
            ",EL-BAD,elevator,门机故障,critical,坏行,2025-06-20 08:30:00,李四,位置",
        ])

        with pytest.raises(ValueError):
            import_and_merge(str(csv_path), config, state_with_data, "BATCH-MIXED-IMP")

        stats_after = state_with_data.stats()
        assert stats_after == stats_before
        assert state_with_data.defects == defects_before


class TestPreviewThenAction:
    """预检后退出或撤销测试"""

    def test_preview_then_formal_import(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "preview_then_import.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,预检后正式导入,2025-06-15 08:30:00,张三,1单元",
        ])

        from patrol_cli.merger import preview_import
        preview_result = preview_import(str(csv_path), config, clean_state, "BATCH-PI")

        assert preview_result.new_defects == 1
        assert len(clean_state.defects) == 0

        import_result = import_and_merge(str(csv_path), config, clean_state, "BATCH-PI")

        assert import_result.new_defects == 1
        assert len(clean_state.defects) == 1

    def test_preview_then_undo_does_nothing(self, config, state_with_data, tmp_path):
        csv_path = tmp_path / "preview_no_undo.csv"
        write_csv(csv_path, [
            "5号楼,EL-PRE,elevator,门机故障,critical,预检无撤销,2025-06-25 08:30:00,张三,位置",
        ])

        undo_size_before = len(state_with_data.undo_stack)

        from patrol_cli.merger import preview_import
        preview_import(str(csv_path), config, state_with_data, "BATCH-PRE-UNDO")

        undo_size_after = len(state_with_data.undo_stack)
        assert undo_size_after == undo_size_before

        from patrol_cli.workflow import undo_last
        action = undo_last(state_with_data)
        assert action is not None


class TestPostImportConsistency:
    """正式导入后 stats、list、export、日志前后一致测试"""

    def test_stats_consistent_after_import(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "consistent.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,一致性1,2025-06-15 08:30:00,张三,位置",
            "2号楼,EL-002,elevator,按钮失灵,medium,一致性2,2025-06-16 09:00:00,李四,位置",
        ])

        result = import_and_merge(str(csv_path), config, clean_state, "BATCH-CONSIST")

        stats = clean_state.stats()
        assert stats["total"] == result.new_defects
        assert stats["imported_files"] == 1

    def test_list_consistent_after_import(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "list_consistent.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,列表一致,2025-06-15 08:30:00,张三,位置",
        ])

        result = import_and_merge(str(csv_path), config, clean_state, "BATCH-LIST")

        defects = clean_state.list_defects()
        assert len(defects) == result.new_defects
        assert defects[0].defect_id == result.new_defect_details[0]["defect_id"]

    def test_export_consistent_after_import(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "export_consistent.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,导出一致,2025-06-15 08:30:00,张三,位置",
            "2号楼,EL-002,elevator,按钮失灵,medium,导出一致2,2025-06-16 09:00:00,李四,位置",
        ])

        result = import_and_merge(str(csv_path), config, clean_state, "BATCH-EXPORT")

        output_path = str(tmp_path / "export_out.csv")
        from patrol_cli.exporter import export_csv
        count = export_csv(clean_state, output_path)

        assert count == result.new_defects

    def test_log_consistent_after_import(self, config, clean_state, tmp_path):
        csv_path = tmp_path / "log_consistent.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,日志一致,2025-06-15 08:30:00,张三,位置",
        ])

        result = import_and_merge(str(csv_path), config, clean_state, "BATCH-LOG-CONSIST")

        last_log = clean_state.get_last_import_log("import")
        assert last_log is not None
        assert last_log.new_defects == result.new_defects
        assert last_log.merged_defects == result.merged_defects
        assert last_log.total_rows == result.total_rows
        assert last_log.valid_rows == result.valid_rows
        assert last_log.filename == "log_consistent.csv"
        assert last_log.batch_id == "BATCH-LOG-CONSIST"
        assert last_log.result == "success"


class TestUndoRollback:
    """撤销回滚完整性测试 - 缺陷数、已导入文件数、日志、导出全链路一致"""

    def test_undo_rollback_imported_files_and_defects(self, config, temp_data_dir, tmp_path):
        """撤销后缺陷数和已导入文件数同步回退"""
        state = PatrolState(data_dir=temp_data_dir)
        batch_id = "BATCH-TEST-UNDO"

        csv1 = tmp_path / "baseline.csv"
        write_csv(csv1, [
            "1号楼,EL-001,elevator,门机故障,critical,基线缺陷1,2025-06-15 08:30:00,张三,1单元",
            "2号楼,EL-002,elevator,按钮失灵,medium,基线缺陷2,2025-06-15 09:00:00,李四,2单元",
        ])
        import_and_merge(str(csv1), config, state, batch_id)

        stats_after_baseline = state.stats()
        assert stats_after_baseline["total"] == 2
        assert stats_after_baseline["imported_files"] == 1
        assert state.is_file_imported("baseline.csv") is True
        assert state.can_undo() is True

        csv2 = tmp_path / "second_import.csv"
        write_csv(csv2, [
            "1号楼,EL-001,elevator,门机故障,critical,新增合并到基线缺陷1,2025-06-15 10:00:00,王五,1单元",
            "3号楼,EL-003,elevator,光幕故障,high,新增缺陷3,2025-06-16 14:00:00,赵六,3单元",
        ])
        result = import_and_merge(str(csv2), config, state, batch_id)

        stats_after_second = state.stats()
        assert stats_after_second["total"] == 3
        assert stats_after_second["imported_files"] == 2
        assert state.is_file_imported("second_import.csv") is True
        assert result.new_defects == 1
        assert result.merged_defects == 1

        from patrol_cli.workflow import undo_last
        action = undo_last(state)
        assert action == "导入文件 second_import.csv"

        stats_after_undo = state.stats()
        assert stats_after_undo["total"] == 2, f"撤销后缺陷数应为2，实际为{stats_after_undo['total']}"
        assert stats_after_undo["imported_files"] == 1, f"撤销后已导入文件数应为1，实际为{stats_after_undo['imported_files']}"
        assert state.is_file_imported("baseline.csv") is True
        assert state.is_file_imported("second_import.csv") is False, "撤销后second_import.csv不应再标记为已导入"
        assert state.can_undo() is True

        state2 = PatrolState(data_dir=temp_data_dir)
        stats_reload = state2.stats()
        assert stats_reload["total"] == 2
        assert stats_reload["imported_files"] == 1
        assert state2.is_file_imported("second_import.csv") is False

    def test_undo_then_reimport_same_file(self, config, temp_data_dir, tmp_path):
        """撤销后可以重新导入同一份CSV"""
        state = PatrolState(data_dir=temp_data_dir)
        batch_id = "BATCH-TEST-REIMPORT"

        csv_path = tmp_path / "reimport.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,测试撤销重导,2025-06-15 08:30:00,张三,1单元",
        ])

        result1 = import_and_merge(str(csv_path), config, state, batch_id)
        assert result1.new_defects == 1
        assert state.is_file_imported("reimport.csv") is True

        from patrol_cli.workflow import undo_last
        undo_last(state)
        assert state.is_file_imported("reimport.csv") is False

        result2 = import_and_merge(str(csv_path), config, state, batch_id)
        assert result2.new_defects == 1
        assert state.is_file_imported("reimport.csv") is True

        stats = state.stats()
        assert stats["total"] == 1
        assert stats["imported_files"] == 1

    def test_undo_rollback_log_and_export_consistency(self, config, temp_data_dir, tmp_path):
        """撤销后导入日志、导出结果与回滚状态一致"""
        state = PatrolState(data_dir=temp_data_dir)
        batch_id = "BATCH-TEST-LOG"

        csv1 = tmp_path / "log_test1.csv"
        write_csv(csv1, [
            "1号楼,EL-001,elevator,门机故障,critical,日志测试1,2025-06-15 08:30:00,张三,1单元",
        ])
        import_and_merge(str(csv1), config, state, batch_id)

        csv2 = tmp_path / "log_test2.csv"
        write_csv(csv2, [
            "2号楼,EL-002,elevator,按钮失灵,medium,日志测试2,2025-06-15 09:00:00,李四,2单元",
        ])
        import_and_merge(str(csv2), config, state, batch_id)

        logs_before = state.get_import_logs()
        assert len([l for l in logs_before if l.log_type == "import"]) == 2

        last_log_before = state.get_last_import_log("import")
        assert last_log_before.filename == "log_test2.csv"

        from patrol_cli.workflow import undo_last
        undo_last(state)

        logs_after = state.get_import_logs()
        assert len([l for l in logs_after if l.log_type == "import"]) == 1, \
            f"撤销后日志应回退，期望1条import日志，实际{len([l for l in logs_after if l.log_type == 'import'])}条"

        last_log = state.get_last_import_log("import")
        assert last_log is not None
        assert last_log.filename == "log_test1.csv", \
            f"撤销后最近导入日志应为log_test1.csv，实际为{last_log.filename}"

        from patrol_cli.exporter import export_html
        html_path = tmp_path / "export_after_undo.html"
        export_html(state, str(html_path), config)

        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        assert "last-import" in html_content
        assert "log_test1.csv" in html_content, "HTML导出应显示回退后的最近导入文件"
        last_import_log = state.get_last_import_log("import")
        assert last_import_log is not None
        assert last_import_log.filename == "log_test1.csv", \
            f"撤销后最近导入日志应为log_test1.csv，实际为{last_import_log.filename}"
        assert batch_id in html_content

        stats = state.stats()
        assert stats["total"] == 1
        assert stats["imported_files"] == 1

    def test_undo_rollback_duplicate_check_after_reimport(self, config, temp_data_dir, tmp_path):
        """重新导入后重复导入判断正确生效"""
        state = PatrolState(data_dir=temp_data_dir)
        batch_id = "BATCH-TEST-DUP"

        csv_path = tmp_path / "dup_test.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,重复测试,2025-06-15 08:30:00,张三,1单元",
        ])

        import_and_merge(str(csv_path), config, state, batch_id)
        assert state.is_file_imported("dup_test.csv") is True

        from patrol_cli.workflow import undo_last
        undo_last(state)
        assert state.is_file_imported("dup_test.csv") is False

        import_and_merge(str(csv_path), config, state, batch_id)
        assert state.is_file_imported("dup_test.csv") is True

        with pytest.raises(ValueError, match="已经导入过了"):
            import_and_merge(str(csv_path), config, state, batch_id)

        stats = state.stats()
        assert stats["total"] == 1
        assert stats["imported_files"] == 1

    def test_undo_full_chain_rollback(self, config, temp_data_dir, tmp_path):
        """回归测试：撤销后缺陷数、已导入文件数、最近导入信息、import-log、导出结果和用户可见提示全部一致回退"""
        state = PatrolState(data_dir=temp_data_dir)
        batch_id = "BATCH-CHAIN"

        csv1 = tmp_path / "baseline.csv"
        write_csv(csv1, [
            "1号楼,EL-001,elevator,门机故障,critical,基线缺陷1,2025-06-15 08:30:00,张三,1单元",
            "2号楼,EL-002,elevator,按钮失灵,medium,基线缺陷2,2025-06-15 09:00:00,李四,2单元",
        ])
        import_and_merge(str(csv1), config, state, batch_id)

        stats_baseline = state.stats()
        assert stats_baseline["total"] == 2
        assert stats_baseline["imported_files"] == 1

        import_logs_baseline = len([l for l in state.import_logs if l.log_type == "import"])
        last_import_baseline = state.get_last_import_log("import")
        assert last_import_baseline.filename == "baseline.csv"

        from patrol_cli.exporter import export_html
        html_baseline_path = tmp_path / "baseline.html"
        export_html(state, str(html_baseline_path), config)
        with open(html_baseline_path, "r", encoding="utf-8") as f:
            html_baseline = f.read()
        assert "baseline.csv" in html_baseline

        csv2 = tmp_path / "second.csv"
        write_csv(csv2, [
            "1号楼,EL-001,elevator,门机故障,critical,合并到基线缺陷1,2025-06-15 10:00:00,王五,1单元",
            "3号楼,EL-003,elevator,光幕故障,high,新增缺陷3,2025-06-16 14:00:00,赵六,3单元",
        ])
        result = import_and_merge(str(csv2), config, state, batch_id)
        assert result.new_defects == 1
        assert result.merged_defects == 1

        stats_after = state.stats()
        assert stats_after["total"] == 3
        assert stats_after["imported_files"] == 2
        assert state.is_file_imported("second.csv") is True

        import_logs_after = len([l for l in state.import_logs if l.log_type == "import"])
        assert import_logs_after == import_logs_baseline + 1

        last_import_after = state.get_last_import_log("import")
        assert last_import_after.filename == "second.csv"

        from patrol_cli.workflow import undo_last
        action = undo_last(state)
        assert "second.csv" in action

        stats_undo = state.stats()
        assert stats_undo["total"] == stats_baseline["total"], \
            f"撤销后缺陷数应回到{stats_baseline['total']}，实际为{stats_undo['total']}"
        assert stats_undo["imported_files"] == stats_baseline["imported_files"], \
            f"撤销后已导入文件数应回到{stats_baseline['imported_files']}，实际为{stats_undo['imported_files']}"
        assert state.is_file_imported("second.csv") is False
        assert state.is_file_imported("baseline.csv") is True

        import_logs_undo = len([l for l in state.import_logs if l.log_type == "import"])
        assert import_logs_undo == import_logs_baseline, \
            f"撤销后import日志数应回到{import_logs_baseline}，实际为{import_logs_undo}"

        last_import_undo = state.get_last_import_log("import")
        assert last_import_undo.filename == "baseline.csv", \
            f"撤销后最近导入信息应为baseline.csv，实际为{last_import_undo.filename}"

        html_undo_path = tmp_path / "after_undo.html"
        export_html(state, str(html_undo_path), config)
        with open(html_undo_path, "r", encoding="utf-8") as f:
            html_undo = f.read()
        assert "baseline.csv" in html_undo, "撤销后HTML导出应显示baseline.csv"
        last_import_undo = state.get_last_import_log("import")
        assert last_import_undo.filename == "baseline.csv", \
            f"撤销后最近导入信息应为baseline.csv，实际为{last_import_undo.filename}"

        result2 = import_and_merge(str(csv2), config, state, batch_id)
        assert result2.new_defects == 1
        assert result2.merged_defects == 1
        assert state.is_file_imported("second.csv") is True

        with pytest.raises(ValueError, match="已经导入过了"):
            import_and_merge(str(csv2), config, state, batch_id)

        state_reload = PatrolState(data_dir=temp_data_dir)
        assert state_reload.stats()["total"] == 3
        assert state_reload.stats()["imported_files"] == 2
        assert state_reload.is_file_imported("second.csv") is True
        last_import_reload = state_reload.get_last_import_log("import")
        assert last_import_reload.filename == "second.csv"


class TestReviewLogs:
    """复核日志测试"""

    def test_single_review_creates_log(self, config, state_with_data):
        """单条复核产生日志"""
        defect = state_with_data.list_defects()[0]
        defect_id = defect.defect_id

        from patrol_cli.workflow import review_defect
        logs_before = len(state_with_data.review_logs)

        review_defect(state_with_data, defect_id, "dispatched", handler="张三", remark="测试复核")

        logs_after = len(state_with_data.review_logs)
        assert logs_after == logs_before + 1

        last_log = state_with_data.get_last_review_log(defect_id=defect_id)
        assert last_log is not None
        assert last_log.log_type == "review"
        assert last_log.defect_id == defect_id
        assert last_log.from_status == "pending"
        assert last_log.to_status == "dispatched"
        assert last_log.handler == "张三"
        assert last_log.remark == "测试复核"
        assert last_log.batch_id == state_with_data.batch_id
        assert last_log.timestamp != ""

    def test_batch_review_creates_logs(self, config, state_with_data):
        """批量复核产生多条日志"""
        defects = state_with_data.list_defects()
        defect_ids = [d.defect_id for d in defects[:3]]

        from patrol_cli.workflow import batch_review
        logs_before = len(state_with_data.review_logs)

        success_count, errors = batch_review(
            state_with_data, defect_ids, "dispatched",
            handler="李四", remark="批量测试"
        )

        assert success_count == 3
        assert len(errors) == 0

        logs_after = len(state_with_data.review_logs)
        assert logs_after == logs_before + 3

        for defect_id in defect_ids:
            log = state_with_data.get_last_review_log(defect_id=defect_id)
            assert log is not None
            assert log.log_type == "batch_review"
            assert log.handler == "李四"
            assert log.remark == "批量测试"
            assert log.parent_log_id != ""

        parent_ids = set()
        for log in state_with_data.review_logs[-3:]:
            parent_ids.add(log.parent_log_id)
        assert len(parent_ids) == 1

    def test_undo_creates_log(self, config, state_with_data):
        """撤销操作产生日志"""
        defect = state_with_data.list_defects()[0]
        defect_id = defect.defect_id

        from patrol_cli.workflow import review_defect, undo_last

        review_defect(state_with_data, defect_id, "dispatched")
        logs_before_undo = len(state_with_data.review_logs)

        action = undo_last(state_with_data)
        assert action is not None

        logs_after_undo = len(state_with_data.review_logs)
        assert logs_after_undo == logs_before_undo + 1

        last_log = state_with_data.get_review_logs(limit=1)[0]
        assert last_log.log_type == "undo"
        assert "撤销操作" in last_log.remark

    def test_undo_single_review_log_has_full_info(self, config, state_with_data):
        """撤销单条复核：undo日志带完整缺陷信息（缺陷ID、状态变更、处理人、备注"""
        defects = state_with_data.list_defects()
        defect_id = defects[0].defect_id

        from patrol_cli.workflow import review_defect, undo_last

        review_defect(state_with_data, defect_id, "dispatched",
                       handler="测试员A", remark="测试复核")

        undo_last(state_with_data)

        undo_logs = state_with_data.get_review_logs(log_type="undo")
        assert len(undo_logs) >= 1

        undo_log = undo_logs[0]
        assert undo_log.defect_id == defect_id
        assert undo_log.from_status == "dispatched"
        assert undo_log.to_status == "pending"
        assert undo_log.handler == "测试员A"
        assert "撤销操作" in undo_log.remark

    def test_undo_batch_review_creates_log_per_defect(self, config, state_with_data):
        """撤销批量复核：每个缺陷各有一条undo日志，都带完整信息"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]

        from patrol_cli.workflow import batch_review, undo_last

        batch_review(state_with_data, defect_ids, "closed",
                     handler="批量员B", remark="批量关闭")

        undo_logs_before = len(state_with_data.get_review_logs(log_type="undo"))
        undo_last(state_with_data)
        undo_logs_after = state_with_data.get_review_logs(log_type="undo")

        assert len(undo_logs_after) == undo_logs_before + 3

        defect_ids_set = set(defect_ids)
        undo_defect_ids = {log.defect_id for log in undo_logs_after[:3]}
        assert undo_defect_ids == defect_ids_set

        for log in undo_logs_after[:3]:
            assert log.from_status == "closed"
            assert log.to_status == "pending"
            assert log.handler == "批量员B"
            assert "撤销操作" in log.remark

    def test_undo_does_not_delete_existing_review_logs(self, config, state_with_data):
        """撤销不删除已写入的复核日志：单条→批量→撤销后，所有历史都还在"""
        defects = state_with_data.list_defects()
        d1 = defects[0].defect_id
        d2 = defects[1].defect_id
        d3 = defects[2].defect_id if len(defects) > 2 else d2

        from patrol_cli.workflow import review_defect, batch_review, undo_last

        review_defect(state_with_data, d1, "dispatched", handler="A", remark="单条复核")
        logs_after_single = len(state_with_data.review_logs)

        batch_review(state_with_data, [d2, d3], "closed", handler="B", remark="批量复核")
        logs_after_batch = len(state_with_data.review_logs)
        assert logs_after_batch == logs_after_single + 2

        undo_last(state_with_data)
        logs_after_undo = len(state_with_data.review_logs)
        assert logs_after_undo == logs_after_batch + 2

        single_logs = state_with_data.get_review_logs(log_type="review")
        assert len(single_logs) >= 1, "单条复核日志不应被撤销删除"

        batch_logs = state_with_data.get_review_logs(log_type="batch_review")
        assert len(batch_logs) >= 2, "批量复核日志不应被撤销删除"

        all_logs = state_with_data.get_review_logs()
        log_types_ordered = [l.log_type for l in all_logs]
        assert log_types_ordered.count("undo") >= 2
        assert log_types_ordered.count("batch_review") >= 2
        assert log_types_ordered.count("review") >= 1

    def test_single_defect_history_chain_complete_after_undo(self, config, state_with_data):
        """单缺陷历史链路完整：单条复核→批量复核→撤销后，该缺陷所有历史仍可按顺序追溯"""
        defects = state_with_data.list_defects()
        d = defects[0].defect_id

        from patrol_cli.workflow import review_defect, batch_review, undo_last

        review_defect(state_with_data, d, "dispatched", handler="张工", remark="第一次单条")

        batch_review(state_with_data, [d], "closed", handler="李组", remark="第二次批量")

        undo_logs_before = len(state_with_data.get_review_logs(defect_id=d, log_type="undo"))
        undo_last(state_with_data)

        defect_logs = state_with_data.get_review_logs(defect_id=d)

        assert len(defect_logs) == 3, f"该缺陷应有3条历史(单条+批量+撤销)，实际{len(defect_logs)}条"

        log_types = [l.log_type for l in defect_logs]
        assert log_types[0] == "undo", "最新的应该是撤销(倒序)"
        assert log_types[1] == "batch_review", "然后是批量复核"
        assert log_types[2] == "review", "最早的是单条复核"

        undo_log = defect_logs[0]
        assert undo_log.from_status == "closed"
        assert undo_log.to_status == "dispatched"
        assert undo_log.handler == "李组"
        assert "撤销操作" in undo_log.remark

        batch_log = defect_logs[1]
        assert batch_log.from_status == "dispatched"
        assert batch_log.to_status == "closed"
        assert batch_log.handler == "李组"
        assert batch_log.remark == "第二次批量"

        single_log = defect_logs[2]
        assert single_log.from_status == "pending"
        assert single_log.to_status == "dispatched"
        assert single_log.handler == "张工"
        assert single_log.remark == "第一次单条"

    def test_logs_persist_after_restart(self, config, temp_data_dir, tmp_path):
        """复核日志重启后可查"""
        state = PatrolState(data_dir=temp_data_dir)
        csv_path = tmp_path / "persist_review.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,持久化测试,2025-06-15 08:30:00,张三,1单元",
        ])
        import_and_merge(str(csv_path), config, state, "BATCH-REVIEW-PERSIST")

        defect_id = state.list_defects()[0].defect_id

        from patrol_cli.workflow import review_defect
        review_defect(state, defect_id, "dispatched", handler="王五", remark="重启测试")

        log_count_before = len(state.review_logs)

        state2 = PatrolState(data_dir=temp_data_dir)
        log_count_after = len(state2.review_logs)

        assert log_count_after == log_count_before
        last_log = state2.get_last_review_log(defect_id=defect_id)
        assert last_log is not None
        assert last_log.handler == "王五"
        assert last_log.remark == "重启测试"
        assert last_log.batch_id == "BATCH-REVIEW-PERSIST"

    def test_get_review_logs_filter_by_defect_id(self, config, state_with_data):
        """按缺陷编号筛选复核日志"""
        defects = state_with_data.list_defects()
        d1 = defects[0].defect_id
        d2 = defects[1].defect_id

        from patrol_cli.workflow import review_defect
        review_defect(state_with_data, d1, "dispatched")
        review_defect(state_with_data, d2, "false_positive")

        logs_d1 = state_with_data.get_review_logs(defect_id=d1)
        assert len(logs_d1) >= 1
        for log in logs_d1:
            assert log.defect_id == d1

        logs_d2 = state_with_data.get_review_logs(defect_id=d2)
        assert len(logs_d2) >= 1
        for log in logs_d2:
            assert log.defect_id == d2

    def test_get_review_logs_filter_by_handler(self, config, state_with_data):
        """按处理人筛选复核日志"""
        defects = state_with_data.list_defects()

        from patrol_cli.workflow import review_defect
        review_defect(state_with_data, defects[0].defect_id, "dispatched", handler="赵六")
        review_defect(state_with_data, defects[1].defect_id, "false_positive", handler="钱七")

        logs_zhao = state_with_data.get_review_logs(handler="赵六")
        assert len(logs_zhao) >= 1
        for log in logs_zhao:
            assert log.handler == "赵六"

    def test_get_review_logs_filter_by_type(self, config, state_with_data):
        """按操作类型筛选复核日志"""
        defects = state_with_data.list_defects()
        d1 = defects[0].defect_id
        d2 = defects[1].defect_id
        d3 = defects[2].defect_id if len(defects) > 2 else d1

        from patrol_cli.workflow import review_defect, batch_review
        review_defect(state_with_data, d1, "dispatched")
        batch_review(state_with_data, [d2, d3], "false_positive")

        single_logs = state_with_data.get_review_logs(log_type="review")
        batch_logs = state_with_data.get_review_logs(log_type="batch_review")

        assert len(single_logs) >= 1
        for log in single_logs:
            assert log.log_type == "review"

        assert len(batch_logs) >= 2
        for log in batch_logs:
            assert log.log_type == "batch_review"

    def test_get_review_logs_reverse_order(self, config, state_with_data):
        """复核日志默认倒序"""
        defects = state_with_data.list_defects()[:3]

        from patrol_cli.workflow import review_defect
        for i, d in enumerate(defects):
            review_defect(state_with_data, d.defect_id, "dispatched", handler=f"测试{i}")

        logs = state_with_data.get_review_logs()
        timestamps = [l.timestamp for l in logs]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_review_logs_with_limit(self, config, state_with_data):
        """复核日志限制条数"""
        defects = state_with_data.list_defects()[:5]

        from patrol_cli.workflow import review_defect
        for d in defects:
            review_defect(state_with_data, d.defect_id, "dispatched")

        logs = state_with_data.get_review_logs(limit=2)
        assert len(logs) == 2


class TestBatchReviewAtomic:
    """批量复核原子性测试 - 有错误则整批失败，不留脏数据"""

    def test_batch_with_nonexistent_defect_fails_completely(self, config, state_with_data):
        """批量复核包含不存在的缺陷，整批失败"""
        defects = state_with_data.list_defects()
        valid_id = defects[0].defect_id
        fake_id = "DEF-NOTEXIST-0000"

        defects_before = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        logs_before = len(state_with_data.review_logs)
        undo_before = len(state_with_data.undo_stack)

        from patrol_cli.workflow import batch_review
        success_count, errors = batch_review(
            state_with_data, [valid_id, fake_id], "dispatched"
        )

        assert success_count == 0
        assert len(errors) >= 1
        assert any("不存在" in e for e in errors)

        defects_after = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        assert defects_before == defects_after

        assert len(state_with_data.review_logs) == logs_before
        assert len(state_with_data.undo_stack) == undo_before

    def test_batch_with_duplicate_defect_fails_completely(self, config, state_with_data):
        """批量复核包含重复编号，整批失败"""
        defects = state_with_data.list_defects()
        defect_id = defects[0].defect_id

        defects_before = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        logs_before = len(state_with_data.review_logs)

        from patrol_cli.workflow import batch_review
        success_count, errors = batch_review(
            state_with_data, [defect_id, defect_id], "dispatched"
        )

        assert success_count == 0
        assert len(errors) >= 1
        assert any("重复" in e for e in errors)

        defects_after = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        assert defects_before == defects_after
        assert len(state_with_data.review_logs) == logs_before

    def test_batch_with_invalid_transition_fails_completely(self, config, state_with_data):
        """批量复核包含不可转换的状态，整批失败"""
        defects = state_with_data.list_defects()
        d1 = defects[0].defect_id
        d2 = defects[1].defect_id

        from patrol_cli.workflow import review_defect, batch_review
        review_defect(state_with_data, d1, "closed")

        defects_before = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        logs_before = len(state_with_data.review_logs)

        success_count, errors = batch_review(
            state_with_data, [d1, d2], "false_positive"
        )

        assert success_count == 0
        assert len(errors) >= 1
        assert any("不允许" in e for e in errors)

        defects_after = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        assert defects_before == defects_after

    def test_batch_all_valid_succeeds(self, config, state_with_data):
        """全部有效则批量成功"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]

        from patrol_cli.workflow import batch_review
        success_count, errors = batch_review(
            state_with_data, defect_ids, "dispatched", handler="批量员"
        )

        assert success_count == 3
        assert len(errors) == 0

        for defect_id in defect_ids:
            d = state_with_data.get_defect(defect_id)
            assert d.status == "dispatched"
            assert d.handler == "批量员"

    def test_batch_failure_leaves_undo_stack_clean(self, config, state_with_data):
        """批量复核失败：撤销栈无脏数据"""
        defects = state_with_data.list_defects()
        valid_id = defects[0].defect_id
        fake_id = "DEF-NOTEXIST-0000"

        undo_before = len(state_with_data.undo_stack)

        from patrol_cli.workflow import batch_review
        success_count, errors = batch_review(
            state_with_data, [valid_id, fake_id], "dispatched"
        )

        assert success_count == 0
        assert len(errors) >= 1
        assert len(state_with_data.undo_stack) == undo_before

    def test_batch_failure_leaves_review_logs_clean(self, config, state_with_data):
        """批量复核失败：复核日志无脏数据"""
        defects = state_with_data.list_defects()
        valid_id = defects[0].defect_id
        fake_id = "DEF-NOTEXIST-0000"

        logs_before = len(state_with_data.review_logs)

        from patrol_cli.workflow import batch_review
        success_count, errors = batch_review(
            state_with_data, [valid_id, fake_id], "dispatched"
        )

        assert success_count == 0
        assert len(errors) >= 1
        assert len(state_with_data.review_logs) == logs_before


class TestExportCsvDetailReviews:
    """csv-detail 导出复核记录测试"""

    def test_csv_detail_has_multiple_review_columns(self, config, state_with_data, tmp_path):
        """csv-detail 导出：包含最近5条复核记录的列"""
        from patrol_cli.exporter import export_csv_with_sources

        output = tmp_path / "detail_reviews.csv"
        count = export_csv_with_sources(state_with_data, str(output))
        assert count > 0

        with open(output, "r", encoding="utf-8-sig") as f:
            header_line = f.readline().strip()

        assert "最近复核1_时间" in header_line
        assert "最近复核1_状态变更" in header_line
        assert "最近复核1_处理人" in header_line
        assert "最近复核1_备注" in header_line
        assert "最近复核1_类型" in header_line
        assert "最近复核5_时间" in header_line
        assert "最近复核5_类型" in header_line

    def test_csv_detail_contains_review_history_chain(self, config, state_with_data, tmp_path):
        """csv-detail 导出：能看到多次复核的完整链路"""
        defects = state_with_data.list_defects()
        d = defects[0]

        from patrol_cli.workflow import review_defect
        review_defect(state_with_data, d.defect_id, "dispatched",
                       handler="A", remark="第一次复核")
        review_defect(state_with_data, d.defect_id, "closed",
                       handler="B", remark="第二次复核")

        from patrol_cli.exporter import export_csv_with_sources
        output = tmp_path / "detail_chain.csv"
        export_csv_with_sources(state_with_data, str(output))

        with open(output, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        data_line = lines[1].strip()
        assert "第一次复核" in data_line
        assert "第二次复核" in data_line
        assert "A" in data_line
        assert "B" in data_line
        assert "单条复核" in data_line


class TestReviewFullChain:
    """全链路测试：导入 -> 复核 -> 撤销 -> 重启查询 -> 导出"""

    def test_full_chain_import_review_undo_restart_export(self, config, temp_data_dir, tmp_path):
        """全链路回归测试"""
        state = PatrolState(data_dir=temp_data_dir)

        csv1 = tmp_path / "full_chain.csv"
        write_csv(csv1, [
            "1号楼,EL-001,elevator,门机故障,critical,全链路测试1,2025-06-15 08:30:00,张三,1单元",
            "2号楼,EL-002,elevator,按钮失灵,medium,全链路测试2,2025-06-16 09:00:00,李四,2单元",
            "3号楼,EL-003,elevator,光幕故障,high,全链路测试3,2025-06-17 10:00:00,王五,3单元",
        ])
        import_result = import_and_merge(str(csv1), config, state, "BATCH-FULL-CHAIN")
        assert import_result.new_defects == 3

        defect_ids = [d.defect_id for d in state.list_defects()]
        d1, d2, d3 = defect_ids[0], defect_ids[1], defect_ids[2]

        from patrol_cli.workflow import review_defect, batch_review, undo_last

        review_defect(state, d1, "dispatched", handler="复核员A", remark="单条复核")

        batch_review(state, [d2, d3], "closed", handler="复核员B", remark="批量关闭")

        assert state.get_defect(d1).status == "dispatched"
        assert state.get_defect(d2).status == "closed"
        assert state.get_defect(d3).status == "closed"

        review_logs = state.get_review_logs()
        assert len(review_logs) >= 3

        undo_last(state)
        assert state.get_defect(d2).status == "pending"
        assert state.get_defect(d3).status == "pending"

        undo_logs = state.get_review_logs(log_type="undo")
        assert len(undo_logs) >= 1

        batch_logs_after_undo = state.get_review_logs(log_type="batch_review")
        assert len(batch_logs_after_undo) >= 2, "撤销后批量复核日志不应被删除"

        single_logs_after_undo = state.get_review_logs(log_type="review")
        assert len(single_logs_after_undo) >= 1, "撤销后单条复核日志不应被删除"

        state_reload = PatrolState(data_dir=temp_data_dir)
        assert state_reload.get_defect(d1).status == "dispatched"
        assert state_reload.get_defect(d2).status == "pending"
        assert len(state_reload.review_logs) == len(state.review_logs)

        from patrol_cli.exporter import export_html, export_csv_with_sources

        html_path = tmp_path / "full_chain.html"
        html_count = export_html(state_reload, str(html_path), config)
        assert html_count > 0

        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        assert "最近复核记录" in html_content
        assert "复核员A" in html_content
        assert "单条复核" in html_content

        csv_path = tmp_path / "full_chain_detail.csv"
        csv_count = export_csv_with_sources(state_reload, str(csv_path))
        assert csv_count > 0

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            csv_content = f.read()
        assert "最近复核1_时间" in csv_content
        assert "最近复核1_状态变更" in csv_content
        assert "最近复核1_处理人" in csv_content
        assert "最近复核1_备注" in csv_content
        assert "最近复核5_时间" in csv_content
        assert "复核员A" in csv_content
        assert "单条复核" in csv_content


def write_defect_ids_csv(path, defect_ids):
    """写入缺陷ID列表CSV"""
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("缺陷ID\n")
        for did in defect_ids:
            f.write(f"{did}\n")


class TestDraftCreate:
    """草稿创建测试"""

    def test_create_draft_from_ids(self, config, state_with_data):
        """从ID列表创建草稿"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]
        ids_str = ",".join(defect_ids)

        from patrol_cli.workflow import create_draft

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched",
            name="测试草稿", handler="张工", remark="批量派单测试", created_by="testuser"
        )

        assert draft.draft_id.startswith("DRAFT-")
        assert draft.name == "测试草稿"
        assert draft.target_status == "dispatched"
        assert draft.handler == "张工"
        assert draft.remark == "批量派单测试"
        assert draft.created_by == "testuser"
        assert draft.status == "pending"
        assert len(draft.items) == 3
        assert draft.source_type == "ids"

        for item, defect in zip(draft.items, defects):
            assert item.defect_id == defect.defect_id
            assert item.target_status == "dispatched"
            assert item.defect_snapshot["status"] == defect.status

    def test_create_draft_from_csv(self, config, state_with_data, tmp_path):
        """从CSV创建草稿"""
        defects = state_with_data.list_defects()[:2]
        defect_ids = [d.defect_id for d in defects]

        csv_path = tmp_path / "defect_ids.csv"
        write_defect_ids_csv(csv_path, defect_ids)

        from patrol_cli.workflow import create_draft

        draft = create_draft(
            state_with_data, str(csv_path), "csv", "closed",
            name="CSV草稿", handler="李工"
        )

        assert draft.draft_id.startswith("DRAFT-")
        assert draft.name == "CSV草稿"
        assert draft.target_status == "closed"
        assert draft.handler == "李工"
        assert draft.status == "pending"
        assert len(draft.items) == 2
        assert draft.source_type == "csv"

    def test_create_draft_deduplicates_ids(self, config, state_with_data):
        """创建草稿时自动去重重复ID"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id
        ids_str = f"{d1},{d2},{d1}"

        from patrol_cli.workflow import create_draft

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        assert len(draft.items) == 2

    def test_create_draft_rejects_nonexistent(self, config, state_with_data):
        """创建草稿时如果所有ID都不存在则失败"""
        fake_ids = "DEF-NOTEXIST-001,DEF-NOTEXIST-002"

        from patrol_cli.workflow import create_draft, WorkflowError

        with pytest.raises(WorkflowError) as excinfo:
            create_draft(state_with_data, fake_ids, "ids", "dispatched")

        assert "缺陷不存在" in str(excinfo.value)

    def test_create_draft_invalid_status_rejected(self, config, state_with_data):
        """创建草稿时无效状态被拒绝"""
        defects = state_with_data.list_defects()[:1]
        ids_str = defects[0].defect_id

        from patrol_cli.workflow import create_draft, WorkflowError

        with pytest.raises(WorkflowError) as excinfo:
            create_draft(state_with_data, ids_str, "ids", "invalid_status")

        assert "无效的目标状态" in str(excinfo.value)

    def test_create_draft_persists_on_disk(self, config, temp_data_dir, tmp_path):
        """草稿创建后持久化到磁盘，重启后仍在"""
        state = PatrolState(data_dir=temp_data_dir)

        csv_path = tmp_path / "persist_data.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,测试,2025-06-15 08:30:00,张三,1单元",
            "2号楼,EL-002,elevator,按钮失灵,medium,测试,2025-06-16 09:00:00,李四,2单元",
        ])
        import_and_merge(str(csv_path), config, state, "BATCH-DRAFT-PERSIST")

        defect_ids = [d.defect_id for d in state.list_defects()]
        ids_str = ",".join(defect_ids)

        from patrol_cli.workflow import create_draft

        draft = create_draft(
            state, ids_str, "ids", "dispatched", name="持久化草稿"
        )
        draft_id = draft.draft_id

        state2 = PatrolState(data_dir=temp_data_dir)
        loaded_draft = state2.get_draft(draft_id)

        assert loaded_draft is not None
        assert loaded_draft.draft_id == draft_id
        assert loaded_draft.name == "持久化草稿"
        assert loaded_draft.status == "pending"
        assert len(loaded_draft.items) == 2

    def test_create_draft_with_some_nonexistent_skips_them(self, config, state_with_data):
        """创建草稿时部分ID不存在则跳过，只保留有效的"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id
        fake_id = "DEF-NOTEXIST-001"
        ids_str = f"{d1},{fake_id},{d2}"

        from patrol_cli.workflow import create_draft

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        assert len(draft.items) == 2
        item_ids = [item.defect_id for item in draft.items]
        assert d1 in item_ids
        assert d2 in item_ids
        assert fake_id not in item_ids

    def test_create_draft_saves_defect_snapshot(self, config, state_with_data):
        """创建草稿时保存缺陷快照"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import review_defect, create_draft

        review_defect(state_with_data, d1, "dispatched", handler="先改一条")

        ids_str = f"{d1},{d2}"
        draft = create_draft(
            state_with_data, ids_str, "ids", "closed"
        )

        items_by_id = {item.defect_id: item for item in draft.items}
        assert items_by_id[d1].defect_snapshot["status"] == "dispatched"
        assert items_by_id[d2].defect_snapshot["status"] == "pending"


class TestDraftPreview:
    """草稿预览测试"""

    def test_preview_shows_will_change(self, config, state_with_data):
        """预览显示将变更的记录"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]
        ids_str = ",".join(defect_ids)

        from patrol_cli.workflow import create_draft, preview_draft

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        preview = preview_draft(state_with_data, draft.draft_id)

        assert preview["draft_id"] == draft.draft_id
        assert len(preview["will_change"]) == 3
        assert len(preview["same_status"]) == 0
        assert len(preview["invalid_transition"]) == 0
        assert len(preview["not_found"]) == 0

        for item in preview["will_change"]:
            assert item["current_status"] == "pending"
            assert item["target_status"] == "dispatched"

    def test_preview_shows_same_status(self, config, state_with_data):
        """预览显示已是目标状态的记录"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import review_defect, create_draft, preview_draft

        review_defect(state_with_data, d1, "dispatched")

        ids_str = f"{d1},{d2}"
        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        preview = preview_draft(state_with_data, draft.draft_id)

        assert len(preview["will_change"]) == 1
        assert len(preview["same_status"]) == 1
        assert preview["same_status"][0]["defect_id"] == d1

    def test_preview_shows_invalid_transition(self, config, state_with_data):
        """预览显示状态转换不合法的记录"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import review_defect, create_draft, preview_draft

        review_defect(state_with_data, d1, "closed")

        ids_str = f"{d1},{d2}"
        draft = create_draft(
            state_with_data, ids_str, "ids", "false_positive"
        )

        preview = preview_draft(state_with_data, draft.draft_id)

        assert len(preview["will_change"]) == 1
        assert len(preview["invalid_transition"]) == 1
        assert preview["invalid_transition"][0]["defect_id"] == d1

    def test_preview_nonexistent_draft_raises(self, config, state_with_data):
        """预览不存在的草稿抛出异常"""
        from patrol_cli.workflow import preview_draft, WorkflowError

        with pytest.raises(WorkflowError) as excinfo:
            preview_draft(state_with_data, "DRAFT-NOTEXIST")

        assert "草稿不存在" in str(excinfo.value)


class TestDraftConflictDetection:
    """草稿冲突检测测试"""

    def test_detects_status_change_after_draft_created(self, config, state_with_data):
        """检测草稿创建后缺陷状态被修改的冲突"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import create_draft, review_defect, _check_draft_conflicts

        ids_str = f"{d1},{d2}"
        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        review_defect(state_with_data, d1, "false_positive")

        conflicts, duplicates, not_found, invalid = _check_draft_conflicts(state_with_data, draft)

        assert len(conflicts) == 1
        assert d1 in conflicts[0]
        assert "创建草稿时状态为pending" in conflicts[0]
        assert "当前状态为false_positive" in conflicts[0]

    def test_detects_duplicate_ids(self, config, state_with_data):
        """检测重复的缺陷编号"""
        defects = state_with_data.list_defects()[:1]
        d1 = defects[0].defect_id

        from patrol_cli.workflow import create_draft, _check_draft_conflicts
        from patrol_cli.models import DraftItem

        ids_str = d1
        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        draft.items.append(DraftItem(defect_id=d1, target_status="dispatched", defect_snapshot={}))

        conflicts, duplicates, not_found, invalid = _check_draft_conflicts(state_with_data, draft)

        assert len(duplicates) == 1
        assert d1 in duplicates

    def test_detects_nonexistent_defect(self, config, state_with_data):
        """检测不存在的缺陷"""
        defects = state_with_data.list_defects()[:1]
        d1 = defects[0].defect_id
        fake_id = "DEF-NOTEXIST-001"

        from patrol_cli.workflow import create_draft, _check_draft_conflicts
        from patrol_cli.models import DraftItem

        ids_str = d1
        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        draft.items.append(DraftItem(
            defect_id=fake_id, target_status="dispatched",
            defect_snapshot={"status": "pending"}
        ))

        conflicts, duplicates, not_found, invalid = _check_draft_conflicts(state_with_data, draft)

        assert len(not_found) == 1
        assert fake_id in not_found

    def test_detects_already_target_status(self, config, state_with_data):
        """检测已经是目标状态的缺陷"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import create_draft, review_defect, _check_draft_conflicts

        review_defect(state_with_data, d1, "dispatched")

        ids_str = f"{d1},{d2}"
        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )

        conflicts, duplicates, not_found, invalid = _check_draft_conflicts(state_with_data, draft)

        assert len(invalid) == 1
        assert d1 in invalid[0]
        assert "已经是" in invalid[0]


class TestDraftExecute:
    """草稿执行测试"""

    def test_execute_draft_success(self, config, state_with_data):
        """成功执行草稿"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]
        ids_str = ",".join(defect_ids)

        from patrol_cli.workflow import create_draft, execute_draft

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched",
            handler="执行员", remark="执行测试"
        )
        draft_id = draft.draft_id

        result = execute_draft(state_with_data, draft_id)

        assert result.success_count == 3
        assert result.executed_at != ""
        assert result.execution_id != ""

        draft_after = state_with_data.get_draft(draft_id)
        assert draft_after.status == "executed"
        assert draft_after.execution.success_count == 3

        for defect_id in defect_ids:
            assert state_with_data.get_defect(defect_id).status == "dispatched"

    def test_execute_draft_is_atomic_on_conflict(self, config, state_with_data):
        """有冲突时整批不执行，原子性保证"""
        defects = state_with_data.list_defects()[:3]
        d1, d2, d3 = [d.defect_id for d in defects]
        ids_str = ",".join([d1, d2, d3])

        from patrol_cli.workflow import create_draft, review_defect, execute_draft, WorkflowError

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )
        draft_id = draft.draft_id

        review_defect(state_with_data, d2, "closed")

        defects_before = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        logs_before = len(state_with_data.review_logs)
        undo_before = len(state_with_data.undo_stack)

        with pytest.raises(WorkflowError) as excinfo:
            execute_draft(state_with_data, draft_id)

        assert "草稿执行冲突" in str(excinfo.value)
        assert d2 in str(excinfo.value)

        defects_after = {k: v.to_dict() for k, v in state_with_data.defects.items()}
        assert defects_before == defects_after
        assert len(state_with_data.review_logs) == logs_before
        assert len(state_with_data.undo_stack) == undo_before

        draft_after = state_with_data.get_draft(draft_id)
        assert draft_after.status == "pending"

    def test_execute_draft_reports_all_conflicts_at_once(self, config, state_with_data):
        """一次报告所有冲突：重复、不存在、状态变化"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id
        fake_id = "DEF-NOTEXIST-001"

        from patrol_cli.workflow import create_draft, review_defect, execute_draft, WorkflowError
        from patrol_cli.models import DraftItem

        ids_str = f"{d1},{d2}"
        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched"
        )
        draft.items.append(DraftItem(
            defect_id=fake_id, target_status="dispatched",
            defect_snapshot={"status": "pending"}
        ))
        draft.items.append(DraftItem(
            defect_id=d1, target_status="dispatched",
            defect_snapshot={"status": "pending"}
        ))

        review_defect(state_with_data, d2, "closed")

        with pytest.raises(WorkflowError) as excinfo:
            execute_draft(state_with_data, draft.draft_id)

        error_msg = str(excinfo.value)
        assert "重复的缺陷编号" in error_msg
        assert "缺陷不存在" in error_msg
        assert "可能已被他人修改" in error_msg

    def test_cannot_execute_executed_draft(self, config, state_with_data):
        """不能重复执行已执行的草稿"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, execute_draft, WorkflowError

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        execute_draft(state_with_data, draft.draft_id)

        with pytest.raises(WorkflowError) as excinfo:
            execute_draft(state_with_data, draft.draft_id)

        assert "草稿已执行，不能重复执行" in str(excinfo.value)

    def test_cannot_execute_voided_draft(self, config, state_with_data):
        """不能执行已作废的草稿"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, void_draft, execute_draft, WorkflowError

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        void_draft(state_with_data, draft.draft_id, "不需要了")

        with pytest.raises(WorkflowError) as excinfo:
            execute_draft(state_with_data, draft.draft_id)

        assert "草稿已作废，不能执行" in str(excinfo.value)

    def test_execute_creates_draft_review_logs(self, config, state_with_data):
        """执行草稿产生类型为 draft_review 的日志，带 draft_id"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id
        ids_str = f"{d1},{d2}"

        from patrol_cli.workflow import create_draft, execute_draft

        draft = create_draft(
            state_with_data, ids_str, "ids", "closed",
            handler="草稿员", remark="草稿执行"
        )
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)

        draft_logs = state_with_data.get_review_logs(log_type="draft_review")
        assert len(draft_logs) == 2

        for log in draft_logs:
            assert log.draft_id == draft_id
            assert log.from_status == "pending"
            assert log.to_status == "closed"
            assert log.handler == "草稿员"
            assert log.remark == "草稿执行"

    def test_execute_pushes_to_undo_stack(self, config, state_with_data):
        """执行草稿推入撤销栈"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, execute_draft

        undo_before = len(state_with_data.undo_stack)

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        execute_draft(state_with_data, draft.draft_id)

        assert len(state_with_data.undo_stack) == undo_before + 1

        last_undo = state_with_data.undo_stack[-1]
        assert "执行草稿" in last_undo["action"]
        assert draft.draft_id in last_undo["action"]


class TestDraftVoid:
    """草稿作废测试"""

    def test_void_pending_draft(self, config, state_with_data):
        """作废待执行的草稿"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, void_draft

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        draft_id = draft.draft_id

        voided = void_draft(state_with_data, draft_id, "测试作废")

        assert voided.status == "voided"
        assert "测试作废" in voided.remark

        loaded = state_with_data.get_draft(draft_id)
        assert loaded.status == "voided"

    def test_cannot_void_executed_draft(self, config, state_with_data):
        """不能作废已执行的草稿"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, execute_draft, void_draft, WorkflowError

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        execute_draft(state_with_data, draft.draft_id)

        with pytest.raises(WorkflowError) as excinfo:
            void_draft(state_with_data, draft.draft_id)

        assert "草稿已执行，不能作废" in str(excinfo.value)

    def test_cannot_void_already_voided(self, config, state_with_data):
        """不能重复作废"""
        defects = state_with_data.list_defects()[:1]
        ids_str = defects[0].defect_id

        from patrol_cli.workflow import create_draft, void_draft, WorkflowError

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        void_draft(state_with_data, draft.draft_id)

        with pytest.raises(WorkflowError) as excinfo:
            void_draft(state_with_data, draft.draft_id)

        assert "草稿已作废" in str(excinfo.value)

    def test_void_persists_after_restart(self, config, temp_data_dir, tmp_path):
        """作废状态重启后仍在"""
        state = PatrolState(data_dir=temp_data_dir)
        csv_path = tmp_path / "void_data.csv"
        write_csv(csv_path, [
            "1号楼,EL-001,elevator,门机故障,critical,测试,2025-06-15 08:30:00,张三,1单元",
        ])
        import_and_merge(str(csv_path), config, state, "BATCH-VOID-TEST")

        from patrol_cli.workflow import create_draft, void_draft

        defect_id = state.list_defects()[0].defect_id
        draft = create_draft(state, defect_id, "ids", "dispatched")
        draft_id = draft.draft_id
        void_draft(state, draft_id, "重启测试作废")

        state2 = PatrolState(data_dir=temp_data_dir)
        loaded = state2.get_draft(draft_id)
        assert loaded.status == "voided"
        assert "重启测试作废" in loaded.remark


class TestDraftUndo:
    """草稿执行撤销测试"""

    def test_undo_draft_execution_rollbacks_status(self, config, state_with_data):
        """撤销草稿执行回退缺陷状态"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]
        ids_str = ",".join(defect_ids)

        from patrol_cli.workflow import create_draft, execute_draft, undo_last

        draft = create_draft(
            state_with_data, ids_str, "ids", "closed", handler="撤销测试"
        )
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)

        for did in defect_ids:
            assert state_with_data.get_defect(did).status == "closed"

        action = undo_last(state_with_data)
        assert action is not None
        assert "执行草稿" in action
        assert draft_id in action

        for did in defect_ids:
            assert state_with_data.get_defect(did).status == "pending"

    def test_undo_draft_preserves_draft_and_execution_record(self, config, state_with_data):
        """撤销草稿执行不删除草稿和执行记录"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, execute_draft, undo_last

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)
        undo_last(state_with_data)

        draft_after = state_with_data.get_draft(draft_id)
        assert draft_after is not None
        assert draft_after.status == "executed"
        assert draft_after.execution.executed_at != ""
        assert draft_after.execution.undo_execution_id != ""
        assert draft_after.execution.undo_at != ""

    def test_undo_draft_creates_undo_logs_with_draft_id(self, config, state_with_data):
        """撤销草稿执行产生的 undo 日志带 draft_id"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id
        ids_str = f"{d1},{d2}"

        from patrol_cli.workflow import create_draft, execute_draft, undo_last

        draft = create_draft(state_with_data, ids_str, "ids", "closed")
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)

        undo_logs_before = len(state_with_data.get_review_logs(log_type="undo"))
        undo_last(state_with_data)
        undo_logs_after = state_with_data.get_review_logs(log_type="undo")

        assert len(undo_logs_after) == undo_logs_before + 2

        for log in undo_logs_after[:2]:
            assert log.draft_id == draft_id
            assert log.from_status == "closed"
            assert log.to_status == "pending"

    def test_undo_draft_then_cannot_execute_again(self, config, state_with_data):
        """撤销草稿执行后不能再次执行（草稿状态仍是 executed）"""
        defects = state_with_data.list_defects()[:2]
        ids_str = ",".join([d.defect_id for d in defects])

        from patrol_cli.workflow import create_draft, execute_draft, undo_last, WorkflowError

        draft = create_draft(state_with_data, ids_str, "ids", "dispatched")
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)
        undo_last(state_with_data)

        with pytest.raises(WorkflowError) as excinfo:
            execute_draft(state_with_data, draft_id)

        assert "草稿已执行，不能重复执行" in str(excinfo.value)


class TestDraftExport:
    """草稿导出测试"""

    def test_export_draft_execution_result(self, config, state_with_data, tmp_path):
        """导出草稿执行结果"""
        defects = state_with_data.list_defects()[:3]
        defect_ids = [d.defect_id for d in defects]
        ids_str = ",".join(defect_ids)

        from patrol_cli.workflow import create_draft, execute_draft
        from patrol_cli.exporter import export_draft_csv

        draft = create_draft(
            state_with_data, ids_str, "ids", "dispatched",
            name="导出测试草稿", handler="导出员", remark="导出测试备注"
        )
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)

        output_path = tmp_path / "draft_result.csv"
        count = export_draft_csv(state_with_data, draft_id, str(output_path))

        assert count == 3

        with open(output_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        assert len(lines) == 4
        header = lines[0]
        assert "草稿ID" in header
        assert "草稿名称" in header
        assert "目标状态" in header
        assert "处理人" in header
        assert "执行时间" in header
        assert "撤销时间" in header
        assert "缺陷ID" in header
        assert "快照状态" in header
        assert "当前状态" in header

        data_content = "".join(lines[1:])
        assert draft_id in data_content
        assert "导出测试草稿" in data_content
        assert "导出员" in data_content
        assert "导出测试备注" in data_content
        assert "已派单" in data_content

    def test_export_draft_list(self, config, state_with_data, tmp_path):
        """导出草稿列表"""
        defects = state_with_data.list_defects()[:3]
        d1, d2, d3 = defects[0].defect_id, defects[1].defect_id, defects[2].defect_id

        from patrol_cli.workflow import create_draft, execute_draft, void_draft
        from patrol_cli.exporter import export_draft_list_csv

        draft1 = create_draft(state_with_data, d1, "ids", "dispatched", name="草稿1")
        execute_draft(state_with_data, draft1.draft_id)

        draft2 = create_draft(state_with_data, d2, "ids", "closed", name="草稿2")
        void_draft(state_with_data, draft2.draft_id, "作废原因")

        draft3 = create_draft(state_with_data, d3, "ids", "false_positive", name="草稿3")

        output_path = tmp_path / "draft_list.csv"
        count = export_draft_list_csv(state_with_data, str(output_path))

        assert count == 3

        with open(output_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        assert len(lines) == 4
        header = lines[0]
        assert "草稿ID" in header
        assert "名称" in header
        assert "状态" in header
        assert "目标状态" in header
        assert "创建时间" in header
        assert "执行时间" in header
        assert "撤销时间" in header
        assert "成功条数" in header

        data_content = "".join(lines[1:])
        assert "草稿1" in data_content
        assert "草稿2" in data_content
        assert "草稿3" in data_content
        assert "已执行" in data_content
        assert "已作废" in data_content
        assert "待执行" in data_content

    def test_export_draft_list_filter_by_status(self, config, state_with_data, tmp_path):
        """按状态筛选导出草稿列表"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import create_draft, execute_draft
        from patrol_cli.exporter import export_draft_list_csv

        create_draft(state_with_data, d1, "ids", "dispatched", name="草稿待执行")
        draft_executed = create_draft(state_with_data, d2, "ids", "closed", name="草稿已执行")
        execute_draft(state_with_data, draft_executed.draft_id)

        output_path = tmp_path / "draft_executed.csv"
        count = export_draft_list_csv(state_with_data, str(output_path), status="executed")

        assert count == 1

        with open(output_path, "r", encoding="utf-8-sig") as f:
            content = f.read()

        assert "草稿已执行" in content
        assert "草稿待执行" not in content


class TestDraftTraceability:
    """草稿来源追溯测试"""

    def test_review_log_shows_draft_origin(self, config, state_with_data):
        """复核日志显示草稿来源"""
        defects = state_with_data.list_defects()[:2]
        d1, d2 = defects[0].defect_id, defects[1].defect_id

        from patrol_cli.workflow import create_draft, execute_draft

        draft = create_draft(
            state_with_data, f"{d1},{d2}", "ids", "dispatched",
            name="追溯测试草稿"
        )
        draft_id = draft.draft_id

        execute_draft(state_with_data, draft_id)

        log_d1 = state_with_data.get_last_review_log(defect_id=d1)
        assert log_d1 is not None
        assert log_d1.draft_id == draft_id
        assert log_d1.log_type == "draft_review"

        log_d2 = state_with_data.get_last_review_log(defect_id=d2)
        assert log_d2 is not None
        assert log_d2.draft_id == draft_id

    def test_defect_drafts_query(self, config, state_with_data):
        """查询缺陷关联的所有草稿"""
        defects = state_with_data.list_defects()[:3]
        d1, d2, d3 = defects[0].defect_id, defects[1].defect_id, defects[2].defect_id

        from patrol_cli.workflow import create_draft

        draft1 = create_draft(state_with_data, f"{d1},{d2}", "ids", "dispatched", name="草稿A")
        draft2 = create_draft(state_with_data, f"{d2},{d3}", "ids", "closed", name="草稿B")

        drafts_for_d1 = state_with_data.get_drafts_for_defect(d1)
        assert len(drafts_for_d1) == 1
        assert drafts_for_d1[0].draft_id == draft1.draft_id

        drafts_for_d2 = state_with_data.get_drafts_for_defect(d2)
        assert len(drafts_for_d2) == 2
        draft_ids = [d.draft_id for d in drafts_for_d2]
        assert draft1.draft_id in draft_ids
        assert draft2.draft_id in draft_ids

        drafts_for_d3 = state_with_data.get_drafts_for_defect(d3)
        assert len(drafts_for_d3) == 1
        assert drafts_for_d3[0].draft_id == draft2.draft_id


class TestDraftFullChain:
    """草稿全链路回归测试"""

    def test_full_chain_create_preview_execute_undo_restart_export(self, config, temp_data_dir, tmp_path):
        """全链路：创建草稿→预览→冲突检查→执行→撤销→重启查询→导出"""
        state = PatrolState(data_dir=temp_data_dir)

        csv_data = tmp_path / "full_chain_data.csv"
        write_csv(csv_data, [
            "1号楼,EL-001,elevator,门机故障,critical,测试1,2025-06-15 08:30:00,张三,1单元",
            "2号楼,EL-002,elevator,按钮失灵,medium,测试2,2025-06-16 09:00:00,李四,2单元",
            "3号楼,EL-003,elevator,光幕故障,high,测试3,2025-06-17 10:00:00,王五,3单元",
            "4号楼,EL-004,elevator,轿厢异响,critical,测试4,2025-06-18 11:00:00,赵六,4单元",
        ])
        import_and_merge(str(csv_data), config, state, "BATCH-DRAFT-FULL")

        defects = state.list_defects()
        d1, d2, d3, d4 = [d.defect_id for d in defects]

        from patrol_cli.workflow import (
            create_draft, preview_draft, execute_draft, undo_last,
            review_defect, WorkflowError
        )

        ids_str = f"{d1},{d2},{d3}"
        draft = create_draft(
            state, ids_str, "ids", "dispatched",
            name="全链路测试草稿", handler="全链路员",
            remark="全链路测试备注", created_by="tester"
        )
        draft_id = draft.draft_id

        preview = preview_draft(state, draft_id)
        assert preview["total_items"] == 3
        assert len(preview["will_change"]) == 3
        assert len(preview["not_found"]) == 0

        review_defect(state, d2, "closed", handler="别人先改了")

        with pytest.raises(WorkflowError) as excinfo:
            execute_draft(state, draft_id)
        assert "冲突" in str(excinfo.value)

        assert state.get_defect(d1).status == "pending"
        assert state.get_defect(d2).status == "closed"
        assert state.get_defect(d3).status == "pending"

        review_defect(state, d2, "pending")

        result = execute_draft(state, draft_id)
        assert result.success_count == 3

        assert state.get_defect(d1).status == "dispatched"
        assert state.get_defect(d2).status == "dispatched"
        assert state.get_defect(d3).status == "dispatched"

        draft_after_exec = state.get_draft(draft_id)
        assert draft_after_exec.status == "executed"
        assert draft_after_exec.execution.success_count == 3

        draft_logs = state.get_review_logs(log_type="draft_review")
        assert len(draft_logs) == 3
        for log in draft_logs:
            assert log.draft_id == draft_id

        action = undo_last(state)
        assert "执行草稿" in action
        assert draft_id in action

        assert state.get_defect(d1).status == "pending"
        assert state.get_defect(d2).status == "pending"
        assert state.get_defect(d3).status == "pending"

        draft_after_undo = state.get_draft(draft_id)
        assert draft_after_undo.status == "executed"
        assert draft_after_undo.execution.undo_execution_id != ""
        assert draft_after_undo.execution.undo_at != ""

        undo_logs = state.get_review_logs(log_type="undo")
        draft_undo_logs = [log for log in undo_logs if log.draft_id == draft_id]
        assert len(draft_undo_logs) == 3

        state_reload = PatrolState(data_dir=temp_data_dir)

        draft_reload = state_reload.get_draft(draft_id)
        assert draft_reload is not None
        assert draft_reload.status == "executed"
        assert draft_reload.execution.undo_execution_id != ""
        assert draft_reload.execution.undo_at != ""
        assert draft_reload.execution.success_count == 3

        assert state_reload.get_defect(d1).status == "pending"

        drafts_for_d1 = state_reload.get_drafts_for_defect(d1)
        assert len(drafts_for_d1) == 1
        assert drafts_for_d1[0].draft_id == draft_id

        from patrol_cli.exporter import export_draft_csv, export_draft_list_csv, export_html

        result_csv = tmp_path / "full_chain_result.csv"
        count = export_draft_csv(state_reload, draft_id, str(result_csv))
        assert count == 3

        with open(result_csv, "r", encoding="utf-8-sig") as f:
            content = f.read()
        assert draft_id in content
        assert "全链路测试草稿" in content
        assert "全链路员" in content
        assert "全链路测试备注" in content

        list_csv = tmp_path / "full_chain_list.csv"
        count = export_draft_list_csv(state_reload, str(list_csv))
        assert count >= 1

        html_path = tmp_path / "full_chain.html"
        export_html(state_reload, str(html_path), config)

        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        assert "草稿复核" in html_content
        assert draft_id in html_content
        assert "全链路测试草稿" in html_content

        drafts_pending = state_reload.list_drafts(status="pending")
        drafts_executed = state_reload.list_drafts(status="executed")
        drafts_voided = state_reload.list_drafts(status="voided")

        assert len(drafts_executed) == 1
        assert drafts_executed[0].draft_id == draft_id


class TestDraftTemplates:
    """模板功能完整测试"""

    def test_create_template_success(self, config, state_with_data):
        """测试创建模板成功"""
        from patrol_cli.workflow import create_template

        template = create_template(
            state_with_data,
            name="标准派单模板",
            target_status="dispatched",
            handler="张三",
            remark="常规巡检发现，已安排处理",
            source_type="ids",
            description="用于常规派单的标准模板"
        )

        assert template.template_id.startswith("TPL-")
        assert template.name == "标准派单模板"
        assert template.target_status == "dispatched"
        assert template.handler == "张三"
        assert template.remark == "常规巡检发现，已安排处理"
        assert template.source_type == "ids"
        assert template.description == "用于常规派单的标准模板"
        assert template.created_at != ""
        assert template.updated_at != ""

        saved = state_with_data.get_template(template.template_id)
        assert saved is not None
        assert saved.name == "标准派单模板"

    def test_create_template_duplicate_name_rejected(self, config, state_with_data):
        """测试创建模板 - 同名拒绝"""
        from patrol_cli.workflow import create_template, WorkflowError

        create_template(state_with_data, name="重复名测试", target_status="dispatched")

        with pytest.raises(WorkflowError) as excinfo:
            create_template(state_with_data, name="重复名测试", target_status="closed")

        assert "模板名称已存在" in str(excinfo.value)

    def test_create_template_invalid_status_rejected(self, config, state_with_data):
        """测试创建模板 - 无效状态拒绝"""
        from patrol_cli.workflow import create_template, WorkflowError

        with pytest.raises(WorkflowError) as excinfo:
            create_template(state_with_data, name="无效状态模板", target_status="invalid")

        assert "无效的目标状态" in str(excinfo.value)

    def test_create_template_empty_name_rejected(self, config, state_with_data):
        """测试创建模板 - 空名称拒绝"""
        from patrol_cli.workflow import create_template, WorkflowError

        with pytest.raises(WorkflowError) as excinfo:
            create_template(state_with_data, name="  ", target_status="dispatched")

        assert "模板名称不能为空" in str(excinfo.value)

    def test_update_template(self, config, state_with_data):
        """测试更新模板"""
        from patrol_cli.workflow import create_template, update_template

        template = create_template(
            state_with_data,
            name="更新测试模板",
            target_status="pending",
            handler="旧处理人",
            remark="旧备注"
        )

        updated = update_template(
            state_with_data,
            template_id=template.template_id,
            name="更新后的模板名",
            target_status="closed",
            handler="新处理人",
            remark="新备注",
            description="新描述"
        )

        assert updated.name == "更新后的模板名"
        assert updated.target_status == "closed"
        assert updated.handler == "新处理人"
        assert updated.remark == "新备注"
        assert updated.description == "新描述"
        assert updated.updated_at >= template.updated_at

        saved = state_with_data.get_template(template.template_id)
        assert saved.name == "更新后的模板名"

    def test_update_template_partial_fields(self, config, state_with_data):
        """测试部分字段更新 - 未指定的字段保持不变"""
        from patrol_cli.workflow import create_template, update_template

        template = create_template(
            state_with_data,
            name="部分更新模板",
            target_status="dispatched",
            handler="保留处理人",
            remark="保留备注"
        )

        updated = update_template(
            state_with_data,
            template_id=template.template_id,
            handler="只改处理人"
        )

        assert updated.name == "部分更新模板"
        assert updated.target_status == "dispatched"
        assert updated.handler == "只改处理人"
        assert updated.remark == "保留备注"

    def test_delete_template(self, config, state_with_data):
        """测试删除模板"""
        from patrol_cli.workflow import create_template, delete_template

        template = create_template(
            state_with_data,
            name="待删除模板",
            target_status="dispatched"
        )

        assert state_with_data.get_template(template.template_id) is not None

        result = delete_template(state_with_data, template.template_id)
        assert result is True

        assert state_with_data.get_template(template.template_id) is None

    def test_delete_nonexistent_template_rejected(self, config, state_with_data):
        """测试删除不存在的模板"""
        from patrol_cli.workflow import delete_template, WorkflowError

        with pytest.raises(WorkflowError) as excinfo:
            delete_template(state_with_data, "TPL-NOTEXIST-001")

        assert "模板不存在" in str(excinfo.value)

    def test_create_draft_from_template(self, config, state_with_data):
        """测试从模板创建草稿"""
        from patrol_cli.workflow import create_template, create_draft_from_template

        template = create_template(
            state_with_data,
            name="派单标准模板",
            target_status="dispatched",
            handler="模板处理人",
            remark="模板备注内容"
        )

        defects = state_with_data.list_defects(status="pending")
        defect_ids = [d.defect_id for d in defects[:3]]
        ids_str = ",".join(defect_ids)

        draft = create_draft_from_template(
            state_with_data,
            template_id=template.template_id,
            source=ids_str,
            source_type="ids"
        )

        assert draft.template_id == template.template_id
        assert draft.target_status == "dispatched"
        assert draft.handler == "模板处理人"
        assert draft.remark == "模板备注内容"
        assert draft.name == "派单标准模板"
        assert len(draft.items) == 3

        assert draft.template_snapshot is not None
        assert draft.template_snapshot["name"] == "派单标准模板"
        assert draft.template_snapshot["target_status"] == "dispatched"
        assert draft.template_snapshot["handler"] == "模板处理人"

    def test_create_draft_from_template_with_overrides(self, config, state_with_data):
        """测试从模板创建草稿 - 命令行参数覆盖模板值"""
        from patrol_cli.workflow import create_template, create_draft_from_template

        template = create_template(
            state_with_data,
            name="覆盖测试模板",
            target_status="dispatched",
            handler="模板处理人",
            remark="模板备注"
        )

        defects = state_with_data.list_defects(status="pending")
        defect_ids = [d.defect_id for d in defects[:2]]
        ids_str = ",".join(defect_ids)

        draft = create_draft_from_template(
            state_with_data,
            template_id=template.template_id,
            source=ids_str,
            source_type="ids",
            name="自定义草稿名",
            status="false_positive",
            handler="自定义处理人",
            remark="自定义备注"
        )

        assert draft.name == "自定义草稿名"
        assert draft.target_status == "false_positive"
        assert draft.handler == "自定义处理人"
        assert draft.remark == "自定义备注"

        assert draft.template_snapshot["name"] == "覆盖测试模板"
        assert draft.template_snapshot["target_status"] == "dispatched"

    def test_template_modification_does_not_affect_existing_draft(self, config, state_with_data):
        """测试模板修改不影响已有的草稿（快照机制）"""
        from patrol_cli.workflow import (
            create_template, update_template, create_draft_from_template
        )

        template = create_template(
            state_with_data,
            name="快照验证模板",
            target_status="dispatched",
            handler="原始处理人",
            remark="原始备注"
        )

        defects = state_with_data.list_defects(status="pending")
        defect_ids = [d.defect_id for d in defects[:2]]
        ids_str = ",".join(defect_ids)

        draft = create_draft_from_template(
            state_with_data,
            template_id=template.template_id,
            source=ids_str,
            source_type="ids"
        )

        assert draft.template_snapshot["handler"] == "原始处理人"
        assert draft.template_snapshot["remark"] == "原始备注"
        assert draft.handler == "原始处理人"
        assert draft.remark == "原始备注"

        update_template(
            state_with_data,
            template_id=template.template_id,
            handler="修改后处理人",
            remark="修改后备注"
        )

        updated_template = state_with_data.get_template(template.template_id)
        assert updated_template.handler == "修改后处理人"

        draft_after = state_with_data.get_draft(draft.draft_id)
        assert draft_after.handler == "原始处理人"
        assert draft_after.remark == "原始备注"
        assert draft_after.template_snapshot["handler"] == "原始处理人"
        assert draft_after.template_snapshot["remark"] == "原始备注"

    def test_template_export_and_import(self, config, temp_data_dir, tmp_path):
        """测试模板导出和导入"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import (
            create_template, export_templates, import_templates
        )

        state = PatrolState(data_dir=temp_data_dir)

        t1 = create_template(state, name="导出模板1", target_status="dispatched",
                            handler="处理人A", remark="备注A", description="描述A")
        t2 = create_template(state, name="导出模板2", target_status="closed",
                            handler="处理人B", remark="备注B")

        export_file = tmp_path / "templates_export.json"
        count = export_templates(state, str(export_file))
        assert count == 2

        import json
        with open(export_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 2
        assert t1.template_id in data
        assert data[t1.template_id]["name"] == "导出模板1"

        state2_data = tmp_path / "state2_data"
        state2_data.mkdir()
        state2 = PatrolState(data_dir=str(state2_data))

        result = import_templates(state2, str(export_file))
        assert result["imported_count"] == 2
        assert result["skipped_count"] == 0
        assert result["error_count"] == 0

        assert state2.get_template_by_name("导出模板1") is not None
        assert state2.get_template_by_name("导出模板2") is not None

        t1_imported = state2.get_template_by_name("导出模板1")
        assert t1_imported.target_status == "dispatched"
        assert t1_imported.handler == "处理人A"
        assert t1_imported.remark == "备注A"

    def test_template_import_missing_fields_rejected(self, config, temp_data_dir, tmp_path):
        """测试模板导入 - 缺字段的模板被拒绝"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import import_templates
        import json

        bad_templates = [
            {"name": "缺状态模板"},
            {"target_status": "dispatched"},
            {"name": "", "target_status": "dispatched"},
        ]
        bad_file = tmp_path / "bad_templates.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump(bad_templates, f, ensure_ascii=False)

        state = PatrolState(data_dir=temp_data_dir)
        result = import_templates(state, str(bad_file))

        assert result["error_count"] == 3
        assert result["imported_count"] == 0
        assert all("缺少必填字段" in e for e in result["errors"])

    def test_template_import_duplicate_name_skip(self, config, temp_data_dir, tmp_path):
        """测试模板导入 - 同名模板默认跳过（不覆盖）"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import create_template, export_templates, import_templates

        state = PatrolState(data_dir=temp_data_dir)
        create_template(state, name="同名模板", target_status="dispatched",
                        handler="原处理人", remark="原备注")

        tpl_data = {
            "TPL-TEST-001": {
                "template_id": "TPL-TEST-001",
                "name": "同名模板",
                "target_status": "closed",
                "handler": "新处理人",
                "remark": "新备注"
            }
        }
        import_file = tmp_path / "duplicate_name.json"
        import json
        with open(import_file, "w", encoding="utf-8") as f:
            json.dump(tpl_data, f, ensure_ascii=False)

        result = import_templates(state, str(import_file))

        assert result["skipped_count"] == 1
        assert result["imported_count"] == 0
        assert "同名已存在" in result["skipped"][0]

        tpl = state.get_template_by_name("同名模板")
        assert tpl.handler == "原处理人"
        assert tpl.target_status == "dispatched"

    def test_template_import_duplicate_name_overwrite(self, config, temp_data_dir, tmp_path):
        """测试模板导入 - 同名模板 overwrite=True 时覆盖"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import create_template, import_templates

        state = PatrolState(data_dir=temp_data_dir)
        original = create_template(state, name="覆盖测试", target_status="dispatched",
                                   handler="原处理人", remark="原备注")

        tpl_data = {
            "NEW-ID-001": {
                "template_id": "NEW-ID-001",
                "name": "覆盖测试",
                "target_status": "closed",
                "handler": "新处理人",
                "remark": "新备注"
            }
        }
        import json
        import_file = tmp_path / "overwrite_test.json"
        with open(import_file, "w", encoding="utf-8") as f:
            json.dump(tpl_data, f, ensure_ascii=False)

        result = import_templates(state, str(import_file), overwrite=True)

        assert result["imported_count"] == 1
        assert "覆盖" in result["imported"][0]

        tpl = state.get_template_by_name("覆盖测试")
        assert tpl.template_id == original.template_id
        assert tpl.target_status == "closed"
        assert tpl.handler == "新处理人"
        assert tpl.remark == "新备注"

    def test_template_persistence_across_reload(self, config, temp_data_dir):
        """测试模板持久化 - 重启后仍然存在"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import create_template, update_template

        state1 = PatrolState(data_dir=temp_data_dir)
        template = create_template(
            state1,
            name="持久化测试模板",
            target_status="dispatched",
            handler="持久化处理人",
            remark="持久化备注",
            description="持久化描述"
        )
        tpl_id = template.template_id

        state2 = PatrolState(data_dir=temp_data_dir)
        loaded = state2.get_template(tpl_id)

        assert loaded is not None
        assert loaded.name == "持久化测试模板"
        assert loaded.target_status == "dispatched"
        assert loaded.handler == "持久化处理人"
        assert loaded.remark == "持久化备注"
        assert loaded.description == "持久化描述"
        assert loaded.created_at == template.created_at
        assert loaded.updated_at == template.updated_at

        all_templates = state2.list_templates()
        assert len(all_templates) >= 1
        assert any(t.template_id == tpl_id for t in all_templates)

    def test_template_draft_persistence(self, config, temp_data_dir):
        """测试带模板来源的草稿持久化"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import create_template, create_draft_from_template

        state1 = PatrolState(data_dir=temp_data_dir)
        template = create_template(
            state1,
            name="草稿持久化模板",
            target_status="dispatched",
            handler="模板处理人",
            remark="模板备注"
        )

        from patrol_cli.merger import import_and_merge
        import_and_merge("examples/sample_data.csv", config, state1, "BATCH-TPL-PERSIST")

        defects = state1.list_defects(status="pending")
        ids_str = ",".join([d.defect_id for d in defects[:2]])

        draft = create_draft_from_template(
            state1,
            template_id=template.template_id,
            source=ids_str,
            source_type="ids"
        )
        draft_id = draft.draft_id

        state2 = PatrolState(data_dir=temp_data_dir)
        loaded_draft = state2.get_draft(draft_id)

        assert loaded_draft is not None
        assert loaded_draft.template_id == template.template_id
        assert loaded_draft.template_snapshot is not None
        assert loaded_draft.template_snapshot["name"] == "草稿持久化模板"
        assert loaded_draft.template_snapshot["handler"] == "模板处理人"
        assert loaded_draft.handler == "模板处理人"
        assert loaded_draft.target_status == "dispatched"

    def test_template_list_and_get_by_name(self, config, state_with_data):
        """测试模板列表和按名称查询"""
        from patrol_cli.workflow import create_template

        t1 = create_template(state_with_data, name="列表测试A", target_status="dispatched")
        t2 = create_template(state_with_data, name="列表测试B", target_status="closed")

        templates = state_with_data.list_templates()
        assert len(templates) >= 2
        names = [t.name for t in templates]
        assert "列表测试A" in names
        assert "列表测试B" in names

        found = state_with_data.get_template_by_name("列表测试A")
        assert found is not None
        assert found.template_id == t1.template_id

        not_found = state_with_data.get_template_by_name("不存在的模板")
        assert not_found is None

    def test_template_export_draft_includes_template_info(self, config, temp_data_dir, tmp_path):
        """测试草稿导出包含模板来源信息"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import create_template, create_draft_from_template, execute_draft
        from patrol_cli.merger import import_and_merge
        from patrol_cli.exporter import export_draft_csv, export_draft_list_csv

        state = PatrolState(data_dir=temp_data_dir)
        import_and_merge("examples/sample_data.csv", config, state, "BATCH-EXPORT-TPL")

        template = create_template(
            state,
            name="导出测试模板",
            target_status="dispatched",
            handler="导出处理人",
            remark="导出备注"
        )

        defects = state.list_defects(status="pending")
        ids_str = ",".join([d.defect_id for d in defects[:2]])

        draft = create_draft_from_template(
            state,
            template_id=template.template_id,
            source=ids_str,
            source_type="ids"
        )

        execute_draft(state, draft.draft_id)

        detail_csv = tmp_path / "draft_detail_with_tpl.csv"
        count = export_draft_csv(state, draft.draft_id, str(detail_csv))
        assert count == 2

        with open(detail_csv, "r", encoding="utf-8-sig") as f:
            content = f.read()

        assert "模板ID" in content
        assert "模板名称" in content
        assert template.template_id in content
        assert "导出测试模板" in content

        list_csv = tmp_path / "draft_list_with_tpl.csv"
        count = export_draft_list_csv(state, str(list_csv))
        assert count >= 1

        with open(list_csv, "r", encoding="utf-8-sig") as f:
            content = f.read()

        assert "模板ID" in content
        assert "模板名称" in content
        assert template.template_id in content
        assert "导出测试模板" in content

    def test_full_template_workflow_chain(self, config, temp_data_dir):
        """模板全链路回归测试：建模板→套模板生成草稿→改模板→回看旧草稿→重启查询"""
        from patrol_cli.storage import PatrolState
        from patrol_cli.workflow import (
            create_template, update_template, create_draft_from_template,
            execute_draft
        )
        from patrol_cli.merger import import_and_merge

        state = PatrolState(data_dir=temp_data_dir)

        import_and_merge("examples/sample_data.csv", config, state, "BATCH-FULL-CHAIN")

        template = create_template(
            state,
            name="全链路测试模板",
            target_status="dispatched",
            handler="原始处理人",
            remark="原始备注",
            source_type="ids",
            description="用于全链路测试的模板"
        )
        tpl_id = template.template_id

        assert state.get_template(tpl_id) is not None
        assert state.get_template_by_name("全链路测试模板") is not None

        defects = state.list_defects(status="pending")
        defect_ids = [d.defect_id for d in defects[:3]]
        ids_str = ",".join(defect_ids)

        draft = create_draft_from_template(
            state,
            template_id=tpl_id,
            source=ids_str,
            source_type="ids",
            name="全链路草稿"
        )
        draft_id = draft.draft_id

        assert draft.template_id == tpl_id
        assert draft.handler == "原始处理人"
        assert draft.remark == "原始备注"
        assert draft.template_snapshot["name"] == "全链路测试模板"

        update_template(
            state,
            template_id=tpl_id,
            handler="修改后处理人",
            remark="修改后备注",
            target_status="closed"
        )

        updated_tpl = state.get_template(tpl_id)
        assert updated_tpl.handler == "修改后处理人"
        assert updated_tpl.target_status == "closed"

        draft_after_update = state.get_draft(draft_id)
        assert draft_after_update.handler == "原始处理人"
        assert draft_after_update.remark == "原始备注"
        assert draft_after_update.target_status == "dispatched"
        assert draft_after_update.template_snapshot["handler"] == "原始处理人"
        assert draft_after_update.template_snapshot["target_status"] == "dispatched"

        state_reload = PatrolState(data_dir=temp_data_dir)

        reloaded_tpl = state_reload.get_template(tpl_id)
        assert reloaded_tpl is not None
        assert reloaded_tpl.handler == "修改后处理人"
        assert reloaded_tpl.target_status == "closed"

        reloaded_draft = state_reload.get_draft(draft_id)
        assert reloaded_draft is not None
        assert reloaded_draft.template_id == tpl_id
        assert reloaded_draft.handler == "原始处理人"
        assert reloaded_draft.remark == "原始备注"
        assert reloaded_draft.template_snapshot["name"] == "全链路测试模板"
        assert reloaded_draft.template_snapshot["handler"] == "原始处理人"

        templates = state_reload.list_templates()
        assert any(t.template_id == tpl_id for t in templates)

        execute_draft(state_reload, draft_id)

        executed_draft = state_reload.get_draft(draft_id)
        assert executed_draft.status == "executed"
        assert executed_draft.template_id == tpl_id
        assert executed_draft.template_snapshot["name"] == "全链路测试模板"

        review_logs = state_reload.get_review_logs(draft_id=draft_id)
        assert len(review_logs) > 0
        for log in review_logs:
            assert log.draft_id == draft_id
