"""Tooling to update and stage regression test reference results (*.results.txt).

Automates Part 3 of testing workflow ('How to Update Reference Files in pipeline-testdata'):
1. Locates *.NEW.results.txt and matching baseline reference file (*.results.txt).
2. Merges new execution values into the reference structure while strictly preserving
   custom per-key tolerances (:::<tolerance>).
3. Safely stages changes as a '.candidate' (or '.staged') file for manual inspection.
4. Provides diff inspection and atomic promotion (--apply / --promote) to update
   the canonical file in pipeline-testdata or working directory with backup preservation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import sys
from typing import NamedTuple, Sequence


# Regex patterns matching CASA and Pipeline version tags in filenames or logs
REGEX_CASA_VERSION = re.compile(r".*casa-([\d.]+(?:-\d+)?)")
REGEX_PIPELINE_VERSION = re.compile(r".*pipeline-([\d.]+(?:\.\d+)*\d)(?=\b|[-_]|$)")
REGEX_CASA_LOG_VERSION = re.compile(r"CASA Version\s+(?:CASALITH\s+)?(\d+(?:\.\d+)+(?:-\d+)?)")
REGEX_CASA_ROOT_VERSION = re.compile(r"CASA_ROOT\s*=\s*\S*casa-(\d+(?:\.\d+)+-\d+)")
REGEX_PIPELINE_LOG_VERSION = re.compile(r"['\"]pipeline_version['\"]\s*:\s*\{['\"]value['\"]\s*:\s*['\"]([^'\"]+)['\"]")
REGEX_AQUA_CASA_VERSION = re.compile(r"<CasaVersion>\s*([^<\s]+)\s*</CasaVersion>")
REGEX_AQUA_PIPE_VERSION = re.compile(r"<PipelineVersion>\s*([^<\s]+)\s*</PipelineVersion>")
REGEX_MANIFEST_CASA_VERSION = re.compile(r"<casaversion\s+name=['\"]([^'\"]+)['\"]")
REGEX_MANIFEST_PIPE_VERSION = re.compile(r"<pipeline_version\s+name=['\"]([^'\"]+)['\"]")
REGEX_WEBLOG_CASA_VERSION = re.compile(r"<th>\s*CASA Version\s*</th>\s*<td>\s*([\d.]+(?:-\d+)?)", re.IGNORECASE)
REGEX_WEBLOG_PIPE_VERSION = re.compile(r"<th>\s*Pipeline Version\s*</th>\s*<td>\s*([^\s<]+)", re.IGNORECASE)


def normalize_casa_version(raw: str) -> str:
    """Normalize CASA version string to testdata filename convention (e.g. 6.7.4.12 -> 6.7.4-12)."""
    v = raw.strip()
    if "-" in v:
        return v
    parts = v.split(".")
    if len(parts) >= 4:
        return f"{'.'.join(parts[:3])}-{parts[3]}"
    return v


def normalize_pipeline_version(raw: str) -> str:
    """Normalize Pipeline version string by stripping git commit/branch suffixes (e.g. 2026.3.0.42+1-g... -> 2026.3.0.42)."""
    v = raw.strip()
    clean_m = re.match(r"^([\d.]+(?:\.\d+)*\d)", v)
    return clean_m.group(1) if clean_m else v


class RefEntry(NamedTuple):
    """Single reference record in a .results.txt file."""

    key: str
    value: str
    tolerance: str | None = None  # None if no ::: suffix, else string (including empty "")

    def to_line(self) -> str:
        """Format back to .results.txt line string."""
        if self.tolerance is not None:
            return f"{self.key}={self.value}:::{self.tolerance}"
        return f"{self.key}={self.value}"


class MergeSummary(NamedTuple):
    """Summary of changes between old reference and new results."""

    merged_entries: list[RefEntry]
    added_keys: list[str]
    removed_keys: list[str]
    modified_keys: list[tuple[str, str, str]]  # (key, old_value, new_value)
    unchanged_count: int
    tolerances_preserved: int


def parse_results_content(content: str) -> dict[str, RefEntry]:
    """Parse text content of a .results.txt file into a dictionary of RefEntry."""
    entries: dict[str, RefEntry] = {}
    for line in content.splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#"):
            continue

        tol: str | None = None
        if ":::" in line_clean:
            keyval, tol_str = line_clean.split(":::", 1)
            tol = tol_str.strip()
        else:
            keyval = line_clean

        if "=" not in keyval:
            continue

        key, val = keyval.split("=", 1)
        key = key.strip()
        val = val.strip()
        entries[key] = RefEntry(key=key, value=val, tolerance=tol)
    return entries


def parse_results_file(path: str | Path) -> dict[str, RefEntry]:
    """Read and parse a .results.txt file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Results file does not exist: {path}")
    return parse_results_content(path.read_text(encoding="utf-8", errors="replace"))


