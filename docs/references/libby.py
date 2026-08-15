#!/usr/bin/env python3
"""
libby.py — live LCPL OverDrive finder. Builds a Thunder API URL, fetches it
directly (needs network), and triages with the shipped libby_triage logic.

Run on any machine/sandbox with network egress to thunder.api.overdrive.com
(Claude Code web works). No saved JSON, no manual URLs.

Examples
  python3 libby.py --subject 24 80 --format audio --available --under 12
  python3 libby.py --creator "Will Wight" --format ebook --available
  python3 libby.py --bisac FIC129000 --format audio --available
  python3 libby.py --subject 80 --format audio --holds        # hold advisor
"""
import argparse, sys, urllib.parse, urllib.request, json
import libby_triage as T   # reuse the validated parsing/ranking/links

BASE = "https://thunder.api.overdrive.com/v2/libraries/{slug}/media"
FORMATS = {"audio": "audiobook-overdrive", "ebook": "ebook-overdrive"}


def build_url(a):
    p = []
    for s in a.subject or []:
        p.append(("subject", s))
    if a.creator:
        p.append(("creator", a.creator))
    if a.bisac:
        p.append(("bisacCode", a.bisac))
    if a.series_id:
        p.append(("seriesId", a.series_id))
    p.append(("format", FORMATS.get(a.format, a.format)))
    if a.available:
        p.append(("showOnlyAvailable", "true"))
    else:
        p.append(("availableFirst", "true"))
    p.append(("maturityLevel", "generalcontent"))
    p.append(("perPage", str(min(a.per_page, 100))))   # 100 is the hard cap
    if a.page > 1:
        p.append(("page", str(a.page)))
    return BASE.format(slug=a.slug) + "?" + urllib.parse.urlencode(p)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "libby-finder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", nargs="*", help="subject IDs (e.g. 24 80)")
    ap.add_argument("--creator")
    ap.add_argument("--bisac")
    ap.add_argument("--series-id", dest="series_id")
    ap.add_argument("--format", default="audio", choices=["audio", "ebook"])
    ap.add_argument("--available", action="store_true")
    ap.add_argument("--holds", action="store_true", help="hold advisor on waitlisted")
    ap.add_argument("--under", type=float, help="max hours (audio only)")
    ap.add_argument("--exclude-bisac", dest="exclude")
    ap.add_argument("--slug", default="lcpl")
    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--md")
    a = ap.parse_args()

    url = build_url(a)
    print(f"GET {url}\n")
    try:
        data = fetch(url)
    except Exception as e:
        sys.exit(f"Fetch failed: {e}\n(If blocked, allow thunder.api.overdrive.com "
                 f"in the environment's network settings.)")

    last = (data.get("links", {}).get("last") or {}).get("page", 1) or 1
    if last > 1:
        print(f'NOTE: {data.get("totalItemsText","?")} titles / {last} pages; '
              f'this is page {a.page}. Re-run with --page 2…\n')

    keep = {a.bisac.upper()} if a.bisac else set()
    drop = set(c.strip().upper() for c in a.exclude.split(",")) if a.exclude else set()
    rows = [T.normalize(it, a.slug) for it in data.get("items", [])]
    rows = [r for r in rows if T.passes_bisac(r, keep, drop)]
    if a.under:
        rows = [r for r in rows if r["hours"] is None or r["hours"] <= a.under]

    if a.holds:
        wait = [r for r in rows if not r["available_now"] and r["holds"] > 0]
        wait.sort(key=lambda r: (r["holds_ratio"], -r["owned"]))
        print(f"{len(wait)} waitlisted · ranked by shortest realistic wait\n")
        for r in wait:
            per = r["holds_ratio"]
            v = "good hold" if per <= 3 else "okay" if per <= 6 else "long wait"
            print(f'⏳ {r["title"]} — {r["author"]} | {r["holds"]}/{r["owned"]} '
                  f'(~{per}/copy → {v})\n   {r["link"]}')
        return

    if a.available:
        rows = [r for r in rows if r["available_now"]]
    rows.sort(key=T.rank_key)
    print(f"{len(rows)} results · {sum(1 for r in rows if r['available_now'])} now\n")
    for r in rows:
        print(T.fmt_line(r))
    if a.md:
        open(a.md, "w").write(T.to_markdown(rows))
        print(f"\nWrote -> {a.md}")


if __name__ == "__main__":
    main()
