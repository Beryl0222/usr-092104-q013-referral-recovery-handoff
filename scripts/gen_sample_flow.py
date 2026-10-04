"""生成 data/sample_flow.json：一条完整的康复下转责任交接联调样例。

事件全部经 HandoffService 产生，保证信封、版本与业务规则合法，可被投影直接回放。
重新生成：python3 scripts/gen_sample_flow.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from src import events as ev
from src.service import HandoffService, MED_ORIGIN_SUPPLIES

CASE_ID = "case-sample-001"


def main() -> None:
    clock = datetime(2026, 9, 20, 9, 0).astimezone()

    def tick(**kwargs) -> None:
        nonlocal clock
        clock += timedelta(**kwargs)

    svc = HandoffService(clock=lambda: clock)

    # 住院阶段：开案、医生签署 v1 转出计划。
    svc.open_case(
        patient_ref="张某某（联调样例）", patient_contact="138-0000-0000",
        origin_org="清溪县人民医院", origin_contact="县医院转诊办 0577-12345",
        help_phone="12320", case_id=CASE_ID,
    )
    tick(minutes=20)
    medications = [
        {"code": "M-ASP-001", "name": "阿司匹林肠溶片", "posology": "100mg qd"},
        {"code": "M-AML-002", "name": "苯磺酸氨氯地平片", "posology": "5mg qd"},
    ]
    review_items = [
        {"code": "R-BP-01", "name": "血压监测", "schedule": "每日两次",
         "threshold": "收缩压 >=180 触发上转评估"},
        {"code": "R-LF-02", "name": "肝肾功能", "schedule": "下转后第 14 天"},
    ]
    caps = [ev.CAPABILITY_MEDICATION, ev.CAPABILITY_REHAB, ev.CAPABILITY_REVIEW_ITEM]
    svc.sign_plan(
        CASE_ID, signed_by="神经内科 李医生",
        diagnosis_summary="脑梗死稳定期，右侧肢体肌力 4 级",
        rehab_summary="肢体功能训练 + 作业治疗，每日一次",
        precautions=["防跌倒", "血压控制目标 <140/90"],
        medications=medications, review_items=review_items, required_capabilities=caps,
    )

    # 首次邀约被拒（能力档期原因），拒绝方仍是旧版持有人。
    tick(hours=2)
    offer_a = svc.offer_handoff(
        CASE_ID, target_org="东岭乡卫生院",
        serviceable_from=clock + timedelta(days=1),
        expected_arrival_at=clock + timedelta(days=1, hours=3),
        target_capabilities=caps,
    )
    tick(hours=3)
    svc.decline_offer(CASE_ID, offer_a.aggregate_id,
                      reason="康复治疗师外出培训，两周内无法排期")

    # 第二次邀约：西河镇卫生院明确接受具体责任与可服务日期。
    tick(hours=1)
    offer_b = svc.offer_handoff(
        CASE_ID, target_org="西河镇卫生院",
        serviceable_from=clock + timedelta(days=1),
        expected_arrival_at=clock + timedelta(days=1, hours=2),
        target_capabilities=caps,
    )
    tick(hours=5)
    serviceable_from = datetime.fromisoformat(offer_b.payload["serviceable_from"])
    svc.accept_responsibility(
        CASE_ID, offer_id=offer_b.aggregate_id,
        contact_person="全科 王医生", contact_phone="139-5555-0001",
        serviceable_from=serviceable_from, scope_capabilities=caps,
        medication_codes=["M-ASP-001", "M-AML-002"],
        review_codes=["R-BP-01", "R-LF-02"],
    )

    # 患者到达。
    tick(days=1, hours=2)
    svc.confirm_arrival(CASE_ID)

    # 第一次随访：单项轻度异常，未越阈值。
    tick(days=2)
    fu1 = svc.schedule_followup(CASE_ID, due_at=clock + timedelta(days=1))
    tick(days=1)
    svc.record_followup(
        CASE_ID, fu1.aggregate_id, result=ev.FOLLOWUP_ABNORMAL,
        abnormal_items=[{"code": "R-LF-02", "value": "肌酐 132",
                         "threshold_exceeded": False}],
        note="轻度升高，按计划 2 周后复查",
    )

    # 药品不可得：走原机构供药路径，不自行换药。
    tick(hours=4)
    svc.report_medication_unavailable(
        CASE_ID, medication_code="M-AML-002", reported_by="王医生",
        disposition=MED_ORIGIN_SUPPLIES,
        note="镇药房本周缺货，由县医院慢病配送线每周二供药",
    )

    # 计划修订 v2：所有旧版持有人（含曾拒绝的东岭乡）均收到送达并确认。
    tick(days=3)
    svc.revise_plan(
        CASE_ID, signed_by="神经内科 李医生",
        diagnosis_summary="脑梗死稳定期，右侧肢体肌力 4 级",
        rehab_summary="增加平衡训练，每周不少于 3 次",
        precautions=["防跌倒", "血压控制目标 <140/90"],
        medications=medications, review_items=review_items, required_capabilities=caps,
        change_note="康复进展良好，强化平衡训练",
    )
    tick(hours=1)
    svc.acknowledge_plan_version(CASE_ID, "西河镇卫生院", 2)
    tick(hours=1)
    svc.acknowledge_plan_version(CASE_ID, "东岭乡卫生院", 2)

    # 第二次随访：症状恶化、越过计划阈值 → 重新上转，原机构限时接收。
    tick(days=6)
    fu2 = svc.schedule_followup(CASE_ID, due_at=clock)
    svc.record_followup(
        CASE_ID, fu2.aggregate_id, result=ev.FOLLOWUP_WORSENED,
        abnormal_items=[{"code": "R-BP-01", "value": "192/114",
                         "threshold_exceeded": True}],
        note="晨起见头痛伴呕吐，血压持续升高",
    )
    tick(minutes=15)
    svc.request_reescalation(
        CASE_ID, reason="血压骤升伴头痛呕吐，疑似病情反复",
        abnormal_item="R-BP-01", followup_id=fu2.aggregate_id,
    )
    tick(minutes=40)
    svc.receive_reescalation(CASE_ID, received_by="县医院急诊 赵医生",
                             note="120 接回，已收住神经内科")

    # 结案。
    tick(days=7)
    svc.close_case(CASE_ID, reason="再上转后县医院完成后续治疗，本次下转流程闭环")

    records = [e.to_dict() for e in svc.store.events_for_case(CASE_ID)]
    out = Path(__file__).parents[1] / "data" / "sample_flow.json"
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(records)} events -> {out}")


if __name__ == "__main__":
    main()
