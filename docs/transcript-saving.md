# Transcript destinations

This is a local-console integration. Keep `board.py` bound to loopback. Existing Vexa Postgres
capture stays enabled independently of the optional destinations. The console now requires a
per-process CSRF header for POSTs; external scripts that directly call console POSTs must obtain
it from the local page configuration. Vexa gateway APIs are unchanged.

## Use

1. Start the normal Port Call stack and this version of `board.py`.
2. For TOPOS, use the visible **Attach Dialogues** control, sign in on Dialogues, and approve
   transcript writing to your selected TOPOS. The UI shows the attached resource identifier.
3. Expand **Transcript saving** before joining and select the destination and timing.
4. Select TOPOS as the destination, or tick **Send all new transcripts to my TOPOS** alongside
   local file, Supabase, or PostgreSQL. The same selected timing applies to both destinations.
5. Join the call. These choices are frozen for that call. They reset on page reload; no setting
   retroactively enrolls a call or exports history.
6. Manual mode requires **Save this transcript** after the conversation ends. The click first
   fetches the latest transcript, then approves exactly that snapshot. If more final segments
   arrive afterward, click again to approve those additions.

Continuous mode sends newly completed or corrected segments on the worker's roughly two-second
poll. Batch mode sends the first available batch, then batches every 30 seconds and flushes when
Vexa reports the call ended. The worker continues fetching for 120 seconds after end to catch late
segments, including one final catch-up fetch after a console restart. Browser polling is not
required. Unavailable gateway responses never imply a call ended or a transcript became empty.
Calls started elsewhere are not automatically enrolled.

TOPOS uses stable conversation/message IDs, batches up to 100 changed segments, and checks the
processed count/errors before acknowledging. A queued response remains pending until confirmed.
Each retry keeps the same payload and idempotency key. Local files atomically replace one file
per conversation; Supabase/PostgreSQL upsert one row per conversation, including its final state.

Pending payloads/checkpoints and manual approvals live in `.port-call-saves/delivery.sqlite3`,
with restrictive file permissions. This is another local copy of the transcript, not encrypted
storage. Local transcript files are in `.port-call-saves/transcripts`. Set `PORT_CALL_SAVE_DIR`
for a different private directory. The directory is git-ignored. Audit rows store only metadata,
counts, request IDs and outcomes, not transcript content or access tokens.

Tokens stay in process memory; reattach after a restart. Pending TOPOS work resumes only if the
returned resource matches the originally selected resource. A different account or TOPOS does
not receive the old transcript. Detach blocks subsequent sends and drops credentials;
detach waits for an in-flight request to finish. Previously submitted/queued server writes may still
finish. Revoke the grant in Dialogues Sharing → Connected apps to revoke server-side authorization.
A 401/403 clears the ticket and displays an expiration/revocation message with **Attach Dialogues again**.
Local retries remain paused without generating network requests or new denial audit events.
Cancellation, expired consent, and exchange failures return to the console with a retry message.
The connection uses the browser-plugin public PKCE pattern: `/connect` → `/connect/exchange`.
Only the returned `plugin_attach_token` is retained for writes. The optional read/MCP RPT is not requested
or stored; this integration needs `messages:write` only.
Transient failures retry with exponential backoff up to 256 seconds; other destinations continue.

## One-time TOPOS setup

The integration uses the supported public-PKCE `/connect` → `/connect/exchange` flow. It needs no
new Keycloak client or embedded client secret. Register the connector from
`integrations/connector.json` through the authenticated connector registry / App Sheaf. Review
existing app metadata first; do not overwrite an existing registration or broaden its scopes.
The only requested scope is `messages:write`; no read/MCP token is used or retained.

Install `integrations/source.json` on the intended owner's node through the supported
`POST /v1/source-install` owner flow, wrapping it as `source_definition_json` and naming the
intended node/dataset in the normal install scope. The source uses TOPOS's existing
`voxterm.transcript.v1` parser / `voxterm_transcript` canonical mapper, with its own source ID and
ambient posture. Speaker names are sender identifiers; no claim that the owner spoke is supplied.
Manual enrichment is requested; runtime baseline derivation policy is controlled by the node.
Do not assume this source file disables all indexing in a sandbox.

