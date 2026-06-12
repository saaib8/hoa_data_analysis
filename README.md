# HOA Simplified — DataHub

Consolidates three messy CSV exports (associations, board members, vendors) into a
normalized Supabase (Postgres) database, and generates a clean, AI-assisted one-page
profile for any association straight from that live data.

Built for the HOA Simplified case study. Everything is reproducible from the raw CSVs
and the scripts in this repo.

---

## Deliverables map

| Task | Deliverable | Where |
|------|-------------|-------|
| **1 — Schema design** | Normalized Postgres schema | [`migrations/001_schema.sql`](migrations/001_schema.sql) |
| **2 — Ingestion & cleaning** | Cleaning + load pipeline; queryable audit trail | [`src/load.py`](src/load.py), `data_quality_flags` table |
| **3 — One-pager generator** | Live query → template → LLM summary | [`src/generate.py`](src/generate.py), [`templates/onepager.html.j2`](templates/onepager.html.j2), [`output/`](output/) |
| Rationale (Tasks 1 & 2) | Schema + cleaning write-up | `RATIONALE.docx` |

---

## Architecture

```
hoas_export.csv  ┐
board_members.csv ├─►  load.py  ──►  Supabase (Postgres)  ──►  generate.py  ──►  output/SR-XX.html
vendors_intake.csv┘   (clean +        5 tables +              (live query →        (one-page
                       dedup +        data_quality_flags       signals in code →    profile)
                       flag)                                   LLM narrative)
```

Design principle for the AI step: **facts in code, language in the LLM.** Every number
and risk flag on the one-pager is computed deterministically from the database; the LLM
only writes prose grounded in those computed facts. This prevents hallucinated figures.

---

## Project structure

```
.
├── migrations/001_schema.sql     # Task 1 — schema (idempotent)
├── src/
│   ├── load.py                   # Task 2 — clean, de-duplicate, load, flag
│   └── generate.py               # Task 3 — one-pager generator
├── templates/onepager.html.j2    # report template
├── output/SR-01..SR-12.html      # generated examples
├── hoas_export.csv               # raw inputs
├── board_members.csv
├── vendors_intake.csv
├── requirements.txt
└── .env                          # DATABASE_URL + OPENAI_API_KEY (gitignored)
```

---

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` in the project root:

```ini
# Supabase → Connect → Session pooler URI (password URL-encoded)
DATABASE_URL=postgresql://postgres.<ref>:<password>@<host>.pooler.supabase.com:5432/postgres

# Required for the LLM "areas to watch" section (Task 3).
OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o-mini   # optional override
```

---

## Running

```bash
source .venv/bin/activate

# 1. Create the schema (idempotent — drops & recreates)
psql "$DATABASE_URL" -f migrations/001_schema.sql      # or apply via the Supabase SQL editor

# 2. Clean + load all three CSVs into Supabase
python src/load.py
#   → Loaded: {associations: 12, board_members: 52, vendors: 10,
#              vendor_associations: 13, data_quality_flags: 42}

# 3. Generate one-pagers (live from the database)
python src/generate.py SR-04      # by hoa_code
python src/generate.py 4          # by numeric id
python src/generate.py --all      # all 12
open output/SR-04.html
```

`load.py` truncates and reloads on every run, so the full DataHub is reproducible from
the CSVs at any time.

---

## Schema (Task 1)

```
associations ──< board_members
     │
     └──< vendor_associations >── vendors        (many-to-many)

data_quality_flags                                (cleaning audit trail)
```

- **associations** — surrogate `id` + unique `hoa_code`; money as `numeric`, dates as
  `date`, state as `char(2)`, fiscal year as `smallint (1–12)`. `last_reserve_study_precision`
  records whether a source date was full or month-only, so a normalized date never implies
  false precision.
- **board_members** — FK to `associations`; `role` constrained to the five real roles; a
  unique constraint `(association_id, full_name, role, term_start)` removes the duplicate
  intake row while still allowing re-elections.
- **vendors** — `email` is the unique de-duplication key (the one consistently clean field).
- **vendor_associations** — the many-to-many bridge; a **prospect** is simply a vendor with
  zero rows here, so it needs no special-casing.
- **data_quality_flags** — one row per value normalized, nulled, merged, or flagged, making
  the cleaning fully auditable from inside the database.

See `RATIONALE.docx` for the full reasoning.

---

## Data cleaning (Task 2)

`load.py` normalizes every messy field and records each decision:

| Field | Source mess | Handling |
|-------|-------------|----------|
| money (dues, reserve) | `$285.00`, `61,200`, `N/A`, `TBD` | → `numeric`; placeholders → NULL (flagged) |
| state | `CA` / `Calif.` / `California` / `ca` | → `CA` |
| fiscal year end | `December` / `12` / blank | → month number 1–12 |
| reserve study date | 4 formats incl. `04/2022`, `March 2024` | → `date`; month-only → 1st + precision flag |
| phone | 5 formats | → `(NNN) NNN-NNNN` |
| vendors | 3× Bright Path, Reliable variants, etc. | de-duplicated by email; served-HOA sets merged |
| prospects | vendors serving none | zero junction rows (flagged) |

**Result:** 14 vendor rows → 10 real companies (served-HOA sets preserved), the duplicate
board row removed (53 → 52), and **42 data-quality flags** recorded. Every NULL in the
database traces to a flag — nothing was changed silently.

---

## One-pager generator (Task 3)

`generate.py` takes one association (by `hoa_code` or numeric `id`), queries Supabase live,
and renders a self-contained HTML profile covering identity, financial snapshot, board
roster (with active/expired term tags), vendors by trade, and an **LLM-generated
"areas to watch"** section.

- The LLM (OpenAI, default `gpt-4o-mini`) receives only the **computed structured signals**
  and is instructed to ground its prose strictly in them.
- Missing data degrades gracefully (e.g. *"Reserve study: not on file"* rather than a blank).

> **Re-running the LLM section** requires `OPENAI_API_KEY` in `.env` with API credits.
> Cost is negligible (~a fraction of a cent for all 12).

---

## Verification

- **Schema constraints** (PK / FK / CHECK / UNIQUE) reject bad data at load time.
- **`data_quality_flags`** records every transformation and nulled value — every NULL in the
  database traces to a flag, so the cleaning is fully auditable from inside Supabase.
- Generated reports were spot-checked against the database.


