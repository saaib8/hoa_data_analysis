"""Generate a one-page HOA profile from the live Supabase database (Task 3).

Usage: python src/generate.py <hoa_code | id | --all>
Requires DATABASE_URL and OPENAI_API_KEY (optional OPENAI_MODEL) in .env.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
BOARD_ROLES = ["President", "Vice President", "Treasurer", "Secretary", "Member at Large"]
MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def fetch(cur, identifier: str) -> dict | None:
    column = "id" if identifier.isdigit() else "hoa_code"
    value = int(identifier) if identifier.isdigit() else identifier
    cur.execute(f"select * from associations where {column} = %s", (value,))
    association = cur.fetchone()
    if association is None:
        return None

    cur.execute(
        """select full_name, role, email, term_start, term_end
           from board_members where association_id = %s
           order by array_position(%s, role), full_name""",
        (association["id"], BOARD_ROLES))
    board = cur.fetchall()
    today = date.today()
    for m in board:
        m["expired"] = bool(m["term_end"] and m["term_end"] < today)

    cur.execute(
        """select v.vendor_name, v.trade, v.coi_on_file
           from vendors v
           join vendor_associations va on va.vendor_id = v.id
           where va.association_id = %s
           order by v.trade, v.vendor_name""",
        (association["id"],))
    vendors = cur.fetchall()
    return {"association": association, "board": board, "vendors": vendors}


def compute_signals(data: dict) -> dict:
    a, board, vendors = data["association"], data["board"], data["vendors"]

    reserve_per_unit = None
    if a["reserve_balance"] is not None and a["unit_count"]:
        reserve_per_unit = (a["reserve_balance"] / a["unit_count"]).quantize(Decimal("1"))

    present_roles = {m["role"] for m in board}
    missing_roles = [r for r in BOARD_ROLES if r not in present_roles]

    return {
        "reserve_per_unit": reserve_per_unit,
        "has_reserve_study": bool(a["has_reserve_study"]),
        "board_size": len(board),
        "expired_terms": sum(1 for m in board if m["expired"]),
        "missing_roles": missing_roles,
        "vendor_count": len(vendors),
        "trades_covered": sorted({v["trade"] for v in vendors if v["trade"]}),
        "vendors_without_coi": [v["vendor_name"] for v in vendors if v["coi_on_file"] is not True],
    }


def narrative(a: dict, s: dict) -> tuple[str, str]:
    from openai import OpenAI

    facts = {
        "association": a["association_name"],
        "units": a["unit_count"],
        "monthly_dues": _num(a["monthly_dues"]),
        "reserve_balance": _num(a["reserve_balance"]),
        "reserve_per_unit": _num(s["reserve_per_unit"]),
        "has_reserve_study": s["has_reserve_study"],
        "last_reserve_study_completed_on": a["last_reserve_study_date"].isoformat()
            if a["last_reserve_study_date"] else None,
        "board_seats_filled": s["board_size"],
        "board_terms_expired": s["expired_terms"],
        "vacant_board_roles": s["missing_roles"],
        "vendor_count": s["vendor_count"],
        "trades_covered": s["trades_covered"],
        "vendors_missing_insurance": s["vendors_without_coi"],
    }
    system = (
        "You are an HOA portfolio analyst. Write a brief 'areas to watch' summary for a "
        "community-management one-pager. Use ONLY the structured facts provided — never invent "
        "numbers or details. Dates are when an event last occurred (e.g. the reserve study was "
        "last completed), never a future schedule — do not call a past date 'scheduled' or "
        "'upcoming', and state when a study was last completed rather than judging if it is current. "
        "Output 3-5 short bullet points as an HTML <ul>. Flag concrete risks: "
        "a missing reserve study, low reserve funding per unit, an incomplete board, or vendors "
        "without insurance. If the association looks healthy, say so plainly. No preamble, "
        "no headings — return only the <ul>...</ul>."
    )
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    resp = OpenAI().chat.completions.create(
        model=model,
        max_tokens=600,
        temperature=0,
        seed=7,  # best-effort reproducibility; OpenAI output is not fully deterministic
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(facts, indent=2)},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text.startswith("<ul"):
        text = f"<ul><li>{text}</li></ul>"
    return text, f"ChatGPT ({model})"


def _num(v):
    return float(v) if isinstance(v, Decimal) else v


def _money(v) -> str:
    return "—" if v is None else f"${v:,.0f}" if v == v.to_integral_value() else f"${v:,.2f}"


def render(env: Environment, data: dict, signals: dict, text: str, source: str) -> str:
    a = data["association"]
    by_trade: dict[str, list[str]] = {}
    for v in data["vendors"]:
        by_trade.setdefault(v["trade"] or "Other", []).append(v["vendor_name"])

    if not signals["has_reserve_study"]:
        reserve_status = "Not on file"
    elif a["last_reserve_study_date"]:
        d = a["last_reserve_study_date"]
        reserve_status = (f"{MONTHS[d.month]} {d.year}" if a["last_reserve_study_precision"] == "month"
                          else d.strftime("%b %d, %Y"))
    else:
        reserve_status = "On file, date unknown"

    return env.get_template("onepager.html.j2").render(
        a=a, s=signals, board=data["board"], vendors_by_trade=by_trade,
        reserve_study_status=reserve_status, narrative_html=Markup(text), narrative_source=source,
        generated_at=datetime.now().strftime("%b %d, %Y %H:%M"), today=date.today(),
        money=_money, month_name=lambda m: MONTHS[m] if m else "—",
        missing=lambda label="—": Markup(f'<span class="missing">{label}</span>'),
        term=_term)


def _term(start, end) -> str:
    s = start.isoformat() if start else "?"
    return f"{s} → {end.isoformat()}" if end else f"{s} → present"


def generate(cur, env: Environment, identifier: str) -> Path | None:
    data = fetch(cur, identifier)
    if data is None:
        print(f"  {identifier}: not found", file=sys.stderr)
        return None
    code = data["association"]["hoa_code"]
    signals = compute_signals(data)
    text, source = narrative(data["association"], signals)
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{code}.html"
    path.write_text(render(env, data, signals, text, source))
    print(f"  {code}: {path.relative_to(ROOT)}  ({source})")
    return path


def main() -> None:
    load_dotenv(override=True)
    args = sys.argv[1:]
    if not args:
        sys.exit("usage: python src/generate.py <hoa_code> | --all")

    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html"]))
    conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)
    cur = conn.cursor()
    if args == ["--all"]:
        cur.execute("select hoa_code from associations order by hoa_code")
        codes = [r["hoa_code"] for r in cur.fetchall()]
    else:
        codes = [c.upper() for c in args]
    for code in codes:
        generate(cur, env, code)
    conn.close()


if __name__ == "__main__":
    main()
