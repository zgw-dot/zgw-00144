"""工作流模块 - 状态流转和撤销"""

from datetime import datetime
from typing import Optional, List, Tuple
from .models import (
    DefectRecord, STATUS_NAMES, DEFECT_STATUSES, ReviewLogEntry, generate_log_id,
    DraftEntry, DraftItem, DraftExecutionResult, generate_draft_id,
    DraftTemplate, generate_template_id, AuditLogEntry, generate_audit_id
)
from .storage import PatrolState


class WorkflowError(Exception):
    """工作流错误"""
    pass


VALID_TRANSITIONS = {
    "pending": ["dispatched", "false_positive", "closed"],
    "dispatched": ["pending", "false_positive", "closed"],
    "false_positive": ["pending"],
    "closed": ["pending", "dispatched"]
}


def can_transition(current_status: str, target_status: str) -> bool:
    """检查状态转换是否合法"""
    if current_status not in VALID_TRANSITIONS:
        return False
    return target_status in VALID_TRANSITIONS[current_status]


def _make_review_log(
    log_type: str,
    defect_id: str,
    from_status: str,
    to_status: str,
    handler: str,
    remark: str,
    batch_id: str,
    parent_log_id: str = "",
    draft_id: str = ""
) -> ReviewLogEntry:
    """创建复核日志条目"""
    return ReviewLogEntry(
        log_id=generate_log_id(),
        log_type=log_type,
        defect_id=defect_id,
        from_status=from_status,
        to_status=to_status,
        handler=handler,
        remark=remark,
        timestamp=datetime.now().isoformat(),
        batch_id=batch_id,
        parent_log_id=parent_log_id,
        draft_id=draft_id
    )


def review_defect(
    state: PatrolState,
    defect_id: str,
    new_status: str,
    remark: str = "",
    handler: str = ""
) -> DefectRecord:
    """
    复核缺陷，变更状态。
    会推入撤销栈，并记录复核日志。
    """
    if new_status not in DEFECT_STATUSES:
        raise WorkflowError(f"无效的状态: {new_status}")

    defect = state.get_defect(defect_id)
    if not defect:
        raise WorkflowError(f"缺陷不存在: {defect_id}")

    old_status = defect.status

    if old_status == new_status:
        raise WorkflowError(f"缺陷已经是 {STATUS_NAMES[new_status]} 状态")

    if not can_transition(old_status, new_status):
        raise WorkflowError(
            f"不允许从 {STATUS_NAMES[old_status]} 转换到 {STATUS_NAMES[new_status]}"
        )

    snapshot = state.snapshot_defects()

    defect.status = new_status
    if remark:
        defect.review_remark = remark
    if handler:
        defect.handler = handler

    defect.status_history.append({
        "from": old_status,
        "to": new_status,
        "time": datetime.now().isoformat(),
        "remark": remark,
        "handler": handler
    })

    log_entry = _make_review_log(
        log_type="review",
        defect_id=defect_id,
        from_status=old_status,
        to_status=new_status,
        handler=handler,
        remark=remark,
        batch_id=state.batch_id
    )
    state.add_review_log(log_entry)

    review_entries = [{
        "defect_id": defect_id,
        "from_status": old_status,
        "to_status": new_status,
        "handler": handler,
        "remark": remark
    }]

    state.push_undo(
        action=f"复核 {defect_id} {STATUS_NAMES[old_status]}→{STATUS_NAMES[new_status]}",
        snapshot=snapshot,
        review_entries=review_entries
    )

    state.save()

    return defect


def _extract_draft_id_from_action(action: str) -> str:
    """从撤销操作描述中提取草稿ID"""
    import re
    match = re.search(r'\((DRAFT-[A-F0-9]+)\)', action)
    if match:
        return match.group(1)
    return ""


