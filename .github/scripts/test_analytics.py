#!/usr/bin/env python3
"""
Comprehensive Unit & Integration Test Suite for Analytics Dashboard
Tests all 17 requirements specified in the project specification.
"""

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import functions from update_analytics
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from update_analytics import (
    GitHubAPIClient,
    START_MARKER,
    END_MARKER,
    atomic_write_json,
    atomic_write_text,
    safe_load_json,
    initialize_traffic_history,
    merge_traffic_data,
    compute_traffic_metrics,
    initialize_activity_history,
    merge_activity_events,
    generate_svg_chart,
    generate_shields_badges,
    render_dashboard_markdown,
    update_readme_content,
    parse_iso_datetime,
    format_ist,
    IST,
    UTC,
)


class TestAnalyticsDashboard(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_path = Path(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_first_run_no_history(self):
        """1. A first run with no historical data."""
        history = initialize_traffic_history()
        self.assertIsNone(history["collectionStartedAt"])
        self.assertEqual(history["daily"], {})

        clones = {"count": 5, "uniques": 2, "clones": [{"timestamp": "2026-10-20T00:00:00Z", "count": 5, "uniques": 2}]}
        views = {"count": 20, "uniques": 8, "views": [{"timestamp": "2026-10-20T00:00:00Z", "count": 20, "uniques": 8}]}

        updated = merge_traffic_data(history, clones, views, "2026-10-20T12:00:00Z")
        self.assertTrue(updated)
        self.assertEqual(history["collectionStartedAt"], "2026-10-20T12:00:00Z")
        self.assertIn("2026-10-20", history["daily"])
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 5)
        self.assertEqual(history["daily"]["2026-10-20"]["views"], 20)

    def test_02_normal_daily_update(self):
        """2. A normal daily update."""
        history = initialize_traffic_history()
        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-19T00:00:00Z", "count": 4, "uniques": 2}]}, None, "2026-10-19T10:00:00Z")
        self.assertEqual(len(history["daily"]), 1)

        # Next day update
        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-20T00:00:00Z", "count": 7, "uniques": 3}]}, None, "2026-10-20T10:00:00Z")
        self.assertEqual(len(history["daily"]), 2)
        self.assertEqual(history["daily"]["2026-10-19"]["clones"], 4)
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 7)

    def test_03_reprocessing_same_date_no_double_counting(self):
        """3. Reprocessing the same date without double counting."""
        history = initialize_traffic_history()
        payload = {"clones": [{"timestamp": "2026-10-20T00:00:00Z", "count": 10, "uniques": 4}]}

        # First run
        merge_traffic_data(history, payload, None, "2026-10-20T10:00:00Z")
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 10)

        # Re-run same day with same data
        merge_traffic_data(history, payload, None, "2026-10-20T15:00:00Z")
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 10)
        self.assertEqual(len(history["daily"]), 1)

    def test_04_updates_to_earlier_dates_from_github(self):
        """4. Updates to earlier dates returned by GitHub (revision within reporting window)."""
        history = initialize_traffic_history()
        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-18T00:00:00Z", "count": 5, "uniques": 2}]}, None, "2026-10-18T10:00:00Z")

        # GitHub later revises Oct 18 count to 9
        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-18T00:00:00Z", "count": 9, "uniques": 4}]}, None, "2026-10-19T10:00:00Z")
        self.assertEqual(history["daily"]["2026-10-18"]["clones"], 9)
        self.assertEqual(history["daily"]["2026-10-18"]["uniqueCloners"], 4)

    def test_05_missing_traffic_day(self):
        """5. A missing traffic day handled without fabricating data."""
        history = initialize_traffic_history()
        history["daily"] = {
            "2026-10-16": {"clones": 2, "uniqueCloners": 1, "views": 5, "uniqueVisitors": 3},
            # 2026-10-17 is missing
            "2026-10-18": {"clones": 4, "uniqueCloners": 2, "views": 10, "uniqueVisitors": 5},
        }
        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["availableDaysCount"], 2)
        self.assertFalse(metrics["isWindowComplete"])
        dates = [r["date"] for r in metrics["last5DaysRecords"]]
        self.assertEqual(dates, ["2026-10-16", "2026-10-18"])

    def test_06_temporary_api_failure_and_retries(self):
        """6. Bounded retries for temporary API failures."""
        client = GitHubAPIClient()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        # Fail once with 500, then succeed
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_err = urllib.error.HTTPError("http://api", 500, "Server Error", {}, None)
            mock_err.read = MagicMock(return_value=b'{"error": "temp"}')
            mock_urlopen.side_effect = [mock_err, mock_response]

            with patch("time.sleep", return_value=None):
                status, data, _ = client.request("test-endpoint", max_retries=2, backoff_sec=0.01)
                self.assertEqual(status, 200)
                self.assertEqual(data, {"ok": True})
                self.assertEqual(mock_urlopen.call_count, 2)

    def test_07_invalid_json_file(self):
        """7. Handling an invalid/corrupted JSON file safely."""
        corrupt_file = self.test_path / "corrupt.json"
        corrupt_file.write_text("{ unclosed json content ...", encoding="utf-8")

        data = safe_load_json(corrupt_file, initialize_traffic_history)
        self.assertEqual(data["schemaVersion"], 1)
        # Verify corrupt file was preserved as backup
        backups = list(self.test_path.glob("corrupt.json.corrupt-*"))
        self.assertEqual(len(backups), 1)

    def test_08_partial_endpoint_failure(self):
        """8. Partial failure: traffic endpoint fails (e.g. 403), history is preserved."""
        history = initialize_traffic_history()
        history["daily"] = {
            "2026-10-19": {"clones": 15, "uniqueCloners": 5, "views": 40, "uniqueVisitors": 12}
        }
        # Clones fail (None), Views succeed
        views_payload = {"views": [{"timestamp": "2026-10-20T00:00:00Z", "count": 25, "uniques": 8}]}
        updated = merge_traffic_data(history, None, views_payload, "2026-10-20T12:00:00Z")

        self.assertTrue(updated)
        # Previous clones data MUST NOT be erased
        self.assertEqual(history["daily"]["2026-10-19"]["clones"], 15)
        # New views data is recorded
        self.assertEqual(history["daily"]["2026-10-20"]["views"], 25)

    def test_09_five_day_aggregation(self):
        """9. 5-day aggregation arithmetic."""
        history = initialize_traffic_history()
        for i in range(1, 6):
            d_str = f"2026-10-0{i}"
            history["daily"][d_str] = {
                "clones": i * 2,          # 2, 4, 6, 8, 10 -> sum = 30
                "uniqueCloners": i,       # 1, 2, 3, 4, 5  -> avg = 3.0
                "views": i * 10,          # 10, 20, 30, 40, 50 -> sum = 150
                "uniqueVisitors": i * 2,  # 2, 4, 6, 8, 10 -> avg = 6.0
            }
        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["last5DaysTotalClones"], 30)
        self.assertEqual(metrics["last5DaysTotalViews"], 150)
        self.assertEqual(metrics["last5DaysAvgUniqueClonersPerDay"], 3.0)
        self.assertEqual(metrics["last5DaysAvgUniqueVisitorsPerDay"], 6.0)
        self.assertTrue(metrics["isWindowComplete"])

    def test_10_previous_five_day_comparison(self):
        """10. Previous 5-day comparison and growth calculation."""
        history = initialize_traffic_history()
        # Days 1 to 5: previous period (clones=10, views=50)
        for i in range(1, 6):
            history["daily"][f"2026-10-0{i}"] = {"clones": 2, "uniqueCloners": 1, "views": 10, "uniqueVisitors": 5}
        # Days 6 to 10: current period (clones=20, views=100 -> +100% growth)
        for i in range(6, 11):
            history["daily"][f"2026-10-{i:02d}"] = {"clones": 4, "uniqueCloners": 2, "views": 20, "uniqueVisitors": 10}

        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["last5DaysTotalClones"], 20)
        self.assertEqual(metrics["previous5DaysTotalClones"], 10)
        self.assertEqual(metrics["cloneGrowthPercent"], 100.0)

        self.assertEqual(metrics["last5DaysTotalViews"], 100)
        self.assertEqual(metrics["previous5DaysTotalViews"], 50)
        self.assertEqual(metrics["viewGrowthPercent"], 100.0)

    def test_11_cumulative_recorded_totals(self):
        """11. Cumulative recorded totals across full historical record."""
        history = initialize_traffic_history()
        for i in range(1, 21):  # 20 days
            history["daily"][f"2026-09-{i:02d}"] = {"clones": 3, "uniqueCloners": 1, "views": 12, "uniqueVisitors": 4}

        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["recordedCumulativeClones"], 60)
        self.assertEqual(metrics["recordedCumulativeViews"], 240)

    def test_12_activity_deduplication(self):
        """12. Activity event deduplication using stable IDs."""
        store = initialize_activity_history()
        events_batch_1 = [
            {"id": "fork-101", "action": "Fork", "actor": "alice", "timestamp": "2026-10-20T10:00:00Z"},
            {"id": "commit-aaa", "action": "Commit", "actor": "bob", "timestamp": "2026-10-20T11:00:00Z"},
        ]
        merge_activity_events(store, events_batch_1)
        self.assertEqual(len(store["events"]), 2)

        # Batch 2 with duplicate fork-101 and new star-charlie
        events_batch_2 = [
            {"id": "fork-101", "action": "Fork", "actor": "alice", "timestamp": "2026-10-20T10:00:00Z"},
            {"id": "star-charlie", "action": "Star", "actor": "charlie", "timestamp": "2026-10-20T12:00:00Z"},
        ]
        merged = merge_activity_events(store, events_batch_2)
        self.assertEqual(len(merged), 3)
        ids = [e["id"] for e in merged]
        self.assertEqual(ids, ["star-charlie", "commit-aaa", "fork-101"])

    def test_13_date_formatting_and_ist(self):
        """13. Correct date parsing and IST conversion."""
        utc_ts = "2026-10-20T18:00:00Z"
        dt = parse_iso_datetime(utc_ts)
        self.assertIsNotNone(dt)
        ist_str = format_ist(dt)
        # 18:00 UTC + 5h30m = 23:30 IST on same day
        self.assertEqual(ist_str, "2026-10-20 23:30:00 IST")

    def test_14_readme_marker_replacement(self):
        """14. README marker replacement preserves all surrounding documentation."""
        original = (
            "## Copyright\n\n"
            "Copyright (c) 2026 Shivam. All rights reserved.\n\n"
            "### Support\n"
            "Star the repo!\n"
        )
        new_section = f"{START_MARKER}\n# Analytics Dashboard Content\n{END_MARKER}"

        # First insertion before ## Copyright
        updated = update_readme_content(original, new_section)
        self.assertIn(START_MARKER, updated)
        self.assertIn("Copyright (c) 2026 Shivam.", updated)
        self.assertIn("Star the repo!", updated)

        # Second update replacing existing markers
        replacement_section = f"{START_MARKER}\n# Updated Analytics v2\n{END_MARKER}"
        updated_again = update_readme_content(updated, replacement_section)
        self.assertIn("Updated Analytics v2", updated_again)
        self.assertNotIn("Analytics Dashboard Content", updated_again)
        self.assertIn("Copyright (c) 2026 Shivam.", updated_again)
        self.assertIn("Star the repo!", updated_again)

    def test_15_valid_generated_json_and_svg(self):
        """15. Valid generated JSON files and valid SVG format."""
        chart_file = self.test_path / "traffic-chart.svg"
        records = [
            {"date": "2026-10-18", "clones": 3, "uniqueCloners": 2, "views": 15, "uniqueVisitors": 6},
            {"date": "2026-10-19", "clones": 5, "uniqueCloners": 3, "views": 25, "uniqueVisitors": 10},
        ]
        generate_svg_chart(records, chart_file)
        self.assertTrue(chart_file.exists())
        svg_content = chart_file.read_text(encoding="utf-8")
        self.assertTrue(svg_content.startswith("<svg"))
        self.assertTrue(svg_content.endswith("</svg>\n"))
        self.assertIn("Oct 18", svg_content)
        self.assertIn("Oct 19", svg_content)

        # Badges
        shields_dir = self.test_path / "shields"
        metrics = {
            "last5DaysTotalClones": 8,
            "last5DaysTotalViews": 40,
            "last5DaysAvgUniqueClonersPerDay": 2.5,
            "last5DaysAvgUniqueVisitorsPerDay": 8.0,
            "recordedCumulativeClones": 8,
            "recordedCumulativeViews": 40,
        }
        generate_shields_badges(metrics, shields_dir)
        badge_file = shields_dir / "total-clones.json"
        self.assertTrue(badge_file.exists())
        with open(badge_file, "r", encoding="utf-8") as f:
            b_data = json.load(f)
            self.assertEqual(b_data["schemaVersion"], 1)
            self.assertEqual(b_data["message"], "8")

    def test_16_git_changes_filter(self):
        """16. Verify workflow targets only .github/analytics/ and README.md."""
        workflow_file = Path(__file__).resolve().parents[1] / "workflows" / "repository-analytics.yml"
        self.assertTrue(workflow_file.exists())
        wf_text = workflow_file.read_text(encoding="utf-8")
        self.assertIn("git add .github/analytics/ README.md", wf_text)
        self.assertIn("[skip ci]", wf_text)

    def test_17_badge_urls_and_default_branch(self):
        """17. Correct badge URLs and default-branch references."""
        markdown = render_dashboard_markdown(
            repo_name="shivampimple29/Intelligent-Data-System-sem5",
            default_branch="main",
            traffic_metrics={"last5DaysRecords": []},
            growth_metrics={},
            recent_events=[],
            update_time_ist="2026-10-21 23:00:00 IST",
            traffic_status="success",
        )
        self.assertIn("raw.githubusercontent.com%2Fshivampimple29%2FIntelligent-Data-System-sem5%2Fmain", markdown)
        self.assertIn("main", markdown)


if __name__ == "__main__":
    unittest.main()
