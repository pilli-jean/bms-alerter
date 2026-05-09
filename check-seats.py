"""
BookMyShow seat availability checker (dynamic seat layout page).

Given a seat layout URL, this script:
- selects seat count = 1 (to unlock the layout)
- scans the rendered seat map for available seats in the requested rows/numbers

Output is the string "true" or "false" (lowercase), suitable for scripting.
"""

from __future__ import annotations

import argparse
import json
import re
import string
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


@dataclass(frozen=True)
class SeatQuery:
    row_start: str = "B"
    row_end: str = "K"
    seat_min: int = 14
    seat_max: int = 23

    def rows(self) -> set[str]:
        a = self.row_start.upper()
        b = self.row_end.upper()
        if a not in string.ascii_uppercase or b not in string.ascii_uppercase:
            raise ValueError("row_start/row_end must be letters A-Z")
        if a > b:
            a, b = b, a
        return set(string.ascii_uppercase[string.ascii_uppercase.index(a) : string.ascii_uppercase.index(b) + 1])

    def seat_numbers(self) -> set[int]:
        lo, hi = sorted((int(self.seat_min), int(self.seat_max)))
        return set(range(lo, hi + 1))


def _normalize_space(s: str) -> str:
    return " ".join((s or "").split())


def _click_seat_count_one(page, timeout_ms: int) -> None:
    """
    The seat-layout page blocks until a seat count is chosen.
    We try a few common patterns to select "1".
    """
    # If the modal isn't present (cached state), this will time out and we proceed.
    btn = page.get_by_role("button", name=re.compile(r"^Select Seats$", re.I))
    btn.wait_for(state="visible", timeout=timeout_ms)

    # Click the seat count "1" that's closest to the Select Seats button.
    # (BMS sometimes renders the number chips with generic elements.)
    clicked = page.evaluate(
        """
        () => {
          const button = Array.from(document.querySelectorAll('button'))
            .find(b => (b.innerText || '').trim().toLowerCase() === 'select seats');
          if (!button) return false;
          const br = button.getBoundingClientRect();
          const bcX = br.left + br.width / 2;
          const bcY = br.top + br.height / 2;

          const candidates = Array.from(document.querySelectorAll('button,div,span,li,a'))
            .filter(el => (el.innerText || '').trim() === '1')
            .map(el => ({ el, r: el.getBoundingClientRect() }))
            .filter(x => x.r.width > 0 && x.r.height > 0)
            .filter(x => x.r.top < br.top); // should be above the button within the modal

          if (!candidates.length) return false;

          candidates.sort((a, b) => {
            const da = Math.hypot((a.r.left + a.r.width/2) - bcX, (a.r.top + a.r.height/2) - bcY);
            const db = Math.hypot((b.r.left + b.r.width/2) - bcX, (b.r.top + b.r.height/2) - bcY);
            return da - db;
          });

          candidates[0].el.click();
          return true;
        }
        """,
    )

    if not clicked:
        # If we couldn't find a "1" chip, proceed anyway; some sessions default to 1/2.
        pass

    btn.click(timeout=timeout_ms)


