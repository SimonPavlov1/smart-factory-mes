import unittest
from datetime import date, timedelta

from fastapi import HTTPException

from app.api.manufacturing import _parse_optional_date


class OrderDateValidationTest(unittest.TestCase):
    def test_rejects_planned_delivery_date_in_the_past(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        with self.assertRaises(HTTPException) as error:
            _parse_optional_date(yesterday)

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("раньше сегодняшнего дня", error.exception.detail)

    def test_accepts_today_and_future_dates(self):
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        self.assertEqual(_parse_optional_date(today).date(), date.today())
        self.assertEqual(
            _parse_optional_date(tomorrow).date(),
            date.today() + timedelta(days=1),
        )


if __name__ == "__main__":
    unittest.main()
