#!/usr/bin/env python3
"""Inspect/recover old pending writes without printing transcript content. Stop board.py first."""
import argparse
import json
from pathlib import Path
from destinations import DeliveryStore

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--session")
parser.add_argument("--target")
parser.add_argument("--expected-request-id")
parser.add_argument("--abandon-legacy", action="store_true")
parser.add_argument("--acknowledge-uncertain-delivery", action="store_true")
args = parser.parse_args()
if not args.database.is_file():
    parser.error("Existing delivery database required")
store = DeliveryStore(args.database, {})
try:
    if args.abandon_legacy:
        if not args.session or not args.target or not args.expected_request_id:
            parser.error(
                "Recovery requires session, target and exact expected request ID"
            )
        store.recover_legacy(
            args.session,
            args.target,
            args.expected_request_id,
            args.acknowledge_uncertain_delivery,
        )
        print(
            "Original pending payload retained in legacy_recovery. Current approved/automatic state may send as a new revision on restart."
        )
    else:
        rows = store.db.execute(
            "SELECT session,target,request_id,pending FROM deliveries WHERE pending IS NOT NULL"
        )
        print(
            json.dumps(
                [
                    dict(
                        session=r["session"],
                        target=r["target"],
                        request_id=r["request_id"],
                        legacy=isinstance(json.loads(r["pending"]), list),
                    )
                    for r in rows
                ],
                indent=2,
            )
        )
except ValueError as exc:
    parser.error(str(exc))
finally:
    store.db.close()
