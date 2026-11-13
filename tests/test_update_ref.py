"""Unit tests for calibpipe.update_ref."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from calibpipe import update_ref


class TestUpdateRefParsingAndMerging(unittest.TestCase):
    """Test results parsing, tolerance handling, and merge mechanics."""

    def test_parse_results_content(self) -> None:
        content = """
        # Comment line
        s1.importdata.num_antennas=12
        s2.bandpass.qa.score=0.99:::1e-5
        s3.fluxscale.amp=1.234:::
        """
        entries = update_ref.parse_results_content(content)
        self.assertEqual(len(entries), 3)

        self.assertIn("s1.importdata.num_antennas", entries)
        e1 = entries["s1.importdata.num_antennas"]
        self.assertEqual(e1.value, "12")
        self.assertIsNone(e1.tolerance)
        self.assertEqual(e1.to_line(), "s1.importdata.num_antennas=12")

        self.assertIn("s2.bandpass.qa.score", entries)
        e2 = entries["s2.bandpass.qa.score"]
        self.assertEqual(e2.value, "0.99")
        self.assertEqual(e2.tolerance, "1e-5")
        self.assertEqual(e2.to_line(), "s2.bandpass.qa.score=0.99:::1e-5")

        self.assertIn("s3.fluxscale.amp", entries)
        e3 = entries["s3.fluxscale.amp"]
        self.assertEqual(e3.value, "1.234")
        self.assertEqual(e3.tolerance, "")
        self.assertEqual(e3.to_line(), "s3.fluxscale.amp=1.234:::")

    def test_merge_preserves_custom_tolerances(self) -> None:
        old_content = """
        key.a=100:::1e-4
        key.b=200:::0.05
        key.c=300
        key.removed=999:::1e-3
        """
        new_content = """
        key.b=205
        key.a=100
        key.c=350
        key.added=400
        """
        old_entries = update_ref.parse_results_content(old_content)
        new_entries = update_ref.parse_results_content(new_content)

        summary = update_ref.merge_results(old_entries, new_entries)

        # Sorted alphabetical order check
        keys = [e.key for e in summary.merged_entries]
        self.assertEqual(keys, ["key.a", "key.added", "key.b", "key.c"])

        # Tolerance preservation
        merged_dict = {e.key: e for e in summary.merged_entries}
        self.assertEqual(merged_dict["key.a"].tolerance, "1e-4")
        self.assertEqual(merged_dict["key.a"].value, "100")
        self.assertEqual(merged_dict["key.b"].tolerance, "0.05")
        self.assertEqual(merged_dict["key.b"].value, "205")
        self.assertIsNone(merged_dict["key.c"].tolerance)
        self.assertIsNone(merged_dict["key.added"].tolerance)

        # Statistics
        self.assertEqual(summary.added_keys, ["key.added"])
        self.assertEqual(summary.removed_keys, ["key.removed"])
        self.assertEqual(summary.modified_keys, [("key.b", "200", "205"), ("key.c", "300", "350")])
        self.assertEqual(summary.unchanged_count, 1)
        self.assertEqual(summary.tolerances_preserved, 2)


class TestUpdateRefStagingAndApply(unittest.TestCase):
    """Test candidate staging, promotion (--apply), and file outputs."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.td.name)
        self.working_dir = self.tmp_dir / "working"
        self.working_dir.mkdir(parents=True)

        self.ref_file = self.working_dir / "test_ds.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt"
        self.ref_file.write_text(
            "qa.score=0.95:::1e-5\nold.metric=10\nunchanged.val=42\n",
            encoding="utf-8",
        )

        self.new_file = self.working_dir / "uid.ms.NEW.results.txt"
        self.new_file.write_text(
            "new.metric=100\nqa.score=0.98\nunchanged.val=42\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_stage_candidate_by_default(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = update_ref.main([
                str(self.working_dir),
                "--no-color",
            ])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("Regression Reference Update Summary", out)
        self.assertIn("Staged Candidate", out)

        candidate = self.working_dir / "test_ds.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt.candidate"
        self.assertTrue(candidate.is_file())

        # Original ref file must remain untouched
        ref_lines = self.ref_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("qa.score=0.95:::1e-5", ref_lines)
        self.assertIn("old.metric=10", ref_lines)

        # Candidate must have updated values and preserved tolerance
        cand_lines = candidate.read_text(encoding="utf-8").splitlines()
        self.assertIn("qa.score=0.98:::1e-5", cand_lines)
        self.assertIn("new.metric=100", cand_lines)
        self.assertNotIn("old.metric=10", cand_lines)

    def test_apply_promotes_candidate_and_creates_backup(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = update_ref.main([
                str(self.working_dir),
                "--apply",
                "--no-color",
            ])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("Reference file successfully updated", out)

        # Candidate must not linger
        candidate = self.working_dir / "test_ds.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt.candidate"
        self.assertFalse(candidate.exists())

        # Ref file must be updated
        ref_lines = self.ref_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("qa.score=0.98:::1e-5", ref_lines)
        self.assertIn("new.metric=100", ref_lines)

        # Backup file must exist with original contents
        bak_file = self.ref_file.with_name(self.ref_file.name + ".bak")
        self.assertTrue(bak_file.is_file())
        bak_lines = bak_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("qa.score=0.95:::1e-5", bak_lines)

    def test_diff_only_does_not_write_files(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = update_ref.main([
                str(self.new_file),
                "--diff-only",
                "--no-color",
            ])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("Diff Preview", out)

        candidate = self.working_dir / "test_ds.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt.candidate"
        self.assertFalse(candidate.exists())

    def test_version_override(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = update_ref.main([
                str(self.working_dir),
                "--casa-ver=6.7.4-12",
                "--pipeline-ver=2026.3.0.42",
                "--no-color",
            ])
        self.assertEqual(code, 0)

        expected_cand = self.working_dir / "test_ds.casa-6.7.4-12-pipeline-2026.3.0.42.results.txt.candidate"
        self.assertTrue(expected_cand.is_file())

    def test_auto_detect_versions_from_aquareport_and_keep_version(self) -> None:
        aqua = self.working_dir / "pipeline_aquareport.xml"
        aqua.write_text(
            "<PipelineAquaReport><QaSummary>"
            "<CasaVersion>6.7.4.12</CasaVersion>"
            "<PipelineVersion>2026.3.0.42+1-gb62277a0e-PIPE-3111</PipelineVersion>"
            "</QaSummary></PipelineAquaReport>\n",
            encoding="utf-8",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = update_ref.main([
                str(self.working_dir),
                "--no-color",
            ])
        self.assertEqual(code, 0)
        expected_cand = self.working_dir / "test_ds.casa-6.7.4-12-pipeline-2026.3.0.42.results.txt.candidate"
        self.assertTrue(expected_cand.is_file())

        # With --keep-version, it keeps the baseline filename's version tag
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            code2 = update_ref.main([
                str(self.working_dir),
                "--keep-version",
                "--no-color",
            ])
        self.assertEqual(code2, 0)
        kept_cand = self.working_dir / "test_ds.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt.candidate"
        self.assertTrue(kept_cand.is_file())

    def test_recursive_discovery_skips_unchanged_and_pruned_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as batch_td:
            batch_root = Path(batch_td)

            # Run 1: changed
            w1 = batch_root / "test_a" / "test_a" / "working"
            w1.mkdir(parents=True)
            (w1 / "ds_a.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt").write_text("k1=1\n", encoding="utf-8")
            (w1 / "uid_a.ms.NEW.results.txt").write_text("k1=2\n", encoding="utf-8")

            # Run 2: unchanged
            w2 = batch_root / "test_b" / "test_b" / "working"
            w2.mkdir(parents=True)
            (w2 / "ds_b.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt").write_text("k2=10\n", encoding="utf-8")
            (w2 / "uid_b.ms.NEW.results.txt").write_text("k2=10\n", encoding="utf-8")

            # Pruned dir: should not be scanned
            pruned_ms = batch_root / "test_b" / "test_b" / "working" / "uid_b.ms"
            pruned_ms.mkdir(parents=True)
            (pruned_ms / "bogus.NEW.results.txt").write_text("x=1\n", encoding="utf-8")

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = update_ref.main([str(batch_root), "--no-color"])
            self.assertEqual(code, 0)
            out = buf.getvalue()

            self.assertIn("Summary: scanned 2 run(s) — 1 with changes, 1 unchanged.", out)
            self.assertTrue((w1 / "ds_a.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt.candidate").is_file())
            self.assertFalse((w2 / "ds_b.casa-6.7.4-8-pipeline-2026.2.0.27.results.txt.candidate").exists())

    def test_restore_suffix_preserved_and_live_version_preferred_over_stale_products(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            working = root / "working"
            products = root / "products"
            weblog_html = working / "pipeline-123" / "html"
            working.mkdir(parents=True)
            products.mkdir(parents=True)
            weblog_html.mkdir(parents=True)

            # Stale products/pipeline_aquareport.xml from 2021 (as in restore tests)
            (products / "pipeline_aquareport.xml").write_text(
                "<PipelineAquaReport><QaSummary>"
                "<CasaVersion>6.2.1.6</CasaVersion>"
                "<PipelineVersion>2021.3.0.6</PipelineVersion>"
                "</QaSummary></PipelineAquaReport>\n",
                encoding="utf-8",
            )
            # Live weblog index.html from current run
            (weblog_html / "index.html").write_text(
                "<tr><th>Pipeline Version</th><td>2026.3.0.42+1-gb62277a0e</td></tr>"
                "<tr><th>CASA Version</th><td>6.7.4.12</td></tr>",
                encoding="utf-8",
            )

            (working / "13A-537.casa-6.7.4-4-pipeline-2026.1.1.57.restore.results.txt").write_text(
                "s2.hifv_statwt.mean=0.629\n",
                encoding="utf-8",
            )
            (working / "13A-537.ms.NEW.results.txt").write_text(
                "s2.hifv_statwt.mean=0.630\n",
                encoding="utf-8",
            )

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = update_ref.main([str(root), "--no-color"])
            self.assertEqual(code, 0)

            expected = working / "13A-537.casa-6.7.4-12-pipeline-2026.3.0.42.restore.results.txt.candidate"
            self.assertTrue(expected.is_file())


class TestCliIntegration(unittest.TestCase):
    """Test CLI subcommand routing for update-ref."""

    def test_cli_routing_update_ref(self) -> None:
        from calibpipe import cli

        with patch("calibpipe.update_ref.main", return_value=0) as mock_main:
            with self.assertRaises(SystemExit) as cm:
                cli.main(["update-ref", "--diff-only", "."])
            self.assertEqual(cm.exception.code, 0)
            mock_main.assert_called_once_with(["--diff-only", "."])


if __name__ == "__main__":
    unittest.main()
