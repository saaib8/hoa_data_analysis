-- HOA Simplified DataHub schema (Task 1).
-- The full database is reproducible from source CSVs: this script rebuilds the
-- schema, then the loader repopulates it. Safe to re-run.

begin;

drop table if exists data_quality_flags  cascade;
drop table if exists vendor_associations cascade;
drop table if exists board_members       cascade;
drop table if exists vendors             cascade;
drop table if exists associations        cascade;

create table associations (
    id                            bigint generated always as identity primary key,
    hoa_code                      text          not null unique,
    association_name              text          not null,
    city                          text,
    state                         char(2)       check (state ~ '^[A-Z]{2}$'),
    unit_count                    integer       check (unit_count >= 0),
    monthly_dues                  numeric(10,2) check (monthly_dues >= 0),
    fiscal_year_end_month         smallint      check (fiscal_year_end_month between 1 and 12),
    reserve_balance               numeric(14,2) check (reserve_balance >= 0),
    last_reserve_study_date       date,
    -- 'day' = full source date; 'month' = month-only source (e.g. "04/2022")
    -- normalized to the 1st, so the stored date never implies false precision.
    last_reserve_study_precision  text          check (last_reserve_study_precision in ('day','month')),
    has_reserve_study             boolean,
    board_email                   text,
    created_at                    timestamptz   not null default now()
);

create table board_members (
    id             bigint generated always as identity primary key,
    association_id bigint not null references associations(id) on delete cascade,
    full_name      text   not null,
    role           text   not null check (role in
                       ('President','Vice President','Treasurer','Secretary','Member at Large')),
    email          text,
    term_start     date,
    term_end       date,                                  -- null = currently serving
    created_at     timestamptz not null default now(),

    -- Collapses the exact duplicate intake row; still allows re-election to the
    -- same role with a later term_start.
    constraint uq_board_member_term unique (association_id, full_name, role, term_start),
    constraint ck_term_order check (term_end is null or term_start is null or term_end >= term_start)
);

create index idx_board_members_assoc on board_members(association_id);

create table vendors (
    id            bigint generated always as identity primary key,
    vendor_name   text not null,
    trade         text,
    phone         text,
    email         text unique,                             -- dedup key: only consistently clean field
    coi_on_file   boolean,
    service_areas text[],
    created_at    timestamptz not null default now()
);

-- Many-to-many: a vendor serves many associations and vice versa.
-- A vendor with no rows here is a prospect (serves none yet).
create table vendor_associations (
    vendor_id      bigint not null references vendors(id)      on delete cascade,
    association_id bigint not null references associations(id) on delete cascade,
    primary key (vendor_id, association_id)
);

create index idx_vendor_assoc_assoc on vendor_associations(association_id);

-- Audit trail for every value we normalized, nulled, merged, or could not trust,
-- so cleaning decisions are queryable rather than hidden in the loader.
create table data_quality_flags (
    id          bigint generated always as identity primary key,
    entity_type text not null,
    entity_key  text,
    field       text,
    raw_value   text,
    issue       text not null,
    action      text not null check (action in ('normalized','nulled','merged','flagged')),
    severity    text not null default 'info' check (severity in ('info','warning','error')),
    created_at  timestamptz not null default now()
);

create index idx_dqf_entity on data_quality_flags(entity_type, entity_key);

commit;
