"""患者/机构/质控三类视图：数据最小化、责任空档、上转还原。"""

import unittest
from datetime import datetime, timedelta, timezone

from src.handoff import policies, views
from src.handoff.service import DomainError, HandoffService
from src.handoff.store import Store

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 20, 8, 0, tzinfo=TZ)

SUMMARY = {
    "diagnosis": "脑梗死后遗症",
    "condition_summary": "生命体征平稳",
    "allergies": "青霉素过敏",
    "medications": [{"name": "阿司匹林", "dose": "100mg", "freq": "qd"}],
    "recheck_items": ["血压每日两次"],
    "rehab_notes": "每日床旁训练",
}


class ViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.moment = [T0]
        self.service = HandoffService(self.store, clock=lambda: self.moment[0])
        self.service.register_institution(
            "county-1", "县人民医院", "county", phone="0571-1111"
        )
        self.service.register_institution(
            "town-1", "河东镇卫生院", "township",
            capabilities=["rehab_nursing"], phone="0571-2222",
        )
        self.service.register_institution("city-1", "市医院", "city")

    def tearDown(self) -> None:
        self.store.close()

    def advance(self, hours: float) -> None:
        self.moment[0] += timedelta(hours=hours)

    def to_accepted(self, serviceable_to: str = "2026-10-20T08:00:00+08:00") -> None:
        self.service.open_case("ev-open-1", "plan-001", "county-1", "pat-001")
        self.service.sign_plan(
            "ev-sign-1", "plan-001", "王医生", dict(SUMMARY), "med-v1", "chk-v1"
        )
        self.service.offer_handoff(
            "ev-offer-1", "offer-001", "plan-001", "town-1",
            "2026-09-22T08:00:00+08:00", "2026-09-23T08:00:00+08:00",
        )
        self.service.accept(
            "ev-accept-1", "resp-001", "offer-001", "李护士",
            "2026-09-22T08:00:00+08:00", serviceable_to,
        )

    # ---- 患者与家属视图 ----

    def test_patient_view_shows_responsible_next_step_and_help(self) -> None:
        self.service.open_case("ev-open-1", "plan-001", "county-1", "pat-001")
        view = views.patient_view(self.store, "plan-001")
        self.assertEqual(view["responsible_institution"], "县人民医院")
        self.assertIn("住院", view["next_step"])
        self.assertIn(policies.CONSORTIUM_HOTLINE, view["help"])
        self.assertIn("0571-1111", view["help"][0])

        self.to_accepted()
        view = views.patient_view(self.store, "plan-001")
        self.assertEqual(view["responsible_institution"], "河东镇卫生院")
        self.assertEqual(view["responsible_person"], "李护士")
        self.assertIn("2026-09-22", view["next_step"])
        self.assertIn("河东镇卫生院", view["next_step"])
        # 患者视图不含临床细节
        self.assertNotIn("clinical_summary", view)
        self.assertNotIn("diagnosis", view)

    def test_patient_view_after_reescalation(self) -> None:
        self.to_accepted()
        self.service.confirm_arrival("ev-arrive-1", "plan-001")
        self.service.reescalate(
            "ev-esc-1", "esc-1", "plan-001", "症状恶化", target_institution="city-1"
        )
        view = views.patient_view(self.store, "plan-001")
        self.assertIn("市医院", view["next_step"])
        self.assertEqual(view["responsible_institution"], "市医院")

    # ---- 机构视图：仅双方、仅照护必需 ----

    def test_institution_view_limited_to_parties(self) -> None:
        self.to_accepted()
        view = views.institution_view(self.store, "plan-001", "town-1")
        self.assertEqual(view["clinical_summary"]["diagnosis"], "脑梗死后遗症")
        self.assertEqual(view["medication_version"], "med-v1")
        self.assertEqual(view["responsible"]["institution"], "town-1")
        with self.assertRaisesRegex(DomainError, "无权查看"):
            views.institution_view(self.store, "plan-001", "city-1")

    # ---- 质控视图 ----

    def test_qc_flags_overdue_offer_and_arrival(self) -> None:
        self.service.open_case("ev-open-1", "plan-001", "county-1", "pat-001")
        self.service.sign_plan(
            "ev-sign-1", "plan-001", "王医生", dict(SUMMARY), "med-v1", "chk-v1"
        )
        self.service.offer_handoff(
            "ev-offer-1", "offer-001", "plan-001", "town-1",
            "2026-09-22T08:00:00+08:00", "2026-09-23T08:00:00+08:00",
        )
        self.advance(25)  # 超过 24h 要约响应时限
        report = views.qc_view(self.store, now=self.moment[0])
        self.assertIn("要约响应超时", report[0]["flags"])

        self.service.accept(
            "ev-accept-1", "resp-001", "offer-001", "李护士",
            "2026-09-22T08:00:00+08:00", "2026-10-20T08:00:00+08:00",
        )
        self.advance(60)  # 累计 85h，超过预计到达窗口（9-23 08:00）+ 12h 宽限
        report = views.qc_view(self.store, now=self.moment[0])
        self.assertTrue(any("到达逾期" in flag for flag in report[0]["flags"]))

    def test_qc_flags_undelivered_notices(self) -> None:
        self.to_accepted()
        self.service.revise_plan(
            "ev-rev-1", "plan-001", "王医生", "调整复查", recheck_version="chk-v2"
        )
        report = views.qc_view(self.store, now=self.moment[0])
        self.assertTrue(any("未送达" in flag for flag in report[0]["flags"]))
        notice = self.store.one("SELECT * FROM notices WHERE case_id = 'plan-001'")
        self.service.record_notice_delivery("ev-nd-1", notice["notice_id"])
        report = views.qc_view(self.store, now=self.moment[0])
        self.assertFalse(any("未送达" in flag for flag in report[0]["flags"]))

    def test_responsibility_gap_when_serviceable_expired(self) -> None:
        # 基层承诺的可服务日期止于 9-24，患者始终未到达
        self.to_accepted(serviceable_to="2026-09-24T08:00:00+08:00")
        self.advance(24 * 5)
        gaps = views.responsibility_gaps(
            self.store, "plan-001", now=self.moment[0]
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("可服务日期已过", gaps[0]["reason"])
        self.assertIsNone(gaps[0]["to"])  # 空档仍在持续
        report = views.qc_view(self.store, now=self.moment[0])
        self.assertIn("存在责任空档", report[0]["flags"])

    def test_no_gap_in_normal_flow(self) -> None:
        self.to_accepted()
        self.service.confirm_arrival("ev-arrive-1", "plan-001")
        gaps = views.responsibility_gaps(
            self.store, "plan-001", now=self.moment[0]
        )
        self.assertEqual(gaps, [])

    # ---- 从一次重新上转还原 ----

    def test_reescalation_dossier_reconstructs_episode(self) -> None:
        self.to_accepted()
        self.service.confirm_arrival("ev-arrive-1", "plan-001")
        self.service.record_followup(
            "ev-fu-1", "fu-1", "plan-001", "stable", {"note": "耐受良好"}
        )
        self.service.revise_plan(
            "ev-rev-1", "plan-001", "王医生", "调整复查", recheck_version="chk-v2"
        )
        self.service.report_disruption(
            "ev-dis-1", "dis-1", "plan-001", policies.SYMPTOM_WORSENED,
            {"symptom": "剧烈头痛"},
        )
        self.advance(1)
        self.service.reescalate("ev-esc-1", "esc-1", "plan-001", "症状恶化")

        dossier = views.reescalation_dossier(
            self.store, "esc-1", now=self.moment[0]
        )
        self.assertEqual(dossier["case_id"], "plan-001")
        self.assertEqual(dossier["reescalation"]["reason"], "症状恶化")
        # 交接：要约、责任、台账都在
        self.assertEqual(len(dossier["handoff"]["offers"]), 1)
        self.assertEqual(
            dossier["handoff"]["responsibilities"][0]["person"], "李护士"
        )
        self.assertEqual(len(dossier["handoff"]["responsibility_ledger"]), 3)
        # 随访
        self.assertEqual(len(dossier["followups"]), 1)
        # 异常与处理时限：2h 时限内 1h 上转，未超时
        disruption = dossier["disruptions"][0]
        self.assertEqual(disruption["kind"], policies.SYMPTOM_WORSENED)
        self.assertEqual(disruption["status"], "ESCALATED")
        self.assertTrue(disruption["within_deadline"])
        # 计划版本与修订通知送达情况
        self.assertEqual(len(dossier["plan_versions"]), 2)
        self.assertEqual(len(dossier["revision_notices"]), 1)
        self.assertIsNone(dossier["revision_notices"][0]["delivered_at"])
        # 质控视图不含临床内容
        self.assertNotIn("clinical_summary", dossier)
        self.assertNotIn("summary", dossier["handoff"]["offers"][0])


if __name__ == "__main__":
    unittest.main()
