"""CLI 入口"""

import click
import sys
from datetime import datetime

from .config import load_rules
from .storage import PatrolState
from .models import STATUS_NAMES
from .merger import import_and_merge, preview_import
from .workflow import (
    review_defect, undo_last, batch_review, WorkflowError,
    create_draft, preview_draft, execute_draft, void_draft,
    create_template, update_template, delete_template,
    import_templates, export_templates, create_draft_from_template,
    snapshot_health_check, snapshot_patch,
    publish_version, list_versions, get_version, compare_versions,
    preview_restore_version, restore_version,
    precheck_import_conflicts, import_with_versions, export_with_versions,
    list_archives, get_archive, compare_archives,
    preview_restore_archive, restore_archive,
    export_archives, import_archives, precheck_archive_import
)
from .models import DRAFT_STATUS_NAMES
from .exporter import export_csv, export_csv_with_sources, export_html, export_draft_csv, export_draft_list_csv, export_health_check_csv, export_health_check_json


SNAPSHOT_KEY_FIELDS = [
    ("name", "模板名称"),
    ("target_status", "目标状态"),
    ("handler", "处理人"),
]


def _classify_snapshot(tpl_snap):
    """
    对 template_snapshot 做三档分类。

    返回 (snapshot_status, missing_fields):
        snapshot_status: "complete" | "incomplete" | "missing"
        missing_fields:  缺失字段的中文显示名列表
    """
    if not tpl_snap:
        return "missing", [label for _, label in SNAPSHOT_KEY_FIELDS]

    missing = []
    for key, label in SNAPSHOT_KEY_FIELDS:
        if key not in tpl_snap:
            missing.append(label)

    if missing:
        return "incomplete", missing
    return "complete", []


def _resolve_draft_template_info(state, draft):
    """
    统一解析草稿的模板来源信息。

    返回 dict:
        has_template: bool           - 是否关联了模板
        template_id: str             - 模板ID（可能为空字符串）
        template_name: str           - 模板名称（仅取快照或ID，绝不反查当前模板）
        template_exists: bool        - 模板在当前存储中是否仍存在
        has_snapshot: bool           - 是否保存了模板快照（残缺也算有）
        snapshot_status: str         - "complete"|"incomplete"|"missing"
        missing_fields: list[str]    - 残缺/缺失字段的中文显示名
        snapshot_target_status: str  - 快照中的目标状态（中文名）
        snapshot_handler: str
        snapshot_remark: str
        note: str                    - 补充提示
    """
    info = {
        "has_template": False,
        "template_id": "",
        "template_name": "",
        "template_exists": False,
        "has_snapshot": False,
        "snapshot_status": "missing",
        "missing_fields": [],
        "snapshot_target_status": "",
        "snapshot_handler": "",
        "snapshot_remark": "",
        "note": "",
    }

    tpl_id = getattr(draft, "template_id", "") or ""
    info["template_id"] = tpl_id

    if not tpl_id:
        return info

    info["has_template"] = True

    snap = getattr(draft, "template_snapshot", None) or {}
    snapshot_status, missing_fields = _classify_snapshot(snap)
    info["snapshot_status"] = snapshot_status
    info["missing_fields"] = missing_fields
    info["has_snapshot"] = snapshot_status in ("complete", "incomplete")

    tpl = state.get_template(tpl_id) if hasattr(state, "get_template") else None
    info["template_exists"] = tpl is not None

    if info["has_snapshot"]:
        snap_name = snap.get("name", "")
        snap_status = snap.get("target_status", "")
        info["template_name"] = snap_name
        info["snapshot_target_status"] = STATUS_NAMES.get(snap_status, snap_status) if snap_status else "(缺失)"
        info["snapshot_handler"] = snap.get("handler", "") if "handler" in snap else "(缺失)"
        if not info["snapshot_handler"] or info["snapshot_handler"] == "":
            info["snapshot_handler"] = "-"
        info["snapshot_remark"] = snap.get("remark", "") if "remark" in snap else "(缺失)"
        if info["snapshot_remark"] == "":
            info["snapshot_remark"] = "-"

    if not info["template_name"]:
        info["template_name"] = tpl_id

    if snapshot_status == "complete":
        if not info["template_exists"]:
            info["note"] = "模板已删除，但执行时的完整快照已保留"
        if draft.snapshot_sealed_at:
            info["note"] = "完整快照已封存，不可变（不受后续变更影响）"
    elif snapshot_status == "incomplete":
        missing_str = ",".join(missing_fields)
        if not info["template_exists"]:
            info["note"] = f"残缺快照: 缺{missing_str}（模板已删除，快照部分缺失）"
        else:
            info["note"] = f"残缺快照: 缺{missing_str}（不受后续变更影响，但部分信息缺失）"
    else:
        if info["template_exists"]:
            info["note"] = "老数据无快照（模板后续变更可能影响溯源）"
        else:
            info["note"] = "老数据，模板已删除且无快照"

    return info


DEFAULT_CONFIG = "examples/rules.yaml"
DEFAULT_DATA_DIR = "data"


def _terminal_supports_unicode() -> bool:
    """检测终端是否支持 Unicode 字符（不产生实际输出）"""
    import os

    NON_UNICODE_ENCODINGS = {"gbk", "gb2312", "cp936", "ms936"}

    env_encoding = os.environ.get("PYTHONIOENCODING", "")
    if env_encoding:
        enc = env_encoding.split(":")[0].lower()
        if enc in NON_UNICODE_ENCODINGS:
            return False

    encoding = getattr(sys.stdout, "encoding", "") or ""
    encoding_lower = encoding.lower()
    if "utf" in encoding_lower:
        return True
    if encoding_lower in NON_UNICODE_ENCODINGS:
        return False
    try:
        test_chars = "\u26a0\u2713"
        encoding_obj = sys.stdout.encoding
        if encoding_obj:
            test_chars.encode(encoding_obj)
            return True
    except (UnicodeEncodeError, UnicodeError, LookupError, Exception):
        pass
    return False


_TERMINAL_UNICODE = None


def _sym(name: str) -> str:
    """获取终端安全的符号，自动降级"""
    global _TERMINAL_UNICODE
    if _TERMINAL_UNICODE is None:
        _TERMINAL_UNICODE = _terminal_supports_unicode()

    symbols = {
        "warn":  ("\u26a0", "[!]"),
        "check": ("\u2713", "[OK]"),
        "cross": ("\u2717", "[x]"),
        "arrow": ("\u2192", "->"),
        "file":  ("\U0001f4c4", "[FILE]"),
    }

    uni, ascii_fb = symbols.get(name, ("", ""))
    return uni if _TERMINAL_UNICODE else ascii_fb


def _ensure_encoding_safety():
    """
    全局编码安全兜底：如果终端编码不支持 Unicode，
    将 stdout/stderr 的错误处理设为 replace，避免 UnicodeEncodeError 崩溃。
    这是第二道防线，第一道是 _sym() 符号降级。
    """
    import io

    def _wrap(stream):
        if not hasattr(stream, "encoding") or not stream.encoding:
            return stream
        try:
            "\u26a0\u2713".encode(stream.encoding)
            return stream
        except (UnicodeEncodeError, UnicodeError, LookupError):
            pass

        try:
            buffer = stream.buffer
        except AttributeError:
            return stream

        try:
            new_stream = io.TextIOWrapper(
                buffer,
                encoding=stream.encoding,
                errors="replace",
                newline=stream.newlines if hasattr(stream, "newlines") else "",
                line_buffering=getattr(stream, "line_buffering", False),
                write_through=getattr(stream, "write_through", False),
            )
            return new_stream
        except Exception:
            return stream

    sys.stdout = _wrap(sys.stdout)
    sys.stderr = _wrap(sys.stderr)


def _load_config(ctx, param, value):
    """加载配置"""
    try:
        return load_rules(value)
    except Exception as e:
        raise click.BadParameter(str(e))


def _get_state(data_dir: str) -> PatrolState:
    return PatrolState(data_dir=data_dir)


@click.group()
@click.option("--config", "-c", default=DEFAULT_CONFIG, help="规则配置文件路径",
              show_default=True)
@click.option("--data-dir", "-d", default=DEFAULT_DATA_DIR, help="数据存储目录",
              show_default=True)
@click.pass_context
def cli(ctx, config, data_dir):
    """物业设备巡检缺陷复核 CLI 工具"""
    _ensure_encoding_safety()
    ctx.ensure_object(dict)
    try:
        ctx.obj["config"] = load_rules(config)
    except Exception as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    ctx.obj["data_dir"] = data_dir


@cli.command()
@click.argument("csv_file", type=click.Path(exists=True))
@click.option("--batch", "-b", default=None, help="批次号，默认自动生成")
@click.option("--dry-run", is_flag=True, help="仅预检，不落盘")
@click.pass_context
def import_cmd(ctx, csv_file, batch, dry_run):
    """导入巡检 CSV 文件并归并缺陷"""
    config = ctx.obj["config"]
    state = _get_state(ctx.obj["data_dir"])

    if not batch:
        if state.batch_id:
            batch = state.batch_id
        else:
            batch = f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    if dry_run:
        try:
            result = preview_import(csv_file, config, state, batch)
            click.echo(click.style("=== 预检结果 (dry-run) ===", fg="cyan", bold=True))
            click.echo(result.detailed_summary())
            if result.invalid_rows:
                click.echo(click.style(f"{_sym('warn')} 存在 {len(result.invalid_rows)} 条无效行，请修正后再导入", fg="yellow"))
            else:
                click.echo(click.style(f"{_sym('check')} 校验通过，可正式导入", fg="green"))
            click.echo(f"批次号: {batch}")
            click.echo(f"数据目录: {ctx.obj['data_dir']}")
        except FileNotFoundError as e:
            click.echo(click.style(f"错误: {e}", fg="red"), err=True)
            sys.exit(1)
        return

    try:
        result = import_and_merge(csv_file, config, state, batch)
        click.echo(click.style("导入完成", fg="green", bold=True))
        click.echo(result.summary())
        if result.new_defect_details or result.merged_defect_details:
            click.echo()
            if result.new_defect_details:
                click.echo(f"新增缺陷: {len(result.new_defect_details)} 条")
            if result.merged_defect_details:
                click.echo(f"合并缺陷: {len(result.merged_defect_details)} 条")
        click.echo(f"\n批次号: {state.batch_id}")
        click.echo(f"数据目录: {ctx.obj['data_dir']}")
    except ValueError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.command()
