"""
Check if requested movies have shows for one date or next 7 days.

Uses list-shows.py scraping logic, then filters:
- by movie title (case-insensitive substring),
- optionally by other_text substrings (case-insensitive),
- and by date validity from meta.final_url.

Output: "true" if at least one matching show is found, else "false".
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

_LIST_SHOWS_PATH = Path(__file__).with_name("list-shows.py")
_SPEC = importlib.util.spec_from_file_location("list_shows_module", _LIST_SHOWS_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load {_LIST_SHOWS_PATH}")
_LIST_SHOWS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _LIST_SHOWS
_SPEC.loader.exec_module(_LIST_SHOWS)

fetch_venue_showtimes = _LIST_SHOWS.fetch_venue_showtimes
parse_buytickets_url = _LIST_SHOWS.parse_buytickets_url
listings_to_dicts = _LIST_SHOWS.listings_to_dicts


def _normalize(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _split_csv_or_repeat(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for item in values:
        parts = [x.strip() for x in item.split(",")]
        out.extend([p for p in parts if p])
    return out


def _parse_day(raw: str) -> date:
    raw = raw.strip()
    if len(raw) == 8 and raw.isdigit():
        return datetime.strptime(raw, "%Y%m%d").date()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _dates_to_check(single_date: str | None, next_days: int) -> list[date]:
    if single_date:
        return [_parse_day(single_date)]
    start = date.today()
    # Include today + next N days, e.g. next_days=7 -> 8 dates.
    return [start + timedelta(days=i) for i in range(next_days + 1)]


def _movie_matches(title: str, wanted_movies: Iterable[str]) -> bool:
    t = _normalize(title)
    return any(_normalize(w) in t for w in wanted_movies)


def _other_text_matches(other_text: list[str], filters: Iterable[str]) -> bool:
    if not filters:
        return True
    hay = [_normalize(x) for x in other_text]
    # Every filter must be a substring of at least one other_text entry.
    for f in filters:
        needle = _normalize(f)
        if not needle:
            continue
        if not any(needle in item for item in hay):
            return False
    return True


def _final_url_has_requested_date(meta: dict, expected: date) -> bool:
    final_url = str(meta.get("final_url", "")).strip()
    if not final_url:
        return False
    try:
        parsed = parse_buytickets_url(final_url)
    except Exception:
        return False
    got = parsed.get("date_yyyymmdd", "")
    return got == expected.strftime("%Y%m%d")


def shows_opened(
    urls: list[str],
    movie_queries: list[str],
    *,
    single_date: str | None = None,
    other_text_filters: list[str] | None = None,
    next_days: int = 7,
) -> bool:
    dates = _dates_to_check(single_date, next_days=next_days)
    filters = other_text_filters or []

    for base_url in urls:
        parsed = parse_buytickets_url(base_url)
        host = parsed["host"]
        city = parsed["city_slug"]
        cinema = parsed["cinema_slug"]
        venue = parsed["venue_code"]

        for d in dates:
            day_url = (
                f"https://{host}/cinemas/{city}/{cinema}/buytickets/{venue}/{d.strftime('%Y%m%d')}"
            )
            rows, meta = fetch_venue_showtimes(day_url)
            if meta.get("status_code") != 200:
                continue

            # If final_url date differs, treat as fallback/redirect date and skip.
            if not _final_url_has_requested_date(meta, d):
                continue

            for movie in listings_to_dicts(rows):
                title = str(movie.get("title", ""))
                if not _movie_matches(title, movie_queries):
                    continue
                other = movie.get("other_text") or []
                if not isinstance(other, list):
                    other = [str(other)]
                if not _other_text_matches([str(x) for x in other], filters):
                    continue
                times = movie.get("show_times") or []
                if times:
                    return True
    return False


def main() -> str:
    p = argparse.ArgumentParser(
        description=(
            "Return true if requested movies have shows across URLs for a date "
            "or for today+next 7 days."
        )
    )
    p.add_argument(
        "--urls",
        nargs="+",
        required=True,
        help="One or more buytickets URLs (supports comma-separated values too).",
    )
    p.add_argument(
        "--movies",
        nargs="+",
        required=True,
        help="Movie title query strings (supports comma-separated values too).",
    )
    p.add_argument(
        "--date",
        default="",
        help="Optional single date: YYYYMMDD or YYYY-MM-DD. If omitted, checks today+next 7 days.",
    )
    p.add_argument(
        "--other-text",
        nargs="*",
        default=[],
        help="Optional other_text filters; each must be a substring match.",
    )
    p.add_argument(
        "--next-days",
        type=int,
        default=7,
        help="How many days after today to include when --date is omitted.",
    )
    args = p.parse_args()

    urls = _split_csv_or_repeat(args.urls)
    movies = _split_csv_or_repeat(args.movies)
    other_filters = _split_csv_or_repeat(args.other_text)

    ok = shows_opened(
        urls=urls,
        movie_queries=movies,
        single_date=args.date or None,
        other_text_filters=other_filters,
        next_days=max(0, int(args.next_days)),
    )
    return "true" if ok else "false"


if __name__ == "__main__":
    print(main())
