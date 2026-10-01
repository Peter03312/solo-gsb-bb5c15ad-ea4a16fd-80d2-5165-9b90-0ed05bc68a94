"""FastAPI 入口：API + 原生静态页面。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .core import (
    Config,
    compensation_preview,
    dispute_preview,
    duration_audit,
    external_merge_preview,
    fare_path_audit,
    gate_queue_sim,
    od_flow_publish,
    parse_fares_text,
    parse_passes_text,
    parse_records_text,
    parse_stations_text,
    pass_backtest,
    repair_preview,
    station_peak_audit,
    trial_compare,
    validate_config,
    verify,
)
from .schemas import (
    CompensationPreviewIn,
    ConfigIn,
    DisputePreviewIn,
    GateQueueIn,
    MergePreviewIn,
    OdPublishIn,
    PassBacktestIn,
    PeakAuditIn,
    TextIn,
    TrialConfigIn,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

logger = logging.getLogger("bus-fare-checker")


class _BackupFileResponse(FileResponse):
    """文件发送结束（含客户端中断）后删除快照临时文件。

    包装父类 ``__call__`` 的收尾路径，不依赖后台任务
    （客户端中途断开时 BackgroundTask 不保证执行），
    也不依赖具体 Starlette 版本的 FileResponse 内部方法。
    """

    def __init__(self, path: str, *args, **kwargs):
        super().__init__(path, *args, **kwargs)
        self._tmp_path = path

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                os.unlink(self._tmp_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("清理备份临时文件失败: %s", self._tmp_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="公交刷卡票价核验台",
    description="录入站点分区、票价矩阵、换乘抵扣、每日封顶与刷卡记录，"
                "按卡号配对进出站并核验计价。",
    version="1.9.0",
    lifespan=lifespan,
)


def _parse_error_response(errors: list[dict], status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"ok": False, "errors": errors})


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    with db.connect() as conn:
        cfg = db.get_config(conn)
    return {
        "transfer_minutes": cfg.transfer_minutes,
        "transfer_discount": cfg.transfer_discount,
        "daily_cap": cfg.daily_cap,
        "day_split_hour": cfg.day_split_hour,
    }


@app.put("/api/config")
def put_config(body: ConfigIn):
    cfg = Config(
        transfer_minutes=body.transfer_minutes,
        transfer_discount=body.transfer_discount,
        daily_cap=body.daily_cap,
        day_split_hour=body.day_split_hour,
    )
    with db.connect() as conn:
        db.update_config(conn, cfg)
        # 规则参数变化后，旧账单可能已不再成立，标记为失效
        db.clear_bills(conn)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 站点分区
# ---------------------------------------------------------------------------

@app.get("/api/stations")
def get_stations():
    with db.connect() as conn:
        return {"items": db.list_stations(conn)}


@app.put("/api/stations")
def put_stations(body: TextIn):
    stations, errors = parse_stations_text(body.text)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_stations(conn, stations)
        db.clear_bills(conn)
    return {"ok": True, "count": len(stations)}


# ---------------------------------------------------------------------------
# 票价矩阵
# ---------------------------------------------------------------------------

@app.get("/api/fares")
def get_fares():
    with db.connect() as conn:
        return {"items": db.list_fares(conn)}


@app.put("/api/fares")
def put_fares(body: TextIn):
    fares, errors = parse_fares_text(body.text)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_fares(conn, fares)
        db.clear_bills(conn)
    return {"ok": True, "count": len(fares)}


# ---------------------------------------------------------------------------
# 刷卡记录
# ---------------------------------------------------------------------------

@app.get("/api/records")
def get_records():
    with db.connect() as conn:
        return {"items": db.list_records(conn)}


@app.put("/api/records")
def put_records(body: TextIn):
    default_year = datetime.now().year
    records, errors = parse_records_text(body.text, default_year=default_year)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_records(conn, records)
        db.clear_bills(conn)
    return {"ok": True, "count": len(records)}


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------

@app.post("/api/verify")
def run_verify():
    """执行全量核验。

    成功：清空旧账单并写入新账单（同一事务）。
    失败：返回 422 与全部定位错误，不写入/保留任何账单（无部分账单）。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = verify(stations, fares, records, cfg)
        if not result.ok:
            db.clear_bills(conn)
            return JSONResponse(
                status_code=422,
                content={"ok": False,
                         "errors": result.errors,
                         "bill_count": 0})
        db.save_bills(conn, result.bills)
        return {"ok": True,
                "bill_count": len(result.bills),
                "bills": result.bills}


