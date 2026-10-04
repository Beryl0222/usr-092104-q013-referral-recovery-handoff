# 康复下转责任交接后端

医联体康复患者下转的责任交接领域后端。围绕**住院阶段 → 医生确认的转出计划 → 下转邀约 →
基层明确接受责任 → 患者到达 → 随访 → 异常处置 / 重新上转**建立连续状态，全程事件溯源，
解决“靠电话和便签确认、双方都以为对方负责、基层拿着旧版计划”的问题。

## 核心规则

- **系统不改变治疗方案**：诊断、用药、复查均来自医生签署的 `PLAN_SIGNED` / `PLAN_REVISED`；
  药品替代必须记录确认医生，否则只能走原机构供药。
- **责任无空档**：案例开启后原机构（县医院）始终兜底；基层明确接受**具体责任范围与可服务
  日期**后责任才自该日期起转移；重新上转须原机构明确接收后责任才落回。接收前与上转待接
  收期间责任方均有明确定义。
- **计划版本与送达**：修订版本号 +1，自动向**所有持有旧版的人**（含曾拒绝邀约者）送达新版；
  送达（`PLAN_VERSION_DELIVERED`）与确认（`PLAN_VERSION_ACKNOWLEDGED`）分别留痕，质控可识别
  仍按旧版执行者。
- **四条异常路径互不混淆**：患者未到（核实/改期/原机构追访）、道路阻断（改道/改期，阻断期
  原机构兜底）、药品不可得（原机构供药 / 医生确认替代 / 同目录解决）、症状恶化（重新上转）。
- **重复上报不产生第二次交接**：`event_id` 全局去重，`request_id` 命令幂等。
- **最小必要共享**：患者与家属只看当前责任方、下一节点、求助方式；机构之间只共享照护必需
  摘要，非参与方拒绝读取；质控可识别责任空档，并能从一次重新上转还原交接、随访、异常与处
  理时限。

## 目录

- `contracts/domain.schema.json`：领域事件信封、事件/聚合枚举（向后兼容，最初登记的 5 个
  事件与 4 类聚合保持不变）。
- `src/events.py`：事件类型、稳定枚举（能力、异常、随访分级、责任状态）与信封校验，与 schema
  互相校验不得漂移。
- `src/storage.py`：按案例分区的事件日志，`event_id` 去重、`request_id` 幂等、聚合版本号、
  因果序号 `seq`。
- `src/case_state.py`：事件流 → 案例连续状态的纯归约器（含 `responsibility_at(时刻)` 责任
  回放）。
- `src/service.py`：`HandoffService`，全部业务规则与状态机。
- `src/projections.py`：`ProjectionReader` 三类只读视图。
- `scripts/gen_sample_flow.py`：生成 `data/sample_flow.json` 联调样例（23 个事件的完整案例）。
- `tests/`：契约一致性与领域规则测试。

## 主要事件

| 阶段 | 事件 |
|---|---|
| 案例 | `CASE_OPENED` `CASE_CLOSED` |
| 计划版本 | `PLAN_SIGNED` `PLAN_REVISED` `PLAN_VERSION_DELIVERED` `PLAN_VERSION_ACKNOWLEDGED` |
| 交接邀约 | `HANDOFF_OFFERED` `HANDOFF_DECLINED` `HANDOFF_WITHDRAWN` |
| 责任与到达 | `RESPONSIBILITY_ACCEPTED` `ARRIVAL_CONFIRMED` `ARRIVAL_RESCHEDULED` |
| 异常 | `PATIENT_NO_SHOW` `TRANSFER_BLOCKED` `MEDICATION_UNAVAILABLE` |
| 随访 | `FOLLOWUP_SCHEDULED` `FOLLOWUP_RECORDED` `FOLLOWUP_OVERDUE` |
| 再上转 | `CARE_REESCALATED` `REESCALATION_RECEIVED` |

事件接入沿用仓库既有标识规范：`event_id` / `event_type` / `aggregate_type` /
`aggregate_id` / `occurred_at` / `version` / `summary` 为必填信封字段，业务字段在
`payload`，案例串联用 `case_id`，接入幂等用 `request_id`。

## 使用示例

```python
from datetime import datetime, timedelta
from src.service import HandoffService
from src.projections import ProjectionReader
from src import events as ev

svc = HandoffService()
svc.open_case(patient_ref="张某某", patient_contact="138…",
              origin_org="县医院", origin_contact="转诊办", help_phone="12320",
              case_id="case-001")
svc.sign_plan("case-001", signed_by="李医生", diagnosis_summary="…", rehab_summary="…",
              precautions=["防跌倒"],
              medications=[{"code": "M1", "name": "阿司匹林"}],
              review_items=[{"code": "R1", "name": "血压监测"}],
              required_capabilities=[ev.CAPABILITY_MEDICATION, ev.CAPABILITY_REHAB])
offer = svc.offer_handoff("case-001", target_org="西河镇卫生院",
                          target_capabilities=[ev.CAPABILITY_MEDICATION, ev.CAPABILITY_REHAB])
svc.accept_responsibility("case-001", offer_id=offer.aggregate_id,
                          contact_person="王医生", contact_phone="139…",
                          serviceable_from=datetime.now().astimezone() + timedelta(days=1),
                          scope_capabilities=[ev.CAPABILITY_MEDICATION, ev.CAPABILITY_REHAB],
                          medication_codes=["M1"], review_codes=["R1"])
svc.confirm_arrival("case-001")

reader = ProjectionReader(svc.store)
reader.patient_view("case-001")   # 当前责任方 / 下一节点 / 求助电话
reader.org_view("case-001", "西河镇卫生院")  # 最小必要临床摘要 + 版本持有情况
reader.quality_view("case-001")   # 责任空档、旧版计划、超时限、再上转全链路溯源
```

## 本地检查

```bash
python3 -m unittest discover -s tests
python3 scripts/gen_sample_flow.py   # 重新生成联调样例
```
