-- Run once in Supabase SQL Editor.
create extension if not exists pgcrypto;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  role text not null default 'reviewer' check (role in ('reviewer','admin')),
  created_at timestamptz not null default now()
);

create or replace function public.create_reviewer_profile()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles(id,email,role)
  values(new.id,coalesce(new.email,''),'reviewer')
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created_screening on auth.users;
create trigger on_auth_user_created_screening
  after insert on auth.users for each row execute procedure public.create_reviewer_profile();

create table if not exists public.papers (
  id uuid primary key default gen_random_uuid(),
  fingerprint text not null unique,
  item_type text,
  publication_year text,
  author text,
  title text not null,
  publication_title text,
  abstract text,
  doi text,
  url text,
  imported_at timestamptz not null default now()
);
create index if not exists papers_year_idx on public.papers(publication_year);

create table if not exists public.votes (
  paper_id uuid not null references public.papers(id) on delete cascade,
  reviewer_id uuid not null references public.profiles(id) on delete cascade,
  decision text not null check(decision in ('yes','no','uncertain')),
  note text not null default '',
  updated_at timestamptz not null default now(),
  primary key (paper_id, reviewer_id)
);
create index if not exists votes_reviewer_idx on public.votes(reviewer_id);

create table if not exists public.resolutions (
  paper_id uuid primary key references public.papers(id) on delete cascade,
  decision text not null check(decision in ('yes','no','pending')),
  note text not null default '',
  resolved_by uuid not null references public.profiles(id),
  updated_at timestamptz not null default now()
);

-- Defense in depth: normal client tokens can only access allowed data.
create or replace function public.is_screening_admin(uid uuid)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.profiles where id = uid and role = 'admin')
$$;

alter table public.profiles enable row level security;
alter table public.papers enable row level security;
alter table public.votes enable row level security;
alter table public.resolutions enable row level security;

drop policy if exists profile_read on public.profiles;
create policy profile_read on public.profiles for select to authenticated
  using (id = auth.uid() or public.is_screening_admin(auth.uid()));

drop policy if exists paper_read on public.papers;
create policy paper_read on public.papers for select to authenticated using (true);
-- Paper modifications are only performed by trusted server-side service role.

drop policy if exists vote_read on public.votes;
create policy vote_read on public.votes for select to authenticated
  using (reviewer_id = auth.uid() or public.is_screening_admin(auth.uid()));
drop policy if exists vote_insert on public.votes;
create policy vote_insert on public.votes for insert to authenticated
  with check(reviewer_id = auth.uid() and not public.is_screening_admin(auth.uid()));
drop policy if exists vote_update on public.votes;
create policy vote_update on public.votes for update to authenticated
  using (reviewer_id = auth.uid() and not public.is_screening_admin(auth.uid()))
  with check(reviewer_id = auth.uid() and not public.is_screening_admin(auth.uid()));

drop policy if exists resolution_read on public.resolutions;
create policy resolution_read on public.resolutions for select to authenticated
  using (public.is_screening_admin(auth.uid()));
-- Resolutions are written through trusted server-side service role.

-- After creating users in Supabase Authentication > Users, give YOUR account admin access:
-- update public.profiles set role='admin' where email='YOUR_ADMIN_EMAIL';