@click.argument("csv_file", type=click.Path(exists=True))
@click.option("--batch", "-b", default=None, help="批次号")
@click.pass_context
def preview(ctx, csv_file, batch):
    """预检 CSV 文件（dry-run），不落盘"""
    config = ctx.obj["config"]
    state = _get_state(ctx.obj["data_dir"])

    if not batch:
        if state.batch_id:
            batch = state.batch_id
        else:
            batch = f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    try:
        result = preview_import(csv_file, config, state, batch)
        click.echo(click.style("=== 预检结果 ===", fg="cyan", bold=True))
        click.echo(result.detailed_summary())

        if result.invalid_rows:
            click.echo(click.style(f"{_sym('warn')} 存在 {len(result.invalid_rows)} 条无效行，请修正后再导入", fg="yellow"))
        elif result.valid_rows == 0:
            click.echo(click.style(f"{_sym('warn')} 没有有效数据行", fg="yellow"))
        else:
            click.echo(click.style(f"{_sym('check')} 校验通过，可正式导入", fg="green"))
            click.echo(f"  将新增 {result.new_defects} 条缺陷")
            click.echo(f"  将合并 {result.merged_defects} 条来源到已有缺陷")

        click.echo(f"\n批次号: {batch}")
        click.echo(f"数据目录: {ctx.obj['data_dir']}")
        click.echo("模式: 预检 (不落盘)")
    except FileNotFoundError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.command("import-log")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.option("--type", "log_type", default=None,
              type=click.Choice(["preview", "import"]),
              help="按类型筛选")
@click.pass_context
def import_log_cmd(ctx, limit, log_type):
    """查看导入日志"""
    state = _get_state(ctx.obj["data_dir"])

    logs = state.get_import_logs(limit=limit)
    if log_type:
        logs = [l for l in logs if l.log_type == log_type]

    if not logs:
        click.echo("暂无导入日志")
        return

    click.echo(click.style(f"=== 导入日志 (最近 {len(logs)} 条) ===", bold=True))
    click.echo()

    for i, log in enumerate(logs, 1):
        type_label = "预检" if log.log_type == "preview" else "导入"
        result_color = "green" if log.result == "success" else "red" if log.result == "failed" else "yellow"
        result_label = {
            "success": "成功",
            "failed": "失败",
            "partial": "部分有效",
            "empty": "空文件"
        }.get(log.result, log.result)

        click.echo(f"[{i}] {click.style(type_label, fg='cyan')} "
                   f"{click.style(result_label, fg=result_color)} "
                   f"- {log.filename}")
        click.echo(f"    时间: {log.timestamp[:19]}  批次: {log.batch_id or '-'}")
        click.echo(f"    总行: {log.total_rows}  有效: {log.valid_rows}  "
                   f"无效: {log.invalid_rows}  新增: {log.new_defects}  "
                   f"合并: {log.merged_defects}")
        if log.error_summary:
            click.echo(f"    错误: {click.style(log.error_summary, fg='yellow')}")
        click.echo()

    total_all = len(state.import_logs)
    if total_all > limit:
        click.echo(f"... 还有 {total_all - limit} 条历史记录")


@cli.command("review-log")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.option("--defect-id", "-d", default=None, help="按缺陷编号筛选")
@click.option("--handler", "-H", default=None, help="按处理人筛选")
@click.option("--type", "log_type", default=None,
              type=click.Choice(["review", "batch_review", "undo"]),
              help="按操作类型筛选")
@click.pass_context
def review_log_cmd(ctx, limit, defect_id, handler, log_type):
    """查看复核操作历史"""
    state = _get_state(ctx.obj["data_dir"])

    logs = state.get_review_logs(
        defect_id=defect_id or "",
        handler=handler or "",
        log_type=log_type or "",
        limit=limit
    )

    if not logs:
        click.echo("暂无复核日志")
        return

    click.echo(click.style(f"=== 复核日志 (最近 {len(logs)} 条) ===", bold=True))
    click.echo()

    type_labels = {
        "review": "单条复核",
        "batch_review": "批量复核",
        "draft_review": "草稿复核",
        "undo": "撤销"
    }

    for i, log in enumerate(logs, 1):
        type_label = type_labels.get(log.log_type, log.log_type)
        type_color = {
            "review": "cyan",
            "batch_review": "blue",
            "draft_review": "magenta",
            "undo": "yellow"
        }.get(log.log_type, "white")

        if log.log_type == "undo":
            if log.defect_id:
                from_name = STATUS_NAMES.get(log.from_status, log.from_status)
                to_name = STATUS_NAMES.get(log.to_status, log.to_status)
                click.echo(f"[{i}] {click.style(type_label, fg=type_color)} "
                           f"{click.style(log.defect_id, fg='cyan')} "
                           f"{from_name} {_sym('arrow')} {to_name}")
                click.echo(f"    时间: {log.timestamp[:19]}  批次: {log.batch_id or '-'}")
                if log.handler:
                    click.echo(f"    处理人: {log.handler}")
                if log.remark:
                    click.echo(f"    备注: {log.remark}")
            else:
                click.echo(f"[{i}] {click.style(type_label, fg=type_color)} "
                           f"- {log.remark}")
                click.echo(f"    时间: {log.timestamp[:19]}  批次: {log.batch_id or '-'}")
        else:
            from_name = STATUS_NAMES.get(log.from_status, log.from_status)
            to_name = STATUS_NAMES.get(log.to_status, log.to_status)
            click.echo(f"[{i}] {click.style(type_label, fg=type_color)} "
                       f"{click.style(log.defect_id, fg='cyan')} "
                       f"{from_name} {_sym('arrow')} {to_name}")
            click.echo(f"    时间: {log.timestamp[:19]}  批次: {log.batch_id or '-'}")
            if log.handler:
                click.echo(f"    处理人: {log.handler}")
            if log.remark:
                click.echo(f"    备注: {log.remark}")
            if log.parent_log_id:
                click.echo(f"    批次组: {log.parent_log_id}")
            if log.draft_id:
                draft = state.get_draft(log.draft_id)
                draft_name = draft.name if draft else log.draft_id
                draft_tpl_info = ""
                if draft:
                    tpl_info = _resolve_draft_template_info(state, draft)
                    if tpl_info["has_template"]:
                        ss = tpl_info["snapshot_status"]
                        if ss == "complete":
                            ss_tag = f" {_sym('check')}完整"
                        elif ss == "incomplete":
                            missing_str = ",".join(tpl_info["missing_fields"])
                            ss_tag = f" {_sym('warn')}残缺(缺{missing_str})"
                        else:
                            ss_tag = f" {_sym('cross')}无快照"
                        draft_tpl_info = f" [模板: {tpl_info['template_name']}{ss_tag}]"
                    else:
                        draft_tpl_info = " [未使用模板]"
                click.echo(f"    草稿: {log.draft_id} ({draft_name}){draft_tpl_info}")
        click.echo()

    total_all = len(state.review_logs)
    if total_all > limit:
        click.echo(f"... 还有 {total_all - limit} 条历史记录")


@cli.command("list")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="按状态筛选")
@click.option("--building", default=None, help="按楼栋筛选")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.option("--verbose", "-v", is_flag=True, help="显示详细信息")
@click.pass_context
def list_cmd(ctx, status, building, limit, verbose):
    """列出缺陷记录"""
    state = _get_state(ctx.obj["data_dir"])
    defects = state.list_defects(status=status, building=building)

    if not defects:
        click.echo("暂无缺陷记录")
        return

    stats = state.stats()
    click.echo(f"共 {len(defects)} 条缺陷（总计 {stats['total']} 条）")
    click.echo()

    display = defects[:limit]

    if verbose:
        for d in display:
            status_name = STATUS_NAMES.get(d.status, d.status)
            click.echo(click.style(f"[{d.defect_id}]", fg="cyan", bold=True))
            click.echo(f"  楼栋: {d.building}  设备: {d.device_id}")
            click.echo(f"  类别: {d.device_category}  缺陷: {d.defect_type}")
            click.echo(f"  等级: {d.severity}  状态: {status_name}")
            click.echo(f"  描述: {d.description}")
            click.echo(f"  首次: {d.first_seen[:19]}  最后: {d.last_seen[:19]}")
            click.echo(f"  来源: {len(d.source_rows)} 条")
            if d.handler:
                click.echo(f"  处理人: {d.handler}")
            if d.review_remark:
                click.echo(f"  备注: {d.review_remark}")
            click.echo()
    else:
        header = f"{'ID':<18} {'楼栋':<8} {'设备':<12} {'类型':<14} {'等级':<8} {'状态':<8} {'来源数':<6} {'描述':<20}"
        click.echo(click.style(header, bold=True))
        click.echo("-" * len(header))
        for d in display:
            status_name = STATUS_NAMES.get(d.status, d.status)
            desc = d.description[:20]
            line = f"{d.defect_id:<18} {d.building:<8} {d.device_id:<12} {d.defect_type:<14} {d.severity:<8} {status_name:<8} {len(d.source_rows):<6} {desc:<20}"
            if d.status == "pending":
                click.echo(click.style(line, fg="yellow"))
            elif d.status == "closed":
                click.echo(click.style(line, fg="green"))
            elif d.status == "false_positive":
                click.echo(click.style(line, fg="bright_black"))
            else:
                click.echo(line)

    if len(defects) > limit:
        click.echo(f"\n... 还有 {len(defects) - limit} 条，使用 -n 调整显示数量")


@cli.command()
@click.argument("defect_id")
@click.argument("new_status", type=click.Choice(
    ["pending", "dispatched", "false_positive", "closed"]))
