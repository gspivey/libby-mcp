#!/usr/bin/env python3
"""
libby_triage.py — turn a raw OverDrive "Thunder" media.json into a clean,
de-collisioned, available-now reading list with direct deep links.

DESIGN PRINCIPLE (learned the hard way)
  Server-side FILTER params are unreliable: showOnlyAvailable works, but
  bisacCode silently returns 0. So: pull BROAD from the server (one subject,
  available-now), and do all genre/pace/dedup filtering HERE, client-side,
  where it's deterministic and debuggable. Record data (bisacCodes, copies,
  duration) is reliable; only the server filters are flaky.

USAGE
  python3 libby_triage.py media.json                      # ranked, all
  python3 libby_triage.py media.json --available          # borrowable now
  python3 libby_triage.py media.json --bisac FIC129000    # only LitRPG
  python3 libby_triage.py media.json --exclude-bisac FIC009020   # no Epic
  python3 libby_triage.py media.json --bisac FIC129000 --md picks.md
  (--bisac / --exclude-bisac take comma-separated lists; match = ANY)

VALIDATED RULES
  * BORROWABLE NOW := isAvailable AND availableCopies >= 1   (ground-truth ok)
  * Deep-link by numeric id to dodge same-title collisions.
  * IGNORE estimatedWaitDays / isRecommendableToLibrary / isOwned (noise).
"""
import argparse, json, sys

# Readable labels for the fiction BISAC codes we've actually seen.
BISAC_LABELS = {
    "FIC129000": "LitRPG",
    "FIC009000": "Fantasy/General",
    "FIC009020": "Fantasy/Epic",
    "FIC009030": "Fantasy/Historical",
    "FIC009120": "Fantasy/Dragons",
    "FIC010000": "Myth/Legend",
    "FIC028000": "SciFi/General",
    "FIC028010": "SciFi/Action",
    "FIC031000": "Thriller",
    "FIC055000": "Dystopian",
    "FIC061000": "Magical Realism",
    "FIC019000": "Literary",
    "FIC043000": "Coming of Age",
    "FIC027030": "Romance/Fantasy",
}
# Codes that signal rich worldbuilding (good when paired with momentum)
WORLDBUILDING = {"FIC009000", "FIC009020", "FIC009030", "FIC009120", "FIC010000"}
# Codes that, alone, tend to mean fast/gamey or doorstopper-epic
FAST_GAMEY = {"FIC129000"}
DOORSTOPPER = {"FIC009020"}  # Epic — often the snail-pace slog (verify per-author)


def hms_to_hours(s):
    if not s or not isinstance(s, str):
        return None
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return round(h + m / 60 + sec / 3600, 1)


def first_duration(item):
    for f in item.get("formats", []) or []:
        if f.get("duration"):
            return f["duration"]
    return None


def normalize(item, slug):
    ac = item.get("availableCopies", 0) or 0
    oc = item.get("ownedCopies", 0) or 0
    hc = item.get("holdsCount", 0) or 0
    codes = item.get("bisacCodes", []) or []
    tid = item.get("id")
    return {
        "title": item.get("title", "?"),
        "author": item.get("firstCreatorName", "?"),
        "available_now": bool(item.get("isAvailable")) and ac >= 1,
        "avail": ac, "owned": oc, "holds": hc,
        "holds_ratio": round(hc / oc, 1) if oc else (hc or 0),
        "hours": hms_to_hours(first_duration(item)),
        "id": tid,
        "publisher": (item.get("publisher") or {}).get("name", ""),
        "codes": codes,
        "genres": [BISAC_LABELS.get(c, c) for c in codes],
        "link": f"https://{slug}.overdrive.com/media/{tid}" if tid else "",
    }


def pace_hint(r):
    s = set(r["codes"])
    has_litrpg = bool(s & FAST_GAMEY)
    has_world = bool(s & WORLDBUILDING)
    if has_litrpg and has_world:
        return "rich-world progression"      # the sweet spot for this reader
    if has_litrpg:
        return "fast/gamey litrpg"
    if s & DOORSTOPPER and not has_litrpg:
        return "epic (check pace)"
    if has_world:
        return "worldbuilt fantasy"
    return ""


def looks_niche_selfpub(r):
    author = (r["author"] or "").strip()
    return bool((author and " " not in author) or not r["publisher"])


def passes_bisac(r, keep, drop):
    s = set(r["codes"])
    if keep and not (s & keep):
        return False
    if drop and (s & drop):
        return False
    return True


def rank_key(r):
    # sweet-spot first, then available, then short line, then more copies
    pace_rank = {"rich-world progression": 0, "worldbuilt fantasy": 1,
                 "fast/gamey litrpg": 2}.get(pace_hint(r), 3)
    return (0 if r["available_now"] else 1, pace_rank, r["holds_ratio"], -r["avail"])


