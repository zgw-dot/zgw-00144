"""工作流模块 - 状态流转和撤销"""

from datetime import datetime
from typing import Optional
from .models import DefectRecord, STATUS_NAMES, DEFECT_STATUSES
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


def review_defect(
    state: PatrolState,
    defect_id: str,
    new_status: str,
    remark: str = "",
    handler: str = ""
) -> DefectRecord:
    """
    复核缺陷，变更状态。
    会推入撤销栈。
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

    state.push_undo(
        action=f"复核 {defect_id} {STATUS_NAMES[old_status]}→{STATUS_NAMES[new_status]}",
        snapshot=snapshot
    )

    state.save()

    return defect


def undo_last(state: PatrolState) -> Optional[str]:
    """撤销最后一步操作"""
    if not state.can_undo():
        return None

    undo_item = state.pop_undo()
    state.restore_defects(undo_item["snapshot"])
    state.save()

    return undo_item["action"]


def batch_review(
    state: PatrolState,
    defect_ids: list,
    new_status: str,
    remark: str = "",
    handler: str = ""
) -> tuple:
    """批量复核"""
    if not defect_ids:
        return 0, []

    snapshot = state.snapshot_defects()

    success_count = 0
    errors = []

    for defect_id in defect_ids:
        defect = state.get_defect(defect_id)
        if not defect:
            errors.append(f"{defect_id}: 缺陷不存在")
            continue

        old_status = defect.status
        if old_status == new_status:
            errors.append(f"{defect_id}: 已经是 {STATUS_NAMES[new_status]}")
            continue

        if not can_transition(old_status, new_status):
            errors.append(f"{defect_id}: 不允许 {STATUS_NAMES[old_status]}→{STATUS_NAMES[new_status]}")
            continue

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
        success_count += 1

    if success_count > 0:
        state.push_undo(
            action=f"批量复核 {success_count} 条→{STATUS_NAMES[new_status]}",
            snapshot=snapshot
        )
        state.save()

    return success_count, errors
