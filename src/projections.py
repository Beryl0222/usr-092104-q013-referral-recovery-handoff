"""只读投影视图。

三类读者各取所需，互不可见越权内容：

- 患者/家属：当前由谁负责、下一节点、求助方式；不展示机构间评估细节；
- 机构：只返回本机构照护必需资料，并提示手中计划是否已是最新版；
- 质控：责任空档识别，以及从一次重新上转向前还原交接、随访、异常、时限。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .case_state import CaseState, fold_case
from .events import (
    EXCEPTION_MED_UNAVAILABLE,
    EXCEPTION_NO_SHOW,
    EXCEPTION_ROAD_BLOCKED,
    EXCEPTION_SYMPTOM_WORSENED,
    RESP_ACCEPTED,
    RESP_ARRIVED,
    RESP_BACKSTOP,
    RESP_CLOSED,
    RESP_REESCALATED,
)
from .service import SLA, DomainError
from .storage import EventStore

_RESP_LABEL = {
    RESP_BACKSTOP: "县医院兜底负责",
    RESP_ACCEPTED: "乡镇卫生院已接管",
    RESP_ARRIVED: "患者已到达，乡镇卫生院负责",
    RESP_REESCALATED: "已转回县医院负责",
    RESP_CLOSED: "本案已结案",
}

_INCIDENT_LABEL = {
    EXCEPTION_NO_SHOW: "患者未到",
    EXCEPTION_ROAD_BLOCKED: "道路阻断",
    EXCEPTION_MED_UNAVAILABLE: "药品不可得",
    EXCEPTION_SYMPTOM_WORSENED: "症状恶化",
}


@dataclass
class ProjectionReader:
    store: EventStore

    def _state(self, case_id: str) -> CaseState:
        state = fold_case(self.store.events_for_case(case_id))
        if state.opened_at is None:
            raise DomainError("case_not_found", f"案例不存在或未开启：{case_id}")
        return state

    # ---- 患者/家属视图 ----------------------------------------------------

    def patient_view(self, case_id: str, *, at: datetime | None = None) -> dict[str, Any]:
        state = self._state(case_id)
        at = at or self.store.now()
        current = state.responsibility_at(at)
        responsible: dict[str, Any]
        if current["org"] == state.origin_org:
            responsible = {
                "org": state.origin_org,
                "contact": state.origin_contact,
                "role": "原转出医院（兜底）",
            }
        else:
            acc = state.latest_acceptance
            responsible = {
                "org": current["org"],
                "contact": f"{acc.contact_person} {acc.contact_phone}" if acc else "",
                "role": "接管的乡镇卫生院",
            }

        next_node, next_node_at = self._next_node(state, at)
        return {
            "case_id": case_id,
            "responsible_now": responsible,
            "responsibility_status": _RESP_LABEL.get(current["status"], current["status"]),
            "next_node": next_node,
            "next_node_at": next_node_at,
            "help_phone": state.help_phone,
            "plan_version_in_charge": state.plan_version,
        }

    def _next_node(self, state: CaseState, at: datetime) -> tuple[str, str | None]:
        if state.closed_at is not None:
            return "随访结束，注意康复锻炼，有疑问拨打求助电话", None
        pending_re = next((r for r in reversed(state.reescalations) if r.status == "requested"), None)
        if pending_re is not None:
            return "等待县医院接收重新上转，请就近等待并配合应急处置", None
        if state.reescalations and state.reescalations[-1].status == "received":
            return "已到县医院继续治疗，等待县医院安排", state.reescalations[-1].received_at.isoformat() if state.reescalations[-1].received_at else None
        if state.arrival_status == "blocked":
            return "道路阻断处理中，县医院正协调改道或改期", None
        if state.arrival_status == "no_show":
            return "医院在联系您确认到达时间，请保持电话畅通", state.expected_arrival_at.isoformat() if state.expected_arrival_at else None
        if state.latest_acceptance is not None and state.arrived_at is None:
            return "按约定时间到乡镇卫生院报到", state.expected_arrival_at.isoformat() if state.expected_arrival_at else None
        offer = state.is_open_offer()
        if offer is not None:
            return "等待乡镇卫生院确认接收", None
        if state.arrived_at is not None:
            due = self._next_followup_due(state)
            if due:
                return "按时到乡镇卫生院复查/随访", due.isoformat()
            return "在乡镇卫生院康复，有不适及时联系责任医生", None
        return "等待县医院安排下转机构", None

    @staticmethod
    def _next_followup_due(state: CaseState) -> datetime | None:
        pendings = [f.due_at for f in state.followups.values() if f.status != "recorded"]
        return min(pendings) if pendings else None

    # ---- 机构视图（最小必要共享） ----------------------------------------

    def org_view(self, case_id: str, viewer_org: str) -> dict[str, Any]:
        """仅返回该机构参与照护所必需的资料；非参与方拒绝读取。"""
        state = self._state(case_id)
        involved = {state.origin_org} | {o.target_org for o in state.offers.values()}
        involved |= {a.org for a in state.acceptances}
        if viewer_org not in involved:
            raise DomainError("not_authorized", "该机构未参与本案例，不共享任何资料")

        delivered_versions = sorted({
            d.plan_version for d in state.deliveries
            if d.holder == viewer_org and (d.delivered_at or d.acknowledged_at)
        })
        acked_versions = sorted({
            d.plan_version for d in state.deliveries
            if d.holder == viewer_org and d.acknowledged_at
        })

        view: dict[str, Any] = {
            "case_id": case_id,
            "viewer_org": viewer_org,
            "role": "origin" if viewer_org == state.origin_org else "receiver",
            "plan_versions_held": acked_versions,
            "latest_plan_version": state.plan_version,
            "stale_version_held": state.plan_version not in acked_versions and bool(acked_versions or delivered_versions),
            "pending_version_deliveries": [
                {
                    "plan_version": d.plan_version,
                    "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
                    "acknowledged": d.acknowledged_at is not None,
                }
                for d in state.undelivered_or_unacked() if d.holder == viewer_org
            ],
        }

        plan = state.plan
        if plan is not None:
            view["current_plan_minimal"] = {
                "version": plan.version,
                "signed_by": plan.signed_by,
                "diagnosis_summary": plan.diagnosis_summary,
                "rehab_summary": plan.rehab_summary,
                "precautions": plan.precautions,
                "medications": plan.medications,
                "review_items": plan.review_items,
            }

        # 责任与排程只给与本机构相关的部分。
        now = self.store.now()
        current = state.responsibility_at(now)
        view["currently_responsible"] = (current["org"] == viewer_org)
        if viewer_org != state.origin_org:
            my_offer = next((o for o in state.offers.values() if o.target_org == viewer_org), None)
            if my_offer is not None:
                view["my_offer"] = {
                    "offer_id": my_offer.offer_id,
                    "status": my_offer.status,
                    "serviceable_from": my_offer.serviceable_from.isoformat() if my_offer.serviceable_from else None,
                    "expected_arrival_at": my_offer.expected_arrival_at.isoformat() if my_offer.expected_arrival_at else None,
                }
            my_acceptance = next((a for a in reversed(state.acceptances) if a.org == viewer_org), None)
            if my_acceptance is not None:
                view["my_accepted_scope"] = {
                    "serviceable_from": my_acceptance.serviceable_from.isoformat(),
                    "capabilities": my_acceptance.scope_capabilities,
                    "medication_codes": my_acceptance.medication_codes,
                    "review_codes": my_acceptance.review_codes,
                }
            view["my_followups"] = [
                {
                    "followup_id": f.followup_id,
                    "due_at": f.due_at.isoformat(),
                    "status": f.status,
                    "result": f.result,
                }
                for f in state.followups.values() if f.responsible_org == viewer_org
            ]
        else:
            view["backstop_active"] = current["org"] == state.origin_org and current["status"] == RESP_BACKSTOP
            view["medications_origin_supplies"] = sorted(
                code for code, org in state.item_responsibility.items() if org == state.origin_org
            )
        return view

    # ---- 质控视图 ---------------------------------------------------------

    def quality_view(self, case_id: str, *, at: datetime | None = None) -> dict[str, Any]:
        state = self._state(case_id)
        at = at or self.store.now()
        gaps: list[dict[str, Any]] = []

        # 1) 旧版计划仍有人持有：新版未确认即可能按旧计划执行。
        for holder, ver in state.holders_of_stale_version().items():
            gaps.append({
                "type": "stale_plan_in_use",
                "holder": holder,
                "held_version": ver,
                "latest_version": state.plan_version,
                "severity": "high",
            })
        for d in state.undelivered_or_unacked():
            gaps.append({
                "type": "plan_version_not_confirmed",
                "holder": d.holder,
                "plan_version": d.plan_version,
                "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
                "severity": "medium",
            })

        # 2) 邀约超过 24h 未被接受：责任仍在原机构，但交接悬空。
        offer = state.is_open_offer()
        if offer is not None:
            elapsed = at - offer.offered_at
            if elapsed > SLA["offer_to_accept"]:
                gaps.append({
                    "type": "offer_unaccepted_overdue",
                    "target_org": offer.target_org,
                    "offer_id": offer.offer_id,
                    "overdue_hours": round(elapsed.total_seconds() / 3600, 1),
                    "severity": "high",
                })

        # 3) 异常未处置或处置超时限。
        for inc in state.incidents:
            sla_key = {
                EXCEPTION_NO_SHOW: "no_show_disposition",
                EXCEPTION_ROAD_BLOCKED: "road_block_disposition",
                EXCEPTION_MED_UNAVAILABLE: "medication_disposition",
            }.get(inc.kind)
            if inc.kind == EXCEPTION_SYMPTOM_WORSENED:
                continue  # 上转时限单独评估
            if inc.disposition is None and sla_key:
                elapsed = at - inc.reported_at
                if elapsed > SLA[sla_key]:
                    gaps.append({
                        "type": "incident_unresolved_overdue",
                        "incident_id": inc.incident_id,
                        "kind": _INCIDENT_LABEL[inc.kind],
                        "overdue_hours": round(elapsed.total_seconds() / 3600, 1),
                        "severity": "high",
                    })

        # 4) 随访逾期。
        for f in state.followups.values():
            if f.status in ("scheduled", "overdue") and f.due_at < at:
                gaps.append({
                    "type": "followup_overdue",
                    "followup_id": f.followup_id,
                    "responsible_org": f.responsible_org,
                    "due_at": f.due_at.isoformat(),
                    "severity": "medium",
                })

        # 5) 重新上转未被接收超过 2h：责任未落回原机构，风险最高。
        pending_re = next((r for r in reversed(state.reescalations) if r.status == "requested"), None)
        if pending_re is not None:
            elapsed = at - pending_re.requested_at
            gaps.append({
                "type": "reescalation_not_received",
                "reescalation_id": pending_re.reescalation_id,
                "elapsed_hours": round(elapsed.total_seconds() / 3600, 1),
                "within_sla": elapsed <= SLA["reescalation_receive"],
                "severity": "high" if elapsed > SLA["reescalation_receive"] else "low",
            })

        # 6) 责任空档扫描：逐小时回放，任何时点都必须有责任方。
        gap_windows = self._responsibility_gap_windows(state)

        return {
            "case_id": case_id,
            "open": state.closed_at is None,
            "gaps": gaps,
            "responsibility_gap_windows": gap_windows,
            "reescalations": [self._trace_reescalation(state, r.reescalation_id)
                              for r in state.reescalations],
        }

    @staticmethod
    def _responsibility_gap_windows(state: CaseState) -> list[dict[str, Any]]:
        """责任空档在建模上不应出现；此函数作为运行时不变量校验。"""
        if state.opened_at is None:
            return []
        checkpoints = [state.opened_at]
        checkpoints += [a.serviceable_from for a in state.acceptances]
        if state.arrived_at:
            checkpoints.append(state.arrived_at)
        checkpoints += [r.requested_at for r in state.reescalations]
        checkpoints += [r.received_at for r in state.reescalations if r.received_at]
        windows: list[dict[str, Any]] = []
        for ts in sorted(set(checkpoints)):
            snap = state.responsibility_at(ts)
            if not snap["org"]:
                windows.append({"at": ts.isoformat(), "status": snap["status"]})
        return windows

    def _trace_reescalation(self, state: CaseState, reescalation_id: str) -> dict[str, Any]:
        """从一次重新上转还原：交接、随访、异常与处理时限全链路。"""
        target = next((r for r in state.reescalations if r.reescalation_id == reescalation_id), None)
        if target is None:
            raise DomainError("reescalation_not_found", f"上转记录不存在：{reescalation_id}")
        cutoff = target.received_at or self.store.now()

        trace: list[dict[str, Any]] = []
        # 邀约与接受。
        related_offer = state.offers.get(target.offer_id) if target.offer_id else None
        if related_offer is not None:
            trace.append({
                "stage": "handoff_offer",
                "at": related_offer.offered_at.isoformat(),
                "org": related_offer.target_org,
                "plan_version": related_offer.plan_version,
            })
        for acc in state.acceptances:
            if related_offer is None or acc.org == related_offer.target_org:
                trace.append({
                    "stage": "responsibility_accepted",
                    "at": acc.accepted_at.isoformat(),
                    "org": acc.org,
                    "serviceable_from": acc.serviceable_from.isoformat(),
                    "plan_version": _accepted_plan_version(state, acc),
                })
        if state.arrived_at and state.arrived_at <= target.requested_at:
            trace.append({"stage": "arrival", "at": state.arrived_at.isoformat()})

        # 随访（只取上转之前的）。
        for f in state.followups.values():
            if f.recorded_at and f.recorded_at <= target.requested_at:
                trace.append({
                    "stage": "followup",
                    "at": f.recorded_at.isoformat(),
                    "followup_id": f.followup_id,
                    "result": f.result,
                    "abnormal_items": f.abnormal_items,
                })

        # 异常（症状恶化已由 reescalation_requested 阶段表达，不重复列出）。
        for inc in state.incidents:
            if inc.kind == EXCEPTION_SYMPTOM_WORSENED:
                continue
            if inc.reported_at <= target.requested_at:
                entry = {
                    "stage": "incident",
                    "at": inc.reported_at.isoformat(),
                    "kind": _INCIDENT_LABEL.get(inc.kind, inc.kind),
                    "disposition": inc.disposition,
                }
                if inc.disposition_at:
                    entry["resolved_at"] = inc.disposition_at.isoformat()
                    entry["resolution_hours"] = round(
                        (inc.disposition_at - inc.reported_at).total_seconds() / 3600, 2
                    )
                trace.append(entry)

        # 上转与接收时限。
        trace.append({
            "stage": "reescalation_requested",
            "at": target.requested_at.isoformat(),
            "from_org": target.from_org,
            "to_org": target.to_org,
            "reason": target.reason,
            "abnormal_item": target.abnormal_item,
        })
        receive_hours = None
        received_within_sla = None
        if target.received_at:
            receive_hours = round((target.received_at - target.requested_at).total_seconds() / 3600, 2)
            received_within_sla = (target.received_at - target.requested_at) <= SLA["reescalation_receive"]
            trace.append({
                "stage": "reescalation_received",
                "at": target.received_at.isoformat(),
                "receive_hours": receive_hours,
                "within_sla": received_within_sla,
            })

        trace.sort(key=lambda x: x["at"])
        return {
            "reescalation_id": reescalation_id,
            "status": target.status,
            "reason": target.reason,
            "abnormal_item": target.abnormal_item,
            "receive_hours": receive_hours,
            "receive_within_sla": received_within_sla,
            "timeline": trace,
        }


def _accepted_plan_version(state: CaseState, acc: Any) -> int | None:
    offer = next((o for o in state.offers.values() if o.target_org == acc.org), None)
    return offer.plan_version if offer else None
