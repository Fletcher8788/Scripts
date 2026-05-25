#!/usr/bin/env python3
"""
Mylar3 Post-Processing Script: Image Converter (Windows 11)
===========================================================
Conversion rules
----------------
  JPG / JPEG        → losslessly optimised JPEG  (mozjpeg jpegtran)
  PNG               → lossless WebP              (cwebp -lossless)
  GIF               → WebP                       (gif2webp)
  Lossless WebP     → lossless WebP              (cwebp -lossless, re-optimise)
  Lossy WebP        → SKIPPED  (kept as-is)

  In all cases: if the converted file is larger than the original,
  the original is kept and the conversion output is discarded.

Mylar3 passes these positional arguments:
    $1  nzb_name        – NZB name
    $2  nzb_folder      – Download folder
    $3  filename        – Comic filename (CBZ etc.)
    $4  file_path       – Full path to the comic file
    $5  seriesmetadata  – Series metadata string

Register in Mylar3  Settings → Quality & Post-Processing → Extra Script Location:
    "C:\\Scripts\\mylar3_image_convert.py"

Dependencies (all resolved from PATH):
    jpegtran  – mozjpeg  https://github.com/mozilla/mozjpeg/releases
    cwebp     – libwebp  https://developers.google.com/speed/webp/download
    gif2webp  – libwebp  (same package as cwebp)

Log files written to LOG_DIR:
    mylar3_convert_YYYY-MM-DD.log  – per-run detail log (one file per day)
    savings_total.log              – running total of all space ever saved
"""

import os
import sys
import json
import shutil
import struct
import logging
import zipfile
import tempfile
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directory where all log files are written
LOG_DIR = Path(r"C:\services\mylar3-python3-dev\postprocesslogs")

# cwebp lossless compression level  (0 = fast/larger … 9 = slow/smaller)
WEBP_COMPRESSION_LEVEL = 9

