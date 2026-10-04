"""三类视图：患者与家属、交接双方机构、质控人员。

数据最小化原则：
- 患者视图只含责任方、下一节点与求助方式，不含临床细节；
- 机构视图仅限交接双方，临床摘要写入侧已按最少必要白名单过滤；
- 质控视图只含流程元数据（时限、台账、送达），不含临床内容。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import events, policies
from .events import parse_time
from .service import (
    ACCEPTED,
    ARRIVED,
    CLOSED,
    OFFERED,
    PLAN_SIGNED,
    PLANNING,
    REESCALATED,
    STATE_LABELS,
    DomainError,
)
from .store import Store, decode


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _case(store: Store, case_id: str) -> dict:
    row = store.one("SELECT * FROM cases WHERE case_id = ?", (case_id,))
    if not row:
        raise DomainError(f"交接案例不存在：{case_id}")
    return row


def _institution(store: Store, institution_id: str | None) -> dict | None:
    if not institution_id:
        return None
    return store.one(
        "SELECT * FROM institutions WHERE institution_id = ?", (institution_id,)
    )


def _current_responsibility(store: Store, case_id: str) -> dict | None:
    return store.one(
        "SELECT * FROM resp_ledger WHERE case_id = ? AND ended_at IS NULL"
        " ORDER BY started_at DESC LIMIT 1",
        (case_id,),
    )


def _open_disruptions(store: Store, case_id: str) -> list[dict]:
    rows = store.query(
        "SELECT * FROM disruptions WHERE case_id = ? AND status = 'OPEN'"
        " ORDER BY reported_at",
        (case_id,),
    )
    for row in rows:
        row["detail"] = json.loads(row["detail"])
    return rows


# ---------------------------------------------------------------------------
# 患者与家属视图
# ---------------------------------------------------------------------------

def patient_view(
    store: Store, case_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """当前由谁负责、下一节点、求助方式。"""
    case = _case(store, case_id)
    responsibility = _current_responsibility(store, case_id)
    institution = _institution(store, responsibility["institution"]) if responsibility else None

    next_step = _next_step(store, case, responsibility)
    help_channels = [policies.CONSORTIUM_HOTLINE]
    if institution and institution["phone"]:
        help_channels.insert(0, f"{institution['name']} {institution['phone']}")

    reminders = []
    for disruption in _open_disruptions(store, case_id):
        label = policies.DISRUPTION_LABELS[disruption["kind"]]
        reminders.append(f"{label}正在处置中，如有变化请拨打求助电话")

    return {
        "case_id": case_id,
        "state": case["state"],
        "state_label": STATE_LABELS[case["state"]],
        "responsible_institution": institution["name"] if institution else None,
        "responsible_person": responsibility["person"] if responsibility else None,
        "next_step": next_step,
        "help": help_channels,
        "reminders": reminders,
    }


def _next_step(store: Store, case: dict, responsibility: dict | None) -> str:
    state = case["state"]
    if state == PLANNING:
        return "住院康复中，由县医院负责"
    if state == PLAN_SIGNED:
        return "县医院正在安排下转，请等待通知"
    if state == OFFERED:
        return "正在等待基层机构确认接收"
    if state == ACCEPTED:
        row = store.one(
            "SELECT * FROM responsibilities WHERE responsibility_id = ?",
            (case["responsibility_id"],),
        )
        name = _institution(store, case["receiving_institution"])
        return (
            f"请于 {row['serviceable_from'][:10]} 至 {row['serviceable_to'][:10]}"
            f" 期间到 {name['name'] if name else case['receiving_institution']} 报到"
        )
    if state == ARRIVED:
        followup = store.one(
            "SELECT detail FROM followups WHERE case_id = ?"
            " ORDER BY occurred_at DESC LIMIT 1",
            (case["case_id"],),
        )
        text = "在基层机构接受康复与随访"
        if followup:
            next_recheck = json.loads(followup["detail"]).get("next_recheck_at")
            if next_recheck:
                text += f"，下次复查 {next_recheck[:10]}"
        return text
    if state == REESCALATED:
        reescalation = store.one(
            "SELECT target_institution FROM reescalations WHERE case_id = ?"
            " ORDER BY occurred_at DESC LIMIT 1",
            (case["case_id"],),
        )
        target = _institution(store, reescalation["target_institution"]) if reescalation else None
        return f"已转回 {target['name'] if target else '上级医院'}，由上级医院继续负责"
    if state == CLOSED:
        return "本期康复交接已完成"
    return STATE_LABELS.get(state, state)


# ---------------------------------------------------------------------------
# 机构视图（仅交接双方，仅照护必需资料）
# ---------------------------------------------------------------------------

def institution_view(
    store: Store, case_id: str, institution_id: str
) -> dict[str, Any]:
    case = _case(store, case_id)
    parties = {case["source_institution"], case["receiving_institution"]}
    offered = store.query(
        "SELECT institution FROM offers WHERE case_id = ?", (case_id,)
    )
    parties.update(row["institution"] for row in offered)
    if institution_id not in parties:
        raise DomainError("非交接双方机构，无权查看该案例")

    plan_events = store.events_for(events.AGG_RECOVERY_PLAN, case_id)
    summary: dict = {}
    for event in reversed(plan_events):
        if event["payload"].get("summary") is not None:
            summary = event["payload"]["summary"]
            break

    responsibility = _current_responsibility(store, case_id)
    followups = store.query(
        "SELECT followup_id, occurred_at, outcome FROM followups"
        " WHERE case_id = ? ORDER BY occurred_at",
        (case_id,),
    )
    disruptions = store.query(
        "SELECT disruption_id, kind, status, reported_at, deadline_at, resolved_at"
        " FROM disruptions WHERE case_id = ? ORDER BY reported_at",
        (case_id,),
    )
    return {
        "case_id": case_id,
        "state": case["state"],
        "patient_ref": case["patient_ref"],
        "plan_version": case["plan_version"],
        "medication_version": case["medication_version"],
        "recheck_version": case["recheck_version"],
        "clinical_summary": summary,
        "responsible": {
            "institution": responsibility["institution"],
            "person": responsibility["person"],
            "since": responsibility["started_at"],
        }
        if responsibility
        else None,
        "expected_arrival": {
            "start": case["expected_arrival_start"],
            "end": case["expected_arrival_end"],
        },
        "followups": followups,
        "disruptions": disruptions,
    }


# ---------------------------------------------------------------------------
# 质控视图
# ---------------------------------------------------------------------------

def responsibility_gaps(
    store: Store, case_id: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """识别责任空档：台账断档、可服务日期过期未到达、无在任责任方。"""
    case = _case(store, case_id)
    current = _now(now)
    entries = store.query(
        "SELECT * FROM resp_ledger WHERE case_id = ? ORDER BY started_at",
        (case_id,),
    )
    gaps: list[dict[str, Any]] = []
    for previous, following in zip(entries, entries[1:]):
        if previous["ended_at"] and previous["ended_at"] < following["started_at"]:
            gaps.append(
                {
                    "from": previous["ended_at"],
                    "to": following["started_at"],
                    "reason": "责任台账断档",
                }
            )
    arrival = store.one(
        "SELECT occurred_at FROM events WHERE event_type = ?"
        " AND json_extract(payload, '$.case_id') = ?",
        (events.ARRIVAL_CONFIRMED, case_id),
    )
    arrival_at = arrival["occurred_at"] if arrival else None
    for entry in entries:
        if not entry["serviceable_to"]:
            continue
        # 基层承诺的可服务日期过期时，患者既未到达、责任也未移交他人
        expiry = parse_time(entry["serviceable_to"])
        cutoffs = [parse_time(t) for t in (arrival_at, entry["ended_at"]) if t]
        cutoff = min(cutoffs) if cutoffs else None
        if cutoff is None:
            if expiry < current:
                gaps.append(
                    {
                        "from": entry["serviceable_to"],
                        "to": None,
                        "reason": "可服务日期已过，患者未到达，责任归属待明确",
                    }
                )
        elif expiry < cutoff:
            gaps.append(
                {
                    "from": entry["serviceable_to"],
                    "to": cutoff.isoformat(),
                    "reason": "可服务日期已过才到达或移交，期间责任归属待明确",
                }
            )
    if case["state"] not in (CLOSED,) and not any(
        entry["ended_at"] is None for entry in entries
    ):
        gaps.append({"from": case["updated_at"], "to": None, "reason": "无在任责任方"})
    return gaps


def qc_view(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """全部在办案例的时限与空档扫描，供质控人员巡查。"""
    current = _now(now)
    report = []
    cases = store.query(
        "SELECT * FROM cases WHERE state NOT IN (?, ?) ORDER BY created_at",
        (CLOSED, REESCALATED),
    )
    for case in cases:
        flags: list[str] = []
        if case["state"] == OFFERED:
            offer = store.one(
                "SELECT * FROM offers WHERE offer_id = ?", (case["current_offer_id"],)
            )
            deadline = parse_time(offer["created_at"]) + timedelta(
                hours=policies.OFFER_RESPONSE_HOURS
            )
            if current > deadline:
                flags.append("要约响应超时")
        if case["state"] == ACCEPTED and case["expected_arrival_end"]:
            deadline = parse_time(case["expected_arrival_end"]) + timedelta(
                hours=policies.ARRIVAL_GRACE_HOURS
            )
            if current > deadline:
                flags.append("到达逾期，建议按患者未到上报")
        overdue_disruptions = [
            row["disruption_id"]
            for row in _open_disruptions(store, case["case_id"])
            if current > parse_time(row["deadline_at"])
        ]
        if overdue_disruptions:
            flags.append(f"异常处置超时：{'、'.join(overdue_disruptions)}")
        undelivered = store.one(
            "SELECT COUNT(*) AS n FROM notices WHERE case_id = ? AND delivered_at IS NULL",
            (case["case_id"],),
        )["n"]
        if undelivered:
            flags.append(f"{undelivered} 条修订通知未送达")
        gaps = responsibility_gaps(store, case["case_id"], current)
        if gaps:
            flags.append("存在责任空档")
        report.append(
            {
                "case_id": case["case_id"],
                "state": case["state"],
                "state_label": STATE_LABELS[case["state"]],
                "source_institution": case["source_institution"],
                "receiving_institution": case["receiving_institution"],
                "flags": flags,
                "responsibility_gaps": gaps,
            }
        )
    return report


def reescalation_dossier(
    store: Store, reescalation_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """从一次重新上转还原交接、随访、异常与处理时限（仅流程元数据）。"""
    current = _now(now)
    reescalation = store.one(
        "SELECT * FROM reescalations WHERE reescalation_id = ?", (reescalation_id,)
    )
    if not reescalation:
        raise DomainError(f"重新上转记录不存在：{reescalation_id}")
    case_id = reescalation["case_id"]

    offers = [
        decode(row, "required_capabilities")
        for row in store.query(
            "SELECT offer_id, institution, status, required_capabilities,"
            " created_at, responded_at FROM offers WHERE case_id = ?"
            " ORDER BY created_at",
            (case_id,),
        )
    ]
    responsibilities = store.query(
        "SELECT responsibility_id, institution, person, serviceable_from,"
        " serviceable_to, accepted_at FROM responsibilities WHERE case_id = ?",
        (case_id,),
    )
    ledger = store.query(
        "SELECT institution, person, reason, started_at, ended_at, serviceable_to"
        " FROM resp_ledger WHERE case_id = ? ORDER BY started_at",
        (case_id,),
    )
    followups = store.query(
        "SELECT followup_id, occurred_at, outcome FROM followups"
        " WHERE case_id = ? ORDER BY occurred_at",
        (case_id,),
    )
    disruptions = []
    for row in store.query(
        "SELECT disruption_id, kind, status, reported_at, deadline_at, resolved_at"
        " FROM disruptions WHERE case_id = ? ORDER BY reported_at",
        (case_id,),
    ):
        resolved_at = row["resolved_at"]
        row["within_deadline"] = (
            parse_time(resolved_at) <= parse_time(row["deadline_at"])
            if resolved_at
            else current <= parse_time(row["deadline_at"])
        )
        row["kind_label"] = policies.DISRUPTION_LABELS[row["kind"]]
        disruptions.append(row)
    notices = store.query(
        "SELECT notice_id, institution, plan_version, created_at, delivered_at"
        " FROM notices WHERE case_id = ? ORDER BY created_at",
        (case_id,),
    )
    plan_versions = [
        {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "occurred_at": event["occurred_at"],
            "medication_version": event["payload"].get("medication_version"),
            "recheck_version": event["payload"].get("recheck_version"),
        }
        for event in store.events_for(events.AGG_RECOVERY_PLAN, case_id)
    ]
    return {
        "reescalation": reescalation,
        "case_id": case_id,
        "handoff": {
            "offers": offers,
            "responsibilities": responsibilities,
            "responsibility_ledger": ledger,
        },
        "followups": followups,
        "disruptions": disruptions,
        "plan_versions": plan_versions,
        "revision_notices": notices,
    }
