-- FridgeIntel schema
-- Run this once in the Supabase dashboard: SQL Editor -> New query -> Run.
--
-- Design notes:
--   * Everything is scoped to a household, so one deployment can serve more
--     than a single fridge. Four roommates share one household row.
--   * Row Level Security is on for every table. A logged-in roommate can only
--     ever read or write rows belonging to their own household.
--   * Quantity changes go through the RPC functions at the bottom rather than
--     through plain UPDATEs. They do the arithmetic inside Postgres, so two
--     roommates editing at the same time cannot clobber each other.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table if not exists households (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  capacity   int  not null default 24 check (capacity > 0),
  created_at timestamptz not null default now()
);

-- Links a Supabase auth user to their household.
create table if not exists members (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  household_id uuid not null references households (id) on delete cascade,
  display_name text not null,
  joined_at    timestamptz not null default now()
);

create index if not exists members_household_idx on members (household_id);

create table if not exists items (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households (id) on delete cascade,
  name         text not null check (length(trim(name)) > 0),
  qty          int  not null check (qty > 0),
  added_by     uuid references auth.users (id) on delete set null,
  added_at     timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- One row per item name per household, case-insensitively: adding "milk" when
-- "Milk" is already there bumps the existing row instead of creating a second.
create unique index if not exists items_household_name_key
  on items (household_id, lower(name));

-- Names the household has used before, for the "Used before" dropdown.
create table if not exists item_history (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households (id) on delete cascade,
  name         text not null,
  last_used    timestamptz not null default now()
);

create unique index if not exists item_history_household_name_key
  on item_history (household_id, lower(name));

-- ---------------------------------------------------------------------------
-- Helper: which household does the caller belong to?
-- ---------------------------------------------------------------------------

-- SECURITY DEFINER so the lookup itself is not filtered by the policies below,
-- which would otherwise recurse.
create or replace function current_household()
returns uuid
language sql
stable
security definer
set search_path = public
as $$
  select household_id from members where user_id = auth.uid()
$$;

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

alter table households   enable row level security;
alter table members      enable row level security;
alter table items        enable row level security;
alter table item_history enable row level security;

drop policy if exists households_read on households;
create policy households_read on households
  for select using (id = current_household());

drop policy if exists members_read on members;
create policy members_read on members
  for select using (household_id = current_household());

drop policy if exists items_read on items;
create policy items_read on items
  for select using (household_id = current_household());

drop policy if exists items_write on items;
create policy items_write on items
  for all
  using (household_id = current_household())
  with check (household_id = current_household());

drop policy if exists history_read on item_history;
create policy history_read on item_history
  for select using (household_id = current_household());

drop policy if exists history_write on item_history;
create policy history_write on item_history
  for all
  using (household_id = current_household())
  with check (household_id = current_household());

-- ---------------------------------------------------------------------------
-- RPC: the only supported way to change quantities
-- ---------------------------------------------------------------------------
--
-- The app never reads a quantity, adds to it in Python, and writes it back.
-- That read-modify-write pattern loses data when two roommates act at the same
-- time. These functions do the arithmetic inside a single statement in
-- Postgres, so concurrent calls serialise instead of overwriting each other.

-- Commit a batch of staged items. `entries` is [{"name": "Milk", "qty": 2}, ...]
create or replace function save_items(entries jsonb)
returns void
language plpgsql
security invoker
set search_path = public
as $$
declare
  hh       uuid := current_household();
  cap      int;
  used     int;
  incoming int;
begin
  if hh is null then
    raise exception 'You are not a member of a household yet';
  end if;

  select capacity into cap from households where id = hh;
  select coalesce(sum(qty), 0) into used from items where household_id = hh;
  select coalesce(sum((e ->> 'qty')::int), 0) into incoming
    from jsonb_array_elements(entries) e;

  if incoming <= 0 then
    raise exception 'Nothing to save';
  end if;

  -- Capacity is enforced here, not in the app. Two roommates saving at once
  -- cannot both pass a client-side check and overfill the fridge between them.
  if used + incoming > cap then
    raise exception 'Not enough room: only % slot(s) left', cap - used;
  end if;

  insert into items (household_id, name, qty, added_by)
  select hh, trim(e ->> 'name'), (e ->> 'qty')::int, auth.uid()
    from jsonb_array_elements(entries) e
  on conflict (household_id, lower(name)) do update
    set qty        = items.qty + excluded.qty,
        updated_at = now();

  insert into item_history (household_id, name, last_used)
  select hh, trim(e ->> 'name'), now()
    from jsonb_array_elements(entries) e
  on conflict (household_id, lower(name)) do update
    set last_used = now();
end;
$$;

-- Nudge one item's quantity. Deleting at zero and the capacity ceiling are
-- both handled here so every client behaves the same way.
create or replace function adjust_item(item_id uuid, delta int)
returns void
language plpgsql
security invoker
set search_path = public
as $$
declare
  hh   uuid := current_household();
  cap  int;
  used int;
  now_qty int;
begin
  select qty into now_qty
    from items where id = item_id and household_id = hh
    for update;

  if not found then
    raise exception 'That item is no longer in the fridge';
  end if;

  if delta > 0 then
    select capacity into cap from households where id = hh;
    select coalesce(sum(qty), 0) into used from items where household_id = hh;
    if used + delta > cap then
      raise exception 'Not enough room: only % slot(s) left', cap - used;
    end if;
  end if;

  if now_qty + delta <= 0 then
    delete from items where id = item_id;
  else
    update items
       set qty = qty + delta, updated_at = now()
     where id = item_id;
  end if;
end;
$$;

-- Everything the app needs for one screen, in a single round trip.
create or replace function fridge_state()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'capacity', (select capacity from households where id = current_household()),
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', i.id, 'name', i.name, 'qty', i.qty,
               'added_by', coalesce(m.display_name, 'someone'),
               'updated_at', i.updated_at)
             order by i.added_at)
        from items i
        left join members m on m.user_id = i.added_by
       where i.household_id = current_household()), '[]'::jsonb),
    'history', coalesce((
      select jsonb_agg(h.name order by h.last_used desc)
        from item_history h
       where h.household_id = current_household()), '[]'::jsonb)
  );
