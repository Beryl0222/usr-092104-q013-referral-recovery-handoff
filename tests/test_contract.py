import json
import unittest
from pathlib import Path

from src.validator import validate_event


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        path = Path(__file__).parents[1] / "data" / "sample.json"
        self.assertEqual(validate_event(json.loads(path.read_text(encoding="utf-8"))), [])


if __name__ == "__main__":
    unittest.main()
