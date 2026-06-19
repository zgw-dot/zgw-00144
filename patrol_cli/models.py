"""数据模型"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any
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


def generate_log_id() -> str:
    """生成日志 ID"""
    return f"LOG-{uuid.uuid4().hex[:12].upper()}"


def generate_draft_id() -> str:
    """生成草稿 ID"""
    return f"DRAFT-{uuid.uuid4().hex[:12].upper()}"


def generate_template_id() -> str:
    """生成模板 ID"""
    return f"TPL-{uuid.uuid4().hex[:10].upper()}"


DRAFT_STATUSES = ["pending", "executed", "voided"]
DRAFT_STATUS_NAMES = {
    "pending": "待执行",
    "executed": "已执行",
    "voided": "已作废"
}


@dataclass
class DraftItem:
    """草稿条目 - 单条缺陷的复核计划"""
    defect_id: str
    target_status: str
    defect_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DraftItem":
        return cls(**data)


@dataclass
class DraftExecutionResult:
    """草稿执行结果"""
    execution_id: str = ""
    executed_at: str = ""
    success_count: int = 0
    error_count: int = 0
    errors: List[str] = field(default_factory=list)
    undo_execution_id: str = ""
    undo_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DraftExecutionResult":
        return cls(**data)


@dataclass
class DraftTemplate:
    """复核方案模板"""
    template_id: str = ""
    name: str = ""
    target_status: str = ""
    handler: str = ""
    remark: str = ""
    source_type: str = ""
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DraftTemplate":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DraftEntry:
    """复核方案草稿"""
    draft_id: str = ""
    name: str = ""
    source_type: str = ""
    source_ref: str = ""
    target_status: str = ""
    handler: str = ""
    remark: str = ""
    created_at: str = ""
    created_by: str = ""
    status: str = "pending"
    items: List[DraftItem] = field(default_factory=list)
    execution: DraftExecutionResult = field(default_factory=DraftExecutionResult)
    template_id: str = ""
    template_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "name": self.name,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "target_status": self.target_status,
            "handler": self.handler,
            "remark": self.remark,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "execution": self.execution.to_dict(),
            "template_id": self.template_id,
            "template_snapshot": copy.deepcopy(self.template_snapshot)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DraftEntry":
        items = [DraftItem.from_dict(d) for d in data.get("items", [])]
        execution_data = data.get("execution", {})
        if isinstance(execution_data, dict):
            execution = DraftExecutionResult.from_dict(execution_data)
        else:
            execution = execution_data
        return cls(
            draft_id=data["draft_id"],
            name=data.get("name", ""),
            source_type=data.get("source_type", ""),
            source_ref=data.get("source_ref", ""),
            target_status=data.get("target_status", ""),
            handler=data.get("handler", ""),
            remark=data.get("remark", ""),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by", ""),
            status=data.get("status", "pending"),
            items=items,
            execution=execution,
            template_id=data.get("template_id", ""),
            template_snapshot=copy.deepcopy(data.get("template_snapshot", {}))
        )


@dataclass
class ImportLogEntry:
    """导入日志条目"""
    log_id: str = ""
    log_type: str = "import"
    filename: str = ""
    batch_id: str = ""
    result: str = ""
    error_summary: str = ""
    timestamp: str = ""
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    new_defects: int = 0
    merged_defects: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportLogEntry":
        return cls(**data)


@dataclass
class ReviewLogEntry:
    """复核日志条目"""
    log_id: str = ""
    log_type: str = "review"
    defect_id: str = ""
    from_status: str = ""
    to_status: str = ""
    handler: str = ""
    remark: str = ""
    timestamp: str = ""
    batch_id: str = ""
    parent_log_id: str = ""
    draft_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewLogEntry":
        return cls(
            log_id=data.get("log_id", ""),
            log_type=data.get("log_type", "review"),
            defect_id=data.get("defect_id", ""),
            from_status=data.get("from_status", ""),
            to_status=data.get("to_status", ""),
            handler=data.get("handler", ""),
            remark=data.get("remark", ""),
            timestamp=data.get("timestamp", ""),
            batch_id=data.get("batch_id", ""),
            parent_log_id=data.get("parent_log_id", ""),
            draft_id=data.get("draft_id", "")
        )
