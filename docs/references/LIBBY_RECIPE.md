# Libby / LCPL availability recipe

A repeatable way to find **what's actually borrowable right now** at Loudoun
County Public Library (or any OverDrive library), without playing whack-a-mole
in the app. Pull JSON from the public Thunder API → run `libby_triage.py` →
get a ranked, deep-linked, collision-proof list.

---

## TL;DR workflow

1. Build a URL (template below), open it in any browser.
2. Select-all the JSON, save it as `media.json`.
3. `python3 libby_triage.py media.json --available --md picks.md`
4. Open the deep links in `picks.md` — each goes straight to the *exact*
   edition, so no same-title mix-ups.

---

## The endpoint

```
https://thunder.api.overdrive.com/v2/libraries/{slug}/media?{params}
```

`{slug}` for Loudoun is **`lcpl`**. No login, no token. Public.

### Libraries (two cards)

| Library | Slug | Loans | Holds | Status |
|---|---|---|---|---|
| Loudoun County (LCPL) | `lcpl` | 15 | **6** | slug CONFIRMED |
| Fairfax County (FCPL) | `fairfaxcounty`? | 10 | **15** | **slug UNVERIFIED — test before relying on it** |

Both cards are on the same Libby account. The collections, queues, and caps are
**independent** — a title waitlisted at LCPL may be available at FCPL (this
happened with *Bookshops & Bonedust*: 2 days left at LCPL, fresh 21-day loan at
FCPL). So any lookup should check **both** and report which is faster.

FCPL slug is a guess (`fairfaxcounty`, fallback `fcpl`). Verify by hitting the
endpoint — the response echoes the library name, so a wrong slug is obvious.
Confirm this first; it's the one unknown blocking multi-library queries.

### Authoritative spec
`thunder_openapi_v1.json` (in this repo) is the full OpenAPI 3.0.4 contract —
121 paths, every param. The "v1" filename documents the `/v2/` endpoints; there
is no separate v2 spec. Use it as the source of truth over this doc's summary.

### Parameters that matter

| Param      | Example                | Notes |
|------------|------------------------|-------|
| `subject`  | `subject=8`            | **Clean filter.** Use subject IDs (below). Best signal-to-noise. |
| `query`    | `query=cultivation`    | Full-text match on title+subtitle+description. **Pollutes** — "cultivation" returns fruit-farming memoirs. Use only for things subjects can't catch (e.g. LitRPG). |
| `format`   | `format=audiobook-overdrive` | Audiobooks. Use `ebook-overdrive` for ebooks. |
| `showOnlyAvailable` | `showOnlyAvailable=true` | **Server-side available-now filter. CONFIRMED.** Shrinks results before download (e.g. Business 843 → 423). Facet flips to "Available now". |
| `perPage`  | `perPage=100`          | Max ~100/page. |
| `page`     | `page=2`               | Paginate big subjects. |
| `sortBy`   | `sortBy=mostpopular-site` | Optional. |

### Subject IDs

You never have to memorize these — **every response carries the full list in
`facets.subjects`** (id + name + count). Harvest them from any pull. Confirmed
so far:

```
Fiction lane:   Fantasy 24 · Fiction 26 · Science Fiction 80 · Literature 49
                Thriller 100 · Historical Fiction 115 · Romance 77
                Mythology 58 · Young Adult Fiction 127 · Suspense 86
Nonfiction:     Nonfiction 111 · Business 8 · Self-Improvement 81
                Biography & Autobiography 7 · Economics 144 · Finance 27
                Politics 67 · Sociology 83
```

> LitRPG / progression fantasy has **no subject tag**. Use `query=` with terms
> like `litrpg`, `dungeon`, `progression fantasy` — but expect noise and rely
> on the triage step. Avoid `cultivation` (too many false hits). Better: if you
> know the author, use `creator=` (below) instead of guessing keywords.

### Power parameters (from the OpenAPI spec, `/swagger/v1/swagger.json`)

The endpoint accepts ~55 params. The ones worth knowing:

