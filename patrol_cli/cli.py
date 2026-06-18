"""CLI 入口"""

import click
import sys
from datetime import datetime
from pathlib import Path

from .config import load_rules
from .storage import PatrolState
from .models import STATUS_NAMES
from .merger import import_and_merge
from .workflow import review_defect, undo_last, batch_review, WorkflowError
from .exporter import export_csv, export_csv_with_sources, export_html


DEFAULT_CONFIG = "examples/rules.yaml"
DEFAULT_DATA_DIR = "data"


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
@click.pass_context
def import_cmd(ctx, csv_file, batch):
    """导入巡检 CSV 文件并归并缺陷"""
    config = ctx.obj["config"]
    state = _get_state(ctx.obj["data_dir"])

    if not batch:
        if state.batch_id:
            batch = state.batch_id
        else:
            batch = f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    try:
        result = import_and_merge(csv_file, config, state, batch)
        click.echo(click.style("导入完成", fg="green", bold=True))
        click.echo(result.summary())
        if result.invalid_rows:
            click.echo(click.style(f"\n无效行 ({len(result.invalid_rows)}):", fg="yellow"))
            for item in result.invalid_rows[:10]:
                click.echo(f"  第{item['line']}行: {'; '.join(item['errors'])}")
            if len(result.invalid_rows) > 10:
                click.echo(f"  ... 还有 {len(result.invalid_rows) - 10} 条")
        click.echo(f"\n批次号: {state.batch_id}")
        click.echo(f"数据目录: {ctx.obj['data_dir']}")
    except ValueError as e:
        click.echo(click.style(f"错误: {e}", fg="red"), err=True)
        sys.exit(1)


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
        click.echo(click.style(f"复核成功: {defect_id} → {status_name}", fg="green"))
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
        click.echo(click.style(f"失败 {len(errors)} 条:", fg="yellow"))
        for err in errors:
            click.echo(f"  {err}")


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
@click.argument("format", type=click.Choice(["csv", "csv-detail", "html"]))
@click.option("--output", "-o", default=None, help="输出文件路径")
@click.option("--status", "-s", default=None,
              type=click.Choice(["pending", "dispatched", "false_positive", "closed"]),
              help="按状态筛选")
@click.option("--building", default=None, help="按楼栋筛选")
@click.pass_context
def export(ctx, format, output, status, building):
    """导出报告 (csv / csv-detail / html)"""
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
        else:
            click.echo(click.style(f"不支持的格式: {format}", fg="red"), err=True)
            sys.exit(1)

        click.echo(click.style(f"导出成功: {output}", fg="green"))
        click.echo(f"  记录数: {count}")
    except Exception as e:
        click.echo(click.style(f"导出失败: {e}", fg="red"), err=True)
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
            click.echo(f"  {h['time'][:19]}: {from_name} → {to_name} "
                       f"[{handler}] {remark}")


def main():
    cli()


if __name__ == "__main__":
    main()
