#!/usr/bin/env python3
"""
CBL → JSON Comic Reading List Converter
Converts .cbl (XML) files to the JSON CBL Standard schema

Schema:  https://github.com/ComicReadingLists/json-cbl-standard

CBL structure (actual):
  <ReadingList>
    <n>List Title</n>
    <NumIssues>N</NumIssues>
    <Books>
      <Book Series="..." Number="..." Volume="<seriesStartYear>" Year="<coverYear>">
        <Database Name="cv" Series="<cvSeriesId>" Issue="<cvIssueId>" />
      </Book>
      ...
    </Books>
  </ReadingList>

JSON schema requirements:
  fileDetails  : { version, UUID }
  listDetails  : { name, ... }
  issueList    : [ { seriesName, seriesStartYear, issueNumber, issueCoverDate,
                     issueType?, id?: [{name, series, issue}] } ]
"""

import xml.etree.ElementTree as ET
import json
import argparse
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1.0

# CBL Database/@Name  →  JSON schema enum value
DB_NAME_MAP = {
    "cv":                  "comicvine",
    "comicvine":           "comicvine",
    "metron":              "metron",
    "gcd":                 "grandComicsDatabase",
    "grandcomicsdatabase": "grandComicsDatabase",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(el, tag, default=None):
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return default


def _attr(el, name, default=None):
    val = el.get(name)
    return val.strip() if val else default


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cover_date(year_str):
    """Build an ISO-8601 date from a year string; schema requires YYYY-MM-DD."""
    y = _int(year_str)
    if y and 1900 <= y <= 2100:
        return f"{y:04d}-01-01"
    return "1900-01-01"


# ---------------------------------------------------------------------------
# Issue parsing
# ---------------------------------------------------------------------------

def parse_book(book_el):
    """Convert a <Book> element to a JSON issueList entry."""

    series_name  = _attr(book_el, "Series") or "Unknown Series"
    issue_number = _attr(book_el, "Number") or "1"
    # In CBL, Volume = the series launch year; Year = cover/publication year
    series_start = _int(_attr(book_el, "Volume"))
    cover_year   = _attr(book_el, "Year")

    issue = {
        "seriesName":      series_name,
        "seriesStartYear": series_start if series_start else _int(cover_year, 1900),
        "issueNumber":     issue_number,
        "issueCoverDate":  _cover_date(cover_year),
    }

    # Database IDs (e.g. ComicVine)
    ids = []
    for db_el in book_el.findall("Database"):
        raw_name    = _attr(db_el, "Name", "").lower()
        schema_name = DB_NAME_MAP.get(raw_name)
        if not schema_name:
            continue  # unknown DB – skip (schema enforces an enum)
        series_id = _attr(db_el, "Series")
        issue_id  = _attr(db_el, "Issue")
        if series_id and issue_id:
            ids.append({"name": schema_name, "series": series_id, "issue": issue_id})

    if ids:
        issue["id"] = ids

    return issue


# ---------------------------------------------------------------------------
# Root / reading-list parsing
# ---------------------------------------------------------------------------

def parse_cbl(input_path):
    """Parse a CBL file and return a schema-compliant dict."""
    try:
        tree = ET.parse(str(input_path))
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    root = tree.getroot()

    # Handle optional <ReadingLists> wrapper
    if root.tag == "ReadingLists":
        root = root.find("ReadingList")
        if root is None:
            raise ValueError("No <ReadingList> element found.")

    if root.tag != "ReadingList":
        raise ValueError(f"Unexpected root element <{root.tag}>; expected <ReadingList>.")

    # --- List name: CBL uses <n> (not <Name>) ---
    name = (
        _text(root, "n")
        or _text(root, "Name")
        or _attr(root, "Name")
        or Path(input_path).stem
        or "Untitled Reading List"
    )

    description = _text(root, "Description") or _attr(root, "Description")

    # --- Issues ---
    books_el = root.find("Books")
    book_elements = books_el.findall("Book") if books_el is not None else root.findall("Book")
    issue_list = [parse_book(b) for b in book_elements]

    # --- Derive year range from cover dates ---
    cover_years = [_int(i["issueCoverDate"][:4]) for i in issue_list if i.get("issueCoverDate")]
    cover_years = [y for y in cover_years if y]

    # --- Build output ---
    out = {
        "$schema": (
            "https://raw.githubusercontent.com/ComicReadingLists/"
            "json-cbl-standard/main/schema/1.0/comic-reading-list.schema.json"
        ),
        "fileDetails": {
            "version": SCHEMA_VERSION,
            "UUID":    str(uuid.uuid4()),
        },
        "listDetails": {
            "name": name,
        },
        "issueList": issue_list,
    }

    if description:
        out["listDetails"]["description"] = description
    if cover_years:
        out["listDetails"]["startYear"] = min(cover_years)
        out["listDetails"]["endYear"]   = max(cover_years)

    return out


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def convert_file(input_path, output_path, pretty):
    data   = parse_cbl(input_path)
    indent = 2 if pretty else None
    Path(output_path).write_text(
        json.dumps(data, indent=indent, ensure_ascii=False),
        encoding="utf-8",
    )
    return data


def report(input_path, output_path, data):
    ld = data["listDetails"]
    print(f"  Input  : {input_path}")
    print(f"  Output : {output_path}")
    print(f"  Name   : {ld['name']}")
    print(f"  Issues : {len(data['issueList'])}")
    if "startYear" in ld:
        print(f"  Years  : {ld['startYear']} – {ld['endYear']}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert .cbl (XML) comic reading-list files to JSON CBL Standard v1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single file – output placed beside the input
  python cbl_to_json.py "Doctor Strange.cbl"

  # Single file – explicit output path
  python cbl_to_json.py "Doctor Strange.cbl" -o lists/doctor-strange.json

  # Batch: convert every .cbl found recursively under a directory
  python cbl_to_json.py --batch ./CBL-ReadingLists/

  # Batch with a separate output root (mirrors input structure)
  python cbl_to_json.py --batch ./CBL-ReadingLists/ --output-dir ./json-lists/

  # Compact (non-indented) JSON
  python cbl_to_json.py "Doctor Strange.cbl" --no-pretty
        """,
    )

    parser.add_argument("input", nargs="?", type=Path,
                        help="Path to a .cbl file (omit when using --batch).")
    parser.add_argument("-o", "--output", type=Path, default=None, metavar="FILE",
                        help="Output .json path (single-file mode only).")
    parser.add_argument("--batch", type=Path, default=None, metavar="DIR",
                        help="Directory to scan recursively for .cbl files.")
    parser.add_argument("--output-dir", type=Path, default=None, metavar="DIR",
                        help="Root output directory for --batch (mirrors input structure).")
    parser.add_argument("--no-pretty", action="store_true", default=False,
                        help="Write compact JSON instead of indented output.")

    args   = parser.parse_args()
    pretty = not args.no_pretty

    # ── Batch mode ──────────────────────────────────────────────────────────
    if args.batch:
        if not args.batch.is_dir():
            sys.exit(f"ERROR: --batch path is not a directory: {args.batch}")
        cbl_files = sorted(args.batch.rglob("*.cbl"))
        if not cbl_files:
            sys.exit(f"No .cbl files found under: {args.batch}")
        print(f"Found {len(cbl_files)} .cbl file(s) under {args.batch}\n")
        ok = err = 0
        for cbl in cbl_files:
            if args.output_dir:
                rel  = cbl.relative_to(args.batch)
                dest = (args.output_dir / rel).with_suffix(".json")
                dest.parent.mkdir(parents=True, exist_ok=True)
            else:
                dest = cbl.with_suffix(".json")
            try:
                data = convert_file(cbl, dest, pretty)
                report(cbl, dest, data)
                ok += 1
            except (ValueError, OSError) as exc:
                print(f"  ERROR  : {cbl}\n  Reason : {exc}\n")
                err += 1
        print(f"Done – {ok} converted, {err} failed.")
        if err:
            sys.exit(1)

    # ── Single-file mode ────────────────────────────────────────────────────
    elif args.input:
        if not args.input.is_file():
            sys.exit(f"ERROR: File not found: {args.input}")
        dest = args.output or args.input.with_suffix(".json")
        try:
            data = convert_file(args.input, dest, pretty)
            report(args.input, dest, data)
        except (ValueError, OSError) as exc:
            sys.exit(f"ERROR: {exc}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
