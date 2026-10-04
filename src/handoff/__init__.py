"""康复下转责任交接后端服务。

在仓库既有事件契约（contracts/domain.schema.json）之上提供：

- 连续状态机：住院阶段 → 转出计划签署 → 交接要约 → 责任接受 →
  患者到达 → 随访 → 关闭或重新上转；
- 事件接入：统一信封、event_id 幂等、聚合内版本递增；
- 责任台账：接受前原机构兜底，接受后基层机构承担，全程无空档可审计；
- 三类视图：患者与家属、交接双方机构（仅照护必需资料）、质控人员。
"""

from .events import AGGREGATE_TYPES, EVENT_TYPES, validate_envelope
from .service import DomainError, HandoffService
from .store import Store

__all__ = [
    "AGGREGATE_TYPES",
    "EVENT_TYPES",
    "DomainError",
    "HandoffService",
    "Store",
    "validate_envelope",
]
