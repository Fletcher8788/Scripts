"""
komga_kavita_sync.py
────────────────────
Bidirectional reading progress sync supporting:
  • Komga A  ↔  Kavita
  • Komga A  ↔  Komga B

Sync order (each pair runs independently):
  Pass 1  Server A → Server B   push progress that is ahead on A
  Pass 2  Server B → Server A   push any progress still ahead on B after Pass 1

Matches books by relative path (after stripping each server's media root prefix).
This handles mixed OS paths — e.g. "F:/data/media" on Windows vs "/data/media" on Linux.
"""

import json
import logging
import sys
from pathlib import Path

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

# Primary Komga instance (required)
KOMGA_A_URL        = "http://localhost:25600"
KOMGA_A_API_KEY    = "Komga API"         # Komga → My Account → API Keys
KOMGA_A_MEDIA_ROOT = "/data/media"                # Path as Komga A sees it

# Kavita instance — set SYNC_KOMGA_KAVITA = False to disable this pair
SYNC_KOMGA_KAVITA  = True
KAVITA_URL         = "http://localhost:5000"
KAVITA_API_KEY     = "Kavita API"           # Kavita → Settings → Auth Keys
KAVITA_MEDIA_ROOT  = "/data/media"                  # Path as Kavita sees it

# Second Komga instance — set SYNC_KOMGA_KOMGA = False to disable this pair
SYNC_KOMGA_KOMGA   = False
KOMGA_B_URL        = "http://localhost:25600"
KOMGA_B_API_KEY    = "Komga API"         # Komga → My Account → API Keys
KOMGA_B_MEDIA_ROOT = "F:\data\media"                  # Path as Komga B sees it

# Log file — written to %USERPROFILE%\komga_kavita_sync.log
LOG_FILE  = Path.home() / "komga_kavita_sync.log"
LOG_LEVEL = logging.DEBUG     # Change to logging.DEBUG to trace every comparison

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Path normalisation ────────────────────────────────────────────────────────

def _rel_key(raw_path: str, media_root: str) -> str:
    """
    Strip the server-specific media root prefix from a file path and return
    the normalised relative portion as the match key.

    e.g. "F:\\data\\media\\Manga\\Series\\Vol1.cbz"  with root "F:/data/media"
         -> "manga/series/vol1.cbz"   (lowercased for case-insensitive match)

         "/data/media/Manga/Series/Vol1.cbz"  with root "/data/media"
         -> "manga/series/vol1.cbz"
    """
    norm_path = raw_path.replace("\\", "/")
    norm_root = media_root.rstrip("/").replace("\\", "/")

    if norm_path.lower().startswith(norm_root.lower() + "/"):
        rel = norm_path[len(norm_root) + 1:]
    else:
        # Root prefix not found — fall back to filename only
        rel = norm_path.split("/")[-1]
        log.debug(f"  Root prefix not matched for path: {raw_path!r}")

    return rel.lower()


# ── Komga helpers ─────────────────────────────────────────────────────────────

class KomgaServer:
    """Thin wrapper that binds a Komga URL + API key together."""

    def __init__(self, name: str, base_url: str, api_key: str, media_root: str):
        self.name       = name
        self.base_url   = base_url.rstrip("/")
        self.media_root = media_root
        self._session   = requests.Session()
        self._session.headers.update({"X-API-Key": api_key})

    def build_book_map(self) -> dict:
        """
        Fetches ALL books and returns:
            { rel_path: { "book_id", "page", "completed", "total_pages" } }
        """
        book_map: dict = {}
        page = 0

        while True:
            r = self._session.get(
                f"{self.base_url}/api/v1/books",
                params={"size": 500, "page": page},
            )
            r.raise_for_status()
            data  = r.json()
            books = data.get("content", [])

            if not books:
                break

            for book in books:
                rel  = _rel_key(book["url"], self.media_root)
                prog = book.get("readProgress") or {}
                book_map[rel] = {
                    "book_id":     book["id"],
                    "page":        prog.get("page", 0),
                    "completed":   prog.get("completed", False),
                    "total_pages": book.get("media", {}).get("pagesCount", 0),
                }

            if data.get("last", True):
                break
            page += 1

        started = sum(1 for v in book_map.values() if v["page"] > 0 or v["completed"])
        log.info(f"{self.name}: indexed {len(book_map)} books total, {started} with progress")
        return book_map

    def set_progress(self, book_id: str, kavita_page: int, total_pages: int) -> None:
        """
        Write progress. kavita_page is on Kavita scale (0-indexed; totalPages = completed).
        """
        completed  = kavita_page >= total_pages
        komga_page = min(kavita_page + 1, total_pages)  # clamp and convert to 1-indexed

        r = self._session.patch(
            f"{self.base_url}/api/v1/books/{book_id}/read-progress",
            json={"page": komga_page, "completed": completed},
        )
        r.raise_for_status()

    @staticmethod
    def as_kavita_page(book: dict) -> int:
        """
        Convert a Komga book dict to a Kavita-scale page number for comparison.
          completed    -> totalPages      (Kavita's "read" marker)
          in-progress  -> page - 1        (convert to 0-indexed)
          not started  -> 0
        """
        if book["completed"]:
            return book["total_pages"]
        return max(book["page"] - 1, 0)


