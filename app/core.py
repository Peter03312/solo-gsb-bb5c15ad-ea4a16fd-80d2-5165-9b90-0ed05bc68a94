"""公交刷卡票价核验核心逻辑。

纯标准库实现：文本解析、记录校验、进出站配对、
换乘抵扣与运营日封顶计算。不依赖 FastAPI / SQLite，便于单独测试。

金额在内部统一以「元」的浮点数表示并保留两位小数；
存储层写入 SQLite 时转换为整数分，避免二进制浮点误差。
"""
from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 允许的文本分隔符：逗号、制表符、分号、空白(多空格)、全角逗号
_SPLIT_RE = re.compile(r"[,\t;，]|\s+")

# 日期与时刻之间可能用空格连接（如 "2026-09-18 08:00:00"），
# 而空格本身又是字段分隔符，切分前先把时间戳内部空白替换为 T。
# 形如：YYYY[-/]MM[-/]DD HH:MM[:SS] 或 MM[-/]DD HH:MM
_DATETIME_RE = re.compile(
    r"\d{2,4}[-/]\d{1,2}(?:[-/]\d{1,2})?[ \t]+\d{1,2}:\d{2}(?::\d{2})?"
)

# 支持的进站时刻格式
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%dT%H:%M:%S",
    "%Y/%m/%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%m-%dT%H:%M",       # 缺省年份，补当前年
    "%m-%d %H:%M",
    "%m/%dT%H:%M",
    "%m/%d %H:%M",
)

ACTION_IN = "IN"
ACTION_OUT = "OUT"
_ACTION_ALIASES = {
    "IN": "IN", "进": "IN", "进站": "IN", "入": "IN", "上": "IN",
    "OUT": "OUT", "出": "OUT", "出站": "OUT", "下": "OUT",
}


def _err(code: str, message: str, line: Optional[int] = None,
         card: Optional[str] = None, time: Optional[str] = None,
         station: Optional[str] = None, param: Optional[str] = None) -> dict:
    """构造定位错误对象。行号 / 卡号 / 时刻 / 站点 / 参数名尽量补齐，便于定位。"""
    loc: dict = {}
    if line is not None:
        loc["line"] = line
    if card:
        loc["card"] = card
    if time:
        loc["time"] = time
    if station:
        loc["station"] = station
    if param:
        loc["param"] = param
    e: dict = {"code": code, "message": message}
    if loc:
        e["location"] = loc
    return e


def _split(line: str) -> list[str]:
    """按分隔符切分一行文本，去掉空白段。"""
    return [p for p in _SPLIT_RE.split(line.strip()) if p != ""]


def _round2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Station:
    name: str
    zone: int
    line: int = 1  # 保存时的原始行号，用于错误定位


@dataclass
class Fare:
    from_zone: int
    to_zone: int
    price: float
    line: int = 1


@dataclass
class Swipe:
    card: str
    t: datetime
    station: str
    action: str          # IN / OUT
    line: int = 1        # 文本行号（按卡号排序校验时用于定位）

    @property
    def time_text(self) -> str:
        return self.t.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Config:
    transfer_minutes: int = 90          # 换乘有效间隔（分钟）
    transfer_discount: float = 1.0      # 换乘抵扣额（元）
    daily_cap: float = 10.0             # 每卡每运营日封顶额（元）
    day_split_hour: int = 4             # 运营日分界时刻


@dataclass
class VerifyResult:
    ok: bool
    bills: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    # 配对成功的行程 (卡号, 卡内序号, 进站记录, 出站记录)，内部使用：
    # 即使核验失败（如 MISSING_FARE）也会带出，供路径审计复用。
    trips: list = field(default_factory=list)


@dataclass
class TrialResult:
    """规则试算对比结果。

    ok 为 False 时 current / draft 两个分块各自携带错误数组（原有定位错误）；
    ok 为 True 时携带对齐后的行程行 rows、按卡汇总 card_summaries 与
    全批次汇总 batch_summary。
    """
    ok: bool
    current: dict = field(default_factory=dict)
    draft: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    card_summaries: list[dict] = field(default_factory=list)
    batch_summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 文本解析（录入）
# ---------------------------------------------------------------------------

def parse_time(text: str, default_year: Optional[int] = None) -> datetime:
    """解析刷卡时刻，支持多种常见格式；失败抛 ValueError。"""
    text = text.strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=default_year or datetime.now().year)
        return dt
    raise ValueError(f"无法识别的时刻格式：{text}（示例 2026-09-18 08:00:00）")


