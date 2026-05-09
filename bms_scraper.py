"""
BookMyShow cinema showtimes (HTML) scraper.

BookMyShow serves Cloudflare; use TLS impersonation (curl_cffi) instead of
plain ``requests``. Show listings for a venue page are present in the initial
HTML in styled-component wrappers (``div.sc-1412vr2-0`` per movie format row).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cf_requests
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Install curl_cffi: pip install curl_cffi beautifulsoup4"
    ) from e

# Venue page markup: one block per movie language/format row (see <style> sc-1412vr2 ids).
_MOVIE_ROW_SEL = "div.sc-1412vr2-0"
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", re.I)
_EVENT_CODE_RE = re.compile(r"(ET\d+)(?:[/?#]|$)")
_DEFAULT_IMPERSONATE = "chrome120"


@dataclass
class Showlisting:
    title: str
    event_code: str | None
    detail_url: str | None
    show_times: list[str] = field(default_factory=list)


def region_cookie_from_city_slug(city_slug: str, region_name: str | None = None) -> str:
    """
    Build an ``Rgn`` cookie value. Most city slugs match BookMyShow region codes
    (e.g. hyderabad -> HYD). Override ``region_name`` if the display name matters
    for the API; for HTML fetch it is usually optional.
    """
    code = city_slug.strip().upper()[:3] if len(city_slug) >= 3 else city_slug.upper()
    name = region_name or city_slug.replace("-", " ").title()
    return f"Code={code}|text={name}"


def parse_buytickets_url(url: str) -> dict[str, str]:
    """
    Parse
    ``https://in.bookmyshow.com/cinemas/{city}/{cinema}/buytickets/{venue}/{yyyymmdd}``.

    The trailing date segment must be YYYYMMDD (8 digits). Paths without it will
    still load (site may normalize to "today").
    """
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 5 or parts[0] != "cinemas":
        raise ValueError(f"Not a cinema buytickets URL: {url}")
    # cinemas / {city} / {cinema...} / buytickets / {venue} / {date?}
    try:
        bi = parts.index("buytickets")
    except ValueError as e:
        raise ValueError(f"Missing buytickets segment in path: {url}") from e
    venue = parts[bi + 1]
    date_str = parts[bi + 2] if len(parts) > bi + 2 else ""
    if date_str and not re.fullmatch(r"\d{8}", date_str):
        date_str = ""
    city = parts[1]
    cinema_slug = "/".join(parts[2:bi])
    return {
        "host": u.netloc or "in.bookmyshow.com",
        "city_slug": city,
        "cinema_slug": cinema_slug,
        "venue_code": venue,
        "date_yyyymmdd": date_str,
    }


def build_buytickets_url(
    city_slug: str,
    cinema_path: str,
    venue_code: str,
    *,
    host: str = "in.bookmyshow.com",
    on_date: date | str | None = None,
) -> str:
    """
    ``cinema_path`` is the path segment between city and ``buytickets``, e.g.
    ``allu-cinemas-kokapet``.
    """
    if isinstance(on_date, date):
        ds = on_date.strftime("%Y%m%d")
    elif on_date is None or on_date == "":
        ds = datetime.now().strftime("%Y%m%d")
    else:
        ds = str(on_date)
        if len(ds) == 10 and ds[4] == "-":
            ds = ds.replace("-", "")
        if not re.fullmatch(r"\d{8}", ds):
            raise ValueError("on_date must be YYYYMMDD, YYYY-MM-DD, or a date object")
    cinema_path = cinema_path.strip("/ ")
    return f"https://{host}/cinemas/{city_slug}/{cinema_path}/buytickets/{venue_code}/{ds}"


def _extract_listings_from_soup(soup: BeautifulSoup) -> list[Showlisting]:
    rows = soup.select(_MOVIE_ROW_SEL)
    out: list[Showlisting] = []
    for row in rows:
        link = row.find(
            "a",
            href=lambda h: h and "/movies/" in h and "ET" in (h or ""),
        )
        if not link:
            continue
        href = link.get("href")
        abs_url = href if href.startswith("http") else f"https://in.bookmyshow.com{href}"
        m = _EVENT_CODE_RE.search(href)
        event_code = m.group(1) if m else None
        title = link.get_text(strip=True)
        blob = row.get_text(" ", strip=True)
        times = _TIME_RE.findall(blob)
        out.append(
            Showlisting(
                title=title,
                event_code=event_code,
                detail_url=abs_url,
                show_times=times,
            )
        )
    return out


def fetch_venue_showtimes(
    url: str,
    *,
    rgn_cookie: str | None = None,
    impersonate: str = _DEFAULT_IMPERSONATE,
    timeout: int = 45,
) -> tuple[list[Showlisting], dict[str, Any]]:
    """
    GET the cinema buytickets page and parse movie rows.

    Returns (listings, meta) where meta includes HTTP status and resolved URL info.
    """
    parsed = parse_buytickets_url(url)
    city_slug = parsed["city_slug"]
    if rgn_cookie is None:
        rgn_cookie = region_cookie_from_city_slug(city_slug)

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Cookie": f"Rgn={rgn_cookie}",
    }
    r = cf_requests.get(
        url,
        headers=headers,
        impersonate=impersonate,
        timeout=timeout,
    )
    meta: dict[str, Any] = {
        "status_code": r.status_code,
        "final_url": str(r.url),
        "parsed_url": parsed,
    }
    if r.status_code != 200:
        meta["body_preview"] = r.text[:500]
        return [], meta

    soup = BeautifulSoup(r.text, "html.parser")
    listings = _extract_listings_from_soup(soup)
    if not listings and "Just a moment" in r.text:
        meta["error"] = "Cloudflare challenge page; try updating curl_cffi / impersonate string"
    elif not listings:
        meta["warning"] = (
            f"No rows matched {_MOVIE_ROW_SEL}; site layout may have changed."
        )
    return listings, meta


def listings_to_dicts(rows: list[Showlisting]) -> list[dict[str, Any]]:
    return [
        {
            "title": x.title,
            "event_code": x.event_code,
            "detail_url": x.detail_url,
            "show_times": x.show_times,
        }
        for x in rows
    ]


def main() -> None:
    p = argparse.ArgumentParser(description="List movies and showtimes for a BookMyShow venue day.")
    p.add_argument(
        "url",
        nargs="?",
        default="https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC/20260509",
        help="Full buytickets URL (…/buytickets/{VENUE}/{YYYYMMDD})",
    )
    p.add_argument("--json", action="store_true", help="Print JSON only")
    args = p.parse_args()

    rows, meta = fetch_venue_showtimes(args.url)
    payload = {"meta": meta, "movies": listings_to_dicts(rows)}
    text = json.dumps(payload, indent=2)
    return text


if __name__ == "__main__":
    print(main())
