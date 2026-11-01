import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import board
from destinations import DeliveryStore


class BoardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=DeliveryStore(Path(self.tmp.name)/'db', {})
        self.addCleanup(self.store.db.close)
    def test_worker_does_not_enroll_unrequested_running_call(self):
        calls=[]
        def gateway(path,*args):
            calls.append(path)
            return {'running':[{'id': 'historical-or-external'}]}
        with patch.object(board,'SAVES',self.store), patch.object(board,'gw',gateway):
            board.saving_tick()
        self.assertEqual(calls,['/bots/status'])
        self.assertEqual(self.store.sessions(),[])
    def test_final_fetch_uses_id_not_room_and_handles_late_segments(self):
        sid=self.store.enroll('42','abc-defg-hij',{})
        self.store.db.execute('UPDATE sessions SET started=0'); self.store.db.commit()
        calls=[]
        def gateway(path,*args):
            calls.append(path)
            if path=='/bots/status':return {'running':[]}
            return {'segments':[{'id':'a','completed':True,'start':1700000000,'text':'Synthetic final'}]}
        with patch.object(board,'SAVES',self.store), patch.object(board,'gw',gateway):
            board.saving_tick();board.saving_tick()
        self.assertEqual(calls.count('/transcripts/by-id/42'),2)
        self.assertEqual(len(json.loads(self.store.sessions()[0]['segments'])),1)
        self.assertTrue(self.store.sessions()[0]['ended'])
    def test_gateway_failure_does_not_end_call(self):
        sid=self.store.enroll('42','abc-defg-hij',{})
        with patch.object(board,'SAVES',self.store), patch.object(board,'gw',side_effect=OSError('offline')):
            board.saving_tick()
        self.assertFalse(self.store.sessions()[0]['ended'])
        self.assertIsNotNone(board.SAVE_POLL_ERROR)

class HandlerTests(unittest.TestCase):
    class Wire:
        def __init__(self, request):
            import io
            self.input=io.BytesIO(request);self.output=b''
        def makefile(self,*args):return self.input
        def sendall(self,data):self.output+=data
    def call(self,path,body=None,host='localhost:8090',csrf=None):
        payload=json.dumps(body or {}).encode()
        request=(f'POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(payload)}\r\nX-Port-Call-CSRF: {csrf or ""}\r\n\r\n').encode()+payload
        wire=self.Wire(request)
        board.H(wire,('127.0.0.1',1),object())
        return wire.output
    def test_cross_site_requests_rejected_before_gateway(self):
        with patch.object(board,'gw') as gateway:
            self.assertIn(b'403',self.call('/join',{'room':'abc-defg-hij'}).split(b'\r\n')[0])
            self.assertIn(b'403',self.call('/join',{'room':'abc-defg-hij'},host='attacker.example',csrf=board.CSRF).split(b'\r\n')[0])
            gateway.assert_not_called()
    def test_disconnected_topos_join_rejected_before_start(self):
        board.ATTACHMENT.disconnect()
        with patch.object(board,'gw') as gateway:
            response=self.call('/join',{'room':'abc-defg-hij','saving':{'destination':'topos','mode':'manual'}},csrf=board.CSRF)
            self.assertIn(b'400',response.split(b'\r\n')[0]);gateway.assert_not_called()
    def test_join_uses_the_name_typed_in_the_console(self):
        board.ATTACHMENT.disconnect()
        for typed,sent in [('  Frank   the  Bot ','Frank the Bot'),('','Roost'),(None,'Roost'),('x'*90,'x'*60)]:
            with patch.object(board,'gw',return_value={'id':1}) as gateway:
                self.call('/join',{'room':'abc-defg-hij','name':typed},csrf=board.CSRF)
                spawn=[c for c in gateway.call_args_list if c.args[0]=='/bots'][0]
                self.assertEqual(spawn.kwargs['body']['bot_name'],sent)
                self.assertEqual(board.LAST['name'],sent)
    def test_invalid_timing_rejected_before_start(self):
        with patch.object(board,'gw') as gateway:
            response=self.call('/join',{'room':'abc-defg-hij','saving':{'mode':'invalid'}},csrf=board.CSRF)
            self.assertIn(b'400',response.split(b'\r\n')[0]);gateway.assert_not_called()

class AttachmentFlowTests(unittest.TestCase):
    Wire=HandlerTests.Wire
    call=HandlerTests.call
    def get(self, path, cookie=''):
        wire=self.Wire((f'GET {path} HTTP/1.1\r\nHost: localhost:8090\r\nCookie: {cookie}\r\n\r\n').encode())
        board.H(wire,('127.0.0.1',1),object())
        return wire.output

    def test_browser_attach_callback_status_and_detach(self):
        from destinations import Attachment
        from http.cookies import SimpleCookie
        from urllib.parse import parse_qs, urlsplit, urlencode
        exchange=[]
        def request(url,body):
            exchange.append(body)
            return dict(state=state,plugin_attach_token='synthetic-private-ticket',resource_id='dataset:owner:data:node',mcp_access_token='unused-read-token')
        auth=Attachment({},request)
        with tempfile.TemporaryDirectory() as root:
            store=DeliveryStore(Path(root)/'db',{})
            self.addCleanup(store.db.close)
            with patch.object(board,'ATTACHMENT',auth),patch.object(board,'SAVES',store):
                response=self.call('/saving/connect',csrf=board.CSRF)
                header,body=response.split(b'\r\n\r\n',1)
                state=parse_qs(urlsplit(json.loads(body)['url']).query)['state'][0]
                cookie=SimpleCookie(next(line.split(': ',1)[1] for line in header.decode().split('\r\n') if line.startswith('Set-Cookie:')))
                self.assertTrue(auth.status()['pending'])
                response=self.get('/saving/callback?'+urlencode(dict(code='synthetic-code',state=state)),'pc_attach='+cookie['pc_attach'].value)
                self.assertIn(b'303',response.split(b'\r\n')[0]);self.assertIn(b'Location: /\r\n',response)
                status=self.get('/saving')
                self.assertIn(b'"connected": true',status)
                self.assertIn(b'dataset:owner:data:node',status)
                self.assertNotIn(b'synthetic-private-ticket',status);self.assertNotIn(b'unused-read-token',status)
                self.assertEqual(len(exchange),1)
                response=self.call('/saving/disconnect',csrf=board.CSRF)
                self.assertIn(b'200',response.split(b'\r\n')[0]);self.assertFalse(auth.status()['connected'])
                self.assertEqual(auth.token,'');self.assertEqual(auth.resource,'')

    def test_detach_cancels_pending_callback_and_returns_safe_error_to_console(self):
        from destinations import Attachment
        calls=[];auth=Attachment({},lambda *args:calls.append(args))
        with patch.object(board,'ATTACHMENT',auth):
            auth.start('http://localhost:8090/saving/callback')
            state,_,cookie,_=auth.pending
            self.call('/saving/disconnect',csrf=board.CSRF)
            response=self.get('/saving/callback?code=synthetic&state='+state,'pc_attach='+cookie)
            self.assertIn(b'303',response.split(b'\r\n')[0]);self.assertIn(b'Max-Age=0',response)
            self.assertFalse(auth.status()['connected']);self.assertIn('expired',auth.status()['error'])
            self.assertEqual(calls,[])
