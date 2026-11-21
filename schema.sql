-- Supabase Dashboard > SQL Editor에서 실행. 사용자별 데이터는 RLS로 보호됩니다.
create table if not exists public.user_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  profile jsonb not null default '{}'::jsonb,
  settings jsonb not null default '{}'::jsonb,
  saved jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now(),
  constraint preference_profile_object check (jsonb_typeof(profile)='object'),
  constraint preference_settings_object check (jsonb_typeof(settings)='object'),
  constraint preference_saved_array check (jsonb_typeof(saved)='array')
);
create table if not exists public.push_subscriptions (
  user_id uuid not null references auth.users(id) on delete cascade,
  endpoint text not null,
  p256dh text not null,
  auth text not null,
  updated_at timestamptz not null default now(),
  primary key(user_id,endpoint)
);
create table if not exists public.push_deliveries (
  user_id uuid not null references auth.users(id) on delete cascade,
  project_id text not null,
  kind text not null check (kind in ('new','deadline')),
  event_date date not null,
  delivered_at timestamptz not null default now(),
  primary key(user_id,project_id,kind,event_date)
);
alter table public.user_preferences enable row level security;
alter table public.push_subscriptions enable row level security;
alter table public.push_deliveries enable row level security;
-- Policies may be recreated when setup is re-run.
drop policy if exists "Own preferences select" on public.user_preferences;
drop policy if exists "Own preferences insert" on public.user_preferences;
drop policy if exists "Own preferences update" on public.user_preferences;
create policy "Own preferences select" on public.user_preferences for select to authenticated using ((select auth.uid())=user_id);
create policy "Own preferences insert" on public.user_preferences for insert to authenticated with check ((select auth.uid())=user_id);
create policy "Own preferences update" on public.user_preferences for update to authenticated using ((select auth.uid())=user_id) with check ((select auth.uid())=user_id);
drop policy if exists "Own subscriptions select" on public.push_subscriptions;
drop policy if exists "Own subscriptions insert" on public.push_subscriptions;
drop policy if exists "Own subscriptions update" on public.push_subscriptions;
drop policy if exists "Own subscriptions delete" on public.push_subscriptions;
create policy "Own subscriptions select" on public.push_subscriptions for select to authenticated using ((select auth.uid())=user_id);
create policy "Own subscriptions insert" on public.push_subscriptions for insert to authenticated with check ((select auth.uid())=user_id);
create policy "Own subscriptions update" on public.push_subscriptions for update to authenticated using ((select auth.uid())=user_id) with check ((select auth.uid())=user_id);
create policy "Own subscriptions delete" on public.push_subscriptions for delete to authenticated using ((select auth.uid())=user_id);
-- Delivery ledger: backend service_role only; no client policies.
revoke all on table public.push_deliveries from anon, authenticated;