def fmt_line(r):
    flag = "✅" if r["available_now"] else "⏳"
    hrs = f'{r["hours"]}h' if r["hours"] is not None else "?h"
    status = (f'{r["avail"]}/{r["owned"]} free' if r["available_now"]
              else f'{r["holds"]} waiting/{r["owned"]}')
    ph = pace_hint(r)
    tag = f' [{ph}]' if ph else ''
    genres = "·".join(g for g in r["genres"] if g)
    niche = "  ⚠verify" if looks_niche_selfpub(r) else ""
    return (f'{flag} {r["title"]} — {r["author"]} | {hrs} | {status}{tag}{niche}\n'
            f'   {genres}\n   {r["link"]}')


def to_markdown(rows):
    out = ["# Libby triage\n"]
    for bucket, label in [(True, "Available now"), (False, "Waitlisted")]:
        grp = [r for r in rows if r["available_now"] == bucket]
        if not grp:
            continue
        out.append(f"## {label}\n")
        for r in grp:
            hrs = f'{r["hours"]}h' if r["hours"] is not None else "?h"
            ph = pace_hint(r); tag = f' _{ph}_' if ph else ''
            cnt = (f'{r["avail"]}/{r["owned"]} free' if r["available_now"]
                   else f'{r["holds"]} waiting')
            out.append(f'- **[{r["title"]}]({r["link"]})** — {r["author"]} '
                       f'· {hrs} · {cnt}{tag}'
                       + ("  ⚠verify" if looks_niche_selfpub(r) else ""))
        out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--available", action="store_true")
    ap.add_argument("--holds", action="store_true",
                    help="hold-advisor: rank WAITLISTED titles by how worth-the-wait")
    ap.add_argument("--bisac", help="comma-sep codes to KEEP (match any)")
    ap.add_argument("--exclude-bisac", dest="exclude", help="comma-sep codes to DROP")
    ap.add_argument("--slug", default="lcpl")
    ap.add_argument("--md", metavar="FILE")
    a = ap.parse_args()

    try:
        data = json.load(open(a.path))
    except Exception as e:
        sys.exit(f"Could not read {a.path}: {e}")

    links = data.get("links", {})
    last_page = (links.get("last") or {}).get("page", 1) or 1
    if last_page > 1:
        print(f'NOTE: server reports {data.get("totalItemsText","?")} titles across '
              f'{last_page} pages; this file is one page. Add &page=2… for the rest.\n')

    keep = set(c.strip().upper() for c in a.bisac.split(",")) if a.bisac else set()
    drop = set(c.strip().upper() for c in a.exclude.split(",")) if a.exclude else set()

    rows = [normalize(it, a.slug) for it in data.get("items", [])]
    rows = [r for r in rows if passes_bisac(r, keep, drop)]

    if a.holds:
        # Hold-advisor: only waitlisted titles, ranked by shortest realistic wait.
        # Best hold = fewest people per copy (line moves fast) + decent copy count.
        wait = [r for r in rows if not r["available_now"] and r["holds"] > 0]
        wait.sort(key=lambda r: (r["holds_ratio"], -r["owned"]))
        print(f'{len(wait)} waitlisted titles · ranked by shortest realistic wait\n')
        print("Spend hold slots top-down; skip anything with a brutal ratio.\n")
        for r in wait:
            per = r["holds_ratio"]
            verdict = ("good hold" if per <= 3 else
                       "okay" if per <= 6 else "long wait")
            ph = pace_hint(r); tag = f' [{ph}]' if ph else ''
            genres = "·".join(g for g in r["genres"] if g)
            print(f'⏳ {r["title"]} — {r["author"]} | {r["holds"]} waiting / '
                  f'{r["owned"]} copies (~{per}/copy → {verdict}){tag}'
                  + ("  ⚠verify" if looks_niche_selfpub(r) else "")
                  + f'\n   {genres}\n   {r["link"]}')
        return

    if a.available:
        rows = [r for r in rows if r["available_now"]]
    rows.sort(key=rank_key)

    n_av = sum(1 for r in rows if r["available_now"])
    filt = []
    if keep: filt.append("keep " + ",".join(keep))
    if drop: filt.append("drop " + ",".join(drop))
    print(f'{len(data.get("items",[]))} in file · {len(rows)} after filter'
          f'{" ("+"; ".join(filt)+")" if filt else ""} · {n_av} borrowable now\n')
    for r in rows:
        print(fmt_line(r))
    if a.md:
        open(a.md, "w").write(to_markdown(rows))
        print(f"\nWrote -> {a.md}")


if __name__ == "__main__":
    main()
