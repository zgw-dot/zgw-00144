"""缺陷归并模块"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from collections import defaultdict

from .config import RulesConfig
from .models import DefectRecord, SourceRow, generate_defect_id, ImportLogEntry, generate_log_id
from .storage import PatrolState


def _parse_time(ts: str) -> Optional[datetime]:
    """解析时间字符串"""
    if not ts:
        return None
    for fmt in [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _merge_key(building: str, device_id: str, defect_type: str) -> str:
    """归并键"""
    return f"{building}||{device_id}||{defect_type}"


def merge_source_rows_into_defects(
    source_rows: List[SourceRow],
    config: RulesConfig,
    existing_defects: Dict[str, DefectRecord]
) -> Tuple[Dict[str, DefectRecord], int, int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    将来源行归并到缺陷中。
    归并规则：同一楼栋 + 同一设备 + 同一缺陷类型 + 时间窗口内 => 同一缺陷
    
    返回: (缺陷字典, 新增数量, 合并数量, 新增缺陷详情, 合并缺陷详情)
    """
    merge_window = timedelta(hours=config.merge_window_hours)

    groups: Dict[str, List[DefectRecord]] = defaultdict(list)
    for defect in existing_defects.values():
        key = _merge_key(defect.building, defect.device_id, defect.defect_type)
        groups[key].append(defect)

    new_defects: Dict[str, DefectRecord] = {}
    updated_existing: Dict[str, DefectRecord] = {}

    new_count = 0
    merged_count = 0

    new_defect_details: List[Dict[str, Any]] = []
    merged_defect_details: List[Dict[str, Any]] = []
    merged_defect_rows: Dict[str, int] = defaultdict(int)

    for sr in source_rows:
        building = getattr(sr, "_building", "")
        device_id = getattr(sr, "_device_id", "")
        defect_type = getattr(sr, "_defect_type", "")
        severity = getattr(sr, "_severity", "")
        description = getattr(sr, "_description", "")
        inspect_time_str = getattr(sr, "_parsed_time", "")
        device_category = getattr(sr, "_device_category", "")

        inspect_time = _parse_time(inspect_time_str)
        if not inspect_time:
            continue

        key = _merge_key(building, device_id, defect_type)
        candidates = groups.get(key, [])

        matched_defect: Optional[DefectRecord] = None
        for defect in candidates:
            last_seen = _parse_time(defect.last_seen)
            first_seen = _parse_time(defect.first_seen)
            if not last_seen or not first_seen:
                continue

            if (first_seen - merge_window) <= inspect_time <= (last_seen + merge_window):
                matched_defect = defect
                break

        if matched_defect:
            last_seen_dt = _parse_time(matched_defect.last_seen)
            if inspect_time > last_seen_dt:
                matched_defect.last_seen = inspect_time.isoformat()

            first_seen_dt = _parse_time(matched_defect.first_seen)
            if inspect_time < first_seen_dt:
                matched_defect.first_seen = inspect_time.isoformat()

            matched_defect.source_rows.append(sr)
            updated_existing[matched_defect.defect_id] = matched_defect
            merged_count += 1
            merged_defect_rows[matched_defect.defect_id] += 1
        else:
            defect_id = generate_defect_id()
            defect = DefectRecord(
                defect_id=defect_id,
                building=building,
                device_id=device_id,
                device_category=device_category,
                defect_type=defect_type,
                severity=severity,
                description=description,
                first_seen=inspect_time.isoformat(),
                last_seen=inspect_time.isoformat(),
                status="pending",
                source_rows=[sr],
                review_remark="",
                handler="",
                status_history=[{
                    "from": "",
                    "to": "pending",
                    "time": datetime.now().isoformat(),
                    "remark": "初始创建",
                    "handler": "system"
                }]
            )
            new_defects[defect_id] = defect
            groups[key].append(defect)
            new_count += 1
            new_defect_details.append({
                "defect_id": defect_id,
                "building": building,
                "device_id": device_id,
                "device_category": device_category,
                "defect_type": defect_type,
                "severity": severity,
                "description": description,
                "first_seen": defect.first_seen,
            })

    for defect_id, added_rows in merged_defect_rows.items():
        defect = updated_existing[defect_id]
        merged_defect_details.append({
            "defect_id": defect_id,
            "building": defect.building,
            "device_id": defect.device_id,
            "defect_type": defect.defect_type,
            "severity": defect.severity,
            "added_rows": added_rows,
        })

    result = dict(existing_defects)
    result.update(updated_existing)
    result.update(new_defects)

    return result, new_count, merged_count, new_defect_details, merged_defect_details


