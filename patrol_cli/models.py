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


def generate_audit_id() -> str:
    """生成审计日志 ID"""
    return f"AUDIT-{uuid.uuid4().hex[:12].upper()}"


def generate_archive_id() -> str:
    """生成档案 ID"""
    return f"ARC-{uuid.uuid4().hex[:10].upper()}"


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
    snapshot_sealed_at: str = ""

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
            "template_snapshot": copy.deepcopy(self.template_snapshot),
            "snapshot_sealed_at": self.snapshot_sealed_at
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
            template_snapshot=copy.deepcopy(data.get("template_snapshot", {})),
            snapshot_sealed_at=data.get("snapshot_sealed_at", "")
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


@dataclass
class AuditLogEntry:
    """审计日志条目"""
    audit_id: str = ""
    action: str = ""
    target_type: str = ""
    target_id: str = ""
    detail: str = ""
    result: str = ""
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditLogEntry":
        return cls(
            audit_id=data.get("audit_id", ""),
            action=data.get("action", ""),
            target_type=data.get("target_type", ""),
            target_id=data.get("target_id", ""),
            detail=data.get("detail", ""),
            result=data.get("result", ""),
            timestamp=data.get("timestamp", "")
        )


def generate_version_id() -> str:
    return f"VER-{uuid.uuid4().hex[:10].upper()}"


@dataclass
class TemplateVersion:
    version_id: str = ""
    template_id: str = ""
    template_name: str = ""
    version_name: str = ""
    target_status: str = ""
    handler: str = ""
    remark: str = ""
    source_type: str = ""
    description: str = ""
    template_snapshot: Dict[str, Any] = field(default_factory=dict)
    published_at: str = ""
    published_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateVersion":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class VersionDiffItem:
    field_name: str = ""
    field_label: str = ""
    old_value: str = ""
    new_value: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VersionDiffItem":
        return cls(**data)


@dataclass
class VersionCompareResult:
    version_a_id: str = ""
    version_a_name: str = ""
    version_b_id: str = ""
    version_b_name: str = ""
    diffs: List[VersionDiffItem] = field(default_factory=list)
    is_same: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_a_id": self.version_a_id,
            "version_a_name": self.version_a_name,
            "version_b_id": self.version_b_id,
            "version_b_name": self.version_b_name,
            "diffs": [d.to_dict() for d in self.diffs],
            "is_same": self.is_same
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VersionCompareResult":
        diffs = [VersionDiffItem.from_dict(d) for d in data.get("diffs", [])]
        return cls(
            version_a_id=data.get("version_a_id", ""),
            version_a_name=data.get("version_a_name", ""),
            version_b_id=data.get("version_b_id", ""),
            version_b_name=data.get("version_b_name", ""),
            diffs=diffs,
            is_same=data.get("is_same", True)
        )


