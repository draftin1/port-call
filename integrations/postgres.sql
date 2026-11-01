-- Apply explicitly to a dedicated database (also works in Supabase SQL editor).
CREATE TABLE IF NOT EXISTS public.port_call_transcripts (
  conversation_id text PRIMARY KEY,
  segments jsonb NOT NULL,
  ended boolean NOT NULL DEFAULT false,
  revision text NOT NULL
);
-- Supabase: deny browser/anon/authenticated access. Only the local backend's
-- dedicated service credential may write. Never put that credential in HTML.
ALTER TABLE public.port_call_transcripts ENABLE ROW LEVEL SECURITY;
