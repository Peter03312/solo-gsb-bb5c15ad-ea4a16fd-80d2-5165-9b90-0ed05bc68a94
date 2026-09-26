"""API 请求模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TextIn(BaseModel):
    text: str = Field(default="", description="批量录入文本")


class ConfigIn(BaseModel):
    transfer_minutes: int = Field(gt=0, description="换乘有效间隔（分钟）")
    transfer_discount: float = Field(ge=0, description="换乘抵扣额（元）")
    daily_cap: float = Field(ge=0, description="每卡每运营日封顶额（元）")
    day_split_hour: int = Field(ge=0, le=23, description="运营日分界时刻")


class TrialConfigIn(BaseModel):
    """规则试算对比的草案参数。

    不在此处做数值约束：非法草案应走 core.validate_config，
    返回与正式核验一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    transfer_minutes: float = Field(description="草案：换乘有效间隔（分钟）")
    transfer_discount: float = Field(description="草案：换乘抵扣额（元）")
    daily_cap: float = Field(description="草案：每卡每运营日封顶额（元）")
    day_split_hour: float = Field(description="草案：运营日分界时刻（0-23）")


class PeakAuditIn(BaseModel):
    """站点闸机峰值审计参数。

    不在此处做数值约束：非法参数应走核心层校验，
    返回与其他接口一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    window_minutes: float = Field(
        description="峰值统计窗口长度（分钟），取 1～120 的整数")
    min_alert: float = Field(
        default=1,
        description="最低告警数；峰值刷卡数达到该值的组才标记告警")


class MergePreviewIn(BaseModel):
    """外部刷卡批次合并预览参数。

    待导入记录文本与保存记录共用 parse_records_text 解析（解析错误按
    原有定位错误返回）；容差不在此处约束，非法值走核心层校验，
    返回与其他接口一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    text: str = Field(default="", description="待导入刷卡记录文本")
    tolerance_seconds: float = Field(
        description="重复判定时间容差（秒），取 0～120 的整数")


class OdPublishIn(BaseModel):
    """匿名客流 OD 发布参数。

    不在此处做数值约束：非法参数应走核心层校验，
    返回与其他接口一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    start_day: str = Field(description="运营日起始日期 YYYY-MM-DD（闭区间）")
    end_day: str = Field(description="运营日结束日期 YYYY-MM-DD（闭区间）")
    bucket_minutes: float = Field(
        description="初始时间桶粒度（分钟），取 15 / 30 / 60 之一")
    k: float = Field(
        description="匿名阈值：每个发布区间内的去重卡数下限，取 2～50 的整数")


class GateQueueIn(BaseModel):
    """闸机容量排队推演参数。

    不在此处做数值约束：非法参数应走核心层校验，
    返回与其他接口一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    operating_day: str = Field(description="运营日 YYYY-MM-DD")
    bucket_minutes: float = Field(
        description="时间桶粒度（分钟），取 1 / 5 / 15 之一")
    capacity: float = Field(
        description="每桶通行能力（人次），不小于 1 的整数")
    max_extra_buckets: float = Field(
        description="最后一笔到达之后最多续算的空桶数，不小于 0 的整数")


class PassBacktestIn(BaseModel):
    """通票购买策略回测参数。

    不在此处做数值约束：非法日期 / 产品应走核心层校验，
    返回与其他接口一致的定位错误（HTTP 422）。
    """
    passes_text: str = Field(
        default="",
        description="通票产品文本，每行：名称, 价格（元）, 连续有效运营日数")
    start_day: str = Field(description="运营日起始日期 YYYY-MM-DD（闭区间）")
    end_day: str = Field(description="运营日结束日期 YYYY-MM-DD（闭区间）")


class DisputeEditIn(BaseModel):
    """单笔账单争议更正：按原始行号选择记录，仅可临时修改时刻或站点。

    不在此处做数值约束：行号合法性、修改值合法性与同卡约束均走核心层
    校验，返回与其他接口一致的定位错误（HTTP 422）。
    """
    line: float = Field(description="已保存刷卡记录的原始行号（正整数）")
    time: Optional[str] = Field(
        default=None, description="新时刻（可选；留空表示不修改时刻）")
    station: Optional[str] = Field(
        default=None, description="新站点（可选；留空表示不修改站点）")


class DisputePreviewIn(BaseModel):
    """账单争议更正预演参数。

    一至两笔更正（同一卡的原始行号 + 新时刻 / 新站点）；笔数、同卡、
    重复行号与修改值合法性均由核心层校验，返回 DISPUTE_* 定位错误。
    """
    edits: list[DisputeEditIn] = Field(
        description="更正列表：1～2 笔同一卡记录的临时修改")


class CompensationPreviewIn(BaseModel):
    """服务中断补偿预览参数。

    不在此处做数值约束：站点、起止时刻与金额的合法性均走核心层校验，
    返回与其他接口一致的定位错误（HTTP 422，location.param 指明出错参数）。
    """
    station: str = Field(description="发生服务中断的站点名")
    start_time: str = Field(
        description="中断开始时刻（闭区间起点），如 2026-09-18 08:00:00")
    end_time: str = Field(
        description="中断结束时刻（闭区间终点），如 2026-09-18 09:00:00")
    budget: float = Field(description="补偿预算（元），不小于 0")
    per_card_cap: float = Field(description="单卡补偿上限（元），不小于 0")
