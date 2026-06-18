"""数据模型"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from datetime import datetime
import uuid
import copy


DEFECT_STATUSES = ["pending", "dispatched", "false_positive", "closed"]
STATUS_NAMES = {
    "pending": "待派单",
    "dispatched": "已派单",
    "false_positive": "误报",
    "closed": "已关闭"
}


@dataclass
class SourceRow:
    """来源行记录"""
    row_id: str
    source_file: str
    line_number: int
    raw_data: Dict[str, str]
    import_time: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceRow":
        return cls(**data)


@dataclass
class DefectRecord:
    """缺陷记录"""
    defect_id: str
    building: str
    device_id: str
    device_category: str
    defect_type: str
    severity: str
    description: str
    first_seen: str
    last_seen: str
    status: str = "pending"
    source_rows: List[SourceRow] = field(default_factory=list)
    review_remark: str = ""
    handler: str = ""
    status_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "defect_id": self.defect_id,
            "building": self.building,
            "device_id": self.device_id,
            "device_category": self.device_category,
            "defect_type": self.defect_type,
            "severity": self.severity,
            "description": self.description,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "status": self.status,
            "source_rows": [sr.to_dict() for sr in self.source_rows],
            "review_remark": self.review_remark,
            "handler": self.handler,
            "status_history": copy.deepcopy(self.status_history)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DefectRecord":
        source_rows = [SourceRow.from_dict(sr) for sr in data.get("source_rows", [])]
        return cls(
            defect_id=data["defect_id"],
            building=data["building"],
            device_id=data["device_id"],
            device_category=data["device_category"],
            defect_type=data["defect_type"],
            severity=data["severity"],
            description=data["description"],
            first_seen=data["first_seen"],
            last_seen=data["last_seen"],
            status=data.get("status", "pending"),
            source_rows=source_rows,
            review_remark=data.get("review_remark", ""),
            handler=data.get("handler", ""),
            status_history=copy.deepcopy(data.get("status_history", []))
        )


def generate_defect_id() -> str:
    """生成缺陷 ID"""
    return f"DEF-{uuid.uuid4().hex[:12].upper()}"


def generate_row_id() -> str:
    return f"ROW-{uuid.uuid4().hex[:10].upper()}"