@app.get("/api/bills")
def get_bills():
    with db.connect() as conn:
        bills = db.list_bills(conn)
        last = db.get_meta(conn, "last_verified_at")
    return {"items": bills, "count": len(bills), "last_verified_at": last}


# ---------------------------------------------------------------------------
# 规则试算对比（只读：不改配置、不写账单）
# ---------------------------------------------------------------------------

@app.post("/api/trial-compare")
def run_trial_compare(body: TrialConfigIn):
    """用一组未保存的草案参数，对同一批已保存刷卡记录做双边试算。

    - 当前配置（已保存）与草案配置分别复用 core.verify 的配对 / 计价规则；
    - 任一方案存在错误（CONFIG_BAD 或数据错误）时返回 422，
      current / draft 各自携带该方案的原有定位错误，不修改任何数据；
    - 两边均成功时返回按 (卡号, 卡内序号) 对齐的行程行、
      每张卡汇总与全批次汇总。
    """
    draft_cfg = Config(
        transfer_minutes=body.transfer_minutes,
        transfer_discount=body.transfer_discount,
        daily_cap=body.daily_cap,
        day_split_hour=body.day_split_hour,
    )
    with db.connect() as conn:
        current_cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        # 草案参数先按核心规则校验（错误返回原有 CONFIG_BAD）；
        # 合法时把整数型字段规整为 int，使运营日/分钟相关文案与正式配置口径一致。
        draft_errors = validate_config(draft_cfg)
        if not draft_errors:
            draft_cfg = Config(
                transfer_minutes=int(draft_cfg.transfer_minutes),
                transfer_discount=draft_cfg.transfer_discount,
                daily_cap=draft_cfg.daily_cap,
                day_split_hour=int(draft_cfg.day_split_hour),
            )
        result = trial_compare(stations, fares, records, current_cfg, draft_cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False,
                     "current": result.current,
                     "draft": result.draft})

    return {
        "ok": True,
        "current": result.current,
        "draft": result.draft,
        "rows": result.rows,
        "card_summaries": result.card_summaries,
        "batch_summary": result.batch_summary,
    }


# ---------------------------------------------------------------------------
# 配对修复预览（只读：不改记录、不写/清空账单）
# ---------------------------------------------------------------------------

@app.post("/api/repair-preview")
def run_repair_preview():
    """对已保存刷卡记录生成「可完整配对」的最小编辑修复预览。

    - 仅针对缺失进站 / 缺失出站 / 连续同向刷卡（MISSING_ENTRY /
      MISSING_EXIT）：按卡号 + 时间排序后做全局最小编辑搜索，
      编辑仅限「翻转进/出方向」与「删除记录」，时刻与站点不改写；
    - 同成本方案按稳定顺序最多返回 5 个，并标明最小编辑数与
      未展示的方案数量，不替用户选定任何方案；
    - 存在非配对错误（未知站点、缺票价、时间逆序、重复刷卡等）时
      返回 422 与 verify() 的原有定位错误，不生成修复方案；
    - 全程只读：不保存记录、不写入也不清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = repair_preview(stations, fares, records, cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "min_edits": result.min_edits,
        "total_plans": result.total_plans,
        "shown_plans": result.shown_plans,
        "hidden_plans": result.hidden_plans,
        "plans": result.plans,
    }


# ---------------------------------------------------------------------------
# 票价矩阵路径审计（只读：不改矩阵、不写/清空账单）
# ---------------------------------------------------------------------------

@app.post("/api/fare-path-audit")
def run_fare_path_audit():
    """对已保存数据做票价矩阵路径审计。

    - 以分区为节点、已配置的有向票价为边，对已保存刷卡记录配对出的
      实际行程分区对计算最低成本路径（整数分 Dijkstra，相同总价按
      分区序列稳定选路，零价边与非对称方向按图语义处理）；
    - 缺直达票价但可达：给出建议票价、完整路径、建议补录行及
      受影响卡号与原始行号；不可达：保留原 MISSING_FARE 定位错误；
    - 已有直达票价高于间接路径：标出差额（直达 − 间接最低）；
    - 除 MISSING_FARE 外的数据错误：返回 422 与 verify() 的原有定位错误；
    - 全程只读：不修改票价矩阵、不写入也不清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = fare_path_audit(stations, fares, records, cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "trip_count": result.trip_count,
        "pair_count": result.pair_count,
        "missing": result.missing,
        "overpriced": result.overpriced,
    }


