import tempfile
import unittest
from pathlib import Path

from pxq_monitor.db import Database
from pxq_monitor.service import WatchService, extract_show_id, parse_plan_numbers


class ServiceHelpersTests(unittest.TestCase):
    def test_extract_show_id_from_link(self) -> None:
        show_id = "0123456789abcdef01234567"
        self.assertEqual(extract_show_id(f"https://example.test/show/{show_id}"), show_id)

    def test_parse_plan_numbers_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(parse_plan_numbers("3，1,3", 3), [1, 3])

    def test_parse_plan_numbers_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_plan_numbers("1,4", 3)


class WatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "monitor.db")
        self.service = WatchService(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_add_watch_is_idempotent_for_same_session(self) -> None:
        first_id, first_created = self.service.add_watch(
            "show-1", "演出", "session-1", 10, set()
        )
        second_id, second_created = self.service.add_watch(
            "show-1", "演出", "session-1", 60, {"plan-1"}
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.db.list_watches()), 1)
        self.assertEqual(self.db.get_watch(first_id)["interval_sec"], 15)


if __name__ == "__main__":
    unittest.main()