# Archive extensions Mylar3 produces that will be repacked
ARCHIVE_EXTENSIONS = {".cbz", ".zip"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# One detail log file per calendar day so logs stay manageable
LOG_DIR.mkdir(parents=True, exist_ok=True)
_today     = datetime.now().strftime("%Y-%m-%d")
LOG_FILE   = LOG_DIR / f"mylar3_convert_{_today}.log"

# Running savings ledger (JSON)
SAVINGS_FILE = LOG_DIR / "savings_total.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Savings ledger
# ---------------------------------------------------------------------------

def _load_savings() -> dict:
    """Load the savings ledger from disk, or return a fresh one."""
    if SAVINGS_FILE.exists():
        try:
            return json.loads(SAVINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "total_bytes_saved": 0,
        "total_files_converted": 0,
        "total_files_kept_original": 0,
        "runs": 0,
        "first_run": None,
        "last_run": None,
        "by_format": {
            "jpeg_optimise":    {"converted": 0, "kept_original": 0, "bytes_saved": 0},
            "png_to_webp":      {"converted": 0, "kept_original": 0, "bytes_saved": 0},
            "gif_to_webp":      {"converted": 0, "kept_original": 0, "bytes_saved": 0},
            "webp_reopt":       {"converted": 0, "kept_original": 0, "bytes_saved": 0},
        },
    }


def _save_savings(data: dict) -> None:
    """Persist the savings ledger to disk."""
    try:
        SAVINGS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not write savings ledger: %s", exc)


# Map Action constants → ledger format keys
_FORMAT_KEY = {
    "jpeg_optimise":    "jpeg_optimise",
    "png→webp":         "png_to_webp",
    "gif→webp":         "gif_to_webp",
    "webp(lossless)→webp": "webp_reopt",
}


def record_savings(
    bytes_saved_per_action: dict[str, int],
    kept_per_action:        dict[str, int],
) -> None:
    """
    Update the running savings ledger with the results of this run.

    bytes_saved_per_action  – {action: net bytes saved (may be 0 for kept-original)}
    kept_per_action         – {action: count of files where original was kept}
    """
    data    = _load_savings()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    data["runs"] += 1
    if data["first_run"] is None:
        data["first_run"] = now_iso
    data["last_run"] = now_iso

    for action, saved in bytes_saved_per_action.items():
        fkey = _FORMAT_KEY.get(action)
        if fkey is None:
            continue
        data["total_bytes_saved"]      += saved
        data["total_files_converted"]  += 1
        data["by_format"][fkey]["converted"]   += 1
        data["by_format"][fkey]["bytes_saved"] += saved

    for action, count in kept_per_action.items():
        fkey = _FORMAT_KEY.get(action)
        if fkey is None:
            continue
        data["total_files_kept_original"]         += count
        data["by_format"][fkey]["kept_original"]  += count

    _save_savings(data)
    _log_savings_summary(data)


def _fmt_bytes(n: int | float) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _log_savings_summary(data: dict) -> None:
    """Write a human-readable savings summary to the detail log."""
    total = data["total_bytes_saved"]
    log.info("─" * 64)
    log.info("CUMULATIVE SAVINGS  (all runs since %s)", data.get("first_run", "unknown"))
    log.info("  Total space saved   : %s  (%d bytes)", _fmt_bytes(total), total)
    log.info("  Files converted     : %d", data["total_files_converted"])
    log.info("  Files kept original : %d  (conversion was larger)", data["total_files_kept_original"])
    log.info("  Total runs          : %d", data["runs"])
    log.info("  By format:")
    for fkey, stats in data["by_format"].items():
        log.info(
            "    %-18s  converted: %4d  kept_orig: %4d  saved: %s",
            fkey,
            stats["converted"],
            stats["kept_original"],
            _fmt_bytes(stats["bytes_saved"]),
        )
    log.info("  Ledger file : %s", SAVINGS_FILE)
    log.info("─" * 64)


# ---------------------------------------------------------------------------
# WebP lossless / lossy detection
# ---------------------------------------------------------------------------

def _webp_primary_chunk(path: Path) -> bytes | None:
    """
    Return the primary chunk FourCC from a WebP RIFF container:
        b'VP8 '  – lossy baseline
        b'VP8L'  – lossless
        b'VP8X'  – extended (needs sub-chunk walk)
    Returns None when the file is not a valid WebP.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(16)
    except OSError:
        return None

    if len(header) < 16:
        return None
    if header[0:4] != b"RIFF" or header[8:12] != b"WEBP":
        return None
    return header[12:16]


def is_lossless_webp(path: Path) -> bool:
    """
    Return True only when the WebP file is losslessly encoded.

      VP8   → lossy  → False
      VP8L  → lossless → True
      VP8X  → extended; walk sub-chunks to find VP8 / VP8L
    """
    fourcc = _webp_primary_chunk(path)
    if fourcc is None:
        return False
    if fourcc == b"VP8L":
        return True
    if fourcc == b"VP8 ":
        return False

    if fourcc == b"VP8X":
        try:
            with open(path, "rb") as fh:
                fh.seek(12)                         # skip RIFF<size>WEBP
                while True:
                    raw = fh.read(8)
                    if len(raw) < 8:
                        break
                    cid  = raw[:4]
                    size = struct.unpack_from("<I", raw, 4)[0]
                    if cid in (b"VP8 ", b"VP8L"):
                        return cid == b"VP8L"
                    fh.seek(size + (size & 1), os.SEEK_CUR)  # chunks are word-aligned
        except OSError:
            pass

    return False  # unreadable / unknown → treat as lossy (safe default)


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    """Locate a binary on PATH or raise RuntimeError."""
    path = shutil.which(name) or shutil.which(name + ".exe")
    if not path:
        raise RuntimeError(
            f"'{name}' not found on PATH.\n"
            f"  Make sure mozjpeg / libwebp are installed and added to the system PATH."
        )
    return path


def _run(cmd: list[str]) -> bool:
    """Run a subprocess. Returns True on success."""
    log.debug("CMD: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("  Tool failed (rc=%d): %s", r.returncode, r.stderr.strip())
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Size-aware conversion result
# ---------------------------------------------------------------------------

class Result:
    """
    Returned by convert_one() to describe what happened to a single file.

    kept_path  – path to write into the archive (may be src or dst)
    arc_name   – in-archive filename to use          (may differ from src name)
    action     – one of the Action.* constants
    note       – short human-readable suffix for the log line
    """
    __slots__ = ("kept_path", "arc_name", "action", "note")

    def __init__(self, kept_path: Path, arc_name: str, action: str, note: str = ""):
        self.kept_path = kept_path
        self.arc_name  = arc_name
        self.action    = action
        self.note      = note


class Action:
    JPEG_OPT        = "jpeg_optimise"
    PNG_WEBP        = "png→webp"
    GIF_WEBP        = "gif→webp"
    WEBP_REOPT      = "webp(lossless)→webp"
    SKIP_LOSSY_WEBP = "skip(lossy webp)"
    KEPT_ORIG_SIZE  = "kept(orig smaller)"
    CONV_FAILED     = "conversion failed"
    IGNORE          = "ignore"


# ---------------------------------------------------------------------------
# Per-format tool wrappers
# ---------------------------------------------------------------------------

def _jpeg_optimise(src: Path, dst: Path, jpegtran: str) -> bool:
    """Losslessly optimise a JPEG with mozjpeg jpegtran (no pixel changes)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    return _run([
        jpegtran,
        "-copy",        "none",     # strip all metadata
        "-optimise",                # optimal Huffman tables
        "-progressive",             # progressive scan order
        "-outfile",     str(dst),
        str(src),
    ])


def _to_lossless_webp(src: Path, dst: Path, cwebp: str) -> bool:
    """Convert PNG / lossless-WebP to lossless WebP with cwebp."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    return _run([
        cwebp,
        "-lossless",
        "-z",  str(WEBP_COMPRESSION_LEVEL),
        "-sharp_yuv",
        "-alpha_filter", "best",
        "-metadata",     "all",
        "-mt",              # multi-threaded
        str(src),
        "-o",  str(dst),
    ])


def _gif_to_webp(src: Path, dst: Path, gif2webp: str) -> bool:
    """Convert GIF (including animated) to WebP with gif2webp."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    return _run([
        gif2webp,
        "-min_size",    # minimise output size
        "-mt",
        str(src),
        "-o",  str(dst),
    ])


