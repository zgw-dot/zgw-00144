"""规则配置模块"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
import yaml
from pathlib import Path


@dataclass
class DeviceCategory:
    """设备类别"""
    code: str
    name: str
    defect_types: List[str] = field(default_factory=list)


@dataclass
class SeverityLevel:
    """严重等级"""
    level: str
    name: str
    color: str = "#666"
    rectify_hours: int = 72


@dataclass
class RulesConfig:
    """巡检规则配置"""
    device_categories: List[DeviceCategory] = field(default_factory=list)
    severity_levels: List[SeverityLevel] = field(default_factory=list)
    merge_window_hours: int = 24
    required_fields: List[str] = field(default_factory=list)
    csv_field_mapping: Dict[str, str] = field(default_factory=dict)

    def get_category(self, code: str) -> Optional[DeviceCategory]:
        for cat in self.device_categories:
            if cat.code == code:
                return cat
        return None

    def get_severity(self, level: str) -> Optional[SeverityLevel]:
        for sev in self.severity_levels:
            if sev.level == level:
                return sev
        return None

    def get_rectify_hours(self, severity_level: str) -> int:
        sev = self.get_severity(severity_level)
        return sev.rectify_hours if sev else 72

    def validate_row(self, row: Dict[str, str]) -> List[str]:
        """验证一行数据，返回错误列表"""
        errors = []
        for field_name in self.required_fields:
            csv_field = self.csv_field_mapping.get(field_name, field_name)
            value = row.get(csv_field, "").strip()
            if not value:
                errors.append(f"必填字段缺失: {field_name}")
        return errors


def load_rules(config_path: str) -> RulesConfig:
    """从 YAML 文件加载规则配置"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"规则配置文件不存在: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    device_categories = []
    for cat_data in data.get("device_categories", []):
        device_categories.append(DeviceCategory(
            code=cat_data["code"],
            name=cat_data["name"],
            defect_types=cat_data.get("defect_types", [])
        ))

    severity_levels = []
    for sev_data in data.get("severity_levels", []):
        severity_levels.append(SeverityLevel(
            level=sev_data["level"],
            name=sev_data["name"],
            color=sev_data.get("color", "#666"),
            rectify_hours=sev_data.get("rectify_hours", 72)
        ))

    config = RulesConfig(
        device_categories=device_categories,
        severity_levels=severity_levels,
        merge_window_hours=data.get("merge_window_hours", 24),
        required_fields=data.get("required_fields", []),
        csv_field_mapping=data.get("csv_field_mapping", {})
    )

    return config