@click.option("--remark", "-r", default="", help="复核备注")
@click.option("--handler", "-H", default="", help="处理人")
@click.pass_context
def review(ctx, defect_id, new_status, remark, handler):
    """复核缺陷，变更状态"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        defect = review_defect(state, defect_id, new_status, remark, handler)
        status_name = STATUS_NAMES.get(new_status, new_status)
        click.echo(click.style(f"复核成功: {defect_id} {_sym('arrow')} {status_name}", fg="green"))
        if remark:
            click.echo(f"  备注: {remark}")
        if handler:
            click.echo(f"  处理人: {handler}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.command()
@click.argument("defect_ids", nargs=-1)
@click.option("--status", "-s", required=True,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="目标状态")
@click.option("--remark", "-r", default="", help="复核备注")
@click.option("--handler", "-H", default="", help="处理人")
@click.pass_context
def batch(ctx, defect_ids, status, remark, handler):
    """批量复核缺陷"""
    state = _get_state(ctx.obj["data_dir"])

    if not defect_ids:
        click.echo(click.style("错误: 请至少指定一个缺陷 ID", fg="red"), err=True)
        sys.exit(1)

    success_count, errors = batch_review(state, list(defect_ids), status, remark, handler)
    status_name = STATUS_NAMES.get(status, status)

    if success_count > 0:
        click.echo(click.style(f"成功复核 {success_count} 条 → {status_name}", fg="green"))
    if errors:
        click.echo(click.style(f"失败 {len(errors)} 条:", fg="red"), err=True)
        for err in errors:
            click.echo(f"  {err}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def undo(ctx):
    """撤销最后一步操作"""
    state = _get_state(ctx.obj["data_dir"])

    if not state.can_undo():
        click.echo(click.style("撤销栈为空，没有可撤销的操作", fg="yellow"))
        return

    action = undo_last(state)
    if action:
        click.echo(click.style(f"已撤销: {action}", fg="green"))
    else:
        click.echo(click.style("撤销失败", fg="red"), err=True)


@cli.command()
@click.argument("format", type=click.Choice(["csv", "csv-detail", "html", "draft-csv", "draft-list-csv"]))
@click.option("--output", "-o", default=None, help="输出文件路径")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="按状态筛选")
@click.option("--building", default=None, help="按楼栋筛选")
@click.option("--draft-id", default=None, help="草稿ID（用于 draft-csv 格式）")
@click.option("--draft-status", default=None,
              type=click.Choice(["pending", "executed", "voided"]),
              help="按草稿状态筛选（用于 draft-list-csv 格式）")
@click.pass_context
def export(ctx, format, output, status, building, draft_id, draft_status):
    """导出报告 (csv / csv-detail / html / draft-csv / draft-list-csv)"""
    state = _get_state(ctx.obj["data_dir"])
    config = ctx.obj["config"]

    if not output:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ext = "html" if format == "html" else "csv"
        output = f"report-{timestamp}.{ext}"

    try:
        if format == "csv":
            count = export_csv(state, output, status=status, building=building)
        elif format == "csv-detail":
            count = export_csv_with_sources(state, output, status=status)
        elif format == "html":
            count = export_html(state, output, config=config, status=status, building=building)
        elif format == "draft-csv":
            if not draft_id:
                click.echo(click.style("错误: 导出草稿执行结果必须指定 --draft-id", fg="red"), err=True)
                sys.exit(1)
            count = export_draft_csv(state, draft_id, output)
        elif format == "draft-list-csv":
            count = export_draft_list_csv(state, output, status=draft_status)
        else:
            click.echo(click.style(f"不支持的格式: {format}", fg="red"), err=True)
            sys.exit(1)

        click.echo(click.style(f"导出成功: {output}", fg="green"))
        click.echo(f"  记录数: {count}")
    except Exception as e:
        click.echo(click.style(f"导出失败: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.group()
@click.pass_context
def draft(ctx):
    """复核方案草稿管理"""
    pass


@draft.command("create")
@click.option("--ids", "defect_ids", default=None, help="逗号分隔的缺陷ID列表")
@click.option("--csv", "csv_path", type=click.Path(exists=True), default=None, help="CSV文件路径")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="目标状态（使用模板时可省略）")
@click.option("--name", "-n", default="", help="草稿名称")
@click.option("--handler", "-H", default=None, help="处理人")
@click.option("--remark", "-r", default=None, help="备注")
@click.option("--created-by", default="", help="创建人")
@click.option("--template", "-t", "template_id", default=None, help="使用的模板ID或名称")
@click.pass_context
def draft_create(ctx, defect_ids, csv_path, status, name, handler, remark, created_by, template_id):
    """创建复核方案草稿"""
    state = _get_state(ctx.obj["data_dir"])

    if not defect_ids and not csv_path:
        click.echo(click.style("错误: 必须指定 --ids 或 --csv", fg="red"), err=True)
        sys.exit(1)

    if defect_ids and csv_path:
        click.echo(click.style("错误: --ids 和 --csv 不能同时使用", fg="red"), err=True)
        sys.exit(1)

    if template_id and not status:
        pass
    elif not template_id and not status:
        click.echo(click.style("错误: 必须指定 --status 或 --template", fg="red"), err=True)
        sys.exit(1)

    try:
        if template_id:
            template = state.get_template(template_id)
            if not template:
                template = state.get_template_by_name(template_id)
            if not template:
                click.echo(click.style(f"错误: 模板不存在: {template_id}", fg="red"), err=True)
                sys.exit(1)
            template_id = template.template_id

            source = csv_path if csv_path else defect_ids
            source_type = "csv" if csv_path else "ids"

            handler_val = handler if handler is not None else ""
            remark_val = remark if remark is not None else ""

            draft = create_draft_from_template(
                state,
                template_id=template_id,
                source=source,
                source_type=source_type,
                name=name,
                status=status or "",
                handler=handler_val,
                remark=remark_val,
                created_by=created_by
            )
        else:
            handler_val = handler if handler is not None else ""
            remark_val = remark if remark is not None else ""

            if csv_path:
                draft = create_draft(
                    state, csv_path, "csv", status,
                    name=name, handler=handler_val, remark=remark_val, created_by=created_by
                )
            else:
                draft = create_draft(
                    state, defect_ids, "ids", status,
                    name=name, handler=handler_val, remark=remark_val, created_by=created_by
                )

        status_name = STATUS_NAMES.get(draft.target_status, draft.target_status)
        click.echo(click.style(f"草稿创建成功: {draft.draft_id}", fg="green"))
        click.echo(f"  名称: {draft.name}")
        click.echo(f"  目标状态: {status_name}")
        click.echo(f"  包含缺陷: {len(draft.items)} 条")
        if draft.handler:
            click.echo(f"  处理人: {draft.handler}")
        if draft.remark:
            click.echo(f"  备注: {draft.remark}")
        if draft.template_id:
            tpl_info = _resolve_draft_template_info(state, draft)
            click.echo(f"  模板来源: {tpl_info['template_name']} ({draft.template_id})")
            ss = tpl_info["snapshot_status"]
            if ss == "complete":
                click.echo(f"  模板快照: {_sym('check')} 完整快照已保存（修改模板不影响此草稿）")
            elif ss == "incomplete":
                missing_str = ",".join(tpl_info["missing_fields"])
                click.echo(click.style(
                    f"  模板快照: {_sym('warn')} 字段残缺（缺: {missing_str}）", fg="yellow"))
            else:
                click.echo(click.style(
                    f"  模板快照: {_sym('cross')} 未保存快照", fg="red"))
        click.echo(f"  创建时间: {draft.created_at[:19]}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@draft.command("preview")
@click.argument("draft_id")
@click.pass_context
def draft_preview(ctx, draft_id):
    """预览草稿影响的记录"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        preview = preview_draft(state, draft_id)

        status_name = STATUS_NAMES.get(preview["target_status"], preview["target_status"])
        click.echo(click.style(f"=== 草稿预览: {preview['name']} ===", bold=True, fg="cyan"))
        click.echo(f"草稿ID: {preview['draft_id']}")

        preview_tpl_id = preview.get("template_id", "")
        preview_tpl_snap = preview.get("template_snapshot", {})
        if preview_tpl_id:
            tpl = state.get_template(preview_tpl_id)
            preview_ss, preview_missing = _classify_snapshot(preview_tpl_snap)
            if preview_ss == "complete":
                tpl_name = preview_tpl_snap.get("name", preview_tpl_id)
                click.echo(f"模板: {tpl_name} ({preview_tpl_id})")
                snap_status = preview_tpl_snap.get("target_status", "")
                snap_status_name = STATUS_NAMES.get(snap_status, snap_status)
                click.echo(f"     快照完整度: {_sym('check')} 完整快照")
                click.echo(f"     快照目标状态: {snap_status_name}  "
                           f"处理人: {preview_tpl_snap.get('handler','-')}")
                if not tpl:
                    click.echo(click.style(f"     [当前模板已删除，完整快照已保留]", fg="yellow"))
            elif preview_ss == "incomplete":
                tpl_name = preview_tpl_snap.get("name", preview_tpl_id)
                missing_str = ",".join(preview_missing)
                click.echo(f"模板: {tpl_name} ({preview_tpl_id})")
                click.echo(click.style(f"     快照完整度: {_sym('warn')} 字段残缺（缺: {missing_str}）", fg="yellow"))
                if "target_status" in preview_tpl_snap:
                    snap_status = preview_tpl_snap.get("target_status", "")
                    click.echo(f"     快照目标状态: {STATUS_NAMES.get(snap_status, snap_status)}")
                else:
                    click.echo(click.style(f"     快照目标状态: (缺失)", fg="yellow"))
                if "handler" in preview_tpl_snap:
                    click.echo(f"     处理人: {preview_tpl_snap.get('handler','-')}")
                else:
                    click.echo(click.style(f"     处理人: (缺失)", fg="yellow"))
                if not tpl:
                    click.echo(click.style(f"     [当前模板已删除，残缺快照已保留]", fg="yellow"))
            else:
                click.echo(f"模板: {preview_tpl_id}")
                click.echo(click.style(f"     快照完整度: {_sym('cross')} 老数据无快照", fg="red"))
                if tpl:
                    click.echo(click.style(f"     [模板后续变更可能影响溯源]", fg="yellow"))
                else:
                    click.echo(click.style(f"     [模板已删除且无快照]", fg="red"))
        else:
            click.echo("模板: 未使用模板（手动创建）")

        click.echo(f"目标状态: {status_name}")
        click.echo(f"创建时间: {preview['created_at'][:19]}")
        click.echo(f"总条目数: {preview['total_items']}")
        if preview["handler"]:
            click.echo(f"处理人: {preview['handler']}")
        if preview["remark"]:
            click.echo(f"备注: {preview['remark']}")
        click.echo()

        if preview["will_change"]:
            click.echo(click.style(f"将变更 {len(preview['will_change'])} 条:", fg="green", bold=True))
            for item in preview["will_change"]:
                from_name = STATUS_NAMES.get(item["current_status"], item["current_status"])
                to_name = STATUS_NAMES.get(item["target_status"], item["target_status"])
                click.echo(f"  {item['defect_id']}: {item['building']} {item['device_id']} "
                           f"{from_name} {_sym('arrow')} {to_name} - {item['description'][:30]}")
            click.echo()

        if preview["same_status"]:
            click.echo(click.style(f"已是目标状态 {len(preview['same_status'])} 条:", fg="yellow"))
            for item in preview["same_status"]:
                status_name = STATUS_NAMES.get(item["current_status"], item["current_status"])
                click.echo(f"  {item['defect_id']}: {item['building']} {item['device_id']} "
                           f"- {status_name} - {item['description'][:30]}")
            click.echo()

        if preview["invalid_transition"]:
            click.echo(click.style(f"状态转换不合法 {len(preview['invalid_transition'])} 条:", fg="red"))
            for item in preview["invalid_transition"]:
                from_name = STATUS_NAMES.get(item["current_status"], item["current_status"])
                to_name = STATUS_NAMES.get(item["target_status"], item["target_status"])
                click.echo(f"  {item['defect_id']}: {from_name} {_sym('arrow')} {to_name} 不允许")
            click.echo()

        if preview["not_found"]:
            click.echo(click.style(f"缺陷不存在 {len(preview['not_found'])} 条:", fg="red"))
            for did in preview["not_found"]:
                click.echo(f"  {did}")
            click.echo()

    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@draft.command("execute")
