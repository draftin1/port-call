"""Actual Roost UI + synthetic consent, tickets and transcripts. No external requests.
Run: python3 tests/preview_attachment.py (http://127.0.0.1:18091).
"""
import html
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
import tempfile
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, urlencode
import board
from destinations import Attachment, DeliveryStore, DeliveryError, Topos

PORT=18091
RESOURCE='dataset:synthetic-owner:sandbox-node:default:test-db'
class PreviewAttachment(Attachment):
    def start(self, redirect):
        url,cookie=super().start(redirect)
        self.preview_state=parse_qs(urlsplit(url).query)['state'][0]
        return '/preview/consent?'+urlencode(dict(state=self.preview_state)),cookie

def synthetic_request(url,body,headers=None):
    if url.endswith('/connect/exchange') and body['code']=='synthetic-approved':
        return dict(state=board.ATTACHMENT.preview_state,plugin_attach_token='synthetic-ticket',resource_id=RESOURCE)
    if url.endswith('/v1/ingestion/app_ingest'):
        return dict(status='queued',write_id='synthetic-queued-write')
    raise DeliveryError('Synthetic consent declined')

root=tempfile.TemporaryDirectory(prefix='port-call-attachment-preview-')
board.ATTACHMENT=PreviewAttachment({},synthetic_request)
board.SAVES=DeliveryStore(Path(root.name)/'db',{'topos':Topos(board.ATTACHMENT)})
sid=board.SAVES.enroll('synthetic-call','synthetic-room',{'topos':'continuous'},RESOURCE)
board.SAVES.observe(sid,[dict(id='demo',start=1700000000,completed=True,speaker='Test participant',text='Synthetic preview transcript.')],ended=True)
board.state=lambda floor=None:dict(phase='none',bot=None,room=None,since=None,joined_at=None,camera=None,presenting=None,failure=None,last_room=None,transcript=[],events=[],pending=[],health=dict(gateway=False,tokens=False,tts=False,stt={}))
def no_gateway(*args,**kwargs):raise RuntimeError('Live meeting actions disabled in this preview')
board.gw=no_gateway
board.PAGE=board.PAGE.replace('<header>', '<div style="padding:8px 16px;background:#332b13;color:#f5c518;font-size:12px">Synthetic Roost preview · simulated consent and ticket · no live account or writes</div><header>',1)
class Preview(board.H):
    def _local(self):return self.headers.get('Host') in (f'localhost:{PORT}',f'127.0.0.1:{PORT}')
    def do_GET(self):
        if not self._local():return self._send('Invalid host','text/plain',code=403)
        url=urlsplit(self.path)
        if url.path=='/preview/consent':
            state=parse_qs(url.query).get('state',[''])[0]
            approve=html.escape('/saving/callback?'+urlencode(dict(code='synthetic-approved',state=state)))
            cancel=html.escape('/saving/callback?'+urlencode(dict(error='access_denied',state=state)))
            return self._send(f'''<!doctype html><title>Synthetic Dialogues consent</title><body style="font:16px system-ui;background:#14141c;color:#e8e8f0;padding:40px"><h1>Synthetic Dialogues consent</h1><p>No real account or live service. Preview only.</p><p>Allow Roost to write transcripts to Synthetic Sandbox?</p><p><a style="color:#8ab4ff" href="{approve}">Approve synthetic attachment</a></p><a style="color:#8ab4ff" href="{cancel}">Cancel</a>''')
        if url.path=='/saving':board.SAVES.flush()
        return super().do_GET()
    def do_POST(self):
        if self.path not in ('/saving/connect','/saving/disconnect'):
            return self._send('Meeting actions disabled in synthetic preview','text/plain',code=403)
        return super().do_POST()

print(f'Synthetic Roost attachment preview: http://127.0.0.1:{PORT}',flush=True)
ThreadingHTTPServer(('127.0.0.1',PORT),Preview).serve_forever()
