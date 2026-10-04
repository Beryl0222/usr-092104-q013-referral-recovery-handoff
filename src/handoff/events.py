"""事件目录与信封校验。

事件接入遵循仓库已有标识规范：event_id、event_type、aggregate_type、
aggregate_id、occurred_at、version、summary 七要素缺一不可，version 为
同一聚合内从 1 开始递增的事件序号。已注册事件保持兼容，仅做新增。
"""

from __future__ import annotations

from datetime import datetime

from src.validator import validate_event

# 已注册事件（保持兼容，不得改名或删除）
PLAN_SIGNED = "PLAN_SIGNED"
HANDOFF_OFFERED = "HANDOFF_OFFERED"
RESPONSIBILITY_ACCEPTED = "RESPONSIBILITY_ACCEPTED"
ARRIVAL_CONFIRMED = "ARRIVAL_CONFIRMED"
CARE_REESCALATED = "CARE_REESCALATED"

# 本服务新增事件
CASE_OPENED = "CASE_OPENED"
PLAN_REVISED = "PLAN_REVISED"
HANDOFF_DECLINED = "HANDOFF_DECLINED"
FOLLOWUP_RECORDED = "FOLLOWUP_RECORDED"
DISRUPTION_REPORTED = "DISRUPTION_REPORTED"
DISRUPTION_RESOLVED = "DISRUPTION_RESOLVED"
NOTICE_DELIVERED = "NOTICE_DELIVERED"
CASE_CLOSED = "CASE_CLOSED"

EVENT_TYPES = (
    PLAN_SIGNED,
    HANDOFF_OFFERED,
    RESPONSIBILITY_ACCEPTED,
    ARRIVAL_CONFIRMED,
    CARE_REESCALATED,
    CASE_OPENED,
    PLAN_REVISED,
    HANDOFF_DECLINED,
    FOLLOWUP_RECORDED,
    DISRUPTION_REPORTED,
    DISRUPTION_RESOLVED,
    NOTICE_DELIVERED,
    CASE_CLOSED,
)

# 聚合类型：既有四类 + 交接案例级聚合 handoff_case。
# handoff_case 的 aggregate_id 与对应 recovery_plan 相同，一个下转 episode
# 由计划聚合锚定，案例级事件（异常、通知送达、关闭）挂在 handoff_case 上。
AGG_RECOVERY_PLAN = "recovery_plan"
AGG_HANDOFF_OFFER = "handoff_offer"
AGG_CARE_RESPONSIBILITY = "care_responsibility"
AGG_FOLLOWUP_RESULT = "followup_result"
AGG_HANDOFF_CASE = "handoff_case"

AGGREGATE_TYPES = (
    AGG_RECOVERY_PLAN,
    AGG_HANDOFF_OFFER,
    AGG_CARE_RESPONSIBILITY,
    AGG_FOLLOWUP_RESULT,
    AGG_HANDOFF_CASE,
)

# 事件类型允许挂载的聚合类型
EVENT_AGGREGATE = {
    PLAN_SIGNED: AGG_RECOVERY_PLAN,
    PLAN_REVISED: AGG_RECOVERY_PLAN,
    HANDOFF_OFFERED: AGG_HANDOFF_OFFER,
    HANDOFF_DECLINED: AGG_HANDOFF_OFFER,
    RESPONSIBILITY_ACCEPTED: AGG_CARE_RESPONSIBILITY,
    ARRIVAL_CONFIRMED: AGG_CARE_RESPONSIBILITY,
    CARE_REESCALATED: AGG_CARE_RESPONSIBILITY,
    FOLLOWUP_RECORDED: AGG_FOLLOWUP_RESULT,
    CASE_OPENED: AGG_HANDOFF_CASE,
    DISRUPTION_REPORTED: AGG_HANDOFF_CASE,
    DISRUPTION_RESOLVED: AGG_HANDOFF_CASE,
    NOTICE_DELIVERED: AGG_HANDOFF_CASE,
    CASE_CLOSED: AGG_HANDOFF_CASE,
}


def parse_time(value: str) -> datetime:
    """把契约中的 date-time 字符串解析为带时区的时间。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"occurred_at 不是合法的 date-time：{value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"occurred_at 必须携带时区：{value!r}")
    return parsed


def validate_envelope(record: dict) -> list[str]:
    """在基础字段校验之上，校验事件目录、聚合映射与时间格式。"""
    errors = validate_event(record)
    if errors:
        return errors
    event_type = record["event_type"]
    aggregate_type = record["aggregate_type"]
    if event_type not in EVENT_TYPES:
        errors.append(f"未登记的事件类型：{event_type}")
    elif aggregate_type != EVENT_AGGREGATE[event_type]:
        errors.append(
            f"事件 {event_type} 应挂在聚合 {EVENT_AGGREGATE[event_type]}，"
            f"而不是 {aggregate_type}"
        )
    if aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未登记的聚合类型：{aggregate_type}")
    try:
        parse_time(record["occurred_at"])
    except ValueError as exc:
        errors.append(str(exc))
    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        errors.append("payload 必须是对象")
    return errors
