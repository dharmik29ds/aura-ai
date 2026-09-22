-- Aura database schema (Postgres + Supabase-style auth)
-- Every user-owned table has user_id and Row Level Security so one user
-- can never read or write another user's data.

create extension if not exists vector;
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------
-- Profiles & preferences (1 row per user)
-- ---------------------------------------------------------------
create table profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  display_name  text,
  language_pref text not null default 'auto'
                check (language_pref in ('auto','en','hi','hinglish')),
  tone_pref     text not null default 'friendly'
                check (tone_pref in ('friendly','formal','concise','detailed')),
  timezone      text not null default 'Asia/Kolkata',
  memory_enabled boolean not null default true,   -- user consent switch
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------
-- Long-term memory (facts, preferences, goals, standing instructions)
-- ---------------------------------------------------------------
create table memories (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  kind       text not null check (kind in ('preference','fact','goal','instruction')),
  content    text not null,
  embedding  vector(1536),          -- match this to your embedding model's dimension
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz            -- soft delete
);
create index on memories (user_id) where deleted_at is null;
create index on memories using hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------
-- Notes
-- ---------------------------------------------------------------
create table notes (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  title      text,
  body       text not null,
  tags       text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);
create index on notes (user_id, created_at desc) where deleted_at is null;

-- ---------------------------------------------------------------
-- Reminders (stored in UTC; scheduler polls remind_at)
-- ---------------------------------------------------------------
create table reminders (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  title      text not null,
  remind_at  timestamptz not null,
  recurrence text,                   -- iCal RRULE, e.g. 'FREQ=DAILY'; null = one-time
  status     text not null default 'pending'
             check (status in ('pending','sent','done','cancelled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);
create index on reminders (status, remind_at) where deleted_at is null;
create index on reminders (user_id, remind_at) where deleted_at is null;

-- ---------------------------------------------------------------
-- Conversations & messages
-- ---------------------------------------------------------------
create table conversations (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  title      text,
  created_at timestamptz not null default now()
);

create table messages (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references profiles(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  role            text not null check (role in ('user','assistant','tool')),
  content         jsonb not null,
  created_at      timestamptz not null default now()
);
create index on messages (conversation_id, created_at);

-- ---------------------------------------------------------------
-- Pending actions: the confirmation gate for sensitive operations.
-- The model can only CREATE a pending action. Only the user (via a
-- button or explicit "yes" handled by your backend) can confirm it.
-- ---------------------------------------------------------------
create table pending_actions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  tool_name  text not null,
  tool_input jsonb not null,
  summary    text not null,          -- human-readable, shown to the user
  status     text not null default 'pending'
             check (status in ('pending','confirmed','rejected','expired')),
  expires_at timestamptz not null default now() + interval '10 minutes',
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------
-- Audit log of every tool call
-- ---------------------------------------------------------------
create table action_logs (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references profiles(id) on delete cascade,
  tool_name  text not null,
  tool_input jsonb,
  result     jsonb,
  ok         boolean not null,
  created_at timestamptz not null default now()
);
create index on action_logs (user_id, created_at desc);

-- ---------------------------------------------------------------
-- Row Level Security: users only ever see their own rows
-- ---------------------------------------------------------------
alter table profiles enable row level security;
create policy profiles_owner on profiles
  for all using (id = auth.uid()) with check (id = auth.uid());

do $$
declare t text;
begin
  foreach t in array array[
    'memories','notes','reminders','conversations',
    'messages','pending_actions','action_logs'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy %I on %I for all using (user_id = auth.uid()) with check (user_id = auth.uid())',
      t || '_owner', t);
  end loop;
end $$;

-- ---------------------------------------------------------------
-- Semantic memory search (SECURITY INVOKER so RLS still applies)
-- ---------------------------------------------------------------
create or replace function match_memories(
  query_embedding vector(1536),
  match_count int default 5
) returns table (id uuid, kind text, content text, similarity float)
language sql stable security invoker as $$
  select m.id, m.kind, m.content,
         1 - (m.embedding <=> query_embedding) as similarity
  from memories m
  where m.deleted_at is null
  order by m.embedding <=> query_embedding
  limit match_count;
$$;

-- IMPORTANT: if your backend connects with a service-role key (which
-- bypasses RLS), you MUST add "where user_id = $1" to every query.
-- Safer: run queries with the user's own JWT so RLS is enforced.
