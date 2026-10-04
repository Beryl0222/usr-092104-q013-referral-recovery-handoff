"""康复下转责任交接后端领域测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from src import events as ev
from src.case_state import fold_case
from src.projections import ProjectionReader
from src.service import (
    DISPOSITION_REROUTE,
    MED_ORIGIN_SUPPLIES,
    MED_SUBSTITUTE_DOCTOR,
    DomainError,
    HandoffService,
)
from src.storage import ConcurrentVersionConflict, DuplicateEvent, EventStore

T0 = datetime(2026, 9, 20, 8, 0).astimezone()

MEDS = [
    {"code": "M1", "name": "阿司匹林", "posology": "100mg qd"},
    {"code": "M2", "name": "氨氯地平", "posology": "5mg qd"},
]
REVIEWS = [
    {"code": "R1", "name": "血压监测", "schedule": "每日"},
    {"code": "R2", "name": "肝肾功能", "schedule": "2 周后"},
]
CAPS = [ev.CAPABILITY_MEDICATION, ev.CAPABILITY_REHAB, ev.CAPABILITY_REVIEW_ITEM]


class HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = T0
        self.svc = HandoffService(clock=lambda: self.clock)
        self.reader = ProjectionReader(self.svc.store)
        self.case_id = self._open_case()

    def advance(self, **kwargs) -> datetime:
        self.clock = self.clock + timedelta(**kwargs)
        return self.clock

    def _open_case(self, case_id: str = "case-001") -> str:
        self.svc.open_case(
            patient_ref="张某某", patient_contact="13800000000",
            origin_org="县医院", origin_contact="县医院转诊办 0577-0000",
            help_phone="12320", case_id=case_id,
        )
        return case_id

    def _signed_plan(self) -> None:
        self.svc.sign_plan(
            self.case_id, signed_by="李医生",
            diagnosis_summary="脑梗死稳定期，右侧肢体肌力 4 级",
            rehab_summary="肢体功能训练 + 作业治疗",
            precautions=["防跌倒", "血压控制目标 <140/90"],
            medications=MEDS, review_items=REVIEWS, required_capabilities=CAPS,
        )

    def _offered(self, target: str = "西山乡卫生院"):
        return self.svc.offer_handoff(
            self.case_id, target_org=target,
            serviceable_from=self.clock + timedelta(days=1),
            expected_arrival_at=self.clock + timedelta(days=1, hours=2),
            target_capabilities=CAPS,
        )

    def _accepted(self, offer, target: str = "西山乡卫生院"):
        return self.svc.accept_responsibility(
            self.case_id, offer_id=offer.aggregate_id,
            contact_person="王医生", contact_phone="139-1111",
            serviceable_from=self.clock + timedelta(days=1),
            scope_capabilities=CAPS,
            medication_codes=["M1", "M2"], review_codes=["R1", "R2"],
        )

    def _full_handoff(self):
        self._signed_plan()
        offer = self._offered()
        self._accepted(offer)
        self.svc.confirm_arrival(self.case_id, arrived_at=self.clock + timedelta(days=1, hours=2))
        return offer


class TestPlanAndOffer(HandoffTestCase):
    def test_backstop_before_acceptance(self) -> None:
        self._signed_plan()
        self._offered()
        # 已邀约但未接受：原机构继续兜底。
        pv = self.reader.patient_view(self.case_id, at=self.clock)
        self.assertEqual(pv["responsible_now"]["org"], "县医院")
        self.assertIn("兜底", pv["responsibility_status"])

    def test_capability_gap_blocks_offer(self) -> None:
        self._signed_plan()
        with self.assertRaises(DomainError) as ctx:
            self.svc.offer_handoff(
                self.case_id, target_org="薄弱乡卫生院",
                target_capabilities=[ev.CAPABILITY_REHAB],
            )
        self.assertEqual(ctx.exception.code, "capability_gap")

    def test_acceptance_must_cover_concrete_scope(self) -> None:
        self._signed_plan()
        offer = self._offered()
        with self.assertRaises(DomainError) as ctx:
            self.svc.accept_responsibility(
                self.case_id, offer_id=offer.aggregate_id,
                contact_person="王医生", contact_phone="139-1111",
                serviceable_from=self.clock + timedelta(days=1),
                scope_capabilities=CAPS,
                medication_codes=["M1"],  # 漏 M2
                review_codes=["R1", "R2"],
            )
        self.assertEqual(ctx.exception.code, "scope_gap")
        # 未接受期间仍由原机构兜底。
        state = fold_case(self.svc.store.events_for_case(self.case_id))
        self.assertEqual(state.responsibility_at(self.clock)["org"], "县医院")

    def test_system_cannot_change_treatment(self) -> None:
        self._signed_plan()
        offer = self._offered()
        self._accepted(offer)
        # 药品不可得想替代用药，却不给医生确认：拒绝。
        with self.assertRaises(DomainError) as ctx:
            self.svc.report_medication_unavailable(
                self.case_id, medication_code="M2", reported_by="王医生",
                disposition=MED_SUBSTITUTE_DOCTOR,
                substitute={"code": "M9", "name": "其他降压药"},
            )
        self.assertEqual(ctx.exception.code, "doctor_confirmation_required")

    def test_duplicate_event_id_does_not_create_second_handoff(self) -> None:
        self._signed_plan()
        offer = self._offered()
        self._accepted(offer)
        before = len(self.svc.store.events_for_case(self.case_id))
        # 同一 event_id 重复上报到达：第二次被幂等吞掉。
        self.svc.confirm_arrival(self.case_id, arrived_at=self.clock, event_id="evt-arrival-1")
        dup = self.svc.confirm_arrival(self.case_id, arrived_at=self.clock, event_id="evt-arrival-1")
        self.assertEqual(dup.event_id, "evt-arrival-1")
        self.assertEqual(len(self.svc.store.events_for_case(self.case_id)), before + 1)
        with self.assertRaises(DomainError):
            self.svc.confirm_arrival(self.case_id)

    def test_request_id_idempotent_followup(self) -> None:
        self._full_handoff()
        fu = self.svc.schedule_followup(self.case_id, due_at=self.clock + timedelta(days=2))
        self.svc.record_followup(
            self.case_id, fu.aggregate_id, result=ev.FOLLOWUP_STABLE,
            request_id="req-fu-1",
        )
        with self.assertRaises(DomainError):
            # 重复登记随访结果必须被阻止，不会产生第二条结果。
            self.svc.record_followup(
                self.case_id, fu.aggregate_id, result=ev.FOLLOWUP_STABLE,
                request_id="req-fu-2",
            )

    def test_conflicting_event_id_rejected(self) -> None:
        store = EventStore()

        def mk(**overrides):
            kwargs = dict(
                event_id="e1", event_type=ev.CASE_OPENED, aggregate_type=ev.REFERRAL_CASE,
                aggregate_id="c1", occurred_at=T0, version=1, summary="开案",
                payload={"origin_org": "县医院"}, case_id="c1",
            )
            kwargs.update(overrides)
            return ev.Event(**kwargs)

        store.append(mk())
        with self.assertRaises(DuplicateEvent):
            store.append(mk(summary="被篡改的同号事件"))
        with self.assertRaises(ConcurrentVersionConflict):
            store.append(mk(event_id="e2", version=3))


class TestPlanRevision(HandoffTestCase):
    def test_revision_notifies_all_stale_holders(self) -> None:
        self._signed_plan()
        offer_a = self._offered("东山乡卫生院")
        # A 拒绝；A 已收过 v1，仍属于旧版持有人。
        self.svc.decline_offer(self.case_id, offer_a.aggregate_id, reason="康复设备检修")
        offer_b = self._offered("西山乡卫生院")
        self._accepted(offer_b, target="西山乡卫生院")
        # A 明确确认过 v1，B 接受责任时自动确认 v1。
        self.svc.acknowledge_plan_version(self.case_id, "东山乡卫生院", 1)

        revised = self.svc.revise_plan(
            self.case_id, signed_by="李医生",
            diagnosis_summary="脑梗死稳定期",
            rehab_summary="增加平衡训练", precautions=["防跌倒"],
            medications=MEDS, review_items=REVIEWS, required_capabilities=CAPS,
            change_note="复查周期调整",
        )
        types = [e.event_type for e in revised]
        self.assertEqual(types[0], ev.PLAN_REVISED)
        # 两个旧版持有人都应收到 v2 送达。
        holders = {e.payload["holder"] for e in revised if e.event_type == ev.PLAN_VERSION_DELIVERED}
        self.assertEqual(holders, {"东山乡卫生院", "西山乡卫生院"})

        state = fold_case(self.svc.store.events_for_case(self.case_id))
        stale = state.holders_of_stale_version()
        self.assertEqual(set(stale), {"东山乡卫生院", "西山乡卫生院"})

        qv = self.reader.quality_view(self.case_id, at=self.clock)
        stale_gap = next(g for g in qv["gaps"] if g["type"] == "stale_plan_in_use")
        self.assertEqual(stale_gap["latest_version"], 2)

        # B 确认新版后不再是旧版持有人。
        self.svc.acknowledge_plan_version(self.case_id, "西山乡卫生院", 2)
        state = fold_case(self.svc.store.events_for_case(self.case_id))
        self.assertNotIn("西山乡卫生院", state.holders_of_stale_version())

    def test_revision_idempotent_under_same_request_id(self) -> None:
        self._signed_plan()
        offer = self._offered()
        self._accepted(offer)
        n_before = len(self.svc.store.events_for_case(self.case_id))
        kw = dict(
            signed_by="李医生", diagnosis_summary="x", rehab_summary="y",
            precautions=[], medications=MEDS, review_items=REVIEWS,
            required_capabilities=CAPS, change_note="修订", request_id="rev-1",
        )
        first = self.svc.revise_plan(self.case_id, **kw)
        second = self.svc.revise_plan(self.case_id, **kw)
        self.assertEqual([e.event_id for e in first], [e.event_id for e in second])
        self.assertEqual(len(self.svc.store.events_for_case(self.case_id)) - n_before, len(first))


class TestExceptionPaths(HandoffTestCase):
    def test_patient_no_show_path(self) -> None:
        self._signed_plan()
        offer = self._offered()
        self._accepted(offer)
        inc = self.svc.report_no_show(self.case_id, reported_by="王医生", detail="过午未到")
        self.assertEqual(inc.event_type, ev.PATIENT_NO_SHOW)
        # 重复上报未到不允许生成第二次异常。
        with self.assertRaises(DomainError) as ctx:
            self.svc.report_no_show(self.case_id, reported_by="王医生")
        self.assertEqual(ctx.exception.code, "incident_open")
        # 未到走改期路径，与道路阻断/药品路径区分；改期后患者到达。
        self.svc.reschedule_arrival(
            self.case_id, new_expected_arrival_at=self.clock + timedelta(days=2),
            reason="患者家中有事",
        )
        self.svc.confirm_arrival(self.case_id, arrived_at=self.clock + timedelta(days=2))
        # 异常已随改期/到达了结，质控不再报“未处置超时”。
        qv = self.reader.quality_view(
            self.case_id, at=self.clock + timedelta(days=10)
        )
        self.assertFalse(
            [g for g in qv["gaps"] if g["type"] == "incident_unresolved_overdue"]
        )

    def test_road_blocked_keeps_origin_backstop_before_acceptance(self) -> None:
        self._signed_plan()
        offer = self._offered()
        # 阻断发生在转运途中、基层尚未接受：原机构兜底，处置为改道。
        blocked = self.svc.report_road_blocked(
            self.case_id, reported_by="转运司机", detail="塌方封路",
            disposition=DISPOSITION_REROUTE,
        )
        self.assertEqual(blocked.event_type, ev.TRANSFER_BLOCKED)
        state = fold_case(self.svc.store.events_for_case(self.case_id))
        self.assertEqual(state.responsibility_at(self.clock)["org"], "县医院")
        # 阻断不影响随后接受与到达。
        self._accepted(offer)
        self.svc.confirm_arrival(self.case_id, arrived_at=self.clock + timedelta(days=1))

    def test_medication_unavailable_origin_supplies(self) -> None:
        self._full_handoff()
        self.svc.report_medication_unavailable(
            self.case_id, medication_code="M2", reported_by="王医生",
            disposition=MED_ORIGIN_SUPPLIES, note="药房缺货，县医院每周配送",
        )
        ov = self.reader.org_view(self.case_id, "县医院")
        self.assertIn("M2", ov["medications_origin_supplies"])

    def test_medication_unavailable_requires_known_code(self) -> None:
        self._full_handoff()
        with self.assertRaises(DomainError):
            self.svc.report_medication_unavailable(
                self.case_id, medication_code="M-UNKNOWN",
                reported_by="王医生", disposition=MED_ORIGIN_SUPPLIES,
            )


class TestReescalation(HandoffTestCase):
    def _worsen_and_reescalate(self):
        offer = self._full_handoff()
        fu = self.svc.schedule_followup(self.case_id, due_at=self.clock + timedelta(days=3))
        self.advance(days=3)
        self.svc.record_followup(
            self.case_id, fu.aggregate_id, result=ev.FOLLOWUP_WORSENED,
            abnormal_items=[{"code": "R1", "value": "185/112", "threshold_exceeded": True}],
        )
        self.svc.request_reescalation(
            self.case_id, reason="血压骤升伴剧烈头痛",
            abnormal_item="R1", followup_id=fu.aggregate_id,
        )
        return offer, fu

    def test_responsibility_returns_only_on_receive(self) -> None:
        self._worsen_and_reescalate()
        state = fold_case(self.svc.store.events_for_case(self.case_id))
        # 已请求、县医院未接收：责任仍是基层（现场处置），但患者视图提示等待接收。
        current = state.responsibility_at(self.clock)
        self.assertEqual(current["org"], "西山乡卫生院")
        self.advance(hours=1)
        self.svc.receive_reescalation(self.case_id, received_by="县医院急诊赵医生")
        state = fold_case(self.svc.store.events_for_case(self.case_id))
        self.assertEqual(state.responsibility_at(self.clock)["org"], "县医院")

    def test_duplicate_reescalation_blocked(self) -> None:
        self._worsen_and_reescalate()
        with self.assertRaises(DomainError) as ctx:
            self.svc.request_reescalation(self.case_id, reason="再次申请")
        self.assertEqual(ctx.exception.code, "reescalation_open")

    def test_quality_trace_reconstructs_chain(self) -> None:
        self._worsen_and_reescalate()
        self.advance(hours=1)
        self.svc.receive_reescalation(self.case_id, received_by="赵医生")
        qv = self.reader.quality_view(self.case_id, at=self.clock)
        self.assertEqual(len(qv["reescalations"]), 1)
        trace = qv["reescalations"][0]
        stages = [t["stage"] for t in trace["timeline"]]
        self.assertEqual(
            stages,
            [
                "handoff_offer", "responsibility_accepted", "arrival",
                "followup", "reescalation_requested", "reescalation_received",
            ],
        )
        followup = next(t for t in trace["timeline"] if t["stage"] == "followup")
        self.assertEqual(followup["result"], ev.FOLLOWUP_WORSENED)
        self.assertTrue(trace["receive_within_sla"])
        # 建模不变量：任何时刻都没有责任空档。
        self.assertEqual(qv["responsibility_gap_windows"], [])

    def test_overdue_reescalation_flagged(self) -> None:
        self._worsen_and_reescalate()
        self.advance(hours=3)  # 超过 2 小时接收时限
        qv = self.reader.quality_view(self.case_id, at=self.clock)
        gap = next(g for g in qv["gaps"] if g["type"] == "reescalation_not_received")
        self.assertFalse(gap["within_sla"])
        self.assertEqual(gap["severity"], "high")


class TestFollowupSweep(HandoffTestCase):
    def test_overdue_sweep(self) -> None:
        self._full_handoff()
        self.svc.schedule_followup(self.case_id, due_at=T0 - timedelta(hours=1))
        marked = self.svc.sweep_followup_overdue(now=T0)
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0].event_type, ev.FOLLOWUP_OVERDUE)
        # 再扫一次不重复标记。
        self.assertEqual(self.svc.sweep_followup_overdue(now=T0), [])


class TestViews(HandoffTestCase):
    def test_patient_view_fields(self) -> None:
        self._full_handoff()
        pv = self.reader.patient_view(self.case_id, at=self.clock + timedelta(days=2))
        self.assertEqual(pv["responsible_now"]["org"], "西山乡卫生院")
        self.assertTrue(pv["help_phone"])
        self.assertTrue(pv["next_node"])

    def test_org_view_minimization(self) -> None:
        self._full_handoff()
        ov = self.reader.org_view(self.case_id, "西山乡卫生院")
        self.assertIn("current_plan_minimal", ov)  # 参与方可见照护必需摘要
        self.assertFalse(ov["stale_version_held"])
        with self.assertRaises(DomainError) as ctx:
            self.reader.org_view(self.case_id, "无关民营诊所")
        self.assertEqual(ctx.exception.code, "not_authorized")

    def test_org_view_warns_stale_plan(self) -> None:
        self._full_handoff()
        self.svc.revise_plan(
            self.case_id, signed_by="李医生",
            diagnosis_summary="x", rehab_summary="y", precautions=[],
            medications=MEDS, review_items=REVIEWS, required_capabilities=CAPS,
            change_note="调整",
        )
        ov = self.reader.org_view(self.case_id, "西山乡卫生院")
        self.assertTrue(ov["stale_version_held"])
        self.assertEqual(ov["latest_plan_version"], 2)


if __name__ == "__main__":
    unittest.main()
