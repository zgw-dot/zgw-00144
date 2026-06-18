"""CSV 导入模块"""

import csv
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Any

from .config import RulesConfig
from .models import SourceRow, generate_row_id
from .storage import PatrolState


DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y%m%d%H%M%S",
]


def parse_datetime(value: str) -> Optional[datetime]:
    """解析日期时间字符串，尝试多种格式"""
    value = value.strip()
    if not value:
        return None

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None


class ImportResult:
    """导入结果"""
    def __init__(self):
        self.total_rows = 0
        self.valid_rows = 0
        self.invalid_rows: List[Dict[str, Any]] = []
        self.new_defects = 0
        self.merged_defects = 0
        self.source_rows: List[SourceRow] = []

    def summary(self) -> str:
        lines = [
            f"总行数: {self.total_rows}",
            f"有效行: {self.valid_rows}",
            f"无效行: {len(self.invalid_rows)}",
            f"新增缺陷: {self.new_defects}",
            f"合并缺陷: {self.merged_defects}",
        ]
        return "\n".join(lines)


def read_csv(file_path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """读取 CSV 文件，返回行数据和字段名"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {file_path}")

    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for i, row in enumerate(reader, start=2):
            row_dict = dict(row)
            row_dict["__line_number__"] = str(i)
            rows.append(row_dict)

    return rows, fieldnames


def _get_field(row: Dict[str, str], field_name: str, mapping: Dict[str, str]) -> str:
    """获取字段值，考虑字段映射"""
    csv_field = mapping.get(field_name, field_name)
    return row.get(csv_field, "").strip()


def validate_and_transform_rows(
    rows: List[Dict[str, str]],
    config: RulesConfig,
    source_file: str
) -> Tuple[List[SourceRow], List[Dict[str, Any]]]:
    """验证并转换行数据"""
    valid_rows: List[SourceRow] = []
    invalid_rows: List[Dict[str, Any]] = []

    mapping = config.csv_field_mapping
    now = datetime.now().isoformat()

    for row in rows:
        line_number = int(row.get("__line_number__", 0))
        errors = []

        building = _get_field(row, "building", mapping)
        device_id = _get_field(row, "device_id", mapping)
        device_category = _get_field(row, "device_category", mapping)
        defect_type = _get_field(row, "defect_type", mapping)
        severity = _get_field(row, "severity", mapping)
        description = _get_field(row, "description", mapping)
        inspect_time_str = _get_field(row, "inspect_time", mapping)

        for field_name in config.required_fields:
            val = _get_field(row, field_name, mapping)
            if not val:
                errors.append(f"必填字段缺失: {field_name}")

        inspect_time = parse_datetime(inspect_time_str) if inspect_time_str else None
        if not inspect_time and inspect_time_str:
            errors.append(f"时间格式无法解析: {inspect_time_str}")
        elif not inspect_time:
            errors.append("巡检时间缺失或无效")

        if building:
            pass
        else:
            if "building" not in [e.split(": ")[-1] for e in errors]:
                errors.append("楼栋信息缺失")

        if errors:
            invalid_rows.append({
                "line": line_number,
                "errors": errors,
                "row_data": {k: v for k, v in row.items() if k != "__line_number__"}
            })
        else:
            clean_row = {k: v for k, v in row.items() if k != "__line_number__"}
            source_row = SourceRow(
                row_id=generate_row_id(),
                source_file=source_file,
                line_number=line_number,
                raw_data=clean_row,
                import_time=now
            )
            source_row._parsed_time = inspect_time.isoformat() if inspect_time else ""
            source_row._building = building
            source_row._device_id = device_id
            source_row._device_category = device_category
            source_row._defect_type = defect_type
            source_row._severity = severity
            source_row._description = description
            valid_rows.append(source_row)

    return valid_rows, invalid_rows


def _row_fingerprint(row: Dict[str, str]) -> str:
    """生成行的指纹，用于去重判断"""
    raw = "|".join(sorted([f"{k}={v}" for k, v in row.items() if k != "__line_number__"]))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
