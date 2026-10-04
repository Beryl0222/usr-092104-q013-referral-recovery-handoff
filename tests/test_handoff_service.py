"""核心流程：状态机、责任移交、幂等、异常路径、修订通知。"""

import unittest
from datetime import datetime, timedelta, timezone

from src.handoff import events, policies
from src.handoff.service import (
    ACCEPTED,
    ARRIVED,
    CLOSED,
    OFFERED,
    PLAN_SIGNED,
    PLANNING,
    REESCALATED,
    DomainError,
    HandoffService,
)
from src.handoff.store import Store

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 20, 8, 0, tzinfo=TZ)

SUMMARY = {
    "diagnosis": "脑梗死后遗症，右侧肢体活动障碍",
    "condition_summary": "生命体征平稳，可床边坐立",
    "allergies": "青霉素过敏",
    "medications": [{"name": "阿司匹林", "dose": "100mg", "freq": "qd"}],
    "recheck_items": ["血压每日两次", "两周后复查凝血"],
    "rehab_notes": "每日床旁康复训练 30 分钟",
    "warning_signs": "突发剧烈头痛、意识模糊",
}


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.moment = [T0]
        self.service = HandoffService(self.store, clock=lambda: self.moment[0])
        self.service.register_institution(
            "county-1", "县人民医院", "county", phone="0571-1111"
        )
        self.service.register_institution(
            "town-1",
            "河东镇卫生院",
            "township",
            capabilities=["rehab_nursing", "pharmacy_common"],
            phone="0571-2222",
        )
        self.service.register_institution(
            "town-2", "河西镇卫生院", "township", capabilities=["rehab_nursing"]
        )

    def tearDown(self) -> None:
        self.store.close()

    # ---- 流程辅助 ----

    def advance(self, hours: float) -> None:
        self.moment[0] += timedelta(hours=hours)

    def open_and_sign(self, case_id: str = "plan-001") -> None:
        self.service.open_case(f"ev-open-{case_id}", case_id, "county-1", "pat-001")
        self.service.sign_plan(
            f"ev-sign-{case_id}",
            case_id,
            physician="王医生",
            clinical_summary=dict(SUMMARY),
            medication_version="med-v1",
            recheck_version="chk-v1",
        )

    def offer(self, case_id: str = "plan-001", offer_id: str = "offer-001") -> None:
        self.service.offer_handoff(
            f"ev-offer-{offer_id}",
            offer_id,
            case_id,
            "town-1",
            expected_arrival_start="2026-09-22T08:00:00+08:00",
            expected_arrival_end="2026-09-23T08:00:00+08:00",
            required_capabilities=["rehab_nursing"],
        )

    def accept(
        self,
        offer_id: str = "offer-001",
        responsibility_id: str = "resp-001",
        serviceable_to: str = "2026-10-20T08:00:00+08:00",
    ) -> None:
        self.service.accept(
            f"ev-accept-{responsibility_id}",
            responsibility_id,
            offer_id,
            responsible_person="李护士",
            serviceable_from="2026-09-22T08:00:00+08:00",
            serviceable_to=serviceable_to,
        )

    def to_accepted(self) -> None:
        self.open_and_sign()
        self.offer()
        self.accept()

    def case(self, case_id: str = "plan-001") -> dict:
        return self.store.one("SELECT * FROM cases WHERE case_id = ?", (case_id,))

    # ---- 主流程与责任移交 ----

    def test_full_flow_transfers_responsibility(self) -> None:
        self.open_and_sign()
        self.assertEqual(self.case()["state"], PLAN_SIGNED)
        self.offer()
        self.assertEqual(self.case()["state"], OFFERED)
        # 接受之前原机构兜底
        ledger = self.store.query(
            "SELECT * FROM resp_ledger WHERE case_id = 'plan-001'"
        )
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["institution"], "county-1")
        self.assertIsNone(ledger[0]["ended_at"])

        self.accept()
        self.assertEqual(self.case()["state"], ACCEPTED)
        ledger = self.store.query(
            "SELECT * FROM resp_ledger WHERE case_id = 'plan-001'"
            " ORDER BY started_at"
        )
        self.assertEqual(len(ledger), 2)
        self.assertIsNotNone(ledger[0]["ended_at"])  # 兜底结束
        self.assertEqual(ledger[1]["institution"], "town-1")
        self.assertEqual(ledger[1]["person"], "李护士")
        self.assertEqual(ledger[1]["serviceable_to"], "2026-10-20T08:00:00+08:00")

        self.service.confirm_arrival("ev-arrive-1", "plan-001")
        self.assertEqual(self.case()["state"], ARRIVED)
        self.service.record_followup(
            "ev-fu-1", "fu-1", "plan-001", outcome="stable",
            detail={"note": "训练耐受良好"},
        )
        self.assertEqual(self.case()["state"], ARRIVED)
        result = self.service.record_followup(
            "ev-fu-2", "fu-2", "plan-001", outcome="completed"
        )
        self.assertEqual(result.state, CLOSED)
        self.assertEqual(self.case()["state"], CLOSED)

    def test_state_machine_rejects_illegal_transitions(self) -> None:
        self.open_and_sign()
        with self.assertRaises(DomainError):
            self.service.confirm_arrival("ev-x1", "plan-001")  # 未接受不能到达
        with self.assertRaises(DomainError):
            self.service.accept(  # 未要约不能接受
                "ev-x2", "resp-x", "offer-001", "李护士",
                "2026-09-22T08:00:00+08:00", "2026-10-20T08:00:00+08:00",
            )
        self.offer()
        with self.assertRaises(DomainError):
            self.service.record_followup("ev-x3", "fu-x", "plan-001", "stable")

    # ---- 幂等与重复上报 ----

    def test_same_event_id_is_idempotent(self) -> None:
        self.open_and_sign()
        first = self.service.ingest(
            {
                "event_id": "ev-offer-dup",
                "event_type": events.HANDOFF_OFFERED,
                "aggregate_type": "handoff_offer",
                "aggregate_id": "offer-dup",
                "occurred_at": "2026-09-20T12:00:00+08:00",
                "version": 1,
                "summary": "向基层机构发出交接要约",
                "payload": {
                    "case_id": "plan-001",
                    "receiving_institution": "town-1",
                    "required_capabilities": [],
                    "expected_arrival_start": "2026-09-22T08:00:00+08:00",
                    "expected_arrival_end": "2026-09-23T08:00:00+08:00",
                    "plan_version": 1,
                    "medication_version": "med-v1",
                    "recheck_version": "chk-v1",
                    "summary": SUMMARY,
                },
            }
        )
        self.assertFalse(first.idempotent)
        stored = self.store.events_for("handoff_offer", "offer-dup")[0]
        again = self.service.ingest(stored)
        self.assertTrue(again.idempotent)
        offers = self.store.query("SELECT * FROM offers WHERE case_id = 'plan-001'")
        self.assertEqual(len(offers), 1)  # 没有生成第二次交接

    def test_duplicate_offer_and_accept_rejected(self) -> None:
        self.open_and_sign()
        self.offer()
        # 已有待响应要约时再次要约，被状态机拦下，不会生成第二次交接
        with self.assertRaisesRegex(DomainError, "该操作要求处于"):
            self.service.offer_handoff(
                "ev-offer-2", "offer-002", "plan-001", "town-1",
                "2026-09-22T08:00:00+08:00", "2026-09-23T08:00:00+08:00",
            )
        self.accept()
        with self.assertRaisesRegex(DomainError, "不能重复接受|该操作要求处于"):
            self.service.accept(
                "ev-accept-2", "resp-002", "offer-001", "赵护士",
                "2026-09-22T08:00:00+08:00", "2026-10-20T08:00:00+08:00",
            )
        responsibilities = self.store.query("SELECT * FROM responsibilities")
        self.assertEqual(len(responsibilities), 1)

    def test_version_must_be_sequential(self) -> None:
        self.service.open_case("ev-open-1", "plan-001", "county-1", "pat-001")
        with self.assertRaisesRegex(DomainError, "下一版本应为 2"):
            self.service.ingest(
                {
                    "event_id": "ev-bad-version",
                    "event_type": events.DISRUPTION_REPORTED,
                    "aggregate_type": "handoff_case",
                    "aggregate_id": "plan-001",
                    "occurred_at": "2026-09-20T09:00:00+08:00",
                    "version": 5,
                    "summary": "版本错乱的事件",
                    "payload": {"case_id": "plan-001", "disruption_id": "d-x",
                                "kind": "no_show", "detail": {}},
                }
            )

    def test_unknown_event_type_rejected(self) -> None:
        with self.assertRaisesRegex(DomainError, "未登记的事件类型"):
            self.service.ingest(
                {
                    "event_id": "ev-unknown",
                    "event_type": "PLAN_DELETED",
                    "aggregate_type": "recovery_plan",
                    "aggregate_id": "plan-001",
                    "occurred_at": "2026-09-20T09:00:00+08:00",
                    "version": 1,
                    "summary": "未登记事件",
                    "payload": {},
                }
            )

    # ---- 系统不得自行改变治疗方案 ----

    def test_only_physician_signs_or_revises(self) -> None:
        self.service.open_case("ev-open-1", "plan-001", "county-1", "pat-001")
        with self.assertRaisesRegex(DomainError, "只有医生"):
            self.service.sign_plan(
                "ev-sign-1", "plan-001", "王医生", dict(SUMMARY),
                "med-v1", "chk-v1", actor_role="nurse",
            )
        self.open_and_sign("plan-002")
        with self.assertRaisesRegex(DomainError, "只有医生"):
            self.service.revise_plan(
                "ev-rev-1", "plan-002", "王医生", "调整用药",
                actor_role="system",
            )

    def test_summary_limited_to_care_necessary_fields(self) -> None:
        self.service.open_case("ev-open-1", "plan-001", "county-1", "pat-001")
        with self.assertRaisesRegex(DomainError, "最少必要"):
            self.service.sign_plan(
                "ev-sign-1", "plan-001", "王医生",
                {**SUMMARY, "id_number": "3301...", "income": "..."},
                "med-v1", "chk-v1",
            )

    def test_medication_unavailable_requires_physician_decision(self) -> None:
        self.to_accepted()
        self.service.confirm_arrival("ev-arrive-1", "plan-001")
        self.service.report_disruption(
            "ev-dis-1", "dis-1", "plan-001", policies.MEDICATION_UNAVAILABLE,
            detail={"drug": "阿司匹林"},
        )
        with self.assertRaisesRegex(DomainError, "只有医生"):
            self.service.resolve_disruption(
                "ev-res-1", "dis-1", "改用替代药",
                physician_decision="王医生决定维持原药，县医院配送",
                actor_role="pharmacist",
            )
        self.service.resolve_disruption(
            "ev-res-2", "dis-1", "维持原方案，县医院药房每周配送",
            physician_decision="王医生 2026-09-24 决定",
            actor_role="physician",
        )
        row = self.store.one("SELECT * FROM disruptions WHERE disruption_id = 'dis-1'")
        self.assertEqual(row["status"], "RESOLVED")
        # 系统未改动用药版本
        self.assertEqual(self.case()["medication_version"], "med-v1")

    # ---- 接收机构能力与谢绝 ----

    def test_offer_checks_receiving_capability(self) -> None:
        self.open_and_sign()
        with self.assertRaisesRegex(DomainError, "能力不足"):
            self.service.offer_handoff(
                "ev-offer-1", "offer-001", "plan-001", "town-2",
                "2026-09-22T08:00:00+08:00", "2026-09-23T08:00:00+08:00",
                required_capabilities=["pharmacy_common"],
            )

    def test_decline_keeps_fallback_and_allows_reoffer(self) -> None:
        self.open_and_sign()
        self.offer()
        self.service.decline("ev-decline-1", "offer-001", "床位不足")
        self.assertEqual(self.case()["state"], PLAN_SIGNED)
        ledger = self.store.query("SELECT * FROM resp_ledger WHERE ended_at IS NULL")
        self.assertEqual(ledger[0]["institution"], "county-1")  # 原机构继续兜底
        self.service.offer_handoff(
            "ev-offer-3", "offer-003", "plan-001", "town-1",
            "2026-09-25T08:00:00+08:00", "2026-09-26T08:00:00+08:00",
        )
        self.assertEqual(self.case()["state"], OFFERED)

    # ---- 计划修订通知 ----

    def test_revision_notifies_stale_holders_and_tracks_delivery(self) -> None:
        self.open_and_sign()
        self.offer()  # 基层随要约持有 v1
        self.service.revise_plan(
            "ev-rev-1", "plan-001", "王医生", "调整复查项目",
            recheck_version="chk-v2",
        )
        notices = self.store.query("SELECT * FROM notices WHERE case_id = 'plan-001'")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["institution"], "town-1")
        self.assertEqual(notices[0]["plan_version"], 2)
        self.assertIsNone(notices[0]["delivered_at"])

        self.service.record_notice_delivery("ev-nd-1", notices[0]["notice_id"])
        delivered = self.store.one(
            "SELECT * FROM notices WHERE notice_id = ?", (notices[0]["notice_id"],)
        )
        self.assertIsNotNone(delivered["delivered_at"])
        held = self.store.one(
            "SELECT MAX(plan_version) AS v FROM distributions"
            " WHERE case_id = 'plan-001' AND institution = 'town-1'"
        )
        self.assertEqual(held["v"], 2)
        with self.assertRaisesRegex(DomainError, "已登记送达"):
            self.service.record_notice_delivery("ev-nd-2", notices[0]["notice_id"])

    # ---- 四类异常的不同处置路径 ----

    def test_no_show_path(self) -> None:
        self.to_accepted()
        self.service.report_disruption(
            "ev-dis-1", "dis-1", "plan-001", policies.NO_SHOW,
            detail={"expected": "2026-09-23"},
        )
        row = self.store.one("SELECT * FROM disruptions WHERE disruption_id = 'dis-1'")
        deadline = datetime.fromisoformat(row["deadline_at"])
        reported = datetime.fromisoformat(row["reported_at"])
        self.assertEqual((deadline - reported), timedelta(hours=24))
        with self.assertRaisesRegex(DomainError, "重复上报"):
            self.service.report_disruption(
                "ev-dis-2", "dis-2", "plan-001", policies.NO_SHOW
            )
        with self.assertRaisesRegex(DomainError, "located_outcome"):
            self.service.resolve_disruption("ev-res-1", "dis-1", "已找到患者")
        self.service.resolve_disruption(
            "ev-res-2", "dis-1", "已找到患者，约定次日到达",
            located_outcome="患者家中休养，9-24 到达",
        )

    def test_route_blocked_path_extends_arrival_window(self) -> None:
        self.to_accepted()
        self.service.report_disruption(
            "ev-dis-1", "dis-1", "plan-001", policies.ROUTE_BLOCKED,
            detail={"road": "省道 S201 塌方"},
        )
        row = self.store.one("SELECT * FROM disruptions WHERE disruption_id = 'dis-1'")
        deadline = datetime.fromisoformat(row["deadline_at"])
        reported = datetime.fromisoformat(row["reported_at"])
        self.assertEqual((deadline - reported), timedelta(hours=12))
        self.service.resolve_disruption(
            "ev-res-1", "dis-1", "改道县道 X304",
            new_expected_arrival_end="2026-09-25T08:00:00+08:00",
        )
        self.assertEqual(
            self.case()["expected_arrival_end"], "2026-09-25T08:00:00+08:00"
        )

    def test_symptom_worsened_fast_path_and_reescalation(self) -> None:
        self.to_accepted()
        self.service.confirm_arrival("ev-arrive-1", "plan-001")
        self.service.report_disruption(
            "ev-dis-1", "dis-1", "plan-001", policies.SYMPTOM_WORSENED,
            detail={"symptom": "突发剧烈头痛"},
        )
        row = self.store.one("SELECT * FROM disruptions WHERE disruption_id = 'dis-1'")
        deadline = datetime.fromisoformat(row["deadline_at"])
        reported = datetime.fromisoformat(row["reported_at"])
        self.assertEqual((deadline - reported), timedelta(hours=2))
        self.service.reescalate(
            "ev-esc-1", "esc-1", "plan-001", "症状恶化，CT 提示新发出血"
        )
        self.assertEqual(self.case()["state"], REESCALATED)
        row = self.store.one("SELECT * FROM disruptions WHERE disruption_id = 'dis-1'")
        self.assertEqual(row["status"], "ESCALATED")
        ledger = self.store.query(
            "SELECT * FROM resp_ledger WHERE ended_at IS NULL"
        )
        self.assertEqual(ledger[0]["institution"], "county-1")  # 上级医院接管

    def test_disruption_state_restrictions(self) -> None:
        self.open_and_sign()
        with self.assertRaises(DomainError):  # 未接受前不存在“患者未到”
            self.service.report_disruption(
                "ev-dis-1", "dis-1", "plan-001", policies.NO_SHOW
            )


if __name__ == "__main__":
    unittest.main()
