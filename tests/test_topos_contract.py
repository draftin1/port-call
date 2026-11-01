"""Optional read-only checks against a shipping TOPOS checkout; never starts a node."""
import json
import os
from pathlib import Path
import sys
import unittest

@unittest.skipUnless(os.environ.get('TOPOS_CHECKOUT'), 'Set TOPOS_CHECKOUT to test the installed TOPOS parser contract')
class ShippingToposContract(unittest.TestCase):
    def test_source_and_segment_identity(self):
        sys.path.insert(0,os.environ['TOPOS_CHECKOUT'])
        from topos.sources.bundled_canonical_triples import normalize_canonical_source_payload
        from topos.sources.definitions import DataSourceDefinition
        from topos.ingestion.parsers.voxterm_parser import VoxtermTranscriptParser
        from topos.ingestion.sources.base import RawRecord
        source=DataSourceDefinition(**normalize_canonical_source_payload(json.loads((Path(__file__).parents[1]/'integrations/source.json').read_text())))
        self.assertEqual(source.canonical_mapper_id,'voxterm_transcript')
        parser=VoxtermTranscriptParser('synthetic-owner:roost')
        payload=dict(message_id='pc-synthetic:one',conversation_id='pc-synthetic',content='Synthetic sentence',sender_id='Synthetic speaker',event_at='2026-10-22T14:00:00+00:00')
        raw=RawRecord(record_id=payload['message_id'],payload=payload)
        self.assertTrue(parser.validate(raw).is_valid)
        normalized=parser.parse(raw)
        self.assertEqual(normalized.record_id,payload['message_id'])
        self.assertEqual(normalized.payload['thread_id'],payload['conversation_id'])
        self.assertEqual(normalized.payload['sender_id'],payload['sender_id'])
        payload['content']='Corrected synthetic sentence'
        self.assertEqual(parser.parse(raw).record_id,normalized.record_id)