def seats_available(
    url: str,
    *,
    query: SeatQuery = SeatQuery(),
    headless: bool = True,
    timeout_ms: int = 45_000,
    debug_dir: str | None = None,
) -> bool:
    rows = query.rows()
    nums = query.seat_numbers()
    debug_path = Path(debug_dir) if debug_dir else None
    if debug_path:
        debug_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        captured_json: list[dict[str, object]] = []
        captured_seatish: list[dict[str, object]] = []

        def _maybe_capture_json(resp) -> None:  # noqa: ANN001 - Playwright callback
            try:
                if resp.request.resource_type not in {"xhr", "fetch"}:
                    return
                ct = (resp.headers or {}).get("content-type", "")
                if "application/json" not in ct and "text/json" not in ct:
                    # Still capture potential seat/layout payloads even if not JSON.
                    u = resp.url.lower()
                    if ("seat" in u or "layout" in u) and len(captured_seatish) < 10:
                        try:
                            body = resp.text()
                        except Exception:
                            body = ""
                        captured_seatish.append(
                            {
                                "url": resp.url,
                                "status": resp.status,
                                "content_type": ct,
                                "body_head": (body or "")[:5000],
                            }
                        )
                    return
                if len(captured_json) >= 30:
                    return
                # Parsing might fail if it's not JSON; ignore.
                data = resp.json()
                captured_json.append({"url": resp.url, "json": data})
            except Exception:
                return

        page.on("response", _maybe_capture_json)

        # Load and wait for network to settle a bit; the seat layout continues to fetch.
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Try to unlock the seat layout by selecting seat count=1.
        try:
            _click_seat_count_one(page, timeout_ms=10_000)
        except Exception:
            # If modal isn't found, continue; sometimes the layout is already unlocked.
            pass

        # Allow time for the seat-layout API calls to fire.
        page.wait_for_timeout(8000)

        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            if debug_path:
                page.screenshot(path=str(debug_path / "timeout.png"), full_page=True)
                (debug_path / "timeout.html").write_text(page.content(), encoding="utf-8")
            context.close()
            browser.close()
            return False

        # Prefer parsing the underlying seat-layout JSON, if present.
        def _search_obj(obj) -> bool:  # noqa: ANN001
            if isinstance(obj, dict):
                # Common-ish keys (BMS may change; keep loose).
                row = obj.get("Row") or obj.get("row") or obj.get("RowName") or obj.get("rowName")
                num = obj.get("SeatNo") or obj.get("seatNo") or obj.get("Seat") or obj.get("seat") or obj.get("Number") or obj.get("number")
                status = obj.get("Status") or obj.get("status") or obj.get("AvailStatus") or obj.get("availStatus")
                if isinstance(row, str) and row.strip().upper() in rows:
                    try:
                        n = int(str(num).strip())
                    except Exception:
                        n = None
                    if n is not None and n in nums:
                        # Treat a bunch of common "available" representations as available.
                        s = str(status).strip().lower()
                        if status is True or s in {"a", "available", "avail", "1", "true", "yes", "y"}:
                            return True
                        # Some APIs use 0/2 for available; accept anything that isn't explicitly sold/blocked.
                        if s and s not in {"sold", "s", "blocked", "b", "unavailable", "na", "disabled"} and s != "0":
                            return True
                for v in obj.values():
                    if _search_obj(v):
                        return True
            elif isinstance(obj, list):
                for it in obj:
                    if _search_obj(it):
                        return True
            return False

        json_hit = False
        for entry in captured_json:
            payload = entry.get("json")
            if _search_obj(payload):
                json_hit = True
                break

        if debug_path:
            page.screenshot(path=str(debug_path / "final.png"), full_page=True)
            (debug_path / "final.html").write_text(page.content(), encoding="utf-8")
            (debug_path / "responses.json").write_text(
                json.dumps({"json": captured_json, "seatish": captured_seatish}, ensure_ascii=False)[:2_000_000],
                encoding="utf-8",
            )

        if json_hit:
            context.close()
            browser.close()
            return True

        # Fallback: DOM-based heuristic across all frames (works if seats exist as DOM/SVG).
        def _scan_frame(frame) -> dict[str, object]:  # noqa: ANN001
            try:
                return frame.evaluate(
                    """
                    ({rows, nums}) => {
              const rowsSet = new Set(rows);
              const numsSet = new Set(nums.map(n => String(n)));
              const all = Array.from(document.querySelectorAll('*'));

              const rowLabelEls = all
                .filter(el => {
                  const t = (el.textContent || '').trim();
                  if (!/^[A-Z]$/.test(t)) return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 && r.left < 200; // row labels are on the left
                })
                .map(el => ({ el, t: (el.textContent || '').trim(), r: el.getBoundingClientRect() }));

              const isGreenish = (cssColor) => {
                const m = String(cssColor || '').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                if (!m) return false;
                const r = Number(m[1]), g = Number(m[2]), b = Number(m[3]);
                return g >= 110 && g > r + 25 && g > b + 25;
              };

              const seatCandidates = all
                .filter(el => /^\\d{1,2}$/.test((el.textContent || '').trim()))
                .map(el => {
                  const r = el.getBoundingClientRect();
                  if (!(r.width > 10 && r.width < 45 && r.height > 10 && r.height < 45)) return null;
                  const st = getComputedStyle(el);
                  return {
                    el,
                    r,
                    num: (el.textContent || '').trim(),
                    border: st.borderColor,
                    bg: st.backgroundColor,
                  };
                })
                .filter(Boolean);

              for (const seat of seatCandidates) {
                if (!numsSet.has(seat.num)) continue;

                // Find nearest row label by vertical alignment.
                const cy = seat.r.top + seat.r.height / 2;
                let best = null;
                for (const rl of rowLabelEls) {
                  const rcy = rl.r.top + rl.r.height / 2;
                  const dy = Math.abs(rcy - cy);
                  if (dy > 14) continue;
                  const dx = seat.r.left - rl.r.right;
                  if (dx < 0) continue; // label must be left of the seat
                  const score = dy * 5 + dx; // prioritize vertical alignment
                  if (!best || score < best.score) best = { row: rl.t, score };
                }
                if (!best) continue;
                if (!rowsSet.has(best.row)) continue;

                // Availability heuristic:
                // available seats tend to have a green border; sold are greyed out.
                const available = isGreenish(seat.border);
                if (!available) continue;

                return { ok: true, row: best.row, num: seat.num, border: seat.border, bg: seat.bg };
              }

                      return { ok: false, seenSeats: seatCandidates.length, seenRows: rowLabelEls.length };
                    }
                    """,
                    {"rows": sorted(rows), "nums": sorted(nums)},
                )
            except Exception:
                return {"ok": False, "error": "frame-eval-failed"}

        result: dict[str, object] = {"ok": False}
        for fr in page.frames:
            r = _scan_frame(fr)
            if isinstance(r, dict) and r.get("ok"):
                result = r
                break
            # keep best diagnostics
            if not result.get("seenSeats") and r.get("seenSeats"):
                result = r

        if debug_path:
            (debug_path / "result.json").write_text(str(result), encoding="utf-8")

        context.close()
        browser.close()
        return bool(result.get("ok"))


def main() -> str:
    p = argparse.ArgumentParser(description="Return true if seats are available in a specified range.")
    p.add_argument(
        "url",
        nargs="?",
        default="https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00497135/ALUC/1172/20260509",
        help="Seat layout URL",
    )
    p.add_argument("--row-start", default="B")
    p.add_argument("--row-end", default="K")
    p.add_argument("--seat-min", type=int, default=14)
    p.add_argument("--seat-max", type=int, default=23)
    p.add_argument("--headed", action="store_true", help="Run with a visible browser window")
    p.add_argument("--debug-dir", default="", help="Write screenshots/HTML into this directory")
    args = p.parse_args()

    q = SeatQuery(
        row_start=_normalize_space(args.row_start) or "B",
        row_end=_normalize_space(args.row_end) or "K",
        seat_min=args.seat_min,
        seat_max=args.seat_max,
    )
    ok = seats_available(
        args.url,
        query=q,
        headless=not args.headed,
        debug_dir=_normalize_space(args.debug_dir) or None,
    )
    return "true" if ok else "false"


if __name__ == "__main__":
    print(main())

