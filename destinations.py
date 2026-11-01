"""Explicit, durable transcript delivery. No history discovery or implicit enrollment."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class DeliveryError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class DeliveryPaused(DeliveryError):
    """Local attachment is absent/mismatched; no request reached a destination."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise DeliveryError('Destination redirected; verify its configured URL', code)


def http_json(url, body=None, headers=None):
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     # The Dialogues edge rejects urllib's default Python user agent.
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'Roost/1.0', **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read()
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError('Expected object')
            return result
    except urllib.error.HTTPError as exc:
        # Response bodies can contain transcript text or credentials. Never log them.
        raise DeliveryError(f'Destination returned HTTP {exc.code}', exc.code) from None
    except (OSError, ValueError):
        raise DeliveryError('Destination unavailable or returned an invalid response') from None


class Attachment:
    """Dialogues public-PKCE attach; secrets live only in this local process."""
    def __init__(self, env, request=http_json):
        self.base = env.get('TOPOS_CONTROL_PLANE_URL', 'https://cp.logu3s.com').rstrip('/')
        if urllib.parse.urlsplit(self.base).scheme != 'https':
            raise ValueError('TOPOS_CONTROL_PLANE_URL must use HTTPS')
        self.app_id = env.get('TOPOS_APP_ID', 'roost')
        self.source = env.get('TOPOS_SOURCE_ID', 'roost_transcripts')
        self.scope = 'messages:write'
        self.request = request
        self.lock = threading.RLock()
        self.token = self.resource = ''
        self.pending = None
        self.error = ''

    def start(self, redirect):
        with self.lock:
            self.error = ''
            state, verifier, browser = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            self.pending = (state, verifier, browser, time.time())
            query = urllib.parse.urlencode(dict(app_id=self.app_id, source_id=self.source,
                scopes=self.scope, redirect_uri=redirect, state=state, code_challenge=challenge,
                code_challenge_method='S256', force_login='1'))
            return self.base + '/connect?' + query, browser

    def finish(self, code, state, browser):
        with self.lock:
            pending, self.pending = self.pending, None
            if not pending or time.time() - pending[3] > 600 or not code or not secrets.compare_digest(state.encode(), pending[0].encode()) or not secrets.compare_digest(browser.encode(), pending[2].encode()):
                self.error = 'Attachment was not completed or expired. Attach Dialogues to try again.'
                raise DeliveryError(self.error)
            try:
                data = self.request(self.base + '/connect/exchange', dict(code=code, app_id=self.app_id, code_verifier=pending[1]))
            except DeliveryError:
                self.error = 'Dialogues could not complete the attachment. Please try again.'
                raise DeliveryError(self.error) from None
            resource = data.get('resource_id', '') if isinstance(data, dict) else ''
            if not isinstance(data, dict) or data.get('state') != state or not isinstance(data.get('plugin_attach_token'), str) or not data['plugin_attach_token'] or not isinstance(resource, str) or not resource.startswith('dataset:') or len(resource.split(':')) < 4:
                self.error = 'Dialogues did not return a valid write ticket and TOPOS. Please attach again.'
                raise DeliveryError(self.error)
            self.token, self.resource = data['plugin_attach_token'], resource
            self.error = ''

    def disconnect(self, reason=''):
        with self.lock:
            self.token = self.resource = ''
            self.pending = None
            self.error = reason

    def status(self):
        with self.lock:
            return dict(connected=bool(self.token), resource_id=self.resource, app_id=self.app_id,
                        pending=bool(self.pending and time.time() - self.pending[3] <= 600),
                        scope=self.scope, error=self.error)