# ── Komga <-> Komga sync ──────────────────────────────────────────────────────

def _sync_komga_pass(
    src: KomgaServer,
    dst: KomgaServer,
    src_books: dict,
    dst_books: dict,
    label: str,
) -> tuple:
    """One-directional pass: push progress from src to dst where src is ahead."""
    updated = skipped = 0

    for rel in set(src_books) & set(dst_books):
        src_book  = src_books[rel]
        dst_book  = dst_books[rel]
        src_kpage = KomgaServer.as_kavita_page(src_book)
        dst_kpage = KomgaServer.as_kavita_page(dst_book)

        if src_kpage > dst_kpage:
            dst.set_progress(dst_book["book_id"], src_kpage, dst_book["total_pages"])
            log.info(
                f"[{label}] {rel}: page {dst_kpage} -> {src_kpage}"
                + (" (completed)" if src_kpage >= src_book["total_pages"] else "")
            )
            updated += 1
        else:
            log.debug(f"[{label} skip] {rel}: dst {dst_kpage} >= src {src_kpage}")
            skipped += 1

    return updated, skipped


def sync_komga_komga(server_a: KomgaServer, server_b: KomgaServer) -> None:
    log.info("=" * 60)
    log.info(f"Syncing {server_a.name} <-> {server_b.name}")
    log.info("=" * 60)

    books_a = server_a.build_book_map()
    books_b = server_b.build_book_map()

    common    = set(books_a) & set(books_b)
    unmatched = len(books_a) - len(common)
    log.info(f"Matched {len(common)} files present in both servers")

    log.info("-" * 60)
    log.info(f"Pass 1: {server_a.name} -> {server_b.name}")
    p1_up, p1_sk = _sync_komga_pass(server_a, server_b, books_a, books_b, f"{server_a.name}->{server_b.name}")
    log.info(f"Pass 1 done -- {p1_up} updated, {p1_sk} already up to date")

    # Refresh B so Pass 2 sees the updates from Pass 1
    books_b = server_b.build_book_map()

    log.info("-" * 60)
    log.info(f"Pass 2: {server_b.name} -> {server_a.name}")
    p2_up, p2_sk = _sync_komga_pass(server_b, server_a, books_b, books_a, f"{server_b.name}->{server_a.name}")
    log.info(f"Pass 2 done -- {p2_up} updated, {p2_sk} already up to date")

    log.info("=" * 60)
    log.info(
        f"{server_a.name} <-> {server_b.name} complete -- "
        f"{p1_up + p2_up} updated, {unmatched} unmatched in {server_b.name}"
    )


# ── Kavita helpers ────────────────────────────────────────────────────────────

def _kavita_session() -> requests.Session:
    """Kavita auth: POST /api/Plugin/authenticate -> JWT Bearer token."""
    r = requests.post(
        f"{KAVITA_URL}/api/Plugin/authenticate",
        params={"apiKey": KAVITA_API_KEY, "pluginName": "KomgaSync"},
        headers={"accept": "application/json"},
    )
    if not r.ok:
        raise RuntimeError(
            f"Kavita Plugin/authenticate failed (HTTP {r.status_code}).\n"
            f"Check KAVITA_URL='{KAVITA_URL}' and KAVITA_API_KEY.\n"
            f"Response: {r.text[:300]}"
        )
    token = r.json().get("token", "")
    if not token:
        raise RuntimeError(f"Kavita auth response missing token.\nResponse: {r.text[:300]}")
    log.debug("Kavita: authenticated via Plugin/authenticate")
    s = requests.Session()
    s.headers.update({
        "Authorization": f"bearer {token}",
        "Content-Type":  "application/json",
        "accept":        "application/json",
    })
    return s


