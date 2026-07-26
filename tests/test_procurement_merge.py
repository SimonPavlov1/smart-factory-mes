import unittest

from app.services.workflow_service import _deduplicate_shortage_lines


class ProcurementMergeTest(unittest.TestCase):
    def test_duplicate_shortage_lines_are_not_counted_twice(self):
        source = {
            "line_uid": "1:4:3",
            "order_item_id": 1,
            "component_id": 4,
            "shortage_qty": 160,
            "qty": 160,
        }

        result = _deduplicate_shortage_lines([source, dict(source)])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["shortage_qty"], 160)
        self.assertEqual(result[0]["qty"], 160)

    def test_distinct_product_lines_are_preserved(self):
        result = _deduplicate_shortage_lines([
            {"line_uid": "1:4:0", "order_item_id": 1, "component_id": 4, "shortage_qty": 10},
            {"line_uid": "2:4:0", "order_item_id": 2, "component_id": 4, "shortage_qty": 20},
        ])

        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