def undo_last(state: PatrolState) -> Optional[str]:
    """撤销最后一步操作"""
    if not state.can_undo():
        return None

    undo_item = state.pop_undo()
    action = undo_item["action"]

    state.restore_defects(undo_item["snapshot"])

    draft_id = _extract_draft_id_from_action(action)

    review_entries = undo_item.get("review_entries", [])
    for entry in review_entries:
        undo_log = ReviewLogEntry(
            log_id=generate_log_id(),
            log_type="undo",
            defect_id=entry["defect_id"],
            from_status=entry["to_status"],
            to_status=entry["from_status"],
            handler=entry.get("handler", ""),
            remark=f"撤销操作: {action}",
            timestamp=datetime.now().isoformat(),
            batch_id=state.batch_id,
            parent_log_id="",
            draft_id=draft_id
        )
        state.add_review_log(undo_log)

    if not review_entries:
        undo_log = ReviewLogEntry(
            log_id=generate_log_id(),
            log_type="undo",
            defect_id="",
            from_status="",
            to_status="",
            handler="",
            remark=f"撤销操作: {action}",
            timestamp=datetime.now().isoformat(),
            batch_id=state.batch_id,
            parent_log_id="",
            draft_id=draft_id
        )
        state.add_review_log(undo_log)

    if draft_id:
        draft = state.get_draft(draft_id)
        if draft and draft.status == "executed":
            draft.execution.undo_execution_id = generate_log_id()
            draft.execution.undo_at = datetime.now().isoformat()
            state.update_draft(draft)
            state.save_drafts()

    state.save()

    return action


def _validate_batch_defects(
    state: PatrolState,
    defect_ids: List[str],
    new_status: str
) -> Tuple[List[str], List[str]]:
    """
    预校验批量复核的缺陷列表。
    返回 (valid_ids, errors)。
    检查：重复编号、不存在的缺陷、状态相同、不可转换。
    """
    errors = []
    seen = set()
    valid_ids = []

    for defect_id in defect_ids:
        if defect_id in seen:
            errors.append(f"{defect_id}: 重复的缺陷编号")
            continue
        seen.add(defect_id)

        defect = state.get_defect(defect_id)
        if not defect:
            errors.append(f"{defect_id}: 缺陷不存在")
            continue

        if defect.status == new_status:
            errors.append(f"{defect_id}: 已经是 {STATUS_NAMES[new_status]}")
            continue

        if not can_transition(defect.status, new_status):
            errors.append(f"{defect_id}: 不允许 {STATUS_NAMES[defect.status]}→{STATUS_NAMES[new_status]}")
            continue

        valid_ids.append(defect_id)

    return valid_ids, errors


def batch_review(
    state: PatrolState,
    defect_ids: list,
    new_status: str,
    remark: str = "",
    handler: str = ""
) -> tuple:
    """
    批量复核缺陷。
    整批校验：如果有任何错误（不存在、重复、状态不可转），整批失败，不做任何修改。
    全部校验通过后，统一执行并记录日志。
    """
    if not defect_ids:
        return 0, []

    valid_ids, errors = _validate_batch_defects(state, defect_ids, new_status)

    if errors:
        return 0, errors

    if not valid_ids:
        return 0, errors

    snapshot = state.snapshot_defects()

    parent_log_id = generate_log_id()

    success_count = 0
    review_entries = []
    for defect_id in valid_ids:
        defect = state.get_defect(defect_id)
        if not defect:
            continue

        old_status = defect.status

        defect.status = new_status
        if remark:
            defect.review_remark = remark
        if handler:
            defect.handler = handler

        defect.status_history.append({
            "from": old_status,
            "to": new_status,
            "time": datetime.now().isoformat(),
            "remark": remark,
            "handler": handler
        })

        log_entry = _make_review_log(
            log_type="batch_review",
            defect_id=defect_id,
            from_status=old_status,
            to_status=new_status,
            handler=handler,
            remark=remark,
            batch_id=state.batch_id,
            parent_log_id=parent_log_id
        )
        state.add_review_log(log_entry)
        review_entries.append({
            "defect_id": defect_id,
            "from_status": old_status,
            "to_status": new_status,
            "handler": handler,
            "remark": remark
        })
        success_count += 1

    if success_count > 0:
        state.push_undo(
            action=f"批量复核 {success_count} 条→{STATUS_NAMES[new_status]}",
            snapshot=snapshot,
            review_entries=review_entries
        )
        state.save()

    return success_count, errors


