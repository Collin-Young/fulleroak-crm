"""Durable, idempotent workflow outbox and worker for CRM automations."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from crm_db import CRMDatabase, utc_now


ALLOWED_TRIGGERS = {
    "lead_created", "activity_added", "incoming_call", "missed_call",
    "sms_sent", "stage_changed",
    "document_sent", "document_viewed", "document_signed", "document_declined",
}
ALLOWED_ACTIONS = {"create_task", "prepare_sms_draft"}


def validate_definition(definition: dict[str, Any]) -> dict[str, Any]:
    trigger = definition.get("trigger") or {}
    trigger_type = trigger.get("type")
    if trigger_type not in ALLOWED_TRIGGERS:
        raise ValueError("Choose a supported workflow trigger.")
    conditions = definition.get("conditions") or {}
    min_score = conditions.get("min_score")
    if min_score not in (None, ""):
        min_score = int(min_score)
        if not 1 <= min_score <= 5:
            raise ValueError("Minimum priority must be between 1 and 5.")
        conditions["min_score"] = min_score
    delay = int(definition.get("delay_minutes") or 0)
    if not 0 <= delay <= 43_200:
        raise ValueError("Delay must be between zero and 30 days.")
    actions = definition.get("actions") or []
    if not 1 <= len(actions) <= 5:
        raise ValueError("A workflow needs between one and five actions.")
    cleaned_actions = []
    for action in actions:
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTIONS:
            raise ValueError("Workflow action is not allowed.")
        if action_type == "create_task":
            title = str(action.get("title") or "").strip()[:240]
            if not title:
                raise ValueError("Task actions require a title.")
            cleaned_actions.append({"type": action_type, "title": title})
        else:
            message = str(action.get("message") or "").strip()[:1600]
            if not message:
                raise ValueError("Text-draft actions require a message.")
            cleaned_actions.append({
                "type": action_type,
                "angle": str(action.get("angle") or "Workflow follow-up").strip()[:80],
                "message": message,
            })
    stage_key = str(trigger.get("stage_key") or "").strip()[:60]
    return {
        "trigger": {"type": trigger_type, **({"stage_key": stage_key} if stage_key else {})},
        "conditions": conditions,
        "delay_minutes": delay,
        "actions": cleaned_actions,
    }


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return ""


class WorkflowEngine:
    def __init__(self, database: CRMDatabase | Path | str = Path("data/leads.db")) -> None:
        self.db = database if isinstance(database, CRMDatabase) else CRMDatabase(database)

    def recover_stale_locks(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE workflow_outbox SET status='failed',available_at=?,last_error='Recovered stale worker lock'
                   WHERE status='processing' AND locked_at<?""",
                (utc_now(), cutoff),
            )
            connection.execute(
                """UPDATE workflow_jobs SET status='failed',available_at=?,last_error='Recovered stale worker lock',updated_at=?
                   WHERE status='processing' AND locked_at<?""",
                (utc_now(), utc_now(), cutoff),
            )

    def process_once(self, limit: int = 50) -> dict[str, int]:
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO workflow_worker_state(worker_name,last_seen_at,status,details_json)
                   VALUES ('default',?,'processing','{}')
                   ON CONFLICT(worker_name) DO UPDATE SET last_seen_at=excluded.last_seen_at,status=excluded.status""",
                (utc_now(),),
            )
        delivered = self.process_outbox(limit)
        jobs = self.process_jobs(limit)
        result = {"events": delivered, "jobs": jobs}
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE workflow_worker_state SET last_seen_at=?,status='idle',details_json=?
                   WHERE worker_name='default'""",
                (utc_now(), json.dumps(result, sort_keys=True)),
            )
        return result

    def process_outbox(self, limit: int = 50) -> int:
        processed = 0
        for _ in range(limit):
            with self.db.connect() as connection:
                event = connection.execute(
                    """SELECT * FROM workflow_outbox
                       WHERE status IN ('pending','failed') AND available_at<=? AND attempts<5
                       ORDER BY id LIMIT 1""",
                    (utc_now(),),
                ).fetchone()
                if not event:
                    break
                claimed = connection.execute(
                    """UPDATE workflow_outbox SET status='processing',attempts=attempts+1,locked_at=?
                       WHERE id=? AND status IN ('pending','failed')""",
                    (utc_now(), event["id"]),
                )
                if not claimed.rowcount:
                    continue
                event = dict(event)
            try:
                self._dispatch_event(event)
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE workflow_outbox SET status='delivered',delivered_at=?,locked_at=NULL,last_error='' WHERE id=?",
                        (utc_now(), event["id"]),
                    )
                processed += 1
            except Exception as exc:
                logging.exception("Workflow outbox event %s failed", event["id"])
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** int(event["attempts"] or 0)))).isoformat(timespec="seconds")
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE workflow_outbox SET status='failed',available_at=?,locked_at=NULL,last_error=? WHERE id=?",
                        (retry_at, str(exc)[:1000], event["id"]),
                    )
        return processed

    def _dispatch_event(self, event: dict[str, Any]) -> None:
        payload = json.loads(event["payload_json"])
        lead_id = payload.get("lead_id")
        activity = None
        trigger_types = {"lead_created"} if event["topic"] == "lead.created" else {"activity_added"}
        if event["topic"] == "activity.created":
            with self.db.connect() as connection:
                activity = connection.execute(
                    "SELECT * FROM activity_events WHERE id=? AND organization_id=?",
                    (payload.get("activity_id"), event["organization_id"]),
                ).fetchone()
            if not activity:
                return
            lead_id = activity["lead_id"]
            if activity["event_type"] == "stage_change":
                trigger_types.add("stage_changed")
            if activity["channel"] == "call" and activity["direction"] == "incoming":
                trigger_types.add("incoming_call")
            if activity["channel"] == "call" and activity["direction"] == "missed":
                trigger_types.add("missed_call")
            if activity["channel"] == "sms" and activity["direction"] == "outgoing":
                trigger_types.add("sms_sent")
            document_trigger = {
                "document_sent": "document_sent", "document_viewed": "document_viewed",
                "document_signed": "document_signed", "document_declined": "document_declined",
            }.get(activity["event_type"])
            if document_trigger:
                trigger_types.add(document_trigger)
        if not lead_id:
            return
        with self.db.connect() as connection:
            lead = connection.execute(
                "SELECT * FROM leads WHERE id=? AND organization_id=?",
                (lead_id, event["organization_id"]),
            ).fetchone()
            definitions = connection.execute(
                """SELECT d.*,v.definition_json FROM workflow_definitions d
                   JOIN workflow_versions v ON v.workflow_id=d.id AND v.version=d.published_version
                   WHERE d.organization_id=? AND d.enabled=1 AND d.published_version IS NOT NULL""",
                (event["organization_id"],),
            ).fetchall()
        if not lead:
            return
        for workflow in definitions:
            definition = validate_definition(json.loads(workflow["definition_json"]))
            trigger = definition["trigger"]
            if trigger["type"] not in trigger_types:
                continue
            if trigger["type"] == "stage_changed" and trigger.get("stage_key") and lead["status"] != trigger["stage_key"]:
                continue
            conditions = definition["conditions"]
            if conditions.get("min_score") and lead["follow_up_score"] < int(conditions["min_score"]):
                continue
            if conditions.get("status") and lead["status"] != conditions["status"]:
                continue
            if conditions.get("has_reference") and not lead["reference_number"]:
                continue
            self._create_run(dict(workflow), definition, event, dict(lead), activity)

    def _create_run(self, workflow: dict[str, Any], definition: dict[str, Any], event: dict[str, Any], lead: dict[str, Any], activity) -> None:
        now = datetime.now(timezone.utc)
        available = (now + timedelta(minutes=definition["delay_minutes"])).isoformat(timespec="seconds")
        key = f"workflow:{workflow['id']}:v{workflow['published_version']}:event:{event['id']}"
        with self.db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO workflow_runs(
                    organization_id,workflow_id,workflow_version,trigger_topic,trigger_event_id,
                    lead_id,idempotency_key,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    workflow["organization_id"], workflow["id"], workflow["published_version"],
                    event["topic"], activity["id"] if activity else lead["id"], lead["id"], key,
                    "waiting" if definition["delay_minutes"] else "queued", utc_now(),
                ),
            )
            run = connection.execute(
                "SELECT id FROM workflow_runs WHERE organization_id=? AND idempotency_key=?",
                (workflow["organization_id"], key),
            ).fetchone()
            for index, action in enumerate(definition["actions"]):
                connection.execute(
                    """INSERT OR IGNORE INTO workflow_jobs(
                        organization_id,run_id,action_index,action_type,payload_json,idempotency_key,
                        status,available_at,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workflow["organization_id"], run["id"], index, action["type"],
                        json.dumps(action, sort_keys=True), f"{key}:action:{index}",
                        "waiting" if definition["delay_minutes"] else "queued", available,
                        utc_now(), utc_now(),
                    ),
                )

    def process_jobs(self, limit: int = 50) -> int:
        processed = 0
        for _ in range(limit):
            with self.db.connect() as connection:
                job = connection.execute(
                    """SELECT j.*,r.lead_id FROM workflow_jobs j JOIN workflow_runs r ON r.id=j.run_id
                       WHERE j.status IN ('queued','waiting','failed') AND j.available_at<=?
                         AND j.attempts<j.max_attempts ORDER BY j.id LIMIT 1""",
                    (utc_now(),),
                ).fetchone()
                if not job:
                    break
                claimed = connection.execute(
                    """UPDATE workflow_jobs SET status='processing',attempts=attempts+1,locked_at=?,updated_at=?
                       WHERE id=? AND status IN ('queued','waiting','failed')""",
                    (utc_now(), utc_now(), job["id"]),
                )
                if not claimed.rowcount:
                    continue
                connection.execute(
                    "UPDATE workflow_runs SET status='running',started_at=COALESCE(started_at,?) WHERE id=?",
                    (utc_now(), job["run_id"]),
                )
                job = dict(job)
            try:
                self._execute_job(job)
                processed += 1
            except Exception as exc:
                logging.exception("Workflow job %s failed", job["id"])
                attempts = int(job["attempts"] or 0) + 1
                status = "dead" if attempts >= int(job["max_attempts"]) else "failed"
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** attempts))).isoformat(timespec="seconds")
                with self.db.connect() as connection:
                    connection.execute(
                        """UPDATE workflow_jobs SET status=?,available_at=?,locked_at=NULL,last_error=?,updated_at=? WHERE id=?""",
                        (status, retry_at, str(exc)[:1000], utc_now(), job["id"]),
                    )
                    connection.execute(
                        "UPDATE workflow_runs SET status='failed',error=?,finished_at=? WHERE id=?",
                        (str(exc)[:1000], utc_now(), job["run_id"]),
                    )
        return processed

    def _execute_job(self, job: dict[str, Any]) -> None:
        action = json.loads(job["payload_json"])
        with self.db.connect() as connection:
            receipt = connection.execute(
                "SELECT * FROM workflow_action_receipts WHERE organization_id=? AND idempotency_key=?",
                (job["organization_id"], job["idempotency_key"]),
            ).fetchone()
            if receipt:
                entity_id = receipt["entity_id"]
            else:
                lead = connection.execute(
                    "SELECT * FROM leads WHERE id=? AND organization_id=?",
                    (job["lead_id"], job["organization_id"]),
                ).fetchone()
                if not lead:
                    raise ValueError("Workflow lead no longer exists")
                first_name = (lead["owner_name"] or "there").split()[0]
                values = _TemplateValues(
                    name=lead["owner_name"] or lead["phone_display"], first_name=first_name,
                    phone=lead["phone_display"], reference=lead["reference_number"] or "your property",
                    status=lead["status"],
                )
                if action["type"] == "create_task":
                    cursor = connection.execute(
                        """INSERT INTO tasks(organization_id,lead_id,title,due_at,status,source,created_at)
                           VALUES (?,?,?,?,'open','workflow',?)""",
                        (job["organization_id"], job["lead_id"], action["title"].format_map(values), utc_now(), utc_now()),
                    )
                    entity_type, entity_id = "task", cursor.lastrowid
                elif action["type"] == "prepare_sms_draft":
                    cursor = connection.execute(
                        """INSERT INTO message_drafts(
                            organization_id,lead_id,angle,message,provider,context_hash,created_at
                        ) VALUES (?,?,?,?, 'workflow',?,?)""",
                        (
                            job["organization_id"], job["lead_id"], action["angle"],
                            action["message"].format_map(values), job["idempotency_key"], utc_now(),
                        ),
                    )
                    entity_type, entity_id = "message_draft", cursor.lastrowid
                else:
                    raise ValueError("Action is not allowlisted")
                connection.execute(
                    """INSERT INTO workflow_action_receipts(
                        organization_id,job_id,idempotency_key,entity_type,entity_id,created_at
                    ) VALUES (?,?,?,?,?,?)""",
                    (job["organization_id"], job["id"], job["idempotency_key"], entity_type, entity_id, utc_now()),
                )
            connection.execute(
                """UPDATE workflow_jobs SET status='succeeded',locked_at=NULL,last_error='',updated_at=? WHERE id=?""",
                (utc_now(), job["id"]),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM workflow_jobs WHERE run_id=? AND status!='succeeded'",
                (job["run_id"],),
            ).fetchone()[0]
            if not remaining:
                connection.execute(
                    "UPDATE workflow_runs SET status='succeeded',error='',finished_at=? WHERE id=?",
                    (utc_now(), job["run_id"]),
                )


def run_worker(database_path: Path | str = Path("data/leads.db"), poll_seconds: float = 5.0, once: bool = False) -> None:
    engine = WorkflowEngine(database_path)
    engine.recover_stale_locks()
    while True:
        result = engine.process_once()
        if once:
            return
        if not result["events"] and not result["jobs"]:
            time.sleep(poll_seconds)