Configure `TOPOS_CONTROL_PLANE_URL`, `TOPOS_APP_ID`, `TOPOS_SOURCE_ID` as needed. The two loopback
callback URLs in the connector manifest must be allowed. The source and connector artifacts
have **not** been registered or installed by this change. Follow the PM's sandbox setup before
live testing. Never use an unrelated app's grant or source merely to make a write succeed.

## Supabase and PostgreSQL

Apply `integrations/postgres.sql` explicitly to a dedicated database. For Supabase, configure
`TRANSCRIPT_SUPABASE_URL` and a backend-only `TRANSCRIPT_SUPABASE_KEY` for that dedicated store.
RLS is enabled with no browser policies; the local backend must use an appropriately privileged
credential. Do not share the local console or put the credential in frontend code. This adapter
is an operator-configured mirror, not a multi-tenant Supabase login product.

For PostgreSQL, install `psycopg[binary]` in the console's Python environment and configure
`TRANSCRIPT_POSTGRES_DSN`. Point it at a dedicated local database. No schema is created automatically;
the adapter uses parameterized upserts and transaction/statement timeouts. The original Vexa
database is neither migrated nor repurposed.

## Verification and limits

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile board.py destinations.py
# Optional real isolated Postgres test (temporary database/socket, automatically stopped):
RUN_POSTGRES_TESTS=1 python3 -m unittest discover -s tests -p test_postgres.py -v
# Synthetic console on localhost:18090, no live services accessed:
python3 tests/preview_saving.py
```

Current live acceptance and analytics gaps are recorded in `destinations-progress.md`. In
particular, stable canonical IDs and local acknowledgments prevent ordinary duplicate records,
but synchronous TOPOS request idempotency and exactly-once analytics counts are not proven. The
CP's direct ingestion path currently creates a separate node request ID; its inbox path honors
the idempotency key. A lost response can therefore repeat processing/counting. No live successful
or denied Port Call request has yet been traced through TOPOS audit persistence and analytics UI.

The manual file-save flow was exercised in a browser using synthetic text. Supabase is contract
tested; live credentials were not supplied. The real PostgreSQL adapter test now passes using the already-installed PostgreSQL 17 runtime.
It creates and stops a private temporary server and verifies actual row readback. PostgreSQL 14
on this host remains broken; no global installation or Vexa database was changed.
Set `POSTGRES_BIN` to another compatible runtime directory if needed.

## Recovering a pending write made by the first iteration

The first iteration saved only segment data, so its original destination metadata cannot be
reconstructed safely after a call ends. Such rows now remain held with a visible recovery error.
The original pending payload and request ID are retained. Leaving them held is safe.

For explicit recovery, stop this console process first and back up its private save directory.
Inspect metadata only (this command does not print transcript text):

```sh
python3 recover_saving.py --database .port-call-saves/delivery.sqlite3
```

Use the returned session, target and exact request ID. If you choose to abandon the uncertain
attempt and allow a new current-state revision, run:

```sh
python3 recover_saving.py --database .port-call-saves/delivery.sqlite3 \
  --session SESSION --target TARGET --expected-request-id REQUEST_ID \
  --abandon-legacy --acknowledge-uncertain-delivery
```

This archives the old payload unchanged in the local `legacy_recovery` table and removes only
its active retry pointer. It never resends a changed body under the old ID. On console restart,
the existing per-call settings apply: automatic mode sends current state; manual mode sends
only the last explicitly approved snapshot. The prior remote attempt may already have succeeded,
so this choice can repeat remote processing/analytics even where stable record IDs prevent duplicate
rows. Inspect the destination first if available. The exact-ID guard prevents abandoning a different
request by accident. This command refuses new version-2 envelopes, which already retry safely.