# ---------------------------------------------------------------------------
# 行程耗时异常审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/duration-audit")
def run_duration_audit():
    """对已保存刷卡记录做行程耗时异常审计。

    - 按（进站站点 -> 出站站点）的有向站点对分组，耗时 = 出站时刻 - 进站时刻；
      仅样本数 >= 6 的站点对参与判定，样本不足的站点对单独列出；
    - 每条行程从基线中排除自身，以其余样本的中位数与 MAD 计算阈值
      max(10 分钟, 3 × 1.4826 × MAD)，绝对偏差严格大于阈值时标记为
      过长 / 过短，并给出基线样本数、中位数、MAD、阈值、偏差与原始进出行号；
    - 配对 / 时间类错误（缺失进出站、时间逆序、重复刷卡）返回 422 与
      verify() 的原有定位错误；未知站点与票价缺项不阻断审计；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = duration_audit(stations, fares, records, cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "trip_count": result.trip_count,
        "pair_count": result.pair_count,
        "audited_pairs": result.audited_pairs,
        "min_samples": result.min_samples,
        "anomaly_count": len(result.anomalies),
        "anomalies": result.anomalies,
        "insufficient": result.insufficient,
    }


# ---------------------------------------------------------------------------
# 站点闸机峰值审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/station-peak-audit")
def run_station_peak_audit(body: PeakAuditIn):
    """对已保存刷卡记录做站点闸机峰值审计。

    - 按（站点, 进/出方向）分组，在 1～120 分钟的给定窗口内查找
      刷卡数最多的半开区间 [开始, 结束)，候选窗口只从实际刷卡时刻起算；
    - 每组返回峰值刷卡数、起止时刻、涉及卡号数与原始文本行号；
      峰值数量并列时依次按更早开始时刻、站点名排序；
    - 峰值刷卡数 >= min_alert 的组标记 alert=true（最低告警数）；
    - 直接统计已保存刷卡记录，不依赖进出站配对、站点分区与票价；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    with db.connect() as conn:
        _stations, _fares, records = db.load_domain(conn)
        result = station_peak_audit(
            records, body.window_minutes, body.min_alert)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "window_minutes": result.window_minutes,
        "min_alert": result.min_alert,
        "record_count": result.record_count,
        "group_count": result.group_count,
        "alert_count": result.alert_count,
        "groups": result.groups,
    }


# ---------------------------------------------------------------------------
# 外部刷卡批次合并预览（只读：不改记录、账单）
# ---------------------------------------------------------------------------

@app.post("/api/external-merge-preview")
def run_external_merge_preview(body: MergePreviewIn):
    """以已保存刷卡记录为基准，预览与外部待导入批次的合并结果。

    - 待导入文本复用 parse_records_text 做行格式解析，解析失败时返回
      422 与原有定位错误（RECORD_BAD_*，行号指向粘贴文本）；
    - 以已保存记录为基准，仅在卡号、站点、进/出方向相同且时间差不超过
      容差（0～120 秒）时建立候选；按键分组做全局一对一匹配：
      先最大化配对数，再最小化总绝对时间差，同分时以基准行号、导入行号
      稳定破同分；
    - 返回重复配对、仅基准 / 仅导入记录与去重后按时间排序的合并文本；
    - 全程只读：不保存待导入记录、不修改已保存记录，也不写入或清空账单。
    """
    default_year = datetime.now().year
    import_records, parse_errors = parse_records_text(
        body.text, default_year=default_year)
    if parse_errors:
        return _parse_error_response(parse_errors)
    with db.connect() as conn:
        _stations, _fares, base_records = db.load_domain(conn)
        result = external_merge_preview(
            base_records, import_records, body.tolerance_seconds)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "tolerance_seconds": result.tolerance_seconds,
        "base_count": result.base_count,
        "import_count": result.import_count,
        "pair_count": result.pair_count,
        "base_only_count": len(result.base_only),
        "import_only_count": len(result.import_only),
        "merged_count": result.base_count + len(result.import_only),
        "total_abs_diff_seconds": result.total_abs_diff_seconds,
        "pairs": result.pairs,
        "base_only": result.base_only,
        "import_only": result.import_only,
        "merged_text": result.merged_text,
    }


