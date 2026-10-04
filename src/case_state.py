"""案例状态归约：把一个案例的事件流折叠为连续状态。

归约器是纯函数式投影，不做业务决策；业务规则在 ``service`` 层。
责任连续性的核心约定：

- 案例开启后，原机构（县级医院）始终是兜底责任人；
- 基层明确接受（RESPONSIBILITY_ACCEPTED，含具体责任范围与可服务日期）
  后，责任自可服务日期起转移；此前一切时间窗都由原机构兜底；
- 重新上转被原机构接收（REESCALATION_RECEIVED）后，责任回到原机构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .events import (
    ARRIVAL_CONFIRMED,
    ARRIVAL_RESCHEDULED,
    CARE_REESCALATED,
    CASE_CLOSED,
    CASE_OPENED,
    EXCEPTION_MED_UNAVAILABLE,
    EXCEPTION_NO_SHOW,
    EXCEPTION_ROAD_BLOCKED,
    EXCEPTION_SYMPTOM_WORSENED,
    FOLLOWUP_OVERDUE,
    FOLLOWUP_RECORDED,
    FOLLOWUP_SCHEDULED,
    FOLLOWUP_STABLE,
    HANDOFF_DECLINED,
    HANDOFF_OFFERED,
    HANDOFF_WITHDRAWN,
    MEDICATION_UNAVAILABLE,
    OFFER_ACCEPTED,
    OFFER_DECLINED,
    OFFER_OPEN,
    OFFER_WITHDRAWN,
    PATIENT_NO_SHOW,
    PLAN_REVISED,
    PLAN_SIGNED,
    PLAN_VERSION_ACKNOWLEDGED,
    PLAN_VERSION_DELIVERED,
    REESCALATION_RECEIVED,
    RESPONSIBILITY_ACCEPTED,
    RESP_ACCEPTED,
    RESP_ARRIVED,
    RESP_BACKSTOP,
    RESP_CLOSED,
    RESP_REESCALATED,
    TRANSFER_BLOCKED,
    Event,
)


@dataclass
class PlanVersion:
    version: int
    signed_by: str
    signed_at: datetime
    diagnosis_summary: str
    rehab_summary: str
    precautions: list[str]
    medications: list[dict[str, Any]]
    review_items: list[dict[str, Any]]
    required_capabilities: list[str]
    supersedes: int | None = None


@dataclass
class DeliveryRecord:
    plan_version: int
    holder: str
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    event_ref: str = ""


@dataclass
class Offer:
    offer_id: str
    target_org: str
    offered_at: datetime
    serviceable_from: datetime | None
    expected_arrival_at: datetime | None
    capabilities: list[str]
    plan_version: int
    status: str = OFFER_OPEN
    decline_reason: str | None = None
    decided_at: datetime | None = None


@dataclass
class Acceptance:
    version: int
    org: str
    contact_person: str
    contact_phone: str
    serviceable_from: datetime
    scope_capabilities: list[str]
    medication_codes: list[str]
    review_codes: list[str]
    accepted_at: datetime


@dataclass
class Incident:
    incident_id: str
    kind: str
    reported_at: datetime
    reported_by: str
    detail: str
    disposition: str | None = None
    disposition_at: datetime | None = None
    note: str = ""


@dataclass
class Followup:
    followup_id: str
    scheduled_at: datetime
    due_at: datetime
    responsible_org: str
    plan_version: int
    recorded_at: datetime | None = None
    result: str | None = None
    abnormal_items: list[dict[str, Any]] = field(default_factory=list)
    status: str = "scheduled"  # scheduled / recorded / overdue


@dataclass
class Reescalation:
    reescalation_id: str
    reason: str
    abnormal_item: str | None
    requested_at: datetime
    from_org: str
    to_org: str
    offer_id: str | None
    received_at: datetime | None = None
    status: str = "requested"  # requested / received


@dataclass
class CaseState:
    case_id: str = ""
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    patient_ref: str = ""
    patient_contact: str = ""
    origin_org: str = ""
    origin_contact: str = ""
    help_phone: str = ""
    plan_versions: dict[int, PlanVersion] = field(default_factory=dict)
    deliveries: list[DeliveryRecord] = field(default_factory=list)
    holders: set[str] = field(default_factory=set)
    offers: dict[str, Offer] = field(default_factory=dict)
    acceptances: list[Acceptance] = field(default_factory=list)
    arrival_status: str = "pending"  # pending / arrived / no_show / blocked
    arrived_at: datetime | None = None
    expected_arrival_at: datetime | None = None
    incidents: list[Incident] = field(default_factory=list)
    followups: dict[str, Followup] = field(default_factory=dict)
    reescalations: list[Reescalation] = field(default_factory=list)
    # 单项责任回退：药品不可得且约定由原机构保障时，项目 -> 责任方。
    item_responsibility: dict[str, str] = field(default_factory=dict)

    # ---- 计划视图 ---------------------------------------------------------

    @property
    def plan_version(self) -> int:
        return max(self.plan_versions) if self.plan_versions else 0

    @property
    def plan(self) -> PlanVersion | None:
        return self.plan_versions.get(self.plan_version) if self.plan_version else None

    def holders_of_stale_version(self) -> dict[str, int]:
        """仍持有旧版计划的人 -> 其确认过的最新版本。"""
        latest = self.plan_version
        stale: dict[str, int] = {}
        acknowledged: dict[str, int] = {}
        for d in self.deliveries:
            if d.acknowledged_at is not None:
                acknowledged[d.holder] = max(acknowledged.get(d.holder, 0), d.plan_version)
        for holder, ver in acknowledged.items():
            if ver < latest:
                stale[holder] = ver
        return stale

    def undelivered_or_unacked(self) -> list[DeliveryRecord]:
        """最新版本尚未送达或已送达未确认的送达记录。"""
        latest = self.plan_version
        return [
            d for d in self.deliveries
            if d.plan_version == latest and d.acknowledged_at is None
        ]

    # ---- 责任视图 ---------------------------------------------------------

    @property
    def latest_acceptance(self) -> Acceptance | None:
        return self.acceptances[-1] if self.acceptances else None

    def is_open_offer(self) -> Offer | None:
        for offer in reversed(list(self.offers.values())):
            if offer.status == OFFER_OPEN:
                return offer
        return None

    def responsibility_at(self, at: datetime) -> dict[str, Any]:
        """返回某时刻的责任状态与责任机构。任何时刻都有责任方，不允许空档。"""
        if self.closed_at is not None and at >= self.closed_at:
            return {"status": RESP_CLOSED, "org": None}
        for r in self.reescalations:
            if r.received_at is not None and at >= r.received_at:
                return {"status": RESP_REESCALATED, "org": r.to_org}
        accepted = None
        for acc in self.acceptances:
            if at >= acc.serviceable_from:
                accepted = acc
        if accepted is None:
            return {"status": RESP_BACKSTOP, "org": self.origin_org}
        if self.arrived_at is not None and at >= self.arrived_at:
            return {"status": RESP_ARRIVED, "org": accepted.org}
        return {"status": RESP_ACCEPTED, "org": accepted.org}


def fold_case(events: list[Event]) -> CaseState:
    """把案例事件流按存储因果序号（seq）归约为 CaseState。

    同刻发生的多个命令（如邀约后立即接受）也能保持正确顺序；
    seq 缺失（未落库的构造场景）时退化为时间戳排序。
    """
    state = CaseState()
    for e in sorted(
        events,
        key=lambda x: (x.seq or 10**18, x.occurred_at, x.aggregate_type, x.aggregate_id, x.version),
    ):
        _apply(state, e)
    return state


def _apply(state: CaseState, e: Event) -> None:
    p = e.payload
    t = e.event_type

    if t == CASE_OPENED:
        state.case_id = e.case_id or e.aggregate_id
        state.opened_at = e.occurred_at
        state.patient_ref = p.get("patient_ref", "")
        state.patient_contact = p.get("patient_contact", "")
        state.origin_org = p["origin_org"]
        state.origin_contact = p.get("origin_contact", "")
        state.help_phone = p.get("help_phone", "")

    elif t == PLAN_SIGNED or t == PLAN_REVISED:
        ver = int(p["plan_version"])
        version = PlanVersion(
            version=ver,
            signed_by=p["signed_by"],
            signed_at=e.occurred_at,
            diagnosis_summary=p.get("diagnosis_summary", ""),
            rehab_summary=p.get("rehab_summary", ""),
            precautions=list(p.get("precautions", [])),
            medications=list(p.get("medications", [])),
            review_items=list(p.get("review_items", [])),
            required_capabilities=list(p.get("required_capabilities", [])),
            supersedes=p.get("supersedes"),
        )
        state.plan_versions[ver] = version

    elif t == PLAN_VERSION_DELIVERED:
        state.deliveries.append(DeliveryRecord(
            plan_version=int(p["plan_version"]),
            holder=p["holder"],
            delivered_at=e.occurred_at,
            event_ref=e.event_id,
        ))

    elif t == PLAN_VERSION_ACKNOWLEDGED:
        holder = p["holder"]
        ver = int(p["plan_version"])
        state.holders.add(holder)
        for d in reversed(state.deliveries):
            if d.holder == holder and d.plan_version == ver and d.acknowledged_at is None:
                d.acknowledged_at = e.occurred_at
                break
        else:
            # 允许接收方直接确认（送达事件由外系统补记时也不丢确认）。
            state.deliveries.append(DeliveryRecord(
                plan_version=ver, holder=holder,
                delivered_at=None, acknowledged_at=e.occurred_at,
                event_ref=e.event_id,
            ))

    elif t == HANDOFF_OFFERED:
        offer = Offer(
            offer_id=e.aggregate_id,
            target_org=p["target_org"],
            offered_at=e.occurred_at,
            serviceable_from=_parse_dt(p.get("serviceable_from")),
            expected_arrival_at=_parse_dt(p.get("expected_arrival_at")),
            capabilities=list(p.get("capabilities", [])),
            plan_version=int(p.get("plan_version", state.plan_version)),
        )
        state.offers[offer.offer_id] = offer
        state.holders.add(offer.target_org)
        if offer.expected_arrival_at:
            state.expected_arrival_at = offer.expected_arrival_at

    elif t == HANDOFF_DECLINED:
        offer = state.offers.get(e.aggregate_id)
        if offer:
            offer.status = OFFER_DECLINED
            offer.decline_reason = p.get("reason")
            offer.decided_at = e.occurred_at

    elif t == HANDOFF_WITHDRAWN:
        offer = state.offers.get(e.aggregate_id)
        if offer:
            offer.status = OFFER_WITHDRAWN
            offer.decided_at = e.occurred_at

    elif t == RESPONSIBILITY_ACCEPTED:
        offer = None
        if p.get("offer_id"):
            offer = state.offers.get(p["offer_id"])
        acc = Acceptance(
            version=e.version,
            org=p["org"],
            contact_person=p["contact_person"],
            contact_phone=p["contact_phone"],
            serviceable_from=_parse_dt(p["serviceable_from"]),
            scope_capabilities=list(p.get("scope_capabilities", [])),
            medication_codes=list(p.get("medication_codes", [])),
            review_codes=list(p.get("review_codes", [])),
            accepted_at=e.occurred_at,
        )
        state.acceptances.append(acc)
        state.holders.add(acc.org)
        if offer:
            offer.status = OFFER_ACCEPTED
            offer.decided_at = e.occurred_at

    elif t == ARRIVAL_CONFIRMED:
        state.arrival_status = "arrived"
        state.arrived_at = e.occurred_at
        # 患者最终到达，此前未到/阻断类异常视为已了结。
        for inc in reversed(state.incidents):
            if inc.disposition is None and inc.kind in (EXCEPTION_NO_SHOW, EXCEPTION_ROAD_BLOCKED):
                inc.disposition = "arrived"
                inc.disposition_at = e.occurred_at

    elif t == ARRIVAL_RESCHEDULED:
        state.expected_arrival_at = _parse_dt(p["new_expected_arrival_at"])
        if state.arrival_status in ("no_show", "blocked"):
            state.arrival_status = "pending"
        # 改期就是“患者未到/道路阻断”的处置路径，留下处置时限。
        for inc in reversed(state.incidents):
            if inc.disposition is None and inc.kind in (EXCEPTION_NO_SHOW, EXCEPTION_ROAD_BLOCKED):
                inc.disposition = "reschedule"
                inc.disposition_at = e.occurred_at
                inc.note = (inc.note + "；改期：" + p.get("reason", "")).strip("；")

    elif t == PATIENT_NO_SHOW:
        state.arrival_status = "no_show"
        state.incidents.append(Incident(
            incident_id=e.event_id, kind=EXCEPTION_NO_SHOW,
            reported_at=e.occurred_at, reported_by=p.get("reported_by", ""),
            detail=p.get("detail", ""),
            disposition=p.get("disposition"),
            disposition_at=_parse_dt(p["disposition_at"]) if p.get("disposition_at") else None,
            note=p.get("note", ""),
        ))

    elif t == TRANSFER_BLOCKED:
        state.arrival_status = "blocked"
        state.incidents.append(Incident(
            incident_id=e.event_id, kind=EXCEPTION_ROAD_BLOCKED,
            reported_at=e.occurred_at, reported_by=p.get("reported_by", ""),
            detail=p.get("detail", ""),
            disposition=p.get("disposition"),
            disposition_at=_parse_dt(p["disposition_at"]) if p.get("disposition_at") else None,
            note=p.get("note", ""),
        ))

    elif t == MEDICATION_UNAVAILABLE:
        state.incidents.append(Incident(
            incident_id=e.event_id, kind=EXCEPTION_MED_UNAVAILABLE,
            reported_at=e.occurred_at, reported_by=p.get("reported_by", ""),
            detail=p.get("medication_code", ""),
            disposition=p.get("disposition"),
            disposition_at=e.occurred_at if p.get("disposition") else None,
            note=p.get("note", ""),
        ))
        if p.get("medication_code"):
            if p.get("disposition") == "origin_supplies":
                state.item_responsibility[p["medication_code"]] = state.origin_org
            elif p.get("disposition") == "resolved_locally":
                state.item_responsibility.pop(p["medication_code"], None)

    elif t == FOLLOWUP_SCHEDULED:
        state.followups[e.aggregate_id] = Followup(
            followup_id=e.aggregate_id,
            scheduled_at=e.occurred_at,
            due_at=_parse_dt(p["due_at"]),
            responsible_org=p["responsible_org"],
            plan_version=int(p.get("plan_version", state.plan_version)),
        )

    elif t == FOLLOWUP_RECORDED:
        fu = state.followups.get(e.aggregate_id)
        if fu:
            fu.recorded_at = e.occurred_at
            fu.result = p.get("result", FOLLOWUP_STABLE)
            fu.abnormal_items = list(p.get("abnormal_items", []))
            fu.status = "recorded"

    elif t == FOLLOWUP_OVERDUE:
        fu = state.followups.get(e.aggregate_id)
        if fu and fu.status == "scheduled":
            fu.status = "overdue"

    elif t == CARE_REESCALATED:
        state.incidents.append(Incident(
            incident_id=e.event_id, kind=EXCEPTION_SYMPTOM_WORSENED,
            reported_at=e.occurred_at, reported_by=p.get("requested_by", ""),
            detail=p.get("reason", ""),
            disposition="reescalation_requested",
            disposition_at=e.occurred_at,
        ))
        state.reescalations.append(Reescalation(
            reescalation_id=e.aggregate_id,
            reason=p["reason"],
            abnormal_item=p.get("abnormal_item"),
            requested_at=e.occurred_at,
            from_org=p["from_org"],
            to_org=p["to_org"],
            offer_id=p.get("offer_id"),
        ))

    elif t == REESCALATION_RECEIVED:
        for r in reversed(state.reescalations):
            if r.status == "requested":
                r.status = "received"
                r.received_at = e.occurred_at
                break

    elif t == CASE_CLOSED:
        state.closed_at = e.occurred_at


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