def _parse_int(text: str, label: str) -> int:
    try:
        v = int(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数：{text!r}")
    return v


def _parse_amount(text: str, label: str = "金额") -> float:
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字：{text!r}")
    if math.isnan(v) or math.isinf(v) or v < 0:
        raise ValueError(f"{label}必须是不小于 0 的数字：{text!r}")
    return _round2(v)


def parse_stations_text(text: str) -> tuple[list[Station], list[dict]]:
    """站点分区文本，每行：站点名, 分区号。"""
    stations: list[Station] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 2:
            errors.append(_err("STATION_BAD_FIELDS",
                               f"站点行应为「站点名, 分区号」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        name, zone_text = parts
        if name in seen:
            errors.append(_err("STATION_DUPLICATE",
                               f"站点 {name} 重复定义", line=i, station=name))
            continue
        try:
            zone = _parse_int(zone_text, "分区号")
        except ValueError as ex:
            errors.append(_err("STATION_BAD_ZONE", str(ex), line=i, station=name))
            continue
        stations.append(Station(name=name, zone=zone, line=i))
        seen.add(name)
    return stations, errors


def parse_fares_text(text: str) -> tuple[list[Fare], list[dict]]:
    """票价矩阵文本，每行：起始分区, 到达分区, 票价。"""
    fares: list[Fare] = []
    errors: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 3:
            errors.append(_err("FARE_BAD_FIELDS",
                               f"票价行应为「起始分区, 到达分区, 票价」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        fz_text, tz_text, price_text = parts
        try:
            fz = _parse_int(fz_text, "起始分区")
            tz = _parse_int(tz_text, "到达分区")
            price = _parse_amount(price_text, "票价")
        except ValueError as ex:
            errors.append(_err("FARE_BAD_VALUE", str(ex), line=i))
            continue
        key = (fz, tz)
        if key in seen:
            errors.append(_err("FARE_DUPLICATE",
                               f"分区 {fz}->{tz} 的票价重复定义", line=i))
            continue
        fares.append(Fare(from_zone=fz, to_zone=tz, price=price, line=i))
        seen.add(key)
    return fares, errors


def parse_records_text(text: str, default_year: Optional[int] = None
                       ) -> tuple[list[Swipe], list[dict]]:
    """刷卡记录文本，每行：卡号, 时刻, 站点, 进/出。

    保存阶段只做行格式解析；同卡配对、时间逆序、重复刷卡等
    跨记录的业务校验统一在 verify() 中完成，保证错误一次性报全。
    """
    records: list[Swipe] = []
    errors: list[dict] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 粘合时间戳内部的空白，避免被当作字段分隔符
        line = _DATETIME_RE.sub(lambda m: re.sub(r"[ \t]+", "T", m.group(0)), line)
        parts = _split(line)
        if len(parts) != 4:
            errors.append(_err("RECORD_BAD_FIELDS",
                               f"记录行应为「卡号, 时刻, 站点, 进/出」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        card, time_text, station, action_text = parts
        card = card.strip()
        if not card:
            errors.append(_err("RECORD_BAD_CARD", "卡号不能为空", line=i))
            continue
        try:
            t = parse_time(time_text, default_year=default_year)
        except ValueError as ex:
            errors.append(_err("RECORD_BAD_TIME", str(ex),
                               line=i, card=card, station=station))
            continue
        action = _ACTION_ALIASES.get(action_text.upper())
        if action is None:
            errors.append(_err("RECORD_BAD_ACTION",
                               f"进出方向必须是 IN/进 或 OUT/出，实际：{action_text!r}",
                               line=i, card=card, time=time_text, station=station))
            continue
        records.append(Swipe(card=card, t=t, station=station,
                             action=action, line=i))
    return records, errors


# ---------------------------------------------------------------------------
# 运营日
# ---------------------------------------------------------------------------

def operating_day(t: datetime, split_hour: int = 4) -> str:
    """以某日 split_hour 点为界归属运营日。

    04:00 之前（含 00:00-03:59:59）归属前一自然日；
    04:00 及之后归属当日。例如 2026-09-18 03:30 属于 2026-09-17 运营日。
    """
    if t.hour < split_hour:
        t = t - timedelta(days=1)
    return t.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _is_int_value(x) -> bool:
    return _is_finite_number(x) and float(x).is_integer()


def validate_config(cfg: Config) -> list[dict]:
    errors: list[dict] = []
    if not _is_int_value(cfg.transfer_minutes) or cfg.transfer_minutes <= 0:
        errors.append(_err("CONFIG_BAD", "换乘有效间隔必须为正整数（分钟）"))
    if not _is_finite_number(cfg.transfer_discount) or cfg.transfer_discount < 0:
        errors.append(_err("CONFIG_BAD", "换乘抵扣额不能为负且必须是有限数字"))
    if not _is_finite_number(cfg.daily_cap) or cfg.daily_cap < 0:
        errors.append(_err("CONFIG_BAD", "每日封顶额不能为负且必须是有限数字"))
    if not _is_int_value(cfg.day_split_hour) or not (0 <= cfg.day_split_hour <= 23):
        errors.append(_err("CONFIG_BAD", "运营日分界时刻必须是 0-23 之间的整数小时"))
    return errors


# ---------------------------------------------------------------------------
# 核验 + 计价
# ---------------------------------------------------------------------------

def _missing_fare_error(card: str, sw_in: "Swipe", sw_out: "Swipe",
                        z1: int, z2: int) -> dict:
    """票价矩阵缺项的定位错误（verify 与路径审计共用同一文案与定位）。"""
    return _err(
        "MISSING_FARE",
        f"票价矩阵缺少分区 {z1}->{z2}（{sw_in.station} -> "
        f"{sw_out.station}）的票价",
        line=sw_in.line, card=card, time=sw_in.time_text,
        station=sw_in.station)


def verify(stations: list[Station],
           fares: list[Fare],
           records: list[Swipe],
           cfg: Config) -> VerifyResult:
    """全量核验并计价。

    原子语义：任何记录级错误（缺出站、缺进站、时间逆序、重复刷卡、
    未知站点、矩阵缺票价）都会使整批核验失败，不生成任何账单。
    只有全部通过时才一次性产出全部行程账单。
    """
    errors: list[dict] = []
    errors.extend(validate_config(cfg))

    zone_of = {s.name: s.zone for s in stations}
    fare_of: dict[tuple[int, int], float] = {
        (f.from_zone, f.to_zone): f.price for f in fares
    }

    # 1) 站点引用检查（未知站点）
    for r in records:
        if r.station not in zone_of:
            errors.append(_err(
                "UNKNOWN_STATION",
                f"刷卡站点 {r.station} 未在站点分区中定义",
                line=r.line, card=r.card,
                time=r.time_text, station=r.station))

    # 2) 按卡号分组，时间升序，逐卡做重复刷卡 / 时间逆序 / 进出站结构检查
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)

    trips: list[tuple[str, int, Swipe, Swipe]] = []
    # trips: (卡号, 卡内序号(从1), 进站记录, 出站记录)

    for card, rs in by_card.items():
        # 录入顺序（数据库按 id 返回，即文本录入先后）必须构成
        # 单调不减的时间流：下降=时间逆序；同卡同时刻=重复刷卡。
        ordered = True
        seen_times: dict[datetime, Swipe] = {}
        for r in rs:
            first = seen_times.get(r.t)
            if first is not None:
                errors.append(_err(
                    "DUPLICATE_SWIPE",
                    f"卡号 {card} 在 {r.time_text} 重复刷卡"
                    f"（第 {r.line} 行与第 {first.line} 行时刻完全相同）",
                    line=r.line, card=card, time=r.time_text,
                    station=r.station))
                ordered = False
            else:
                seen_times[r.t] = r
        for a, b in zip(rs, rs[1:]):
            if b.t < a.t:
                errors.append(_err(
                    "TIME_REVERSED",
                    f"卡号 {card} 刷卡时间逆序：第 {b.line} 行 {b.time_text} "
                    f"早于第 {a.line} 行 {a.time_text}",
                    line=b.line, card=card, time=b.time_text,
                    station=b.station))
                ordered = False

        # 存在逆序/重复时，进出站配对不可靠，跳过该卡结构检查
        # （结构异常很可能是乱序的假象，避免误报叠加）。
        if not ordered:
            continue

        # 时间流有序时录入顺序即时间顺序；逐对做进出站结构检查。
        rs_sorted = rs

        # 3) 进出站交替结构检查并配对。
        #    期望从进站开始，进出成对；起始为出站=缺进站，末尾落单=缺出站。
        i, seq = 0, 0
        while i < len(rs_sorted):
            cur = rs_sorted[i]
            if cur.action != ACTION_IN:
                errors.append(_err(
                    "MISSING_ENTRY",
                    f"卡号 {card} 的出站刷卡缺少配对进站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            if i + 1 >= len(rs_sorted):
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 进站后缺失出站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            nxt = rs_sorted[i + 1]
            if nxt.action != ACTION_OUT:
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 在 {cur.time_text} @ {cur.station} 进站后，"
                    f"下一条不是出站（第 {nxt.line} 行又是进站）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            seq += 1
            trips.append((card, seq, cur, nxt))
            i += 2

    # 4) 票价矩阵覆盖检查（仅对结构完整、站点已知的行程）
    for card, seq, sw_in, sw_out in trips:
        z1 = zone_of.get(sw_in.station)
        z2 = zone_of.get(sw_out.station)
        if z1 is None or z2 is None:
            continue
        if (z1, z2) not in fare_of:
            errors.append(_missing_fare_error(card, sw_in, sw_out, z1, z2))

    if errors:
        # 原子失败：不生成任何账单（无部分账单）
        return VerifyResult(ok=False, bills=[], errors=errors, trips=trips)

    # 5) 计价：按 (卡号, 进站时刻) 顺序处理。
    #    - 换乘抵扣看「同卡前次出站 -> 本次进站」的真实间隔（跨运营日也算）；
    #    - 封顶按行程进站时刻所属运营日、分卡累计。
    per_card_prev_out: dict[str, Swipe] = {}
    # 每卡每运营日累计实扣：card -> {day: amount}
    cumulative: dict[str, dict[str, float]] = {}

    bills: list[dict] = []
    ordered_trips = sorted(trips, key=lambda x: (x[0], x[2].t))

    for card, _seq, sw_in, sw_out in ordered_trips:
        z1 = zone_of[sw_in.station]
        z2 = zone_of[sw_out.station]
        base = _round2(fare_of[(z1, z2)])
        day = operating_day(sw_in.t, cfg.day_split_hour)

        discount = 0.0
        source = "无"
        prev_out = per_card_prev_out.get(card)
        if prev_out is not None:
            gap_min = (sw_in.t - prev_out.t).total_seconds() / 60.0
            if 0 <= gap_min <= cfg.transfer_minutes:
                discount = _round2(min(cfg.transfer_discount, base))
                source = (f"换乘优惠（距上次出站 {int(round(gap_min))} 分钟，"
                          f"≤{cfg.transfer_minutes} 分钟，抵扣 {discount:.2f} 元）")

        after_discount = _round2(base - discount)

        card_days = cumulative.setdefault(card, {})
        already = _round2(card_days.get(day, 0.0))
        cap_room = _round2(cfg.daily_cap - already)
        charge = _round2(max(0.0, min(after_discount, cap_room)))
        if cap_room <= 0:
            source = (f"{source}；当日已封顶" if discount > 0
                      else f"当日封顶（已累计 {already:.2f} 元，达到 "
                           f"{cfg.daily_cap:.2f} 元）")
        elif charge < after_discount:
            source = (f"{source}；封顶保护" if discount > 0
                      else f"封顶保护（当日封顶 {cfg.daily_cap:.2f} 元）")

        card_days[day] = _round2(already + charge)
        per_card_prev_out[card] = sw_out

        bills.append({
            "card": card,
            "seq": None,  # 下方按全局展示顺序统一编号
            "operating_day": day,
            "entry_time": sw_in.time_text,
            "exit_time": sw_out.time_text,
            "entry_station": sw_in.station,
            "exit_station": sw_out.station,
            "from_zone": z1,
            "to_zone": z2,
            "base_fare": base,
            "discount": discount,
            "discount_source": source,
            "charged_fare": charge,
            "daily_cumulative": card_days[day],
            "daily_cap": _round2(cfg.daily_cap),
            "entry_line": sw_in.line,
            "exit_line": sw_out.line,
        })

    # 同一卡号内按进站顺序给出展示序号
    seq_by_card: dict[str, int] = {}
    for b in bills:
        seq_by_card[b["card"]] = seq_by_card.get(b["card"], 0) + 1
        b["seq"] = seq_by_card[b["card"]]

    return VerifyResult(ok=True, bills=bills, errors=[], trips=trips)


# ---------------------------------------------------------------------------
# 配对修复预览（只读：不改记录、不写账单）
# ---------------------------------------------------------------------------

# 修复预览只处理进出站配对结构问题；其余错误（未知站点、缺票价、
# 时间逆序、重复刷卡、参数非法）一经出现即按原样返回，不生成修复方案。
_REPAIRABLE_CODES = {"MISSING_ENTRY", "MISSING_EXIT"}

# 允许的编辑操作（各计 1 次编辑）
OP_KEEP = "keep"      # 原样保留
OP_FLIP = "flip"      # 翻转进/出方向
OP_DELETE = "delete"  # 删除该条记录

# 同成本方案最多返回条数
REPAIR_MAX_PLANS = 5


@dataclass
class RepairPreview:
    """配对修复预览结果。

    ok=False 时 errors 为 verify() 的原有定位错误（未知站点、缺票价、
    时间逆序、重复刷卡等非配对错误原样返回，不生成修复方案）。
    ok=True 时 plans 为最小编辑方案（按稳定顺序，最多 REPAIR_MAX_PLANS 个），
    min_edits 为全局最小编辑数，total_plans 为同成本方案总数，
    hidden_plans 为未展示的条数；min_edits=0 时 plans 为空（无需修复）。
    """
    ok: bool
    min_edits: int = 0
    total_plans: int = 0
    shown_plans: int = 0
    hidden_plans: int = 0
    plans: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _flip_action(action: str) -> str:
    return ACTION_OUT if action == ACTION_IN else ACTION_IN


def _min_edit_solutions(swipes: list[Swipe]
                        ) -> tuple[int, int, list[list[str]]]:
    """单卡（已按时间升序）最小编辑修复：动态规划。

    每条记录可 keep（0 编辑，方向须与期待一致）、flip（1 编辑，
    翻转后须与期待一致）或 delete（1 编辑）；保留记录的方向序列
    必须从进站开始严格交替（即可完整配对）。

    返回 (最小编辑数, 最小编辑方案总数, 按稳定顺序的前
    REPAIR_MAX_PLANS 个操作序列)。稳定顺序为 DFS 字典序
    （keep < flip < delete，靠前的记录优先被编辑）。
    """
    n = len(swipes)
    INF = n + 1
    # f[i][p]：处理第 i..n-1 条、期待方向 p（0=期待进站，1=期待出站）
    # 所需的最小编辑数；g[i][p] 为达到该最小值的方案数。
    f = [[INF, INF] for _ in range(n + 1)]
    f[n][0] = 0
    for i in range(n - 1, -1, -1):
        a = swipes[i].action
        for p in (0, 1):
            best = INF
            if (p == 0) == (a == ACTION_IN):              # keep
                best = min(best, f[i + 1][1 - p])
            if (p == 0) == (_flip_action(a) == ACTION_IN):  # flip
                best = min(best, 1 + f[i + 1][1 - p])
            best = min(best, 1 + f[i + 1][p])             # delete
            f[i][p] = best
    g = [[0, 0] for _ in range(n + 1)]
    g[n][0] = 1
    for i in range(n - 1, -1, -1):
        a = swipes[i].action
        for p in (0, 1):
            total = 0
            if (p == 0) == (a == ACTION_IN) \
                    and f[i + 1][1 - p] == f[i][p]:
                total += g[i + 1][1 - p]
            if (p == 0) == (_flip_action(a) == ACTION_IN) \
                    and 1 + f[i + 1][1 - p] == f[i][p]:
                total += g[i + 1][1 - p]
            if 1 + f[i + 1][p] == f[i][p]:
                total += g[i + 1][p]
            g[i][p] = total

    # 沿最优边按稳定顺序物化前 REPAIR_MAX_PLANS 个操作序列
    plans: list[list[str]] = []

    def walk(i: int, p: int, ops: list[str]) -> None:
        if len(plans) >= REPAIR_MAX_PLANS:
            return
        if i == n:
            plans.append(list(ops))
            return
        a = swipes[i].action
        if (p == 0) == (a == ACTION_IN) and f[i + 1][1 - p] == f[i][p]:
            ops.append(OP_KEEP)
            walk(i + 1, 1 - p, ops)
            ops.pop()
        if (p == 0) == (_flip_action(a) == ACTION_IN) \
                and 1 + f[i + 1][1 - p] == f[i][p]:
            ops.append(OP_FLIP)
            walk(i + 1, 1 - p, ops)
            ops.pop()
        if 1 + f[i + 1][p] == f[i][p]:
            ops.append(OP_DELETE)
            walk(i + 1, p, ops)
            ops.pop()

    walk(0, 0, [])
    return f[0][0], g[0][0], plans


def _build_repair_plan(by_card: dict[str, list[Swipe]],
                       card_plans: dict[str, list[list[str]]],
                       choices: dict[str, int],
                       min_edits: int) -> dict:
    """按各卡选定的操作序列，组装一个完整修复方案。

    方案按卡号排序、卡内按时间顺序逐条给出变更（删除 / 翻转方向），
    并生成修复后的完整记录文本（按卡号 + 时刻排序，方向写作 进/出）。
    """
    changes: list[dict] = []
    fixed: list[Swipe] = []
    for card in sorted(by_card):
        swipes = by_card[card]
        ops = card_plans[card][choices[card]]
        for r, op in zip(swipes, ops):
            if op == OP_DELETE:
                changes.append({
                    "op": OP_DELETE,
                    "card": r.card, "time": r.time_text,
                    "station": r.station, "line": r.line,
                    "from_action": r.action, "to_action": None,
                    "detail": f"删除记录：{r.card} {r.time_text} "
                              f"@ {r.station}（原方向 {r.action}）",
                })
            elif op == OP_FLIP:
                na = _flip_action(r.action)
                changes.append({
                    "op": OP_FLIP,
                    "card": r.card, "time": r.time_text,
                    "station": r.station, "line": r.line,
                    "from_action": r.action, "to_action": na,
                    "detail": f"翻转方向：{r.card} {r.time_text} @ {r.station}"
                              f"（{r.action} → {na}）",
                })
                fixed.append(Swipe(card=r.card, t=r.t, station=r.station,
                                   action=na, line=r.line))
            else:
                fixed.append(r)

    fixed.sort(key=lambda r: (r.card, r.t))
    lines = [f"{r.card}, {r.time_text}, {r.station}, "
             f"{'进' if r.action == ACTION_IN else '出'}" for r in fixed]
    return {
        "edit_count": min_edits,
        "changes": changes,
        "change_count": len(changes),
        "kept_count": len(fixed),
        "trip_count": len(fixed) // 2,
        "fixed_text": "\n".join(lines),
    }


def repair_preview(stations: list[Station],
                   fares: list[Fare],
                   records: list[Swipe],
                   cfg: Config) -> RepairPreview:
    """对已保存刷卡记录生成「可完整配对」的最小编辑修复预览。

    - 先用 verify() 全量校验：通过则无需修复；存在非配对错误
      （未知站点、缺票价、时间逆序、重复刷卡、参数非法）时按原错误返回；
    - 仅针对缺失进站 / 缺失出站 / 连续同向刷卡（verify 的 MISSING_ENTRY /
      MISSING_EXIT），按卡号分组、卡内按时间升序，做全局最小编辑搜索；
    - 允许的编辑只有「翻转进/出方向」与「删除记录」，各计 1 次；
      原始时刻、站点、卡号一律不改写；
    - 同成本方案按稳定顺序最多返回 REPAIR_MAX_PLANS 个，并标明
      最小编辑数与未展示的方案数量，不替用户选定任何方案；
    - 全程只读：不保存记录、不写/清空账单。
    """
    result = verify(stations, fares, records, cfg)
    if result.ok:
        return RepairPreview(ok=True)  # 记录已可完整配对，无需修复

    blocking = [e for e in result.errors
                if e.get("code") not in _REPAIRABLE_CODES]
    if blocking:
        # 非配对错误原样返回，不生成修复方案
        return RepairPreview(ok=False, errors=result.errors)

    # 按卡号分组，卡内按时间升序（verify 已确认有序，即录入顺序；
    # 此处显式排序使预览不依赖录入顺序）
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)
    for rs in by_card.values():
        rs.sort(key=lambda r: r.t)

    # 各卡独立求最小编辑；全局最小编辑数 = 各卡之和，
    # 同成本方案总数 = 各卡方案数之积（卡间相互独立）。
    card_plans: dict[str, list[list[str]]] = {}
    min_edits = 0
    total = 1
    for card in sorted(by_card):
        cost, count, first = _min_edit_solutions(by_card[card])
        card_plans[card] = first
        min_edits += cost
        total *= count

    shown = min(total, REPAIR_MAX_PLANS)
    plans: list[dict] = []
    # 稳定顺序枚举方案组合：混合进制计数，卡号大的方案索引变化最快；
    # 全局前 REPAIR_MAX_PLANS 个组合只依赖各卡物化的前 REPAIR_MAX_PLANS 个序列。
    cards = sorted(by_card)
    for combo in range(shown):
        choices: dict[str, int] = {}
        rest = combo
        for card in reversed(cards):
            choices[card] = rest % len(card_plans[card])
            rest //= len(card_plans[card])
        plans.append(_build_repair_plan(by_card, card_plans,
                                        choices, min_edits))

    return RepairPreview(
        ok=True,
        min_edits=min_edits,
        total_plans=total,
        shown_plans=len(plans),
        hidden_plans=total - len(plans),
        plans=plans,
    )


# ---------------------------------------------------------------------------
# 规则试算对比（不落库、不改配置）
# ---------------------------------------------------------------------------

def _snap_int(x) -> int | float:
    """整数型数字快照为 int（如 120.0 -> 120），非整数保留原值。"""
    return int(x) if _is_finite_number(x) and float(x).is_integer() else x


def _config_snapshot(cfg: Config) -> dict:
    return {
        "transfer_minutes": _snap_int(cfg.transfer_minutes),
        "transfer_discount": _round2(cfg.transfer_discount),
        "daily_cap": _round2(cfg.daily_cap),
        "day_split_hour": _snap_int(cfg.day_split_hour),
    }


def _index_bills(bills: list[dict]) -> dict[tuple[str, int], dict]:
    return {(b["card"], b["seq"]): b for b in bills}


def trial_compare(stations: list[Station],
                  fares: list[Fare],
                  records: list[Swipe],
                  current_cfg: Config,
                  draft_cfg: Config) -> TrialResult:
    """用同一批已保存的刷卡记录，分别按当前配置与草案配置试算计费。

    - 完全复用 verify() 的解析后配对 / 校验 / 计价规则，本函数不做任何写操作；
    - 任一方案失败（配置参数非法或数据错误）时，返回该方案 verify() 产出的
      原有定位错误，不产出对比账单；
    - 两方案都成功时，按 (卡号, 卡内序号) 对齐行程，逐程给出运营日、
      优惠、实扣及差额，并汇总每张卡与全批次的金额变化。
    """
    current = verify(stations, fares, records, current_cfg)
    draft = verify(stations, fares, records, draft_cfg)

    if not current.ok or not draft.ok:
        return TrialResult(
            ok=False,
            current={"config": _config_snapshot(current_cfg),
                     "ok": current.ok,
                     "errors": current.errors},
            draft={"config": _config_snapshot(draft_cfg),
                   "ok": draft.ok,
                   "errors": draft.errors},
        )

    cur_map = _index_bills(current.bills)
    draft_map = _index_bills(draft.bills)
    # 配对与卡内序号仅取决于记录数据本身，两边应当一致；用并集以防万一。
    keys = sorted(set(cur_map) | set(draft_map))

    rows: list[dict] = []
    for card, seq in keys:
        cb = cur_map.get((card, seq))
        db = draft_map.get((card, seq))

        def pick(key: str):
            return (cb[key] if cb is not None else None,
                    db[key] if db is not None else None)

        cur_day, draft_day = pick("operating_day")
        cur_disc, draft_disc = pick("discount")
        cur_charge, draft_charge = pick("charged_fare")
        rows.append({
            "card": card,
            "seq": seq,
            "entry_time": (cb or db)["entry_time"],
            "exit_time": (cb or db)["exit_time"],
            "entry_station": (cb or db)["entry_station"],
            "exit_station": (cb or db)["exit_station"],
            "from_zone": (cb or db)["from_zone"],
            "to_zone": (cb or db)["to_zone"],
            "base_fare": (cb or db)["base_fare"],
            "current_operating_day": cur_day,
            "draft_operating_day": draft_day,
            "day_changed": cur_day != draft_day,
            "current_discount": cur_disc,
            "draft_discount": draft_disc,
            "discount_diff": _round2((draft_disc or 0) - (cur_disc or 0)),
            "current_charge": cur_charge,
            "draft_charge": draft_charge,
            "charge_diff": _round2((draft_charge or 0) - (cur_charge or 0)),
            "draft_discount_source": db["discount_source"] if db else None,
            "current_discount_source": cb["discount_source"] if cb else None,
        })

    card_summaries: list[dict] = []
    for card in sorted({k[0] for k in keys}):
        cb_sum = sum(b["charged_fare"] for b in current.bills if b["card"] == card)
        db_sum = sum(b["charged_fare"] for b in draft.bills if b["card"] == card)
        cd_sum = sum(b["discount"] for b in current.bills if b["card"] == card)
        dd_sum = sum(b["discount"] for b in draft.bills if b["card"] == card)
        card_summaries.append({
            "card": card,
            "trip_count": sum(1 for k in keys if k[0] == card),
            "current_charge_total": _round2(cb_sum),
            "draft_charge_total": _round2(db_sum),
            "charge_diff": _round2(db_sum - cb_sum),
            "current_discount_total": _round2(cd_sum),
            "draft_discount_total": _round2(dd_sum),
            "discount_diff": _round2(dd_sum - cd_sum),
        })

    cur_total = sum(b["charged_fare"] for b in current.bills)
    draft_total = sum(b["charged_fare"] for b in draft.bills)
    cur_disc_total = sum(b["discount"] for b in current.bills)
    draft_disc_total = sum(b["discount"] for b in draft.bills)
    batch_summary = {
        "trip_count": len(keys),
        "current_charge_total": _round2(cur_total),
        "draft_charge_total": _round2(draft_total),
        "charge_diff": _round2(draft_total - cur_total),
        "current_discount_total": _round2(cur_disc_total),
        "draft_discount_total": _round2(draft_disc_total),
        "discount_diff": _round2(draft_disc_total - cur_disc_total),
    }

    return TrialResult(
        ok=True,
        current={"config": _config_snapshot(current_cfg),
                 "ok": True,
                 "errors": [],
                 "bill_count": len(current.bills)},
        draft={"config": _config_snapshot(draft_cfg),
               "ok": True,
               "errors": [],
               "bill_count": len(draft.bills)},
        rows=rows,
        card_summaries=card_summaries,
        batch_summary=batch_summary,
    )


# ---------------------------------------------------------------------------
# 票价矩阵路径审计（只读：不改矩阵、不写账单）
# ---------------------------------------------------------------------------

@dataclass
class FarePathAudit:
    """票价矩阵路径审计结果。

    ok=False 时 errors 为 verify() 的原有定位错误（除 MISSING_FARE 外的
    数据错误原样返回，不生成审计结果）。
    ok=True 时 missing / overpriced 为按 (起始分区, 到达分区) 稳定排序的
    审计发现；不可达的缺失分区对 reachable=False，并在该项 errors 内
    保留原 MISSING_FARE 定位错误。
    """
    ok: bool
    trip_count: int = 0
    pair_count: int = 0
    missing: list[dict] = field(default_factory=list)
    overpriced: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _cents_of(yuan: float) -> int:
    """元 -> 整数分。路径成本全程以整数分累加，避免浮点误差影响等值比较。"""
    return int(round(float(yuan) * 100))


def _yuan_of(cents: int) -> float:
    return round(cents / 100.0, 2)


def _min_cost_paths(fare_cents: dict[tuple[int, int], int], source: int
                    ) -> dict[int, tuple[int, tuple[int, ...]]]:
    """从 source 出发到各分区的 (最低成本分, 分区序列)，Dijkstra。

    - 边为已配置的有向票价（非对称方向按图语义处理，零价边合法）；
    - 优先队列键为 (成本, 分区序列)：相同总价按分区序列字典序稳定选路；
    - 回到 source 的边不参与中途松弛（简单路径不会重返起点）；
      source 自身的同区闭合成本由 _min_cycle 另行计算。
    """
    adj: dict[int, list[tuple[int, int]]] = {}
    for (fz, tz), c in fare_cents.items():
        if tz == source and fz != source:
            continue  # 重返起点的边只在闭合同区路径时考虑
        adj.setdefault(fz, []).append((tz, c))
    for lst in adj.values():
        lst.sort()

    dist: dict[int, tuple[int, tuple[int, ...]]] = {source: (0, (source,))}
    pq: list[tuple[int, tuple[int, ...], int]] = [(0, (source,), source)]
    while pq:
        cost, path, u = heapq.heappop(pq)
        if dist.get(u) != (cost, path):
            continue  # 过期堆项
        for v, w in adj.get(u, ()):
            cand = (cost + w, path + (v,))
            cur = dist.get(v)
            if cur is None or cand < cur:
                dist[v] = cand
                heapq.heappush(pq, (cost + w, path + (v,), v))
    return dist


def _min_cycle(fare_cents: dict[tuple[int, int], int],
               dist: dict[int, tuple[int, tuple[int, ...]]],
               source: int) -> Optional[tuple[int, tuple[int, ...]]]:
    """同区对 (source, source) 的最低成本闭合路径（至少经过一条边）。

    dist 为不重返 source 的最短路径；枚举最后一条回到 source 的边闭合。
    直达自环边（source->source 本身）不算间接路径，不参与闭合。
    """
    best: Optional[tuple[int, tuple[int, ...]]] = None
    for (fz, tz), c in fare_cents.items():
        if tz != source or fz == source:
            continue
        d = dist.get(fz)
        if d is None:
            continue
        cand = (d[0] + c, d[1] + (source,))
        if best is None or cand < best:
            best = cand
    return best


def _path_legs(path: tuple[int, ...],
               fare_yuan: dict[tuple[int, int], float]) -> list[dict]:
    """完整路径的逐段明细（每段的起讫分区与已配置单价）。"""
    return [{"from_zone": a, "to_zone": b, "price": fare_yuan[(a, b)]}
            for a, b in zip(path, path[1:])]


def fare_path_audit(stations: list[Station],
                    fares: list[Fare],
                    records: list[Swipe],
                    cfg: Config) -> FarePathAudit:
    """票价矩阵路径审计（全程只读：不修改矩阵、不写/清空账单）。

    以分区为节点、已配置的有向票价为边，对已保存刷卡记录配对出的
    实际行程分区对计算最低成本路径：

    - 缺直达票价但存在可达路径：给出建议票价（= 最低成本路径总价）、
      完整路径、建议补录行，以及受影响卡号与原始行号；
    - 缺直达票价且不可达：保留 verify() 的原 MISSING_FARE 定位错误；
    - 已有直达票价高于间接路径：标出差额（直达 − 间接最低）与最低成本路径；
    - 除 MISSING_FARE 外的数据错误：按 verify() 原有定位错误返回，不生成审计。

    相同总价按分区序列字典序稳定选路；零价边与非对称方向按有向图语义处理；
    同区行程按「至少经过一条边」的闭合路径计算。
    """
    result = verify(stations, fares, records, cfg)
    if not result.ok:
        blocking = [e for e in result.errors if e.get("code") != "MISSING_FARE"]
        if blocking:
            # 非缺票价错误原样返回（含原有定位信息），不生成审计
            return FarePathAudit(ok=False, errors=result.errors)

    # 此时核验要么全部通过，要么仅剩 MISSING_FARE：站点必然全部已知
    zone_of = {s.name: s.zone for s in stations}
    fare_cents = {(f.from_zone, f.to_zone): _cents_of(f.price) for f in fares}
    fare_yuan = {(f.from_zone, f.to_zone): _round2(f.price) for f in fares}

    # 实际行程分区对 -> 该对上的原始行程（卡号 / 序号 / 进出站记录）
    pair_trips: dict[tuple[int, int], list[tuple[str, int, Swipe, Swipe]]] = {}
    for card, seq, sw_in, sw_out in result.trips:
        key = (zone_of[sw_in.station], zone_of[sw_out.station])
        pair_trips.setdefault(key, []).append((card, seq, sw_in, sw_out))

    # 每个涉及的起始分区跑一次 Dijkstra（整数分、非负边、稳定选路）
    dist_by_src = {z: _min_cost_paths(fare_cents, z)
                   for z in {p[0] for p in pair_trips}}

    def best_path(z1: int, z2: int):
        if z1 == z2:
            return _min_cycle(fare_cents, dist_by_src[z1], z1)
        return dist_by_src[z1].get(z2)

    missing: list[dict] = []
    overpriced: list[dict] = []
    for z1, z2 in sorted(pair_trips):
        raw = sorted(pair_trips[(z1, z2)], key=lambda t: (t[0], t[1]))
        affected = [{
            "card": c, "seq": s,
            "entry_time": i.time_text,
            "entry_station": i.station, "exit_station": o.station,
            "entry_line": i.line, "exit_line": o.line,
        } for c, s, i, o in raw]
        got = best_path(z1, z2)
        direct = fare_cents.get((z1, z2))

        if direct is None:
            item: dict = {
                "from_zone": z1, "to_zone": z2,
                "reachable": got is not None,
                "suggested_price": None,
                "path": None, "path_text": None, "legs": [],
                "suggest_line": None,
                "affected": affected, "trip_count": len(affected),
                "errors": [],
            }
            if got is not None:
                cents, path = got
                price = _yuan_of(cents)
                item.update({
                    "suggested_price": price,
                    "path": list(path),
                    "path_text": " → ".join(str(z) for z in path),
                    "legs": _path_legs(path, fare_yuan),
                    "suggest_line": f"{z1}, {z2}, {price:.2f}",
                })
            else:
                # 不可达：保留原 MISSING_FARE 定位错误（逐受影响行程一条）
                item["errors"] = [
                    _missing_fare_error(c, i, o, z1, z2) for c, s, i, o in raw
                ]
            missing.append(item)
        elif got is not None and got[0] < direct:
            cents, path = got
            best = _yuan_of(cents)
            overpriced.append({
                "from_zone": z1, "to_zone": z2,
                "direct_price": fare_yuan[(z1, z2)],
                "best_price": best,
                "diff": _round2(fare_yuan[(z1, z2)] - best),
                "path": list(path),
                "path_text": " → ".join(str(z) for z in path),
                "legs": _path_legs(path, fare_yuan),
                "affected": affected, "trip_count": len(affected),
            })

    return FarePathAudit(
        ok=True,
        trip_count=len(result.trips),
        pair_count=len(pair_trips),
        missing=missing,
        overpriced=overpriced,
    )


# ---------------------------------------------------------------------------
# 站点闸机峰值审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

# 统计窗口允许的分钟数范围
PEAK_WINDOW_MIN_MINUTES = 1
PEAK_WINDOW_MAX_MINUTES = 120
# 默认最低告警数：峰值刷卡数 >= 该值的组才标记 alert
PEAK_DEFAULT_MIN_ALERT = 1

ACTION_LABELS = {"IN": "进", "OUT": "出"}

# 审计结果中时刻的展示格式（与 Swipe.time_text 一致，精确到秒）
_PEAK_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class PeakAudit:
    """站点闸机峰值审计结果。

    参数非法（窗口分钟数不是 1-120 的整数、最低告警数不是正整数）时
    ok=False，errors 携带 CONFIG_BAD；ok=True 时 groups 按
    峰值数量降序、开始时刻升序、站点名、方向稳定排序，每个组给出
    半开区间 [start, end) 内的峰值刷卡数、涉及卡号数与原始行号。
    """
    ok: bool
    window_minutes: int = 0
    min_alert: int = 0
    record_count: int = 0
    group_count: int = 0
    alert_count: int = 0
    groups: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _validate_peak_params(window_minutes, min_alert) -> list[dict]:
    """峰值审计参数校验，非法时给出与其他接口一致的 CONFIG_BAD 错误。"""
    errors: list[dict] = []
    if not _is_int_value(window_minutes) \
            or not (PEAK_WINDOW_MIN_MINUTES <= int(window_minutes)
                    <= PEAK_WINDOW_MAX_MINUTES):
        errors.append(_err(
            "CONFIG_BAD",
            f"统计窗口必须为 {PEAK_WINDOW_MIN_MINUTES}～"
            f"{PEAK_WINDOW_MAX_MINUTES} 之间的整数（分钟）"))
    if not _is_int_value(min_alert) or int(min_alert) < 1:
        errors.append(_err(
            "CONFIG_BAD", "最低告警数必须为不小于 1 的整数"))
    return errors


def _peak_of_group(events: list[Swipe], window: timedelta
                   ) -> tuple[int, datetime, datetime, int, list[int]]:
    """单组（同站点同方向）事件上求刷卡数最多的半开区间。

    events 已按时刻升序（同时刻按原始行号稳定排序）。候选起点只取
    实际刷卡时刻；窗口为 [开始, 开始 + window)，半开区间保证边界
    时刻的刷卡不会被重复计入。滑动窗口以事件为起点逐个右移，
    出现并列时先遇到的起点更早，故只在严格更大时更新。

    返回 (峰值刷卡数, 开始时刻, 结束时刻, 涉及卡号数, 原始行号列表)。
    """
    best_count = 0
    best_start_idx = 0
    best_end_idx = 0
    right = 0
    for left in range(len(events)):
        start = events[left].t
        limit = start + window
        if right < left:
            right = left
        # 半开区间：时刻严格小于 start + window 才计入
        while right < len(events) and events[right].t < limit:
            right += 1
        count = right - left
        if count > best_count:
            best_count = count
            best_start_idx = left
            best_end_idx = right
    start_t = events[best_start_idx].t
    end_t = start_t + window
    window_events = events[best_start_idx:best_end_idx]
    card_count = len({e.card for e in window_events})
    lines = sorted(e.line for e in window_events)
    return best_count, start_t, end_t, card_count, lines


def station_peak_audit(records: list[Swipe],
                       window_minutes,
                       min_alert) -> PeakAudit:
    """站点闸机峰值审计（全程只读：不改记录、配置或账单）。

    直接统计已保存的刷卡记录（不依赖进出站配对、站点分区与票价）：
    按（站点, 进/出方向）分组，在给定的 1～120 分钟窗口内查找刷卡数
    最多的半开区间 [开始, 结束)。候选窗口只从实际刷卡时刻起算，避免
    固定整点分桶漏掉跨边界高峰；峰值数量并列时取更早的开始时刻
    （事件按时间顺序扫描，首个最大值即最早起点），组间按峰值数量
    降序、开始时刻升序、站点名、方向排序。

    - 每组返回峰值刷卡数、窗口起止时刻、涉及卡号数（去重）与
      窗口内记录的原始文本行号；
    - 峰值刷卡数 >= min_alert 的组标记 alert=true，其余组照常返回
      但不标记；
    - 参数非法（窗口非 1～120 整数、最低告警数非正整数）时返回
      CONFIG_BAD，不生成审计结果。
    """
    errors = _validate_peak_params(window_minutes, min_alert)
    if errors:
        return PeakAudit(ok=False, errors=errors)
    window_minutes = int(window_minutes)
    min_alert = int(min_alert)
    window = timedelta(minutes=window_minutes)

    # （站点, 方向）-> 该组全部刷卡事件
    groups: dict[tuple[str, str], list[Swipe]] = {}
    for r in records:
        groups.setdefault((r.station, r.action), []).append(r)

    result_rows: list[dict] = []
    for (station, action), events in groups.items():
        events.sort(key=lambda e: (e.t, e.line, e.card))
        count, start_t, end_t, card_count, lines = _peak_of_group(
            events, window)
        result_rows.append({
            "_peak": count,
            "_start": start_t,
            "station": station,
            "action": action,
            "direction": ACTION_LABELS.get(action, action),
            "peak_count": count,
            "start_time": start_t.strftime(_PEAK_TIME_FMT),
            "end_time": end_t.strftime(_PEAK_TIME_FMT),
            "card_count": card_count,
            "total_count": len(events),
            "lines": lines,
            "alert": count >= min_alert,
        })

    # 组间排序：峰值数量降序，并列时开始时刻更早优先，再按站点名、方向
    result_rows.sort(key=lambda g: (-g["_peak"], g["_start"],
                                    g["station"], g["action"]))
    for g in result_rows:
        del g["_peak"]
        del g["_start"]

    return PeakAudit(
        ok=True,
        window_minutes=window_minutes,
        min_alert=min_alert,
        record_count=len(records),
        group_count=len(result_rows),
        alert_count=sum(1 for g in result_rows if g["alert"]),
        groups=result_rows,
    )


# ---------------------------------------------------------------------------
# 行程耗时异常审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

# 有向站点对参与判定的最小样本数（行程数），不足的站点对单独列出
DURATION_MIN_SAMPLES = 6
# 阈值下限（分钟）：即使 MAD 为 0，也至少允许 10 分钟的波动
DURATION_FLOOR_MINUTES = 10.0
# 阈值 = max(下限, DURATION_MAD_K × MAD_SCALE × MAD)
DURATION_MAD_K = 3.0
MAD_SCALE = 1.4826  # 正态分布一致性常数

KIND_LONG = "long"    # 耗时过长（高于基线中位数）
KIND_SHORT = "short"  # 耗时过短（低于基线中位数）

# 阻断审计的错误类型（配对 / 时间类）；未知站点、票价缺项、参数类不阻断
_DURATION_BLOCKING_CODES = {
    "MISSING_ENTRY", "MISSING_EXIT", "TIME_REVERSED", "DUPLICATE_SWIPE",
}


@dataclass
class DurationAudit:
    """行程耗时异常审计结果。

    ok=False 时 errors 为 verify() 的原有定位错误（仅配对 / 时间类错误
    阻断审计；未知站点、票价缺项不阻断）。
    ok=True 时 anomalies 为被标记的行程（含基线样本数、中位数、MAD、
    阈值、偏差与原始进出行号），insufficient 为样本不足
    （< DURATION_MIN_SAMPLES 程）而未参与判定的有向站点对。
    """
    ok: bool
    trip_count: int = 0
    pair_count: int = 0
    audited_pairs: int = 0
    min_samples: int = DURATION_MIN_SAMPLES
    anomalies: list[dict] = field(default_factory=list)
    insufficient: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _median(values: list[float]) -> float:
    """中位数；偶数个取中间两值的平均。"""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def duration_audit(stations: list[Station],
                   fares: list[Fare],
                   records: list[Swipe],
                   cfg: Config) -> DurationAudit:
    """行程耗时异常审计（全程只读：不改记录、配置或账单）。

    对已保存且可完整配对的刷卡记录，按（进站站点 -> 出站站点）的有向
    站点对分组，行程耗时 = 出站时刻 - 进站时刻：

    - 仅样本数 >= DURATION_MIN_SAMPLES 的站点对参与判定；样本不足的
      站点对列入 insufficient，不产生任何标记；
    - 每条行程从基线中排除自身，以其余样本的中位数与 MAD（基线样本
      相对中位数的绝对偏差的中位数）计算阈值
      max(10 分钟, 3 × 1.4826 × MAD)；绝对偏差严格大于阈值时标记为
      过长（long）或过短（short）；
    - 判定在秒级精度上进行，展示值换算为分钟并保留两位小数；
    - 配对 / 时间类错误（MISSING_ENTRY / MISSING_EXIT / TIME_REVERSED /
      DUPLICATE_SWIPE）按 verify() 的原有定位错误返回，不生成审计结果；
      未知站点与票价缺项不阻断审计（耗时与分区、票价无关）。
    """
    result = verify(stations, fares, records, cfg)
    if not result.ok:
        blocking = [e for e in result.errors
                    if e.get("code") in _DURATION_BLOCKING_CODES]
        if blocking:
            # 配对 / 时间类错误：原有定位错误原样返回，不生成审计
            return DurationAudit(ok=False, errors=result.errors)

    # 有向站点对 -> 该对上的行程（卡号, 卡内序号, 进站记录, 出站记录）
    pair_trips: dict[tuple[str, str], list[tuple[str, int, Swipe, Swipe]]] = {}
    for card, seq, sw_in, sw_out in result.trips:
        key = (sw_in.station, sw_out.station)
        pair_trips.setdefault(key, []).append((card, seq, sw_in, sw_out))

    anomalies: list[dict] = []
    insufficient: list[dict] = []
    audited = 0
    for pair in sorted(pair_trips):
        trips = sorted(pair_trips[pair], key=lambda t: (t[0], t[1]))
        n = len(trips)
        if n < DURATION_MIN_SAMPLES:
            insufficient.append({
                "entry_station": pair[0],
                "exit_station": pair[1],
                "trip_count": n,
                "required": DURATION_MIN_SAMPLES,
            })
            continue
        audited += 1
        # 秒级耗时（出站 - 进站）；verify 已保证同卡时间有序，耗时非负
        durations = [(t[3].t - t[2].t).total_seconds() for t in trips]
        for idx, (card, seq, sw_in, sw_out) in enumerate(trips):
            baseline = durations[:idx] + durations[idx + 1:]
            median_s = _median(baseline)
            mad_s = _median([abs(x - median_s) for x in baseline])
            threshold_s = max(DURATION_FLOOR_MINUTES * 60.0,
                              DURATION_MAD_K * MAD_SCALE * mad_s)
            dev_s = durations[idx] - median_s
            if abs(dev_s) <= threshold_s:
                continue  # 严格大于阈值才标记
            anomalies.append({
                "kind": KIND_LONG if dev_s > 0 else KIND_SHORT,
                "card": card,
                "seq": seq,
                "entry_station": sw_in.station,
                "exit_station": sw_out.station,
                "entry_time": sw_in.time_text,
                "exit_time": sw_out.time_text,
                "duration_min": _round2(durations[idx] / 60.0),
                "baseline_count": len(baseline),
                "median_min": _round2(median_s / 60.0),
                "mad_min": _round2(mad_s / 60.0),
                "threshold_min": _round2(threshold_s / 60.0),
                "deviation_min": _round2(dev_s / 60.0),
                "entry_line": sw_in.line,
                "exit_line": sw_out.line,
            })

    return DurationAudit(
        ok=True,
        trip_count=len(result.trips),
        pair_count=len(pair_trips),
        audited_pairs=audited,
        anomalies=anomalies,
        insufficient=insufficient,
    )


# ---------------------------------------------------------------------------
# 外部刷卡批次合并预览（只读：不改记录、账单）
# ---------------------------------------------------------------------------

# 配对容差允许的秒数范围（闭区间）
MERGE_TOLERANCE_MIN_SECONDS = 0
MERGE_TOLERANCE_MAX_SECONDS = 120


@dataclass
class MergePreview:
    """外部刷卡批次合并预览结果。

    参数非法（容差不是 0～120 的整数）时 ok=False，errors 携带 CONFIG_BAD；
    待导入文本的解析错误由调用方（parse_records_text）先行拦截。
    ok=True 时 pairs 为重复配对（基准 ↔ 导入 一对一），base_only /
    import_only 为两侧各自独有的记录，merged_text 为去重后按时间排序的
    合并文本（配对取基准行）。
    """
    ok: bool
    tolerance_seconds: int = 0
    base_count: int = 0
    import_count: int = 0
    pair_count: int = 0
    total_abs_diff_seconds: int = 0
    pairs: list[dict] = field(default_factory=list)
    base_only: list[dict] = field(default_factory=list)
    import_only: list[dict] = field(default_factory=list)
    merged_text: str = ""
    errors: list[dict] = field(default_factory=list)


def _lex_bases(base_line: list[int], import_line: list[int], n: int
               ) -> tuple[int, int]:
    """计算费用编码的两个进位基，保证行号分量绝不向上进位。

    一条匹配至多 n 条边：导入行号总和 ≤ n·maxIL（取 B2 严格大于它）；
    单条边中间分量为 基准行号·B2 + 导入行号，n 条之和
    ≤ n·maxBL·B2 + n·maxIL（取 B1 严格大于它）。行号取参与匹配各点的
    真实最大行号，分组很小时基数仍然紧凑。
    """
    max_bl = max(base_line, default=1) or 1
    max_il = max(import_line, default=1) or 1
    b2 = n * max_il + 1
    b1 = n * max_bl * b2 + n * max_il + 1
    return b1, b2


def _hungarian_dense(edge_cost: dict[tuple[int, int], int],
                     n: int, k: int, b1: int, tol: int
                     ) -> list[tuple[int, int]]:
    """稠密 / 完整图上的最小权匹配（e-maxx 匈牙利，整数标量费用）。

    列为 k 条真实导入列 + n 条虚拟列；候选边取已编码整数费用，非候选边
    取足够大的禁止值，虚拟列（未配对）费用 = n·(tol+1)·B1：
    它严格高于任何 n 条候选边的时间差分量总和（每条 ≤ tol，
    加权行号后仍可由 B1 主导），故匹配先最大化配对数、再最小化总费用
    （总时间差 → 基准行号和 → 导入行号和）。
    """
    forbidden = 10 ** 30
    dummy = n * (tol + 1) * b1
    m = k + n
    cost = [[forbidden] * m for _ in range(n)]
    for (bi, ii), c in edge_cost.items():
        cost[bi][ii] = c
    for bi in range(n):
        for j in range(k, m):
            cost[bi][j] = dummy

    INF = 10 ** 36
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while p[j0] != 0:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            r = cost[i0 - 1]
            ui = u[i0]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = r[j - 1] - ui - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    pairs = []
    for j in range(1, m + 1):
        if p[j] != 0 and j - 1 < k:
            pairs.append((p[j] - 1, j - 1))
    return pairs


def _min_cost_flow(edge_cost: dict[tuple[int, int], int],
                   n: int, k: int) -> list[tuple[int, int]]:
    """稀疏图上的最小费用最大流：源→基准→候选导入→汇，容量均为 1。

    沿最短路逐次增广 1 单位，直到汇不可达为止；流量即最大配对数，
    且在最大配对数下费用最小（Johnson 势 + Dijkstra，初始费用非负）。
    相比稠密匈牙利 O(n³)，只遍历容差窗口内的候选边。
    """
    S = n + k
    T = n + k + 1
    V = T + 1
    graph: list[list[list]] = [[] for _ in range(V)]

    def add_edge(u, v, cap, w):
        graph[u].append([v, cap, w, len(graph[v])])
        graph[v].append([u, 0, -w, len(graph[u]) - 1])

    for bi in range(n):
        add_edge(S, bi, 1, 0)
    for ii in range(k):
        add_edge(n + ii, T, 1, 0)
    cand_ref: dict[tuple[int, int], list] = {}
    for (bi, ii), w in edge_cost.items():
        add_edge(bi, n + ii, 1, w)
        cand_ref[(bi, ii)] = graph[bi][-1]

    pot = [0] * V
    for _ in range(n):
        dist: list[Optional[int]] = [None] * V
        prev_v = [-1] * V
        prev_e = [-1] * V
        dist[S] = 0
        pq = [(0, S)]
        while pq:
            d, u = heapq.heappop(pq)
            if d != dist[u]:
                continue
            for ei, e in enumerate(graph[u]):
                v2, cap, w, _rev = e
                if cap <= 0:
                    continue
                nd = d + w + pot[u] - pot[v2]
                if dist[v2] is None or nd < dist[v2]:
                    dist[v2] = nd
                    prev_v[v2] = u
                    prev_e[v2] = ei
                    heapq.heappush(pq, (nd, v2))
        if dist[T] is None:
            break  # 已达最大流
        for v2 in range(V):
            if dist[v2] is not None:
                pot[v2] += dist[v2]
        v2 = T
        while v2 != S:
            u = prev_v[v2]
            e = graph[u][prev_e[v2]]
            e[1] -= 1
            graph[e[0]][e[3]][1] += 1
            v2 = u

    return [(bi, ii) for (bi, ii), e in cand_ref.items() if e[1] == 0]


def _match_group(base: list[Swipe], imports: list[Swipe], tolerance: int
                 ) -> list[tuple[int, int, int]]:
    """单个（卡号, 站点, 方向）分组内的全局一对一匹配。

    仅当 |基准时刻 − 导入时刻| ≤ tolerance（秒）时建立候选边。
    匹配目标依次为：
      1) 配对数最多（最大基数匹配）；
      2) 配对的总绝对时间差最小；
      3) 时间差总和相同时，依次最小化基准行号总和、导入行号总和
         （行号为录入文本原始行，结果与处理顺序无关）。

    候选边费用编码为整数  diff·B1 + 基准行号·B2 + 导入行号，
    B1/B2 取大于任意行号总和的进位基，使整数费用比较等价于上述字典序。
    先剔除不可能配对的孤立点；稠密 / 完整图用匈牙利算法，
    稀疏图（容差窗口内候选很少）用最小费用最大流。

    返回 [(基准下标, 导入下标, 绝对时间差秒), ...]。
    """
    n_all, k_all = len(base), len(imports)
    if n_all == 0 or k_all == 0:
        return []

    # 候选边（原始下标, 秒级绝对时间差）：两侧按时刻排序后滑动窗口生成，
    # 只枚举 |Δt| ≤ tolerance 的对，避免对容差窗口外的记录对做无谓比较。
    bo = sorted(range(n_all), key=lambda i: (base[i].t, base[i].line))
    xo = sorted(range(k_all), key=lambda j: (imports[j].t, imports[j].line))
    bsec = [int(base[i].t.timestamp()) for i in bo]
    xsec = [int(imports[j].t.timestamp()) for j in xo]
    cand: dict[tuple[int, int], int] = {}
    lo = hi = 0
    for pos, bi in enumerate(bo):
        t = bsec[pos]
        while lo < k_all and xsec[lo] < t - tolerance:
            lo += 1
        if hi < lo:
            hi = lo
        while hi < k_all and xsec[hi] <= t + tolerance:
            hi += 1
        for q in range(lo, hi):
            cand[(bi, xo[q])] = abs(t - xsec[q])
    if not cand:
        return []

    # 剔除孤立点：只保留至少有一条候选边的基准 / 导入，压缩成连续子图
    used_b = sorted({bi for bi, _ in cand})
    used_i = sorted({ii for _ , ii in cand})
    bidx = {bi: r for r, bi in enumerate(used_b)}
    iidx = {ii: c for c, ii in enumerate(used_i)}
    n, k = len(used_b), len(used_i)
    bl = [base[bi].line for bi in used_b]
    il = [imports[ii].line for ii in used_i]
    b1, b2 = _lex_bases(bl, il, n)
    edge_cost = {
        (bidx[bi], iidx[ii]): diff * b1 + base[bi].line * b2 + imports[ii].line
        for (bi, ii), diff in cand.items()
    }

    # 稠密（候选占比高）走匈牙利；稀疏走费用流。完整图也归入稠密分支。
    density = len(edge_cost) / (n * k)
    if density >= 0.5:
        rows = _hungarian_dense(edge_cost, n, k, b1, tolerance)
    else:
        rows = _min_cost_flow(edge_cost, n, k)

    return [(used_b[r], used_i[c], cand[(used_b[r], used_i[c])])
            for r, c in rows]


def _merge_item(sw: Swipe, source: str) -> dict:
    return {
        "card": sw.card,
        "time": sw.time_text,
        "station": sw.station,
        "action": sw.action,
        "direction": ACTION_LABELS.get(sw.action, sw.action),
        "line": sw.line,
        "source": source,  # base = 已保存基准记录；import = 待导入记录
    }


def _merge_record_line(card: str, t: datetime, station: str, action: str) -> str:
    return (f"{card}, {t.strftime('%Y-%m-%d %H:%M:%S')}, {station}, "
            f"{'进' if action == ACTION_IN else '出'}")


def external_merge_preview(base_records: list[Swipe],
                           import_records: list[Swipe],
                           tolerance_seconds) -> MergePreview:
    """外部刷卡批次合并预览（全程只读：不改记录、不写/清空账单）。

    - 以**已保存记录为基准**，待导入记录由 parse_records_text() 完成行格式
      解析（解析错误在调用方按原有定位错误返回）；
    - 仅当两条记录的**卡号、站点、进/出方向相同**且时间差不超过
      tolerance 秒（0～120）时建立候选；
    - 按（卡号, 站点, 方向）分组各自做**全局一对一匹配**：先最大化配对数，
      再最小化配对的总绝对时间差，完全同分时以基准行号、导入行号稳定破同分；
    - 返回重复配对、仅基准记录、仅导入记录，以及去重后按时间排序的合并文本
      （配对取基准行，格式与录入文本兼容，可直接复制核对）。
    """
    if not _is_int_value(tolerance_seconds) \
            or not (MERGE_TOLERANCE_MIN_SECONDS
                    <= int(tolerance_seconds) <= MERGE_TOLERANCE_MAX_SECONDS):
        return MergePreview(
            ok=False,
            errors=[_err(
                "CONFIG_BAD",
                f"合并容差必须为 {MERGE_TOLERANCE_MIN_SECONDS}～"
                f"{MERGE_TOLERANCE_MAX_SECONDS} 之间的整数（秒）")])
    tolerance = int(tolerance_seconds)

    groups: dict[tuple[str, str, str],
                 dict[str, list[Swipe]]] = {}
    for r in base_records:
        groups.setdefault((r.card, r.station, r.action),
                          {"base": [], "import": []})["base"].append(r)
    for r in import_records:
        groups.setdefault((r.card, r.station, r.action),
                          {"base": [], "import": []})["import"].append(r)

    pair_objs: list[dict] = []
    matched_base: set[int] = set()   # 以 id() 标记被配对的基准记录
    matched_import: set[int] = set()
    total_diff = 0
    # 合并文本中的条目：(排序键, 文本行)
    merged_entries: list[tuple[tuple, str]] = []

    for key in sorted(groups):
        g = groups[key]
        bs, xs = g["base"], g["import"]
        pairs = _match_group(bs, xs, tolerance)
        for bi, ii, diff in pairs:
            b, x = bs[bi], xs[ii]
            matched_base.add(id(b))
            matched_import.add(id(x))
            total_diff += diff
            pair_objs.append({
                "card": b.card,
                "station": b.station,
                "action": b.action,
                "direction": ACTION_LABELS.get(b.action, b.action),
                "base": _merge_item(b, "base"),
                "import": _merge_item(x, "import"),
                "base_time": b.time_text,
                "import_time": x.time_text,
                "abs_diff_seconds": diff,
                "delta_seconds": int((x.t - b.t).total_seconds()),
            })
            # 配对以基准行计入合并文本
            merged_entries.append((
                (b.t, b.card, b.station, b.action, 0, b.line),
                _merge_record_line(b.card, b.t, b.station, b.action)))

    base_only = [r for r in base_records if id(r) not in matched_base]
    import_only = [r for r in import_records if id(r) not in matched_import]
    for r in base_only:
        merged_entries.append((
            (r.t, r.card, r.station, r.action, 0, r.line),
            _merge_record_line(r.card, r.t, r.station, r.action)))
    for r in import_only:
        merged_entries.append((
            (r.t, r.card, r.station, r.action, 1, r.line),
            _merge_record_line(r.card, r.t, r.station, r.action)))
    merged_entries.sort(key=lambda e: e[0])

    pair_objs.sort(key=lambda p: (p["base"]["time"], p["base"]["line"],
                                  p["import"]["line"]))
    base_only_items = sorted(
        (_merge_item(r, "base") for r in base_only),
        key=lambda it: (it["time"], it["line"]))
    import_only_items = sorted(
        (_merge_item(r, "import") for r in import_only),
        key=lambda it: (it["time"], it["line"]))

    return MergePreview(
        ok=True,
        tolerance_seconds=tolerance,
        base_count=len(base_records),
        import_count=len(import_records),
        pair_count=len(pair_objs),
        total_abs_diff_seconds=total_diff,
        pairs=pair_objs,
        base_only=base_only_items,
        import_only=import_only_items,
        merged_text="\n".join(line for _key, line in merged_entries),
    )


# ---------------------------------------------------------------------------
# 匿名客流 OD 发布（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

# 初始时间桶可选粒度（分钟，按自然日整点对齐）
OD_BUCKET_CHOICES = (15, 30, 60)
# 匿名阈值 k 的合法范围（每个发布区间内的去重卡数下限）
OD_K_MIN = 2
OD_K_MAX = 50
OD_DAY_FMT = "%Y-%m-%d"
OD_TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 阻断发布的错误：配对 / 时间类错误与参数非法（CONFIG_BAD）。
# 未知站点（UNKNOWN_STATION）与票价缺项（MISSING_FARE）不阻断：
# OD 聚合只依赖配对后的行程与站点名，与站点分区、票价无关。
_OD_PUBLISH_BLOCKING_CODES = _DURATION_BLOCKING_CODES | {"CONFIG_BAD"}


@dataclass
class OdFlowPublish:
    """匿名客流 OD 发布结果。

    参数非法（运营日起止格式 / 顺序错误、时间桶不在 15/30/60、
    k 不是 2～50 的整数），或存在配对 / 时间类数据错误时 ok=False，
    errors 携带原有定位错误（CONFIG_BAD 或 verify() 的定位错误），
    不产出任何报表。
    ok=True 时 rows 为通过 k-匿名抑制后的最终发布区间（按运营日、
    OD、区间起点稳定排序），suppressed_* 仅给出被抑制桶 / 行程的
    汇总数量，不含任何卡号、原始行号或单条时刻。
    """
    ok: bool
    start_day: str = ""
    end_day: str = ""
    bucket_minutes: int = 0
    k: int = 0
    day_split_hour: int = 0
    trip_count: int = 0
    day_count: int = 0
    od_count: int = 0
    row_count: int = 0
    suppressed_bucket_count: int = 0
    suppressed_trip_count: int = 0
    rows: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _parse_od_day(value, label: str) -> tuple[Optional[str], Optional[dict]]:
    """解析运营日参数（YYYY-MM-DD）；失败返回 CONFIG_BAD 定位错误。"""
    text = str(value).strip()
    try:
        datetime.strptime(text, OD_DAY_FMT)
    except (ValueError, TypeError):
        return None, _err(
            "CONFIG_BAD",
            f"运营日{label}必须是 YYYY-MM-DD 格式的日期：{value!r}")
    return text, None


def _od_bucket_start(t: datetime, bucket_minutes: int) -> datetime:
    """进站时刻所属初始时间桶的起点。

    按自然日 00:00 整点对齐到 bucket_minutes 网格（半开区间
    [起点, 起点 + 桶长)）；运营日归属与桶网格相互独立，跨运营日
    分界的凌晨时段由分组键（运营日）隔离，不会与当日同名桶混淆。
    """
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    idx = (t.hour * 60 + t.minute) // bucket_minutes
    return midnight + timedelta(minutes=idx * bucket_minutes)


def _merge_od_buckets(components: list[dict], k: int
                      ) -> tuple[list[dict], int, int]:
    """单个（运营日, OD）分组内的 k-匿名桶合并 / 抑制。

    components 已按起点升序，每项为
    {start, end, cards: set, trips, buckets}，仅含非空初始桶；
    buckets 为该存活组件覆盖的初始非空桶数（用于抑制数量统计）。
    反复取最靠左的去重卡数 < k 的桶：
      - 与左 / 右相邻存活桶合并，选择**合并后跨度更小**者；
        跨度相同时选更早的左邻桶；
      - 左右均无邻桶（整组仅剩自身仍不足 k）时抑制（删除）该组件。
    合并后去重卡数取并集、行程数与初始桶数相加，并重新参与下一轮判定。
    本函数只处理单个分组，因此不可能跨运营日或跨 OD 合并。

    返回 (存活区间列表, 被抑制的初始桶数, 被抑制的行程数)。
    """
    comps = [{"start": c["start"], "end": c["end"],
              "cards": set(c["cards"]), "trips": c["trips"],
              "buckets": c["buckets"]}
             for c in components]
    suppressed_buckets = 0
    suppressed_trips = 0
    while True:
        # 最靠左的不足 k 非空桶（确定性的处理顺序，结果与录入顺序无关）
        i = next((j for j, c in enumerate(comps)
                  if len(c["cards"]) < k), None)
        if i is None:
            break
        cur = comps[i]
        left = comps[i - 1] if i > 0 else None
        right = comps[i + 1] if i + 1 < len(comps) else None
        if left is None and right is None:
            # 已无相邻非空桶仍不足 k：抑制，不输出该组件任何明细。
            # 统计被抑制组件覆盖的初始非空桶数（纯聚合数量，不含明细）。
            suppressed_buckets += cur["buckets"]
            suppressed_trips += cur["trips"]
            del comps[i]
            continue
        # 合并后跨度（秒）：并入左邻 = 左邻起点 → 当前组件终点；
        # 并入右邻 = 当前组件起点 → 右邻终点。同分优先左邻（更早邻桶）。
        left_span = ((cur["end"] - left["start"]).total_seconds()
                     if left is not None else None)
        right_span = ((right["end"] - cur["start"]).total_seconds()
                      if right is not None else None)
        if right is None or (left is not None and left_span <= right_span):
            left["cards"] |= cur["cards"]
            left["trips"] += cur["trips"]
            left["buckets"] += cur["buckets"]
            left["end"] = cur["end"]
            del comps[i]
        else:
            cur["cards"] |= right["cards"]
            cur["trips"] += right["trips"]
            cur["buckets"] += right["buckets"]
            cur["end"] = right["end"]
            del comps[i + 1]
    return comps, suppressed_buckets, suppressed_trips


def od_flow_publish(stations: list[Station],
                    fares: list[Fare],
                    records: list[Swipe],
                    cfg: Config,
                    start_day, end_day,
                    bucket_minutes, k) -> OdFlowPublish:
    """匿名客流 OD 发布（全程只读：不改记录、配置或账单）。

    仅从**已保存且可完整配对**的记录生成「进站 → 出站」聚合：
    - 用户选择运营日起止（闭区间，按进站时刻归属运营日）、初始时间桶
      （15/30/60 分钟，按自然日整点对齐）与匿名阈值 k（2～50）；
    - 以（运营日, 进站站点, 出站站点, 初始桶）分组，每桶统计行程数与
      去重卡数；每个运营日、OD 内以去重卡数判定是否达到 k；
    - 低于 k 的非空桶反复与前后相邻**非空**桶合并：优先合并后跨度更小
      者，同分选更早邻桶；禁止跨运营日 / 跨 OD 合并；已无邻桶仍不足 k
      则抑制；
    - 发布结果只含最终区间起止（桶粒度）、行程数与去重卡数；抑制项只
      汇总数量，绝不泄露卡号、原始行号或单条时刻；
    - 配对 / 时间类错误（MISSING_ENTRY / MISSING_EXIT / TIME_REVERSED /
      DUPLICATE_SWIPE）与参数非法（CONFIG_BAD）沿用 verify() 的原有
      定位错误返回且不产出报表；未知站点与票价缺项不阻断
      （聚合与分区、票价无关）。
    """
    errors: list[dict] = []
    start_text, e1 = _parse_od_day(start_day, "起始日期")
    end_text, e2 = _parse_od_day(end_day, "结束日期")
    if e1 is not None:
        errors.append(e1)
    if e2 is not None:
        errors.append(e2)
    if not _is_int_value(bucket_minutes) \
            or int(bucket_minutes) not in OD_BUCKET_CHOICES:
        errors.append(_err(
            "CONFIG_BAD",
            f"初始时间桶必须是 {OD_BUCKET_CHOICES} 之一（分钟），"
            f"实际：{bucket_minutes!r}"))
    if not _is_int_value(k) or not (OD_K_MIN <= int(k) <= OD_K_MAX):
        errors.append(_err(
            "CONFIG_BAD",
            f"匿名阈值 k 必须是 {OD_K_MIN}～{OD_K_MAX} 之间的整数，"
            f"实际：{k!r}"))
    if start_text is not None and end_text is not None \
            and start_text > end_text:
        errors.append(_err(
            "CONFIG_BAD",
            f"运营日起始日期不能晚于结束日期：{start_text} ～ {end_text}"))
    if errors:
        return OdFlowPublish(ok=False, errors=errors)
    bucket_minutes = int(bucket_minutes)
    k = int(k)

    # 复用 verify() 的配对结果；配对 / 时间类错误与配置错误阻断发布，
    # 未知站点、票价缺项不阻断（trips 仍由 verify 完整带出）。
    result = verify(stations, fares, records, cfg)
    if not result.ok:
        blocking = [e for e in result.errors
                    if e.get("code") in _OD_PUBLISH_BLOCKING_CODES]
        if blocking:
            return OdFlowPublish(ok=False, errors=result.errors)

    # (运营日, 进站站点, 出站站点) -> {桶起点: {cards: set, trips: n}}
    groups: dict[tuple[str, str, str],
                 dict[datetime, dict]] = {}
    total_trips = 0
    for card, _seq, sw_in, sw_out in result.trips:
        day = operating_day(sw_in.t, cfg.day_split_hour)
        if not (start_text <= day <= end_text):
            continue  # 运营日范围之外的行程不参与发布
        total_trips += 1
        bs = _od_bucket_start(sw_in.t, bucket_minutes)
        bucket = groups.setdefault(
            (day, sw_in.station, sw_out.station), {}
        ).setdefault(bs, {"cards": set(), "trips": 0})
        bucket["cards"].add(card)
        bucket["trips"] += 1

    rows: list[dict] = []
    suppressed_buckets = 0
    suppressed_trips = 0
    days: set[str] = set()
    ods: set[tuple[str, str]] = set()
    for (day, entry_st, exit_st), buckets in groups.items():
        days.add(day)
        ods.add((entry_st, exit_st))
        components = [{
            "start": bs,
            "end": bs + timedelta(minutes=bucket_minutes),
            "cards": data["cards"],
            "trips": data["trips"],
            "buckets": 1,
        } for bs, data in sorted(buckets.items())]
        alive, sb, st = _merge_od_buckets(components, k)
        suppressed_buckets += sb
        suppressed_trips += st
        for c in alive:
            span_minutes = int(
                (c["end"] - c["start"]).total_seconds() // 60)
            rows.append({
                "operating_day": day,
                "entry_station": entry_st,
                "exit_station": exit_st,
                "start_time": c["start"].strftime(OD_TIME_FMT),
                "end_time": c["end"].strftime(OD_TIME_FMT),
                "bucket_minutes": bucket_minutes,
                "span_minutes": span_minutes,
                "merged_buckets": span_minutes // bucket_minutes,
                "trip_count": c["trips"],
                "card_count": len(c["cards"]),
            })

    rows.sort(key=lambda r: (r["operating_day"], r["entry_station"],
                             r["exit_station"], r["start_time"]))

    return OdFlowPublish(
        ok=True,
        start_day=start_text,
        end_day=end_text,
        bucket_minutes=bucket_minutes,
        k=k,
        day_split_hour=cfg.day_split_hour,
        trip_count=total_trips,
        day_count=len(days),
        od_count=len(ods),
        row_count=len(rows),
        suppressed_bucket_count=suppressed_buckets,
        suppressed_trip_count=suppressed_trips,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# 闸机容量排队推演（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

# 时间桶允许的粒度（分钟）；均能整除 1440，桶可从运营日分界时刻严格对齐
GATE_BUCKET_CHOICES = (1, 5, 15)
GATE_DAY_FMT = "%Y-%m-%d"
GATE_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class GateQueueResult:
    """闸机容量排队推演结果。

    参数非法（运营日格式错误、桶粒度不在 1/5/15、每桶通行能力非正整数、
    最多续算桶数为负整数 / 非整数、运营日分界时刻非法）时 ok=False，
    errors 携带 CONFIG_BAD，不生成任何推演结果。
    ok=True 时 groups 按「站点 × 进/出方向」给出每个组的逐桶到达 / 处理 /
    积压推演与峰值积压、首次超载、清空时刻等汇总。
    """
    ok: bool
    operating_day: str = ""
    day_split_hour: int = 0
    bucket_minutes: int = 0
    capacity: int = 0
    max_extra_buckets: int = 0
    day_start: str = ""
    day_end: str = ""
    record_count: int = 0
    group_count: int = 0
    uncleared_count: int = 0
    groups: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def gate_queue_sim(records: list[Swipe],
                   cfg: Config,
                   operating_day,
                   bucket_minutes,
                   capacity,
                   max_extra_buckets) -> GateQueueResult:
    """闸机容量排队推演（全程只读：不改记录、配置或账单）。

    直接统计**已保存的刷卡记录本身**（与进出站配对、站点分区、票价完全
    无关，因此任何配对 / 站点 / 票价错误都不会阻断推演）：

    - 用户选择运营日（YYYY-MM-DD）、桶粒度（1 / 5 / 15 分钟）、每桶通行
      能力（正整数）与最多续算空桶数（非负整数）；桶从**运营日分界时刻**
      起按桶长对齐，半开区间 [开始, 结束)，运营日窗口为
      [当日 split:00, 次日 split:00)；
    - 按（站点, 进/出方向）分组统计每个桶的刷卡到达量；
    - 每组按半开桶递推：处理量 = min(期初积压 + 到达量, 容量)，
      期末积压 = 期初积压 + 到达量 − 处理量，未处理量结转下一桶；
    - 最后一笔到达所在桶之后继续追加**零到达空桶**消化积压，直到积压
      清零或续算桶数达到上限；达到上限仍未清空则标记 cleared=False；
    - 展示逐桶到达、期初积压、处理、期末积压，以及峰值积压（首次达到的
      桶起点）、首次超载（需求 > 容量的首个桶）与清空时刻
      （积压最后一次由正转 0 的桶结束时刻；从未积压则为 None）。
    """
    errors: list[dict] = []
    day_text = str(operating_day).strip()
    day0: Optional[datetime] = None
    try:
        day0 = datetime.strptime(day_text, GATE_DAY_FMT)
    except (ValueError, TypeError):
        errors.append(_err(
            "CONFIG_BAD",
            f"运营日必须是 YYYY-MM-DD 格式的日期：{operating_day!r}"))
    if not _is_int_value(cfg.day_split_hour) \
            or not (0 <= int(cfg.day_split_hour) <= 23):
        errors.append(_err(
            "CONFIG_BAD", "运营日分界时刻必须是 0-23 之间的整数小时"))
    if not _is_int_value(bucket_minutes) \
            or int(bucket_minutes) not in GATE_BUCKET_CHOICES:
        errors.append(_err(
            "CONFIG_BAD",
            f"时间桶粒度必须是 {GATE_BUCKET_CHOICES} 之一（分钟），"
            f"实际：{bucket_minutes!r}"))
    if not _is_int_value(capacity) or int(capacity) < 1:
        errors.append(_err(
            "CONFIG_BAD",
            f"每桶通行能力必须为不小于 1 的整数（人次），实际：{capacity!r}"))
    if not _is_int_value(max_extra_buckets) or int(max_extra_buckets) < 0:
        errors.append(_err(
            "CONFIG_BAD",
            "最多续算空桶数必须为不小于 0 的整数，"
            f"实际：{max_extra_buckets!r}"))
    if errors:
        return GateQueueResult(ok=False, errors=errors)

    split_hour = int(cfg.day_split_hour)
    bucket_minutes = int(bucket_minutes)
    capacity = int(capacity)
    max_extra_buckets = int(max_extra_buckets)

    day_start = day0 + timedelta(hours=split_hour)
    day_end = day_start + timedelta(days=1)
    step = timedelta(minutes=bucket_minutes)
    step_seconds = bucket_minutes * 60

    # （站点, 方向）-> {运营日内桶序号: 到达量}；只统计窗口内的刷卡记录。
    arrivals_by_group: dict[tuple[str, str], dict[int, int]] = {}
    record_count = 0
    for r in records:
        if not (day_start <= r.t < day_end):
            continue
        record_count += 1
        idx = int((r.t - day_start).total_seconds() // step_seconds)
        arrivals_by_group.setdefault((r.station, r.action), {})
        arrivals_by_group[(r.station, r.action)][idx] = \
            arrivals_by_group[(r.station, r.action)].get(idx, 0) + 1

    def bucket_row(idx: int, arrival: int, begin: int,
                   processed: int, end: int, continuation: bool) -> dict:
        start_t = day_start + idx * step
        return {
            "bucket_index": idx,
            "start_time": start_t.strftime(GATE_TIME_FMT),
            "end_time": (start_t + step).strftime(GATE_TIME_FMT),
            "continuation": continuation,
            "arrival_count": arrival,
            "begin_backlog": begin,
            "processed_count": processed,
            "end_backlog": end,
            "overloaded": begin + arrival > capacity,
        }

    groups: list[dict] = []
    for (station, action), arrivals in arrivals_by_group.items():
        first_idx = min(arrivals)
        last_idx = max(arrivals)

        rows: list[dict] = []
        backlog = 0
        peak = 0
        peak_idx: Optional[int] = None
        first_overload_idx: Optional[int] = None
        # 积压最后一次「由正转 0」的桶（最终清空时刻所在桶）
        clear_idx: Optional[int] = None

        # 最后一笔到达之前的空桶（含两笔到达之间的空槽）照常逐桶递推；
        # 首个到达桶之前的空桶必然全 0，不输出。
        for i in range(first_idx, last_idx + 1):
            arrival = arrivals.get(i, 0)
            demand = backlog + arrival
            processed = min(demand, capacity)
            end = demand - processed
            if demand > capacity and first_overload_idx is None:
                first_overload_idx = i
            if end > peak:
                peak = end
                peak_idx = i
            # 清空时刻只记录积压「由正转 0」的桶结束时刻；全程从未积压的
            # 组（clear_time 保持 None）由 first_overload_idx 是否为空识别。
            if backlog > 0 and end == 0:
                clear_idx = i
            rows.append(bucket_row(i, arrival, backlog, processed, end, False))
            backlog = end

        # 最后一笔到达之后继续零到达空桶消化积压（可越过运营日窗口末端，
        # 仍沿同一桶网格），直到清零或达到续算桶数上限。
        extra_used = 0
        idx = last_idx
        while backlog > 0 and extra_used < max_extra_buckets:
            idx += 1
            extra_used += 1
            demand = backlog  # 到达量为 0
            processed = min(demand, capacity)
            end = demand - processed
            if end > peak:
                peak = end
                peak_idx = idx
            if end == 0:
                clear_idx = idx
            rows.append(bucket_row(idx, 0, backlog, processed, end, True))
            backlog = end

        cleared = backlog == 0
        total_arrivals = sum(arrivals.values())

        def time_at(bucket_idx: Optional[int], end_of: bool = False
                    ) -> Optional[str]:
            if bucket_idx is None:
                return None
            # bucket_idx 为桶起点在窗口网格上的序号；取桶结束时刻时
            # 加 1 个桶长（半开区间的右端）。
            t = day_start + (bucket_idx + (1 if end_of else 0)) * step
            return t.strftime(GATE_TIME_FMT)

        groups.append({
            "_peak": peak,
            "_overload": first_overload_idx,
            "station": station,
            "action": action,
            "direction": ACTION_LABELS.get(action, action),
            "total_arrivals": total_arrivals,
            "processed_total": sum(r["processed_count"] for r in rows),
            "peak_backlog": peak,
            "peak_time": time_at(peak_idx),
            "first_overload_time": time_at(first_overload_idx),
            "cleared": cleared,
            "clear_time": time_at(clear_idx, end_of=True)
                          if cleared and clear_idx is not None else None,
            "remaining_backlog": backlog,
            "extra_buckets_used": extra_used,
            "first_bucket_index": first_idx,
            "last_bucket_index": idx,
            "bucket_count": len(rows),
            "buckets": rows,
        })

    # 组间排序：峰值积压降序；并列时首次超载更早优先（从未超载排后），
    # 再按站点名、方向稳定排序。
    groups.sort(key=lambda g: (
        -g["_peak"],
        1 if g["_overload"] is None else 0,
        g["_overload"] if g["_overload"] is not None else 0,
        g["station"], g["action"]))
    for g in groups:
        del g["_peak"]
        del g["_overload"]

    return GateQueueResult(
        ok=True,
        operating_day=day_text,
        day_split_hour=split_hour,
        bucket_minutes=bucket_minutes,
        capacity=capacity,
        max_extra_buckets=max_extra_buckets,
        day_start=day_start.strftime(GATE_TIME_FMT),
        day_end=day_end.strftime(GATE_TIME_FMT),
        record_count=record_count,
        group_count=len(groups),
        uncleared_count=sum(1 for g in groups if not g["cleared"]),
        groups=groups,
    )


# ---------------------------------------------------------------------------
# 通票购买策略回测（只读：基于最近一次核验账单，不改任何数据）
# ---------------------------------------------------------------------------

# 运营日回测范围上限（天，约 10 年）：防止超长范围使动态规划耗时失控
PASS_MAX_RANGE_DAYS = 3662


@dataclass
class PassProduct:
    """一种通票产品：名称、价格（元）与连续有效运营日数。"""
    name: str
    price: float
    valid_days: int
    line: int = 1  # 文本行号，用于解析错误定位


@dataclass
class PassBacktest:
    """通票购买策略回测结果。

    ok=False 时 errors 为定位错误（NO_BILLS / PASS_* / CONFIG_BAD），
    不产出任何回测结果。ok=True 时 cards 为按卡号排序的逐卡最优购买
    方案（含逐次购买明细），汇总字段为全部卡合计（金额单位：元）。
    """
    ok: bool
    start_day: str = ""
    end_day: str = ""
    day_count: int = 0
    trip_count: int = 0
    card_count: int = 0
    baseline_total: float = 0.0
    pass_cost_total: float = 0.0
    uncovered_total: float = 0.0
    pass_total: float = 0.0
    saving_total: float = 0.0
    passes: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def parse_passes_text(text: str) -> tuple[list[PassProduct], list[dict]]:
    """通票产品文本，每行：名称, 价格（元）, 连续有效运营日数。

    与站点 / 票价录入共用同一套分隔符与注释规则（空行与 # 注释行忽略）。
    只做行级解析与取值校验；名称不可重复，价格须为不小于 0 的有限数字，
    连续有效运营日数须为正整数。
    """
    passes: list[PassProduct] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 3:
            errors.append(_err(
                "PASS_BAD_FIELDS",
                f"通票行应为「名称, 价格, 连续有效运营日数」，"
                f"实际 {len(parts)} 段：{line}", line=i))
            continue
        name, price_text, days_text = parts
        if name in seen:
            errors.append(_err("PASS_DUPLICATE",
                               f"通票 {name} 重复定义", line=i))
            continue
        try:
            price = _parse_amount(price_text, "通票价格")
        except ValueError as ex:
            errors.append(_err("PASS_BAD_PRICE", str(ex), line=i))
            continue
        try:
            days = _parse_int(days_text, "连续有效运营日数")
        except ValueError as ex:
            errors.append(_err("PASS_BAD_DAYS", str(ex), line=i))
            continue
        if days <= 0:
            errors.append(_err(
                "PASS_BAD_DAYS",
                f"连续有效运营日数必须为正整数：{days_text!r}", line=i))
            continue
        passes.append(PassProduct(name=name, price=price,
                                  valid_days=days, line=i))
        seen.add(name)
    return passes, errors


def _optimal_pass_plan(day_costs: list[int],
                       passes: list[tuple[str, int, int]]
                       ) -> tuple[int, list[tuple[int, int]]]:
    """单卡最优通票购买序列（动态规划，金额全程整数分）。

    day_costs[i] 为该卡在第 i 个运营日（自回测范围起点 0 起编号）的
    按次实扣合计（分）；passes 为 (名称, 价格分, 连续有效运营日数)。
    在第 i 天购买通票 p 后，第 i .. i+days-1 个运营日内的全部行程由
    通票覆盖（连续有效，不因中间无行程暂停；覆盖超出范围的天数自动
    截断）。该卡总支出 = 通票价格合计 + 未被覆盖运营日的按次实扣合计。

    破同分（严格按此顺序）：总支出更低 → 通票张数更少 →
    首次购买日更早 → 购买序列的名称字典序更小。
    同一状态下各「购买」选项的首次购买日相同而名称互不相同，
    「不买」选项的首次购买日必然更晚，因此名称字典序在首元素即可
    决出，状态只需记录 (支出, 张数, 首次购买日, 首张名称) 四元键
    即可完整破同分；子问题的最优键对父问题充分（各分量均保序）。

    返回 (最低总支出分, [(购买日序号, 通票下标), ...])，购买日升序、
    覆盖区间两两不重叠（重叠购买可被不重叠方案不增成本地替代）。
    """
    n = len(day_costs)
    # f[i]：第 i..n-1 天子问题的最优键 (支出, 张数, 首次购买日, 首张名称)；
    # 无购买时首次购买日记为 n（范围之外），首张名称为空串。
    f: list[tuple[int, int, int, str]] = [(0, 0, n, "")] * (n + 1)
    choice: list[Optional[int]] = [None] * (n + 1)
    for i in range(n - 1, -1, -1):
        # 选项一：第 i 天不买通票，按次实扣
        c1, k1, d1, m1 = f[i + 1]
        best = (day_costs[i] + c1, k1, d1, m1)
        best_choice: Optional[int] = None
        # 选项二：第 i 天购买通票 p，覆盖至 min(i+days, n)
        for pi, (name, price, days) in enumerate(passes):
            j = i + days
            if j > n:
                j = n
            c2, k2, _d2, _m2 = f[j]
            key = (price + c2, 1 + k2, i, name)
            if key < best:
                best = key
                best_choice = pi
        f[i] = best
        choice[i] = best_choice

    plan: list[tuple[int, int]] = []
    i = 0
    while i < n:
        pi = choice[i]
        if pi is None:
            i += 1
            continue
        plan.append((i, pi))
        i += passes[pi][2]
        if i > n:
            i = n
    return f[0][0], plan


def pass_backtest(bills: list[dict],
                  passes: list[PassProduct],
                  start_day, end_day,
                  last_verified_at: Optional[str]) -> PassBacktest:
    """通票购买策略回测（全程只读：只读取最近一次核验生成的账单）。

    - 无有效账单（从未成功核验，或最近一次核验未产生任何行程账单）
      时返回 NO_BILLS；运营日起止非法（格式、起止顺序、范围超过
      PASS_MAX_RANGE_DAYS 天）返回 CONFIG_BAD；通票产品为空返回
      PASS_EMPTY（行级解析错误由 parse_passes_text 先行返回）；
    - 仅统计运营日落在 [start_day, end_day]（闭区间）内的账单行程：
      按卡号分组，把每卡逐运营日的实扣（charged_fare）汇总为逐日
      按次支出（整数分），用 _optimal_pass_plan 求总支出最低的
      购买序列；
    - 范围内没有行程时正常返回（cards 为空），不视为错误。
    """
    errors: list[dict] = []
    if not last_verified_at:
        errors.append(_err(
            "NO_BILLS",
            "尚未生成有效账单：请先在「7. 核验结果」成功运行核验，"
            "通票回测基于最近一次核验生成的账单"))
    elif not bills:
        errors.append(_err(
            "NO_BILLS",
            "最近一次核验未产生任何行程账单，无法进行通票回测"))
    start_text, e1 = _parse_od_day(start_day, "起始日期")
    end_text, e2 = _parse_od_day(end_day, "结束日期")
    if e1 is not None:
        errors.append(e1)
    if e2 is not None:
        errors.append(e2)
    if not passes:
        errors.append(_err(
            "PASS_EMPTY",
            "至少需要录入一种通票产品（名称, 价格, 连续有效运营日数）"))
    if errors:
        return PassBacktest(ok=False, errors=errors)

    if start_text > end_text:
        return PassBacktest(ok=False, errors=[_err(
            "CONFIG_BAD",
            f"运营日起始日期不能晚于结束日期：{start_text} ～ {end_text}")])
    d0 = datetime.strptime(start_text, OD_DAY_FMT)
    d1 = datetime.strptime(end_text, OD_DAY_FMT)
    n = (d1 - d0).days + 1
    if n > PASS_MAX_RANGE_DAYS:
        return PassBacktest(ok=False, errors=[_err(
            "CONFIG_BAD",
            f"运营日范围最长 {PASS_MAX_RANGE_DAYS} 天，实际 {n} 天："
            f"{start_text} ～ {end_text}")])

    day_list = [(d0 + timedelta(days=i)).strftime(OD_DAY_FMT)
                for i in range(n)]
    day_index = {d: i for i, d in enumerate(day_list)}

    # 每卡逐运营日 [按次实扣合计（分）, 行程数]（仅范围内行程）
    per_card: dict[str, dict[int, list]] = {}
    trip_count = 0
    for b in bills:
        di = day_index.get(b["operating_day"])
        if di is None:
            continue  # 范围外行程不参与回测
        slot = per_card.setdefault(b["card"], {}).setdefault(di, [0, 0])
        slot[0] += _cents_of(b["charged_fare"])
        slot[1] += 1
        trip_count += 1

    pass_tuple = [(p.name, _cents_of(p.price), p.valid_days) for p in passes]

    card_rows: list[dict] = []
    base_all = 0
    pass_cost_all = 0
    uncovered_all = 0
    for card in sorted(per_card):
        day_costs = [0] * n
        day_trips = [0] * n
        for di, (cents, cnt) in per_card[card].items():
            day_costs[di] = cents
            day_trips[di] = cnt
        baseline = sum(day_costs)
        best, plan = _optimal_pass_plan(day_costs, pass_tuple)

        purchases: list[dict] = []
        pass_cost = 0
        covered = [False] * n
        for di, pi in plan:
            name, price, days = pass_tuple[pi]
            j = min(di + days, n)
            for x in range(di, j):
                covered[x] = True
            pass_cost += price
            purchases.append({
                "day": day_list[di],
                "name": name,
                "price": _yuan_of(price),
                "valid_days": days,
                "cover_start": day_list[di],
                "cover_end": day_list[j - 1],
                "covered_days": j - di,
                "covered_trip_count": sum(day_trips[di:j]),
                "covered_baseline": _yuan_of(sum(day_costs[di:j])),
            })
        uncovered = sum(day_costs[x] for x in range(n) if not covered[x])
        # DP 不变式：best == 通票费合计 + 未覆盖运营日按次合计
        card_rows.append({
            "card": card,
            "trip_count": sum(day_trips),
            "baseline_total": _yuan_of(baseline),
            "pass_cost": _yuan_of(pass_cost),
            "uncovered_total": _yuan_of(uncovered),
            "pass_total": _yuan_of(best),
            "saving": _yuan_of(baseline - best),
            "purchase_count": len(purchases),
            "purchases": purchases,
        })
        base_all += baseline
        pass_cost_all += pass_cost
        uncovered_all += uncovered

    return PassBacktest(
        ok=True,
        start_day=start_text,
        end_day=end_text,
        day_count=n,
        trip_count=trip_count,
        card_count=len(card_rows),
        baseline_total=_yuan_of(base_all),
        pass_cost_total=_yuan_of(pass_cost_all),
        uncovered_total=_yuan_of(uncovered_all),
        pass_total=_yuan_of(pass_cost_all + uncovered_all),
        saving_total=_yuan_of(base_all - pass_cost_all - uncovered_all),
        passes=[{"name": p.name, "price": _round2(p.price),
                 "valid_days": p.valid_days} for p in passes],
        cards=card_rows,
    )


# ---------------------------------------------------------------------------
# 账单争议更正预演（只读：不写回记录、配置或账单）
# ---------------------------------------------------------------------------

# 一次预演允许更正的记录笔数（按原始行号选择，须为同一张卡）
DISPUTE_MIN_EDITS = 1
DISPUTE_MAX_EDITS = 2

# 行程对比输出中携带的账单字段（当前 / 更正两侧同一视图）
_DISPUTE_BILL_KEYS = (
    "operating_day", "entry_time", "exit_time",
    "entry_station", "exit_station", "from_zone", "to_zone",
    "base_fare", "discount", "discount_source",
    "charged_fare", "daily_cumulative", "daily_cap",
    "entry_line", "exit_line",
)


@dataclass
class DisputePreview:
    """账单争议更正预演结果。

    ok=False 时 errors 为定位错误（DISPUTE_* / NO_BILLS，或更正后 verify()
    的原有定位错误），不产出任何差额。ok=True 时 trips 以（进站原始行号,
    出站原始行号）对标识，status 为 added / removed / changed / same；
    summary 汇总该卡实扣合计差额（delta>0 为应补收，delta<0 为应退款）。
    """
    ok: bool
    card: str = ""
    edits: list[dict] = field(default_factory=list)
    last_verified_at: Optional[str] = None
    trips: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


def _dispute_bill_view(bill: Optional[dict]) -> Optional[dict]:
    """账单行的对比视图（两侧字段一致；缺失侧为 None）。"""
    if bill is None:
        return None
    return {k: bill[k] for k in _DISPUTE_BILL_KEYS}


def _dispute_changed_fields(cur: dict, new: dict) -> list[str]:
    """同一行程（行号对相同）两侧账单的差异字段列表（供页面高亮）。"""
    fields: list[str] = []
    if cur["entry_time"] != new["entry_time"] \
            or cur["exit_time"] != new["exit_time"]:
        fields.append("time")
    if cur["entry_station"] != new["entry_station"] \
            or cur["exit_station"] != new["exit_station"]:
        fields.append("station")
    if cur["operating_day"] != new["operating_day"]:
        fields.append("operating_day")
    if cur["base_fare"] != new["base_fare"]:
        fields.append("base_fare")
    if cur["discount"] != new["discount"]:
        fields.append("discount")
    if cur["charged_fare"] != new["charged_fare"]:
        fields.append("charged_fare")
    if cur["daily_cumulative"] != new["daily_cumulative"]:
        fields.append("daily_cumulative")
    return fields


def dispute_preview(stations: list[Station],
                    fares: list[Fare],
                    records: list[Swipe],
                    bills: list[dict],
                    cfg: Config,
                    raw_edits,
                    last_verified_at: Optional[str]) -> DisputePreview:
    """账单争议更正预演（全程只读：不写回记录、配置或账单）。

    - 用户按**原始行号**选择同一卡的 1～2 笔已保存记录，仅可临时修改
      **时刻**或**站点**（每项留空表示不修改）；
    - 后端在内存中**原子替换**（新建修正记录对象，原对象不被改动），对该卡
      全部记录按时刻**重新排序**后，连同其他卡记录一起复用 verify()
      重新配对与计价；
    - 更正后若出现配对、未知站点或缺票价等错误，verify() 的**原有定位
      错误**原样返回，不产出任何差额；
    - 成功时以**（进站原始行号, 出站原始行号）对**作为行程标识，对比
      当前账单（最近一次核验写入的账单）与更正方案：新增、移除、
      变化（时刻 / 站点 / 运营日 / 基础票价 / 优惠 / 实扣 / 当日累计）
      与不变逐行程列出，并汇总该卡的**补收**（差额为正）或**退款**
      （差额为负）金额；
    - 跨卡选择（DISPUTE_CROSS_CARD）、重复行号（DISPUTE_LINE_DUPLICATE）、
      行号不存在（DISPUTE_LINE_UNKNOWN）、未给出修改值
      （DISPUTE_BAD_FIELDS）、时刻值非法（DISPUTE_BAD_TIME）、更正笔数
      非法（DISPUTE_EDIT_COUNT）返回明确错误；无有效账单时返回
      NO_BILLS（与通票回测同口径）。
    """
    if not isinstance(raw_edits, (list, tuple)) \
            or not (DISPUTE_MIN_EDITS <= len(raw_edits) <= DISPUTE_MAX_EDITS):
        n = len(raw_edits) if isinstance(raw_edits, (list, tuple)) else 0
        return DisputePreview(ok=False, errors=[_err(
            "DISPUTE_EDIT_COUNT",
            f"更正预演需按原始行号选择 {DISPUTE_MIN_EDITS}～"
            f"{DISPUTE_MAX_EDITS} 笔同一卡的记录，实际 {n} 笔")])

    errors: list[dict] = []
    if not last_verified_at:
        errors.append(_err(
            "NO_BILLS",
            "尚未生成有效账单：请先在「7. 核验结果」成功运行核验，"
            "更正预演以最近一次核验生成的账单为当前账单"))

    line_of = {r.line: r for r in records}
    seen_lines: set[int] = set()
    parsed: list[dict] = []
    for e in raw_edits:
        raw_line = e.get("line")
        if not _is_int_value(raw_line) or int(raw_line) < 1:
            errors.append(_err(
                "DISPUTE_LINE_UNKNOWN",
                f"原始行号必须是不小于 1 的整数：{raw_line!r}"))
            continue
        line = int(raw_line)
        if line in seen_lines:
            errors.append(_err(
                "DISPUTE_LINE_DUPLICATE",
                f"重复选择同一原始行号：第 {line} 行", line=line))
            continue
        seen_lines.add(line)
        rec = line_of.get(line)
        if rec is None:
            errors.append(_err(
                "DISPUTE_LINE_UNKNOWN",
                f"已保存刷卡记录中不存在第 {line} 行"
                "（请对照「5. 刷卡记录」的来源行号）", line=line))
            continue
        raw_time = e.get("time")
        raw_station = e.get("station")
        has_time = raw_time is not None and str(raw_time).strip() != ""
        has_station = raw_station is not None and str(raw_station).strip() != ""
        if not has_time and not has_station:
            errors.append(_err(
                "DISPUTE_BAD_FIELDS",
                f"第 {line} 行的更正未给出任何修改值："
                "新时刻与新站点至少填写一项",
                line=line, card=rec.card, time=rec.time_text,
                station=rec.station))
            continue
        new_t: Optional[datetime] = None
        if has_time:
            try:
                new_t = parse_time(str(raw_time).strip())
            except ValueError as ex:
                errors.append(_err(
                    "DISPUTE_BAD_TIME", str(ex),
                    line=line, card=rec.card, station=rec.station))
                continue
        parsed.append({
            "line": line, "record": rec,
            "time": new_t,
            "station": str(raw_station).strip() if has_station else None,
        })

    cards = {p["record"].card for p in parsed}
    if len(cards) > 1:
        errors.append(_err(
            "DISPUTE_CROSS_CARD",
            "更正预演只能选择同一张卡的记录，实际涉及卡号："
            + "、".join(sorted(cards))))

    if errors:
        return DisputePreview(ok=False, errors=errors)

    card = parsed[0]["record"].card
    edit_by_line = {p["line"]: p for p in parsed}

    # 内存中的原子替换：为该卡生成修正后的全新记录列表（原对象不被改动），
    # 随后对该卡全部记录按时刻重新排序（同时刻按行号稳定，重复刷卡由
    # verify() 按原有 DUPLICATE_SWIPE 定位）。
    corrected_card: list[Swipe] = []
    applied: list[dict] = []
    for r in records:
        if r.card != card:
            continue
        p = edit_by_line.get(r.line)
        if p is None:
            corrected_card.append(r)
            continue
        new_t = p["time"] if p["time"] is not None else r.t
        new_station = p["station"] if p["station"] is not None else r.station
        corrected_card.append(Swipe(card=r.card, t=new_t, station=new_station,
                                    action=r.action, line=r.line))
        parts: list[str] = []
        if p["time"] is not None:
            parts.append(f"时刻 {r.time_text} → "
                         f"{new_t.strftime('%Y-%m-%d %H:%M:%S')}")
        if p["station"] is not None:
            parts.append(f"站点 {r.station} → {new_station}")
        applied.append({
            "line": r.line,
            "card": r.card,
            "action": r.action,
            "direction": ACTION_LABELS.get(r.action, r.action),
            "old_time": r.time_text,
            "new_time": new_t.strftime("%Y-%m-%d %H:%M:%S")
                        if p["time"] is not None else None,
            "old_station": r.station,
            "new_station": p["station"],
            "detail": f"第 {r.line} 行（{r.card} {r.time_text} @ {r.station} "
                      f"{ACTION_LABELS.get(r.action, r.action)}）："
                      + "；".join(parts),
        })
    corrected_card.sort(key=lambda r: (r.t, r.line))
    applied.sort(key=lambda a: a["line"])

    # 其他卡记录保持原样；全量复用 verify() 完成重新配对与计价
    corrected_records = [r for r in records if r.card != card] + corrected_card
    result = verify(stations, fares, corrected_records, cfg)
    if not result.ok:
        # 更正后出现的配对 / 未知站点 / 缺票价等错误：原有定位错误原样返回，
        # 不产出任何差额
        return DisputePreview(ok=False, card=card, edits=applied,
                              errors=result.errors)

    # 以（进站原始行号, 出站原始行号）对为行程标识，对比当前账单与更正方案
    current_bills = [b for b in bills if b["card"] == card]
    corrected_bills = [b for b in result.bills if b["card"] == card]
    cur_map = {(b["entry_line"], b["exit_line"]): b for b in current_bills}
    new_map = {(b["entry_line"], b["exit_line"]): b for b in corrected_bills}

    trips: list[dict] = []
    added = removed = changed = same = 0
    for key in sorted(set(cur_map) | set(new_map)):
        cb = cur_map.get(key)
        nb = new_map.get(key)
        day_changed: Optional[bool] = None
        fields: list[str] = []
        if cb is None:
            status = "added"
            added += 1
            charge_diff = _round2(nb["charged_fare"])
            discount_diff = _round2(nb["discount"])
        elif nb is None:
            status = "removed"
            removed += 1
            charge_diff = _round2(-cb["charged_fare"])
            discount_diff = _round2(-cb["discount"])
        else:
            fields = _dispute_changed_fields(cb, nb)
            day_changed = cb["operating_day"] != nb["operating_day"]
            charge_diff = _round2(nb["charged_fare"] - cb["charged_fare"])
            discount_diff = _round2(nb["discount"] - cb["discount"])
            if fields:
                status = "changed"
                changed += 1
            else:
                status = "same"
                same += 1
        trips.append({
            "entry_line": key[0],
            "exit_line": key[1],
            "trip_key": f"{key[0]}→{key[1]}",
            "status": status,
            "current": _dispute_bill_view(cb),
            "corrected": _dispute_bill_view(nb),
            "charge_diff": charge_diff,
            "discount_diff": discount_diff,
            "day_changed": day_changed,
            "changed_fields": fields,
        })

    cur_total = _round2(sum(b["charged_fare"] for b in current_bills))
    new_total = _round2(sum(b["charged_fare"] for b in corrected_bills))
    cur_disc = _round2(sum(b["discount"] for b in current_bills))
    new_disc = _round2(sum(b["discount"] for b in corrected_bills))
    delta = _round2(new_total - cur_total)
    summary = {
        "current_total": cur_total,
        "corrected_total": new_total,
        "delta": delta,
        "kind": "surcharge" if delta > 0
                else "refund" if delta < 0 else "none",
        "current_discount_total": cur_disc,
        "corrected_discount_total": new_disc,
        "discount_delta": _round2(new_disc - cur_disc),
        "trip_count_current": len(current_bills),
        "trip_count_corrected": len(corrected_bills),
        "added_count": added,
        "removed_count": removed,
        "changed_count": changed,
        "same_count": same,
    }
    return DisputePreview(
        ok=True, card=card, edits=applied,
        last_verified_at=last_verified_at,
        trips=trips, summary=summary)


# ---------------------------------------------------------------------------
# 服务中断补偿预览（只读：基于最近一次核验账单，不改记录与账单）
# ---------------------------------------------------------------------------

COMP_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class CompensationPreview:
    """服务中断补偿预览结果。

    ok=False 时 errors 为定位错误（COMP_BAD_STATION / CONFIG_BAD，
    location.param 指明出错参数），不产出任何分配结果。
    ok=True 时 cards 为按卡号排序的逐卡补偿（受影响行程数、实扣额、
    补偿额、封顶标记），unallocated 为预算用尽或全部封顶后仍未分配的
    金额（元）；无匹配账单时 cards 为空、unallocated 等于预算全额。
    金额在计算层全程为整数分，输出时换算为元（两位小数）。
    """
    ok: bool
    station: str = ""
    start_time: str = ""
    end_time: str = ""
    budget: float = 0.0
    per_card_cap: float = 0.0
    last_verified_at: Optional[str] = None
    trip_count: int = 0
    card_count: int = 0
    charged_total: float = 0.0
    compensated_total: float = 0.0
    uncompensated_total: float = 0.0
    unallocated: float = 0.0
    round_count: int = 0
    cards: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _parse_comp_time(value, param: str) -> tuple[Optional[datetime], Optional[dict]]:
    """解析起止时刻参数（复用 parse_time 的格式集）；失败返回 CONFIG_BAD。"""
    text = str(value).strip()
    try:
        return parse_time(text), None
    except ValueError as ex:
        return None, _err("CONFIG_BAD", str(ex), param=param)


def _comp_amount(value, label: str, param: str) -> tuple[int, Optional[dict]]:
    """金额参数（元）-> 整数分；非法时返回 CONFIG_BAD 定位错误。"""
    if not _is_finite_number(value) or float(value) < 0:
        return 0, _err(
            "CONFIG_BAD",
            f"{label}必须是不小于 0 的有限数字（元）：{value!r}", param=param)
    return _cents_of(float(value)), None


def compensation_preview(bills: list[dict],
                         station,
                         start_time,
                         end_time,
                         budget,
                         per_card_cap,
                         last_verified_at: Optional[str]
                         ) -> CompensationPreview:
    """服务中断补偿预览（全程只读：只读取最近一次核验生成的账单）。

    - 从已保存账单中选出「进站或出站发生在指定站点、且对应刷卡时刻落在
      闭区间 [start_time, end_time] 内」的行程，按卡汇总实扣额；
      同一行程进出站均匹配时只计一次；
    - 预算与金额均按整数分计算：先以各卡实扣额（分）为权重按比例分配，
      每轮按最大余数法把该轮份额补齐为整数分（余数相同按卡号字典序），
      再将超过 min(单卡上限, 该卡实扣额) 的份额封顶，溢出份额进入下一轮
      向未封顶卡重新分配，直到预算用尽或全部卡封顶；
    - 无匹配账单时返回空结果（cards 为空、未分配金额等于预算全额）；
      站点为空、区间非法（无法解析或起始晚于结束）、金额非法（负数 /
      非有限数字）返回定位错误（location.param 指明出错参数）。
    """
    errors: list[dict] = []
    station_text = str(station).strip() if station is not None else ""
    if not station_text:
        errors.append(_err("COMP_BAD_STATION", "站点不能为空", param="station"))
    start_t, e1 = _parse_comp_time(start_time, "start_time")
    end_t, e2 = _parse_comp_time(end_time, "end_time")
    if e1 is not None:
        errors.append(e1)
    if e2 is not None:
        errors.append(e2)
    if start_t is not None and end_t is not None and start_t > end_t:
        errors.append(_err(
            "CONFIG_BAD",
            f"起始时刻不能晚于结束时刻：{start_t.strftime(COMP_TIME_FMT)} ～ "
            f"{end_t.strftime(COMP_TIME_FMT)}", param="start_time"))
    budget_cents, e3 = _comp_amount(budget, "补偿预算", "budget")
    if e3 is not None:
        errors.append(e3)
    cap_cents, e4 = _comp_amount(per_card_cap, "单卡上限", "per_card_cap")
    if e4 is not None:
        errors.append(e4)
    if errors:
        return CompensationPreview(ok=False, errors=errors)

    # 1) 选出受影响行程：进站或出站发生在该站，且对应刷卡时刻落在闭区间内
    per_card: dict[str, dict] = {}
    trip_count = 0
    for b in bills:
        matched = (b["entry_station"] == station_text
                   and start_t <= datetime.strptime(b["entry_time"], COMP_TIME_FMT)
                   <= end_t)
        if not matched:
            matched = (b["exit_station"] == station_text
                       and start_t <= datetime.strptime(b["exit_time"], COMP_TIME_FMT)
                       <= end_t)
        if not matched:
            continue
        slot = per_card.setdefault(b["card"], {"trips": 0, "charged": 0})
        slot["trips"] += 1
        slot["charged"] += _cents_of(b["charged_fare"])
        trip_count += 1

    charged_total = sum(s["charged"] for s in per_card.values())

    # 2) 权重分配：每轮按实扣额（分）为权重比例分配，最大余数法补齐整数分，
    #    超过 min(单卡上限, 该卡实扣额) 的份额封顶，溢出进入下一轮重分。
    cards = sorted(per_card)
    comp = {c: 0 for c in cards}
    # 每卡有效上限：不超过该卡实扣额（补偿不会超过其实际支出）
    eff_cap = {c: min(cap_cents, per_card[c]["charged"]) for c in cards}
    active = {c for c in cards if per_card[c]["charged"] > 0}
    remaining = budget_cents
    rounds = 0
    while remaining > 0 and active:
        weight_total = sum(per_card[c]["charged"] for c in active)
        # 本轮整数份额：floor(remaining * w / W)，余数从大到小逐卡补 1 分，
        # 余数相同按卡号字典序；补齐总额恰为本轮 remaining（最大余数法）。
        quota: list[tuple[int, str, int]] = []
        for c in active:
            w = per_card[c]["charged"]
            exact = remaining * w
            quota.append((-(exact % weight_total), c, exact // weight_total))
        quota.sort()
        apportion = {c: base for _rem, c, base in quota}
        leftover = remaining - sum(apportion.values())
        for _rem, c, _base in quota[:leftover]:
            apportion[c] += 1
        # 封顶：超过有效上限的份额截断，溢出累计到下一轮重新分配
        excess = 0
        for c in list(active):
            take = apportion[c]
            if comp[c] + take > eff_cap[c]:
                take = eff_cap[c] - comp[c]
                active.discard(c)
            comp[c] += take
            excess += apportion[c] - take
        remaining = excess
        rounds += 1

    compensated_total = sum(comp.values())
    card_rows: list[dict] = []
    for c in cards:
        charged = per_card[c]["charged"]
        card_rows.append({
            "card": c,
            "trip_count": per_card[c]["trips"],
            "charged_total": _yuan_of(charged),
            "compensation": _yuan_of(comp[c]),
            "uncompensated": _yuan_of(charged - comp[c]),
            "effective_cap": _yuan_of(eff_cap[c]),
            "capped": comp[c] >= eff_cap[c] and eff_cap[c] > 0,
            "cap_reason": ("cap" if cap_cents <= charged else "charged")
                          if eff_cap[c] > 0 and comp[c] >= eff_cap[c]
                          else None,
        })

    return CompensationPreview(
        ok=True,
        station=station_text,
        start_time=start_t.strftime(COMP_TIME_FMT),
        end_time=end_t.strftime(COMP_TIME_FMT),
        budget=_yuan_of(budget_cents),
        per_card_cap=_yuan_of(cap_cents),
        last_verified_at=last_verified_at,
        trip_count=trip_count,
        card_count=len(card_rows),
        charged_total=_yuan_of(charged_total),
        compensated_total=_yuan_of(compensated_total),
        uncompensated_total=_yuan_of(charged_total - compensated_total),
        unallocated=_yuan_of(budget_cents - compensated_total),
        round_count=rounds,
        cards=card_rows,
    )