# ---------------------------------------------------------------------------
# Size comparison helper
# ---------------------------------------------------------------------------

def _pick_smaller(src: Path, dst: Path, action: str, new_arc_name: str) -> Result:
    """
    Compare src vs dst by file size.
    Return a Result that uses whichever is smaller (src wins on a tie).
    """
    src_sz = src.stat().st_size
    dst_sz = dst.stat().st_size

    if dst_sz < src_sz:
        saving = src_sz - dst_sz
        note   = f"saved {_fmt_bytes(saving)} ({saving * 100 // src_sz}%)"
        return Result(dst, new_arc_name, action, note)
    else:
        bloat = dst_sz - src_sz
        note  = f"orig smaller by {_fmt_bytes(bloat)} — kept original"
        return Result(src, src.name, Action.KEPT_ORIG_SIZE, note)


# ---------------------------------------------------------------------------
# Classify a file and convert it
# ---------------------------------------------------------------------------

def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return Action.JPEG_OPT
    if ext == ".png":
        return Action.PNG_WEBP
    if ext == ".gif":
        return Action.GIF_WEBP
    if ext == ".webp":
        return Action.WEBP_REOPT if is_lossless_webp(path) else Action.SKIP_LOSSY_WEBP
    return Action.IGNORE


def convert_one(
    src: Path,
    out_dir: Path,
    jpegtran: str,
    cwebp: str,
    gif2webp: str,
) -> Result:
    """
    Convert a single image file and apply the size-wins rule.

    The returned Result always carries the path that should be written into the
    archive, plus the in-archive name to use.
    """
    action = classify(src)

    # ── Non-convertible ──────────────────────────────────────────────────────
    if action in (Action.SKIP_LOSSY_WEBP, Action.IGNORE):
        return Result(src, src.name, action)

    # ── Attempt conversion ───────────────────────────────────────────────────
    if action == Action.JPEG_OPT:
        dst = out_dir / src.name                    # same extension
        ok  = _jpeg_optimise(src, dst, jpegtran)

    elif action == Action.PNG_WEBP:
        dst = out_dir / (src.stem + ".webp")
        ok  = _to_lossless_webp(src, dst, cwebp)

    elif action == Action.GIF_WEBP:
        dst = out_dir / (src.stem + ".webp")
        ok  = _gif_to_webp(src, dst, gif2webp)

    elif action == Action.WEBP_REOPT:
        dst = out_dir / src.name
        ok  = _to_lossless_webp(src, dst, cwebp)

    else:
        return Result(src, src.name, Action.IGNORE)

    if not ok or not dst.exists():
        return Result(src, src.name, Action.CONV_FAILED, "tool error — kept original")

    # ── Size check ───────────────────────────────────────────────────────────
    return _pick_smaller(src, dst, action, dst.name)