def merge_results(
    old_entries: dict[str, RefEntry],
    new_entries: dict[str, RefEntry],
) -> MergeSummary:
    """Merge new execution results into baseline reference, preserving tolerances.

    The merged list of entries is strictly sorted by key name, fulfilling
    PipelineTester's exact key ordering requirement.
    """
    merged: list[RefEntry] = []
    added: list[str] = []
    modified: list[tuple[str, str, str]] = []
    unchanged = 0
    tols_preserved = 0

    # Ensure sorted order matching PipelineTester expectations
    for key in sorted(new_entries.keys()):
        new_entry = new_entries[key]
        if key in old_entries:
            old_entry = old_entries[key]
            # Preserve existing tolerance if set
            tol = old_entry.tolerance
            if tol is not None:
                tols_preserved += 1

            if old_entry.value != new_entry.value:
                modified.append((key, old_entry.value, new_entry.value))
            else:
                unchanged += 1

            merged.append(RefEntry(key=key, value=new_entry.value, tolerance=tol))
        else:
            added.append(key)
            merged.append(RefEntry(key=key, value=new_entry.value, tolerance=None))

    removed = [k for k in sorted(old_entries.keys()) if k not in new_entries]

    return MergeSummary(
        merged_entries=merged,
        added_keys=added,
        removed_keys=removed,
        modified_keys=modified,
        unchanged_count=unchanged,
        tolerances_preserved=tols_preserved,
    )


