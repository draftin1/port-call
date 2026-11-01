import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from destinations import Attachment, DeliveryError, DeliveryStore, LocalFile, Topos, Supabase


class Sink:
    def __init__(self):
        self.calls = []
        self.fail = False
    def save(self, session, segments, request_id):
        self.calls.append((session.copy(), segments.copy(), request_id))
        if self.fail:
            raise DeliveryError('offline')
        return dict(outcome='delivered', count=len(segments))


def segment(i=1, text='Synthetic test sentence'):
    return dict(id=i, start=1700000000+i, speaker='Synthetic speaker', text=text, completed=True)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.sink = Sink()
        self.path = Path(self.tmp.name) / 'save.db'
        self.store = DeliveryStore(self.path, {'topos': self.sink, 'file': LocalFile(self.tmp.name)}, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.db.close())
    def enroll(self, mode='continuous', target='topos'):
        return self.store.enroll(7, 'abc-defg-hij', {target: mode}, 'dataset:owner:data:node')
    def test_incremental_corrections_and_duplicate_polls(self):
        sid = self.enroll()
        self.store.observe(sid, [segment()]); self.store.flush(); self.store.flush()
        self.assertEqual(len(self.sink.calls), 1)
        self.store.observe(sid, [segment(), segment(2)]); self.store.flush()
        self.assertEqual(len(self.sink.calls[-1][1]), 1)
        self.store.observe(sid, [segment(text='Corrected sentence')]); self.store.flush()
        self.assertEqual(self.sink.calls[-1][1][0]['id'], self.sink.calls[0][1][0]['id'])
    def test_retry_frozen_batch_survives_restart(self):
        sid = self.enroll(); self.sink.fail = True
        self.store.observe(sid, [segment()]); self.store.flush()
        key = self.sink.calls[0][2]
        self.store.observe(sid, [segment(2)]); self.store.flush()
        self.assertEqual(len(self.sink.calls), 1)
        self.store.db.close()
        self.store = DeliveryStore(self.path, {'topos': self.sink}, clock=lambda: self.now)
        self.now += 3; self.sink.fail = False; self.store.flush()
        self.assertEqual(self.sink.calls[-1][2], key)
        self.assertEqual(len(self.sink.calls[-1][1]), 1)
        self.store.flush()
        self.assertEqual(len(self.sink.calls[-1][1]), 1)
        self.assertNotEqual(self.sink.calls[-1][2], key)
    def test_batch_and_final_flush(self):
        sid = self.enroll('batch')
        self.store.observe(sid, [segment()]); self.store.flush()
        self.now += 1; self.store.observe(sid, [segment(2)]); self.store.flush()
        self.assertEqual(len(self.sink.calls), 1)
        self.store.observe(sid, [], ended=True); self.store.flush()
        self.assertEqual(len(self.sink.calls), 2)
    def test_manual_requires_end_and_click_and_freezes_approved_snapshot(self):
        sid = self.enroll('manual')
        self.store.observe(sid, [segment()]); self.store.flush()
        with self.assertRaises(ValueError): self.store.approve(sid)
        self.store.observe(sid, [], ended=True); self.store.flush()
        self.assertEqual(self.sink.calls, [])
        self.store.approve(sid); self.store.observe(sid, [segment(2)]); self.store.flush()
        self.assertEqual(len(self.sink.calls[0][1]), 1)
        self.store.flush(); self.assertEqual(len(self.sink.calls), 1)
        self.store.approve(sid); self.store.flush(); self.assertEqual(len(self.sink.calls), 2)
    def test_one_file_per_call(self):
        sid = self.enroll(target='file')
        self.store.observe(sid, [segment()]); self.store.flush()
        self.store.observe(sid, [segment(2)]); self.store.flush()
        files = list(Path(self.tmp.name).glob('*.txt'))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(files[0].read_text().splitlines()), 2)
    def test_provisional_ignored_and_calls_separate(self):
        a = self.enroll(); b = self.enroll()
        provisional = segment(); provisional['completed'] = False
        self.store.observe(a, [provisional]); self.store.flush()
        self.assertFalse(self.sink.calls)
        self.store.observe(a, [segment()]); self.store.observe(b, [segment()]); self.store.flush()
        self.assertNotEqual(self.sink.calls[0][1][0]['id'], self.sink.calls[1][1][0]['id'])
    def test_audit_has_counts_no_text(self):
        sid = self.enroll(); self.store.observe(sid, [segment()]); self.store.flush()
        audit = dict(self.store.db.execute('SELECT * FROM audit').fetchone())
        self.assertEqual(audit['delivered_count'], 1)
        self.assertNotIn('Synthetic', json.dumps(audit))
    def test_no_implicit_enrollment(self):
        self.store.flush(); self.assertEqual(self.store.sessions(), [])
    def test_independent_destinations(self):
        self.sink.fail = True
        sid = self.store.enroll(7, 'abc-defg-hij', {'topos':'continuous', 'file':'continuous'}, 'dataset:owner:data:node')
        self.store.observe(sid, [segment()]); self.store.flush()
        self.assertTrue((Path(self.tmp.name) / (sid+'.txt')).exists())
    def test_stale_manual_fetch_refused(self):
        sid = self.enroll('manual'); self.store.observe(sid, [segment()], ended=True)
        self.now += 20
        with self.assertRaises(ValueError): self.store.approve(sid)


