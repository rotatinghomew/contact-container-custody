import json
import unittest
from datetime import datetime
from pathlib import Path


class SourceRecordsTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "data" / "source_records.json"
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def instant(self, value):
        instant = datetime.fromisoformat(value)
        self.assertIsNotNone(instant.utcoffset())
        return instant

    def test_source_documents_agree(self):
        old, new = self.data["labels"][:2]
        self.assertEqual((old["issuer"], old["mark"]), (new["issuer"], new["mark"]))
        self.assertNotEqual(old["shell"], new["shell"])
        wash, revocation = self.data["wash"]
        self.assertLess(set(revocation["coverage"]), set(wash["coverage"]))
        change = self.data["parts"][0]
        self.assertIn(change["replaced"], revocation["coverage"])
        self.assertNotIn(change["actual_part"], wash["coverage"])
        self.assertEqual({x["shell"] for x in self.data["scans"] if x["handoff"] == "D32"}, {"V12", "V13"})

