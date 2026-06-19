"""工作流模块 - 状态流转和撤销"""

from datetime import datetime
from typing import Optional, List, Tuple
from .models import DefectRecord, STATUS_NAMES, DEFECT_STATUSES, ReviewLogEntry, generate_log_id
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
    parent_log_id: str = ""
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
        parent_log_id=parent_log_id
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


def undo_last(state: PatrolState) -> Optional[str]:
    """撤销最后一步操作"""
    if not state.can_undo():
        return None

    undo_item = state.pop_undo()
    action = undo_item["action"]

    state.restore_defects(undo_item["snapshot"])

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
            parent_log_id=""
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
            parent_log_id=""
        )
        state.add_review_log(undo_log)

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