def _read_defect_ids_from_csv(csv_path: str) -> List[str]:
    """从 CSV 读取缺陷编号列表，支持单列或多列（第一列或有名为 defect_id/缺陷ID 的列）"""
    import csv
    from pathlib import Path

    defect_ids = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return defect_ids

    header = rows[0]
    id_col_idx = 0

    for i, col in enumerate(header):
        col_lower = col.lower().strip()
        if col_lower in ["defect_id", "defect id", "缺陷id", "缺陷编号", "id"]:
            id_col_idx = i
            break

    start_row = 1
    if header[id_col_idx].strip().upper().startswith("DEF-"):
        start_row = 0

    for row in rows[start_row:]:
        if id_col_idx < len(row):
            defect_id = row[id_col_idx].strip()
            if defect_id:
                defect_ids.append(defect_id)

    return defect_ids


def _collect_defect_ids(source: str, source_type: str) -> List[str]:
    """收集缺陷编号列表"""
    if source_type == "ids":
        return [x.strip() for x in source.split(",") if x.strip()]
    elif source_type == "csv":
        return _read_defect_ids_from_csv(source)
    return []


def create_draft(
    state: PatrolState,
    source: str,
    source_type: str,
    target_status: str,
    name: str = "",
    handler: str = "",
    remark: str = "",
    created_by: str = ""
) -> DraftEntry:
    """
    创建复核方案草稿。

    Args:
        state: 状态对象
        source: 缺陷来源（csv路径 或 逗号分隔的ID列表）
        source_type: "csv" 或 "ids"
        target_status: 目标状态
        name: 草稿名称
        handler: 处理人
        remark: 备注
        created_by: 创建人

    Returns:
        创建好的 DraftEntry
    """
    if target_status not in DEFECT_STATUSES:
        raise WorkflowError(f"无效的目标状态: {target_status}")

    defect_ids = _collect_defect_ids(source, source_type)

    if not defect_ids:
        raise WorkflowError("未找到任何有效的缺陷编号")

    seen = set()
    unique_ids = []
    duplicate_ids = []
    for did in defect_ids:
        if did in seen:
            duplicate_ids.append(did)
            continue
        seen.add(did)
        unique_ids.append(did)

    items = []
    not_found_ids = []
    for defect_id in unique_ids:
        defect = state.get_defect(defect_id)
        if not defect:
            not_found_ids.append(defect_id)
            continue

        item = DraftItem(
            defect_id=defect_id,
            target_status=target_status,
            defect_snapshot=defect.to_dict()
        )
        items.append(item)

    if not items:
        error_msg = "草稿创建失败："
        if not_found_ids:
            error_msg += f"以下缺陷不存在: {', '.join(not_found_ids)}"
        raise WorkflowError(error_msg)

    draft_id = generate_draft_id()
    draft = DraftEntry(
        draft_id=draft_id,
        name=name or f"草稿-{draft_id[:8]}",
        source_type=source_type,
        source_ref=source if source_type == "csv" else f"{len(unique_ids)}个ID",
        target_status=target_status,
        handler=handler,
        remark=remark,
        created_at=datetime.now().isoformat(),
        created_by=created_by,
        status="pending",
        items=items,
        execution=DraftExecutionResult()
    )

    state.add_draft(draft)
    state.save_drafts()

    return draft


def preview_draft(
    state: PatrolState,
    draft_id: str
) -> dict:
    """
    预览草稿，显示将影响哪些记录。

    Returns:
        包含预览信息的字典
    """
    draft = state.get_draft(draft_id)
    if not draft:
        raise WorkflowError(f"草稿不存在: {draft_id}")

    will_change = []
    same_status = []
    invalid_transition = []
    not_found = []

    for item in draft.items:
        defect = state.get_defect(item.defect_id)
        if not defect:
            not_found.append(item.defect_id)
            continue

        if defect.status == item.target_status:
            same_status.append({
                "defect_id": item.defect_id,
                "building": defect.building,
                "device_id": defect.device_id,
                "description": defect.description,
                "current_status": defect.status
            })
            continue

        if not can_transition(defect.status, item.target_status):
            invalid_transition.append({
                "defect_id": item.defect_id,
                "building": defect.building,
                "device_id": defect.device_id,
                "description": defect.description,
                "current_status": defect.status,
                "target_status": item.target_status
            })
            continue

        will_change.append({
            "defect_id": item.defect_id,
            "building": defect.building,
            "device_id": defect.device_id,
            "description": defect.description,
            "current_status": defect.status,
            "target_status": item.target_status,
            "snapshot_status": item.defect_snapshot.get("status", "")
        })

    return {
        "draft_id": draft.draft_id,
        "name": draft.name,
        "target_status": draft.target_status,
        "handler": draft.handler,
        "remark": draft.remark,
        "created_at": draft.created_at,
        "total_items": len(draft.items),
        "template_id": draft.template_id,
        "template_snapshot": draft.template_snapshot,
        "will_change": will_change,
        "same_status": same_status,
        "invalid_transition": invalid_transition,
        "not_found": not_found
    }