class Topos:
    def __init__(self, attachment):
        self.attachment = attachment

    def pause_reason(self, session):
        auth = self.attachment
        with auth.lock:
            if not auth.token:
                return 'Paused · attach Dialogues to resume TOPOS saves'
            if auth.resource != session['resource'] or session.get('source_id', auth.source) != auth.source:
                return 'Paused · attach the original Dialogues account and TOPOS to resume'
            return ''

    def save(self, session, segments, request_id):
        auth = self.attachment
        with auth.lock:  # disconnect waits for in-flight request; none can start after it returns
            if not auth.token or auth.resource != session['resource'] or session.get('source_id', auth.source) != auth.source:
                raise DeliveryPaused('Paused · attach the original Dialogues account and TOPOS to resume')
            records = [dict(message_id=s['id'], conversation_id=session['id'], content=s['text'],
                sender_id=s['speaker'], event_at=datetime.fromtimestamp(s['t'], timezone.utc).isoformat(),
                origin_device='roost') for s in segments]
            headers = {'Authorization': 'Bearer ' + auth.token, 'Idempotency-Key': request_id, 'X-Request-ID': request_id}
            try:
                data = auth.request(auth.base + '/v1/ingestion/app_ingest',
                    dict(resource_id=session['resource'], source_id=auth.source, records=records), headers)
            except DeliveryError as exc:
                if exc.status in (401, 403):
                    auth.disconnect('Dialogues access expired or was revoked. Attach again to resume TOPOS saves.')
                raise
            if data.get('status') == 'queued' and data.get('write_id'):
                # Keep the SAME payload/key until the server reports delivery on a retry.
                raise DeliveryError('TOPOS queued write ' + str(data['write_id']) + '; delivery not yet confirmed', 202)
            payload = data.get('payload') or {}
            if data.get('status') != 'ok' or payload.get('errors') or payload.get('records_processed') != len(records):
                raise DeliveryError('TOPOS did not confirm every requested record')
            return {'outcome': 'delivered', 'count': len(records), 'write_id': payload.get('write_id')}


class LocalFile:
    def __init__(self, directory):
        self.directory = Path(directory)

    def save(self, session, segments, request_id):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (session['id'] + '.txt')
        temporary = path.with_suffix('.tmp')
        text = '\n'.join(f"[{datetime.fromtimestamp(s['t'], timezone.utc).isoformat()}] {s['speaker']}: {s['text']}" for s in segments) + '\n'
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temporary, path)
        return {'outcome': 'delivered', 'count': len(segments)}


class Supabase:
    def __init__(self, env, request=http_json):
        self.url = env.get('TRANSCRIPT_SUPABASE_URL', '').rstrip('/')
        self.key = env.get('TRANSCRIPT_SUPABASE_KEY', '')
        self.request = request

    def save(self, session, segments, request_id):
        if not self.key or not self.url.startswith('https://'):
            raise DeliveryError('Configure the dedicated Supabase transcript store')
        self.request(self.url + '/rest/v1/port_call_transcripts?on_conflict=conversation_id',
            dict(conversation_id=session['id'], segments=segments, ended=bool(session['ended']), revision=request_id),
            {'apikey': self.key, 'Authorization': 'Bearer ' + self.key, 'Prefer': 'resolution=merge-duplicates,return=minimal'})
        return {'outcome': 'delivered', 'count': len(segments)}


