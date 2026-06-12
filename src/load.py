"""Clean and load the three HOA CSV exports into Supabase (Task 2)."""

from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

DATA_DIR = Path(__file__).resolve().parent.parent
MISSING_TOKENS = {"", "n/a", "na", "tbd", "none", "null"}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass
class Flag:
    entity_type: str
    entity_key: str | None
    field: str | None
    raw_value: str | None
    issue: str
    action: str
    severity: str = "info"


@dataclass
class Loader:
    flags: list[Flag] = field(default_factory=list)

    def flag(self, **kw) -> None:
        self.flags.append(Flag(**kw))

    def is_missing(self, raw: str | None) -> bool:
        return raw is None or raw.strip().lower() in MISSING_TOKENS

    def money(self, raw, *, key, fld) -> Decimal | None:
        if self.is_missing(raw):
            self.flag(entity_type="association", entity_key=key, field=fld,
                      raw_value=raw, issue="missing/placeholder reserve or dues",
                      action="nulled", severity="warning")
            return None
        try:
            value = Decimal(re.sub(r"[$,\s]", "", raw))
        except InvalidOperation:
            self.flag(entity_type="association", entity_key=key, field=fld,
                      raw_value=raw, issue="unparseable money value", action="nulled",
                      severity="error")
            return None
        if value == 0 and fld == "monthly_dues":
            self.flag(entity_type="association", entity_key=key, field=fld,
                      raw_value=raw, issue="$0 monthly dues — verify (new/prospect HOA?)",
                      action="flagged", severity="warning")
        return value

    def state(self, raw, *, key) -> str | None:
        if self.is_missing(raw):
            return None
        norm = raw.strip().lower().rstrip(".")
        code = "CA" if norm in {"ca", "calif", "california"} else raw.strip().upper()[:2]
        if code != raw.strip():
            self.flag(entity_type="association", entity_key=key, field="state",
                      raw_value=raw, issue="non-standard state spelling",
                      action="normalized")
        return code

    def integer(self, raw, *, key, fld) -> int | None:
        if self.is_missing(raw):
            self.flag(entity_type="association", entity_key=key, field=fld,
                      raw_value=raw, issue="missing value", action="nulled")
            return None
        return int(re.sub(r"[,\s]", "", raw))

    def fiscal_month(self, raw, *, key) -> int | None:
        if self.is_missing(raw):
            self.flag(entity_type="association", entity_key=key, field="fiscal_year_end",
                      raw_value=raw, issue="missing fiscal year end", action="nulled")
            return None
        token = raw.strip().lower()
        month = int(token) if token.isdigit() else MONTHS.get(token)
        if month and not token.isdigit():
            self.flag(entity_type="association", entity_key=key, field="fiscal_year_end",
                      raw_value=raw, issue="month name", action="normalized")
        return month

    def yes_no(self, raw) -> bool | None:
        if self.is_missing(raw):
            return None
        return raw.strip().lower() in {"yes", "y", "true"}

    def reserve_study_date(self, raw, *, key) -> tuple[object, str | None]:
        if self.is_missing(raw):
            self.flag(entity_type="association", entity_key=key, field="last_reserve_study",
                      raw_value=raw, issue="no reserve study date on file", action="nulled")
            return None, None
        s = raw.strip()
        if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
            return f"{m[1]}-{m[2]}-{m[3]}", "day"
        if m := re.fullmatch(r"(\d{1,2})/(\d{4})", s):
            self.flag(entity_type="association", entity_key=key, field="last_reserve_study",
                      raw_value=raw, issue="month-only date — stored as 1st of month",
                      action="normalized")
            return f"{m[2]}-{int(m[1]):02d}-01", "month"
        if m := re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", s):
            month = MONTHS.get(m[1].lower())
            if month:
                self.flag(entity_type="association", entity_key=key, field="last_reserve_study",
                          raw_value=raw, issue="month-only date — stored as 1st of month",
                          action="normalized")
                return f"{m[2]}-{month:02d}-01", "month"
        self.flag(entity_type="association", entity_key=key, field="last_reserve_study",
                  raw_value=raw, issue="unrecognized date format", action="nulled",
                  severity="error")
        return None, None

    def term_date(self, raw, *, key) -> str | None:
        if self.is_missing(raw):
            return None
        s = raw.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s):
            return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
        self.flag(entity_type="board_member", entity_key=key, field="term",
                  raw_value=raw, issue="unrecognized date format", action="nulled",
                  severity="error")
        return None

    def phone(self, raw, *, key) -> str | None:
        if self.is_missing(raw):
            return None
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]
        if len(digits) != 10:
            self.flag(entity_type="vendor", entity_key=key, field="phone",
                      raw_value=raw, issue="unexpected phone length", action="flagged",
                      severity="warning")
            return raw.strip()
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    def email(self, raw) -> str | None:
        return None if self.is_missing(raw) else raw.strip().lower()

    def load_associations(self, cur) -> dict[str, int]:
        ids: dict[str, int] = {}
        with open(DATA_DIR / "hoas_export.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                code = row["hoa_code"].strip()
                study_date, precision = self.reserve_study_date(row["last_reserve_study"], key=code)
                board_email = self.email(row["board_email"])
                if board_email is None:
                    self.flag(entity_type="association", entity_key=code, field="board_email",
                              raw_value=row["board_email"], issue="missing board contact email",
                              action="flagged")
                cur.execute(
                    """insert into associations
                       (hoa_code, association_name, city, state, unit_count, monthly_dues,
                        fiscal_year_end_month, reserve_balance, last_reserve_study_date,
                        last_reserve_study_precision, has_reserve_study, board_email)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                    (code, row["association_name"].strip(),
                     row["city"].strip() or None, self.state(row["state"], key=code),
                     self.integer(row["unit_count"], key=code, fld="unit_count"),
                     self.money(row["monthly_dues"], key=code, fld="monthly_dues"),
                     self.fiscal_month(row["fiscal_year_end"], key=code),
                     self.money(row["reserve_balance"], key=code, fld="reserve_balance"),
                     study_date, precision, self.yes_no(row["has_reserve_study"]),
                     board_email))
                ids[code] = cur.fetchone()[0]
        return ids

    def load_board_members(self, cur, assoc_ids: dict[str, int]) -> None:
        seen: set[tuple] = set()
        for raw in self._read("board_members.csv"):
            code = raw["hoa_code"].strip()
            key = f"{code}/{raw['full_name'].strip()}/{raw['role'].strip()}"
            record = (assoc_ids[code], raw["full_name"].strip(), raw["role"].strip(),
                      self.email(raw["email"]),
                      self.term_date(raw["term_start"], key=key),
                      self.term_date(raw["term_end"], key=key))
            dedup_key = (record[0], record[1], record[2], record[4])
            if dedup_key in seen:
                self.flag(entity_type="board_member", entity_key=key, field=None,
                          raw_value=None, issue="exact duplicate row", action="merged")
                continue
            seen.add(dedup_key)
            if record[3] is None:
                self.flag(entity_type="board_member", entity_key=key, field="email",
                          raw_value=None, issue="missing email", action="flagged")
            cur.execute(
                """insert into board_members
                   (association_id, full_name, role, email, term_start, term_end)
                   values (%s,%s,%s,%s,%s,%s)""", record)

    def load_vendors(self, cur, assoc_ids: dict[str, int]) -> None:
        groups: dict[str, list[dict]] = defaultdict(list)
        for raw in self._read("vendors_intake.csv"):
            groups[self.email(raw["email"]) or raw["vendor_name"].strip().lower()].append(raw)

        for key, rows in groups.items():
            names = [r["vendor_name"].strip() for r in rows]
            canonical = min(sorted(set(names), key=names.count, reverse=True), key=len) \
                if len(set(names)) > 1 else names[0]
            if len(set(names)) > 1:
                self.flag(entity_type="vendor", entity_key=key, field="vendor_name",
                          raw_value=" | ".join(sorted(set(names))),
                          issue="duplicate vendor merged across name variants",
                          action="merged")

            cois = {self.yes_no(r["coi_on_file"]) for r in rows}
            coi = True if True in cois else (False if cois == {False} else None)
            if len({c for c in cois if c is not None}) > 1:
                self.flag(entity_type="vendor", entity_key=key, field="coi_on_file",
                          raw_value=str(cois), issue="conflicting COI values across rows",
                          action="flagged", severity="warning")

            areas = sorted({a.strip() for r in rows
                            for a in r["service_area"].split(",") if a.strip()})
            phone = next((self.phone(r["phone"], key=key) for r in rows
                          if not self.is_missing(r["phone"])), None)
            trade = next((r["trade"].strip() for r in rows if r["trade"].strip()), None)

            cur.execute(
                """insert into vendors (vendor_name, trade, phone, email, coi_on_file, service_areas)
                   values (%s,%s,%s,%s,%s,%s) returning id""",
                (canonical, trade, phone, self.email(rows[0]["email"]), coi, areas or None))
            vendor_id = cur.fetchone()[0]

            served = sorted({c.strip() for r in rows
                             for c in r["serves_hoa_codes"].split(";") if c.strip()})
            for code in served:
                if code not in assoc_ids:
                    self.flag(entity_type="vendor", entity_key=key, field="serves_hoa_codes",
                              raw_value=code, issue="references unknown HOA code",
                              action="flagged", severity="error")
                    continue
                cur.execute(
                    """insert into vendor_associations (vendor_id, association_id)
                       values (%s,%s) on conflict do nothing""", (vendor_id, assoc_ids[code]))
            if not served:
                self.flag(entity_type="vendor", entity_key=key, field="serves_hoa_codes",
                          raw_value=None, issue="prospect — serves no association yet",
                          action="flagged")

    def write_flags(self, cur) -> None:
        execute_values(cur,
            """insert into data_quality_flags
               (entity_type, entity_key, field, raw_value, issue, action, severity)
               values %s""",
            [(f.entity_type, f.entity_key, f.field, f.raw_value, f.issue, f.action, f.severity)
             for f in self.flags])

    def _read(self, name: str):
        with open(DATA_DIR / name, newline="") as fh:
            yield from csv.DictReader(fh)


def main() -> None:
    load_dotenv(override=True)
    loader = Loader()
    with psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("truncate associations, board_members, vendors, "
                        "vendor_associations, data_quality_flags restart identity cascade")
            assoc_ids = loader.load_associations(cur)
            loader.load_board_members(cur, assoc_ids)
            loader.load_vendors(cur, assoc_ids)
            loader.write_flags(cur)

            counts = {}
            for table in ("associations", "board_members", "vendors",
                          "vendor_associations", "data_quality_flags"):
                cur.execute(f"select count(*) from {table}")
                counts[table] = cur.fetchone()[0]
    print("Loaded:", counts)
    by_sev = defaultdict(int)
    for f in loader.flags:
        by_sev[f.severity] += 1
    print("Flags by severity:", dict(by_sev))


if __name__ == "__main__":
    main()