def _check_draft_conflicts(
    state: PatrolState,
    draft: DraftEntry
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """
    检查草稿执行前的冲突。

    Returns:
        (conflicts, duplicates, not_found, invalid_transitions)
    """
    seen = set()
    conflicts = []
    duplicates = []
    not_found = []
    invalid_transitions = []

    for item in draft.items:
        if item.defect_id in seen:
            duplicates.append(item.defect_id)
            continue
        seen.add(item.defect_id)

        current_defect = state.get_defect(item.defect_id)
        if not current_defect:
            not_found.append(item.defect_id)
            continue

        snapshot_status = item.defect_snapshot.get("status", "")
        if current_defect.status != snapshot_status:
            conflicts.append(
                f"{item.defect_id}: 创建草稿时状态为{snapshot_status}，"
                f"当前状态为{current_defect.status}，可能已被他人修改"
            )
            continue

        if current_defect.status == item.target_status:
            invalid_transitions.append(
                f"{item.defect_id}: 已经是 {STATUS_NAMES[item.target_status]} 状态"
            )
            continue

        if not can_transition(current_defect.status, item.target_status):
            invalid_transitions.append(
                f"{item.defect_id}: 不允许从 {STATUS_NAMES[current_defect.status]} "
                f"转换到 {STATUS_NAMES[item.target_status]}"
            )
            continue

    return conflicts, duplicates, not_found, invalid_transitions


def execute_draft(
    state: PatrolState,
    draft_id: str
) -> DraftExecutionResult:
    """
    执行草稿，原子执行。

    执行前检查：
    - 草稿状态必须是 pending
    - 所有缺陷存在且状态未被修改（与快照一致）
    - 没有重复编号
    - 所有状态转换合法

    任何检查失败，整批不执行。
    全部通过后，统一执行并记录日志。
    """
    draft = state.get_draft(draft_id)
    if not draft:
        raise WorkflowError(f"草稿不存在: {draft_id}")

    if draft.status == "executed":
        raise WorkflowError(f"草稿已执行，不能重复执行: {draft_id}")

    if draft.status == "voided":
        raise WorkflowError(f"草稿已作废，不能执行: {draft_id}")

    if draft.status != "pending":
        raise WorkflowError(f"草稿状态不允许执行: {draft.status}")

    conflicts, duplicates, not_found, invalid_transitions = _check_draft_conflicts(state, draft)

    all_errors = []
    if conflicts:
        all_errors.extend(conflicts)
    if duplicates:
        all_errors.extend([f"{did}: 重复的缺陷编号" for did in duplicates])
    if not_found:
        all_errors.extend([f"{did}: 缺陷不存在" for did in not_found])
    if invalid_transitions:
        all_errors.extend(invalid_transitions)

    if all_errors:
        error_msg = "草稿执行冲突，整批不执行：\n" + "\n".join(all_errors)
        raise WorkflowError(error_msg)

    snapshot = state.snapshot_defects()
    parent_log_id = generate_log_id()
    execution_id = generate_log_id()

    success_count = 0
    review_entries = []

    for item in draft.items:
        defect = state.get_defect(item.defect_id)
        if not defect:
            continue

        old_status = defect.status
        new_status = item.target_status

        defect.status = new_status
        if draft.handler:
            defect.handler = draft.handler
        if draft.remark:
            defect.review_remark = draft.remark

        defect.status_history.append({
            "from": old_status,
            "to": new_status,
            "time": datetime.now().isoformat(),
            "remark": draft.remark,
            "handler": draft.handler
        })

        log_entry = _make_review_log(
            log_type="draft_review",
            defect_id=item.defect_id,
            from_status=old_status,
            to_status=new_status,
            handler=draft.handler,
            remark=draft.remark,
            batch_id=state.batch_id,
            parent_log_id=parent_log_id,
            draft_id=draft_id
        )
        state.add_review_log(log_entry)

        review_entries.append({
            "defect_id": item.defect_id,
            "from_status": old_status,
            "to_status": new_status,
            "handler": draft.handler,
            "remark": draft.remark
        })
        success_count += 1

    if success_count > 0:
        state.push_undo(
            action=f"执行草稿 {draft.name} ({draft_id}) {success_count}条→{STATUS_NAMES[draft.target_status]}",
            snapshot=snapshot,
            review_entries=review_entries
        )

        execution_result = DraftExecutionResult(
            execution_id=execution_id,
            executed_at=datetime.now().isoformat(),
            success_count=success_count,
            error_count=0,
            errors=[]
        )

        draft.status = "executed"
        draft.execution = execution_result
        state.update_draft(draft)

        state.save()

        return execution_result

    return DraftExecutionResult()


def void_draft(
    state: PatrolState,
    draft_id: str,
    reason: str = ""
) -> DraftEntry:
    """作废草稿"""
    draft = state.get_draft(draft_id)
    if not draft:
        raise WorkflowError(f"草稿不存在: {draft_id}")

    if draft.status == "executed":
        raise WorkflowError(f"草稿已执行，不能作废: {draft_id}")

    if draft.status == "voided":
        raise WorkflowError(f"草稿已作废: {draft_id}")

    draft.status = "voided"
    if reason:
        draft.remark = (draft.remark + " | " if draft.remark else "") + f"作废原因: {reason}"

    state.update_draft(draft)
    state.save_drafts()

    return draft


TEMPLATE_REQUIRED_FIELDS = ["name", "target_status"]


def create_template(
    state: PatrolState,
    name: str,
    target_status: str,
    handler: str = "",
    remark: str = "",
    source_type: str = "",
    description: str = ""
) -> DraftTemplate:
    """
    创建复核方案模板。

    Args:
        state: 状态对象
        name: 模板名称
        target_status: 目标状态
        handler: 默认处理人
        remark: 备注模板
        source_type: 来源方式
        description: 模板描述

    Returns:
        创建好的 DraftTemplate
    """
    if not name.strip():
        raise WorkflowError("模板名称不能为空")

    if target_status not in DEFECT_STATUSES:
        raise WorkflowError(f"无效的目标状态: {target_status}")

    existing = state.get_template_by_name(name)
    if existing:
        raise WorkflowError(f"模板名称已存在: {name}")

    template_id = generate_template_id()
    now = datetime.now().isoformat()

    template = DraftTemplate(
        template_id=template_id,
        name=name.strip(),
        target_status=target_status,
        handler=handler,
        remark=remark,
        source_type=source_type,
        description=description,
        created_at=now,
        updated_at=now
    )

    state.add_template(template)
    state.save_templates()

    return template


def update_template(
    state: PatrolState,
    template_id: str,
    name: Optional[str] = None,
    target_status: Optional[str] = None,
    handler: Optional[str] = None,
    remark: Optional[str] = None,
    source_type: Optional[str] = None,
    description: Optional[str] = None
) -> DraftTemplate:
    """
    更新模板。

    只有传入的参数（非 None）才会更新。
    传空字符串可以清空对应字段（name 除外，name 不能为空）。
    """
    template = state.get_template(template_id)
    if not template:
        raise WorkflowError(f"模板不存在: {template_id}")

    if name is not None:
        if not name.strip():
            raise WorkflowError("模板名称不能为空")
        existing = state.get_template_by_name(name.strip())
        if existing and existing.template_id != template_id:
            raise WorkflowError(f"模板名称已存在: {name}")
        template.name = name.strip()

    if target_status is not None:
        if target_status not in DEFECT_STATUSES:
            raise WorkflowError(f"无效的目标状态: {target_status}")
        template.target_status = target_status

    if handler is not None:
        template.handler = handler
    if remark is not None:
        template.remark = remark
    if source_type is not None:
        template.source_type = source_type
    if description is not None:
        template.description = description

    template.updated_at = datetime.now().isoformat()

    state.update_template(template)
    state.save_templates()

    return template


def delete_template(state: PatrolState, template_id: str) -> bool:
    """删除模板"""
    template = state.get_template(template_id)
    if not template:
        raise WorkflowError(f"模板不存在: {template_id}")

    success = state.delete_template(template_id)
    if success:
        state.save_templates()
    return success


def create_draft_from_template(
    state: PatrolState,
    template_id: str,
    source: str,
    source_type: str,
    name: str = "",
    status: str = "",
    handler: str = "",
    remark: str = "",
    created_by: str = ""
) -> DraftEntry:
    """
    从模板创建草稿，支持命令行参数覆盖。

    优先级：命令行参数 > 模板值

    Args:
        state: 状态对象
        template_id: 模板ID
        source: 缺陷来源
        source_type: 来源类型
        name: 草稿名称（覆盖模板）
        status: 目标状态（覆盖模板）
        handler: 处理人（覆盖模板）
        remark: 备注（覆盖模板）
        created_by: 创建人

    Returns:
        创建好的 DraftEntry
    """
    template = state.get_template(template_id)
    if not template:
        raise WorkflowError(f"模板不存在: {template_id}")

    target_status = status if status else template.target_status
    final_handler = handler if handler else template.handler
    final_remark = remark if remark else template.remark
    draft_name = name if name else template.name

    if not target_status:
        raise WorkflowError("目标状态不能为空")

    draft = create_draft(
        state,
        source=source,
        source_type=source_type,
        target_status=target_status,
        name=draft_name,
        handler=final_handler,
        remark=final_remark,
        created_by=created_by
    )

    draft.template_id = template.template_id
    draft.template_snapshot = template.to_dict()

    state.update_draft(draft)
    state.save_drafts()

    return draft


def import_templates(
    state: PatrolState,
    file_path: str,
    overwrite: bool = False
) -> dict:
    """
    从 JSON 文件导入模板。

    规则：
    - 缺字段的模板直接拒绝，不导入
    - 同名模板：overwrite=True 则覆盖，overwrite=False 则跳过并提示
    - 重复导入（相同 template_id）视为同名处理逻辑

    Args:
        state: 状态对象
        file_path: JSON 文件路径
        overwrite: 是否覆盖同名模板

    Returns:
        包含导入统计信息的字典
    """
    import json
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        raise WorkflowError(f"文件不存在: {file_path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise WorkflowError(f"JSON 解析失败: {e}")

    if isinstance(data, dict):
        templates_data = list(data.values())
    elif isinstance(data, list):
        templates_data = data
    else:
        raise WorkflowError("JSON 格式不正确，应为列表或对象")

    imported = []
    skipped = []
    errors = []

    for idx, tpl_data in enumerate(templates_data, 1):
        missing = [f for f in TEMPLATE_REQUIRED_FIELDS if f not in tpl_data or not tpl_data.get(f)]
        if missing:
            errors.append(f"第{idx}条: 缺少必填字段 {', '.join(missing)}")
            continue

        name = tpl_data.get("name", "").strip()
        target_status = tpl_data.get("target_status", "")

        if target_status not in DEFECT_STATUSES:
            errors.append(f"第{idx}条 ({name}): 无效的目标状态 {target_status}")
            continue

        existing_by_name = state.get_template_by_name(name)

        if existing_by_name:
            if overwrite:
                template_id = existing_by_name.template_id
                template = DraftTemplate.from_dict(tpl_data)
                template.template_id = template_id
                template.created_at = existing_by_name.created_at
                template.updated_at = datetime.now().isoformat()
                state.update_template(template)
                imported.append(f"{name} (覆盖)")
            else:
                skipped.append(f"{name} (同名已存在)")
            continue

        template_id = tpl_data.get("template_id", "")
        if template_id and state.get_template(template_id):
            existing = state.get_template(template_id)
            if overwrite:
                template = DraftTemplate.from_dict(tpl_data)
                template.template_id = template_id
                template.created_at = existing.created_at
                template.updated_at = datetime.now().isoformat()
                state.update_template(template)
                imported.append(f"{name} (按ID覆盖)")
            else:
                skipped.append(f"{name} (同ID已存在)")
            continue

        template = DraftTemplate.from_dict(tpl_data)
        if not template.template_id:
            template.template_id = generate_template_id()
        if not template.created_at:
            template.created_at = datetime.now().isoformat()
        if not template.updated_at:
            template.updated_at = template.created_at

        state.add_template(template)
        imported.append(name)

    state.save_templates()

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "error_count": len(errors)
    }


def export_templates(
    state: PatrolState,
    file_path: str,
    template_ids: Optional[List[str]] = None
) -> int:
    """
    导出模板到 JSON 文件。

    Args:
        state: 状态对象
        file_path: 输出文件路径
        template_ids: 指定模板ID列表，None 则导出全部

    Returns:
        导出的模板数量
    """
    import json
    from pathlib import Path

    if template_ids:
        templates = []
        for tid in template_ids:
            tpl = state.get_template(tid)
            if tpl:
                templates.append(tpl)
    else:
        templates = state.list_templates()

    data = {tpl.template_id: tpl.to_dict() for tpl in templates}

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return len(templates)


SNAPSHOT_KEY_FIELDS = [
    ("name", "模板名称"),
    ("target_status", "目标状态"),
    ("handler", "处理人"),
]


def _classify_snapshot_for_check(tpl_snap):
    if not tpl_snap:
        return "missing", [label for _, label in SNAPSHOT_KEY_FIELDS]
    missing = []
    for key, label in SNAPSHOT_KEY_FIELDS:
        if key not in tpl_snap:
            missing.append(label)
    if missing:
        return "incomplete", missing
    return "complete", []


def snapshot_health_check(state: PatrolState) -> List[dict]:
    """
    快照体检：扫描所有草稿，对每条给出快照分类、缺失字段、可否补档及风险原因。
    不修改任何状态。

    返回 list[dict]，每条包含：
        draft_id, draft_name, draft_status,
        template_id, template_exists, template_name,
        snapshot_status, missing_fields, sealed,
        can_patch, cannot_patch_reason, risk_reason
    """
    results = []
    for draft in state.drafts.values():
        tpl_id = draft.template_id or ""
        tpl_snap = draft.template_snapshot or {}
        snapshot_status, missing_fields = _classify_snapshot_for_check(tpl_snap)
        sealed = bool(draft.snapshot_sealed_at)
        template_exists = False
        template_name = ""
        current_tpl = None

        if tpl_id:
            current_tpl = state.get_template(tpl_id)
            template_exists = current_tpl is not None
            if snapshot_status in ("complete", "incomplete"):
                template_name = tpl_snap.get("name", "")
            if not template_name and current_tpl:
                template_name = current_tpl.name
            if not template_name:
                template_name = tpl_id

        can_patch = False
        cannot_patch_reason = ""
        risk_reason = ""

        if not tpl_id:
            can_patch = False
            cannot_patch_reason = "非模板草稿，无需补档"
        elif sealed:
            can_patch = False
            cannot_patch_reason = "快照已封存，不可重复补档"
        elif snapshot_status == "complete":
            can_patch = True
            cannot_patch_reason = ""
        elif snapshot_status == "incomplete":
            if template_exists:
                can_patch = True
            else:
                can_patch = False
                cannot_patch_reason = "残缺快照且模板已删除，无法补齐缺失字段"
        else:
            if template_exists:
                can_patch = True
            else:
                can_patch = False
                cannot_patch_reason = "无快照且模板已删除，无法补档"

        if snapshot_status == "complete" and not sealed:
            risk_reason = "快照完整但未封存，建议封存以确保不可变"
        elif snapshot_status == "incomplete":
            if template_exists:
                conflict_fields = []
                for key, label in SNAPSHOT_KEY_FIELDS:
                    if key in tpl_snap and current_tpl is not None:
                        snap_val = tpl_snap[key]
                        cur_val = getattr(current_tpl, key, None)
                        if snap_val and cur_val and snap_val != cur_val:
                            conflict_fields.append(label)
                if conflict_fields:
                    risk_reason = f"快照与当前模板不一致: {','.join(conflict_fields)}，补档将用当前模板覆盖"
                else:
                    risk_reason = f"残缺快照(缺{','.join(missing_fields)})，可从当前模板补齐"
            else:
                risk_reason = "残缺快照且模板已删除，信息永久丢失"
        elif snapshot_status == "missing":
            if template_exists:
                risk_reason = "老数据无快照，可从当前模板补档（快照反映当前模板状态，非创建时状态）"
            else:
                risk_reason = "老数据无快照且模板已删除，无法恢复"
        elif sealed:
            risk_reason = ""

        results.append({
            "draft_id": draft.draft_id,
            "draft_name": draft.name,
            "draft_status": draft.status,
            "template_id": tpl_id,
            "template_exists": template_exists,
            "template_name": template_name,
            "snapshot_status": snapshot_status,
            "missing_fields": missing_fields,
            "sealed": sealed,
            "can_patch": can_patch,
            "cannot_patch_reason": cannot_patch_reason,
            "risk_reason": risk_reason,
        })

    results.sort(key=lambda r: r["draft_id"])
    return results


def _validate_patch_batch(
    state: PatrolState,
    draft_ids: List[str]
) -> Tuple[List[str], List[str]]:
    """
    批次校验：检查重复记录、来源冲突、关键字段对不上。
    返回 (patchable_ids, errors)。
    任一错误则整批失败。
    """
    errors = []
    seen = set()
    for did in draft_ids:
        if did in seen:
            errors.append(f"{did}: 重复的草稿ID")
            continue
        seen.add(did)

    for did in draft_ids:
        draft = state.get_draft(did)
        if not draft:
            errors.append(f"{did}: 草稿不存在")
            continue

        if draft.snapshot_sealed_at:
            errors.append(f"{did}: 快照已封存，不可重复补档")
            continue

        if not draft.template_id:
            errors.append(f"{did}: 非模板草稿，无需补档")
            continue

        tpl_snap = draft.template_snapshot or {}
        snapshot_status, _ = _classify_snapshot_for_check(tpl_snap)

        current_tpl = state.get_template(draft.template_id)

        if snapshot_status == "missing" and not current_tpl:
            errors.append(f"{did}: 无快照且模板已删除，无法补档")
            continue

        if snapshot_status == "incomplete" and not current_tpl:
            errors.append(f"{did}: 残缺快照且模板已删除，无法补齐")
            continue

        if snapshot_status == "incomplete" and current_tpl:
            for key, label in SNAPSHOT_KEY_FIELDS:
                if key in tpl_snap:
                    snap_val = tpl_snap[key]
                    cur_val = getattr(current_tpl, key, None)
                    if snap_val and cur_val and snap_val != cur_val:
                        errors.append(
                            f"{did}: 来源冲突 - 快照{label}='{snap_val}'与当前模板{label}='{cur_val}'不一致"
                        )

    if errors:
        return [], errors

    health = snapshot_health_check(state)
    id_to_health = {h["draft_id"]: h for h in health}

    patchable = []
    for did in draft_ids:
        h = id_to_health.get(did)
        if h and h["can_patch"]:
            patchable.append(did)

    return patchable, []


def snapshot_patch(
    state: PatrolState,
    draft_ids: List[str]
) -> dict:
    """
    快照补档：把当时模板封成只读副本写入 template_snapshot，并设置 snapshot_sealed_at。
    先做批次校验，有冲突/重复/字段对不上则整批失败并记审计日志。

    返回 dict:
        patched: list[str] - 成功补档的 draft_id
        errors: list[str] - 失败原因（整批失败时非空）
        audit_id: str - 审计日志ID
    """
    now = datetime.now().isoformat()
    audit_id = generate_audit_id()

    patchable, errors = _validate_patch_batch(state, draft_ids)

    if errors:
        detail = "; ".join(errors)
        audit_entry = AuditLogEntry(
            audit_id=audit_id,
            action="snapshot_patch",
            target_type="batch",
            target_id=",".join(draft_ids),
            detail=detail,
            result="failed",
            timestamp=now
        )
        state.add_audit_log(audit_entry)
        state.save_audit_logs()
        return {
            "patched": [],
            "errors": errors,
            "audit_id": audit_id
        }

    patched = []
    for did in patchable:
        draft = state.get_draft(did)
        if not draft:
            continue

        current_tpl = state.get_template(draft.template_id)
        if not current_tpl:
            continue

        draft.template_snapshot = current_tpl.to_dict()
        draft.snapshot_sealed_at = now
        state.update_draft(draft)
        patched.append(did)

    if patched:
        detail = f"补档成功 {len(patched)} 条: {','.join(patched)}"
        audit_entry = AuditLogEntry(
            audit_id=audit_id,
            action="snapshot_patch",
            target_type="batch",
            target_id=",".join(patched),
            detail=detail,
            result="success",
            timestamp=now
        )
        state.add_audit_log(audit_entry)
        state.save_drafts()
        state.save_audit_logs()

    return {
        "patched": patched,
        "errors": [],
        "audit_id": audit_id
    }