# ---------------------------------------------------------------------------
# 匿名客流 OD 发布（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/od-flow-publish")
def run_od_flow_publish(body: OdPublishIn):
    """对已保存且可完整配对的记录生成匿名进站→出站客流聚合。

    - 用户选择运营日起止（闭区间）、15/30/60 分钟初始时间桶与匿名
      阈值 k（2～50）；每个运营日、OD 内以去重卡数判定；
    - 低于 k 的非空桶反复与前后相邻非空桶合并（优先合并后跨度更小者，
      同分选更早邻桶），禁止跨运营日或跨 OD 合并；已无邻桶仍不足 k
      则抑制，抑制项只报数量；
    - 配对 / 时间类错误与参数非法（CONFIG_BAD）沿用原有定位错误返回，
      不产出报表；未知站点与票价缺项不阻断；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = od_flow_publish(
            stations, fares, records, cfg,
            body.start_day, body.end_day,
            body.bucket_minutes, body.k)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "start_day": result.start_day,
        "end_day": result.end_day,
        "bucket_minutes": result.bucket_minutes,
        "k": result.k,
        "day_split_hour": result.day_split_hour,
        "trip_count": result.trip_count,
        "day_count": result.day_count,
        "od_count": result.od_count,
        "row_count": result.row_count,
        "suppressed_bucket_count": result.suppressed_bucket_count,
        "suppressed_trip_count": result.suppressed_trip_count,
        "rows": result.rows,
    }


# ---------------------------------------------------------------------------
# 闸机容量排队推演（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/gate-queue-sim")
def run_gate_queue_sim(body: GateQueueIn):
    """对已保存刷卡记录做闸机容量排队推演。

    - 选择运营日、1/5/15 分钟桶、每桶通行能力与最多续算空桶数；桶从
      运营日分界时刻起对齐，半开区间 [开始, 结束)；
    - 按（站点, 进/出方向）分组统计刷卡到达量，逐桶递推
      处理量 = min(期初积压 + 到达量, 容量)，未处理量结转下一桶；
      最后一笔到达后继续空桶消化，达到续算上限仍未清空则标记未清；
    - 直接统计已保存刷卡记录，不依赖进出站配对、站点分区与票价；
    - 参数非法（CONFIG_BAD）返回 422；全程只读：不修改记录、配置或账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        _stations, _fares, records = db.load_domain(conn)
        result = gate_queue_sim(
            records, cfg,
            body.operating_day, body.bucket_minutes,
            body.capacity, body.max_extra_buckets)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "operating_day": result.operating_day,
        "day_split_hour": result.day_split_hour,
        "bucket_minutes": result.bucket_minutes,
        "capacity": result.capacity,
        "max_extra_buckets": result.max_extra_buckets,
        "day_start": result.day_start,
        "day_end": result.day_end,
        "record_count": result.record_count,
        "group_count": result.group_count,
        "uncleared_count": result.uncleared_count,
        "groups": result.groups,
    }


# ---------------------------------------------------------------------------
# 通票购买策略回测（只读：基于最近一次核验账单，不改任何数据）
# ---------------------------------------------------------------------------