def _kavita_safe_json(r: requests.Response, label: str):
    if not r.content:
        raise RuntimeError(f"{label} returned HTTP {r.status_code} with empty body.")
    try:
        return r.json()
    except Exception:
        raise RuntimeError(
            f"{label} returned HTTP {r.status_code} but body is not JSON.\n"
            f"First 300 chars: {r.text[:300]!r}"
        )


def kavita_build_chapter_map(ks: requests.Session) -> dict:
    """Returns: { rel_path: { chapterId, volumeId, seriesId, libraryId, totalPages } }"""
    chapter_map: dict = {}

    libs_r = ks.get(f"{KAVITA_URL}/api/Library/libraries")
    log.debug(f"GET /api/Library/libraries -> HTTP {libs_r.status_code}")
    if not libs_r.ok:
        raise RuntimeError(
            f"GET /api/Library/libraries failed (HTTP {libs_r.status_code}).\n"
            f"Response: {libs_r.text[:300]}"
        )
    libraries = _kavita_safe_json(libs_r, "GET /api/Library/libraries")
    if not isinstance(libraries, list):
        raise RuntimeError(f"Expected list from /api/Library/libraries, got {type(libraries).__name__}.")
    log.info(f"Kavita: found {len(libraries)} libraries")

    for lib in libraries:
        lib_id      = lib["id"]
        lib_name    = lib["name"]
        series_page = 0

        while True:
            series_r = ks.post(
                f"{KAVITA_URL}/api/Series/v2",
                params={"libraryId": lib_id, "PageNumber": series_page, "PageSize": 500},
                json={"readStatus": {"notRead": True, "inProgress": True, "read": True}},
            )
            if not series_r.ok:
                log.warning(
                    f"POST /api/Series/v2 for library '{lib_name}' page {series_page} "
                    f"failed (HTTP {series_r.status_code}): {series_r.text[:200]}"
                )
                break

            series_data = _kavita_safe_json(series_r, f"POST /api/Series/v2 lib={lib_id} page={series_page}")

            if isinstance(series_data, list):
                series_list  = series_data
                is_last_page = True
            else:
                series_list  = series_data.get("result", [])
                total_pages  = series_data.get("totalPages", 1)
                is_last_page = series_page + 1 >= total_pages

            log.debug(f"  Library '{lib_name}' page {series_page}: {len(series_list)} series")

            for series in series_list:
                series_id = series["id"]
                vols_r = ks.get(f"{KAVITA_URL}/api/Series/volumes", params={"seriesId": series_id})
                if not vols_r.ok:
                    log.debug(f"    Skipping series {series_id}: HTTP {vols_r.status_code}")
                    continue

                for vol in vols_r.json():
                    vol_id = vol["id"]
                    for chapter in vol.get("chapters", []):
                        chapter_id  = chapter["id"]
                        total_pages = chapter.get("pages", 0)
                        for f in chapter.get("files", []):
                            rel = _rel_key(f.get("filePath", ""), KAVITA_MEDIA_ROOT)
                            if rel:
                                chapter_map[rel] = {
                                    "chapterId":  chapter_id,
                                    "volumeId":   vol_id,
                                    "seriesId":   series_id,
                                    "libraryId":  lib_id,
                                    "totalPages": total_pages,
                                }

            if is_last_page:
                break
            series_page += 1

    log.info(f"Kavita: indexed {len(chapter_map)} chapters")
    return chapter_map


def kavita_get_progress(ks: requests.Session, chapter_id: int) -> int:
    r = ks.get(f"{KAVITA_URL}/api/Reader/get-progress", params={"chapterId": chapter_id})
    if r.status_code == 404:
        return 0
    r.raise_for_status()
    return r.json().get("pageNum", 0)


def kavita_set_progress(
    ks: requests.Session,
    series_id: int,
    volume_id: int,
    chapter_id: int,
    page_num: int,
    library_id: int = 0,
) -> None:
    page_num = max(page_num, 0)
    payload  = {
        "libraryId":    library_id,
        "volumeId":     volume_id,
        "chapterId":    chapter_id,
        "pageNum":      page_num,
        "seriesId":     series_id,
        "bookScrollId": None,
    }
    log.debug(f"  POST /api/Reader/progress  payload={payload}")
    r = ks.post(f"{KAVITA_URL}/api/Reader/progress", data=json.dumps(payload))
    if not r.ok:
        raise RuntimeError(
            f"kavita_set_progress failed (HTTP {r.status_code}) for "
            f"chapterId={chapter_id} libraryId={library_id} page={page_num}\n"
            f"Payload: {payload}\nResponse: {r.text[:400]}"
        )