class AttachmentTests(unittest.TestCase):
    def test_pkce_state_cookie_and_exchange(self):
        calls = []
        auth = Attachment({}, lambda url, body: calls.append(body) or dict(state=state, plugin_attach_token='synthetic-secret', resource_id='dataset:owner:data:node'))
        url, cookie = auth.start('http://localhost:8090/saving/callback')
        query = parse_qs(urlsplit(url).query); state = query['state'][0]
        self.assertEqual(query['scopes'], ['messages:write'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        auth.finish('test-code', state, cookie)
        self.assertTrue(auth.status()['connected'])
        self.assertNotIn('secret', json.dumps(auth.status()))
        self.assertTrue(calls[0]['code_verifier'])
        auth.disconnect(); self.assertFalse(auth.status()['connected'])
    def test_mismatch_never_exchanges(self):
        calls=[]; auth = Attachment({}, lambda *args: calls.append(args))
        _, cookie = auth.start('http://localhost:8090/saving/callback')
        with self.assertRaises(DeliveryError): auth.finish('code', 'wrong', cookie)
        self.assertEqual(calls, [])
    def test_wrong_browser_never_exchanges(self):
        calls=[]; auth = Attachment({}, lambda *args: calls.append(args))
        url, _ = auth.start('http://localhost:8090/saving/callback')
        with self.assertRaises(DeliveryError): auth.finish('code', parse_qs(urlsplit(url).query)['state'][0], 'wrong')
        self.assertEqual(calls, [])


class AdapterTests(unittest.TestCase):
    def setup_topos(self, response):
        calls=[]
        def request(*args):
            calls.append(args)
            if isinstance(response, Exception): raise response
            return response
        auth = Attachment({}, request)
        auth.token, auth.resource = 'synthetic-token', 'dataset:owner:data:node'
        session=dict(id='pc-test', resource=auth.resource)
        segments=[dict(id='stable', t=1700000000, text='Synthetic only', speaker='Test')]
        return auth, Topos(auth), session, segments, calls
    def test_actual_contract_and_stable_identity(self):
        auth, adapter, session, segments, calls = self.setup_topos(dict(status='ok',payload=dict(records_processed=1,errors=[])))
        receipt = adapter.save(session,segments,'request-1')
        self.assertEqual(receipt['count'],1)
        self.assertTrue(calls[0][0].endswith('/v1/ingestion/app_ingest'))
        self.assertEqual(calls[0][1]['records'][0]['message_id'],'stable')
        self.assertEqual(calls[0][2]['Idempotency-Key'],'request-1')
    def test_denial_clears_auth_and_sends_no_further_requests(self):
        auth, adapter, session, segments, calls = self.setup_topos(DeliveryError('denied',403))
        for _ in range(2):
            with self.assertRaises(DeliveryError): adapter.save(session,segments,'request-1')
        self.assertFalse(auth.status()['connected']); self.assertEqual(len(calls),1)
    def test_different_account_never_receives_pending_transcript(self):
        auth, adapter, session, segments, calls = self.setup_topos({})
        auth.resource='dataset:other:data:node'
        with self.assertRaises(DeliveryError): adapter.save(session,segments,'request-1')
        self.assertFalse(calls)
    def test_partial_and_queued_are_not_reported_delivered(self):
        for response in [dict(status='queued',write_id='queue-1'),dict(status='ok',payload=dict(records_processed=0,errors=[])),dict(status='ok',payload={})]:
            auth, adapter, session, segments, calls = self.setup_topos(response)
            with self.assertRaises(DeliveryError): adapter.save(session,segments,'request-1')
    def test_supabase_upsert_contract(self):
        calls=[]
        adapter=Supabase(dict(TRANSCRIPT_SUPABASE_URL='https://example.supabase.co',TRANSCRIPT_SUPABASE_KEY='synthetic'),lambda *args:calls.append(args))
        adapter.save(dict(id='pc-test',ended=None), [], 'req-1')
        self.assertIn('on_conflict=conversation_id', calls[0][0])
        self.assertEqual(calls[0][1]['conversation_id'],'pc-test')
        self.assertIn('merge-duplicates', calls[0][2]['Prefer'])

if __name__ == '__main__': unittest.main()

class RedirectTests(unittest.TestCase):
    def test_credentials_never_follow_redirect(self):
        from destinations import NoRedirect
        with self.assertRaises(DeliveryError):
            NoRedirect().redirect_request(None,None,302,'Moved',{},'https://unexpected.example')

class FrozenEnvelopeTests(unittest.TestCase):
    def test_supabase_retry_then_distinct_final_revision_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'save.db'; now=[1000]; calls=[]; fail=[True]
            def request(url,body,headers):
                calls.append(json.loads(json.dumps(body)))
                if fail[0]: raise DeliveryError('connection lost')
                return {}
            adapter=Supabase(dict(TRANSCRIPT_SUPABASE_URL='https://synthetic.supabase.co',TRANSCRIPT_SUPABASE_KEY='synthetic'),request)
            store=DeliveryStore(path,{'supabase':adapter},clock=lambda:now[0])
            sid=store.enroll(42,'abc-defg-hij',{'supabase':'continuous'})
            store.observe(sid,[segment()]);store.flush()
            store.observe(sid,[],ended=True);store.db.close()
            store=DeliveryStore(path,{'supabase':adapter},clock=lambda:now[0])
            try:
                fail[0]=False;now[0]+=3;store.flush()
                self.assertEqual(calls[0],calls[1])
                self.assertFalse(calls[1]['ended'])
                store.flush()
                self.assertTrue(calls[2]['ended'])
                self.assertNotEqual(calls[2]['revision'],calls[1]['revision'])
                self.assertEqual(calls[2]['segments'],calls[1]['segments'])
                store.flush();self.assertEqual(len(calls),3)
            finally:store.db.close()
    def test_metadata_only_finalization_changes_revision(self):
        with tempfile.TemporaryDirectory() as root:
            sink=Sink();store=DeliveryStore(Path(root)/'db',{'supabase':sink})
            try:
                sid=store.enroll(42,'abc-defg-hij',{'supabase':'continuous'})
                store.observe(sid,[segment()]);store.flush()
                store.observe(sid,[],ended=True);store.flush()
                self.assertEqual(len(sink.calls),2)
                self.assertNotEqual(sink.calls[0][2],sink.calls[1][2])
                self.assertFalse(sink.calls[0][0]['ended']);self.assertTrue(sink.calls[1][0]['ended'])
            finally:store.db.close()
    def test_legacy_pending_does_not_guess_original_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            sink=Sink();store=DeliveryStore(Path(root)/'db',{'supabase':sink})
            try:
                sid=store.enroll(42,'abc-defg-hij',{'supabase':'continuous'})
                store.db.execute("UPDATE deliveries SET pending='[]', request_id='legacy'");store.db.commit()
                store.flush();self.assertEqual(sink.calls,[])
                self.assertIn('explicit recovery',store.status()[0]['error'])
            finally:store.db.close()

class LegacyRecoveryTests(unittest.TestCase):
    def test_explicit_recovery_preserves_unknown_payload_and_requires_exact_id(self):
        with tempfile.TemporaryDirectory() as root:
            sink=Sink();store=DeliveryStore(Path(root)/'db',{'supabase':sink})
            try:
                sid=store.enroll(42,'synthetic',{'supabase':'continuous'})
                store.observe(sid,[segment()],ended=True)
                legacy=json.dumps([{'id':'unknown-old','text':'synthetic old payload'}])
                store.db.execute('UPDATE deliveries SET pending=?,request_id=?',(legacy,'legacy-id'));store.db.commit()
                with self.assertRaises(ValueError):store.recover_legacy(sid,'supabase','legacy-id')
                with self.assertRaises(ValueError):store.recover_legacy(sid,'supabase','wrong',True)
                store.recover_legacy(sid,'supabase','legacy-id',True)
                row=store.db.execute('SELECT * FROM legacy_recovery').fetchone()
                self.assertEqual(row['pending'],legacy);self.assertEqual(row['request_id'],'legacy-id')
                store.flush();self.assertEqual(len(sink.calls),1)
                self.assertNotEqual(sink.calls[0][2],'legacy-id')
            finally:store.db.close()

class AttachmentRetryTests(unittest.TestCase):
    def test_detached_queue_is_paused_and_only_same_target_can_resume_frozen_write(self):
        calls=[];now=[1000];queued=[True]
        def request(url,body,headers):
            calls.append((json.loads(json.dumps(body)),dict(headers)))
            return dict(status='queued',write_id='synthetic-write') if queued[0] else dict(status='ok',payload=dict(records_processed=len(body['records'])))
        auth=Attachment({},request);auth.token='synthetic';auth.resource='dataset:owner:data:node'
        with tempfile.TemporaryDirectory() as root:
            store=DeliveryStore(Path(root)/'db',{'topos':Topos(auth)},clock=lambda:now[0])
            try:
                sid=store.enroll(1,'synthetic',{'topos':'continuous'},auth.resource)
                store.observe(sid,[segment()]);store.flush()
                before=dict(store.db.execute('SELECT * FROM deliveries').fetchone())
                auth.disconnect();now[0]+=1000;store.flush();store.flush()
                paused=dict(store.db.execute('SELECT * FROM deliveries').fetchone())
                self.assertEqual(len(calls),1);self.assertEqual(paused['pending'],before['pending'])
                self.assertEqual(paused['attempt'],before['attempt']);self.assertEqual(paused['request_id'],before['request_id'])
                self.assertIn('Paused',paused['error'])
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],1)
                auth.token='other-ticket';auth.resource='dataset:other:data:node';store.flush()
                self.assertEqual(len(calls),1)
                auth.token='reattached-ticket';auth.resource='dataset:owner:data:node';queued[0]=False;store.flush()
                self.assertEqual(len(calls),2);self.assertEqual(calls[0][0],calls[1][0])
                self.assertEqual(calls[0][1]['Idempotency-Key'],calls[1][1]['Idempotency-Key'])
                self.assertEqual(calls[1][1]['Authorization'],'Bearer reattached-ticket')
                self.assertFalse(store.status()[0]['pending'])
            finally:store.db.close()

    def test_revocation_clears_ticket_and_explains_reattachment(self):
        def revoked(*args):raise DeliveryError('Unauthorized',401)
        auth=Attachment({},revoked);auth.token='synthetic';auth.resource='dataset:owner:data:node'
        with self.assertRaises(DeliveryError):Topos(auth).save(dict(id='s',resource=auth.resource),[], 'req')
        self.assertFalse(auth.status()['connected']);self.assertIn('expired or was revoked',auth.status()['error'])
        auth.start('http://localhost:8090/saving/callback');self.assertEqual(auth.status()['error'],'')

    def test_expired_consent_does_not_exchange(self):
        from unittest.mock import patch
        calls=[];auth=Attachment({},lambda *args:calls.append(args))
        with patch('destinations.time.time',return_value=1000):
            auth.start('http://localhost:8090/saving/callback');state,_,cookie,_=auth.pending
        with patch('destinations.time.time',return_value=1601):
            self.assertFalse(auth.status()['pending'])
            with self.assertRaises(DeliveryError):auth.finish('code',state,cookie)
        self.assertEqual(calls,[]);self.assertFalse(auth.status()['connected'])

    def test_detach_waits_for_inflight_send_then_refuses_subsequent_sends(self):
        import threading
        started=threading.Event();release=threading.Event();detached=threading.Event();calls=[]
        def request(*args):
            calls.append(args);started.set();release.wait(2)
            return dict(status='ok',payload=dict(records_processed=0))
        auth=Attachment({},request);auth.token='synthetic';auth.resource='dataset:owner:data:node'
        adapter=Topos(auth);session=dict(id='s',resource=auth.resource)
        sender=threading.Thread(target=lambda:adapter.save(session,[],'before-detach'))
        sender.start();self.assertTrue(started.wait(1))
        def detach():auth.disconnect();detached.set()
        detacher=threading.Thread(target=detach);detacher.start()
        try:self.assertFalse(detached.wait(.02))
        finally:release.set();sender.join(2);detacher.join(2)
        self.assertTrue(detached.is_set());self.assertFalse(sender.is_alive())
        with self.assertRaises(DeliveryError):adapter.save(session,[],'after-detach')
        self.assertEqual(len(calls),1)

    def test_non_ascii_callback_state_is_rejected_without_exchange(self):
        calls=[];auth=Attachment({},lambda *args:calls.append(args))
        _,cookie=auth.start('http://localhost:8090/saving/callback')
        with self.assertRaises(DeliveryError):auth.finish('code','not-valid-✓',cookie)
        self.assertEqual(calls,[]);self.assertFalse(auth.status()['connected'])

    def test_detach_between_preflight_and_send_is_not_recorded_as_remote_denial(self):
        calls=[];auth=Attachment({},lambda *args:calls.append(args))
        auth.token='synthetic';auth.resource='dataset:owner:data:node'
        class DetachDuringPreflight(Topos):
            def pause_reason(self,session):
                reason=super().pause_reason(session)
                self.attachment.disconnect()
                return reason
        with tempfile.TemporaryDirectory() as root:
            store=DeliveryStore(Path(root)/'db',{'topos':DetachDuringPreflight(auth)})
            try:
                sid=store.enroll(1,'synthetic',{'topos':'continuous'},auth.resource)
                store.observe(sid,[segment()]);store.flush()
                self.assertEqual(calls,[]);self.assertEqual(store.status()[0]['attempt'],0)
                self.assertTrue(store.status()[0]['pending'])
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],0)
            finally:store.db.close()
