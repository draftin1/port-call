"""Real isolated PostgreSQL adapter test; uses a temporary Unix socket, no shared DB."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from destinations import Postgres, DeliveryStore, DeliveryError


@unittest.skipUnless(os.environ.get('RUN_POSTGRES_TESTS') == '1', 'Set RUN_POSTGRES_TESTS=1 with working local PostgreSQL server tools')
class PostgresIntegration(unittest.TestCase):
    def test_upsert_and_final_state(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest('psycopg unavailable')
        bindir=Path(os.environ.get('POSTGRES_BIN', '/opt/homebrew/Cellar/postgresql@17/17.10/bin'))
        tool=lambda name: str(bindir/name) if bindir.exists() else name
        with tempfile.TemporaryDirectory(prefix='pc-pg-') as root:
            data, socket = Path(root)/'data', Path(root)/'socket'
            socket.mkdir()
            subprocess.run([tool('initdb'),'-D',str(data),'-A','trust','--no-locale','-E','UTF8'],check=True,capture_output=True)
            subprocess.run([tool('pg_ctl'),'-D',str(data),'-l',str(Path(root)/'postgres.log'),'-o',f"-h '' -k {socket} -p 55439",'-w','start'],check=True,capture_output=True)
            try:
                dsn=f'host={socket} port=55439 dbname=postgres'
                with psycopg.connect(dsn) as conn:
                    conn.execute((Path(__file__).parents[1]/'integrations/postgres.sql').read_text())
                adapter=Postgres({'TRANSCRIPT_POSTGRES_DSN':dsn})
                session=dict(id='pc-synthetic', ended=None)
                adapter.save(session,[{'id':'one','text':'synthetic'}],'revision-one')
                adapter.save(session,[{'id':'one','text':'synthetic'}],'revision-one')
                session['ended']=1700000000
                adapter.save(session,[{'id':'one','text':'corrected synthetic'}, {'id':'two','text':'synthetic two'}],'revision-two')
                with psycopg.connect(dsn) as conn:
                    rows=conn.execute('SELECT segments, ended, revision FROM port_call_transcripts').fetchall()
                self.assertEqual(len(rows),1)
                self.assertEqual(len(rows[0][0]),2)
                self.assertTrue(rows[0][1])
                self.assertEqual(rows[0][2],'revision-two')
                # Lost acknowledgment after a real commit, then restart with final state observed.
                class LoseFirstAck:
                    def __init__(self): self.first=True
                    def save(self, session, segments, request_id):
                        receipt=adapter.save(session,segments,request_id)
                        if self.first:
                            self.first=False
                            raise DeliveryError('synthetic lost acknowledgment')
                        return receipt
                now=[1000]; wrapper=LoseFirstAck(); path=Path(root)/'delivery.db'
                store=DeliveryStore(path,{'postgres':wrapper},clock=lambda:now[0])
                sid=store.enroll(42,'synthetic-room',{'postgres':'continuous'})
                store.observe(sid,[dict(id=1,start=1700000000,speaker='Synthetic',text='Synthetic',completed=True)])
                store.flush()
                def readback():
                    with psycopg.connect(dsn) as conn:
                        return conn.execute('SELECT segments, ended, revision FROM port_call_transcripts WHERE conversation_id=%s',(sid,)).fetchone()
                original=readback();self.assertFalse(original[1])
                store.observe(sid,[],ended=True);store.db.close()
                store=DeliveryStore(path,{'postgres':wrapper},clock=lambda:now[0])
                now[0]+=3;store.flush()
                self.assertEqual(readback(),original)
                store.flush();final=readback()
                self.assertTrue(final[1]);self.assertNotEqual(final[2],original[2])
                self.assertEqual(final[0],original[0])
                store.flush();self.assertEqual(readback(),final)
                store.db.close()
                # Successful initial write followed by metadata-only finalization.
                store=DeliveryStore(Path(root)/'normal.db',{'postgres':adapter})
                sid=store.enroll(43,'synthetic-room',{'postgres':'continuous'})
                store.observe(sid,[dict(id=1,start=1700000000,speaker='Synthetic',text='Synthetic',completed=True)])
                store.flush();original=readback()
                store.observe(sid,[],ended=True);store.flush();final=readback()
                self.assertFalse(original[1]);self.assertTrue(final[1]);self.assertNotEqual(original[2],final[2])
                self.assertEqual(original[0],final[0]);store.db.close()

            finally:
                subprocess.run([tool('pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,capture_output=True)
