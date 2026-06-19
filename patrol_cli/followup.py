"""整改回访计划工作流模块"""

from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple

from .models import (
    FollowUpPlan, FollowUpPlanItem, generate_followup_id,
    FOLLOWUP_STATUSES, FOLLOWUP_STATUS_NAMES,
    FOLLOWUP_ITEM_STATUSES, FOLLOWUP_ITEM_STATUS_NAMES,
    STATUS_NAMES, FollowUpCreatePreview
)
from .storage import PatrolState
from .config import RulesConfig


class FollowUpError(Exception):
    """回访计划工作流错误"""
    pass


def _collect_defect_ids_from_source(
    state: PatrolState,
    defect_ids: Optional[List[str]] = None,
    building: Optional[str] = None,
    status: Optional[str] = None,
) -> List[str]:
    if defect_ids:
        return list(defect_ids)

    defects = state.list_defects(status=status, building=building)
    return [d.defect_id for d in defects]


def _calculate_deadline(
    config: RulesConfig,
    severity: str,
    override_hours: Optional[int] = None
) -> str:
    hours = override_hours if override_hours is not None else config.get_rectify_hours(severity)
    deadline = datetime.now() + timedelta(hours=hours)
    return deadline.isoformat()


def _validate_candidates(
    state: PatrolState,
    defect_ids: List[str],
    status_fingerprint: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """
    校验候选缺陷集合，返回所有冲突（一次性报全）。

    Returns:
        (active_plan_conflicts, status_drift_conflicts, not_found, duplicates)
    """
    seen = set()
    duplicates = []
    not_found = []
    status_drift_conflicts = []
    active_plan_conflicts = []

    for defect_id in defect_ids:
        if defect_id in seen:
            duplicates.append(defect_id)
            continue
        seen.add(defect_id)

        defect = state.get_defect(defect_id)
        if not defect:
            not_found.append(defect_id)
            continue

        if status_fingerprint and defect_id in status_fingerprint:
            expected = status_fingerprint[defect_id]
            if defect.status != expected:
                expected_name = STATUS_NAMES.get(expected, expected)
                actual_name = STATUS_NAMES.get(defect.status, defect.status)
                status_drift_conflicts.append(
                    f"{defect_id}: 状态已变更（预览时={expected_name}, 当前={actual_name}）"
                )

        active_plans = state.get_followup_plans_for_defect(defect_id, active_only=True)
        if active_plans:
            plan_names = [f"{p.name}({p.plan_id})" for p in active_plans]
            active_plan_conflicts.append(
                f"{defect_id}: 已存在于未完成计划中: {', '.join(plan_names)}"
            )

    return active_plan_conflicts, status_drift_conflicts, not_found, duplicates


def _prepare_candidates(
    state: PatrolState,
    config: RulesConfig,
    name: str,
    defect_ids: Optional[List[str]] = None,
    building: Optional[str] = None,
    status: Optional[str] = None,
    handler: str = "",
    remark: str = "",
    created_by: str = "",
    deadline_override_hours: Optional[int] = None,
    deadline_override: Optional[str] = None,
    status_fingerprint: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[str], Dict[str, str]]:
    """
    构建候选集并做整批校验——preview 与 create 共用此入口。

    Returns:
        (items, conflicts, warnings, fingerprint)
        fingerprint: {defect_id: status} 供后续 create 做快照校验
    """
    if not name.strip():
        raise FollowUpError("计划名称不能为空")

    ids = _collect_defect_ids_from_source(state, defect_ids, building, status)

    if not ids:
        raise FollowUpError("未找到符合条件的缺陷记录")

    active_conflicts, status_drift, not_found, duplicates = _validate_candidates(
        state, ids, status_fingerprint=status_fingerprint
    )

    items = []
    warnings = []
    conflicts = []

    if duplicates:
        conflicts.extend([f"{did}: 重复的缺陷编号" for did in duplicates])

    if not_found:
        conflicts.extend([f"{did}: 缺陷不存在" for did in not_found])

    if active_conflicts:
        conflicts.extend(active_conflicts)

    if status_drift:
        conflicts.extend(status_drift)

    skipped = set(duplicates) | set(not_found)
    fingerprint: Dict[str, str] = {}

    for defect_id in ids:
        if defect_id in skipped:
            continue

        defect = state.get_defect(defect_id)
        if not defect:
            continue

        fingerprint[defect_id] = defect.status

        if deadline_override:
            deadline = deadline_override
        else:
            deadline = _calculate_deadline(config, defect.severity, deadline_override_hours)

        item_info = {
            "defect_id": defect_id,
            "building": defect.building,
            "device_id": defect.device_id,
            "severity": defect.severity,
            "current_status": defect.status,
            "current_status_name": STATUS_NAMES.get(defect.status, defect.status),
            "description": defect.description,
            "deadline": deadline,
            "defect_snapshot": defect.to_dict()
        }
        items.append(item_info)

    return items, conflicts, warnings, fingerprint


def preview_create_followup(
    state: PatrolState,
    config: RulesConfig,
    name: str,
    defect_ids: Optional[List[str]] = None,
    building: Optional[str] = None,
    status: Optional[str] = None,
    handler: str = "",
    remark: str = "",
    created_by: str = "",
    deadline_override_hours: Optional[int] = None,
    deadline_override: Optional[str] = None,
) -> FollowUpCreatePreview:
    """
    预览创建回访计划（dry-run）。

    返回预览结果，包含将包含的缺陷、冲突、警告和状态指纹。
    """
    items, conflicts, warnings, fingerprint = _prepare_candidates(
        state, config, name,
        defect_ids=defect_ids,
        building=building,
        status=status,
        handler=handler,
        remark=remark,
        created_by=created_by,
        deadline_override_hours=deadline_override_hours,
        deadline_override=deadline_override,
    )

    can_create = len(conflicts) == 0

    return FollowUpCreatePreview(
        name=name,
        handler=handler,
        deadline=deadline_override or "",
        remark=remark,
        items=items,
        conflicts=conflicts,
        warnings=warnings,
        total_count=len(items),
        can_create=can_create,
        status_fingerprint=fingerprint,
    )


def create_followup_plan(
    state: PatrolState,
    config: RulesConfig,
    name: str,
    defect_ids: Optional[List[str]] = None,
    building: Optional[str] = None,
    status: Optional[str] = None,
    handler: str = "",
    remark: str = "",
    created_by: str = "",
    deadline_override_hours: Optional[int] = None,
    deadline_override: Optional[str] = None,
    status_fingerprint: Optional[Dict[str, str]] = None,
) -> FollowUpPlan:
    """
    创建回访计划。

    整批校验：如果有任何冲突（缺陷在其他未完成计划中、不存在、重复、
    或状态自预览后发生了变更），整批失败，不创建任何计划，
    不写入任何日志或状态。

    若提供了 status_fingerprint（来自 preview 返回值），
    则校验每个缺陷当前状态是否与指纹一致，不一致即冲突。
    """
    items, conflicts, warnings, fingerprint = _prepare_candidates(
        state, config, name,
        defect_ids=defect_ids,
        building=building,
        status=status,
        handler=handler,
        remark=remark,
        created_by=created_by,
        deadline_override_hours=deadline_override_hours,
        deadline_override=deadline_override,
        status_fingerprint=status_fingerprint,
    )

    if conflicts:
        error_msg = "回访计划创建冲突，整批不创建：\n" + "\n".join(conflicts)
        raise FollowUpError(error_msg)

    plan_id = generate_followup_id()
    now = datetime.now().isoformat()

    plan_items = []
    for item_info in items:
        item = FollowUpPlanItem(
            defect_id=item_info["defect_id"],
            defect_snapshot=item_info["defect_snapshot"],
            item_status="pending",
            result="",
            result_remark="",
            result_at="",
            result_by=""
        )
        plan_items.append(item)

    plan = FollowUpPlan(
        plan_id=plan_id,
        name=name.strip(),
        handler=handler,
        deadline=items[0]["deadline"] if items and not deadline_override else (deadline_override or ""),
        remark=remark,
        created_at=now,
        created_by=created_by,
        status="pending",
        items=plan_items,
        dispatched_at="",
        dispatched_by="",
        completed_at="",
        cancelled_at="",
        cancel_reason=""
    )

    state.add_followup_plan(plan)
    state.save_followup_plans()

    return plan


def dispatch_followup_plan(
    state: PatrolState,
    plan_id: str,
    handler: str = "",
    dispatched_by: str = "",
) -> FollowUpPlan:
    plan = state.get_followup_plan(plan_id)
    if not plan:
        raise FollowUpError(f"回访计划不存在: {plan_id}")

    if plan.status == "dispatched":
        raise FollowUpError(f"计划已签收: {plan_id}")

    if plan.status == "completed":
        raise FollowUpError(f"计划已完成，不能签收: {plan_id}")

    if plan.status == "cancelled":
        raise FollowUpError(f"计划已撤销，不能签收: {plan_id}")

    if plan.status != "pending":
        raise FollowUpError(f"计划状态不允许签收: {plan.status}")

    plan.status = "dispatched"
    plan.dispatched_at = datetime.now().isoformat()
    plan.dispatched_by = dispatched_by
    if handler:
        plan.handler = handler

    state.update_followup_plan(plan)
    state.save_followup_plans()

    return plan


def complete_followup_item(
    state: PatrolState,
    plan_id: str,
    defect_id: str,
    result: str = "",
    result_remark: str = "",
    result_by: str = "",
) -> FollowUpPlan:
    plan = state.get_followup_plan(plan_id)
    if not plan:
        raise FollowUpError(f"回访计划不存在: {plan_id}")

    if plan.status not in ("dispatched", "pending"):
        raise FollowUpError(f"计划状态不允许完成回访: {plan.status}")

    item = None
    for it in plan.items:
        if it.defect_id == defect_id:
            item = it
            break

    if not item:
        raise FollowUpError(f"计划中不存在该缺陷: {defect_id}")

    if item.item_status == "completed":
        raise FollowUpError(f"该缺陷已完成回访: {defect_id}")

    if item.item_status == "cancelled":
        raise FollowUpError(f"该缺陷回访已取消: {defect_id}")

    item.item_status = "completed"
    item.result = result
    item.result_remark = result_remark
    item.result_at = datetime.now().isoformat()
    item.result_by = result_by

    all_completed = all(
        it.item_status in ("completed", "cancelled")
        for it in plan.items
    )
    if all_completed:
        plan.status = "completed"
        plan.completed_at = datetime.now().isoformat()

    state.update_followup_plan(plan)
    state.save_followup_plans()

    return plan


def complete_followup_plan(
    state: PatrolState,
    plan_id: str,
    results: Optional[Dict[str, Dict[str, str]]] = None,
    result_by: str = "",
) -> FollowUpPlan:
    plan = state.get_followup_plan(plan_id)
    if not plan:
        raise FollowUpError(f"回访计划不存在: {plan_id}")

    if plan.status not in ("dispatched", "pending"):
        raise FollowUpError(f"计划状态不允许完成: {plan.status}")

    results = results or {}
    now = datetime.now().isoformat()

    for item in plan.items:
        if item.item_status == "completed" or item.item_status == "cancelled":
            continue

        item_data = results.get(item.defect_id, {})
        item.item_status = "completed"
        item.result = item_data.get("result", "")
        item.result_remark = item_data.get("result_remark", "")
        item.result_at = now
        item.result_by = result_by

    plan.status = "completed"
    plan.completed_at = now

    state.update_followup_plan(plan)
    state.save_followup_plans()

    return plan


def cancel_followup_plan(
    state: PatrolState,
    plan_id: str,
    reason: str = "",
) -> FollowUpPlan:
    plan = state.get_followup_plan(plan_id)
    if not plan:
        raise FollowUpError(f"回访计划不存在: {plan_id}")

    if plan.status == "cancelled":
        raise FollowUpError(f"计划已撤销: {plan_id}")

    if plan.status == "completed":
        raise FollowUpError(f"计划已完成，不能撤销: {plan_id}")

    plan.status = "cancelled"
    plan.cancelled_at = datetime.now().isoformat()
    plan.cancel_reason = reason

    for item in plan.items:
        if item.item_status == "pending":
            item.item_status = "cancelled"

    state.update_followup_plan(plan)
    state.save_followup_plans()

    return plan


def get_followup_plan_detail(
    state: PatrolState,
    plan_id: str,
) -> dict:
    plan = state.get_followup_plan(plan_id)
    if not plan:
        raise FollowUpError(f"回访计划不存在: {plan_id}")

    items_detail = []
    for item in plan.items:
        current_defect = state.get_defect(item.defect_id)
        current_status = current_defect.status if current_defect else "(已删除)"
        current_status_name = STATUS_NAMES.get(current_status, current_status) if current_defect else "(已删除)"

        snapshot_status = item.defect_snapshot.get("status", "")
        snapshot_status_name = STATUS_NAMES.get(snapshot_status, snapshot_status)

        item_detail = {
            "defect_id": item.defect_id,
            "item_status": item.item_status,
            "item_status_name": FOLLOWUP_ITEM_STATUS_NAMES.get(item.item_status, item.item_status),
            "snapshot_status": snapshot_status,
            "snapshot_status_name": snapshot_status_name,
            "current_status": current_status,
            "current_status_name": current_status_name,
            "snapshot_building": item.defect_snapshot.get("building", ""),
            "snapshot_device_id": item.defect_snapshot.get("device_id", ""),
            "snapshot_severity": item.defect_snapshot.get("severity", ""),
            "snapshot_description": item.defect_snapshot.get("description", ""),
            "result": item.result,
            "result_remark": item.result_remark,
            "result_at": item.result_at,
            "result_by": item.result_by,
        }
        items_detail.append(item_detail)

    return {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "handler": plan.handler,
        "deadline": plan.deadline,
        "remark": plan.remark,
        "created_at": plan.created_at,
        "created_by": plan.created_by,
        "status": plan.status,
        "status_name": FOLLOWUP_STATUS_NAMES.get(plan.status, plan.status),
        "dispatched_at": plan.dispatched_at,
        "dispatched_by": plan.dispatched_by,
        "completed_at": plan.completed_at,
        "cancelled_at": plan.cancelled_at,
        "cancel_reason": plan.cancel_reason,
        "total_items": len(plan.items),
        "completed_items": sum(1 for i in plan.items if i.item_status == "completed"),
        "pending_items": sum(1 for i in plan.items if i.item_status == "pending"),
        "cancelled_items": sum(1 for i in plan.items if i.item_status == "cancelled"),
        "items": items_detail
    }


def list_followup_plans(
    state: PatrolState,
    status: Optional[str] = None,
    handler: Optional[str] = None,
    limit: int = 0,
) -> List[dict]:
    plans = state.list_followup_plans(status=status, handler=handler, limit=limit)

    result = []
    for plan in plans:
        completed = sum(1 for i in plan.items if i.item_status == "completed")
        pending = sum(1 for i in plan.items if i.item_status == "pending")
        cancelled = sum(1 for i in plan.items if i.item_status == "cancelled")

        result.append({
            "plan_id": plan.plan_id,
            "name": plan.name,
            "status": plan.status,
            "status_name": FOLLOWUP_STATUS_NAMES.get(plan.status, plan.status),
            "handler": plan.handler,
            "deadline": plan.deadline,
            "created_at": plan.created_at,
            "total_items": len(plan.items),
            "completed_items": completed,
            "pending_items": pending,
            "cancelled_items": cancelled,
        })

    return result


def export_followup_plans_json(
    state: PatrolState,
    file_path: str,
    plan_ids: Optional[List[str]] = None,
) -> int:
    import json
    from pathlib import Path

    if plan_ids:
        plans = []
        for pid in plan_ids:
            plan = state.get_followup_plan(pid)
            if plan:
                plans.append(plan)
    else:
        plans = state.list_followup_plans()

    data = {plan.plan_id: plan.to_dict() for plan in plans}

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return len(plans)


def export_followup_plans_csv(
    state: PatrolState,
    file_path: str,
    plan_ids: Optional[List[str]] = None,
) -> int:
    import csv
    from pathlib import Path

    if plan_ids:
        plans = []
        for pid in plan_ids:
            plan = state.get_followup_plan(pid)
            if plan:
                plans.append(plan)
    else:
        plans = state.list_followup_plans()

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    headers = [
        "计划ID", "计划名称", "计划状态", "回访人", "截止时间",
        "创建时间", "缺陷编号", "楼栋", "设备编号", "严重等级",
        "创建时状态", "回访状态", "回访结果", "回访备注",
        "回访时间", "回访人", "缺陷描述"
    ]

    rows = []
    for plan in plans:
        plan_status_name = FOLLOWUP_STATUS_NAMES.get(plan.status, plan.status)

        for item in plan.items:
            snap = item.defect_snapshot
            item_status_name = FOLLOWUP_ITEM_STATUS_NAMES.get(item.item_status, item.item_status)
            snap_status_name = STATUS_NAMES.get(snap.get("status", ""), snap.get("status", ""))

            rows.append([
                plan.plan_id,
                plan.name,
                plan_status_name,
                plan.handler,
                plan.deadline,
                plan.created_at,
                item.defect_id,
                snap.get("building", ""),
                snap.get("device_id", ""),
                snap.get("severity", ""),
                snap_status_name,
                item_status_name,
                item.result,
                item.result_remark,
                item.result_at,
                item.result_by,
                snap.get("description", "")
            ])

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    return len(rows)


def import_followup_plans_json(
    state: PatrolState,
    file_path: str,
    overwrite: bool = False,
) -> dict:
    import json
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        raise FollowUpError(f"文件不存在: {file_path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise FollowUpError(f"JSON 解析失败: {e}")

    if isinstance(data, dict):
        plans_data = list(data.values())
    elif isinstance(data, list):
        plans_data = data
    else:
        raise FollowUpError("JSON 格式不正确，应为列表或对象")

    imported = []
    skipped = []
    errors = []

    for idx, plan_data in enumerate(plans_data, 1):
        plan_id = plan_data.get("plan_id", "")
        name = plan_data.get("name", "").strip()

        if not name:
            errors.append(f"第{idx}条: 缺少计划名称")
            continue

        if plan_id and state.get_followup_plan(plan_id):
            if overwrite:
                plan = FollowUpPlan.from_dict(plan_data)
                state.update_followup_plan(plan)
                imported.append(f"{name} (覆盖)")
            else:
                skipped.append(f"{name} (同ID已存在)")
            continue

        plan = FollowUpPlan.from_dict(plan_data)
        if not plan.plan_id:
            plan.plan_id = generate_followup_id()
        if not plan.created_at:
            plan.created_at = datetime.now().isoformat()

        state.add_followup_plan(plan)
        imported.append(name)

    state.save_followup_plans()

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "error_count": len(errors)
    }
