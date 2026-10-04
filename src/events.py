"""康复下转责任交接领域事件定义。

事件类型与聚合类型的权威枚举与 ``contracts/domain.schema.json`` 保持一致；
``tests/test_contract.py`` 会校验两份清单不得漂移。

系统只记录事实，不生成或修改治疗方案：计划与用药内容均由医生签署的
PLAN_SIGNED / PLAN_REVISED 携带，后端原样保存、按版本分发。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .validator import validate_event

# ---- 聚合类型 -------------------------------------------------------------

RECOVERY_PLAN = "recovery_plan"
HANDOFF_OFFER = "handoff_offer"
CARE_RESPONSIBILITY = "care_responsibility"
FOLLOWUP_RESULT = "followup_result"
REFERRAL_CASE = "referral_case"

AGGREGATE_TYPES = (
    RECOVERY_PLAN,
    HANDOFF_OFFER,
    CARE_RESPONSIBILITY,
    FOLLOWUP_RESULT,
    REFERRAL_CASE,
)

# ---- 事件类型 -------------------------------------------------------------

PLAN_SIGNED = "PLAN_SIGNED"
PLAN_REVISED = "PLAN_REVISED"
PLAN_VERSION_DELIVERED = "PLAN_VERSION_DELIVERED"
PLAN_VERSION_ACKNOWLEDGED = "PLAN_VERSION_ACKNOWLEDGED"

HANDOFF_OFFERED = "HANDOFF_OFFERED"
HANDOFF_DECLINED = "HANDOFF_DECLINED"
HANDOFF_WITHDRAWN = "HANDOFF_WITHDRAWN"

RESPONSIBILITY_ACCEPTED = "RESPONSIBILITY_ACCEPTED"
ARRIVAL_CONFIRMED = "ARRIVAL_CONFIRMED"
PATIENT_NO_SHOW = "PATIENT_NO_SHOW"
TRANSFER_BLOCKED = "TRANSFER_BLOCKED"
ARRIVAL_RESCHEDULED = "ARRIVAL_RESCHEDULED"
MEDICATION_UNAVAILABLE = "MEDICATION_UNAVAILABLE"

FOLLOWUP_SCHEDULED = "FOLLOWUP_SCHEDULED"
FOLLOWUP_RECORDED = "FOLLOWUP_RECORDED"
FOLLOWUP_OVERDUE = "FOLLOWUP_OVERDUE"

CARE_REESCALATED = "CARE_REESCALATED"
REESCALATION_RECEIVED = "REESCALATION_RECEIVED"

CASE_OPENED = "CASE_OPENED"
CASE_CLOSED = "CASE_CLOSED"

EVENT_TYPES = (
    PLAN_SIGNED,
    PLAN_REVISED,
    PLAN_VERSION_DELIVERED,
    PLAN_VERSION_ACKNOWLEDGED,
    HANDOFF_OFFERED,
    HANDOFF_DECLINED,
    HANDOFF_WITHDRAWN,
    RESPONSIBILITY_ACCEPTED,
    ARRIVAL_CONFIRMED,
    PATIENT_NO_SHOW,
    TRANSFER_BLOCKED,
    ARRIVAL_RESCHEDULED,
    MEDICATION_UNAVAILABLE,
    FOLLOWUP_SCHEDULED,
    FOLLOWUP_RECORDED,
    FOLLOWUP_OVERDUE,
    CARE_REESCALATED,
    REESCALATION_RECEIVED,
    CASE_OPENED,
    CASE_CLOSED,
)

# ---- 稳定枚举 -------------------------------------------------------------

# 接收机构能力项：邀约前可核对，接受责任时必须覆盖计划需要的能力。
CAPABILITY_MEDICATION = "medication"          # 康复用药供给
CAPABILITY_REHAB = "rehab_service"            # 康复服务
CAPABILITY_REVIEW_ITEM = "review_item"        # 单项复查能力（以项目编码声明）
CAPABILITY_EMERGENCY = "emergency_stabilize"  # 应急稳定处置

# 异常分类：四类问题走不同处置路径。
EXCEPTION_NO_SHOW = "patient_no_show"         # 患者未到
EXCEPTION_ROAD_BLOCKED = "road_blocked"       # 道路阻断
EXCEPTION_MED_UNAVAILABLE = "medication_unavailable"  # 药品不可得
EXCEPTION_SYMPTOM_WORSENED = "symptom_worsened"      # 症状恶化

EXCEPTION_TYPES = (
    EXCEPTION_NO_SHOW,
    EXCEPTION_ROAD_BLOCKED,
    EXCEPTION_MED_UNAVAILABLE,
    EXCEPTION_SYMPTOM_WORSENED,
)

# 随访结果分级。
FOLLOWUP_STABLE = "stable"
FOLLOWUP_ABNORMAL = "abnormal"                 # 单项异常：按计划阈值处理
FOLLOWUP_WORSENED = "symptom_worsened"         # 症状恶化：重新上转路径

# 责任状态（投影用，非事件）。
RESP_BACKSTOP = "backstop"        # 原机构兜底（尚未交接成功）
RESP_OFFERED = "offered"          # 已邀约，基层未接受
RESP_ACCEPTED = "accepted"        # 基层已接受具体责任
RESP_ARRIVED = "patient_arrived"  # 患者已到达
RESP_REESCALATED = "reescalated"  # 已重新上转
RESP_CLOSED = "closed"            # 结案

# 交接邀约状态。
OFFER_OPEN = "open"
OFFER_DECLINED = "declined"
OFFER_WITHDRAWN = "withdrawn"
OFFER_ACCEPTED = "accepted"


@dataclass(frozen=True)
class Event:
    """领域事件信封，字段命名沿用仓库既有标识规范。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None
    request_id: str | None = None
    seq: int = 0  # 存储层分配的全局因果序号；0 表示尚未入库

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
        }
        if self.case_id is not None:
            record["case_id"] = self.case_id
        if self.request_id is not None:
            record["request_id"] = self.request_id
        if self.seq:
            record["seq"] = self.seq
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Event":
        occurred_at = record["occurred_at"]
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)
        return cls(
            event_id=record["event_id"],
            event_type=record["event_type"],
            aggregate_type=record["aggregate_type"],
            aggregate_id=record["aggregate_id"],
            occurred_at=occurred_at,
            version=record["version"],
            summary=record["summary"],
            payload=dict(record.get("payload") or {}),
            case_id=record.get("case_id"),
            request_id=record.get("request_id"),
            seq=int(record.get("seq") or 0),
        )


def validate_envelope(event: Event) -> list[str]:
    """信封级校验：基础字段 + 类型枚举。返回错误信息列表（空列表为通过）。"""
    errors = validate_event(event.to_dict())
    if event.event_type not in EVENT_TYPES:
        errors.append(f"未知事件类型：{event.event_type}")
    if event.aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{event.aggregate_type}")
    return errors
