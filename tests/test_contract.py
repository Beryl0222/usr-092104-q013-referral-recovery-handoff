import json
import unittest
from pathlib import Path

from src import events
from src.validator import validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        path = ROOT / "data" / "sample.json"
        self.assertEqual(validate_event(json.loads(path.read_text(encoding="utf-8"))), [])

    def test_sample_flow_events_match_envelope(self) -> None:
        path = ROOT / "data" / "sample_flow.json"
        records = json.loads(path.read_text(encoding="utf-8"))
        for record in records:
            self.assertEqual(validate_event(record), [], record["event_id"])
            self.assertIn(record["event_type"], events.EVENT_TYPES)
            self.assertIn(record["aggregate_type"], events.AGGREGATE_TYPES)

    def test_code_enums_match_schema(self) -> None:
        """代码注册表与 JSON schema 两份清单不得漂移。"""
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(events.EVENT_TYPES),
            sorted(schema["properties"]["event_type"]["enum"]),
        )
        self.assertEqual(
            sorted(events.AGGREGATE_TYPES),
            sorted(schema["properties"]["aggregate_type"]["enum"]),
        )

    def test_registered_events_cover_originally_registered(self) -> None:
        # 仓库最初登记的 5 个事件必须继续存在，保持事件兼容。
        for name in (
            "PLAN_SIGNED", "HANDOFF_OFFERED", "RESPONSIBILITY_ACCEPTED",
            "ARRIVAL_CONFIRMED", "CARE_REESCALATED",
        ):
            self.assertIn(name, events.EVENT_TYPES)
        for name in ("recovery_plan", "handoff_offer", "care_responsibility", "followup_result"):
            self.assertIn(name, events.AGGREGATE_TYPES)


if __name__ == "__main__":
    unittest.main()
