-- FridgeIntel: profiles, household ownership, categories and the item catalog.
-- Run this in the Supabase SQL Editor after schema.sql. Safe to re-run.

-- ---------------------------------------------------------------------------
-- Profiles: account-level identity, separate from household membership
-- ---------------------------------------------------------------------------
-- Kept apart from `members` so a username and avatar survive leaving one
-- household and joining another.

create table if not exists profiles (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  username   text not null check (length(trim(username)) between 1 and 32),
  avatar     text not null default 'apple',
  created_at timestamptz not null default now()
);

alter table profiles enable row level security;

-- Everyone in your household can see each other's profile; nobody else can.
drop policy if exists profiles_read on profiles;
create policy profiles_read on profiles
  for select using (
    user_id = auth.uid()
    or user_id in (select user_id from members
                    where household_id = current_household())
  );

drop policy if exists profiles_write on profiles;
create policy profiles_write on profiles
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Household ownership and personalisation
-- ---------------------------------------------------------------------------

alter table households add column if not exists icon text not null default 'kitchen';
alter table households add column if not exists owner_id uuid references auth.users (id);

-- Existing households predate ownership: hand each to its earliest member.
update households h
   set owner_id = (select m.user_id from members m
                    where m.household_id = h.id
                    order by m.joined_at limit 1)
 where h.owner_id is null;

-- ---------------------------------------------------------------------------
-- Categories on items
-- ---------------------------------------------------------------------------

alter table items add column if not exists category text;
alter table item_history add column if not exists category text;

-- ---------------------------------------------------------------------------
-- Catalog of common fridge items, shared by every household
-- ---------------------------------------------------------------------------

create table if not exists catalog (
  name     text primary key,
  category text not null
);

alter table catalog enable row level security;

drop policy if exists catalog_read on catalog;
create policy catalog_read on catalog
  for select using (auth.uid() is not null);

-- Seeded catalog. ON CONFLICT keeps re-runs idempotent and lets the list be
-- extended later without wiping anything.
insert into catalog (name, category) values
  ('Milk', 'Dairy'),
  ('Butter', 'Dairy'),
  ('Cheddar cheese', 'Dairy'),
  ('Greek yogurt', 'Dairy'),
  ('Cream cheese', 'Dairy'),
  ('Eggs', 'Dairy'),
  ('Sour cream', 'Dairy'),
  ('Heavy cream', 'Dairy'),
  ('Mozzarella', 'Dairy'),
  ('Parmesan', 'Dairy'),
  ('Lettuce', 'Produce'),
  ('Spinach', 'Produce'),
  ('Tomatoes', 'Produce'),
  ('Carrots', 'Produce'),
  ('Bell peppers', 'Produce'),
  ('Broccoli', 'Produce'),
  ('Cucumber', 'Produce'),
  ('Onions', 'Produce'),
  ('Apples', 'Produce'),
  ('Grapes', 'Produce'),
  ('Strawberries', 'Produce'),
  ('Blueberries', 'Produce'),
  ('Lemons', 'Produce'),
  ('Avocado', 'Produce'),
  ('Mushrooms', 'Produce'),
  ('Celery', 'Produce'),
  ('Chicken breast', 'Meat & fish'),
  ('Ground beef', 'Meat & fish'),
  ('Bacon', 'Meat & fish'),
  ('Deli turkey', 'Meat & fish'),
  ('Ham', 'Meat & fish'),
  ('Salmon', 'Meat & fish'),
  ('Shrimp', 'Meat & fish'),
  ('Sausage', 'Meat & fish'),
  ('Orange juice', 'Drinks'),
  ('Apple juice', 'Drinks'),
  ('Soda', 'Drinks'),
  ('Beer', 'Drinks'),
  ('White wine', 'Drinks'),
  ('Iced tea', 'Drinks'),
  ('Sparkling water', 'Drinks'),
  ('Cold brew', 'Drinks'),
  ('Ketchup', 'Condiments'),
  ('Mustard', 'Condiments'),
  ('Mayonnaise', 'Condiments'),
  ('Ranch dressing', 'Condiments'),
  ('Soy sauce', 'Condiments'),
  ('Hot sauce', 'Condiments'),
  ('BBQ sauce', 'Condiments'),
  ('Salsa', 'Condiments'),
  ('Pickles', 'Condiments'),
  ('Jam', 'Condiments'),
  ('Peanut butter', 'Condiments'),
  ('Maple syrup', 'Condiments'),
  ('Leftover pizza', 'Leftovers'),
  ('Leftover pasta', 'Leftovers'),
  ('Leftover rice', 'Leftovers'),
  ('Leftover curry', 'Leftovers'),
  ('Meal prep', 'Leftovers'),
  ('Bread', 'Bakery'),
  ('Tortillas', 'Bakery'),
  ('Bagels', 'Bakery'),
  ('Croissants', 'Bakery'),
  ('Tofu', 'Other'),
  ('Hummus', 'Other'),
  ('Guacamole', 'Other'),
  ('Cooked beans', 'Other'),
  ('Stock', 'Other')
