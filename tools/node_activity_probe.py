"""Offline probe: real node handler/readers, temporary SQLite, no issued grant."""
import asyncio
import json
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.core.state import get_uma_request_counts
from topos.dashboard.measurements import daily_measurements
from topos.sources.definitions import DataSourceDefinition
from topos.sources.registry import REGISTRY
from topos.storage.db.migrations import apply_all_migrations


@pytest.mark.asyncio
async def test_port_call_write_count_and_daily_arrival(monkeypatch, tmp_path, pin_db_path):
    def no_network(*args, **kwargs):
        raise AssertionError("Network is disabled in this offline probe")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    source = DataSourceDefinition(**json.loads(Path(
        os.environ["PORT_CALL_SOURCE_MANIFEST"]
    ).read_text()))
    monkeypatch.setitem(REGISTRY, source.source_id, source)
    db = tmp_path / "probe.sqlite"
    pin_db_path(db)
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: conn)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda *_: None)
    now = datetime.now(timezone.utc)
    start, end = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    owner = "offline-port-call-owner"
    resource = f"dataset:{owner}:port-call"
    record = dict(message_id="offline-pc-session:segment-1", conversation_id="offline-pc-session",
                  content="Synthetic offline probe", sender_id="synthetic-speaker", event_at=now.isoformat())
    summary = []
    for name, records in (("success", [record]), ("replay", [record]), ("failed", ["invalid-record"])):
        result = await handle_control_plane_request({
            "id": f"offline-port-call-{name}", "type": "app_ingest",
            "payload": {"user_id": owner, "dataset_id": f"{owner}:port-call:offline",
                        "source_id": source.source_id, "schema_id": source.schema_id,
                        "resource_id": resource, "app_id": "offline-port-call",
                        "requesting_user_id": owner, "records": records},
        })
        daily = daily_measurements(conn, start, end)
        counts = await asyncio.to_thread(get_uma_request_counts, conn, owner)
        summary.append({"case": name, "response_status": result["status"],
                        "processed": result["payload"]["records_processed"],
                        "requested": result["payload"]["records_total"],
                        "uma_write_requests": counts["total_write_requests"],
                        "owner_self_writes": counts["access_attribution_all_time"]["owner_self_writes"],
                        "inbound": daily["metrics"]["inbound_records"]["buckets"][0]})
    conn.close()
    with sqlite3.connect(db) as reopened:
        persisted = get_uma_request_counts(reopened, owner)
        other = get_uma_request_counts(reopened, "offline-other-owner")
        columns = [r[1] for r in reopened.execute("PRAGMA table_info(uma_access_requests)")]
        arrivals = [dict(zip(("family", "item_key", "source_id"), r)) for r in reopened.execute(
            "SELECT family,item_key,source_id FROM dashboard_measurement_items WHERE recorded_at IS NOT NULL")]
    print("OFFLINE_PORT_CALL_PROBE=" + json.dumps({"cases": summary, "persisted_writes": persisted["total_write_requests"],
          "other_owner_writes": other["total_write_requests"], "uma_columns": columns, "arrivals": arrivals}, sort_keys=True))
    assert [r["processed"] for r in summary] == [1, 1, 0]
    assert [r["uma_write_requests"] for r in summary] == [1, 2, 3]
    assert [r["inbound"]["count"] for r in summary] == [1, 1, 1]
    assert persisted["total_write_requests"] == 3
    assert other["total_write_requests"] == 0
