"""康复下转责任交接应用服务。

职责边界：
- 服务只搬运和校对医生已经签署的计划内容，**不生成、不改写治疗方案**；
  用药替代必须携带医生确认，否则只能走原机构供药路径。
- 责任转移以基层明确接受为唯一界点；接受之前、再上转接收之前，
  原机构始终兜底，任何时刻都存在唯一责任方。
- 计划修订自动向所有旧版持有人生成新版本送达记录；送达与确认分开留痕。
- 患者未到、道路阻断、药品不可得、症状恶化四条路径互不混淆。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from . import events as ev
from .case_state import CaseState, fold_case
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
    FOLLOWUP_WORSENED,
    HANDOFF_DECLINED,
    HANDOFF_OFFERED,
    HANDOFF_WITHDRAWN,
    MEDICATION_UNAVAILABLE,
    OFFER_OPEN,
    PATIENT_NO_SHOW,
    PLAN_REVISED,
    PLAN_SIGNED,
    PLAN_VERSION_ACKNOWLEDGED,
    PLAN_VERSION_DELIVERED,
    REESCALATION_RECEIVED,
    RESPONSIBILITY_ACCEPTED,
    TRANSFER_BLOCKED,
    CARE_RESPONSIBILITY,
    FOLLOWUP_RESULT,
    HANDOFF_OFFER as OFFER_AGG,
    RECOVERY_PLAN,
    REFERRAL_CASE,
    Event,
)
from .storage import EventStore

# 处理时限（质控用），超时不阻断业务，只在质控视图标记。
SLA = {
    "offer_to_accept": timedelta(hours=24),
    "no_show_disposition": timedelta(hours=24),
    "road_block_disposition": timedelta(hours=12),
    "medication_disposition": timedelta(hours=24),
    "reescalation_receive": timedelta(hours=2),
}

# 药品不可得的处置路径。
MED_ORIGIN_SUPPLIES = "origin_supplies"          # 原机构保障供药
MED_SUBSTITUTE_DOCTOR = "substitute_doctor_confirmed"  # 医生确认的替代
MED_RESOLVED_LOCALLY = "resolved_locally"        # 基层在同目录内解决

# 未到/阻断的处置路径。
DISPOSITION_RESCHEDULE = "reschedule"
DISPOSITION_REROUTE = "reroute"
DISPOSITION_ORIGIN_FOLLOW = "origin_follow"


class DomainError(Exception):
    """业务规则冲突；code 供接入侧稳定判别。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class HandoffService:
    def __init__(self, store: EventStore | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store or EventStore()
        self._clock = clock or (lambda: datetime.now().astimezone())

    # ---- 内部工具 ---------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _state(self, case_id: str) -> CaseState:
        state = fold_case(self.store.events_for_case(case_id))
        if state.opened_at is None:
            raise DomainError("case_not_found", f"案例不存在或未开启：{case_id}")
        return state

    def _next_version(self, case_id: str, aggregate_type: str, aggregate_id: str) -> int:
        stream = self.store.events_for_case(case_id)
        versions = [
            e.version for e in stream
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]
        return (max(versions) + 1) if versions else 1

    def _append(
        self, *, case_id: str, aggregate_type: str, aggregate_id: str,
        event_type: str, summary: str, payload: dict[str, Any],
        request_id: str | None = None, at: datetime | None = None,
        event_id: str | None = None,
    ) -> Event:
        event = Event(
            event_id=event_id or _new_id("evt"),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=at or self._now(),
            version=self._next_version(case_id, aggregate_type, aggregate_id),
            summary=summary,
            payload=payload,
            case_id=case_id,
            request_id=request_id,
        )
        errors = ev.validate_envelope(event)
        if errors:
            raise DomainError("invalid_event", "；".join(errors))
        return self.store.append(event)

    def _prior_submission(
        self, event_id: str | None = None, request_id: str | None = None,
    ) -> Event | None:
        """外部上报重试短路：同 event_id / request_id 直接回放首次事件。"""
        if event_id:
            prior = self.store.get_event(event_id)
            if prior is not None:
                return prior
        if request_id:
            return self.store.get_by_request(request_id)
        return None

    def _plan_agg(self, case_id: str) -> str:
        return f"plan-{case_id}"

    def _resp_agg(self, case_id: str) -> str:
        return f"resp-{case_id}"

    # ---- 1. 住院阶段：开案与计划签署/修订 --------------------------------

    def open_case(
        self, *, patient_ref: str, patient_contact: str,
        origin_org: str, origin_contact: str, help_phone: str,
        case_id: str | None = None, request_id: str | None = None,
    ) -> Event:
        case_id = case_id or _new_id("case")
        prior = self._prior_submission(None, request_id)
        if prior is not None:
            return prior
        return self._append(
            case_id=case_id, aggregate_type=REFERRAL_CASE, aggregate_id=case_id,
            event_type=CASE_OPENED, summary=f"开启下转案例：{patient_ref}",
            payload={
                "patient_ref": patient_ref,
                "patient_contact": patient_contact,
                "origin_org": origin_org,
                "origin_contact": origin_contact,
                "help_phone": help_phone,
            },
            request_id=request_id,
        )

    def sign_plan(
        self, case_id: str, *, signed_by: str, diagnosis_summary: str,
        rehab_summary: str, precautions: list[str],
        medications: list[dict[str, Any]], review_items: list[dict[str, Any]],
        required_capabilities: list[str], request_id: str | None = None,
    ) -> Event:
        """住院医生签署转出计划 v1。内容必须来自医生，服务不得补造。"""
        prior = self._prior_submission(None, request_id)
        if prior is not None:
            return prior
        self._state(case_id)
        self._validate_plan_content(medications, review_items, required_capabilities)
        agg = self._plan_agg(case_id)
        if self.store.events_for_aggregate(case_id, RECOVERY_PLAN, agg):
            raise DomainError("plan_exists", "计划已签署，修订请使用 revise_plan")
        return self._append(
            case_id=case_id, aggregate_type=RECOVERY_PLAN, aggregate_id=agg,
            event_type=PLAN_SIGNED, summary="医生签署康复转出计划 v1",
            payload={
                "plan_version": 1,
                "signed_by": signed_by,
                "diagnosis_summary": diagnosis_summary,
                "rehab_summary": rehab_summary,
                "precautions": precautions,
                "medications": medications,
                "review_items": review_items,
                "required_capabilities": required_capabilities,
            },
            request_id=request_id,
        )

    def revise_plan(
        self, case_id: str, *, signed_by: str, diagnosis_summary: str,
        rehab_summary: str, precautions: list[str],
        medications: list[dict[str, Any]], review_items: list[dict[str, Any]],
        required_capabilities: list[str], change_note: str,
        request_id: str | None = None,
    ) -> list[Event]:
        """医生修订计划：版本 +1，并向所有旧版持有人送达新版。

        返回事件列表，首个为 PLAN_REVISED，其余为 PLAN_VERSION_DELIVERED。
        服务不改内容，只负责版本推进与送达留痕。
        """
        state = self._state(case_id)
        if request_id and self.store.has_request(request_id):
            # 命令重试：不重复修订、不重复送达，回放该命令产生的全部事件。
            return sorted(self.store.events_for_request(request_id), key=lambda e: e.seq)
        if not state.plan_versions:
            raise DomainError("plan_not_signed", "计划尚未签署，不能修订")
        self._validate_plan_content(medications, review_items, required_capabilities)
        agg = self._plan_agg(case_id)
        new_version = state.plan_version + 1
        revised = self._append(
            case_id=case_id, aggregate_type=RECOVERY_PLAN, aggregate_id=agg,
            event_type=PLAN_REVISED, summary=f"医生修订康复转出计划 v{new_version}：{change_note}",
            payload={
                "plan_version": new_version,
                "supersedes": state.plan_version,
                "signed_by": signed_by,
                "diagnosis_summary": diagnosis_summary,
                "rehab_summary": rehab_summary,
                "precautions": precautions,
                "medications": medications,
                "review_items": review_items,
                "required_capabilities": required_capabilities,
                "change_note": change_note,
            },
            request_id=request_id,
        )
        delivered: list[Event] = [revised]
        # 所有曾收到过任一旧版本的持有人都必须收到新版（原机构是修订方，不再投送）。
        stale_holders = sorted(h for h in state.holders if h and h != state.origin_org)
        for holder in stale_holders:
            delivered.append(self._append(
                case_id=case_id, aggregate_type=RECOVERY_PLAN, aggregate_id=agg,
                event_type=PLAN_VERSION_DELIVERED,
                summary=f"计划 v{new_version} 送达 {holder}",
                payload={"plan_version": new_version, "holder": holder,
                         "supersedes": state.plan_version},
                request_id=request_id,
            ))
        return delivered

    def acknowledge_plan_version(self, case_id: str, holder: str, plan_version: int) -> Event:
        state = self._state(case_id)
        if plan_version not in state.plan_versions:
            raise DomainError("unknown_version", f"计划版本不存在：v{plan_version}")
        return self._append(
            case_id=case_id, aggregate_type=RECOVERY_PLAN, aggregate_id=self._plan_agg(case_id),
            event_type=PLAN_VERSION_ACKNOWLEDGED,
            summary=f"{holder} 确认收到计划 v{plan_version}",
            payload={"plan_version": plan_version, "holder": holder},
        )

    @staticmethod
    def _validate_plan_content(
        medications: list[dict[str, Any]], review_items: list[dict[str, Any]],
        required_capabilities: list[str],
    ) -> None:
        for med in medications:
            if not med.get("code") or not med.get("name"):
                raise DomainError("invalid_plan", "用药必须含 code 与 name")
        for item in review_items:
            if not item.get("code") or not item.get("name"):
                raise DomainError("invalid_plan", "复查项目必须含 code 与 name")
        if not required_capabilities:
            raise DomainError("invalid_plan", "计划必须声明接收机构所需能力")

    # ---- 2. 下转邀约（最少必要临床摘要随邀约共享） ------------------------

    def offer_handoff(
        self, case_id: str, *, target_org: str,
        serviceable_from: datetime | None = None,
        expected_arrival_at: datetime | None = None,
        target_capabilities: list[str] | None = None,
    ) -> Event:
        state = self._state(case_id)
        if state.closed_at:
            raise DomainError("case_closed", "案例已结案，不能再发起交接")
        if not state.plan:
            raise DomainError("plan_not_signed", "计划尚未签署，不能发起交接")
        if state.is_open_offer() is not None:
            raise DomainError("offer_open", "已有未决邀约，须先拒绝或撤回后再发起")
        if target_org == state.origin_org:
            raise DomainError("invalid_target", "接收机构不能是原机构")
        plan = state.plan
        missing_caps = sorted(set(plan.required_capabilities) - set(target_capabilities or []))
        if missing_caps:
            raise DomainError(
                "capability_gap",
                f"接收机构能力不覆盖计划需要：{missing_caps}",
            )
        offer_id = _new_id("offer")
        offered = self._append(
            case_id=case_id, aggregate_type=OFFER_AGG, aggregate_id=offer_id,
            event_type=HANDOFF_OFFERED,
            summary=f"向 {target_org} 发起康复下转邀约（计划 v{plan.version}）",
            payload={
                "target_org": target_org,
                "plan_version": plan.version,
                "serviceable_from": serviceable_from.isoformat() if serviceable_from else None,
                "expected_arrival_at": expected_arrival_at.isoformat() if expected_arrival_at else None,
                "capabilities": list(target_capabilities or []),
                # 仅共享照护必需摘要：诊断/康复要点、风险提示、用药与复查版本内容。
                "clinical_summary_minimal": {
                    "diagnosis_summary": plan.diagnosis_summary,
                    "rehab_summary": plan.rehab_summary,
                    "precautions": plan.precautions,
                    "medications": plan.medications,
                    "review_items": plan.review_items,
                },
            },
        )
        # 邀约即把当前版本计划投递给接收方，纳入版本送达追踪。
        self._append(
            case_id=case_id, aggregate_type=RECOVERY_PLAN, aggregate_id=self._plan_agg(case_id),
            event_type=PLAN_VERSION_DELIVERED,
            summary=f"计划 v{plan.version} 送达 {target_org}",
            payload={"plan_version": plan.version, "holder": target_org},
        )
        return offered

    def decline_offer(self, case_id: str, offer_id: str, reason: str) -> Event:
        state = self._state(case_id)
        offer = state.offers.get(offer_id)
        if offer is None:
            raise DomainError("offer_not_found", f"邀约不存在：{offer_id}")
        if offer.status != OFFER_OPEN:
            raise DomainError("offer_decided", f"邀约已{offer.status}，不能拒绝")
        return self._append(
            case_id=case_id, aggregate_type=OFFER_AGG, aggregate_id=offer_id,
            event_type=HANDOFF_DECLINED,
            summary=f"{offer.target_org} 拒绝下转邀约：{reason}",
            payload={"reason": reason},
        )

    def withdraw_offer(self, case_id: str, offer_id: str, reason: str) -> Event:
        state = self._state(case_id)
        offer = state.offers.get(offer_id)
        if offer is None:
            raise DomainError("offer_not_found", f"邀约不存在：{offer_id}")
        if offer.status != OFFER_OPEN:
            raise DomainError("offer_decided", f"邀约已{offer.status}，不能撤回")
        return self._append(
            case_id=case_id, aggregate_type=OFFER_AGG, aggregate_id=offer_id,
            event_type=HANDOFF_WITHDRAWN,
            summary=f"原机构撤回下转邀约：{reason}",
            payload={"reason": reason},
        )

    # ---- 3. 基层明确接受具体责任与可服务日期 ------------------------------

    def accept_responsibility(
        self, case_id: str, *, offer_id: str, contact_person: str,
        contact_phone: str, serviceable_from: datetime,
        scope_capabilities: list[str],
        medication_codes: list[str] | None = None,
        review_codes: list[str] | None = None,
    ) -> Event:
        state = self._state(case_id)
        offer = state.offers.get(offer_id)
        if offer is None:
            raise DomainError("offer_not_found", f"邀约不存在：{offer_id}")
        if offer.status != OFFER_OPEN:
            raise DomainError("offer_decided", f"邀约已{offer.status}，不能接受")
        plan = state.plan_versions[offer.plan_version]
        if serviceable_from < state.opened_at:
            raise DomainError("invalid_date", "可服务日期不能早于案例开启时间")

        # 接受必须覆盖计划的具体责任范围，避免“双方都以为对方负责”。
        missing_caps = sorted(set(plan.required_capabilities) - set(scope_capabilities))
        plan_meds = [m["code"] for m in plan.medications]
        plan_reviews = [r["code"] for r in plan.review_items]
        missing_meds = sorted(set(plan_meds) - set(medication_codes or []))
        missing_reviews = sorted(set(plan_reviews) - set(review_codes or []))
        gaps = []
        if missing_caps:
            gaps.append(f"能力 {missing_caps}")
        if missing_meds:
            gaps.append(f"用药 {missing_meds}")
        if missing_reviews:
            gaps.append(f"复查 {missing_reviews}")
        if gaps:
            raise DomainError("scope_gap", "接受范围未覆盖计划要求：" + "；".join(gaps))

        accepted = self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=RESPONSIBILITY_ACCEPTED,
            summary=(f"{offer.target_org} 接受下转责任，"
                     f"可服务日期 {serviceable_from.date().isoformat()}"),
            payload={
                "offer_id": offer_id,
                "org": offer.target_org,
                "contact_person": contact_person,
                "contact_phone": contact_phone,
                "serviceable_from": serviceable_from.isoformat(),
                "scope_capabilities": scope_capabilities,
                "medication_codes": sorted(medication_codes or []),
                "review_codes": sorted(review_codes or []),
                "plan_version": offer.plan_version,
            },
        )
        # 接受即证明接收方持有并依据该计划版本，补一条版本确认。
        already_acked = any(
            d.holder == offer.target_org and d.plan_version == offer.plan_version
            and d.acknowledged_at is not None
            for d in state.deliveries
        )
        if not already_acked:
            self._append(
                case_id=case_id, aggregate_type=RECOVERY_PLAN, aggregate_id=self._plan_agg(case_id),
                event_type=PLAN_VERSION_ACKNOWLEDGED,
                summary=f"{offer.target_org} 接受责任并确认计划 v{offer.plan_version}",
                payload={"plan_version": offer.plan_version, "holder": offer.target_org,
                         "via": "responsibility_accepted"},
            )
        return accepted

    # ---- 4. 患者到达 ------------------------------------------------------

    def confirm_arrival(self, case_id: str, *, arrived_at: datetime | None = None,
                        event_id: str | None = None, request_id: str | None = None) -> Event:
        prior = self._prior_submission(event_id, request_id)
        if prior is not None:
            return prior
        state = self._state(case_id)
        if state.arrived_at is not None:
            raise DomainError("already_arrived", "患者已确认到达，请勿重复上报")
        if state.latest_acceptance is None:
            # 责任未接受前到达也不能算交接完成：原机构仍兜底。
            raise DomainError("not_accepted", "基层尚未接受责任，到达不能完成交接")
        at = arrived_at or self._now()
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=ARRIVAL_CONFIRMED, summary="患者已到达接收机构",
            payload={"at": at.isoformat()}, at=at,
            event_id=event_id, request_id=request_id,
        )

    def reschedule_arrival(self, case_id: str, *, new_expected_arrival_at: datetime, reason: str) -> Event:
        state = self._state(case_id)
        if state.arrived_at:
            raise DomainError("already_arrived", "患者已到达，无需改期")
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=ARRIVAL_RESCHEDULED,
            summary=f"预计到达改期：{reason}",
            payload={
                "new_expected_arrival_at": new_expected_arrival_at.isoformat(),
                "reason": reason,
            },
        )

    # ---- 5. 四条异常路径 --------------------------------------------------

    def report_no_show(
        self, case_id: str, *, reported_by: str, detail: str = "",
        disposition: str | None = None, disposition_at: datetime | None = None,
        event_id: str | None = None, request_id: str | None = None,
    ) -> Event:
        """患者未到：联系核实、改期/原机构追访；不改变接收方的责任约定。"""
        prior = self._prior_submission(event_id, request_id)
        if prior is not None:
            return prior
        state = self._state(case_id)
        if state.arrived_at:
            raise DomainError("already_arrived", "患者已到达，不能上报未到")
        if any(i.kind == EXCEPTION_NO_SHOW and i.disposition is None for i in state.incidents):
            raise DomainError("incident_open", "已存在未处置的未到上报，请勿重复上报")
        if disposition is not None and disposition not in (
            DISPOSITION_RESCHEDULE, DISPOSITION_REROUTE, DISPOSITION_ORIGIN_FOLLOW,
        ):
            raise DomainError("invalid_disposition", "未到处置方式不合法")
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=PATIENT_NO_SHOW, summary="患者未按约到达",
            payload={
                "reported_by": reported_by, "detail": detail,
                "disposition": disposition,
                "disposition_at": disposition_at.isoformat() if disposition_at else None,
            },
            event_id=event_id, request_id=request_id,
        )

    def report_road_blocked(
        self, case_id: str, *, reported_by: str, detail: str,
        disposition: str | None = None, new_route_eta: datetime | None = None,
        event_id: str | None = None, request_id: str | None = None,
    ) -> Event:
        """道路阻断：等待通路/改道/改期；阻断期间原机构继续兜底。"""
        prior = self._prior_submission(event_id, request_id)
        if prior is not None:
            return prior
        state = self._state(case_id)
        if state.arrived_at:
            raise DomainError("already_arrived", "患者已到达，不能上报道路阻断")
        if disposition is not None and disposition not in (
            DISPOSITION_RESCHEDULE, DISPOSITION_REROUTE, DISPOSITION_ORIGIN_FOLLOW,
        ):
            raise DomainError("invalid_disposition", "阻断处置方式不合法")
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=TRANSFER_BLOCKED, summary=f"转运道路阻断：{detail}",
            payload={
                "reported_by": reported_by, "detail": detail,
                "disposition": disposition,
                "new_route_eta": new_route_eta.isoformat() if new_route_eta else None,
            },
            event_id=event_id, request_id=request_id,
        )

    def report_medication_unavailable(
        self, case_id: str, *, medication_code: str, reported_by: str,
        disposition: str, substitute: dict[str, Any] | None = None,
        doctor_confirmed_by: str | None = None, note: str = "",
        event_id: str | None = None, request_id: str | None = None,
    ) -> Event:
        """药品不可得：原机构供药 / 医生确认替代 / 同目录解决。

        替代用药必须携带医生确认，系统绝不自行换药或调整治疗。
        """
        prior = self._prior_submission(event_id, request_id)
        if prior is not None:
            return prior
        state = self._state(case_id)
        if state.latest_acceptance is None:
            raise DomainError("not_accepted", "基层尚未接受责任，药品保障由原机构负责")
        plan = state.plan
        known = {m["code"] for m in plan.medications} if plan else set()
        if medication_code not in known:
            raise DomainError("unknown_medication", f"计划用药中无此编码：{medication_code}")
        if disposition not in (MED_ORIGIN_SUPPLIES, MED_SUBSTITUTE_DOCTOR, MED_RESOLVED_LOCALLY):
            raise DomainError("invalid_disposition", "药品不可得处置方式不合法")
        if disposition == MED_SUBSTITUTE_DOCTOR and not doctor_confirmed_by:
            raise DomainError(
                "doctor_confirmation_required",
                "替代用药必须记录确认医生，系统不得自行改变治疗方案",
            )
        open_incident = next(
            (i for i in state.incidents
             if i.kind == EXCEPTION_MED_UNAVAILABLE and i.detail == medication_code
             and i.disposition is None),
            None,
        )
        if open_incident is not None:
            raise DomainError("incident_open", "该药品已存在未处置的不可得上报")
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=MEDICATION_UNAVAILABLE,
            summary=f"计划用药 {medication_code} 在接收机构不可得",
            payload={
                "medication_code": medication_code,
                "reported_by": reported_by,
                "disposition": disposition,
                "substitute": substitute,
                "doctor_confirmed_by": doctor_confirmed_by,
                "note": note,
            },
            event_id=event_id, request_id=request_id,
        )

    # ---- 6. 随访与异常 ----------------------------------------------------

    def schedule_followup(
        self, case_id: str, *, due_at: datetime, responsible_org: str | None = None,
        followup_id: str | None = None,
    ) -> Event:
        state = self._state(case_id)
        org = responsible_org or state.responsibility_at(self._now())["org"]
        if not state.plan:
            raise DomainError("plan_not_signed", "计划尚未签署，不能安排随访")
        return self._append(
            case_id=case_id, aggregate_type=FOLLOWUP_RESULT,
            aggregate_id=followup_id or _new_id("followup"),
            event_type=FOLLOWUP_SCHEDULED,
            summary=f"预约随访，截止 {due_at.isoformat()}",
            payload={
                "due_at": due_at.isoformat(),
                "responsible_org": org,
                "plan_version": state.plan_version,
            },
        )

    def record_followup(
        self, case_id: str, followup_id: str, *, result: str,
        abnormal_items: list[dict[str, Any]] | None = None,
        note: str = "", request_id: str | None = None,
    ) -> Event:
        """登记随访结果。

        result=stable/abnormal/symptom_worsened；abnormal_items 携带
        指标值与是否越过计划阈值（threshold_exceeded）。症状恶化需另行
        调用 request_reescalation 走上转路径，服务不替医生做临床判断。
        """
        prior = self._prior_submission(None, request_id)
        if prior is not None:
            return prior
        state = self._state(case_id)
        fu = state.followups.get(followup_id)
        if fu is None:
            raise DomainError("followup_not_found", f"随访不存在：{followup_id}")
        if fu.status == "recorded":
            raise DomainError("followup_recorded", "随访已登记结果，不能重复登记")
        if result not in (ev.FOLLOWUP_STABLE, ev.FOLLOWUP_ABNORMAL, FOLLOWUP_WORSENED):
            raise DomainError("invalid_result", "随访结果分级不合法")
        return self._append(
            case_id=case_id, aggregate_type=FOLLOWUP_RESULT, aggregate_id=followup_id,
            event_type=FOLLOWUP_RECORDED,
            summary=f"随访结果登记：{result}",
            payload={
                "result": result,
                "abnormal_items": abnormal_items or [],
                "note": note,
            },
            request_id=request_id,
        )

    def sweep_followup_overdue(self, *, now: datetime | None = None) -> list[Event]:
        """把已过截止时间且未登记结果的随访标记逾期（可定时调用）。"""
        at = now or self._now()
        marked: list[Event] = []
        for event in self.store.all_events():
            if event.event_type != FOLLOWUP_SCHEDULED:
                continue
            state = self._state(event.case_id)
            fu = state.followups.get(event.aggregate_id)
            if fu and fu.status == "scheduled" and fu.due_at < at:
                marked.append(self._append(
                    case_id=event.case_id, aggregate_type=FOLLOWUP_RESULT,
                    aggregate_id=event.aggregate_id,
                    event_type=FOLLOWUP_OVERDUE,
                    summary="随访超过截止时间未回报",
                    payload={"due_at": fu.due_at.isoformat()},
                    at=at,
                ))
        return marked

    # ---- 7. 症状恶化与重新上转 -------------------------------------------

    def request_reescalation(
        self, case_id: str, *, reason: str, abnormal_item: str | None = None,
        followup_id: str | None = None, incident_detail: str = "",
        event_id: str | None = None, request_id: str | None = None,
    ) -> Event:
        """基层发起重新上转。原机构接收前基层继续负责在院应急处置。"""
        prior = self._prior_submission(event_id, request_id)
        if prior is not None:
            return prior
        state = self._state(case_id)
        acc = state.latest_acceptance
        if acc is None:
            raise DomainError("not_accepted", "责任尚未移交基层，原机构本就在负责，无需上转")
        if any(r.status == "requested" for r in state.reescalations):
            raise DomainError("reescalation_open", "已有未接收的上转请求，请勿重复发起")
        if followup_id and followup_id not in state.followups:
            raise DomainError("followup_not_found", f"随访不存在：{followup_id}")
        open_offer_id = next(
            (o.offer_id for o in state.offers.values()
             if o.target_org == acc.org and o.status == "accepted"),
            None,
        )
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=CARE_REESCALATED,
            summary=f"症状恶化，{acc.org} 申请重新上转：{reason}",
            payload={
                "reason": reason,
                "abnormal_item": abnormal_item,
                "followup_id": followup_id,
                "incident_detail": incident_detail,
                "from_org": acc.org,
                "to_org": state.origin_org,
                "offer_id": open_offer_id,
            },
            event_id=event_id, request_id=request_id,
        )

    def receive_reescalation(self, case_id: str, *, received_by: str, note: str = "") -> Event:
        """原机构明确接收，责任自此回到原机构。未接收不落回，杜绝空档。"""
        state = self._state(case_id)
        pending = next((r for r in reversed(state.reescalations) if r.status == "requested"), None)
        if pending is None:
            raise DomainError("no_open_reescalation", "没有待接收的上转请求")
        return self._append(
            case_id=case_id, aggregate_type=CARE_RESPONSIBILITY,
            aggregate_id=self._resp_agg(case_id),
            event_type=REESCALATION_RECEIVED,
            summary=f"原机构 {state.origin_org} 接收重新上转患者",
            payload={"received_by": received_by, "reescalation_id": pending.reescalation_id,
                     "note": note},
        )

    # ---- 8. 结案 ----------------------------------------------------------

    def close_case(self, case_id: str, *, reason: str) -> Event:
        state = self._state(case_id)
        if state.closed_at:
            raise DomainError("case_closed", "案例已结案")
        return self._append(
            case_id=case_id, aggregate_type=REFERRAL_CASE, aggregate_id=case_id,
            event_type=CASE_CLOSED, summary=f"案例结案：{reason}",
            payload={"reason": reason},
        )