@app.post("/api/pass-backtest")
def run_pass_backtest(body: PassBacktestIn):
    """基于最近一次核验生成的账单，回测每张卡的最优通票购买序列。

    - 通票产品以文本录入（每行：名称, 价格, 连续有效运营日数），行级
      解析错误按 PASS_BAD_* 定位错误返回（行号指向产品文本）；
    - 通票自选定运营日起连续覆盖其有效运营日内的全部行程，不因中间
      无行程暂停；按卡号用动态规划求「通票费 + 未覆盖按次实扣」最低
      的购买序列，同价时依次选择通票更少、首次购买日更早、名称字典序
      更小的方案；
    - 无有效账单（NO_BILLS）、日期非法（CONFIG_BAD）或产品非法
      （PASS_*）时返回 422 与定位错误；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    passes, parse_errors = parse_passes_text(body.passes_text)
    if parse_errors:
        return _parse_error_response(parse_errors)
    with db.connect() as conn:
        bills = db.list_bills(conn)
        last = db.get_meta(conn, "last_verified_at")
    result = pass_backtest(bills, passes, body.start_day, body.end_day, last)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "start_day": result.start_day,
        "end_day": result.end_day,
        "day_count": result.day_count,
        "last_verified_at": last,
        "trip_count": result.trip_count,
        "card_count": result.card_count,
        "baseline_total": result.baseline_total,
        "pass_cost_total": result.pass_cost_total,
        "uncovered_total": result.uncovered_total,
        "pass_total": result.pass_total,
        "saving_total": result.saving_total,
        "passes": result.passes,
        "cards": result.cards,
    }


# ---------------------------------------------------------------------------
# 账单争议更正预演（只读：不写回记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/dispute-preview")
def run_dispute_preview(body: DisputePreviewIn):
    """对已保存刷卡记录与最近一次核验账单做账单争议更正预演。

    - 按原始行号选择同一卡的 1～2 笔已保存记录，仅可临时修改时刻或站点；
    - 后端在内存中原子替换后，对该卡全部记录重新排序、配对与计价
      （复用 verify()），不写回记录、配置或账单；
    - 以进出站原始行号对为行程标识，对比当前账单与更正方案，列出新增、
      移除及金额 / 运营日 / 优惠变化，并汇总该卡补收或退款额；
    - 更正后出现配对、未知站点或缺票价错误时返回 verify() 的原有定位
      错误且不产出差额；跨卡选择、重复行号或修改值非法返回 DISPUTE_*
      明确定位错误；无有效账单时返回 NO_BILLS。
    """
    raw_edits = [{"line": e.line, "time": e.time, "station": e.station}
                 for e in body.edits]
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        bills = db.list_bills(conn)
        last = db.get_meta(conn, "last_verified_at")
        result = dispute_preview(
            stations, fares, records, bills, cfg, raw_edits, last)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False,
                     "card": result.card or None,
                     "edits": result.edits,
                     "errors": result.errors})

    return {
        "ok": True,
        "card": result.card,
        "last_verified_at": result.last_verified_at,
        "edits": result.edits,
        "trips": result.trips,
        "summary": result.summary,
    }


# ---------------------------------------------------------------------------
# 服务中断补偿预览（只读：基于最近一次核验账单，不改记录与账单）
# ---------------------------------------------------------------------------

@app.post("/api/compensation-preview")
def run_compensation_preview(body: CompensationPreviewIn):
    """对已保存账单做服务中断补偿预览（只读）。

    - 选出进站或出站发生在指定站点、且对应刷卡时刻落在闭区间
      [start_time, end_time] 内的行程，按卡汇总实扣额（整数分）；
    - 以各卡实扣额为权重按比例分配预算（整数分），每轮按最大余数
      补齐整数分（余数相同按卡号排序），超过 min(单卡上限, 实扣额)
      的份额封顶并向未封顶卡重新分配，直到预算用尽或全部封顶；
    - 返回每卡受影响行程数、实扣额、补偿额与未分配金额；无匹配账单
      时返回空结果；站点为空、区间非法或金额非法返回 422 定位错误；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    with db.connect() as conn:
        bills = db.list_bills(conn)
        last = db.get_meta(conn, "last_verified_at")
    result = compensation_preview(
        bills, body.station, body.start_time, body.end_time,
        body.budget, body.per_card_cap, last)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "station": result.station,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "budget": result.budget,
        "per_card_cap": result.per_card_cap,
        "last_verified_at": result.last_verified_at,
        "trip_count": result.trip_count,
        "card_count": result.card_count,
        "charged_total": result.charged_total,
        "compensated_total": result.compensated_total,
        "uncompensated_total": result.uncompensated_total,
        "unallocated": result.unallocated,
        "round_count": result.round_count,
        "cards": result.cards,
    }


@app.get("/api/backup")
def download_backup():
    """下载运行中数据库的一致性快照（只读，不改业务数据）。

    - 通过 SQLite 在线备份 API 生成快照，对应某一完整提交点，
      即使备份期间有并发写入也不会得到撕裂文件，不直接复制正在使用的库；
    - 快照先写入临时文件，完成 integrity_check / 表齐全 / 行数一致
      检查后才作为文件下载；
    - 响应结束（含客户端中断）或快照失败后清理临时文件，失败返回 500。
    """
    try:
        tmp_path = db.create_backup_snapshot()
        try:
            return _BackupFileResponse(
                tmp_path,
                media_type="application/vnd.sqlite3",
                filename="bus-backup-%s.db"
                         % datetime.now().strftime("%Y%m%d-%H%M%S"),
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            # 响应对象构造失败（如文件 stat 异常）时也不残留临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except HTTPException:
        raise
    except Exception:
        logger.exception("生成数据库备份快照失败")
        raise HTTPException(status_code=500, detail="backup failed")


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# 原生前端（静态资源）
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