on conflict (name) do update set category = excluded.category;

-- ---------------------------------------------------------------------------
-- Who owns the household?
-- ---------------------------------------------------------------------------

create or replace function is_owner()
returns boolean
language sql stable security definer set search_path = public
as $$
  select exists (
    select 1 from households
     where id = current_household() and owner_id = auth.uid()
  )
$$;

-- ---------------------------------------------------------------------------
-- Profile
-- ---------------------------------------------------------------------------

create or replace function set_profile(new_username text, new_avatar text)
returns void
language plpgsql security definer set search_path = public
as $$
declare
  clean text := trim(coalesce(new_username, ''));
begin
  if auth.uid() is null then
    raise exception 'You must be signed in';
  end if;
  if length(clean) < 1 or length(clean) > 32 then
    raise exception 'Username must be 1 to 32 characters';
  end if;

  insert into profiles (user_id, username, avatar)
       values (auth.uid(), clean, coalesce(nullif(trim(new_avatar), ''), 'apple'))
  on conflict (user_id) do update
       set username = excluded.username, avatar = excluded.avatar;

  -- Keep the household-facing name in step, so "added by" stays right.
  update members set display_name = clean where user_id = auth.uid();
end;
$$;

-- ---------------------------------------------------------------------------
-- Household management
-- ---------------------------------------------------------------------------

create or replace function update_household(new_name text, new_icon text)
returns void
language plpgsql security definer set search_path = public
as $$
declare
  clean text := trim(coalesce(new_name, ''));
begin
  if not is_owner() then
    raise exception 'Only the person who created the household can change it';
  end if;
  if length(clean) < 1 or length(clean) > 40 then
    raise exception 'Household name must be 1 to 40 characters';
  end if;
  update households
     set name = clean,
         icon = coalesce(nullif(trim(new_icon), ''), icon)
   where id = current_household();
end;
$$;

create or replace function delete_household()
returns void
language plpgsql security definer set search_path = public
as $$
begin
  if not is_owner() then
    raise exception 'Only the person who created the household can delete it';
  end if;
  -- members, items and history all cascade from this row.
  delete from households where id = current_household();
end;
$$;

create or replace function remove_member(target uuid)
returns void
language plpgsql security definer set search_path = public
as $$
begin
  if not is_owner() then
    raise exception 'Only the person who created the household can remove members';
  end if;
  if target = auth.uid() then
    raise exception 'You cannot remove yourself. Transfer or delete the household instead';
  end if;
  if not exists (select 1 from members
                  where user_id = target and household_id = current_household()) then
    raise exception 'That person is not in this household';
  end if;
  delete from members where user_id = target;
end;
$$;

create or replace function leave_household()
returns void
language plpgsql security definer set search_path = public
as $$
begin
  if current_household() is null then
    raise exception 'You are not in a household';
  end if;
  -- Without this the household would be left with no one able to manage it.
  if is_owner() then
    raise exception 'You own this household. Hand it to someone else, or delete it';
  end if;
  delete from members where user_id = auth.uid();
end;
$$;

create or replace function transfer_ownership(target uuid)
returns void
language plpgsql security definer set search_path = public
as $$
begin
  if not is_owner() then
    raise exception 'Only the current owner can hand the household over';
  end if;
  if not exists (select 1 from members
                  where user_id = target and household_id = current_household()) then
    raise exception 'That person is not in this household';
  end if;
  update households set owner_id = target where id = current_household();
end;
$$;

-- ---------------------------------------------------------------------------
-- Account deletion
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER so it can reach auth.users, which a client key cannot.
-- Everything else cascades from that row.

create or replace function delete_account()
returns void
language plpgsql security definer set search_path = public
as $$
begin
  if auth.uid() is null then
    raise exception 'You must be signed in';
  end if;
  if is_owner() then
    raise exception 'You own a household. Hand it over or delete it first';
  end if;
  delete from auth.users where id = auth.uid();
end;
$$;

