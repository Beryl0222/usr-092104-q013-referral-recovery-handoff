import json
import unittest
from pathlib import Path

from src.handoff import events
from src.validator import validate_event

ROOT = Path(__file__).parents[1]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        path = ROOT / "data" / "sample.json"
        self.assertEqual(validate_event(json.loads(path.read_text(encoding="utf-8"))), [])

    def test_registered_events_kept_compatible(self) -> None:
        """已注册事件不得改名或删除，契约枚举与代码目录一致。"""
        schema = json.loads(
            (ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )
        event_enum = set(schema["properties"]["event_type"]["enum"])
        aggregate_enum = set(schema["properties"]["aggregate_type"]["enum"])
        for registered in (
            "PLAN_SIGNED",
            "HANDOFF_OFFERED",
            "RESPONSIBILITY_ACCEPTED",
            "ARRIVAL_CONFIRMED",
            "CARE_REESCALATED",
        ):
            self.assertIn(registered, event_enum)
        self.assertEqual(event_enum, set(events.EVENT_TYPES))
        self.assertEqual(aggregate_enum, set(events.AGGREGATE_TYPES))

    def test_sample_passes_extended_validation(self) -> None:
        path = ROOT / "data" / "sample.json"
        self.assertEqual(
            events.validate_envelope(json.loads(path.read_text(encoding="utf-8"))),
            [],
        )


if __name__ == "__main__":
    unittest.main()
