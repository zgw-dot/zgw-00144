"""导出模块 - CSV 和 HTML 报告"""

import csv
from typing import Optional
from datetime import datetime

from .models import STATUS_NAMES, DRAFT_STATUS_NAMES
from .config import RulesConfig
from .storage import PatrolState


def _resolve_template_fields(state, draft):
    """
    统一解析导出用的模板字段。
    返回 (template_id, template_name, template_note)
    """
    tpl_id = getattr(draft, "template_id", "") or ""
    tpl_snap = getattr(draft, "template_snapshot", None) or {}
    has_snapshot = bool(tpl_snap)
    tpl_name = ""
    tpl_note = ""

    if not tpl_id:
        return "", "未使用模板", "手动创建"

    current_tpl = state.get_template(tpl_id) if state and hasattr(state, "get_template") else None
    template_exists = current_tpl is not None

    if has_snapshot:
        tpl_name = tpl_snap.get("name", "")

    if not tpl_name and template_exists:
        tpl_name = current_tpl.name

    if not tpl_name:
        tpl_name = tpl_id

    if has_snapshot:
        if not template_exists:
            tpl_note = "模板已删除，但快照已保留"
        else:
            tpl_note = "已保存快照（不受模板后续变更影响）"
    else:
        if template_exists:
            tpl_note = "老数据，未保存模板快照（模板后续变更可能影响溯源）"
        else:
            tpl_note = "老数据，模板已删除且无快照"

    return tpl_id, tpl_name, tpl_note


def export_csv(
    state: PatrolState,
    output_path: str,
    status: Optional[str] = None,
    building: Optional[str] = None
) -> int:
    """导出 CSV 报告，返回导出条数"""
    defects = state.list_defects(status=status, building=building)

    if not defects:
        return 0

    fieldnames = [
        "缺陷ID", "楼栋", "设备编号", "设备类别", "缺陷类型",
        "严重等级", "状态", "描述", "首次发现", "最后发现",
        "来源条数", "处理人", "复核备注"
    ]

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for d in defects:
            writer.writerow({
                "缺陷ID": d.defect_id,
                "楼栋": d.building,
                "设备编号": d.device_id,
                "设备类别": d.device_category,
                "缺陷类型": d.defect_type,
                "严重等级": d.severity,
                "状态": STATUS_NAMES.get(d.status, d.status),
                "描述": d.description,
                "首次发现": d.first_seen,
                "最后发现": d.last_seen,
                "来源条数": len(d.source_rows),
                "处理人": d.handler,
                "复核备注": d.review_remark
            })

    return len(defects)


def export_csv_with_sources(
    state: PatrolState,
    output_path: str,
    status: Optional[str] = None
) -> int:
    """导出带来源行明细的 CSV，返回导出行数"""
    defects = state.list_defects(status=status)

    if not defects:
        return 0

    review_fields = []
    for i in range(1, 6):
        review_fields.extend([
            f"最近复核{i}_时间",
            f"最近复核{i}_状态变更",
            f"最近复核{i}_处理人",
            f"最近复核{i}_备注",
            f"最近复核{i}_类型"
        ])

    fieldnames = [
        "缺陷ID", "楼栋", "设备编号", "设备类别", "缺陷类型",
        "严重等级", "状态", "描述", "首次发现", "最后发现",
        "来源文件", "来源行号", "导入时间", "处理人", "复核备注",
    ] + review_fields

    type_labels = {"review": "单条复核", "batch_review": "批量复核", "undo": "撤销"}

    row_count = 0
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for d in defects:
            review_logs = state.get_review_logs(defect_id=d.defect_id, limit=5)
            review_row_data = {}
            for idx, log in enumerate(review_logs, 1):
                if idx > 5:
                    break
                prefix = f"最近复核{idx}_"
                from_name = STATUS_NAMES.get(log.from_status, log.from_status) if log.from_status else ""
                to_name = STATUS_NAMES.get(log.to_status, log.to_status) if log.to_status else ""
                review_row_data[prefix + "时间"] = log.timestamp[:19] if log.timestamp else ""
                review_row_data[prefix + "状态变更"] = f"{from_name} → {to_name}" if from_name and to_name else ""
                review_row_data[prefix + "处理人"] = log.handler
                review_row_data[prefix + "备注"] = log.remark
                review_row_data[prefix + "类型"] = type_labels.get(log.log_type, log.log_type)

            for sr in d.source_rows:
                row = {
                    "缺陷ID": d.defect_id,
                    "楼栋": d.building,
                    "设备编号": d.device_id,
                    "设备类别": d.device_category,
                    "缺陷类型": d.defect_type,
                    "严重等级": d.severity,
                    "状态": STATUS_NAMES.get(d.status, d.status),
                    "描述": d.description,
                    "首次发现": d.first_seen,
                    "最后发现": d.last_seen,
                    "来源文件": sr.source_file,
                    "来源行号": sr.line_number,
                    "导入时间": sr.import_time,
                    "处理人": d.handler,
                    "复核备注": d.review_remark,
                }
                row.update(review_row_data)
                writer.writerow(row)
                row_count += 1

    return row_count


