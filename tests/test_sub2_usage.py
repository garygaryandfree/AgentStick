import unittest

from vibe_stick.usage.sub2 import Sub2UsageError, parse_sub2_usage


class Sub2UsageTests(unittest.TestCase):
    def test_parse_reads_remaining_windows_and_account_name(self) -> None:
        snapshot = parse_sub2_usage(
            "codex",
            52,
            {
                "account_id": 52,
                "account_name": "Codex Main",
                "windows": {
                    "five_hour": {"remaining_percent": 83.6},
                    "seven_day": {"remaining_percent": 55},
                },
            },
        )

        self.assertEqual(snapshot.account_name, "Codex Main")
        self.assertEqual(snapshot.quota_5h_remaining, 84)
        self.assertEqual(snapshot.quota_7d_remaining, 55)

    def test_parse_allows_current_response_without_account_name(self) -> None:
        snapshot = parse_sub2_usage(
            "claude",
            29,
            {
                "account_id": 29,
                "windows": {
                    "five_hour": {"remaining_percent": 100},
                    "seven_day": {"remaining_percent": 80},
                },
            },
        )

        self.assertEqual(snapshot.account_name, "")
        self.assertEqual(snapshot.account_id, 29)

    def test_parse_rejects_missing_usage_window(self) -> None:
        with self.assertRaises(Sub2UsageError):
            parse_sub2_usage("codex", 52, {"windows": {}})


if __name__ == "__main__":
    unittest.main()
