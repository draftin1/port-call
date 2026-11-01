"""Synthetic-only console preview on :18090. No Docker, TOPOS, or live history access."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
import tempfile
import threading
import board
from destinations import DeliveryStore, LocalFile
from http.server import ThreadingHTTPServer
root=tempfile.TemporaryDirectory(prefix='port-call-preview-')
board.SAVES=DeliveryStore(Path(root.name)/'db',{'file':LocalFile(root.name)})
sid=board.SAVES.enroll('synthetic-42','abc-defg-hij',{'file':'manual'})
board.SAVES.observe(sid,[dict(id='demo',start=1700000000,completed=True,speaker='Test participant',text='Synthetic preview transcript.')],ended=True)
board.state=lambda floor=None:dict(phase='none',bot=None,room=None,since=None,joined_at=None,camera=None,presenting=None,failure=None,last_room=None,transcript=[],events=[],pending=[],health=dict(gateway=False,tokens=False,tts=False,stt={}))
board.gw=lambda *args,**kwargs:dict(segments=[dict(id='demo',start=1700000000,completed=True,speaker='Test participant',text='Synthetic preview transcript.')])
class Preview(board.H):
    def _local(self):return self.headers.get('Host') in ('localhost:18090','127.0.0.1:18090')
threading.Thread(target=board.saving_worker,daemon=True).start()
ThreadingHTTPServer(('127.0.0.1',18090),Preview).serve_forever()