def _severity_color(severity: str, config: Optional[RulesConfig]) -> str:
    if config:
        sev = config.get_severity(severity)
        if sev:
            return sev.color
    return "#666"


def _status_color(status: str) -> str:
    colors = {
        "pending": "#f59e0b",
        "dispatched": "#3b82f6",
        "false_positive": "#6b7280",
        "closed": "#10b981"
    }
    return colors.get(status, "#666")


def export_html(
    state: PatrolState,
    output_path: str,
    config: Optional[RulesConfig] = None,
    status: Optional[str] = None,
    building: Optional[str] = None,
    title: str = "物业设备巡检缺陷复核报告"
) -> int:
    """导出 HTML 报告，返回导出条数"""
    defects = state.list_defects(status=status, building=building)

    if not defects:
        defects_html = "<p>暂无数据</p>"
    else:
        rows_html = []
        for i, d in enumerate(defects, 1):
            status_color = _status_color(d.status)
            sev_color = _severity_color(d.severity, config)
            status_name = STATUS_NAMES.get(d.status, d.status)
            source_count = len(d.source_rows)

            source_details = []
            for sr in d.source_rows:
                source_details.append(
                    f"<li>{sr.source_file} 第{sr.line_number}行 "
                    f"（导入：{sr.import_time[:19]}）</li>"
                )
            sources_html = "".join(source_details)

            history_html = ""
            if d.status_history:
                history_items = []
                for h in d.status_history:
                    from_name = STATUS_NAMES.get(h["from"], h["from"]) if h["from"] else "无"
                    to_name = STATUS_NAMES.get(h["to"], h["to"])
                    history_items.append(
                        f"<li>{h['time'][:19]}: {from_name} → {to_name} "
                        f"[{h['handler']}] {h['remark']}</li>"
                    )
                history_html = (
                    f"<div class='history'><h5>状态历史</h5><ul>{''.join(history_items)}</ul></div>"
                )

            row = f"""
            <tr class="defect-row" onclick="toggleDetail('{d.defect_id}')">
                <td>{i}</td>
                <td class="mono">{d.defect_id}</td>
                <td>{d.building}</td>
                <td>{d.device_id}</td>
                <td>{d.defect_type}</td>
                <td><span class="badge sev" style="background:{sev_color}">{d.severity}</span></td>
                <td><span class="badge status" style="background:{status_color}">{status_name}</span></td>
                <td>{d.description[:40]}{'...' if len(d.description) > 40 else ''}</td>
                <td>{d.first_seen[:16]}</td>
                <td>{source_count}</td>
            </tr>
            <tr class="detail-row" id="detail-{d.defect_id}" style="display:none">
                <td colspan="10">
                    <div class="detail-content">
                        <div class="detail-section">
                            <h4>详细信息</h4>
                            <p><strong>设备类别：</strong>{d.device_category}</p>
                            <p><strong>完整描述：</strong>{d.description}</p>
                            <p><strong>最后发现：</strong>{d.last_seen}</p>
                            <p><strong>处理人：</strong>{d.handler or '-'}</p>
                            <p><strong>复核备注：</strong>{d.review_remark or '-'}</p>
                        </div>
                        <div class="detail-section">
                            <h4>来源记录（{source_count}条）</h4>
                            <ul class="source-list">{sources_html}</ul>
                        </div>
                        {history_html}
                    </div>
                </td>
            </tr>
            """
            rows_html.append(row)

        defects_html = "".join(rows_html)

    stats = state.stats()
    status_stats = stats["by_status"]
    total = stats["total"]

    status_cards = []
    for st, name in STATUS_NAMES.items():
        count = status_stats.get(st, 0)
        color = _status_color(st)
        status_cards.append(
            f'<div class="stat-card" style="border-left: 4px solid {color}">'
            f'<div class="stat-label">{name}</div>'
            f'<div class="stat-value">{count}</div>'
            f'</div>'
        )
    stats_html = "".join(status_cards)

    last_import_html = ""
    last_import = state.get_last_import_log("import")
    if last_import:
        result_label = {
            "success": "成功",
            "failed": "失败",
            "partial": "部分有效",
            "empty": "空文件"
        }.get(last_import.result, last_import.result)
        result_color = "#10b981" if last_import.result == "success" else "#ef4444"
        last_import_html = f"""
  <div class="last-import">
    <h3>最近一次导入</h3>
    <div class="import-info">
      <span class="import-file">📄 {last_import.filename}</span>
      <span class="import-result" style="color:{result_color}">{result_label}</span>
    </div>
    <div class="import-stats">
      <span>批次: {last_import.batch_id or '-'}</span>
      <span>时间: {last_import.timestamp[:19]}</span>
      <span>总行: {last_import.total_rows}</span>
      <span>有效: {last_import.valid_rows}</span>
      <span>新增缺陷: {last_import.new_defects}</span>
      <span>合并来源: {last_import.merged_defects}</span>
    </div>
    {f'<div class="import-error">错误: {last_import.error_summary}</div>' if last_import.error_summary else ''}
  </div>
        """.strip()

    recent_reviews_html = ""
    recent_reviews = state.get_review_logs(limit=5)
    if recent_reviews:
        type_labels = {"review": "单条复核", "batch_review": "批量复核", "draft_review": "草稿复核", "undo": "撤销"}
        review_rows = []
        for log in recent_reviews:
            type_label = type_labels.get(log.log_type, log.log_type)
            type_badge_color = {
                "review": "#3b82f6",
                "batch_review": "#8b5cf6",
                "draft_review": "#ec4899",
                "undo": "#f59e0b"
            }.get(log.log_type, "#6b7280")

            if log.log_type == "undo":
                status_text = log.remark
                defect_text = "-"
                handler_text = "-"
            else:
                from_name = STATUS_NAMES.get(log.from_status, log.from_status)
                to_name = STATUS_NAMES.get(log.to_status, log.to_status)
                status_text = f"{from_name} → {to_name}"
                defect_text = log.defect_id
                handler_text = log.handler or "-"

            draft_info = ""
            if log.draft_id:
                draft = state.get_draft(log.draft_id)
                draft_name = draft.name if draft else log.draft_id
                draft_info = f"<br><small>草稿: {log.draft_id} ({draft_name})</small>"

            review_rows.append(f"""
            <tr>
              <td><span class="badge" style="background:{type_badge_color}">{type_label}</span></td>
              <td class="mono">{defect_text}</td>
              <td>{status_text}{draft_info}</td>
              <td>{handler_text}</td>
              <td>{log.timestamp[:19] if log.timestamp else '-'}</td>
              <td>{log.remark if log.log_type != 'undo' else ''}</td>
            </tr>
            """)

        recent_reviews_html = f"""
  <div class="last-import">
    <h3>最近复核记录（最近 {len(recent_reviews)} 条）</h3>
    <table class="review-table">
      <thead>
        <tr>
          <th>操作类型</th>
          <th>缺陷ID</th>
          <th>状态变更</th>
          <th>处理人</th>
          <th>时间</th>
          <th>备注</th>
        </tr>
      </thead>
      <tbody>
        {''.join(review_rows)}
      </tbody>
    </table>
  </div>
        """.strip()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
          background: #f5f7fa; color: #333; padding: 20px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ text-align: center; color: #1e293b; margin-bottom: 20px; font-size: 24px; }}
  .meta {{ text-align: center; color: #64748b; margin-bottom: 20px; font-size: 14px; }}
  .stats {{ display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }}
  .stat-card {{ flex: 1; min-width: 140px; background: #fff; padding: 16px 20px; border-radius: 8px;
                 box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .stat-label {{ font-size: 13px; color: #64748b; margin-bottom: 6px; }}
  .stat-value {{ font-size: 28px; font-weight: bold; color: #1e293b; }}
  .last-import {{ background: #fff; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;
                  box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .last-import h3 {{ font-size: 14px; color: #475569; margin-bottom: 10px; }}
  .import-info {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .import-file {{ font-weight: 500; color: #1e293b; }}
  .import-result {{ font-weight: bold; }}
  .import-stats {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; color: #64748b; }}
  .import-error {{ margin-top: 8px; padding: 8px 12px; background: #fef2f2;
                   border-left: 3px solid #ef4444; color: #b91c1c; font-size: 13px; border-radius: 4px; }}
  .review-table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }}
  .review-table th, .review-table td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
  .review-table th {{ background: #f8fafc; font-weight: 600; color: #475569; font-size: 12px; }}
  .table-wrapper {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow: hidden; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
  th {{ background: #f8fafc; font-weight: 600; color: #475569; font-size: 13px; }}
  tr.defect-row:hover {{ background: #f1f5f9; cursor: pointer; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; color: #fff;
            font-size: 12px; font-weight: 500; }}
  .mono {{ font-family: Consolas, Monaco, monospace; font-size: 12px; }}
  .detail-row td {{ background: #f8fafc; padding: 0; }}
  .detail-content {{ padding: 20px; }}
  .detail-section {{ margin-bottom: 16px; }}
  .detail-section h4 {{ color: #334155; margin-bottom: 10px; font-size: 14px; }}
  .detail-section h5 {{ color: #475569; margin-bottom: 8px; font-size: 13px; }}
  .detail-section p {{ margin-bottom: 6px; font-size: 13px; color: #334155; }}
  .source-list, .history ul {{ padding-left: 20px; font-size: 12px; color: #64748b; }}
  .source-list li, .history li {{ margin-bottom: 4px; }}
  .footer {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="container">
  <h1>{title}</h1>
  <div class="meta">
    批次号：{state.batch_id or '-'} | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 缺陷总数：{total}
  </div>
  {last_import_html}
  {recent_reviews_html}
  <div class="stats">{stats_html}</div>
  <div class="table-wrapper">
    <table>
      <thead>
        <tr>
          <th>#</th><th>缺陷ID</th><th>楼栋</th><th>设备编号</th>
          <th>缺陷类型</th><th>严重等级</th><th>状态</th>
          <th>描述</th><th>首次发现</th><th>来源数</th>
        </tr>
      </thead>
      <tbody>
        {defects_html}
      </tbody>
    </table>
  </div>
  <div class="footer">点击行查看详情 | 物业设备巡检缺陷复核系统</div>
</div>
<script>
function toggleDetail(id) {{
  const el = document.getElementById('detail-' + id);
  if (el) {{
    el.style.display = el.style.display === 'none' ? 'table-row' : 'none';
  }}
}}
</script>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return len(defects)


def export_draft_csv(
    state: PatrolState,
    draft_id: str,
    output_path: str
) -> int:
    """
    导出草稿执行结果为 CSV。

    包含草稿元信息和所有缺陷的处理结果。
    """
    draft = state.get_draft(draft_id)
    if not draft:
        raise ValueError(f"草稿不存在: {draft_id}")

    if draft.status != "executed":
        raise ValueError(f"草稿尚未执行，无法导出: {draft_id}")

    fieldnames = [
        "草稿ID", "草稿名称", "目标状态", "处理人", "备注",
        "创建时间", "执行时间", "撤销时间",
        "模板ID", "模板名称", "模板溯源备注",
        "缺陷ID", "楼栋", "设备编号", "设备类别", "缺陷类型",
        "严重等级", "快照状态", "当前状态", "描述"
    ]

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        target_status_name = STATUS_NAMES.get(draft.target_status, draft.target_status)
        undo_time = draft.execution.undo_at[:19] if draft.execution.undo_at else ""

        tpl_id, tpl_name, tpl_note = _resolve_template_fields(state, draft)

        for item in draft.items:
            snapshot = item.defect_snapshot
            current = state.get_defect(item.defect_id)
            current_status = current.status if current else "已删除"
            current_status_name = STATUS_NAMES.get(current_status, current_status)
            snap_status_name = STATUS_NAMES.get(
                snapshot.get("status", ""), snapshot.get("status", "")
            )

            writer.writerow({
                "草稿ID": draft.draft_id,
                "草稿名称": draft.name,
                "目标状态": target_status_name,
                "处理人": draft.handler,
                "备注": draft.remark,
                "创建时间": draft.created_at[:19],
                "执行时间": draft.execution.executed_at[:19] if draft.execution.executed_at else "",
                "撤销时间": undo_time,
                "模板ID": tpl_id,
                "模板名称": tpl_name,
                "模板溯源备注": tpl_note,
                "缺陷ID": item.defect_id,
                "楼栋": snapshot.get("building", ""),
                "设备编号": snapshot.get("device_id", ""),
                "设备类别": snapshot.get("device_category", ""),
                "缺陷类型": snapshot.get("defect_type", ""),
                "严重等级": snapshot.get("severity", ""),
                "快照状态": snap_status_name,
                "当前状态": current_status_name,
                "描述": snapshot.get("description", "")
            })

    return len(draft.items)


def export_draft_list_csv(
    state: PatrolState,
    output_path: str,
    status: Optional[str] = None
) -> int:
    """导出草稿列表为 CSV"""
    drafts = state.list_drafts(status=status)

    if not drafts:
        return 0

    fieldnames = [
        "草稿ID", "草稿名称", "状态", "来源类型", "来源引用",
        "目标状态", "处理人", "备注", "创建人", "创建时间",
        "条目数", "执行时间", "撤销时间", "成功条数",
        "模板ID", "模板名称", "模板溯源备注"
    ]

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for draft in drafts:
            status_name = DRAFT_STATUS_NAMES.get(draft.status, draft.status)
            target_status_name = STATUS_NAMES.get(draft.target_status, draft.target_status)

            tpl_id, tpl_name, tpl_note = _resolve_template_fields(state, draft)

            writer.writerow({
                "草稿ID": draft.draft_id,
                "草稿名称": draft.name,
                "状态": status_name,
                "来源类型": draft.source_type,
                "来源引用": draft.source_ref,
                "目标状态": target_status_name,
                "处理人": draft.handler,
                "备注": draft.remark,
                "创建人": draft.created_by,
                "创建时间": draft.created_at[:19] if draft.created_at else "",
                "条目数": len(draft.items),
                "执行时间": draft.execution.executed_at[:19] if draft.execution.executed_at else "",
                "撤销时间": draft.execution.undo_at[:19] if draft.execution.undo_at else "",
                "成功条数": draft.execution.success_count if draft.execution.success_count else 0,
                "模板ID": tpl_id,
                "模板名称": tpl_name,
                "模板溯源备注": tpl_note
            })

    return len(drafts)