-- ---------------------------------------------------------------------------
-- Reads, extended for the new fields
-- ---------------------------------------------------------------------------

create or replace function whoami()
returns jsonb
language sql stable security definer set search_path = public
as $$
  select jsonb_build_object(
    'user_id', auth.uid(),
    'email', (select email from auth.users where id = auth.uid()),
    'username', (select p.username from profiles p where p.user_id = auth.uid()),
    'avatar', coalesce((select p.avatar from profiles p
                         where p.user_id = auth.uid()), 'apple'),
    'household_id', (select household_id from members where user_id = auth.uid()),
    'display_name', (select display_name from members where user_id = auth.uid()),
    'household_name', (select h.name from households h
                        join members m on m.household_id = h.id
                       where m.user_id = auth.uid()),
    'household_icon', coalesce((select h.icon from households h
                        join members m on m.household_id = h.id
                       where m.user_id = auth.uid()), 'kitchen'),
    'is_owner', is_owner(),
    'roommates', coalesce((
      select jsonb_agg(coalesce(p.username, m2.display_name) order by m2.joined_at)
        from members m2
        left join profiles p on p.user_id = m2.user_id
       where m2.household_id = current_household()), '[]'::jsonb)
  );
$$;

-- Everyone in the household, for the members screen.
create or replace function household_members()
returns jsonb
language sql stable security definer set search_path = public
as $$
  select coalesce((
    select jsonb_agg(jsonb_build_object(
             'user_id', m.user_id,
             'name', coalesce(p.username, m.display_name),
             'avatar', coalesce(p.avatar, 'apple'),
             'is_owner', (m.user_id = h.owner_id),
             'is_you', (m.user_id = auth.uid()),
             'joined_at', m.joined_at)
           order by m.joined_at)
      from members m
      join households h on h.id = m.household_id
      left join profiles p on p.user_id = m.user_id
     where m.household_id = current_household()), '[]'::jsonb);
$$;

create or replace function fridge_state()
returns jsonb
language sql stable security invoker set search_path = public
as $$
  select jsonb_build_object(
    'capacity', (select capacity from households where id = current_household()),
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', i.id, 'name', i.name, 'qty', i.qty,
               'category', i.category,
               'added_by', coalesce(p.username, m.display_name, 'someone'),
               'avatar', coalesce(p.avatar, 'apple'),
               'updated_at', i.updated_at)
             order by i.added_at)
        from items i
        left join members m on m.user_id = i.added_by
        left join profiles p on p.user_id = i.added_by
       where i.household_id = current_household()), '[]'::jsonb),
    'history', coalesce((
      select jsonb_agg(jsonb_build_object('name', h.name, 'category', h.category)
             order by h.last_used desc)
        from item_history h
       where h.household_id = current_household()), '[]'::jsonb),
    'catalog', coalesce((
      select jsonb_agg(jsonb_build_object('name', c.name, 'category', c.category)
             order by c.category, c.name)
        from catalog c), '[]'::jsonb)
  );
$$;

-- save_items now carries a category per entry.
create or replace function save_items(entries jsonb)
returns void
language plpgsql security invoker set search_path = public
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
  if used + incoming > cap then
    raise exception 'Not enough room: only % slot(s) left', cap - used;
  end if;

  insert into items (household_id, name, qty, category, added_by)
  select hh, trim(e ->> 'name'), (e ->> 'qty')::int,
         nullif(trim(coalesce(e ->> 'category', '')), ''), auth.uid()
    from jsonb_array_elements(entries) e
  on conflict (household_id, lower(name)) do update
    set qty        = items.qty + excluded.qty,
        category   = coalesce(excluded.category, items.category),
        updated_at = now();

  insert into item_history (household_id, name, last_used, category)
  select hh, trim(e ->> 'name'), now(),
         nullif(trim(coalesce(e ->> 'category', '')), '')
    from jsonb_array_elements(entries) e
  on conflict (household_id, lower(name)) do update
    set last_used = now(),
        category  = coalesce(excluded.category, item_history.category);
end;
$$;

-- create_household now records the owner and the chosen icon.
create or replace function create_household(name text, display_name text,
                                            icon text default 'kitchen')
returns uuid
language plpgsql security definer set search_path = public
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

  insert into households (name, icon, owner_id)
       values (create_household.name,
               coalesce(nullif(trim(create_household.icon), ''), 'kitchen'),
               auth.uid())
    returning id into new_id;
  insert into members (user_id, household_id, display_name)
       values (auth.uid(), new_id, create_household.display_name);
  return new_id;
end;
$$;
