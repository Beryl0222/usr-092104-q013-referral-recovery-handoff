"""康复下转责任交接核心服务。

连续状态链：

    PLANNING（住院阶段）→ PLAN_SIGNED（医生签署转出计划）
        → OFFERED（交接要约）→ ACCEPTED（基层明确接受责任与可服务日期）
        → ARRIVED（患者到达）→ CLOSED（随访完成）
    任意已接受/已到达阶段 → REESCALATED（重新上转，上级医院接管）

责任不变量：
- 接受之前，原机构始终兜底（责任台账自 CASE_OPENED 起连续记录）；
- 接受之时责任移交基层，并记录责任人与可服务日期；
- 重新上转之时责任回到目标上级机构。

系统不变量：
- 系统不自行改变治疗方案：计划签署与修订仅限医生角色，药品不可得的
  结案必须引用医生决定，服务本身没有任何修改用药/复查内容的入口；
- 重复上报不生成两次交接：event_id 幂等、同一案例同时只允许一个待响应
  要约、同一要约只能被接受一次；
- 计划修订通知所有旧版持有者，送达须显式登记。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import events, policies
from .events import parse_time, validate_envelope
from .store import Store, decode

# 案例状态
PLANNING = "PLANNING"
PLAN_SIGNED = "PLAN_SIGNED"
OFFERED = "OFFERED"
ACCEPTED = "ACCEPTED"
ARRIVED = "ARRIVED"
REESCALATED = "REESCALATED"
CLOSED = "CLOSED"

STATE_LABELS = {
    PLANNING: "住院阶段",
    PLAN_SIGNED: "转出计划已签署",
    OFFERED: "等待基层确认接收",
    ACCEPTED: "基层已接受责任",
    ARRIVED: "患者已到达基层",
    REESCALATED: "已重新上转",
    CLOSED: "交接已完成",
}

ROLE_PHYSICIAN = "physician"


class DomainError(Exception):
    """领域规则冲突：非法状态迁移、越权操作、负载缺字段等。"""


@dataclass
class IngestResult:
    """一次事件接入的结果。"""

    event_id: str
    idempotent: bool  # True 表示重复接入，未产生新的状态变更
    case_id: str | None = None
    state: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _iso(value: datetime) -> str:
    return value.isoformat()


class HandoffService:
    """命令与事件接入统一入口。

    所有命令方法只负责构造符合仓库标识规范的事件信封并调用 ingest；
    外部系统上报的原始事件同样走 ingest，两条路径共享全部校验与幂等逻辑。
    """

    def __init__(
        self,
        store: Store,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 机构目录（参考数据，非领域事件）
    # ------------------------------------------------------------------

    def register_institution(
        self,
        institution_id: str,
        name: str,
        level: str,
        capabilities: list[str] | None = None,
        phone: str = "",
    ) -> None:
        """登记机构及其可服务能力，供要约时核对接收机构能力。"""
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO institutions (institution_id, name, level, capabilities, phone)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(institution_id) DO UPDATE SET"
                " name=excluded.name, level=excluded.level,"
                " capabilities=excluded.capabilities, phone=excluded.phone",
                (
                    institution_id,
                    name,
                    level,
                    json.dumps(capabilities or [], ensure_ascii=False),
                    phone,
                ),
            )

    # ------------------------------------------------------------------
    # 命令：每个命令构造一个事件并接入
    # ------------------------------------------------------------------

    def open_case(
        self,
        event_id: str,
        case_id: str,
        source_institution: str,
        patient_ref: str,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """住院阶段开启交接案例，原机构自此刻起承担兜底责任。"""
        self._institution(source_institution)
        return self.ingest(
            self._envelope(
                event_id,
                events.CASE_OPENED,
                events.AGG_HANDOFF_CASE,
                case_id,
                occurred_at,
                "开启康复下转交接案例",
                {
                    "case_id": case_id,
                    "source_institution": source_institution,
                    "patient_ref": patient_ref,
                },
            )
        )

    def sign_plan(
        self,
        event_id: str,
        case_id: str,
        physician: str,
        clinical_summary: dict,
        medication_version: str,
        recheck_version: str,
        occurred_at: str | None = None,
        actor_role: str = ROLE_PHYSICIAN,
    ) -> IngestResult:
        """医生确认转出计划（含最少必要临床摘要与药物/复查版本）。"""
        self._require_physician(actor_role, "签署转出计划")
        extra = policies.check_minimal_summary(clinical_summary)
        if extra:
            raise DomainError(f"临床摘要超出最少必要范围：{'、'.join(extra)}")
        missing = [
            key
            for key in ("diagnosis", "medications", "recheck_items")
            if key not in clinical_summary
        ]
        if missing:
            raise DomainError(f"临床摘要缺少必要项：{'、'.join(missing)}")
        return self.ingest(
            self._envelope(
                event_id,
                events.PLAN_SIGNED,
                events.AGG_RECOVERY_PLAN,
                case_id,
                occurred_at,
                "医生签署康复转出计划",
                {
                    "case_id": case_id,
                    "physician": physician,
                    "summary": clinical_summary,
                    "medication_version": medication_version,
                    "recheck_version": recheck_version,
                },
            )
        )

    def revise_plan(
        self,
        event_id: str,
        case_id: str,
        physician: str,
        change_note: str,
        clinical_summary: dict | None = None,
        medication_version: str | None = None,
        recheck_version: str | None = None,
        occurred_at: str | None = None,
        actor_role: str = ROLE_PHYSICIAN,
    ) -> IngestResult:
        """医生修订计划；所有旧版持有者将收到通知并须登记送达。"""
        self._require_physician(actor_role, "修订转出计划")
        if not change_note:
            raise DomainError("修订必须说明变更原因")
        if clinical_summary is not None:
            extra = policies.check_minimal_summary(clinical_summary)
            if extra:
                raise DomainError(f"临床摘要超出最少必要范围：{'、'.join(extra)}")
        case = self._case(case_id)
        payload = {
            "case_id": case_id,
            "physician": physician,
            "change_note": change_note,
            "medication_version": medication_version or case["medication_version"],
            "recheck_version": recheck_version or case["recheck_version"],
        }
        if clinical_summary is not None:
            payload["summary"] = clinical_summary
        return self.ingest(
            self._envelope(
                event_id,
                events.PLAN_REVISED,
                events.AGG_RECOVERY_PLAN,
                case_id,
                occurred_at,
                f"医生修订转出计划：{change_note}",
                payload,
            )
        )

    def offer_handoff(
        self,
        event_id: str,
        offer_id: str,
        case_id: str,
        receiving_institution: str,
        expected_arrival_start: str,
        expected_arrival_end: str,
        required_capabilities: list[str] | None = None,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """向基层机构发出交接要约，附当前版计划的最少必要摘要。"""
        case = self._case(case_id)
        if case["source_institution"] == receiving_institution:
            raise DomainError("接收机构不能是原机构")
        self._institution(receiving_institution)
        start = parse_time(expected_arrival_start)
        end = parse_time(expected_arrival_end)
        if end < start:
            raise DomainError("预计到达窗口结束时间早于开始时间")
        return self.ingest(
            self._envelope(
                event_id,
                events.HANDOFF_OFFERED,
                events.AGG_HANDOFF_OFFER,
                offer_id,
                occurred_at,
                "向基层机构发出交接要约",
                {
                    "case_id": case_id,
                    "receiving_institution": receiving_institution,
                    "required_capabilities": required_capabilities or [],
                    "expected_arrival_start": _iso(start),
                    "expected_arrival_end": _iso(end),
                    "plan_version": case["plan_version"],
                    "medication_version": case["medication_version"],
                    "recheck_version": case["recheck_version"],
                    "summary": self._current_summary(case_id),
                },
            )
        )

    def accept(
        self,
        event_id: str,
        responsibility_id: str,
        offer_id: str,
        responsible_person: str,
        serviceable_from: str,
        serviceable_to: str,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """基层明确接受：指定责任人与可服务日期，责任自此刻移交。"""
        offer = self._offer(offer_id)
        start = parse_time(serviceable_from)
        end = parse_time(serviceable_to)
        if end < start:
            raise DomainError("可服务日期结束早于开始")
        return self.ingest(
            self._envelope(
                event_id,
                events.RESPONSIBILITY_ACCEPTED,
                events.AGG_CARE_RESPONSIBILITY,
                responsibility_id,
                occurred_at,
                "基层机构接受交接责任",
                {
                    "case_id": offer["case_id"],
                    "offer_id": offer_id,
                    "institution": offer["institution"],
                    "responsible_person": responsible_person,
                    "serviceable_from": _iso(start),
                    "serviceable_to": _iso(end),
                },
            )
        )

    def decline(
        self,
        event_id: str,
        offer_id: str,
        reason: str,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """基层谢绝要约；原机构继续兜底，可另择机构重新要约。"""
        offer = self._offer(offer_id)
        return self.ingest(
            self._envelope(
                event_id,
                events.HANDOFF_DECLINED,
                events.AGG_HANDOFF_OFFER,
                offer_id,
                occurred_at,
                "基层机构谢绝交接要约",
                {"case_id": offer["case_id"], "offer_id": offer_id, "reason": reason},
            )
        )

    def confirm_arrival(
        self,
        event_id: str,
        case_id: str,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """基层确认患者到达。"""
        case = self._case(case_id)
        return self.ingest(
            self._envelope(
                event_id,
                events.ARRIVAL_CONFIRMED,
                events.AGG_CARE_RESPONSIBILITY,
                case["responsibility_id"] or case_id,
                occurred_at,
                "患者已到达基层机构",
                {"case_id": case_id},
            )
        )

    def record_followup(
        self,
        event_id: str,
        followup_id: str,
        case_id: str,
        outcome: str,
        detail: dict | None = None,
        next_recheck_at: str | None = None,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """登记随访结果；outcome 为 completed 时自动结案。"""
        result = self.ingest(
            self._envelope(
                event_id,
                events.FOLLOWUP_RECORDED,
                events.AGG_FOLLOWUP_RESULT,
                followup_id,
                occurred_at,
                "登记随访结果",
                {
                    "case_id": case_id,
                    "outcome": outcome,
                    "detail": detail or {},
                    "next_recheck_at": next_recheck_at,
                },
            )
        )
        if outcome == "completed":
            # 结案事件可重试：上次若在随访落库后中断，这里补发结案
            row = self.store.one(
                "SELECT state FROM cases WHERE case_id = ?", (case_id,)
            )
            if row and row["state"] == ARRIVED:
                self.ingest(
                    self._envelope(
                        f"{event_id}-close",
                        events.CASE_CLOSED,
                        events.AGG_HANDOFF_CASE,
                        case_id,
                        occurred_at,
                        "随访完成，交接结案",
                        {"case_id": case_id},
                    )
                )
                result.state = CLOSED
        return result

    def report_disruption(
        self,
        event_id: str,
        disruption_id: str,
        case_id: str,
        kind: str,
        detail: dict | None = None,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """上报异常。四类异常走不同处置路径，时限见 policies。"""
        if kind not in policies.DISRUPTION_KINDS:
            raise DomainError(f"未登记的异常类型：{kind}")
        return self.ingest(
            self._envelope(
                event_id,
                events.DISRUPTION_REPORTED,
                events.AGG_HANDOFF_CASE,
                case_id,
                occurred_at,
                f"上报异常：{policies.DISRUPTION_LABELS[kind]}",
                {
                    "case_id": case_id,
                    "disruption_id": disruption_id,
                    "kind": kind,
                    "detail": detail or {},
                },
            )
        )

    def resolve_disruption(
        self,
        event_id: str,
        disruption_id: str,
        resolution: str,
        occurred_at: str | None = None,
        actor_role: str | None = None,
        **extra: Any,
    ) -> IngestResult:
        """异常结案。结案字段要求按异常类型区分（见 policies）。"""
        row = self._disruption(disruption_id)
        kind = row["kind"]
        missing = [
            key
            for key in policies.DISRUPTION_RESOLUTION_REQUIREMENTS[kind]
            if key not in extra
        ]
        if missing:
            raise DomainError(
                f"{policies.DISRUPTION_LABELS[kind]}结案缺少：{'、'.join(missing)}"
            )
        if kind == policies.MEDICATION_UNAVAILABLE:
            # 用药决定只能来自医生；系统不自行调整治疗方案
            self._require_physician(actor_role, "决定药品不可得处置")
        return self.ingest(
            self._envelope(
                event_id,
                events.DISRUPTION_RESOLVED,
                events.AGG_HANDOFF_CASE,
                row["case_id"],
                occurred_at,
                f"异常结案：{policies.DISRUPTION_LABELS[kind]}",
                {
                    "case_id": row["case_id"],
                    "disruption_id": disruption_id,
                    "kind": kind,
                    "resolution": resolution,
                    **extra,
                },
            )
        )

    def reescalate(
        self,
        event_id: str,
        reescalation_id: str,
        case_id: str,
        reason: str,
        target_institution: str | None = None,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """重新上转：责任移交目标上级机构（默认回原机构）。"""
        case = self._case(case_id)
        target = target_institution or case["source_institution"]
        self._institution(target)
        return self.ingest(
            self._envelope(
                event_id,
                events.CARE_REESCALATED,
                events.AGG_CARE_RESPONSIBILITY,
                case["responsibility_id"] or case_id,
                occurred_at,
                "患者重新上转",
                {
                    "case_id": case_id,
                    "reescalation_id": reescalation_id,
                    "reason": reason,
                    "target_institution": target,
                },
            )
        )

    def record_notice_delivery(
        self,
        event_id: str,
        notice_id: str,
        occurred_at: str | None = None,
    ) -> IngestResult:
        """登记计划修订通知送达；送达后该机构视为持有新版。"""
        notice = self._notice(notice_id)
        return self.ingest(
            self._envelope(
                event_id,
                events.NOTICE_DELIVERED,
                events.AGG_HANDOFF_CASE,
                notice["case_id"],
                occurred_at,
                "计划修订通知已送达",
                {
                    "case_id": notice["case_id"],
                    "notice_id": notice_id,
                    "institution": notice["institution"],
                    "plan_version": notice["plan_version"],
                },
            )
        )

    # ------------------------------------------------------------------
    # 事件接入：校验 → 幂等 → 版本 → 状态机 → 落库
    # ------------------------------------------------------------------

    def ingest(self, envelope: dict) -> IngestResult:
        errors = validate_envelope(envelope)
        if errors:
            raise DomainError("；".join(errors))
        if self.store.event_exists(envelope["event_id"]):
            return self._result(envelope, idempotent=True)
        expected = self.store.next_version(
            envelope["aggregate_type"], envelope["aggregate_id"]
        )
        if envelope["version"] != expected:
            raise DomainError(
                f"聚合 {envelope['aggregate_type']}/{envelope['aggregate_id']}"
                f" 下一版本应为 {expected}，收到 {envelope['version']}"
            )
        handler = getattr(self, f"_apply_{envelope['event_type']}")
        with self.store.transaction():
            handler(envelope)
            self.store.append_event(envelope)
        return self._result(envelope, idempotent=False)

    # ------------------------------------------------------------------
    # 事件处理器：状态机与投影更新
    # ------------------------------------------------------------------

    def _apply_CASE_OPENED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        if self.store.one("SELECT case_id FROM cases WHERE case_id = ?",
                          (payload["case_id"],)):
            raise DomainError(f"交接案例已存在：{payload['case_id']}")
        now = envelope["occurred_at"]
        self.store.execute(
            "INSERT INTO cases (case_id, state, patient_ref, source_institution,"
            " plan_version, medication_version, recheck_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 0, '', '', ?, ?)",
            (
                payload["case_id"],
                PLANNING,
                payload["patient_ref"],
                payload["source_institution"],
                now,
                now,
            ),
        )
        self._open_ledger(
            payload["case_id"],
            payload["source_institution"],
            None,
            "住院阶段·原机构负责",
            now,
        )

    def _apply_PLAN_SIGNED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, PLANNING)
        self.store.execute(
            "UPDATE cases SET state = ?, plan_version = 1, medication_version = ?,"
            " recheck_version = ?, updated_at = ? WHERE case_id = ?",
            (
                PLAN_SIGNED,
                payload["medication_version"],
                payload["recheck_version"],
                envelope["occurred_at"],
                case["case_id"],
            ),
        )
        self._distribute(
            case["case_id"],
            case["source_institution"],
            1,
            payload["medication_version"],
            payload["recheck_version"],
            envelope["occurred_at"],
        )

    def _apply_PLAN_REVISED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, PLAN_SIGNED, OFFERED, ACCEPTED, ARRIVED)
        new_version = case["plan_version"] + 1
        self.store.execute(
            "UPDATE cases SET plan_version = ?, medication_version = ?,"
            " recheck_version = ?, updated_at = ? WHERE case_id = ?",
            (
                new_version,
                payload["medication_version"],
                payload["recheck_version"],
                envelope["occurred_at"],
                case["case_id"],
            ),
        )
        # 原机构立即持有新版；其余旧版持有者生成送达待办
        self._distribute(
            case["case_id"],
            case["source_institution"],
            new_version,
            payload["medication_version"],
            payload["recheck_version"],
            envelope["occurred_at"],
        )
        holders = self.store.query(
            "SELECT institution, MAX(plan_version) AS held FROM distributions"
            " WHERE case_id = ? GROUP BY institution",
            (case["case_id"],),
        )
        for holder in holders:
            if holder["held"] >= new_version:
                continue
            self.store.execute(
                "INSERT INTO notices (notice_id, case_id, institution, plan_version,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    f"notice_{uuid.uuid4().hex[:12]}",
                    case["case_id"],
                    holder["institution"],
                    new_version,
                    envelope["occurred_at"],
                ),
            )

    def _apply_HANDOFF_OFFERED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, PLAN_SIGNED)
        open_offer = self.store.one(
            "SELECT offer_id FROM offers WHERE case_id = ? AND status = 'OPEN'",
            (case["case_id"],),
        )
        if open_offer:
            raise DomainError(
                f"已存在待响应的交接要约 {open_offer['offer_id']}，不得重复发起"
            )
        institution = self._institution(payload["receiving_institution"])
        missing = sorted(
            set(payload["required_capabilities"])
            - set(json.loads(institution["capabilities"]))
        )
        if missing:
            raise DomainError(
                f"接收机构能力不足，缺少：{'、'.join(missing)}"
            )
        self.store.execute(
            "INSERT INTO offers (offer_id, case_id, institution, status,"
            " required_capabilities, summary, created_at)"
            " VALUES (?, ?, ?, 'OPEN', ?, ?, ?)",
            (
                envelope["aggregate_id"],
                case["case_id"],
                payload["receiving_institution"],
                json.dumps(payload["required_capabilities"], ensure_ascii=False),
                json.dumps(payload["summary"], ensure_ascii=False),
                envelope["occurred_at"],
            ),
        )
        self.store.execute(
            "UPDATE cases SET state = ?, current_offer_id = ?,"
            " expected_arrival_start = ?, expected_arrival_end = ?, updated_at = ?"
            " WHERE case_id = ?",
            (
                OFFERED,
                envelope["aggregate_id"],
                payload["expected_arrival_start"],
                payload["expected_arrival_end"],
                envelope["occurred_at"],
                case["case_id"],
            ),
        )
        # 要约即把当前版计划摘要送达接收机构
        self._distribute(
            case["case_id"],
            payload["receiving_institution"],
            payload["plan_version"],
            payload["medication_version"],
            payload["recheck_version"],
            envelope["occurred_at"],
        )

    def _apply_HANDOFF_DECLINED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        offer = self._offer(payload["offer_id"])
        if offer["status"] != "OPEN":
            raise DomainError(f"要约 {payload['offer_id']} 已响应，不能重复谢绝")
        self.store.execute(
            "UPDATE offers SET status = 'DECLINED', responded_at = ? WHERE offer_id = ?",
            (envelope["occurred_at"], payload["offer_id"]),
        )
        self.store.execute(
            "UPDATE cases SET state = ?, current_offer_id = NULL,"
            " expected_arrival_start = NULL, expected_arrival_end = NULL, updated_at = ?"
            " WHERE case_id = ?",
            (PLAN_SIGNED, envelope["occurred_at"], offer["case_id"]),
        )

    def _apply_RESPONSIBILITY_ACCEPTED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, OFFERED)
        offer = self._offer(payload["offer_id"])
        if offer["status"] != "OPEN":
            raise DomainError(f"要约 {payload['offer_id']} 已响应，不能重复接受")
        if offer["offer_id"] != case["current_offer_id"]:
            raise DomainError("只能接受当前待响应的要约")
        if payload["institution"] != offer["institution"]:
            raise DomainError("接受机构与要约机构不一致")
        self.store.execute(
            "INSERT INTO responsibilities (responsibility_id, case_id, offer_id,"
            " institution, person, serviceable_from, serviceable_to, accepted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope["aggregate_id"],
                case["case_id"],
                offer["offer_id"],
                payload["institution"],
                payload["responsible_person"],
                payload["serviceable_from"],
                payload["serviceable_to"],
                envelope["occurred_at"],
            ),
        )
        self.store.execute(
            "UPDATE offers SET status = 'ACCEPTED', responded_at = ? WHERE offer_id = ?",
            (envelope["occurred_at"], offer["offer_id"]),
        )
        self.store.execute(
            "UPDATE cases SET state = ?, responsibility_id = ?,"
            " receiving_institution = ?, updated_at = ? WHERE case_id = ?",
            (
                ACCEPTED,
                envelope["aggregate_id"],
                payload["institution"],
                envelope["occurred_at"],
                case["case_id"],
            ),
        )
        # 责任移交：原机构兜底结束，基层责任开始
        self._close_ledger(case["case_id"], envelope["occurred_at"])
        self._open_ledger(
            case["case_id"],
            payload["institution"],
            payload["responsible_person"],
            "基层已接受责任",
            envelope["occurred_at"],
            serviceable_to=payload["serviceable_to"],
        )

    def _apply_ARRIVAL_CONFIRMED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, ACCEPTED)
        if envelope["aggregate_id"] != case["responsibility_id"]:
            raise DomainError("到达确认未挂在当前责任聚合上")
        self.store.execute(
            "UPDATE cases SET state = ?, updated_at = ? WHERE case_id = ?",
            (ARRIVED, envelope["occurred_at"], case["case_id"]),
        )

    def _apply_FOLLOWUP_RECORDED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, ARRIVED)
        self.store.execute(
            "INSERT INTO followups (followup_id, case_id, occurred_at, outcome, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                envelope["aggregate_id"],
                case["case_id"],
                envelope["occurred_at"],
                payload["outcome"],
                json.dumps(payload["detail"], ensure_ascii=False),
            ),
        )

    def _apply_CASE_CLOSED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, ARRIVED)
        self.store.execute(
            "UPDATE cases SET state = ?, updated_at = ? WHERE case_id = ?",
            (CLOSED, envelope["occurred_at"], case["case_id"]),
        )
        self._close_ledger(case["case_id"], envelope["occurred_at"])

    def _apply_DISRUPTION_REPORTED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        kind = payload["kind"]
        allowed = {
            policies.NO_SHOW: (ACCEPTED,),
            policies.ROUTE_BLOCKED: (ACCEPTED,),
            policies.MEDICATION_UNAVAILABLE: (ACCEPTED, ARRIVED),
            policies.SYMPTOM_WORSENED: (ACCEPTED, ARRIVED),
        }
        self._require_state(case, *allowed[kind])
        existing = self.store.one(
            "SELECT disruption_id FROM disruptions"
            " WHERE case_id = ? AND kind = ? AND status = 'OPEN'",
            (case["case_id"], kind),
        )
        if existing:
            raise DomainError(
                f"该异常已在处置中：{existing['disruption_id']}，重复上报不再生成"
            )
        reported_at = parse_time(envelope["occurred_at"])
        deadline = policies.disruption_deadline(kind, reported_at)
        self.store.execute(
            "INSERT INTO disruptions (disruption_id, case_id, kind, status,"
            " reported_at, deadline_at, detail)"
            " VALUES (?, ?, ?, 'OPEN', ?, ?, ?)",
            (
                payload["disruption_id"],
                case["case_id"],
                kind,
                envelope["occurred_at"],
                _iso(deadline),
                json.dumps(payload["detail"], ensure_ascii=False),
            ),
        )

    def _apply_DISRUPTION_RESOLVED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        row = self._disruption(payload["disruption_id"])
        if row["status"] != "OPEN":
            raise DomainError(f"异常 {payload['disruption_id']} 已结案或已随上转结束")
        record = {"note": payload["resolution"]}
        for key in policies.DISRUPTION_RESOLUTION_REQUIREMENTS[row["kind"]]:
            record[key] = payload[key]
        self.store.execute(
            "UPDATE disruptions SET status = 'RESOLVED', resolved_at = ?,"
            " resolution = ? WHERE disruption_id = ?",
            (
                envelope["occurred_at"],
                json.dumps(record, ensure_ascii=False),
                payload["disruption_id"],
            ),
        )
        # 道路阻断结案须给出新的预计到达时间
        if row["kind"] == policies.ROUTE_BLOCKED:
            self.store.execute(
                "UPDATE cases SET expected_arrival_end = ?, updated_at = ?"
                " WHERE case_id = ?",
                (
                    payload["new_expected_arrival_end"],
                    envelope["occurred_at"],
                    row["case_id"],
                ),
            )

    def _apply_CARE_REESCALATED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        case = self._case(payload["case_id"])
        self._require_state(case, ACCEPTED, ARRIVED)
        if envelope["aggregate_id"] != case["responsibility_id"]:
            raise DomainError("重新上转未挂在当前责任聚合上")
        self.store.execute(
            "INSERT INTO reescalations (reescalation_id, case_id, reason,"
            " target_institution, occurred_at) VALUES (?, ?, ?, ?, ?)",
            (
                payload["reescalation_id"],
                case["case_id"],
                payload["reason"],
                payload["target_institution"],
                envelope["occurred_at"],
            ),
        )
        self.store.execute(
            "UPDATE cases SET state = ?, updated_at = ? WHERE case_id = ?",
            (REESCALATED, envelope["occurred_at"], case["case_id"]),
        )
        # 未结异常随上转一并关闭，保留时限记录供质控核对
        self.store.execute(
            "UPDATE disruptions SET status = 'ESCALATED', resolved_at = ?"
            " WHERE case_id = ? AND status = 'OPEN'",
            (envelope["occurred_at"], case["case_id"]),
        )
        self._close_ledger(case["case_id"], envelope["occurred_at"])
        self._open_ledger(
            case["case_id"],
            payload["target_institution"],
            None,
            "重新上转·上级医院接管",
            envelope["occurred_at"],
        )

    def _apply_NOTICE_DELIVERED(self, envelope: dict) -> None:
        payload = envelope["payload"]
        notice = self._notice(payload["notice_id"])
        if notice["delivered_at"]:
            raise DomainError(f"通知 {payload['notice_id']} 已登记送达，请勿重复")
        self.store.execute(
            "UPDATE notices SET delivered_at = ? WHERE notice_id = ?",
            (envelope["occurred_at"], payload["notice_id"]),
        )
        med, recheck = self._versions_at_plan(
            payload["case_id"], notice["plan_version"]
        )
        self._distribute(
            payload["case_id"],
            notice["institution"],
            notice["plan_version"],
            med,
            recheck,
            envelope["occurred_at"],
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _envelope(
        self,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str | None,
        summary: str,
        payload: dict,
    ) -> dict:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or _iso(self.clock()),
            "version": self.store.next_version(aggregate_type, aggregate_id),
            "summary": summary,
            "payload": payload,
        }

    def _result(self, envelope: dict, idempotent: bool) -> IngestResult:
        case_id = envelope["payload"].get("case_id")
        if case_id is None and envelope["aggregate_type"] in (
            events.AGG_RECOVERY_PLAN,
            events.AGG_HANDOFF_CASE,
        ):
            case_id = envelope["aggregate_id"]
        state = None
        if case_id:
            row = self.store.one(
                "SELECT state FROM cases WHERE case_id = ?", (case_id,)
            )
            state = row["state"] if row else None
        return IngestResult(
            event_id=envelope["event_id"],
            idempotent=idempotent,
            case_id=case_id,
            state=state,
        )

    def _case(self, case_id: str) -> dict:
        row = self.store.one("SELECT * FROM cases WHERE case_id = ?", (case_id,))
        if not row:
            raise DomainError(f"交接案例不存在：{case_id}")
        return row

    def _offer(self, offer_id: str) -> dict:
        row = decode(
            self.store.one("SELECT * FROM offers WHERE offer_id = ?", (offer_id,)),
            "required_capabilities",
            "summary",
        )
        if not row:
            raise DomainError(f"交接要约不存在：{offer_id}")
        return row

    def _disruption(self, disruption_id: str) -> dict:
        row = self.store.one(
            "SELECT * FROM disruptions WHERE disruption_id = ?", (disruption_id,)
        )
        if not row:
            raise DomainError(f"异常记录不存在：{disruption_id}")
        return row

    def _notice(self, notice_id: str) -> dict:
        row = self.store.one(
            "SELECT * FROM notices WHERE notice_id = ?", (notice_id,)
        )
        if not row:
            raise DomainError(f"修订通知不存在：{notice_id}")
        return row

    def _institution(self, institution_id: str) -> dict:
        row = self.store.one(
            "SELECT * FROM institutions WHERE institution_id = ?", (institution_id,)
        )
        if not row:
            raise DomainError(f"机构未登记：{institution_id}")
        return row

    @staticmethod
    def _require_state(case: dict, *states: str) -> None:
        if case["state"] not in states:
            labels = "、".join(STATE_LABELS[s] for s in states)
            raise DomainError(
                f"案例 {case['case_id']} 当前为「{STATE_LABELS[case['state']]}」，"
                f"该操作要求处于：{labels}"
            )

    @staticmethod
    def _require_physician(actor_role: str | None, action: str) -> None:
        if actor_role != ROLE_PHYSICIAN:
            raise DomainError(f"只有医生可以{action}")

    def _open_ledger(
        self,
        case_id: str,
        institution: str,
        person: str | None,
        reason: str,
        started_at: str,
        serviceable_to: str | None = None,
    ) -> None:
        self.store.execute(
            "INSERT INTO resp_ledger (case_id, institution, person, reason,"
            " started_at, serviceable_to) VALUES (?, ?, ?, ?, ?, ?)",
            (case_id, institution, person, reason, started_at, serviceable_to),
        )

    def _close_ledger(self, case_id: str, ended_at: str) -> None:
        self.store.execute(
            "UPDATE resp_ledger SET ended_at = ? WHERE case_id = ? AND ended_at IS NULL",
            (ended_at, case_id),
        )

    def _distribute(
        self,
        case_id: str,
        institution: str,
        plan_version: int,
        medication_version: str,
        recheck_version: str,
        delivered_at: str,
    ) -> None:
        self.store.execute(
            "INSERT INTO distributions (case_id, institution, plan_version,"
            " medication_version, recheck_version, delivered_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                case_id,
                institution,
                plan_version,
                medication_version,
                recheck_version,
                delivered_at,
            ),
        )

    def _current_summary(self, case_id: str) -> dict:
        """最近一次签署/修订中的临床摘要。"""
        for event in reversed(
            self.store.events_for(events.AGG_RECOVERY_PLAN, case_id)
        ):
            summary = event["payload"].get("summary")
            if summary is not None:
                return summary
        return {}

    def _versions_at_plan(self, case_id: str, plan_version: int) -> tuple[str, str]:
        """某一计划版本对应的药物/复查版本。"""
        version = 0
        for event in self.store.events_for(events.AGG_RECOVERY_PLAN, case_id):
            if event["event_type"] in (events.PLAN_SIGNED, events.PLAN_REVISED):
                version += 1
                if version == plan_version:
                    return (
                        event["payload"]["medication_version"],
                        event["payload"]["recheck_version"],
                    )
        raise DomainError(f"计划版本不存在：{case_id} v{plan_version}")