class Postgres:
    def __init__(self, env):
        self.dsn = env.get('TRANSCRIPT_POSTGRES_DSN', '')

    def save(self, session, segments, request_id):
        if not self.dsn:
            raise DeliveryError('Configure TRANSCRIPT_POSTGRES_DSN')
        try:
            import psycopg
        except ImportError:
            raise DeliveryError('Install psycopg[binary] for PostgreSQL saving') from None
        try:
            with psycopg.connect(self.dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute('SET statement_timeout = 10000')
                    cur.execute('''INSERT INTO port_call_transcripts (conversation_id, segments, ended, revision)
                        VALUES (%s, %s::jsonb, %s, %s) ON CONFLICT (conversation_id) DO UPDATE
                        SET segments=excluded.segments, ended=excluded.ended, revision=excluded.revision''',
                        (session['id'], json.dumps(segments), bool(session['ended']), request_id))
        except Exception:
            raise DeliveryError('PostgreSQL save failed; check configuration and schema') from None
        return {'outcome': 'delivered', 'count': len(segments)}


class DeliveryStore:
    def __init__(self, path, adapters, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.adapters, self.clock = adapters, clock
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, bot TEXT, room TEXT, started REAL,
                ended REAL, segments TEXT, targets TEXT, resource TEXT, approved TEXT, observed REAL);
            CREATE TABLE IF NOT EXISTS deliveries (session TEXT, target TEXT, sent TEXT DEFAULT '{}',
                pending TEXT, request_id TEXT, attempt INTEGER DEFAULT 0, due REAL DEFAULT 0,
                last REAL DEFAULT 0, error TEXT, PRIMARY KEY(session,target));
            CREATE TABLE IF NOT EXISTS legacy_recovery (session TEXT, target TEXT, request_id TEXT,
                pending TEXT NOT NULL, recovered_at REAL NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (request_id TEXT, session TEXT, target TEXT, resource TEXT,
                action TEXT, requested_count INTEGER, delivered_count INTEGER, outcome TEXT, at REAL);
        ''')
        self.db.commit()

    def enroll(self, bot, room, targets, resource=''):
        if any(k not in self.adapters or v not in ('continuous', 'batch', 'manual') for k, v in targets.items()):
            raise ValueError('Invalid destination or timing')
        if 'topos' in targets and not resource:
            raise ValueError('Connect Dialogues before selecting TOPOS')
        with self.lock, self.db:
            sid = 'pc-' + secrets.token_hex(16)
            self.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (sid, str(bot), room, self.clock(), None, '[]', json.dumps(targets), resource, None, 0))
            for target in targets:
                self.db.execute('INSERT INTO deliveries(session,target) VALUES (?,?)', (sid, target))
            return sid

    def sessions(self):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM sessions ORDER BY started')]

    def observe(self, sid, raw, ended=False):
        with self.lock, self.db:
            session = dict(self.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
            # Stable upstream segment ID, or stable timestamp+speaker fallback. Text corrections replace.
            segments = {s['id']: s for s in json.loads(session['segments'])}
            for raw_segment in raw:
                if not raw_segment.get('completed') or not str(raw_segment.get('text') or '').strip():
                    continue
                stamp = float(raw_segment.get('start') or 0)
                if stamp <= 0:
                    continue
                key = str(raw_segment.get('id') or raw_segment.get('segment_id') or digest([stamp, raw_segment.get('speaker')]))
                segment_id = sid + ':' + key
                segments[segment_id] = dict(id=segment_id, t=stamp, speaker=raw_segment.get('speaker') or '?', text=raw_segment['text'].strip())
            snapshot = sorted(segments.values(), key=lambda s: (s['t'], s['id']))
            self.db.execute('UPDATE sessions SET segments=?, ended=?, observed=? WHERE id=?',
                (json.dumps(snapshot), session['ended'] or (self.clock() if ended else None), self.clock(), sid))

    def approve(self, sid):
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row or not row['ended']:
                raise ValueError('End the conversation before saving')
            if self.clock() - row['observed'] > 10:
                raise ValueError('Transcript unavailable; wait for a successful final fetch')
            self.db.execute('UPDATE sessions SET approved=? WHERE id=?', (row['segments'], sid))

    def flush(self):
        with self.lock:
            for session in self.sessions():
                for target, mode in json.loads(session['targets']).items():
                    self._deliver(session, target, mode)

    def _deliver(self, session, target, mode):
        row = dict(self.db.execute('SELECT * FROM deliveries WHERE session=? AND target=?', (session['id'], target)).fetchone())
        now = self.clock()
        if now < row['due']:
            return
        segments = json.loads(session['approved'] or '[]') if mode == 'manual' else json.loads(session['segments'])
        if row['pending']:
            envelope = json.loads(row['pending'])
            if not isinstance(envelope, dict) or envelope.get('version') != 2:
                # Old code persisted only segments; the original ended state is unknowable.
                # Never invent a retry body for a previously used request ID.
                with self.db:
                    self.db.execute('UPDATE deliveries SET error=? WHERE session=? AND target=?',
                        ('Legacy pending write needs explicit recovery; original metadata was not saved', session['id'], target))
                return
            segments = envelope['segments']
        else:
            if not segments or (mode == 'batch' and not session['ended'] and now - row['last'] < 30):
                return
            sent = json.loads(row['sent'])
            changed = [s for s in segments if sent.get(s['id']) != digest(s)]
            metadata_changed = target != 'topos' and sent.get('__ended') != bool(session['ended'])
            if not changed and not metadata_changed:
                return
            if target == 'topos':
                segments = changed[:100]
            frozen_session = {key: session[key] for key in ('id', 'resource', 'ended')}
            if isinstance(self.adapters[target], Topos):
                frozen_session['source_id'] = self.adapters[target].attachment.source
            envelope = dict(version=2, target=target, session=frozen_session, segments=segments)
            request_id = digest(envelope)
            with self.db:
                self.db.execute('UPDATE deliveries SET pending=?,request_id=? WHERE session=? AND target=?',
                    (json.dumps(envelope), request_id, session['id'], target))
            row['request_id'] = request_id
        adapter = self.adapters[target]
        if isinstance(adapter, Topos):
            reason = adapter.pause_reason(envelope['session'])
            if reason:
                with self.db:
                    self.db.execute('UPDATE deliveries SET error=? WHERE session=? AND target=?',
                        (reason, session['id'], target))
                return  # Preserve checkpoint/key; no network attempts or invented denial events.
        try:
            receipt = adapter.save(envelope['session'], segments, row['request_id'])
        except DeliveryPaused as exc:
            # Detach may win the race between the preflight check and save's auth lock.
            with self.db:
                self.db.execute('UPDATE deliveries SET error=? WHERE session=? AND target=?',
                    (str(exc), session['id'], target))
            return
        except Exception as exc:
            status = exc.status if isinstance(exc, DeliveryError) else 0
            message = str(exc) if isinstance(exc, DeliveryError) else 'Destination save failed'
            outcome = 'denied' if status in (401, 403) else 'queued' if status == 202 else 'unconfirmed'
            with self.db:
                self.db.execute('UPDATE deliveries SET attempt=attempt+1,due=?,error=? WHERE session=? AND target=?',
                    (now + min(300, 2 ** min(row['attempt'] + 1, 8)), message, session['id'], target))
                self._audit(session, target, row['request_id'], len(segments), 0 if outcome == 'denied' else None, outcome)
            return
        sent = json.loads(row['sent'])
        sent.update({s['id']: digest(s) for s in segments})
        sent['__ended'] = bool(envelope['session']['ended'])
        with self.db:
            self.db.execute('UPDATE deliveries SET sent=?,pending=NULL,attempt=0,due=0,last=?,error=NULL WHERE session=? AND target=?',
                (json.dumps(sent), now, session['id'], target))
            self._audit(session, target, row['request_id'], len(segments), receipt['count'], receipt['outcome'])

    def _audit(self, session, target, request_id, count, delivered, outcome):
        self.db.execute('INSERT INTO audit VALUES (?,?,?,?,?,?,?,?,?)',
            (request_id, session['id'], target, session['resource'] if target == 'topos' else '',
             'write', count, delivered, outcome, self.clock()))

    def recover_legacy(self, sid, target, expected_request_id, acknowledge_unknown_delivery=False):
        """Retain original evidence; explicitly abandon an unknowable legacy retry, never rewrite it."""
        if not acknowledge_unknown_delivery:
            raise ValueError('Explicit acknowledgment of uncertain prior delivery is required')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.db.execute('SELECT * FROM deliveries WHERE session=? AND target=?', (sid, target)).fetchone()
                if not row or row['request_id'] != expected_request_id or not row['pending']:
                    raise ValueError('Pending request changed or does not exist')
                if not isinstance(json.loads(row['pending']), list):
                    raise ValueError('Only legacy list-only pending writes need this recovery')
                self.db.execute('INSERT INTO legacy_recovery VALUES (?,?,?,?,?,?)',
                    (sid, target, row['request_id'], row['pending'], self.clock(), 'explicit_abandon_unknown_delivery'))
                self.db.execute('UPDATE deliveries SET pending=NULL,request_id=NULL,attempt=0,due=0,error=NULL WHERE session=? AND target=?',
                    (sid, target))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def status(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT s.id,s.room,s.ended,s.targets,s.resource,
                d.target,d.last,d.error,d.attempt,(d.pending IS NOT NULL) AS pending
                FROM sessions s JOIN deliveries d ON s.id=d.session ORDER BY s.started DESC LIMIT 40''')]