$$;

-- ---------------------------------------------------------------------------
-- RPC: getting into a household
-- ---------------------------------------------------------------------------
--
-- The policies above deliberately grant no INSERT on households or members --
-- otherwise anyone could add themselves to any household. These two functions
-- are the only way in. They are SECURITY DEFINER so they can write those
-- tables, and each one checks the caller first.

-- One roommate runs this once. The returned id is the join code for the others.
create or replace function create_household(name text, display_name text)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  new_id uuid;
begin
  if auth.uid() is null then
    raise exception 'You must be signed in';
  end if;
  if exists (select 1 from members where user_id = auth.uid()) then
    raise exception 'You already belong to a household';
  end if;

  insert into households (name) values (create_household.name) returning id into new_id;
  insert into members (user_id, household_id, display_name)
    values (auth.uid(), new_id, create_household.display_name);
  return new_id;
end;
$$;

-- The other three paste the code from above.
create or replace function join_household(code uuid, display_name text)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.uid() is null then
    raise exception 'You must be signed in';
  end if;
  if exists (select 1 from members where user_id = auth.uid()) then
    raise exception 'You already belong to a household';
  end if;
  if not exists (select 1 from households where id = code) then
    raise exception 'No household with that code';
  end if;

  insert into members (user_id, household_id, display_name)
    values (auth.uid(), code, join_household.display_name);
end;
$$;

-- Who am I, and am I in a household yet? Drives which screen opens at launch.
create or replace function whoami()
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'user_id', auth.uid(),
    'household_id', (select household_id from members where user_id = auth.uid()),
    'display_name', (select display_name from members where user_id = auth.uid()),
    'household_name', (select h.name from households h
                        join members m on m.household_id = h.id
                       where m.user_id = auth.uid()),
    'roommates', coalesce((
      select jsonb_agg(m2.display_name order by m2.joined_at)
        from members m2
       where m2.household_id = (select household_id from members
                                 where user_id = auth.uid())), '[]'::jsonb)
  );
$$;

-- ---------------------------------------------------------------------------
-- RPC: changing how much the fridge holds
-- ---------------------------------------------------------------------------
--
-- Capacity is shared, so this changes it for the whole household. SECURITY
-- DEFINER because the policies above grant no UPDATE on households; this
-- function is the only way to change it, and it validates first.

create or replace function set_capacity(new_capacity int)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  hh   uuid := current_household();
  used int;
begin
  if hh is null then
    raise exception 'You are not a member of a household yet';
  end if;
  if new_capacity is null or new_capacity < 1 or new_capacity > 999 then
    raise exception 'Capacity must be a number between 1 and 999';
  end if;

  -- Refuse to shrink below what is already in there; the alternative is an
  -- over-full fridge that every other check then has to reason about.
  select coalesce(sum(qty), 0) into used from items where household_id = hh;
  if new_capacity < used then
    raise exception 'The fridge already holds % item(s). Remove some first.', used;
  end if;

  update households set capacity = new_capacity where id = hh;
end;
$$;
