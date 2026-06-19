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
    import_templates, export_templates, create_draft_from_template
)
from .models import DRAFT_STATUS_NAMES
from .exporter import export_csv, export_csv_with_sources, export_html, export_draft_csv, export_draft_list_csv


def _resolve_draft_template_info(state, draft):
    """
    统一解析草稿的模板来源信息。

    返回 dict:
        has_template: bool  - 是否关联了模板
        template_id: str    - 模板ID（可能为空字符串）
        template_name: str  - 模板名称（优先快照，其次当前模板，再次ID）
        template_exists: bool - 模板在当前存储中是否仍存在
        has_snapshot: bool  - 是否保存了模板快照
        snapshot_target_status: str - 快照中的目标状态（中文名）
        snapshot_handler: str
        snapshot_remark: str
        note: str          - 补充提示（老数据、模板已删除、快照缺失等）
    """
    info = {
        "has_template": False,
        "template_id": "",
        "template_name": "",
        "template_exists": False,
        "has_snapshot": False,
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
    info["has_snapshot"] = bool(snap)

    if snap:
        snap_name = snap.get("name", "")
        snap_status = snap.get("target_status", "")
        info["template_name"] = snap_name
        info["snapshot_target_status"] = STATUS_NAMES.get(snap_status, snap_status)
        info["snapshot_handler"] = snap.get("handler", "") or "-"
        info["snapshot_remark"] = snap.get("remark", "") or "-"

    tpl = state.get_template(tpl_id) if hasattr(state, "get_template") else None
    info["template_exists"] = tpl is not None

    if tpl and not info["template_name"]:
        info["template_name"] = tpl.name

    if not info["template_name"]:
        info["template_name"] = tpl_id

    if info["has_snapshot"]:
        if not info["template_exists"]:
            info["note"] = "模板已删除，但执行时的快照已保留"
        else:
            info["note"] = ""
    else:
        if info["template_exists"]:
            info["note"] = "老数据，未保存模板快照（模板后续变更可能影响溯源）"
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
                        draft_tpl_info = f" [模板: {tpl_info['template_name']}]"
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
            tpl = state.get_template(draft.template_id)
            tpl_name = tpl.name if tpl else draft.template_id
            click.echo(f"  模板来源: {tpl_name} ({draft.template_id})")
            if draft.template_snapshot:
                click.echo(f"  模板快照: 已保存（修改模板不影响此草稿）")
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
            if preview_tpl_snap:
                tpl_name = preview_tpl_snap.get("name", preview_tpl_id)
                click.echo(f"模板: {tpl_name} ({preview_tpl_id})")
                snap_status = preview_tpl_snap.get("target_status", "")
                snap_status_name = STATUS_NAMES.get(snap_status, snap_status)
                click.echo(f"     快照目标状态: {snap_status_name}  "
                           f"处理人: {preview_tpl_snap.get('handler','-')}")
                if not tpl:
                    click.echo(click.style(f"     [当前模板已删除，快照已保留]", fg="yellow"))
            elif tpl:
                click.echo(f"模板: {tpl.name} ({preview_tpl_id})")
                click.echo(click.style(f"     [老数据，未保存模板快照]", fg="yellow"))
            else:
                click.echo(f"模板: {preview_tpl_id}")
                click.echo(click.style(f"     [老数据，模板已删除且无快照]", fg="yellow"))
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
                if tpl_info["has_snapshot"]:
                    click.echo(f"  模板快照: 已保存（目标状态={tpl_info['snapshot_target_status']}, "
                               f"处理人={tpl_info['snapshot_handler']}）")
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
            tpl_line = f"    模板: {tpl_info['template_name']} ({tpl_info['template_id']})"
            if not tpl_info["template_exists"]:
                tpl_line += " [模板已删除]"
            elif not tpl_info["has_snapshot"]:
                tpl_line += " [老数据无快照]"
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
        if tpl_info["has_snapshot"]:
            click.echo(f"     快照保存时间点的配置已保留，不受模板后续修改影响")
            click.echo(f"     快照目标状态: {tpl_info['snapshot_target_status']}")
            click.echo(f"     快照处理人: {tpl_info['snapshot_handler']}")
            if tpl_info["snapshot_remark"] and tpl_info["snapshot_remark"] != "-":
                remark_display = tpl_info["snapshot_remark"]
                if len(remark_display) > 30:
                    remark_display = remark_display[:30] + "..."
                click.echo(f"     快照备注: {remark_display}")
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
                        draft_tpl_info = f" [模板: {tpl_info['template_name']}]"
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
                click.echo(f"      模板: {tpl_info['template_name']} ({tpl_info['template_id']})")
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


def main():
    cli()


if __name__ == "__main__":
    main()