@click.argument("draft_id")
@click.pass_context
def draft_execute(ctx, draft_id):
    """执行草稿（原子执行）"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        result = execute_draft(state, draft_id)
        draft = state.get_draft(draft_id)
        if draft:
            status_name = STATUS_NAMES.get(draft.target_status, draft.target_status)
            click.echo(click.style(
                f"草稿执行成功: {draft.name} ({draft_id})",
                fg="green", bold=True
            ))

            tpl_info = _resolve_draft_template_info(state, draft)
            if tpl_info["has_template"]:
                click.echo(f"  使用模板: {tpl_info['template_name']} ({tpl_info['template_id']})")
                ss = tpl_info["snapshot_status"]
                if ss == "complete":
                    click.echo(f"  模板快照: {_sym('check')} 完整快照（目标状态={tpl_info['snapshot_target_status']}, "
                               f"处理人={tpl_info['snapshot_handler']}）")
                elif ss == "incomplete":
                    missing_str = ",".join(tpl_info["missing_fields"])
                    click.echo(click.style(
                        f"  模板快照: {_sym('warn')} 字段残缺（缺: {missing_str}）", fg="yellow"))
                    snap = getattr(draft, "template_snapshot", None) or {}
                    if "target_status" in snap:
                        click.echo(f"  快照目标状态: {tpl_info['snapshot_target_status']}")
                    else:
                        click.echo(click.style(f"  快照目标状态: (缺失)", fg="yellow"))
                    if "handler" in snap:
                        click.echo(f"  快照处理人: {tpl_info['snapshot_handler']}")
                    else:
                        click.echo(click.style(f"  快照处理人: (缺失)", fg="yellow"))
                else:
                    click.echo(click.style(f"  模板快照: {_sym('cross')} 老数据无快照", fg="red"))
                if tpl_info["note"]:
                    click.echo(click.style(f"  提示: {tpl_info['note']}", fg="yellow"))
            else:
                click.echo(f"  模板来源: 未使用模板（手动创建）")

            click.echo(f"  执行批次: {result.execution_id}")
            click.echo(f"  执行时间: {result.executed_at[:19]}")
            click.echo(f"  成功处理: {result.success_count} 条 → {status_name}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@draft.command("list")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "executed", "voided"]),
              help="按状态筛选")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.pass_context
def draft_list(ctx, status, limit):
    """列出草稿"""
    state = _get_state(ctx.obj["data_dir"])

    drafts = state.list_drafts(status=status, limit=limit)

    if not drafts:
        click.echo("暂无草稿")
        return

    click.echo(click.style(f"=== 草稿列表 (最近 {len(drafts)} 条) ===", bold=True))
    click.echo()

    for i, draft in enumerate(drafts, 1):
        status_name = DRAFT_STATUS_NAMES.get(draft.status, draft.status)
        status_color = {
            "pending": "yellow",
            "executed": "green",
            "voided": "bright_black"
        }.get(draft.status, "white")

        target_status_name = STATUS_NAMES.get(draft.target_status, draft.target_status)

        click.echo(f"[{i}] {click.style(status_name, fg=status_color)} "
                   f"{click.style(draft.draft_id, fg='cyan')} "
                   f"- {draft.name}")
        click.echo(f"    创建时间: {draft.created_at[:19]}  "
                   f"目标: {target_status_name}  "
                   f"条目: {len(draft.items)}条")
        if draft.handler:
            click.echo(f"    处理人: {draft.handler}")

        tpl_info = _resolve_draft_template_info(state, draft)
        if tpl_info["has_template"]:
            ss = tpl_info["snapshot_status"]
            if ss == "complete":
                status_tag = f" [{_sym('check')}完整快照]"
            elif ss == "incomplete":
                missing_str = ",".join(tpl_info["missing_fields"])
                status_tag = f" [{_sym('warn')}字段残缺:缺{missing_str}]"
            else:
                status_tag = f" [{_sym('cross')}老数据无快照]"
            tpl_line = f"    模板: {tpl_info['template_name']} ({tpl_info['template_id']}){status_tag}"
            if not tpl_info["template_exists"]:
                tpl_line += " [模板已删除]"
            click.echo(tpl_line)
        else:
            click.echo("    模板: 未使用模板（手动创建）")

        if draft.status == "executed" and draft.execution.executed_at:
            click.echo(f"    执行时间: {draft.execution.executed_at[:19]}  "
                       f"成功: {draft.execution.success_count}条")
            if draft.execution.undo_at:
                click.echo(f"    撤销时间: {draft.execution.undo_at[:19]}")
        if draft.status == "voided":
            click.echo(f"    {draft.remark}")
        click.echo()

    total_all = len(state.drafts)
    if total_all > limit:
        click.echo(f"... 还有 {total_all - limit} 条历史记录")


@draft.command("show")
@click.argument("draft_id")
@click.pass_context
def draft_show(ctx, draft_id):
    """显示草稿详情"""
    state = _get_state(ctx.obj["data_dir"])

    draft = state.get_draft(draft_id)
    if not draft:
        click.echo(click.style(f"草稿不存在: {draft_id}", fg="red"), err=True)
        sys.exit(1)

    status_name = DRAFT_STATUS_NAMES.get(draft.status, draft.status)
    status_color = {
        "pending": "yellow",
        "executed": "green",
        "voided": "bright_black"
    }.get(draft.status, "white")

    target_status_name = STATUS_NAMES.get(draft.target_status, draft.target_status)

    click.echo(click.style(f"=== {draft.name} ===", bold=True, fg="cyan"))
    click.echo(f"草稿ID: {draft.draft_id}")
    click.echo(f"状态: {click.style(status_name, fg=status_color)}")
    click.echo(f"来源: {draft.source_type} - {draft.source_ref}")

    tpl_info = _resolve_draft_template_info(state, draft)
    if tpl_info["has_template"]:
        click.echo(f"模板: {tpl_info['template_name']} ({tpl_info['template_id']})")
        ss = tpl_info["snapshot_status"]
        if ss == "complete":
            click.echo(f"     快照完整度: {_sym('check')} 完整快照")
            click.echo(f"     快照保存时间点的配置已保留，不受模板后续修改影响")
            click.echo(f"     快照目标状态: {tpl_info['snapshot_target_status']}")
            click.echo(f"     快照处理人: {tpl_info['snapshot_handler']}")
            if tpl_info["snapshot_remark"] and tpl_info["snapshot_remark"] != "-":
                remark_display = tpl_info["snapshot_remark"]
                if len(remark_display) > 30:
                    remark_display = remark_display[:30] + "..."
                click.echo(f"     快照备注: {remark_display}")
        elif ss == "incomplete":
            missing_str = ",".join(tpl_info["missing_fields"])
            click.echo(click.style(f"     快照完整度: {_sym('warn')} 字段残缺（缺: {missing_str}）", fg="yellow"))
            snap = getattr(draft, "template_snapshot", None) or {}
            if "target_status" in snap:
                click.echo(f"     快照目标状态: {tpl_info['snapshot_target_status']}")
            else:
                click.echo(click.style(f"     快照目标状态: (缺失)", fg="yellow"))
            if "handler" in snap:
                click.echo(f"     快照处理人: {tpl_info['snapshot_handler']}")
            else:
                click.echo(click.style(f"     快照处理人: (缺失)", fg="yellow"))
        else:
            click.echo(click.style(f"     快照完整度: {_sym('cross')} 老数据无快照", fg="red"))
        if not tpl_info["template_exists"]:
            click.echo(click.style(f"     [当前模板已删除]", fg="yellow"))
        if tpl_info["note"]:
            click.echo(click.style(f"     提示: {tpl_info['note']}", fg="yellow"))
    else:
        click.echo("模板: 未使用模板（手动创建）")

    click.echo(f"目标状态: {target_status_name}")
    click.echo(f"创建时间: {draft.created_at[:19]}")
    if draft.created_by:
        click.echo(f"创建人: {draft.created_by}")
    if draft.handler:
        click.echo(f"处理人: {draft.handler}")
    if draft.remark:
        click.echo(f"备注: {draft.remark}")
    click.echo()

    if draft.status == "executed":
        click.echo(click.style("=== 执行信息 ===", bold=True))
        click.echo(f"执行ID: {draft.execution.execution_id}")
        click.echo(f"执行时间: {draft.execution.executed_at[:19]}")
        click.echo(f"成功条数: {draft.execution.success_count}")
        if draft.execution.undo_at:
            click.echo(click.style("已撤销", fg="yellow"))
            click.echo(f"撤销时间: {draft.execution.undo_at[:19]}")
            click.echo(f"撤销执行ID: {draft.execution.undo_execution_id}")
        click.echo()

    click.echo(click.style(f"=== 包含缺陷 ({len(draft.items)} 条) ===", bold=True))
    for i, item in enumerate(draft.items, 1):
        snapshot = item.defect_snapshot
        snap_status = STATUS_NAMES.get(snapshot.get("status", ""), snapshot.get("status", ""))
        click.echo(f"{i}. {item.defect_id}: "
                   f"{snapshot.get('building', '')} "
                   f"{snapshot.get('device_id', '')} "
                   f"[{snap_status}] "
                   f"{snapshot.get('description', '')[:40]}")
        current = state.get_defect(item.defect_id)
        if current:
            current_status = STATUS_NAMES.get(current.status, current.status)
            if current.status != snapshot.get("status", ""):
                click.echo(f"   ⚠ 当前状态: {current_status}（与快照不一致）")


@draft.command("void")
@click.argument("draft_id")
@click.option("--reason", "-r", default="", help="作废原因")
@click.pass_context
def draft_void(ctx, draft_id, reason):
    """作废草稿"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        draft = void_draft(state, draft_id, reason)
        click.echo(click.style(f"草稿已作废: {draft.name} ({draft.draft_id})", fg="green"))
        if reason:
            click.echo(f"原因: {reason}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def stats(ctx):
    """显示统计信息"""
    state = _get_state(ctx.obj["data_dir"])
    s = state.stats()

    click.echo(click.style("=== 统计信息 ===", bold=True))
    click.echo(f"批次号: {state.batch_id or '-'}")
    click.echo(f"缺陷总数: {s['total']}")
    click.echo()

    click.echo("按状态:")
    for st, name in STATUS_NAMES.items():
        count = s["by_status"].get(st, 0)
        click.echo(f"  {name}: {count}")

    click.echo()
    if s["by_building"]:
        click.echo("按楼栋:")
        for bld, count in sorted(s["by_building"].items()):
            click.echo(f"  {bld}: {count}")
        click.echo()

    click.echo(f"已导入文件: {s['imported_files']} 个")
    click.echo(f"撤销栈深度: {s['undo_stack_size']}")
    click.echo(f"导入日志: {len(state.import_logs)} 条")
    if state.drafts:
        draft_stats = {"pending": 0, "executed": 0, "voided": 0}
        for draft in state.drafts.values():
            draft_stats[draft.status] = draft_stats.get(draft.status, 0) + 1
        click.echo()
        click.echo("草稿:")
        for st, name in DRAFT_STATUS_NAMES.items():
            count = draft_stats.get(st, 0)
            click.echo(f"  {name}: {count}")
    click.echo(f"数据目录: {ctx.obj['data_dir']}")


@cli.command()
@click.argument("defect_id")
@click.pass_context
def show(ctx, defect_id):
    """显示缺陷详情"""
    state = _get_state(ctx.obj["data_dir"])
    defect = state.get_defect(defect_id)

    if not defect:
        click.echo(click.style(f"缺陷不存在: {defect_id}", fg="red"), err=True)
        sys.exit(1)

    status_name = STATUS_NAMES.get(defect.status, defect.status)

    click.echo(click.style(f"=== {defect.defect_id} ===", bold=True, fg="cyan"))
    click.echo(f"状态: {status_name}")
    click.echo(f"楼栋: {defect.building}")
    click.echo(f"设备编号: {defect.device_id}")
    click.echo(f"设备类别: {defect.device_category}")
    click.echo(f"缺陷类型: {defect.defect_type}")
    click.echo(f"严重等级: {defect.severity}")
    click.echo(f"描述: {defect.description}")
    click.echo(f"首次发现: {defect.first_seen}")
    click.echo(f"最后发现: {defect.last_seen}")
    click.echo(f"处理人: {defect.handler or '-'}")
    click.echo(f"复核备注: {defect.review_remark or '-'}")
    click.echo()

    click.echo(f"来源记录 ({len(defect.source_rows)} 条):")
    for i, sr in enumerate(defect.source_rows, 1):
        click.echo(f"  {i}. {sr.source_file} 第{sr.line_number}行 "
                   f"(导入: {sr.import_time[:19]})")

    if defect.status_history:
        click.echo()
        click.echo("状态历史:")
        for h in defect.status_history:
            from_name = STATUS_NAMES.get(h["from"], h["from"]) if h["from"] else "无"
            to_name = STATUS_NAMES.get(h["to"], h["to"])
            handler = h.get("handler", "-")
            remark = h.get("remark", "")
            click.echo(f"  {h['time'][:19]}: {from_name} {_sym('arrow')} {to_name} "
                       f"[{handler}] {remark}")

    review_logs_defect = state.get_review_logs(defect_id=defect_id)
    if review_logs_defect:
        click.echo()
        click.echo(f"复核历史 ({len(review_logs_defect)} 条):")
        type_labels = {
            "review": "单条复核",
            "batch_review": "批量复核",
            "draft_review": "草稿复核",
            "undo": "撤销"
        }
        type_colors = {
            "review": "cyan",
            "batch_review": "blue",
            "draft_review": "magenta",
            "undo": "yellow"
        }
        for i, log in enumerate(review_logs_defect, 1):
            type_label = type_labels.get(log.log_type, log.log_type)
            type_color = type_colors.get(log.log_type, "white")
            from_name = STATUS_NAMES.get(log.from_status, log.from_status) if log.from_status else "无"
            to_name = STATUS_NAMES.get(log.to_status, log.to_status) if log.to_status else "无"
            click.echo(f"  [{i}] {click.style(type_label, fg=type_color)} "
                       f"{from_name} {_sym('arrow')} {to_name}")
            click.echo(f"      处理人: {log.handler or '-'}  时间: {log.timestamp[:19]}")
            if log.remark:
                click.echo(f"      备注: {log.remark}")
            if log.batch_id:
                click.echo(f"      批次: {log.batch_id}")
            if log.parent_log_id:
                click.echo(f"      批次组: {log.parent_log_id}")
            if log.draft_id:
                draft = state.get_draft(log.draft_id)
                draft_name = draft.name if draft else log.draft_id
                draft_tpl_info = ""
                if draft:
                    tpl_info = _resolve_draft_template_info(state, draft)
                    if tpl_info["has_template"]:
                        ss = tpl_info["snapshot_status"]
                        if ss == "complete":
                            ss_tag = f" {_sym('check')}完整"
                        elif ss == "incomplete":
                            missing_str = ",".join(tpl_info["missing_fields"])
                            ss_tag = f" {_sym('warn')}残缺(缺{missing_str})"
                        else:
                            ss_tag = f" {_sym('cross')}无快照"
                        draft_tpl_info = f" [模板: {tpl_info['template_name']}{ss_tag}]"
                    else:
                        draft_tpl_info = " [未使用模板]"
                click.echo(f"      草稿来源: {log.draft_id} ({draft_name}){draft_tpl_info}")

    drafts_for_defect = state.get_drafts_for_defect(defect_id)
    if drafts_for_defect:
        click.echo()
        click.echo(f"关联草稿 ({len(drafts_for_defect)} 个):")
        for i, draft in enumerate(drafts_for_defect, 1):
            status_name = DRAFT_STATUS_NAMES.get(draft.status, draft.status)
            status_color = {
                "pending": "yellow",
                "executed": "green",
                "voided": "bright_black"
            }.get(draft.status, "white")
            target_status = STATUS_NAMES.get(draft.target_status, draft.target_status)
            click.echo(f"  [{i}] {click.style(draft.draft_id, fg='cyan')} "
                       f"{click.style(status_name, fg=status_color)} "
                       f"- {draft.name}")
            tpl_info = _resolve_draft_template_info(state, draft)
            if tpl_info["has_template"]:
                ss = tpl_info["snapshot_status"]
                if ss == "complete":
                    ss_tag = " [完整快照]"
                elif ss == "incomplete":
                    missing_str = ",".join(tpl_info["missing_fields"])
                    ss_tag = f" [残缺:缺{missing_str}]"
                else:
                    ss_tag = " [无快照]"
                click.echo(f"      模板: {tpl_info['template_name']} ({tpl_info['template_id']}){ss_tag}")
            else:
                click.echo(f"      模板: 未使用模板（手动创建）")
            click.echo(f"      创建: {draft.created_at[:19]}  目标: {target_status}")
            if draft.status == "executed":
                if draft.execution.undo_at:
                    click.echo(f"      执行: {draft.execution.executed_at[:19]} (已撤销)")
                else:
                    click.echo(f"      执行: {draft.execution.executed_at[:19]}")


@cli.group()
@click.pass_context
def template(ctx):
    """复核方案模板管理"""
    pass


@template.command("create")
@click.option("--name", "-n", required=True, help="模板名称")
@click.option("--status", "-s", required=True,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="目标状态")
@click.option("--handler", "-H", default="", help="默认处理人")
@click.option("--remark", "-r", default="", help="备注模板")
@click.option("--source-type", default="", help="来源方式")
@click.option("--description", "-d", default="", help="模板描述")
@click.pass_context
def template_create(ctx, name, status, handler, remark, source_type, description):
    """创建模板"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        template = create_template(
            state,
            name=name,
            target_status=status,
            handler=handler,
            remark=remark,
            source_type=source_type,
            description=description
        )
        status_name = STATUS_NAMES.get(status, status)
        click.echo(click.style(f"模板创建成功: {template.template_id}", fg="green"))
        click.echo(f"  名称: {template.name}")
        click.echo(f"  目标状态: {status_name}")
        if handler:
            click.echo(f"  默认处理人: {handler}")
        if remark:
            click.echo(f"  备注模板: {remark}")
        if source_type:
            click.echo(f"  来源方式: {source_type}")
        if description:
            click.echo(f"  描述: {description}")
        click.echo(f"  创建时间: {template.created_at[:19]}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@template.command("update")
@click.argument("template_id_or_name")
@click.option("--name", "-n", default=None, help="模板名称")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="目标状态")
@click.option("--handler", "-H", default=None, help="默认处理人")
@click.option("--remark", "-r", default=None, help="备注模板")
@click.option("--source-type", default=None, help="来源方式")
@click.option("--description", "-d", default=None, help="模板描述")
@click.pass_context
def template_update(ctx, template_id_or_name, name, status, handler, remark, source_type, description):
    """更新模板"""
    state = _get_state(ctx.obj["data_dir"])

    template = state.get_template(template_id_or_name)
    if not template:
        template = state.get_template_by_name(template_id_or_name)
    if not template:
        click.echo(click.style(f"错误: 模板不存在: {template_id_or_name}", fg="red"), err=True)
        sys.exit(1)

    try:
        template = update_template(
            state,
            template_id=template.template_id,
            name=name,
            target_status=status,
            handler=handler,
            remark=remark,
            source_type=source_type,
            description=description
        )
        status_name = STATUS_NAMES.get(template.target_status, template.target_status)
        click.echo(click.style(f"模板更新成功: {template.template_id}", fg="green"))
        click.echo(f"  名称: {template.name}")
        click.echo(f"  目标状态: {status_name}")
        click.echo(f"  默认处理人: {template.handler or '-'}")
        click.echo(f"  备注模板: {template.remark or '-'}")
        click.echo(f"  来源方式: {template.source_type or '-'}")
        click.echo(f"  描述: {template.description or '-'}")
        click.echo(f"  更新时间: {template.updated_at[:19]}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@template.command("delete")
@click.argument("template_id_or_name")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@click.pass_context
def template_delete(ctx, template_id_or_name, yes):
    """删除模板"""
    state = _get_state(ctx.obj["data_dir"])

    template = state.get_template(template_id_or_name)
    if not template:
        template = state.get_template_by_name(template_id_or_name)
    if not template:
        click.echo(click.style(f"错误: 模板不存在: {template_id_or_name}", fg="red"), err=True)
        sys.exit(1)

    if not yes:
        click.echo(f"模板名称: {template.name}")
        click.echo(f"模板ID: {template.template_id}")
        status_name = STATUS_NAMES.get(template.target_status, template.target_status)
        click.echo(f"目标状态: {status_name}")
        if not click.confirm("确定要删除此模板吗？"):
            click.echo("已取消")
            return

    try:
        delete_template(state, template.template_id)
        click.echo(click.style(f"模板已删除: {template.name}", fg="green"))
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@template.command("list")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.pass_context
def template_list(ctx, limit):
    """列出模板"""
    state = _get_state(ctx.obj["data_dir"])

    templates = state.list_templates()

    if not templates:
        click.echo("暂无模板")
        return

    display = templates[:limit]

    click.echo(click.style(f"=== 模板列表 (共 {len(templates)} 条) ===", bold=True))
    click.echo()

    for i, tpl in enumerate(display, 1):
        status_name = STATUS_NAMES.get(tpl.target_status, tpl.target_status)
        click.echo(f"[{i}] {click.style(tpl.template_id, fg='cyan')} - {tpl.name}")
        click.echo(f"    目标状态: {status_name}  "
                   f"处理人: {tpl.handler or '-'}  "
                   f"创建: {tpl.created_at[:19]}")
        if tpl.description:
            click.echo(f"    描述: {tpl.description[:40]}")
        click.echo()

    if len(templates) > limit:
        click.echo(f"... 还有 {len(templates) - limit} 条，使用 -n 调整显示数量")


@template.command("show")
@click.argument("template_id_or_name")
@click.pass_context
def template_show(ctx, template_id_or_name):
    """显示模板详情"""
    state = _get_state(ctx.obj["data_dir"])

    template = state.get_template(template_id_or_name)
    if not template:
        template = state.get_template_by_name(template_id_or_name)
    if not template:
        click.echo(click.style(f"错误: 模板不存在: {template_id_or_name}", fg="red"), err=True)
        sys.exit(1)

    status_name = STATUS_NAMES.get(template.target_status, template.target_status)

    click.echo(click.style(f"=== {template.name} ===", bold=True, fg="cyan"))
    click.echo(f"模板ID: {template.template_id}")
    click.echo(f"目标状态: {status_name}")
    click.echo(f"默认处理人: {template.handler or '-'}")
    click.echo(f"备注模板: {template.remark or '-'}")
    click.echo(f"来源方式: {template.source_type or '-'}")
    click.echo(f"描述: {template.description or '-'}")
    click.echo(f"创建时间: {template.created_at[:19]}")
    click.echo(f"更新时间: {template.updated_at[:19]}")

    related_drafts = [
        d for d in state.drafts.values()
        if d.template_id == template.template_id
    ]
    if related_drafts:
        click.echo()
        click.echo(f"关联草稿: {len(related_drafts)} 个")
        for d in related_drafts[:5]:
            draft_status = DRAFT_STATUS_NAMES.get(d.status, d.status)
            click.echo(f"  - {d.draft_id} ({d.name}) [{draft_status}]")
        if len(related_drafts) > 5:
            click.echo(f"  ... 还有 {len(related_drafts) - 5} 个")


@template.command("import")
@click.argument("json_file", type=click.Path(exists=True))
@click.option("--overwrite", "-o", is_flag=True, help="覆盖同名模板")
@click.pass_context
def template_import(ctx, json_file, overwrite):
    """从 JSON 文件导入模板"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        result = import_templates(state, json_file, overwrite=overwrite)

        click.echo(click.style("=== 模板导入结果 ===", bold=True))
        click.echo(f"文件: {json_file}")
        click.echo()

        if result["imported"]:
            click.echo(click.style(f"成功导入 {result['imported_count']} 个:", fg="green"))
            for name in result["imported"]:
                click.echo(f"  {_sym('check')} {name}")
            click.echo()

        if result["skipped"]:
            click.echo(click.style(f"跳过 {result['skipped_count']} 个:", fg="yellow"))
            for name in result["skipped"]:
                click.echo(f"  {_sym('warn')} {name}")
            click.echo()

        if result["errors"]:
            click.echo(click.style(f"错误 {result['error_count']} 个:", fg="red"))
            for err in result["errors"]:
                click.echo(f"  {_sym('cross')} {err}")
            click.echo()

        if result["error_count"] > 0 and result["imported_count"] == 0:
            sys.exit(1)

    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@template.command("export")
@click.argument("output_file")
@click.option("--ids", default=None, help="逗号分隔的模板ID列表，不指定则导出全部")
@click.pass_context
def template_export(ctx, output_file, ids):
    """导出模板到 JSON 文件"""
    state = _get_state(ctx.obj["data_dir"])

    template_ids = None
    if ids:
        template_ids = [x.strip() for x in ids.split(",") if x.strip()]

    try:
        count = export_templates(state, output_file, template_ids=template_ids)
        click.echo(click.style(f"导出成功: {output_file}", fg="green"))
        click.echo(f"  模板数量: {count}")
    except Exception as e:
        click.echo(click.style(f"导出失败: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.group()
@click.pass_context
def snapshot(ctx):
    """快照体检与补档"""
    pass


@snapshot.command("check")
@click.option("--output", "-o", default=None, help="导出文件路径（不指定则仅终端输出）")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv", help="导出格式")
@click.pass_context
def snapshot_check(ctx, output, fmt):
    """快照体检：扫描所有草稿快照完整性，不修改任何状态"""
    state = _get_state(ctx.obj["data_dir"])

    results = snapshot_health_check(state)

    if not results:
        click.echo("暂无草稿记录")
        return

    snap_map = {"complete": "完整快照", "incomplete": "残缺快照", "missing": "老数据无快照"}
    status_map = {"pending": "待执行", "executed": "已执行", "voided": "已作废"}

    complete_count = sum(1 for r in results if r["snapshot_status"] == "complete")
    incomplete_count = sum(1 for r in results if r["snapshot_status"] == "incomplete")
    missing_count = sum(1 for r in results if r["snapshot_status"] == "missing")
    sealed_count = sum(1 for r in results if r["sealed"])
    patchable_count = sum(1 for r in results if r["can_patch"])

    click.echo(click.style("=== 快照体检报告 ===", bold=True))
    click.echo(f"草稿总数: {len(results)}")
    click.echo(f"  完整快照: {complete_count} (已封存: {sealed_count})")
    click.echo(f"  残缺快照: {incomplete_count}")
    click.echo(f"  老数据无快照: {missing_count}")
    click.echo(f"  可补档: {patchable_count}")
    click.echo()

    for r in results:
        ss = snap_map.get(r["snapshot_status"], r["snapshot_status"])
        ds = status_map.get(r["draft_status"], r["draft_status"])
        sealed_tag = " [已封存]" if r["sealed"] else ""
        patch_tag = ""
        if r["can_patch"]:
            patch_tag = f" {_sym('check')}可补档"
        elif r["cannot_patch_reason"]:
            patch_tag = f" {_sym('cross')}{r['cannot_patch_reason']}"

        click.echo(f"  {click.style(r['draft_id'], fg='cyan')} {r['draft_name']} [{ds}]")
        click.echo(f"    快照: {ss}{sealed_tag}{patch_tag}")
        if r["missing_fields"]:
            click.echo(click.style(f"    缺失字段: {','.join(r['missing_fields'])}", fg="yellow"))
        if r["risk_reason"]:
            click.echo(click.style(f"    风险: {r['risk_reason']}", fg="yellow"))
        click.echo()

    if output:
        if fmt == "csv":
            count = export_health_check_csv(results, output)
        else:
            count = export_health_check_json(results, output)
        click.echo(click.style(f"体检报告已导出: {output} ({count}条)", fg="green"))


@snapshot.command("patch")
@click.option("--ids", "draft_ids", default=None, help="逗号分隔的草稿ID列表，不指定则补档所有可补档草稿")
@click.option("--dry-run", is_flag=True, help="仅预检，不落盘")
@click.pass_context
def snapshot_patch_cmd(ctx, draft_ids, dry_run):
    """快照补档：封存模板快照为只读副本"""
    state = _get_state(ctx.obj["data_dir"])

    if draft_ids:
        target_ids = [x.strip() for x in draft_ids.split(",") if x.strip()]
    else:
        health = snapshot_health_check(state)
        target_ids = [r["draft_id"] for r in health if r["can_patch"]]

    if not target_ids:
        click.echo(click.style("没有可补档的草稿", fg="yellow"))
        return

    health = snapshot_health_check(state)
    id_to_health = {r["draft_id"]: r for r in health}

    click.echo(click.style("=== 补档预检 ===", bold=True))
    for did in target_ids:
        h = id_to_health.get(did)
        if h:
            snap_map = {"complete": "完整快照", "incomplete": "残缺快照", "missing": "老数据无快照"}
            ss = snap_map.get(h["snapshot_status"], h["snapshot_status"])
            click.echo(f"  {did}: {h['draft_name']} - {ss}")
            if h["can_patch"]:
                click.echo(f"    {_sym('check')} 可补档")
            else:
                click.echo(click.style(f"    {_sym('cross')} {h['cannot_patch_reason']}", fg="red"))
            if h["risk_reason"]:
                click.echo(click.style(f"    风险: {h['risk_reason']}", fg="yellow"))
    click.echo()

    if dry_run:
        click.echo(click.style("预检模式 (dry-run)，不执行补档", fg="cyan"))
        return

    result = snapshot_patch(state, target_ids)

    if result["errors"]:
        click.echo(click.style("补档失败（整批）:", fg="red", bold=True))
        for err in result["errors"]:
            click.echo(click.style(f"  {_sym('cross')} {err}", fg="red"))
        click.echo(f"  审计ID: {result['audit_id']}")
    else:
        click.echo(click.style(f"补档成功: {len(result['patched'])} 条", fg="green", bold=True))
        for did in result["patched"]:
            draft = state.get_draft(did)
            name = draft.name if draft else did
            click.echo(f"  {_sym('check')} {did}: {name}")
        click.echo(f"  审计ID: {result['audit_id']}")


@cli.group()
@click.pass_context
def version(ctx):
    pass


@version.command("publish")
@click.argument("template_id_or_name")
@click.option("--name", "-n", required=True, help="版本名称（如 v1.0、2024Q1）")
@click.option("--published-by", "-b", default="", help="发布人")
@click.pass_context
def version_publish(ctx, template_id_or_name, name, published_by):
    state = _get_state(ctx.obj["data_dir"])

    template = state.get_template(template_id_or_name)
    if not template:
        template = state.get_template_by_name(template_id_or_name)
    if not template:
        click.echo(click.style(f"错误: 模板不存在: {template_id_or_name}", fg="red"), err=True)
        sys.exit(1)

    try:
        ver = publish_version(state, template.template_id, name, published_by=published_by)
        status_name = STATUS_NAMES.get(ver.target_status, ver.target_status)
        click.echo(click.style(f"版本发布成功: {ver.version_id}", fg="green"))
        click.echo(f"  模板: {ver.template_name} ({ver.template_id})")
        click.echo(f"  版本名: {ver.version_name}")
        click.echo(f"  目标状态: {status_name}")
        click.echo(f"  处理人: {ver.handler or '-'}")
        click.echo(f"  备注: {ver.remark or '-'}")
        click.echo(f"  来源: {ver.source_type or '-'}")
        click.echo(f"  发布时间: {ver.published_at[:19]}")
        if published_by:
            click.echo(f"  发布人: {published_by}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@version.command("list")
@click.option("--template", "-t", "template_id_or_name", default=None, help="按模板ID或名称筛选")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.pass_context
def version_list(ctx, template_id_or_name, limit):
    state = _get_state(ctx.obj["data_dir"])

    template_id = ""
    if template_id_or_name:
        tpl = state.get_template(template_id_or_name)
        if not tpl:
            tpl = state.get_template_by_name(template_id_or_name)
        if tpl:
            template_id = tpl.template_id
        else:
            click.echo(click.style(f"错误: 模板不存在: {template_id_or_name}", fg="red"), err=True)
            sys.exit(1)

    versions = list_versions(state, template_id=template_id)

    if not versions:
        click.echo("暂无版本记录")
        return

    display = versions[:limit]

    click.echo(click.style(f"=== 版本列表 (共 {len(versions)} 条) ===", bold=True))
    click.echo()

    for i, ver in enumerate(display, 1):
        status_name = STATUS_NAMES.get(ver.target_status, ver.target_status)
        click.echo(f"[{i}] {click.style(ver.version_id, fg='cyan')} - {ver.version_name}")
        click.echo(f"    模板: {ver.template_name} ({ver.template_id})")
        click.echo(f"    目标状态: {status_name}  处理人: {ver.handler or '-'}  "
                   f"备注: {ver.remark or '-'}  来源: {ver.source_type or '-'}")
        click.echo(f"    发布时间: {ver.published_at[:19]}")
        if ver.published_by:
            click.echo(f"    发布人: {ver.published_by}")
        click.echo()

    if len(versions) > limit:
        click.echo(f"... 还有 {len(versions) - limit} 条，使用 -n 调整显示数量")


@version.command("show")
@click.argument("version_id")
@click.pass_context
def version_show(ctx, version_id):
    state = _get_state(ctx.obj["data_dir"])

    try:
        ver = get_version(state, version_id)
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)

    status_name = STATUS_NAMES.get(ver.target_status, ver.target_status)

    click.echo(click.style(f"=== {ver.version_name} ===", bold=True, fg="cyan"))
    click.echo(f"版本ID: {ver.version_id}")
    click.echo(f"模板: {ver.template_name} ({ver.template_id})")
    click.echo(f"目标状态: {status_name}")
    click.echo(f"处理人: {ver.handler or '-'}")
    click.echo(f"备注: {ver.remark or '-'}")
    click.echo(f"来源: {ver.source_type or '-'}")
    click.echo(f"描述: {ver.description or '-'}")
    click.echo(f"发布时间: {ver.published_at[:19]}")
    if ver.published_by:
        click.echo(f"发布人: {ver.published_by}")

    tpl = state.get_template(ver.template_id)
    if not tpl:
        click.echo(click.style("  [当前模板已删除，版本历史仍可查看]", fg="yellow"))


@version.command("compare")
@click.argument("version_a_id")
@click.argument("version_b_id")
@click.pass_context
def version_compare(ctx, version_a_id, version_b_id):
    state = _get_state(ctx.obj["data_dir"])

    try:
        result = compare_versions(state, version_a_id, version_b_id)
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style("=== 版本比较 ===", bold=True, fg="cyan"))
    click.echo(f"版本A: {result.version_a_name} ({result.version_a_id})")
    click.echo(f"版本B: {result.version_b_name} ({result.version_b_id})")
    click.echo()

    if result.is_same:
        click.echo(click.style(f"{_sym('check')} 两个版本在比较字段上完全一致", fg="green"))
    else:
        click.echo(click.style(f"发现 {len(result.diffs)} 处差异:", fg="yellow", bold=True))
        for diff in result.diffs:
            click.echo(f"  {diff.field_label}:")
            click.echo(f"    版本A: {diff.old_value or '(空)'}")
            click.echo(f"    版本B: {diff.new_value or '(空)'}")
            click.echo()


@version.command("restore")
@click.argument("version_id")
@click.option("--dry-run", is_flag=True, help="仅预览恢复效果，不落盘")
@click.pass_context
def version_restore(ctx, version_id, dry_run):
    state = _get_state(ctx.obj["data_dir"])

    try:
        preview = preview_restore_version(state, version_id)
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style("=== 版本恢复预览 ===", bold=True, fg="cyan"))
    click.echo(f"版本: {preview.version_name} ({preview.version_id})")
    click.echo(f"模板: {preview.template_name} ({preview.template_id})")
    click.echo()

    if not preview.diffs:
        click.echo(click.style(f"{_sym('check')} 当前模板与版本一致，无需恢复", fg="green"))
        return

    click.echo(click.style(f"将变更 {len(preview.diffs)} 处:", fg="yellow", bold=True))
    for diff in preview.diffs:
        click.echo(f"  {diff.field_label}:")
        click.echo(f"    当前: {diff.old_value or '(空)'}")
        click.echo(f"    恢复为: {diff.new_value or '(空)'}")
    click.echo()

    if dry_run:
        click.echo(click.style("预览模式 (dry-run)，不执行恢复", fg="cyan"))
        return

    try:
        template = restore_version(state, version_id)
        status_name = STATUS_NAMES.get(template.target_status, template.target_status)
        click.echo(click.style(f"恢复成功: {template.name}", fg="green", bold=True))
        click.echo(f"  目标状态: {status_name}")
        click.echo(f"  处理人: {template.handler or '-'}")
        click.echo(f"  备注: {template.remark or '-'}")
        click.echo(f"  更新时间: {template.updated_at[:19]}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@version.command("import")
@click.argument("json_file", type=click.Path(exists=True))
@click.option("--strategy", "-s", type=click.Choice(["overwrite", "save_as", "skip", "abort"]),
              default="skip", help="冲突处理策略", show_default=True)
@click.option("--dry-run", is_flag=True, help="仅预检冲突，不导入")
@click.pass_context
def version_import(ctx, json_file, strategy, dry_run):
    state = _get_state(ctx.obj["data_dir"])

    try:
        if dry_run:
            result = precheck_import_conflicts(state, json_file)
            click.echo(click.style("=== 导入预检结果 ===", bold=True, fg="cyan"))
            click.echo(f"文件: {json_file}")
            click.echo(f"待导入模板: {result.total_import_templates} 个")
            click.echo(f"待导入版本: {result.total_import_versions} 个")
            click.echo()

            if result.has_conflicts:
                click.echo(click.style(f"发现 {len(result.conflicts)} 个冲突:", fg="yellow", bold=True))
                for c in result.conflicts:
                    click.echo(f"  {_sym('warn')} {c.template_name}")
                    click.echo(f"      冲突类型: {c.conflict_type}")
                    click.echo(f"      本地来源: {c.local_version_source or '-'}  导入来源: {c.import_version_source or '-'}")
                    if c.local_versions:
                        click.echo(f"      本地版本: {', '.join(c.local_versions)}")
                    if c.import_versions:
                        click.echo(f"      导入版本: {', '.join(c.import_versions)}")
                    click.echo()
                click.echo("可选策略: overwrite(覆盖) / save_as(另存) / skip(跳过) / abort(整批失败)")
            else:
                click.echo(click.style(f"{_sym('check')} 无冲突，可安全导入", fg="green"))
            return

        result = import_with_versions(state, json_file, conflict_strategy=strategy)

        click.echo(click.style("=== 导入结果 ===", bold=True))
        click.echo(f"文件: {json_file}")
        click.echo(f"冲突策略: {strategy}")
        click.echo()

        if result["imported_templates"]:
            click.echo(click.style(f"成功导入模板 {len(result['imported_templates'])} 个:", fg="green"))
            for name in result["imported_templates"]:
                click.echo(f"  {_sym('check')} {name}")
            click.echo()

        if result["imported_versions"]:
            click.echo(click.style(f"成功导入版本 {len(result['imported_versions'])} 个:", fg="green"))
            for name in result["imported_versions"]:
                click.echo(f"  {_sym('check')} {name}")
            click.echo()

        if result["saved_as"]:
            click.echo(click.style(f"另存 {len(result['saved_as'])} 个:", fg="cyan"))
            for name in result["saved_as"]:
                click.echo(f"  {_sym('arrow')} {name}")
            click.echo()

        if result["skipped"]:
            click.echo(click.style(f"跳过 {len(result['skipped'])} 个:", fg="yellow"))
            for name in result["skipped"]:
                click.echo(f"  {_sym('warn')} {name}")
            click.echo()

        if result["errors"]:
            click.echo(click.style(f"错误 {len(result['errors'])} 个:", fg="red"))
            for err in result["errors"]:
                click.echo(f"  {_sym('cross')} {err}")
            click.echo()

        click.echo(f"审计ID: {result['audit_id']}")

    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@version.command("export")
@click.argument("output_file")
@click.option("--ids", default=None, help="逗号分隔的模板ID列表，不指定则导出全部")
@click.pass_context
def version_export(ctx, output_file, ids):
    state = _get_state(ctx.obj["data_dir"])

    template_ids = None
    if ids:
        template_ids = [x.strip() for x in ids.split(",") if x.strip()]

    try:
        count = export_with_versions(state, output_file, template_ids=template_ids)
        versions_count = sum(len(state.list_versions(template_id=tid)) for tid in (template_ids or [t.template_id for t in state.list_templates()]))
        click.echo(click.style(f"导出成功: {output_file}", fg="green"))
        click.echo(f"  模板数量: {count}")
        click.echo(f"  版本数量: {versions_count}")
    except Exception as e:
        click.echo(click.style(f"导出失败: {e}", fg="red"), err=True)
        sys.exit(1)


@cli.group()
@click.pass_context
def archive(ctx):
    """模板档案馆 - 永久固化的发布版本，不受模板改名/删除影响"""
    pass


@archive.command("list")
@click.option("--template", "-t", "template_id_or_name", default=None, help="按模板ID或名称筛选")
@click.option("--template-name", default=None, help="按档案中保存的模板名筛选（模板删除后仍可用）")
@click.option("--limit", "-n", default=20, help="显示条数", show_default=True)
@click.pass_context
def archive_list(ctx, template_id_or_name, template_name, limit):
    """列出档案（模板删除后仍可查询）"""
    state = _get_state(ctx.obj["data_dir"])

    if template_name:
        archives = list_archives(state, template_name=template_name)
    elif template_id_or_name:
        tpl = state.get_template(template_id_or_name)
        if not tpl:
            tpl = state.get_template_by_name(template_id_or_name)
        if tpl:
            archives = list_archives(state, template_id=tpl.template_id)
        else:
            archives = list_archives(state, template_name=template_id_or_name)
    else:
        archives = list_archives(state)

    if not archives:
        click.echo("暂无档案记录")
        return

    display = archives[:limit]

    click.echo(click.style(f"=== 档案列表 (共 {len(archives)} 条) ===", bold=True))
    click.echo()

    for i, arc in enumerate(display, 1):
        status_name = STATUS_NAMES.get(arc.target_status, arc.target_status)
        tpl = state.get_template(arc.template_id)
        tpl_status = "" if tpl else " [模板已删除]"
        tpl_status_color = "yellow" if not tpl else "white"

        click.echo(f"[{i}] {click.style(arc.archive_id, fg='cyan')} - {arc.version_name}")
        click.echo(f"    模板: {arc.template_name}{tpl_status}"
                   if tpl_status else f"    模板: {arc.template_name}")
        click.echo(f"    目标状态: {status_name}  处理人: {arc.handler or '-'}  "
                   f"备注: {arc.remark or '-'}  来源: {arc.source_type or '-'}")
        click.echo(f"    归档时间: {arc.archived_at[:19]}  发布时间: {arc.published_at[:19]}")
        if arc.published_by:
            click.echo(f"    发布人: {arc.published_by}")
        if arc.archive_note:
            click.echo(f"    归档备注: {arc.archive_note}")
        click.echo()

    if len(archives) > limit:
        click.echo(f"... 还有 {len(archives) - limit} 条，使用 -n 调整显示数量")


@archive.command("show")
@click.argument("archive_id")
@click.pass_context
def archive_show(ctx, archive_id):
    """显示档案详情"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        arc = get_archive(state, archive_id)
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)

    status_name = STATUS_NAMES.get(arc.target_status, arc.target_status)
    tpl = state.get_template(arc.template_id)

    click.echo(click.style(f"=== {arc.version_name} ===", bold=True, fg="cyan"))
    click.echo(f"档案ID: {arc.archive_id}")
    click.echo(f"模板: {arc.template_name} ({arc.template_id})")
    if not tpl:
        click.echo(click.style("  [当前模板已删除，档案完整保留]", fg="yellow"))
    click.echo(f"版本名: {arc.version_name}")
    click.echo(f"目标状态: {status_name}")
    click.echo(f"处理人: {arc.handler or '-'}")
    click.echo(f"备注: {arc.remark or '-'}")
    click.echo(f"来源: {arc.source_type or '-'}")
    click.echo(f"描述: {arc.description or '-'}")
    click.echo(f"发布时间: {arc.published_at[:19]}")
    click.echo(f"归档时间: {arc.archived_at[:19]}")
    if arc.published_by:
        click.echo(f"发布人: {arc.published_by}")
    if arc.archive_note:
        click.echo(f"归档备注: {arc.archive_note}")

    if arc.template_snapshot:
        click.echo()
        click.echo("模板快照（归档时的完整副本）:")
        snap = arc.template_snapshot
        click.echo(f"  名称: {snap.get('name', '-')}")
        click.echo(f"  目标状态: {STATUS_NAMES.get(snap.get('target_status', ''), snap.get('target_status', '-'))}")
        click.echo(f"  处理人: {snap.get('handler', '-') or '-'}")
        click.echo(f"  备注: {snap.get('remark', '-') or '-'}")
        click.echo(f"  来源: {snap.get('source_type', '-') or '-'}")


@archive.command("compare")
@click.argument("archive_a_id")
@click.argument("archive_b_id")
@click.pass_context
def archive_compare(ctx, archive_a_id, archive_b_id):
    """比较两个档案的差异（目标状态、处理人、备注、来源）"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        result = compare_archives(state, archive_a_id, archive_b_id)
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style("=== 档案比较 ===", bold=True, fg="cyan"))
    click.echo(f"档案A: {result.archive_a_name} ({result.archive_a_id})")
    click.echo(f"档案B: {result.archive_b_name} ({result.archive_b_id})")
    click.echo()

    if result.is_same:
        click.echo(click.style(f"{_sym('check')} 两个档案在比较字段上完全一致", fg="green"))
    else:
        click.echo(click.style(f"发现 {len(result.diffs)} 处差异:", fg="yellow", bold=True))
        for diff in result.diffs:
            click.echo(f"  {diff.field_label}:")
            click.echo(f"    档案A: {diff.old_value or '(空)'}")
            click.echo(f"    档案B: {diff.new_value or '(空)'}")
            click.echo()


@archive.command("restore")
@click.argument("archive_id")
@click.option("--dry-run", is_flag=True, help="仅预览恢复效果，不落盘")
@click.option("--restored-by", default="", help="恢复操作人")
@click.pass_context
def archive_restore(ctx, archive_id, dry_run, restored_by):
    """从档案恢复模板（模板已删除时自动重建，dry-run 先预览）"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        preview = preview_restore_archive(state, archive_id)
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style("=== 档案恢复预览 ===", bold=True, fg="cyan"))
    click.echo(f"档案: {preview.version_name} ({preview.archive_id})")
    click.echo(f"模板: {preview.template_name} ({preview.template_id})")
    click.echo(f"操作: {preview.restore_action}")
    click.echo()

    if not preview.template_exists:
        click.echo(click.style(f"{_sym('warn')} 模板已删除，恢复将新建模板（沿用原ID）", fg="yellow"))
        click.echo()

    if not preview.diffs and preview.template_exists:
        click.echo(click.style(f"{_sym('check')} 当前模板与档案一致，无需恢复", fg="green"))
        return

    if preview.template_exists:
        click.echo(click.style(f"将变更 {len(preview.diffs)} 处:", fg="yellow", bold=True))
    else:
        click.echo(click.style(f"新建模板配置:", fg="yellow", bold=True))
    for diff in preview.diffs:
        click.echo(f"  {diff.field_label}:")
        click.echo(f"    当前: {diff.old_value or '(空)'}")
        click.echo(f"    恢复为: {diff.new_value or '(空)'}")
    click.echo()

    if dry_run:
        click.echo(click.style("预览模式 (dry-run)，不执行恢复", fg="cyan"))
        return

    try:
        template = restore_archive(state, archive_id, restored_by=restored_by)
        status_name = STATUS_NAMES.get(template.target_status, template.target_status)
        action_desc = "新建" if not preview.template_exists else "恢复"
        click.echo(click.style(f"{action_desc}成功: {template.name}", fg="green", bold=True))
        click.echo(f"  模板ID: {template.template_id}")
        click.echo(f"  目标状态: {status_name}")
        click.echo(f"  处理人: {template.handler or '-'}")
        click.echo(f"  备注: {template.remark or '-'}")
        click.echo(f"  来源: {template.source_type or '-'}")
        click.echo(f"  更新时间: {template.updated_at[:19]}")
    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


@archive.command("export")
@click.argument("output_file")
@click.option("--template-ids", default=None, help="逗号分隔的模板ID列表，导出这些模板的所有档案")
@click.option("--archive-ids", default=None, help="逗号分隔的档案ID列表")
@click.pass_context
def archive_export(ctx, output_file, template_ids, archive_ids):
    """导出档案到 JSON 文件"""
    state = _get_state(ctx.obj["data_dir"])

    tid_list = None
    aid_list = None
    if template_ids:
        tid_list = [x.strip() for x in template_ids.split(",") if x.strip()]
    if archive_ids:
        aid_list = [x.strip() for x in archive_ids.split(",") if x.strip()]

    try:
        count = export_archives(state, output_file, template_ids=tid_list, archive_ids=aid_list)
        click.echo(click.style(f"导出成功: {output_file}", fg="green"))
        click.echo(f"  档案数量: {count}")
    except Exception as e:
        click.echo(click.style(f"导出失败: {e}", fg="red"), err=True)
        sys.exit(1)


@archive.command("import")
@click.argument("json_file", type=click.Path(exists=True))
@click.option("--strategy", "-s", type=click.Choice(["overwrite", "save_as", "skip", "abort"]),
              default="skip", help="冲突处理策略", show_default=True)
@click.option("--dry-run", is_flag=True, help="仅预检冲突，不导入")
@click.pass_context
def archive_import(ctx, json_file, strategy, dry_run):
    """从 JSON 文件导入档案（含冲突预检和 4 种策略）"""
    state = _get_state(ctx.obj["data_dir"])

    try:
        if dry_run:
            result = precheck_archive_import(state, json_file)
            click.echo(click.style("=== 档案导入预检结果 ===", bold=True, fg="cyan"))
            click.echo(f"文件: {json_file}")
            click.echo(f"待导入档案: {result['total_archives']} 个")
            click.echo()

            if result["has_conflicts"]:
                click.echo(click.style(f"发现 {len(result['conflicts'])} 个冲突:", fg="yellow", bold=True))
                for c in result["conflicts"]:
                    click.echo(f"  {_sym('warn')} {c['name']}")
                    click.echo(f"      冲突类型: {c['conflict_type']}")
                    click.echo(f"      本地来源: {c['local_source'] or '-'}  导入来源: {c['import_source'] or '-'}")
                    click.echo()
                click.echo("可选策略: overwrite(覆盖) / save_as(另存) / skip(跳过) / abort(整批失败)")
            else:
                click.echo(click.style(f"{_sym('check')} 无冲突，可安全导入", fg="green"))
            return

        result = import_archives(state, json_file, conflict_strategy=strategy)

        click.echo(click.style("=== 档案导入结果 ===", bold=True))
        click.echo(f"文件: {json_file}")
        click.echo(f"冲突策略: {strategy}")
        click.echo()

        if result["imported"]:
            click.echo(click.style(f"成功导入 {len(result['imported'])} 个:", fg="green"))
            for name in result["imported"]:
                click.echo(f"  {_sym('check')} {name}")
            click.echo()

        if result["saved_as"]:
            click.echo(click.style(f"另存 {len(result['saved_as'])} 个:", fg="cyan"))
            for name in result["saved_as"]:
                click.echo(f"  {_sym('arrow')} {name}")
            click.echo()

        if result["skipped"]:
            click.echo(click.style(f"跳过 {len(result['skipped'])} 个:", fg="yellow"))
            for name in result["skipped"]:
                click.echo(f"  {_sym('warn')} {name}")
            click.echo()

        if result["errors"]:
            click.echo(click.style(f"错误 {len(result['errors'])} 个:", fg="red"))
            for err in result["errors"]:
                click.echo(f"  {_sym('cross')} {err}")
            click.echo()

        click.echo(f"审计ID: {result['audit_id']}")

    except WorkflowError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


def main():
    cli()


if __name__ == "__main__":
    main()
