"""工作流模块 - 状态流转和撤销"""

from datetime import datetime
from typing import Optional, List, Tuple
from .models import (
    DefectRecord, STATUS_NAMES, DEFECT_STATUSES, ReviewLogEntry, generate_log_id,
    DraftEntry, DraftItem, DraftExecutionResult, generate_draft_id
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