def colorize(text: str, color: str, enable: bool = True) -> str:
    """Apply terminal color if enabled and connected to a TTY."""
    if not enable or not sys.stdout.isatty():
        return text
    colors = {
        "green": "\033[32m",
        "red": "\033[31m",
        "yellow": "\033[33m",
        "cyan": "\033[36m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "reset": "\033[0m",
    }
    return f"{colors.get(color, '')}{text}{colors['reset']}"


def format_summary_report(
    summary: MergeSummary,
    new_file: Path,
    ref_file: Path | None,
    candidate_file: Path | None,
    casa_ver: str | None = None,
    pipeline_ver: str | None = None,
    applied: bool = False,
    use_color: bool = True,
    preview_limit: int = 15,
) -> str:
    """Generate a structured, human-readable terminal diff and staging report."""
    c = lambda txt, col: colorize(txt, col, enable=use_color)

    lines: list[str] = []
    lines.append(c("=" * 72, "bold"))
    lines.append(c(" Regression Reference Update Summary", "cyan") + c(" [calibpipe]", "dim"))
    lines.append(c("-" * 72, "bold"))
    lines.append(f"  New Results:       {new_file}")
    lines.append(f"  Baseline Reference:{ref_file if ref_file else '(none - new dataset)'}")
    if casa_ver or pipeline_ver:
        lines.append(f"  Target Versions:   CASA {casa_ver or 'unknown'} | Pipeline {pipeline_ver or 'unknown'}")
    if candidate_file:
        status_label = "Updated Target:" if applied else "Staged Candidate:"
        lines.append(f"  {status_label:<19}{candidate_file}")
    lines.append("")

    total_keys = len(summary.merged_entries)
    lines.append(c("Change Statistics:", "bold"))
    lines.append(f"  Total keys in new:   {total_keys}")
    lines.append(f"  {c('+', 'green')} Added keys:         {c(str(len(summary.added_keys)), 'green')}")
    lines.append(f"  {c('-', 'red')} Removed keys:       {c(str(len(summary.removed_keys)), 'red')}")
    lines.append(f"  {c('~', 'yellow')} Modified values:    {c(str(len(summary.modified_keys)), 'yellow')}")
    lines.append(f"    Unchanged values:   {summary.unchanged_count}")
    lines.append(f"    Tolerances kept:    {summary.tolerances_preserved}")

    # Diff preview
    has_diffs = bool(summary.added_keys or summary.removed_keys or summary.modified_keys)
    if has_diffs:
        lines.append("")
        lines.append(c("Diff Preview:", "bold"))
        count = 0

        for key in summary.added_keys:
            if count >= preview_limit:
                lines.append(c(f"  ... and {len(summary.added_keys) - count} more added keys", "dim"))
                break
            # Find entry for value
            val = next((e.value for e in summary.merged_entries if e.key == key), "")
            lines.append(f"  {c('+ ' + key + '=' + val, 'green')}")
            count += 1

        count = 0
        for key in summary.removed_keys:
            if count >= preview_limit:
                lines.append(c(f"  ... and {len(summary.removed_keys) - count} more removed keys", "dim"))
                break
            lines.append(f"  {c('- ' + key, 'red')}")
            count += 1

        count = 0
        for key, old_v, new_v in summary.modified_keys:
            if count >= preview_limit:
                lines.append(c(f"  ... and {len(summary.modified_keys) - count} more modified keys", "dim"))
                break
            lines.append(f"  {c('~ ' + key, 'yellow')}: {old_v} -> {c(new_v, 'bold')}")
            count += 1

    lines.append(c("-" * 72, "bold"))
    if applied:
        lines.append(c(f"✓ Reference file successfully updated: {candidate_file}", "green"))
    elif candidate_file:
        lines.append(c("Candidate file staged for manual review:", "cyan"))
        lines.append(f"  {candidate_file}")
        lines.append("")
        lines.append("Next Steps:")
        if ref_file and ref_file.exists():
            lines.append(f"  1. Review diff:    diff -u '{ref_file}' '{candidate_file}'")
        else:
            lines.append(f"  1. Review file:    head -n 30 '{candidate_file}'")
        lines.append(f"  2. Apply changes:  calibpipe update-ref --apply '{new_file.parent}'")
    lines.append(c("=" * 72, "bold"))

    return "\n".join(lines)


def find_testdata_root(cli_testdata: str | None = None) -> Path | None:
    """Locate pipeline-testdata root directory."""
    if cli_testdata:
        p = Path(cli_testdata).expanduser().resolve()
        if p.is_dir():
            return p

    # Check environment variable
    env_td = os.environ.get("PIPELINE_TESTDATA")
    if env_td:
        p = Path(env_td).expanduser().resolve()
        if p.is_dir():
            return p

    # Check calibpipe config if available
    try:
        from calibpipe import config as envconfig

        cfg = envconfig.load_merged_config(include_site=True)
        if cfg and cfg.paths and cfg.paths.datapath:
            for dp in cfg.paths.datapath:
                dp_path = Path(dp).expanduser().resolve()
                if (dp_path / "pl-regressiontest").is_dir():
                    return dp_path
                if dp_path.name == "pipeline-testdata" and dp_path.is_dir():
                    return dp_path
    except Exception:
        pass

    # Generic fallback paths
    candidates = [
        Path("/data/pipeline/pipeline-testdata"),
        Path.cwd() / "pipeline-testdata",
        Path.cwd().parent / "pipeline-testdata",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand

    return None


def _extract_versions_from_files(
    paths: Sequence[Path],
    casa_patterns: Sequence[re.Pattern[str]],
    pipe_patterns: Sequence[re.Pattern[str]],
    casa_ver: str | None,
    pipe_ver: str | None,
    max_bytes: int = 16384,
) -> tuple[str | None, str | None]:
    """Scan a sequence of text files (e.g. XML/HTML) for CASA and Pipeline version patterns."""
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:max_bytes]
        except Exception:
            continue
        if not casa_ver:
            for pat in casa_patterns:
                m = pat.search(text)
                if m:
                    casa_ver = normalize_casa_version(m.group(1))
                    break
        if not pipe_ver:
            for pat in pipe_patterns:
                m = pat.search(text)
                if m:
                    pipe_ver = normalize_pipeline_version(m.group(1))
                    break
        if casa_ver and pipe_ver:
            break
    return casa_ver, pipe_ver


def detect_versions_from_run(run_dir: Path) -> tuple[str | None, str | None]:
    """Attempt to detect CASA and Pipeline versions from live run weblogs, logs, or XML reports."""
    casa_ver: str | None = None
    pipe_ver: str | None = None

    # Build ordered list of search directories around run_dir (working/ and parent test dirs;
    # keep products/ separate as a last fallback because restore tests copy old products/).
    live_dirs: list[Path] = []
    products_dirs: list[Path] = []
    cur = run_dir.resolve()
    for _ in range(3):
        if cur.is_dir() and cur not in live_dirs:
            live_dirs.append(cur)
            work = cur / "working"
            if work.is_dir() and work not in live_dirs:
                live_dirs.append(work)
            prod = cur / "products"
            if prod.is_dir() and prod not in products_dirs:
                products_dirs.append(prod)
        if cur.parent == cur:
            break
        cur = cur.parent

    # 1. Check live weblog pipeline-*/html/index.html (generated by the current run)
    weblog_files = [p for d in live_dirs for p in d.glob("pipeline-*/html/index.html")]
    casa_ver, pipe_ver = _extract_versions_from_files(
        weblog_files,
        (REGEX_WEBLOG_CASA_VERSION,),
        (REGEX_WEBLOG_PIPE_VERSION,),
        casa_ver,
        pipe_ver,
    )
    if casa_ver and pipe_ver:
        return casa_ver, pipe_ver

    # 2. Check live working/pipeline_aquareport.xml (non-products)
    live_aqua_files = [d / "pipeline_aquareport.xml" for d in live_dirs if d.name != "products"]
    casa_ver, pipe_ver = _extract_versions_from_files(
        live_aqua_files,
        (REGEX_AQUA_CASA_VERSION,),
        (REGEX_AQUA_PIPE_VERSION,),
        casa_ver,
        pipe_ver,
        max_bytes=8192,
    )
    if casa_ver and pipe_ver:
        return casa_ver, pipe_ver

    # 3. Search casa-*.log, calibpipe.cmd.*.log, or batch.out
    log_candidates: list[Path] = []
    for d in live_dirs:
        log_candidates.extend(d.glob("casa-*.log"))
        log_candidates.extend(d.glob("calibpipe.cmd.*.log"))
        if (d / "batch.out").is_file():
            log_candidates.append(d / "batch.out")

    for log_path in log_candidates:
        if not log_path.is_file():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not casa_ver:
                        m = REGEX_CASA_LOG_VERSION.search(line) or REGEX_CASA_ROOT_VERSION.search(line)
                        if m:
                            casa_ver = normalize_casa_version(m.group(1))
                    if not pipe_ver:
                        m_pipe = REGEX_PIPELINE_LOG_VERSION.search(line)
                        if m_pipe:
                            pipe_ver = normalize_pipeline_version(m_pipe.group(1))
                    if casa_ver and pipe_ver:
                        return casa_ver, pipe_ver
        except Exception:
            continue

    # 4. Last fallback: products/pipeline_aquareport.xml and *pipeline_manifest.xml
    xml_candidates: list[Path] = []
    for d in products_dirs:
        xml_candidates.append(d / "pipeline_aquareport.xml")
        xml_candidates.extend(d.glob("*pipeline_manifest.xml"))
    casa_ver, pipe_ver = _extract_versions_from_files(
        xml_candidates,
        (REGEX_AQUA_CASA_VERSION, REGEX_MANIFEST_CASA_VERSION),
        (REGEX_AQUA_PIPE_VERSION, REGEX_MANIFEST_PIPE_VERSION),
        casa_ver,
        pipe_ver,
        max_bytes=8192,
    )

    return casa_ver, pipe_ver


# Directories to skip when walking test run trees or pipeline-testdata
_PRUNED_DIR_NAMES = frozenset({
    "rawdata",
    "products",
    "htmlcov",
    "weblog",
    "__pycache__",
    "node_modules",
})
_PRUNED_DIR_PREFIXES = (".", "pipeline-")
_PRUNED_DIR_SUFFIXES = (
    ".ms",
    ".flagversions",
    ".cal",
    ".tbl",
    ".image",
    ".model",
    ".residual",
    ".pb",
    ".mask",
    ".psf",
    ".sumwt",
    ".weight",
    ".workdirectory",
    ".asdm",
    ".s",
)


def _should_prune_dir(name: str) -> bool:
    """Return True if directory should be skipped during recursive results discovery."""
    if name in _PRUNED_DIR_NAMES:
        return True
    if name.startswith(_PRUNED_DIR_PREFIXES):
        return True
    if name.endswith(_PRUNED_DIR_SUFFIXES):
        return True
    return False


def build_testdata_index(testdata_root: Path | None) -> dict[str, Path]:
    """Index *.results.txt files under pipeline-testdata/pl-regressiontest in one fast pruned pass."""
    index: dict[str, Path] = {}
    if not testdata_root or not testdata_root.is_dir():
        return index
    reg_root = testdata_root / "pl-regressiontest"
    if not reg_root.is_dir():
        return index

    base_depth = len(reg_root.parts)
    for root, dirs, files in os.walk(reg_root):
        root_path = Path(root)
        depth = len(root_path.parts) - base_depth
        if depth >= 3:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if not _should_prune_dir(d)]
        for fname in files:
            if fname.endswith(".results.txt") and not fname.endswith((".NEW.results.txt", ".candidate", ".staged")):
                index.setdefault(fname, root_path / fname)
    return index