@dataclass
class VersionRestorePreview:
    version_id: str = ""
    version_name: str = ""
    template_id: str = ""
    template_name: str = ""
    current_target_status: str = ""
    current_handler: str = ""
    current_remark: str = ""
    current_source_type: str = ""
    restore_target_status: str = ""
    restore_handler: str = ""
    restore_remark: str = ""
    restore_source_type: str = ""
    diffs: List[VersionDiffItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "version_name": self.version_name,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "current_target_status": self.current_target_status,
            "current_handler": self.current_handler,
            "current_remark": self.current_remark,
            "current_source_type": self.current_source_type,
            "restore_target_status": self.restore_target_status,
            "restore_handler": self.restore_handler,
            "restore_remark": self.restore_remark,
            "restore_source_type": self.restore_source_type,
            "diffs": [d.to_dict() for d in self.diffs]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VersionRestorePreview":
        diffs = [VersionDiffItem.from_dict(d) for d in data.get("diffs", [])]
        return cls(
            version_id=data.get("version_id", ""),
            version_name=data.get("version_name", ""),
            template_id=data.get("template_id", ""),
            template_name=data.get("template_name", ""),
            current_target_status=data.get("current_target_status", ""),
            current_handler=data.get("current_handler", ""),
            current_remark=data.get("current_remark", ""),
            current_source_type=data.get("current_source_type", ""),
            restore_target_status=data.get("restore_target_status", ""),
            restore_handler=data.get("restore_handler", ""),
            restore_remark=data.get("restore_remark", ""),
            restore_source_type=data.get("restore_source_type", ""),
            diffs=diffs
        )


@dataclass
class ImportConflictItem:
    template_name: str = ""
    local_version_source: str = ""
    import_version_source: str = ""
    conflict_type: str = ""
    local_template_id: str = ""
    import_template_id: str = ""
    local_versions: List[str] = field(default_factory=list)
    import_versions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_name": self.template_name,
            "local_version_source": self.local_version_source,
            "import_version_source": self.import_version_source,
            "conflict_type": self.conflict_type,
            "local_template_id": self.local_template_id,
            "import_template_id": self.import_template_id,
            "local_versions": self.local_versions,
            "import_versions": self.import_versions
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportConflictItem":
        return cls(
            template_name=data.get("template_name", ""),
            local_version_source=data.get("local_version_source", ""),
            import_version_source=data.get("import_version_source", ""),
            conflict_type=data.get("conflict_type", ""),
            local_template_id=data.get("local_template_id", ""),
            import_template_id=data.get("import_template_id", ""),
            local_versions=data.get("local_versions", []),
            import_versions=data.get("import_versions", [])
        )


@dataclass
class ImportConflictResult:
    conflicts: List[ImportConflictItem] = field(default_factory=list)
    has_conflicts: bool = False
    total_import_templates: int = 0
    total_import_versions: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflicts": [c.to_dict() for c in self.conflicts],
            "has_conflicts": self.has_conflicts,
            "total_import_templates": self.total_import_templates,
            "total_import_versions": self.total_import_versions
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportConflictResult":
        conflicts = [ImportConflictItem.from_dict(c) for c in data.get("conflicts", [])]
        return cls(
            conflicts=conflicts,
            has_conflicts=data.get("has_conflicts", False),
            total_import_templates=data.get("total_import_templates", 0),
            total_import_versions=data.get("total_import_versions", 0)
        )


@dataclass
class TemplateArchive:
    """模板档案 - 发布时固化的永久副本，不受模板改名/删除影响"""
    archive_id: str = ""
    template_id: str = ""
    template_name: str = ""
    version_name: str = ""
    target_status: str = ""
    handler: str = ""
    remark: str = ""
    source_type: str = ""
    description: str = ""
    template_snapshot: Dict[str, Any] = field(default_factory=dict)
    published_at: str = ""
    published_by: str = ""
    archived_at: str = ""
    archive_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateArchive":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ArchiveDiffItem:
    field_name: str = ""
    field_label: str = ""
    old_value: str = ""
    new_value: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveDiffItem":
        return cls(**data)


@dataclass
class ArchiveCompareResult:
    archive_a_id: str = ""
    archive_a_name: str = ""
    archive_b_id: str = ""
    archive_b_name: str = ""
    diffs: List[ArchiveDiffItem] = field(default_factory=list)
    is_same: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "archive_a_id": self.archive_a_id,
            "archive_a_name": self.archive_a_name,
            "archive_b_id": self.archive_b_id,
            "archive_b_name": self.archive_b_name,
            "diffs": [d.to_dict() for d in self.diffs],
            "is_same": self.is_same
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveCompareResult":
        diffs = [ArchiveDiffItem.from_dict(d) for d in data.get("diffs", [])]
        return cls(
            archive_a_id=data.get("archive_a_id", ""),
            archive_a_name=data.get("archive_a_name", ""),
            archive_b_id=data.get("archive_b_id", ""),
            archive_b_name=data.get("archive_b_name", ""),
            diffs=diffs,
            is_same=data.get("is_same", True)
        )


@dataclass
class ArchiveRestorePreview:
    archive_id: str = ""
    version_name: str = ""
    template_id: str = ""
    template_name: str = ""
    template_exists: bool = False
    current_target_status: str = ""
    current_handler: str = ""
    current_remark: str = ""
    current_source_type: str = ""
    restore_target_status: str = ""
    restore_handler: str = ""
    restore_remark: str = ""
    restore_source_type: str = ""
    restore_action: str = ""
    diffs: List[ArchiveDiffItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "version_name": self.version_name,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "template_exists": self.template_exists,
            "current_target_status": self.current_target_status,
            "current_handler": self.current_handler,
            "current_remark": self.current_remark,
            "current_source_type": self.current_source_type,
            "restore_target_status": self.restore_target_status,
            "restore_handler": self.restore_handler,
            "restore_remark": self.restore_remark,
            "restore_source_type": self.restore_source_type,
            "restore_action": self.restore_action,
            "diffs": [d.to_dict() for d in self.diffs]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveRestorePreview":
        diffs = [ArchiveDiffItem.from_dict(d) for d in data.get("diffs", [])]
        return cls(
            archive_id=data.get("archive_id", ""),
            version_name=data.get("version_name", ""),
            template_id=data.get("template_id", ""),
            template_name=data.get("template_name", ""),
            template_exists=data.get("template_exists", False),
            current_target_status=data.get("current_target_status", ""),
            current_handler=data.get("current_handler", ""),
            current_remark=data.get("current_remark", ""),
            current_source_type=data.get("current_source_type", ""),
            restore_target_status=data.get("restore_target_status", ""),
            restore_handler=data.get("restore_handler", ""),
            restore_remark=data.get("restore_remark", ""),
            restore_source_type=data.get("restore_source_type", ""),
            restore_action=data.get("restore_action", ""),
            diffs=diffs
        )


@dataclass
class ArchiveExportResult:
    exported_archives: int = 0
    exported_templates: int = 0
    output_file: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveExportResult":
        return cls(**data)


FOLLOWUP_STATUSES = ["pending", "dispatched", "completed", "cancelled"]
FOLLOWUP_STATUS_NAMES = {
    "pending": "待签收",
    "dispatched": "已签收",
    "completed": "已完成",
    "cancelled": "已撤销"
}

FOLLOWUP_ITEM_STATUSES = ["pending", "completed", "cancelled"]
FOLLOWUP_ITEM_STATUS_NAMES = {
    "pending": "待回访",
    "completed": "已完成",
    "cancelled": "已取消"
}


def generate_followup_id() -> str:
    return f"FUP-{uuid.uuid4().hex[:12].upper()}"


@dataclass
class FollowUpPlanItem:
    defect_id: str
    defect_snapshot: Dict[str, Any] = field(default_factory=dict)
    item_status: str = "pending"
    result: str = ""
    result_remark: str = ""
    result_at: str = ""
    result_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FollowUpPlanItem":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class FollowUpPlan:
    plan_id: str = ""
    name: str = ""
    handler: str = ""
    deadline: str = ""
    remark: str = ""
    created_at: str = ""
    created_by: str = ""
    status: str = "pending"
    items: List[FollowUpPlanItem] = field(default_factory=list)
    dispatched_at: str = ""
    dispatched_by: str = ""
    completed_at: str = ""
    cancelled_at: str = ""
    cancel_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "handler": self.handler,
            "deadline": self.deadline,
            "remark": self.remark,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "dispatched_at": self.dispatched_at,
            "dispatched_by": self.dispatched_by,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
            "cancel_reason": self.cancel_reason
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FollowUpPlan":
        items = [FollowUpPlanItem.from_dict(d) for d in data.get("items", [])]
        return cls(
            plan_id=data.get("plan_id", ""),
            name=data.get("name", ""),
            handler=data.get("handler", ""),
            deadline=data.get("deadline", ""),
            remark=data.get("remark", ""),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by", ""),
            status=data.get("status", "pending"),
            items=items,
            dispatched_at=data.get("dispatched_at", ""),
            dispatched_by=data.get("dispatched_by", ""),
            completed_at=data.get("completed_at", ""),
            cancelled_at=data.get("cancelled_at", ""),
            cancel_reason=data.get("cancel_reason", "")
        )


@dataclass
class FollowUpCreatePreview:
    name: str = ""
    handler: str = ""
    deadline: str = ""
    remark: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    total_count: int = 0
    can_create: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FollowUpExportResult:
    exported_plans: int = 0
    output_file: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FollowUpExportResult":
        return cls(**data)
