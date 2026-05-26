#!/usr/bin/env python3
"""
cbz_to_jxl.py — Losslessly re-encode JPG/PNG/WebP images inside CBZ files to JXL.

Encoding strategy:
  • JPEG  → JXL via --lossless_jpeg=1  (bit-perfect JPEG transcoding, no decode/re-encode)
  • PNG   → JXL via --distance=0       (pixel-perfect lossless)
  • WebP  → JXL via --distance=0       (pixel-perfect lossless; cjxl decodes WebP internally)

Non-image files (xml, txt, …) are kept unchanged.
The original CBZ is replaced only on full success; a .bak copy is kept until then.

Requirements:
  cjxl  (libjxl)  — https://github.com/libjxl/libjxl
"""

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CJXL_BINARY = "cjxl"          # override with --cjxl if not on PATH
EFFORT = 9                     # 1 (fastest) – 9 (smallest/slowest)
EXTRA_CJXL_ARGS: list[str] = []  # e.g. ["--num_threads=4"]

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
PNG_EXTENSIONS  = {".png"}
WEBP_EXTENSIONS = {".webp"}
ALL_IMAGE_EXTS  = JPEG_EXTENSIONS | PNG_EXTENSIONS | WEBP_EXTENSIONS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger("cbz_to_jxl")


def _run(cmd: list[str]) -> bool:
    """Run a subprocess; return True on success."""
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("Command failed (exit %d): %s", result.returncode, " ".join(cmd))
        if result.stderr:
            log.error("stderr: %s", result.stderr.strip())
    return result.returncode == 0


def _encode_jpeg(src: Path, dst: Path, cjxl: str) -> bool:
    """Lossless JPEG transcoding — bit-perfect, no pixel decode."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    return _run([
        cjxl,
        "--lossless_jpeg=1",
        "-e", str(EFFORT),
        *EXTRA_CJXL_ARGS,
        str(src),
        str(dst),
    ])


def _encode_lossless(src: Path, dst: Path, cjxl: str) -> bool:
    """Pixel-perfect lossless encode for PNG / WebP."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    return _run([
        cjxl,
        "--distance=0",
        "-e", str(EFFORT),
        *EXTRA_CJXL_ARGS,
        str(src),
        str(dst),
    ])


def _convert_image(src: Path, dst: Path, cjxl: str) -> bool:
    """Dispatch to the right encoder based on source extension."""
    ext = src.suffix.lower()
    if ext in JPEG_EXTENSIONS:
        return _encode_jpeg(src, dst, cjxl)
    if ext in PNG_EXTENSIONS | WEBP_EXTENSIONS:
        return _encode_lossless(src, dst, cjxl)
    raise ValueError(f"Unsupported image extension: {ext}")


# ---------------------------------------------------------------------------
# CBZ processing
# ---------------------------------------------------------------------------

def process_cbz(cbz_path: Path, cjxl: str, dry_run: bool = False) -> bool:
    """
    Re-encode all images inside a CBZ to JXL.
    Returns True if the CBZ was successfully updated (or dry_run skipped it).
    """
    log.info("Processing: %s", cbz_path)

    with tempfile.TemporaryDirectory(prefix="cbz_jxl_") as tmp:
        tmp_path   = Path(tmp)
        extract_dir = tmp_path / "extracted"
        repack_dir  = tmp_path / "repack"
        extract_dir.mkdir()
        repack_dir.mkdir()

        # 1. Extract CBZ (it's just a ZIP)
        try:
            with zipfile.ZipFile(cbz_path, "r") as zf:
                zf.extractall(extract_dir)
                members = zf.namelist()
        except zipfile.BadZipFile as exc:
            log.error("Not a valid ZIP/CBZ: %s — %s", cbz_path, exc)
            return False

        # 2. Convert / copy each file
        ok = True
        for member in members:
            src = extract_dir / member
            if src.is_dir():
                (repack_dir / member).mkdir(parents=True, exist_ok=True)
                continue

            ext = src.suffix.lower()
            if ext in ALL_IMAGE_EXTS:
                dst = repack_dir / Path(member).with_suffix(".jxl")
                log.debug("  encode: %s → %s", member, dst.name)
                if not dry_run and not _convert_image(src, dst, cjxl):
                    log.error("  FAILED: %s", member)
                    ok = False
                    break
            else:
                # Keep non-image files as-is (comicinfo.xml, etc.)
                dst = repack_dir / member
                dst.parent.mkdir(parents=True, exist_ok=True)
                log.debug("  copy:   %s", member)
                if not dry_run:
                    shutil.copy2(src, dst)

        if not ok:
            log.error("Aborted — original CBZ left untouched: %s", cbz_path)
            return False

        if dry_run:
            log.info("  [dry-run] would re-encode %d image(s)", sum(
                1 for m in members if Path(m).suffix.lower() in ALL_IMAGE_EXTS
            ))
            return True

        # 3. Repack into a new CBZ (ZIP with no compression — images are already compressed)
        new_cbz = tmp_path / cbz_path.name
        with zipfile.ZipFile(new_cbz, "w", compression=zipfile.ZIP_STORED) as zf:
            for f in sorted(repack_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(repack_dir))

        # 4. Atomic replace: keep .bak until new file is confirmed written
        bak = cbz_path.with_suffix(".cbz.bak")
        cbz_path.rename(bak)
        shutil.move(str(new_cbz), cbz_path)
        bak.unlink()
        log.info("  Done: %s", cbz_path)
        return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_cbz_files(paths: list[Path], recursive: bool) -> list[Path]:
    result = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".cbz":
            result.append(p)
        elif p.is_dir():
            glob = p.rglob("*.cbz") if recursive else p.glob("*.cbz")
            result.extend(sorted(glob))
        else:
            log.warning("Skipping (not a .cbz or directory): %s", p)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Losslessly re-encode images inside CBZ files to JXL."
    )
    parser.add_argument("inputs", nargs="+", type=Path,
                        help="CBZ file(s) or director(y|ies) to process")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Recurse into directories")
    parser.add_argument("--cjxl", default=CJXL_BINARY,
                        help=f"Path to cjxl binary (default: {CJXL_BINARY})")
    parser.add_argument("--effort", type=int, default=EFFORT, choices=range(1, 10),
                        metavar="1-9",
                        help=f"cjxl effort level (default: {EFFORT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without changing any files")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show debug output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    global EFFORT
    EFFORT = args.effort

    # Check cjxl is available
    if not shutil.which(args.cjxl):
        log.error("cjxl not found: %s — install libjxl or pass --cjxl /path/to/cjxl", args.cjxl)
        return 1

    cbz_files = find_cbz_files(args.inputs, args.recursive)
    if not cbz_files:
        log.error("No CBZ files found.")
        return 1

    log.info("Found %d CBZ file(s).", len(cbz_files))
    failed = []
    for cbz in cbz_files:
        if not process_cbz(cbz, args.cjxl, dry_run=args.dry_run):
            failed.append(cbz)

    if failed:
        log.error("%d file(s) failed:", len(failed))
        for f in failed:
            log.error("  %s", f)
        return 1

    log.info("All done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
