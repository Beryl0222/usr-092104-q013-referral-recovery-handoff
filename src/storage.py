"""事件日志与幂等。

- 事件按 ``case_id`` 分组追加，聚合内 ``version`` 必须严格接续；
- ``event_id`` 全局唯一，重复 event_id 视为重复上报，静默返回已存事件，
  不会产生第二次交接；
- ``request_id`` 接入幂等键：同一命令用同一 request_id 重试只生效一次。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .events import Event


class DuplicateEvent(Exception):
    """event_id 已存在但两次内容不一致。"""


class ConcurrentVersionConflict(Exception):
    """聚合版本断号或重复。"""


class UnknownEventError(KeyError):
    pass


class EventStore:
    """按案例分区的内存事件日志（接口可替换为持久实现）。"""

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._by_event_id: dict[str, Event] = {}
        # 一个命令可能产生多个事件（如计划修订 + 批量送达），保留全部索引。
        self._by_request_id: dict[str, list[Event]] = {}
        self._seq = 0

    # ---- 写入 -------------------------------------------------------------

    def append(self, event: Event) -> Event:
        existing = self._by_event_id.get(event.event_id)
        if existing is not None:
            # 重复上报：内容一致即幂等忽略；内容不同则是编号冲突，明确报错。
            # seq 由存储层分配，不参与内容比较。
            if _content_key(existing) != _content_key(event):
                raise DuplicateEvent(event.event_id)
            return existing

        stream = self._events[event.case_id or ""]
        next_version = self._next_version(stream, event.aggregate_type, event.aggregate_id)
        if event.version != next_version:
            raise ConcurrentVersionConflict(
                f"{event.aggregate_type}/{event.aggregate_id} 版本 {event.version}，"
                f"期望 {next_version}"
            )
        self._seq += 1
        object.__setattr__(event, "seq", self._seq)
        stream.append(event)
        self._by_event_id[event.event_id] = event
        if event.request_id is not None:
            self._by_request_id.setdefault(event.request_id, []).append(event)
        return event

    @staticmethod
    def _next_version(stream: list[Event], aggregate_type: str, aggregate_id: str) -> int:
        versions = [
            e.version for e in stream
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]
        return (max(versions) + 1) if versions else 1

    # ---- 读取 -------------------------------------------------------------

    def events_for_case(self, case_id: str) -> list[Event]:
        return sorted(
            self._events.get(case_id, []),
            key=lambda e: (e.seq, e.occurred_at),
        )

    def events_for_aggregate(self, case_id: str, aggregate_type: str, aggregate_id: str) -> list[Event]:
        return [
            e for e in self.events_for_case(case_id)
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]

    def all_events(self) -> list[Event]:
        events: Iterable[Event] = (e for stream in self._events.values() for e in stream)
        return sorted(events, key=lambda e: (e.seq, e.occurred_at))

    def event_ids(self) -> set[str]:
        return set(self._by_event_id)

    def get_event(self, event_id: str) -> Event | None:
        return self._by_event_id.get(event_id)

    def get_by_request(self, request_id: str) -> Event | None:
        events = self._by_request_id.get(request_id)
        return events[0] if events else None

    def events_for_request(self, request_id: str) -> list[Event]:
        return list(self._by_request_id.get(request_id, []))

    def has_request(self, request_id: str) -> bool:
        return request_id in self._by_request_id

    def now(self) -> datetime:
        return datetime.now().astimezone()


def _content_key(event: Event) -> tuple:
    """除存储层序号外的事件内容，用于重复上报一致性比较。"""
    record = event.to_dict()
    record.pop("seq", None)
    return tuple(sorted(record.items()))