# ---------------------------------------------------------------------------
# Archive processing
# ---------------------------------------------------------------------------

def process_archive(
    archive_path: Path,
    jpegtran: str,
    cwebp: str,
    gif2webp: str,
) -> bool:
    """
    Extract → convert (with size check) → repack a CBZ/ZIP archive in-place.
    Holds a .bak copy until the replacement succeeds.
    Returns True when the archive was successfully written.
    """
    log.info("Opening: %s", archive_path)

    with tempfile.TemporaryDirectory(prefix="mylar3_") as tmp:
        tmp   = Path(tmp)
        exdir = tmp / "extracted"
        cvdir = tmp / "converted"
        exdir.mkdir()
        cvdir.mkdir()

        # ── 1. Extract ────────────────────────────────────────────────────
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(exdir)
                members = zf.namelist()
        except zipfile.BadZipFile as exc:
            log.error("Cannot open archive: %s", exc)
            return False

        # ── 2. Convert every image ────────────────────────────────────────
        results: dict[str, Result] = {}

        counts: dict[str, int] = {
            Action.JPEG_OPT:        0,
            Action.PNG_WEBP:        0,
            Action.GIF_WEBP:        0,
            Action.WEBP_REOPT:      0,
            Action.SKIP_LOSSY_WEBP: 0,
            Action.KEPT_ORIG_SIZE:  0,
            Action.CONV_FAILED:     0,
        }

        # {action: total bytes saved this run} — only for actually converted files
        bytes_saved_by_action: dict[str, int] = {}
        # {action: count of files where original was retained due to size}
        kept_by_action:        dict[str, int] = {}

        for name in members:
            src = exdir / name
            if src.is_dir():
                continue

            out_dir        = cvdir / Path(name).parent
            result         = convert_one(src, out_dir, jpegtran, cwebp, gif2webp)
            results[name]  = result
            counts[result.action] = counts.get(result.action, 0) + 1

            if result.action == Action.IGNORE:
                continue  # non-image file; no log noise

            tag  = result.action.upper().ljust(22)
            note = f"  [{result.note}]" if result.note else ""
            log.info("  %s  %s → %s%s", tag, name, result.arc_name, note)

            # Accumulate savings for the ledger
            orig_action = classify(src)   # the intended conversion action
            fkey        = _FORMAT_KEY.get(orig_action)
            if fkey is None:
                continue

            if result.action == Action.KEPT_ORIG_SIZE:
                kept_by_action[orig_action] = kept_by_action.get(orig_action, 0) + 1
            elif result.action not in (Action.SKIP_LOSSY_WEBP, Action.CONV_FAILED, Action.IGNORE):
                src_sz  = src.stat().st_size
                dst_sz  = result.kept_path.stat().st_size
                saving  = src_sz - dst_sz
                bytes_saved_by_action[orig_action] = (
                    bytes_saved_by_action.get(orig_action, 0) + saving
                )

        # ── 3. Repack ─────────────────────────────────────────────────────
        any_changed = any(
            r.kept_path != (exdir / name)
            for name, r in results.items()
            if r.action not in (Action.IGNORE,)
        )

        # Always repack when anything was attempted, so the archive is clean
        new_arc = tmp / archive_path.name
        with zipfile.ZipFile(new_arc, "w", compression=zipfile.ZIP_STORED) as zout:
            for name in members:
                src = exdir / name
                if src.is_dir():
                    continue
                result   = results.get(name)
                use_path = result.kept_path if result else src
                use_name = result.arc_name  if result else name
                # Preserve original directory prefix inside the archive
                arc_name = str(Path(name).with_name(use_name))
                zout.write(use_path, arc_name)

        # ── 4. Atomic replace with backup ─────────────────────────────────
        backup = archive_path.with_suffix(archive_path.suffix + ".bak")
        shutil.copy2(archive_path, backup)
        try:
            shutil.move(str(new_arc), str(archive_path))
            backup.unlink(missing_ok=True)
        except Exception as exc:
            log.error("Replace failed: %s — restoring backup", exc)
            shutil.copy2(backup, archive_path)
            return False

        log.info(
            "  Done %-40s | "
            "JPEG opt: %d  PNG→WebP: %d  GIF→WebP: %d  WebP re-opt: %d  "
            "Lossy skipped: %d  Orig smaller: %d  Failed: %d",
            archive_path.name,
            counts[Action.JPEG_OPT],
            counts[Action.PNG_WEBP],
            counts[Action.GIF_WEBP],
            counts[Action.WEBP_REOPT],
            counts[Action.SKIP_LOSSY_WEBP],
            counts[Action.KEPT_ORIG_SIZE],
            counts[Action.CONV_FAILED],
        )

        # ── 5. Update running savings ledger ──────────────────────────────
        run_total = sum(bytes_saved_by_action.values())
        log.info("  Space saved this run: %s", _fmt_bytes(run_total))
        record_savings(bytes_saved_by_action, kept_by_action)

        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mylar3 post-processor: optimise JPEG (mozjpeg), "
            "convert PNG/GIF/lossless-WebP to lossless WebP (cwebp/gif2webp), "
            "skip lossy WebP, keep original when it is smaller than the conversion output."
        )
    )
    parser.add_argument("nzb_name")
    parser.add_argument("nzb_folder")
    parser.add_argument("filename")
    parser.add_argument("file_path")
    parser.add_argument("seriesmetadata")
    args = parser.parse_args()

    log.info("=" * 64)
    log.info("Mylar3 Image Converter – started")
    log.info("  file   : %s", args.file_path)
    log.info("  series : %s", args.seriesmetadata)
    log.info("=" * 64)

    # ── Locate binaries on PATH ───────────────────────────────────────────────
    try:
        jpegtran = _require("jpegtran")
        cwebp    = _require("cwebp")
        gif2webp = _require("gif2webp")
    except RuntimeError as exc:
        log.error(str(exc))
        return 1

    log.info("jpegtran : %s", jpegtran)
    log.info("cwebp    : %s", cwebp)
    log.info("gif2webp : %s", gif2webp)

    # ── Resolve the comic file ────────────────────────────────────────────────
    file_path = Path(args.file_path)
    if not file_path.exists():
        candidate = Path(args.nzb_folder) / args.filename
        if candidate.exists():
            file_path = candidate
        else:
            log.error("Comic file not found: %s", file_path)
            return 1

    # ── Dispatch ──────────────────────────────────────────────────────────────
    ext = file_path.suffix.lower()

    if ext in ARCHIVE_EXTENSIONS:
        return 0 if process_archive(file_path, jpegtran, cwebp, gif2webp) else 1

    # Single standalone image file (unusual but handle gracefully)
    action = classify(file_path)

    if action == Action.IGNORE:
        log.warning("Unsupported file type '%s'; nothing to do.", ext)
        return 0

    if action == Action.SKIP_LOSSY_WEBP:
        log.info("Lossy WebP – skipping %s", file_path.name)
        return 0

    with tempfile.TemporaryDirectory(prefix="mylar3_") as tmp:
        result = convert_one(file_path, Path(tmp), jpegtran, cwebp, gif2webp)

        if result.action == Action.KEPT_ORIG_SIZE:
            log.info("Original kept (conversion was larger): %s  [%s]", file_path.name, result.note)
            record_savings({}, {classify(file_path): 1})
            return 0

        if result.kept_path != file_path and result.kept_path.exists():
            src_sz  = file_path.stat().st_size
            dst_sz  = result.kept_path.stat().st_size
            saving  = src_sz - dst_sz
            dest    = file_path.with_name(result.arc_name)
            shutil.move(str(result.kept_path), str(dest))
            if dest != file_path:
                file_path.unlink(missing_ok=True)
            log.info("Converted: %s → %s  [%s]", file_path.name, dest.name, result.note)
            record_savings({classify(file_path): saving}, {})
            return 0

    log.error("Conversion failed for %s", file_path.name)
    return 1


if __name__ == "__main__":
    sys.exit(main())
