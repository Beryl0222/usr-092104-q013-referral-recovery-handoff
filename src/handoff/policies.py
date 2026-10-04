"""处置时限与数据最小化策略。

四类异常采用不同处置路径，时限与结案要求各不相同；此处集中登记，
便于质控核对“处理时限”，也便于后续按医联体制度调整。
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 异常类型
NO_SHOW = "no_show"  # 患者未到
ROUTE_BLOCKED = "route_blocked"  # 道路阻断
MEDICATION_UNAVAILABLE = "medication_unavailable"  # 药品不可得
SYMPTOM_WORSENED = "symptom_worsened"  # 症状恶化

DISRUPTION_KINDS = (NO_SHOW, ROUTE_BLOCKED, MEDICATION_UNAVAILABLE, SYMPTOM_WORSENED)

DISRUPTION_LABELS = {
    NO_SHOW: "患者未到",
    ROUTE_BLOCKED: "道路阻断",
    MEDICATION_UNAVAILABLE: "药品不可得",
    SYMPTOM_WORSENED: "症状恶化",
}

# 要约响应时限：基层需在此时限内明确接受或谢绝
OFFER_RESPONSE_HOURS = 24
# 到达宽限：预计到达窗口结束后超过该时长仍未确认到达，质控标记“到达逾期”
ARRIVAL_GRACE_HOURS = 12

# 各类异常的处置时限（小时）
DISRUPTION_DEADLINE_HOURS = {
    NO_SHOW: 24,  # 24 小时内定位患者并给出新到达安排
    ROUTE_BLOCKED: 12,  # 12 小时内重新规划送达并更新预计到达时间
    MEDICATION_UNAVAILABLE: 24,  # 24 小时内由开方医生给出决定
    SYMPTOM_WORSENED: 2,  # 2 小时内响应，通常进入重新上转快速通道
}

# 各类异常的结案要求：resolution 之外必须携带的字段
# - 药品不可得：必须记录开方医生的决定（physician_decision），系统不得自行改药；
# - 症状恶化：必须关联重新上转单（reescalation_id）或医生排除恶化的说明；
# - 道路阻断：必须给出新的预计到达时间；
# - 患者未到：必须给出定位结果。
DISRUPTION_RESOLUTION_REQUIREMENTS = {
    NO_SHOW: ("located_outcome",),
    ROUTE_BLOCKED: ("new_expected_arrival_end",),
    MEDICATION_UNAVAILABLE: ("physician_decision",),
    SYMPTOM_WORSENED: ("outcome_note",),
}

# 最少必要临床摘要允许携带的键白名单；白名单外的字段一律拒绝，
# 从写入侧保证两个机构之间只共享照护必需资料。
MINIMAL_SUMMARY_FIELDS = frozenset(
    {
        "diagnosis",  # 主要诊断
        "condition_summary",  # 当前病情摘要
        "allergies",  # 过敏史
        "medications",  # 康复用药（与 medication_version 对应）
        "recheck_items",  # 复查项目（与 recheck_version 对应）
        "rehab_notes",  # 康复注意事项
        "warning_signs",  # 需警惕并考虑上转的症状
    }
)

# 医联体统一求助入口（患者视图展示）
CONSORTIUM_HOTLINE = "医联体服务热线 400-000-0120"


def disruption_deadline(kind: str, reported_at: datetime) -> datetime:
    """按异常类型计算处置时限。"""
    return reported_at + timedelta(hours=DISRUPTION_DEADLINE_HOURS[kind])


def check_minimal_summary(summary: dict) -> list[str]:
    """校验临床摘要只含最少必要字段，返回越界字段列表。"""
    return sorted(key for key in summary if key not in MINIMAL_SUMMARY_FIELDS)