def find_baseline_reference(
    new_results_path: Path,
    testdata_root: Path | None = None,
    explicit_ref: str | Path | None = None,
    testdata_index: dict[str, Path] | None = None,
) -> tuple[Path | None, Path | None]:
    """Find the baseline reference file and the canonical destination file.

    Returns:
        (baseline_reference_file, canonical_testdata_destination_file)
    """
    if explicit_ref:
        p = Path(explicit_ref).resolve()
        return p, p

    search_dirs = [new_results_path.parent]
    if new_results_path.parent.name == "working" and new_results_path.parent.parent.is_dir():
        search_dirs.append(new_results_path.parent.parent)

    local_ref: Path | None = None
    for sdir in search_dirs:
        for f in sdir.glob("*.results.txt"):
            if f.name.endswith(".NEW.results.txt") or f.name.endswith(".candidate") or f.name.endswith(".staged"):
                continue
            local_ref = f
            break
        if local_ref:
            break

    canonical_dest: Path | None = None
    if testdata_root and testdata_root.is_dir():
        reg_root = testdata_root / "pl-regressiontest"
        if reg_root.is_dir():
            ref_name = local_ref.name if local_ref else None
            if ref_name:
                if testdata_index is not None:
                    canonical_dest = testdata_index.get(ref_name)
                else:
                    idx = build_testdata_index(testdata_root)
                    canonical_dest = idx.get(ref_name)

            if not canonical_dest:
                # Try matching dataset folder name from MS or test directory
                name_hints = [
                    new_results_path.parent.parent.name,
                    new_results_path.parent.name,
                    new_results_path.name.split(".")[0],
                ]
                for hint in name_hints:
                    for d in reg_root.iterdir():
                        if d.is_dir() and (d.name.lower() == hint.lower() or d.name.replace("-", "_").lower() == hint.replace("-", "_").lower()):
                            # Pick best matching reference file in this directory
                            ref_candidates = [
                                rf for rf in d.glob("*.results.txt")
                                if not rf.name.endswith(".candidate") and not rf.name.endswith(".staged")
                            ]
                            if ref_candidates:
                                canonical_dest = sorted(ref_candidates)[-1]
                                break
                    if canonical_dest:
                        break

    baseline = canonical_dest or local_ref
    return baseline, canonical_dest


