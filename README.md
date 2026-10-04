# 康复下转责任交接

本仓库保存康复下转责任交接的领域词汇、事件约定与后端服务代码，供相关单位统一对象身份、事件顺序和版本语义。

## 目录

- `contracts/domain.schema.json`：领域事件信封与稳定枚举。
- `data/sample.json`：一条中文联调样例。
- `src/validator.py`：事件基础字段校验（稳定契约，保持不变）。
- `src/handoff/`：责任交接后端服务（状态机、存储、视图）。
- `tests/`：领域资料一致性检查与服务行为测试。

## 连续状态链

```
PLANNING（住院阶段）→ PLAN_SIGNED（医生签署转出计划）
    → OFFERED（交接要约）→ ACCEPTED（基层明确接受责任与可服务日期）
    → ARRIVED（患者到达）→ CLOSED（随访完成结案）
已接受/已到达阶段 → REESCALATED（重新上转，上级医院接管）
```

责任规则：接受之前原机构始终兜底；接受之时责任移交基层并记录责任人与
可服务日期；重新上转之时责任回到目标上级机构。责任台账（resp_ledger）
逐段记录“谁在什么时间负责”，质控可据此识别责任空档。

## 事件目录

已注册事件保持兼容（不得改名或删除）：PLAN_SIGNED、HANDOFF_OFFERED、
RESPONSIBILITY_ACCEPTED、ARRIVAL_CONFIRMED、CARE_REESCALATED。

本服务新增：CASE_OPENED、PLAN_REVISED、HANDOFF_DECLINED、
FOLLOWUP_RECORDED、DISRUPTION_REPORTED、DISRUPTION_RESOLVED、
NOTICE_DELIVERED、CASE_CLOSED。

聚合类型在既有 recovery_plan、handoff_offer、care_responsibility、
followup_result 之外新增 handoff_case（aggregate_id 与对应
recovery_plan 相同，承载案例级事件）。信封七要素与 version 递增语义
不变，事件接入统一走 `HandoffService.ingest`：

- `event_id` 唯一，重复接入幂等返回，不产生第二次状态变更；
- 同一聚合内 version 必须严格递增；
- 同一案例同时只允许一个待响应要约、同一要约只能被接受一次，
  重复上报不会生成两次交接。

## 系统不变量

- 系统不自行改变治疗方案：计划签署/修订仅限医生角色；药品不可得的
  结案必须引用医生决定；服务没有任何修改用药或复查内容的入口。
- 临床摘要在写入侧按最少必要白名单校验（见 `policies.MINIMAL_SUMMARY_FIELDS`），
  两个机构之间只共享照护必需资料。
- 计划修订自动向所有旧版持有者生成通知，送达须显式登记
  （NOTICE_DELIVERED），未送达的通知会在质控视图中挂起。

## 四类异常的不同处置路径

| 异常 | 时限 | 结案要求 |
| --- | --- | --- |
| 患者未到 no_show | 24h | 定位结果 located_outcome |
| 道路阻断 route_blocked | 12h | 新的预计到达时间（自动顺延到达窗口） |
| 药品不可得 medication_unavailable | 24h | 医生决定 physician_decision |
| 症状恶化 symptom_worsened | 2h | 处置说明，通常进入重新上转快速通道 |

同一案例同一类型的未结异常重复上报会被拒绝，不会生成第二张处置单。

## 三类视图（`src/handoff/views.py`）

- `patient_view`：患者与家属看到当前负责机构/责任人、下一节点与求助方式，
  不含临床细节；
- `institution_view`：仅交接双方机构可查，仅含照护必需资料；
- `qc_view` / `responsibility_gaps` / `reescalation_dossier`：质控识别
  责任空档、超时项与未送达通知，并可从一次重新上转还原交接、随访、
  异常与处理时限（仅流程元数据，不含临床内容）。

## 本地检查

```bash
python3 -m unittest discover -s tests
```