| Param | Example | What it does |
|-------|---------|--------------|
| `creator` | `creator=Will Wight` | **Author search — clean, no keyword pollution.** Best way to pull "everything available by X". |
| `series` / `seriesId` | `seriesId=307688` | Whole series, in order. Get the id from the `/series` endpoint or a record. |
| `maturityLevel` | `maturityLevel=generalcontent` | Strip kids/YA noise. Values: `generalcontent`, `juvenile`, `youngadult`, `adultonly`. |
| `availableFirst` | `availableFirst=true` | Sort available to top **without hiding** waitlisted titles (softer than `showOnlyAvailable`). |
| `showOnlyAvailable` | `showOnlyAvailable=true` | Hard filter to borrowable-now only. |
| `excludeSubject` | `excludeSubject=77` | Remove a genre (e.g. Romance=77) from results. |
| `audiobookDuration` | `audiobookDuration=...` | Filter by runtime band. |
| `language` | `language=en` | Language filter. |
| `format` | `format=audiobook-overdrive` | `audiobook-overdrive`, `ebook-overdrive`, `audiobook-mp3`. |
| `sortBy` | `sortBy=mostpopular-site` | Also `newlyadded`, etc. |
| `title` | `title=dragon rider` | Title-field search (still collides — deep-link by id). |

Useful sibling endpoints (same host):
```
# Authoritative full subject-ID list for the library:
https://thunder.api.overdrive.com/v2/libraries/lcpl/subjects

# All books in a series:
https://thunder.api.overdrive.com/v2/libraries/lcpl/series/{seriesId}
```
Patron endpoints (holds, checkouts, renew) exist under `.../patrons/me/...`
but require authentication — out of scope for this read-only recipe.

### Ready-made URLs

```
# Available business audiobooks (server-side filtered)
https://thunder.api.overdrive.com/v2/libraries/lcpl/media?subject=8&format=audiobook-overdrive&showOnlyAvailable=true&perPage=100

# Available fantasy audiobooks
https://thunder.api.overdrive.com/v2/libraries/lcpl/media?subject=24&format=audiobook-overdrive&showOnlyAvailable=true&perPage=100

# Available self-improvement audiobooks
https://thunder.api.overdrive.com/v2/libraries/lcpl/media?subject=81&format=audiobook-overdrive&showOnlyAvailable=true&perPage=100

# Everything available by a specific author (clean — use instead of keywords)
https://thunder.api.overdrive.com/v2/libraries/lcpl/media?creator=Will+Wight&format=audiobook-overdrive&availableFirst=true&perPage=100

# Adult-only fantasy, available now, kids/YA stripped out
https://thunder.api.overdrive.com/v2/libraries/lcpl/media?subject=24&maturityLevel=generalcontent&format=audiobook-overdrive&showOnlyAvailable=true&perPage=100
```

> Big subjects paginate. The response's `links.last.page` tells you how many
> pages exist (Business available-now = 22). Add `&page=2`, `&page=3`, … to
> walk them, or raise `perPage`.

---

## Reading the data — validated rules

These were confirmed against Libby ground truth, not assumed.

- **Borrowable now := `isAvailable == true` AND `availableCopies >= 1`.**
  Confirmed: the API said Matharu's *Dragon Rider* was 2/2 available; Libby
  showed "2 of 2 copies available." Copy counts are trustworthy.

- **Copy counts are reliable even for waitlisted titles.** Babel showed 7
  owned / 2 holds in the API and the same in Libby.

- **IGNORE `estimatedWaitDays`** — it's nonzero even when copies are free.
  Noise, not a phantom signal.

- **IGNORE `isOwned` and `isRecommendableToLibrary`** — both are `true` on
  essentially every record, including titles the library clearly owns. They do
  **not** distinguish real from unowned. (This burned two earlier hypotheses.)

- **The real gotcha is TITLE COLLISIONS.** Two different books share a title
  (Cornelia Funke vs Taran Matharu *Dragon Rider*; a generic *Runebound*
  edition). Libby's search surfaces the wrong one and the API looks like it
  lied. It didn't. **Always deep-link by the numeric `id`:**
  `https://lcpl.overdrive.com/media/{id}` — that opens the exact edition.