def process_results_update(
    new_results_path: Path,
    ref_path: Path | None = None,
    target_path: Path | None = None,
    testdata_root: Path | None = None,
    testdata_index: dict[str, Path] | None = None,
    casa_ver: str | None = None,
    pipeline_ver: str | None = None,
    apply: bool = False,
    backup: bool = True,
    diff_only: bool = False,
    force: bool = False,
    use_color: bool = True,
    quiet: bool = False,
) -> tuple[int, bool]:
    """Process a single new results file: compare, merge, stage or apply.

    Returns:
        (exit_code, changed_or_written)
    """
    if not new_results_path.is_file():
        print(f"Error: New results file not found: {new_results_path}", file=sys.stderr)
        return 1, False

    new_entries = parse_results_file(new_results_path)

    baseline_ref, canonical_dest = find_baseline_reference(
        new_results_path,
        testdata_root=testdata_root,
        explicit_ref=ref_path,
        testdata_index=testdata_index,
    )

    old_entries: dict[str, RefEntry] = {}
    if baseline_ref and baseline_ref.is_file():
        old_entries = parse_results_file(baseline_ref)

    summary = merge_results(old_entries, new_entries)
    has_diffs = bool(summary.added_keys or summary.removed_keys or summary.modified_keys)

    # Determine destination filename
    dest_file = target_path
    if not dest_file:
        if canonical_dest:
            dest_file = canonical_dest
        elif baseline_ref:
            dest_file = baseline_ref
        else:
            dest_file = new_results_path.parent / new_results_path.name.replace(".NEW.results.txt", ".results.txt")

    # If version override or auto-detected version is specified and dest_file is in a dataset folder, construct new versioned filename
    c_ver = normalize_casa_version(casa_ver) if casa_ver else (REGEX_CASA_VERSION.match(dest_file.name).group(1) if dest_file and REGEX_CASA_VERSION.match(dest_file.name) else None)
    p_ver = normalize_pipeline_version(pipeline_ver) if pipeline_ver else (REGEX_PIPELINE_VERSION.match(dest_file.name).group(1) if dest_file and REGEX_PIPELINE_VERSION.match(dest_file.name) else None)

    if (casa_ver or pipeline_ver) and dest_file and target_path is None:
        eff_c = c_ver or "latest"
        eff_p = p_ver or "latest"
        # Preserve dataset prefix and any suffix after pipeline-<ver> (e.g. '.restore.results.txt')
        m_name = re.match(
            r"^(.*?)\.casa-[\d.]+(?:-\d+)?-pipeline-[\d.]+(?:\.\d+)*\d(.*)$",
            dest_file.name,
        )
        if m_name:
            prefix, suffix = m_name.group(1), m_name.group(2)
        else:
            prefix = dest_file.name.split(".casa-")[0]
            suffix = ".results.txt"
        new_name = f"{prefix}.casa-{eff_c}-pipeline-{eff_p}{suffix}"
        dest_file = dest_file.parent / new_name

    candidate_file = dest_file.with_name(f"{dest_file.name}.candidate") if dest_file else None

    if not has_diffs and baseline_ref and not force:
        if not quiet:
            c = lambda txt, col: colorize(txt, col, enable=use_color)
            print(f"{c('= UNCHANGED:', 'dim')} {new_results_path.name} ({summary.unchanged_count} keys match {baseline_ref.name})")
        return 0, False

    # Generate merged file content
    output_lines = [entry.to_line() + "\n" for entry in summary.merged_entries]
    merged_text = "".join(output_lines)

    if diff_only:
        report = format_summary_report(
            summary,
            new_file=new_results_path,
            ref_file=baseline_ref,
            candidate_file=None,
            casa_ver=c_ver,
            pipeline_ver=p_ver,
            applied=False,
            use_color=use_color,
        )
        print(report)
        return 0, True

    if apply:
        if not dest_file:
            print("Error: Cannot apply changes: No destination target reference file determined.", file=sys.stderr)
            return 1, False
        # Backup existing target if it exists
        if dest_file.exists() and backup:
            bak_path = dest_file.with_suffix(dest_file.suffix + ".bak")
            shutil.copyfile(dest_file, bak_path)

        dest_file.write_text(merged_text, encoding="utf-8")

        # Also remove candidate file if it was previously staged
        if candidate_file and candidate_file.exists():
            candidate_file.unlink()

        if not quiet:
            report = format_summary_report(
                summary,
                new_file=new_results_path,
                ref_file=baseline_ref,
                candidate_file=dest_file,
                casa_ver=c_ver,
                pipeline_ver=p_ver,
                applied=True,
                use_color=use_color,
            )
            print(report)
    else:
        if candidate_file:
            candidate_file.write_text(merged_text, encoding="utf-8")

        if not quiet:
            report = format_summary_report(
                summary,
                new_file=new_results_path,
                ref_file=baseline_ref,
                candidate_file=candidate_file,
                casa_ver=c_ver,
                pipeline_ver=p_ver,
                applied=False,
                use_color=use_color,
            )
            print(report)

    return 0, True


