from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from funding_bot import FundingBot
from profiling.runner import BenchmarkResult, apply_baselines, render_html_report


class DeduplicationMethodTests(unittest.TestCase):
    def test_deduplicate_filters_duplicates_and_untrusted_sources(self):
        bot = FundingBot(trusted_sources={"Grants Portal"})
        try:
            found = bot.deduplicate(
                [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Education Fund",
                        "title": "Literacy Grant",
                        "portal_url": "https://example.org/grants/literacy",
                        "summary": "Funding for literacy programs.",
                        "tags": ["education", "literacy"],
                        "category": "Education",
                    },
                    {
                        "source": "Grants Portal",
                        "donor_name": "Education Fund",
                        "title": "Literacy Grant",
                        "portal_url": "https://example.org/grants/literacy",
                        "summary": "Funding for literacy programs.",
                        "tags": ["education", "literacy"],
                        "category": "Education",
                    },
                    {
                        "source": "Untrusted Source",
                        "donor_name": "Spam",
                        "title": "Ignore Me",
                        "portal_url": "https://bad.example/grants",
                        "summary": "Funding for literacy programs.",
                        "tags": ["education"],
                        "category": "Education",
                    },
                ],
                keywords=["literacy"],
            )
        finally:
            bot.close()

        self.assertEqual(1, len(found))
        self.assertEqual(1, len(bot.list_opportunities()))
        self.assertEqual("Literacy Grant", found[0]["title"])


class ProfilingReportTests(unittest.TestCase):
    def setUp(self):
        self.output_dir = Path(".test-profiling-report")
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)

    def test_apply_baselines_flags_regressions(self):
        result = BenchmarkResult(
            name="deduplication",
            description="desc",
            iterations=3,
            metadata={},
            durations_seconds=[1.0, 1.1, 1.2],
            mean_seconds=1.1,
            median_seconds=1.1,
            p95_seconds=1.19,
            min_seconds=1.0,
            max_seconds=1.2,
        )
        failures = apply_baselines(
            [result],
            {
                "operations": {
                    "deduplication": {
                        "baseline_seconds": 0.2,
                        "max_regression_factor": 2.0,
                        "allowed_overhead_seconds": 0.1,
                    }
                }
            },
        )

        self.assertEqual(1, len(failures))
        self.assertTrue(result.regression_detected)
        self.assertGreater(result.max_allowed_seconds, result.baseline_seconds)

    def test_render_html_report_links_available_artifacts(self):
        result = BenchmarkResult(
            name="connector_calls",
            description="desc",
            iterations=2,
            metadata={"pages": 4},
            durations_seconds=[0.1, 0.2],
            mean_seconds=0.15,
            median_seconds=0.15,
            p95_seconds=0.195,
            min_seconds=0.1,
            max_seconds=0.2,
            cprofile_stats_path="connector.prof",
            cprofile_text_path="connector.txt",
            flamegraph_svg_path="connector.svg",
        )

        report_path = render_html_report([result], self.output_dir)
        contents = report_path.read_text(encoding="utf-8")

        self.assertIn("connector_calls", contents)
        self.assertIn("connector.prof", contents)
        self.assertIn("connector.txt", contents)
        self.assertIn("connector.svg", contents)


if __name__ == "__main__":
    unittest.main()