def import_and_merge(
    csv_path: str,
    config: RulesConfig,
    state: PatrolState,
    batch_id: str,
    log_import: bool = True
) -> "ImportResult":
    """导入并归并的完整流程"""
    from .importer import (
        read_csv, validate_and_transform_rows, ImportResult
    )
    from pathlib import Path

    source_file = Path(csv_path).name

    if state.is_file_imported(source_file):
        if log_import:
            log_entry = ImportLogEntry(
                log_id=generate_log_id(),
                log_type="import",
                filename=source_file,
                batch_id=batch_id,
                result="failed",
                error_summary=f"文件 {source_file} 已经导入过了，请勿重复导入。",
                timestamp=datetime.now().isoformat(),
                total_rows=0,
                valid_rows=0,
                invalid_rows=0,
                new_defects=0,
                merged_defects=0,
            )
            state.add_import_log(log_entry)
            state.save_import_logs()
        raise ValueError(
            f"文件 {source_file} 已经导入过了，请勿重复导入。"
        )

    rows, fieldnames = read_csv(csv_path)
    result = ImportResult()
    result.total_rows = len(rows)

    valid_rows, invalid_rows = validate_and_transform_rows(rows, config, source_file)
    result.valid_rows = len(valid_rows)
    result.invalid_rows = invalid_rows

    if invalid_rows:
        error_lines = []
        for item in invalid_rows[:10]:
            error_lines.append(f"第{item['line']}行: {'; '.join(item['errors'])}")
        if len(invalid_rows) > 10:
            error_lines.append(f"... 还有 {len(invalid_rows) - 10} 条错误")
        if log_import:
            error_summary = "; ".join([f"第{i['line']}行: {i['errors'][0]}" for i in invalid_rows[:5]])
            if len(invalid_rows) > 5:
                error_summary += f" 等{len(invalid_rows)}条错误"
            log_entry = ImportLogEntry(
                log_id=generate_log_id(),
                log_type="import",
                filename=source_file,
                batch_id=batch_id,
                result="failed",
                error_summary=error_summary,
                timestamp=datetime.now().isoformat(),
                total_rows=result.total_rows,
                valid_rows=result.valid_rows,
                invalid_rows=len(invalid_rows),
                new_defects=0,
                merged_defects=0,
            )
            state.add_import_log(log_entry)
            state.save_import_logs()
        raise ValueError(
            f"文件校验失败，共 {len(invalid_rows)} 行不合法:\n" +
            "\n".join(error_lines)
        )

    if not valid_rows:
        return result

    state.init_batch(batch_id)

    snapshot = state.snapshot_defects()

    current_defects = dict(state.defects)
    merged, new_count, merged_count, new_details, merged_details = merge_source_rows_into_defects(
        valid_rows, config, current_defects
    )

    state.defects = merged
    state.mark_file_imported(source_file)

    result.new_defects = new_count
    result.merged_defects = merged_count
    result.source_rows = valid_rows
    result.new_defect_details = new_details
    result.merged_defect_details = merged_details

    state.push_undo(
        action=f"导入文件 {source_file}",
        snapshot=snapshot
    )

    if log_import:
        log_entry = ImportLogEntry(
            log_id=generate_log_id(),
            log_type="import",
            filename=source_file,
            batch_id=batch_id,
            result="success",
            error_summary="",
            timestamp=datetime.now().isoformat(),
            total_rows=result.total_rows,
            valid_rows=result.valid_rows,
            invalid_rows=0,
            new_defects=new_count,
            merged_defects=merged_count,
        )
        state.add_import_log(log_entry)

    state.save()

    return result


def preview_import(
    csv_path: str,
    config: RulesConfig,
    state: PatrolState,
    batch_id: str = ""
) -> "ImportResult":
    """
    预检导入（dry-run），不落盘。
    只做验证和归并模拟，不修改 state 的任何持久化数据。
    会记录预检日志。
    """
    from .importer import (
        read_csv, validate_and_transform_rows, ImportResult
    )
    from pathlib import Path
    import copy

    source_file = Path(csv_path).name

    rows, fieldnames = read_csv(csv_path)
    result = ImportResult()
    result.total_rows = len(rows)

    valid_rows, invalid_rows = validate_and_transform_rows(rows, config, source_file)
    result.valid_rows = len(valid_rows)
    result.invalid_rows = invalid_rows

    if not valid_rows:
        log_entry = ImportLogEntry(
            log_id=generate_log_id(),
            log_type="preview",
            filename=source_file,
            batch_id=batch_id,
            result="failed" if invalid_rows else "empty",
            error_summary=_build_error_summary(invalid_rows),
            timestamp=datetime.now().isoformat(),
            total_rows=result.total_rows,
            valid_rows=result.valid_rows,
            invalid_rows=len(invalid_rows),
            new_defects=0,
            merged_defects=0,
        )
        state.add_import_log(log_entry)
        state.save_import_logs()
        return result

    existing_defects_copy = {
        did: copy.deepcopy(defect)
        for did, defect in state.defects.items()
    }

    merged, new_count, merged_count, new_details, merged_details = merge_source_rows_into_defects(
        valid_rows, config, existing_defects_copy
    )

    result.new_defects = new_count
    result.merged_defects = merged_count
    result.source_rows = valid_rows
    result.new_defect_details = new_details
    result.merged_defect_details = merged_details

    log_result = "success" if not invalid_rows else "partial"
    log_entry = ImportLogEntry(
        log_id=generate_log_id(),
        log_type="preview",
        filename=source_file,
        batch_id=batch_id,
        result=log_result,
        error_summary=_build_error_summary(invalid_rows),
        timestamp=datetime.now().isoformat(),
        total_rows=result.total_rows,
        valid_rows=result.valid_rows,
        invalid_rows=len(invalid_rows),
        new_defects=new_count,
        merged_defects=merged_count,
    )
    state.add_import_log(log_entry)
    state.save_import_logs()

    return result


def _build_error_summary(invalid_rows: List[Dict[str, Any]]) -> str:
    """构建错误摘要"""
    if not invalid_rows:
        return ""
    parts = []
    for item in invalid_rows[:5]:
        first_error = item['errors'][0] if item['errors'] else ""
        parts.append(f"第{item['line']}行: {first_error}")
    if len(invalid_rows) > 5:
        parts.append(f"... 共{len(invalid_rows)}条错误")
    return "; ".join(parts)