- **`⚠verify` flag** (single-name author or no publisher) marks niche /
  self-published titles, the only place rare edition glitches have shown up
  (e.g. "Runebound Professor" by *Actus*). Sanity-check those in-app before
  trusting. Mainstream titles need no check.

### Useful fields
`title`, `subtitle`, `firstCreatorName` (author), `id` (deep link),
`reserveId` (GUID fallback), `availableCopies`, `ownedCopies`, `holdsCount`,
`isAvailable`, `formats[].duration` (runtime, `HH:MM:SS`), `publisher.name`.

## Genre by BISAC code (the precise lever)

OverDrive `subject` tags are coarse (Fantasy=24 is a giant bucket). Every record
also carries **BISAC codes** (`bisacCodes`), the publishing industry's finer
classification — and the endpoint filters on them via `bisacCode=`.

**Confirmed: LitRPG has its own code → `FIC129000` (Fiction / LitRPG).**
This isolates LitRPG/progression fantasy that the `Fantasy` subject can't.

```
# Every available LitRPG in the catalog (incl. titles that never say "litrpg"):
https://thunder.api.overdrive.com/v2/libraries/lcpl/media?bisacCode=FIC129000&format=audiobook-overdrive&showOnlyAvailable=true&perPage=100
```

Useful fiction BISAC codes seen in the data:
```
FIC129000 LitRPG                       FIC009000 Fantasy / General
FIC009020 Fantasy / Epic               FIC009120 Fantasy / Dragons & Mythical
FIC028000 Science Fiction / General    FIC028010 Sci-Fi / Action & Adventure
FIC043000 Coming of Age                FIC027030 Romance / Fantasy
```

Reading the codes for pacing: pure `FIC129000` skews stat-heavy/fast. The
richer-world, steadier-paced books usually pair it with a *second* code like
`FIC009020` (Epic) or `FIC009000` (Fantasy/General). Rank the multi-coded ones
higher for "rich world without the snail's pace." `excludeBisacCodes=` can drop
a flavor you don't want.

### `FIC129000` is precise but LOW-RECALL — use a hybrid

The code is high-precision (everything it returns really is LitRPG) but
**low-recall**: only a handful of titles at LCPL carry it (≈3 ebooks, ≈6
audiobooks). It *does* tag the big ones — Dungeon Crawler Carl's ebook is coded
FIC129000 — but the coded shelf is just small, so most of the canon won't
surface through the code alone.

**Format matters as much as code.** DCC is ebook-only at LCPL, so it never
appears in an `audiobook-overdrive` search no matter how it's coded. Always
match the format to where the title actually lives (LitRPG skews ebook).

So treat `FIC129000` as a scalpel for the few well-tagged titles — and use
**`creator=`** for the rest of the canon. Seed authors for the
progression/LitRPG lane (pull `creator=<name>&availableFirst=true`):

```
Matt Dinniman (DCC)      Will Wight (Cradle)        Shirtaloon (He Who Fights…)
Dakota Krout             Andrew Rowe                Travis Bagwell (Awaken Online)
James Hunter             Aleron Kong                Casualfarmer (Beware of Chicken)
```

> Pace-heuristic caveat: `libby_triage.py` keys "rich-world" off *Fantasy*
> BISAC codes, so SciFi-coded progression (e.g. DCC) gets under-rated as
> "fast/gamey." Trust the genre tags shown, not just the pace label, for
> SciFi-coded titles.

> Ebooks carry **no length field** (no audio duration, no page count) — only an
> epub fileSize in bytes, a weak proxy. Length math is audio-only.

---



`showOnlyAvailable=true` on the Thunder endpoint filters before download:
a `subject=8` pull dropped from 843 titles to 423, and `facets.availability`
flipped from "All titles" to "Available now". Use it to skip pages of
waitlisted titles. `libby_triage.py --available` still works as a client-side
backstop (and for files pulled without the param).

---

## Personal context (Gerard)

- Holds cap is **6**; loans cap is **15**. Holds are the scarce resource —
  available-now titles go straight to loans and don't touch the cap, so the
  whole point of this tool is to keep loans full from the available shelf and
  spend holds only on things with a real wait.
- DCC books are read in print/ebook; the fun/business listening is audio — so
  the two run in parallel without clock conflicts.