# ── Komga <-> Kavita sync ─────────────────────────────────────────────────────

def sync_komga_kavita(komga: KomgaServer, ks: requests.Session) -> None:
    log.info("=" * 60)
    log.info(f"Syncing {komga.name} <-> Kavita")
    log.info("=" * 60)

    komga_books     = komga.build_book_map()
    kavita_chapters = kavita_build_chapter_map(ks)

    common    = set(komga_books) & set(kavita_chapters)
    unmatched = len(komga_books) - len(common)
    log.info(f"Matched {len(common)} files present in both servers")

    p1_updated = p1_skipped = 0
    p2_updated = p2_skipped = 0

    # ── Pass 1: Komga -> Kavita ───────────────────────────────────────────────
    log.info("-" * 60)
    log.info(f"Pass 1: {komga.name} -> Kavita")

    for rel in common:
        kb      = komga_books[rel]
        chapter = kavita_chapters[rel]

        chapter_id  = chapter["chapterId"]
        total_pages = chapter["totalPages"]

        komga_as_kpage = total_pages if kb["completed"] else max(kb["page"] - 1, 0)
        kavita_page    = kavita_get_progress(ks, chapter_id)

        if komga_as_kpage > kavita_page:
            kavita_set_progress(
                ks,
                chapter["seriesId"],
                chapter["volumeId"],
                chapter_id,
                komga_as_kpage,
                chapter["libraryId"],
            )
            log.info(
                f"[K->V] {rel}: page {kavita_page} -> {komga_as_kpage}"
                + (" (completed)" if kb["completed"] else "")
            )
            p1_updated += 1
        else:
            log.debug(f"[K->V skip] {rel}: Kavita {kavita_page} >= Komga {komga_as_kpage}")
            p1_skipped += 1

    log.info(f"Pass 1 done -- {p1_updated} updated, {p1_skipped} already up to date")

    # ── Pass 2: Kavita -> Komga ───────────────────────────────────────────────
    log.info("-" * 60)
    log.info(f"Pass 2: Kavita -> {komga.name}")

    for rel in common:
        kb      = komga_books[rel]
        chapter = kavita_chapters[rel]

        chapter_id  = chapter["chapterId"]
        total_pages = chapter["totalPages"]

        kavita_page    = kavita_get_progress(ks, chapter_id)
        komga_as_kpage = total_pages if kb["completed"] else max(kb["page"] - 1, 0)

        if kavita_page > komga_as_kpage:
            komga.set_progress(kb["book_id"], kavita_page, total_pages)
            log.info(
                f"[V->K] {rel}: Komga page {kb['page']} -> {kavita_page + 1}"
                + (" (completed)" if kavita_page >= total_pages else "")
            )
            p2_updated += 1
        else:
            log.debug(f"[V->K skip] {rel}: Komga {kb['page']} >= Kavita {kavita_page}")
            p2_skipped += 1

    log.info("=" * 60)
    log.info(
        f"{komga.name} <-> Kavita complete -- "
        f"Pass 1: {p1_updated} updated | Pass 2: {p2_updated} updated | "
        f"Unmatched: {unmatched}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def sync() -> None:
    log.info("=" * 60)
    log.info("Starting sync run")
    log.info("=" * 60)

    komga_a = KomgaServer("KomgaA", KOMGA_A_URL, KOMGA_A_API_KEY, KOMGA_A_MEDIA_ROOT)

    if SYNC_KOMGA_KAVITA:
        kavita = _kavita_session()
        sync_komga_kavita(komga_a, kavita)
    else:
        log.info("Komga <-> Kavita sync disabled (SYNC_KOMGA_KAVITA = False)")

    if SYNC_KOMGA_KOMGA:
        komga_b = KomgaServer("KomgaB", KOMGA_B_URL, KOMGA_B_API_KEY, KOMGA_B_MEDIA_ROOT)
        sync_komga_komga(komga_a, komga_b)
    else:
        log.info("Komga <-> Komga sync disabled (SYNC_KOMGA_KOMGA = False)")

    log.info("=" * 60)
    log.info("All sync runs complete")
    log.info("=" * 60)


if __name__ == "__main__":
    sync()