def discover_new_results_files(targets: Sequence[str | Path]) -> list[Path]:
    """Discover all *.NEW.results.txt files across specified target paths using pruned traversal."""
    discovered_set: set[Path] = set()
    for tgt in targets:
        p = Path(tgt).resolve()
        if p.is_file():
            if p.name.endswith(".NEW.results.txt") or p.name.endswith(".results.txt"):
                discovered_set.add(p)
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                root_path = Path(root)
                for fname in files:
                    if fname.endswith(".NEW.results.txt"):
                        discovered_set.add(root_path / fname)
                # *.NEW.results.txt lives directly inside working/; no need to recurse deeper into working/
                if root_path.name == "working":
                    dirs[:] = []
                else:
                    dirs[:] = [d for d in dirs if not _should_prune_dir(d)]
    return sorted(discovered_set)


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    """Build command-line parser for update-ref."""
    parser = argparse.ArgumentParser(
        prog="calibpipe update-ref",
        description="Update and stage regression test reference results files (*.results.txt).",
        add_help=add_help,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Test run directories, working directories, or *.NEW.results.txt files (default: .)",
    )
    parser.add_argument(
        "--testdata",
        help="Root path to pipeline-testdata repository (or pl-regressiontest parent)",
    )
    parser.add_argument(
        "-r",
        "--ref",
        dest="ref_path",
        help="Explicit baseline reference results file (*.results.txt) to merge against",
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="target_path",
        help="Explicit destination path for candidate or updated reference file",
    )
    parser.add_argument(
        "--casa-ver",
        help="Target CASA version string in output filename (e.g. 6.7.4-12)",
    )
    parser.add_argument(
        "--pipeline-ver",
        help="Target Pipeline version string in output filename (e.g. 2026.3.0.42)",
    )
    parser.add_argument(
        "--keep-version",
        "--in-place",
        dest="keep_version",
        action="store_true",
        help="Keep the baseline reference file's version tag instead of stamping the detected run versions",
    )
    parser.add_argument(
        "--apply",
        "--promote",
        dest="apply",
        action="store_true",
        help="Promote and apply candidate changes to the target reference file directly",
    )
    parser.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        default=True,
        help="Do not create a .bak backup file when overwriting existing reference file",
    )
    parser.add_argument(
        "--diff-only",
        action="store_true",
        help="Show diff and change statistics without writing any candidate or output files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write candidate/target file even when there are no differences against baseline",
    )
    parser.add_argument(
        "--no-color",
        dest="use_color",
        action="store_false",
        default=True,
        help="Disable ANSI colored output",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress detailed diff preview and print only summary status",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint for update-ref."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Check NO_COLOR env
    use_color = args.use_color and ("NO_COLOR" not in os.environ)

    testdata_root = find_testdata_root(args.testdata)
    testdata_index = build_testdata_index(testdata_root)

    discovered = discover_new_results_files(args.paths)
    if not discovered:
        print(f"No *.NEW.results.txt files found in: {args.paths}", file=sys.stderr)
        return 1

    if args.target_path and len(discovered) > 1:
        print(
            f"Error: --output cannot be used when multiple *.NEW.results.txt files are discovered ({len(discovered)} found).",
            file=sys.stderr,
        )
        return 1

    exit_code = 0
    changed_count = 0
    unchanged_count = 0
    for new_file in discovered:
        # Detect versions if not explicitly passed and not --keep-version
        c_ver = args.casa_ver
        p_ver = args.pipeline_ver
        if not args.keep_version and (not c_ver or not p_ver):
            detected_c, detected_p = detect_versions_from_run(new_file.parent)
            c_ver = c_ver or detected_c
            p_ver = p_ver or detected_p

        code, changed = process_results_update(
            new_results_path=new_file,
            ref_path=Path(args.ref_path) if args.ref_path else None,
            target_path=Path(args.target_path) if args.target_path else None,
            testdata_root=testdata_root,
            testdata_index=testdata_index,
            casa_ver=c_ver,
            pipeline_ver=p_ver,
            apply=args.apply,
            backup=args.backup,
            diff_only=args.diff_only,
            force=args.force,
            use_color=use_color,
            quiet=args.quiet,
        )
        if code != 0:
            exit_code = code
        elif changed:
            changed_count += 1
        else:
            unchanged_count += 1

    if len(discovered) > 1 and not args.quiet:
        print(
            f"\nSummary: scanned {len(discovered)} run(s) — "
            f"{changed_count} with changes, {unchanged_count} unchanged."
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
