"""SQLite persistence for the Vumber lead pipeline and local CRM."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

INSERT OR IGNORE INTO organizations(id, name, slug, created_at)
VALUES (1, 'Fuller Oak', 'fuller-oak', CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email TEXT NOT NULL COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','manager','agent','viewer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    UNIQUE(organization_id, email)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    full_name TEXT NOT NULL DEFAULT '',
    primary_phone_normalized TEXT NOT NULL DEFAULT '',
    primary_phone_display TEXT NOT NULL DEFAULT '',
    primary_email TEXT NOT NULL DEFAULT '',
    mailing_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, lead_id),
    UNIQUE(organization_id, primary_phone_normalized)
);

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    reference_number TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '',
    property_address TEXT NOT NULL DEFAULT '',
    county TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    acreage TEXT NOT NULL DEFAULT '',
    apn TEXT NOT NULL DEFAULT '',
    offer_price TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'crm',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_properties_org_reference
    ON properties(organization_id, reference_number) WHERE reference_number != '';

CREATE TABLE IF NOT EXISTS contact_properties (
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL DEFAULT 'owner',
    PRIMARY KEY(organization_id, contact_id, property_id)
);

CREATE TABLE IF NOT EXISTS custom_field_definitions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('contact','property')),
    field_key TEXT NOT NULL,
    label TEXT NOT NULL,
    data_type TEXT NOT NULL CHECK(data_type IN ('text','number','date','boolean','choice')),
    options_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, entity_type, field_key)
);

CREATE TABLE IF NOT EXISTS custom_field_values (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    definition_id INTEGER NOT NULL REFERENCES custom_field_definitions(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('contact','property')),
    entity_id INTEGER NOT NULL,
    value_json TEXT NOT NULL DEFAULT 'null',
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, definition_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#235d4d',
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS entity_tags (
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('contact','property')),
    entity_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(organization_id, tag_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS saved_filters (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('contact','property')),
    definition_json TEXT NOT NULL,
    shared INTEGER NOT NULL DEFAULT 0 CHECK(shared IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, user_id, entity_type, name)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'contact' CHECK(entity_type='contact'),
    status TEXT NOT NULL CHECK(status IN ('staged','validated','committed','failed')),
    duplicate_policy TEXT NOT NULL DEFAULT 'update' CHECK(duplicate_policy IN ('update','skip')),
    headers_json TEXT NOT NULL DEFAULT '[]',
    mapping_json TEXT NOT NULL DEFAULT '{}',
    total_rows INTEGER NOT NULL DEFAULT 0,
    valid_rows INTEGER NOT NULL DEFAULT 0,
    error_rows INTEGER NOT NULL DEFAULT 0,
    created_rows INTEGER NOT NULL DEFAULT 0,
    updated_rows INTEGER NOT NULL DEFAULT 0,
    skipped_rows INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS import_rows (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','valid','error','created','updated','skipped')),
    error TEXT NOT NULL DEFAULT '',
    entity_id INTEGER,
    UNIQUE(batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'acquisition' CHECK(kind IN ('acquisition','disposition','owner_finance','other')),
    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pipelines_org_default
    ON pipelines(organization_id) WHERE is_default=1;

CREATE TABLE IF NOT EXISTS pipeline_stages (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    color TEXT NOT NULL DEFAULT '#235d4d',
    outcome TEXT NOT NULL DEFAULT 'open' CHECK(outcome IN ('open','won','lost')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(pipeline_id, name),
    UNIQUE(pipeline_id, stage_key)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE RESTRICT,
    stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    estimated_value REAL,
    assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, lead_id)
);

CREATE TABLE IF NOT EXISTS stage_transitions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    from_stage_id INTEGER REFERENCES pipeline_stages(id) ON DELETE SET NULL,
    to_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id) ON DELETE RESTRICT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('call','sms','email','document','system','note')),
    direction TEXT NOT NULL DEFAULT 'internal' CHECK(direction IN ('incoming','outgoing','missed','internal','unknown')),
    occurred_at TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, source_table, source_id, event_type)
);

CREATE TRIGGER IF NOT EXISTS activity_events_immutable_update
BEFORE UPDATE ON activity_events BEGIN
    SELECT RAISE(ABORT, 'activity events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS activity_events_immutable_delete
BEFORE DELETE ON activity_events BEGIN
    SELECT RAISE(ABORT, 'activity events are immutable');
END;

CREATE TABLE IF NOT EXISTS workflow_definitions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    draft_json TEXT NOT NULL DEFAULT '{}',
    published_version INTEGER,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    is_template INTEGER NOT NULL DEFAULT 0 CHECK(is_template IN (0,1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workflow_id INTEGER NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(workflow_id, version)
);

CREATE TABLE IF NOT EXISTS workflow_outbox (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','delivered','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(organization_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workflow_id INTEGER NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    workflow_version INTEGER NOT NULL,
    trigger_topic TEXT NOT NULL,
    trigger_event_id INTEGER,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','waiting','running','succeeded','failed')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(organization_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    action_index INTEGER NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('create_task','prepare_sms_draft')),
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','waiting','processing','succeeded','failed','dead')),
    available_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_action_receipts (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_worker_state (
    worker_name TEXT PRIMARY KEY,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS document_templates (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    draft_body TEXT NOT NULL,
    published_version INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS document_template_versions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    template_id INTEGER NOT NULL REFERENCES document_templates(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    merge_fields_json TEXT NOT NULL DEFAULT '[]',
    published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(template_id, version)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    template_id INTEGER REFERENCES document_templates(id) ON DELETE SET NULL,
    template_version INTEGER,
    title TEXT NOT NULL,
    recipient_name TEXT NOT NULL DEFAULT '',
    recipient_email TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','generated','sent','delivered','viewed','signed','declined','expired','completed')),
    provider TEXT NOT NULL DEFAULT 'manual',
    provider_envelope_id TEXT NOT NULL DEFAULT '',
    merge_data_json TEXT NOT NULL DEFAULT '{}',
    rendered_body TEXT NOT NULL DEFAULT '',
    artifact_path TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS document_files (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('generated','signed','attachment')),
    file_path TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_events (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK(event_type IN ('created','generated','sent','delivered','viewed','signed','declined','expired','completed','file_uploaded')),
    actor_name TEXT NOT NULL DEFAULT '',
    actor_email TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'manual',
    provider_event_id TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS document_events_immutable_update
BEFORE UPDATE ON document_events BEGIN
    SELECT RAISE(ABORT, 'document events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS document_events_immutable_delete
BEFORE DELETE ON document_events BEGIN
    SELECT RAISE(ABORT, 'document events are immutable');
END;

CREATE TABLE IF NOT EXISTS ai_knowledge_sources (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_capture_proposals (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    call_id INTEGER REFERENCES calls(id) ON DELETE SET NULL,
    proposal_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','rejected')),
    reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS phone_numbers (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'vumber',
    value TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'sms' CHECK(kind IN ('sms','call','both')),
    assignable_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, provider, value)
);

CREATE TABLE IF NOT EXISTS consent_suppressions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('sms','call','email','all')),
    reason TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_events (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    communication_id INTEGER REFERENCES communications(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'vumber',
    channel TEXT NOT NULL CHECK(channel IN ('sms','call','email')),
    provider_message_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN ('queued','sent','delivered','failed','undelivered')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, provider, provider_message_id, state)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','on_hold','completed','cancelled')),
    template_key TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS project_tasks (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_task_id INTEGER REFERENCES project_tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','in_progress','blocked','done')),
    position INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,
    assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_comments (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES project_tasks(id) ON DELETE CASCADE,
    author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendars (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'appointments' CHECK(kind IN ('appointments','tasks','personal')),
    color TEXT NOT NULL DEFAULT '#235d4d',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS calendar_availability (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    start_minute INTEGER NOT NULL CHECK(start_minute BETWEEN 0 AND 1439),
    end_minute INTEGER NOT NULL CHECK(end_minute BETWEEN 1 AND 1440),
    CHECK(end_minute > start_minute),
    UNIQUE(calendar_id, weekday, start_minute)
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','completed','cancelled','no_show')),
    booked_by TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS esign_webhook_events (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    envelope_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    signature_valid INTEGER NOT NULL CHECK(signature_valid IN (0,1)),
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted','ignored','failed')),
    error TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    UNIQUE(organization_id, provider, provider_event_id)
);

CREATE TABLE IF NOT EXISTS ai_agents (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    agent_type TEXT NOT NULL CHECK(agent_type IN ('workflow_builder','conversation_capture')),
    description TEXT NOT NULL DEFAULT '',
    draft_instructions TEXT NOT NULL,
    tool_allowlist_json TEXT NOT NULL DEFAULT '[]',
    disclosure_text TEXT NOT NULL DEFAULT '',
    handoff_rules_json TEXT NOT NULL DEFAULT '{}',
    published_version INTEGER,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS ai_agent_versions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_id INTEGER NOT NULL REFERENCES ai_agents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    instructions TEXT NOT NULL,
    tool_allowlist_json TEXT NOT NULL,
    disclosure_text TEXT NOT NULL DEFAULT '',
    handoff_rules_json TEXT NOT NULL DEFAULT '{}',
    published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agent_id, version)
);

CREATE TABLE IF NOT EXISTS ai_knowledge_bases (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    draft_content TEXT NOT NULL DEFAULT '',
    published_version INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS ai_knowledge_versions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    knowledge_base_id INTEGER NOT NULL REFERENCES ai_knowledge_bases(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(knowledge_base_id, version)
);

CREATE TABLE IF NOT EXISTS ai_workflow_proposals (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_id INTEGER REFERENCES ai_agents(id) ON DELETE SET NULL,
    agent_version INTEGER,
    prompt TEXT NOT NULL,
    proposed_json TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    workflow_id INTEGER REFERENCES workflow_definitions(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_capture_proposals (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_id INTEGER REFERENCES ai_agents(id) ON DELETE SET NULL,
    agent_version INTEGER,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    call_id INTEGER REFERENCES calls(id) ON DELETE SET NULL,
    proposed_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    provider TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(organization_id, call_id, agent_version)
);

CREATE TABLE IF NOT EXISTS ai_handoffs (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    capture_proposal_id INTEGER REFERENCES ai_capture_proposals(id) ON DELETE SET NULL,
    reason TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('normal','high','urgent')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_evaluation_cases (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_type TEXT NOT NULL CHECK(agent_type IN ('workflow_builder','conversation_capture')),
    name TEXT NOT NULL,
    input_json TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, agent_type, name)
);

CREATE TABLE IF NOT EXISTS ai_evaluation_results (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id INTEGER NOT NULL REFERENCES ai_evaluation_cases(id) ON DELETE CASCADE,
    agent_id INTEGER REFERENCES ai_agents(id) ON DELETE SET NULL,
    agent_version INTEGER,
    passed INTEGER NOT NULL CHECK(passed IN (0,1)),
    actual_json TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_preferences (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    widgets_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, user_id)
);

CREATE TABLE IF NOT EXISTS saved_reports (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    report_type TEXT NOT NULL CHECK(report_type IN ('pipeline','activity','sources','follow_up')),
    config_json TEXT NOT NULL DEFAULT '{}',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS help_articles (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Getting started',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, slug)
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','resolved','closed')),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('low','normal','high','urgent')),
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolution TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS public_forms (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    intro_text TEXT NOT NULL DEFAULT '',
    fields_json TEXT NOT NULL DEFAULT '[]',
    success_text TEXT NOT NULL DEFAULT 'Thank you. Your information was received.',
    lead_source TEXT NOT NULL DEFAULT 'Public form',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS form_submissions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    form_id INTEGER NOT NULL REFERENCES public_forms(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
    payload_json TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL UNIQUE,
    secret_sha256 TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    expires_at TEXT,
    last_used_at TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS api_idempotency (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    api_key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    resource_type TEXT NOT NULL DEFAULT '',
    resource_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(api_key_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS inbound_webhook_endpoints (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    secret_encrypted BLOB NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS inbound_webhook_deliveries (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    endpoint_id INTEGER NOT NULL REFERENCES inbound_webhook_endpoints(id) ON DELETE CASCADE,
    external_event_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    signature_valid INTEGER NOT NULL CHECK(signature_valid IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('accepted','duplicate','rejected','failed')),
    response_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL,
    UNIQUE(endpoint_id, external_event_id)
);

CREATE TABLE IF NOT EXISTS api_request_log (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    request_fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS activity_events_workflow_outbox
AFTER INSERT ON activity_events BEGIN
    INSERT OR IGNORE INTO workflow_outbox(
        organization_id,topic,payload_json,idempotency_key,status,available_at,created_at
    ) VALUES (
        NEW.organization_id,'activity.created',
        json_object('activity_id',NEW.id,'lead_id',NEW.lead_id,'event_type',NEW.event_type,
                    'channel',NEW.channel,'direction',NEW.direction,'opportunity_id',NEW.opportunity_id),
        'activity:' || NEW.id,'pending',NEW.created_at,NEW.created_at
    );
END;

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    phone_normalized TEXT NOT NULL,
    phone_display TEXT NOT NULL,
    owner_name TEXT NOT NULL DEFAULT '',
    reference_number TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    follow_up_plan TEXT NOT NULL DEFAULT '',
    follow_up_score INTEGER NOT NULL DEFAULT 1 CHECK(follow_up_score BETWEEN 1 AND 5),
    reasoning TEXT NOT NULL DEFAULT '',
    distress_signal TEXT NOT NULL DEFAULT '',
    seller_motivation TEXT NOT NULL DEFAULT '',
    price_expectation TEXT NOT NULL DEFAULT '',
    deal_style TEXT NOT NULL DEFAULT '',
    lead_source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new','contacted','qualified','nurture','won','lost')),
    next_follow_up TEXT,
    notes TEXT NOT NULL DEFAULT '',
    first_activity TEXT,
    latest_activity TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, phone_normalized)
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL UNIQUE,
    direction TEXT NOT NULL DEFAULT 'unknown',
    vumber_number TEXT NOT NULL DEFAULT '',
    vumber_label TEXT NOT NULL DEFAULT '',
    occurred_at TEXT,
    time_description TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL DEFAULT '',
    attachment_path TEXT NOT NULL DEFAULT '',
    call_reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    status TEXT NOT NULL CHECK(status IN ('processing','complete','failed')),
    error TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
    source TEXT NOT NULL DEFAULT 'ai',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS communications (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('sms','call')),
    direction TEXT NOT NULL DEFAULT 'outgoing',
    from_number TEXT NOT NULL DEFAULT '',
    to_number TEXT NOT NULL DEFAULT '',
    callback_number TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'submitted',
    response TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcription_jobs (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    audio_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing','complete','failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(organization_id, call_id, provider, model, audio_sha256)
);

CREATE TABLE IF NOT EXISTS message_drafts (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    angle TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'local',
    context_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    messages_seen INTEGER NOT NULL DEFAULT 0,
    messages_processed INTEGER NOT NULL DEFAULT 0,
    calls_added INTEGER NOT NULL DEFAULT 0,
    leads_updated INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_calls_lead_time ON calls(lead_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_communications_lead_time ON communications(lead_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_status
    ON transcription_jobs(status, provider, model);
CREATE INDEX IF NOT EXISTS idx_message_drafts_lead ON message_drafts(lead_id, id);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(follow_up_score DESC, latest_activity DESC);
CREATE INDEX IF NOT EXISTS idx_audit_org_time ON audit_events(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_org_name ON contacts(organization_id, full_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_properties_org_lead ON properties(organization_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_entity_tags_lookup ON entity_tags(organization_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_import_rows_batch ON import_rows(batch_id, row_number);
CREATE INDEX IF NOT EXISTS idx_pipeline_stages_order ON pipeline_stages(pipeline_id, position, id);
CREATE INDEX IF NOT EXISTS idx_opportunities_board ON opportunities(organization_id, pipeline_id, stage_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_contact ON opportunities(organization_id, contact_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_property ON opportunities(organization_id, property_id);
CREATE INDEX IF NOT EXISTS idx_stage_transitions_history ON stage_transitions(opportunity_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_activity_org_time ON activity_events(organization_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_activity_lead_time ON activity_events(organization_id, lead_id, occurred_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_activity_channel_time ON activity_events(organization_id, channel, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_outbox_ready ON workflow_outbox(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_ready ON workflow_jobs(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_time ON workflow_runs(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_templates_org ON document_templates(organization_id, active, name);
CREATE INDEX IF NOT EXISTS idx_documents_org_status ON documents(organization_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_opportunity ON documents(organization_id, opportunity_id);
CREATE INDEX IF NOT EXISTS idx_document_events_history ON document_events(document_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_calendars_org ON calendars(organization_id, active, name);
CREATE INDEX IF NOT EXISTS idx_projects_org_status ON projects(organization_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_tasks_project ON project_tasks(project_id, status, position);
CREATE INDEX IF NOT EXISTS idx_project_comments_project ON project_comments(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_appointments_org_start ON appointments(organization_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_org ON phone_numbers(organization_id, provider, active);
CREATE INDEX IF NOT EXISTS idx_consent_lead ON consent_suppressions(organization_id, lead_id, channel, active);
CREATE INDEX IF NOT EXISTS idx_knowledge_org ON ai_knowledge_sources(organization_id, active, name);
CREATE INDEX IF NOT EXISTS idx_capture_lead ON ai_capture_proposals(organization_id, lead_id, status);
CREATE INDEX IF NOT EXISTS idx_delivery_comm ON delivery_events(organization_id, communication_id);
CREATE INDEX IF NOT EXISTS idx_appointments_calendar ON appointments(calendar_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_appointments_lead ON appointments(organization_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_availability_calendar ON calendar_availability(calendar_id, weekday);
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_events_provider_unique
    ON document_events(organization_id, provider, provider_event_id)
    WHERE provider_event_id != '';
CREATE INDEX IF NOT EXISTS idx_ai_capture_review ON ai_capture_proposals(organization_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_workflow_review ON ai_workflow_proposals(organization_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_handoffs_open ON ai_handoffs(organization_id, status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_support_tickets_org ON support_tickets(organization_id, status, priority, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_form_submissions_form ON form_submissions(organization_id, form_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_requests_key_time ON api_request_log(api_key_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint ON inbound_webhook_deliveries(endpoint_id, received_at DESC);

CREATE TRIGGER IF NOT EXISTS leads_workflow_outbox
AFTER INSERT ON leads BEGIN
    INSERT OR IGNORE INTO workflow_outbox(
        organization_id,topic,payload_json,idempotency_key,status,available_at,created_at
    ) VALUES (
        NEW.organization_id,'lead.created',json_object('lead_id',NEW.id),
        'lead:' || NEW.id,'pending',NEW.created_at,NEW.created_at
    );
END;

CREATE TABLE IF NOT EXISTS land_contacts (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('seller','buyer')),
    dedupe_key TEXT NOT NULL,
    external_match_id TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    mailing_address TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    raw_source_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, role, dedupe_key)
);

CREATE TABLE IF NOT EXISTS land_contact_phones (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES land_contacts(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 6),
    phone_normalized TEXT NOT NULL,
    phone_display TEXT NOT NULL,
    phone_type TEXT NOT NULL DEFAULT '',
    dnc INTEGER NOT NULL DEFAULT 0 CHECK(dnc IN (0,1)),
    state_dnc INTEGER NOT NULL DEFAULT 0 CHECK(state_dnc IN (0,1)),
    litigator INTEGER NOT NULL DEFAULT 0 CHECK(litigator IN (0,1)),
    callable INTEGER NOT NULL DEFAULT 0 CHECK(callable IN (0,1)),
    textable INTEGER NOT NULL DEFAULT 0 CHECK(textable IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(contact_id, slot)
);

CREATE TABLE IF NOT EXISTS land_deals (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE RESTRICT,
    stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id) ON DELETE RESTRICT,
    seller_contact_id INTEGER NOT NULL REFERENCES land_contacts(id) ON DELETE RESTRICT,
    buyer_contact_id INTEGER NOT NULL REFERENCES land_contacts(id) ON DELETE RESTRICT,
    deal_rank INTEGER,
    deal_score REAL,
    county TEXT NOT NULL DEFAULT '',
    target_apn TEXT NOT NULL DEFAULT '',
    target_acreage REAL,
    target_parcel_address TEXT NOT NULL DEFAULT '',
    seller_purchase_date TEXT NOT NULL DEFAULT '',
    seller_purchase_status TEXT NOT NULL DEFAULT '',
    buyer_purchase_date TEXT NOT NULL DEFAULT '',
    shared_boundary_distance REAL,
    sale_reference_value REAL,
    illustrative_acquisition_price REAL,
    illustrative_resale_price REAL,
    illustrative_costs REAL,
    illustrative_margin REAL,
    review_flags TEXT NOT NULL DEFAULT '',
    parcel_link TEXT NOT NULL DEFAULT '',
    map_link TEXT NOT NULL DEFAULT '',
    buyer_mo_parcel_count INTEGER,
    buyer_adjoining_count INTEGER,
    eligible_buyer_identities INTEGER,
    seller_outreach_status TEXT NOT NULL DEFAULT 'Not contacted',
    buyer_outreach_status TEXT NOT NULL DEFAULT 'Not contacted',
    seller_asking_price REAL,
    proposed_acquisition_price REAL,
    buyer_indicated_price REAL,
    estimated_closing_funding_costs REAL,
    expected_spread REAL,
    seller_motivation TEXT NOT NULL DEFAULT '',
    seller_response TEXT NOT NULL DEFAULT '',
    buyer_response TEXT NOT NULL DEFAULT '',
    next_follow_up_date TEXT,
    assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    source_fields_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, external_id)
);

CREATE TABLE IF NOT EXISTS land_deal_stage_history (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    deal_id INTEGER NOT NULL REFERENCES land_deals(id) ON DELETE CASCADE,
    from_stage_id INTEGER REFERENCES pipeline_stages(id) ON DELETE SET NULL,
    to_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id) ON DELETE RESTRICT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS land_outreach_tasks (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    deal_id INTEGER NOT NULL REFERENCES land_deals(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK(side IN ('seller','buyer')),
    task_type TEXT NOT NULL CHECK(task_type IN ('cold_text_review','dnc_review')),
    phone_id INTEGER REFERENCES land_contact_phones(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    draft_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
    due_date TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(deal_id, side, task_type)
);

CREATE TABLE IF NOT EXISTS land_import_runs (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    deals_created INTEGER NOT NULL DEFAULT 0,
    deals_updated INTEGER NOT NULL DEFAULT 0,
    unique_sellers INTEGER NOT NULL DEFAULT 0,
    unique_buyers INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS land_sms_permissions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    phone_id INTEGER NOT NULL REFERENCES land_contact_phones(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('unknown','express_written_consent','opted_out','do_not_contact')),
    consent_scope TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    recorded_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, phone_id)
);

CREATE TABLE IF NOT EXISTS land_sms_campaigns (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    audience_key TEXT NOT NULL DEFAULT 'seller_textable',
    message_template TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'blocked' CHECK(status IN ('draft','blocked','ready','paused','completed')),
    automated_sending INTEGER NOT NULL DEFAULT 0 CHECK(automated_sending IN (0,1)),
    requires_written_consent INTEGER NOT NULL DEFAULT 1 CHECK(requires_written_consent IN (0,1)),
    business_identity TEXT NOT NULL DEFAULT '',
    legal_review_name TEXT NOT NULL DEFAULT '',
    legal_review_date TEXT,
    legal_review_notes TEXT NOT NULL DEFAULT '',
    drip_rate_per_hour INTEGER NOT NULL DEFAULT 10,
    send_window_start TEXT NOT NULL DEFAULT '10:00',
    send_window_end TEXT NOT NULL DEFAULT '16:00',
    send_timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    weekdays_only INTEGER NOT NULL DEFAULT 1 CHECK(weekdays_only IN (0,1)),
    provider TEXT NOT NULL DEFAULT 'vumber',
    sending_number TEXT NOT NULL DEFAULT '',
    provider_connected INTEGER NOT NULL DEFAULT 0 CHECK(provider_connected IN (0,1)),
    launch_state TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, name)
);

CREATE TABLE IF NOT EXISTS land_sms_campaign_members (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES land_sms_campaigns(id) ON DELETE CASCADE,
    deal_id INTEGER NOT NULL REFERENCES land_deals(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES land_contacts(id) ON DELETE CASCADE,
    phone_id INTEGER NOT NULL REFERENCES land_contact_phones(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('blocked_no_consent','ready','suppressed','duplicate')),
    eligibility_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, contact_id)
);

CREATE TABLE IF NOT EXISTS land_sms_campaign_variants (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES land_sms_campaigns(id) ON DELETE CASCADE,
    variant_key TEXT NOT NULL,
    message_template TEXT NOT NULL DEFAULT '',
    weight INTEGER NOT NULL DEFAULT 25 CHECK(weight BETWEEN 0 AND 100),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, variant_key)
);

CREATE TABLE IF NOT EXISTS land_sms_campaign_variant_assignments (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES land_sms_campaigns(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES land_contacts(id) ON DELETE CASCADE,
    variant_id INTEGER NOT NULL REFERENCES land_sms_campaign_variants(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, contact_id)
);

CREATE TABLE IF NOT EXISTS land_sms_campaign_queue (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES land_sms_campaigns(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES land_sms_campaign_members(id) ON DELETE CASCADE,
    variant_id INTEGER NOT NULL REFERENCES land_sms_campaign_variants(id) ON DELETE CASCADE,
    scheduled_for_utc TEXT NOT NULL,
    scheduled_for_local TEXT NOT NULL,
    rendered_message TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'vumber',
    sending_number TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','held','sent','suppressed','failed')),
    provider_message_id TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, member_id)
);

CREATE TABLE IF NOT EXISTS land_sms_campaign_exclusions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES land_sms_campaigns(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES land_contacts(id) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, contact_id)
);

CREATE TABLE IF NOT EXISTS land_sms_campaign_compliance (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES land_sms_campaigns(id) ON DELETE CASCADE,
    federal_dnc_scrub_attested INTEGER NOT NULL DEFAULT 0 CHECK(federal_dnc_scrub_attested IN (0,1)),
    missouri_dnc_scrub_attested INTEGER NOT NULL DEFAULT 0 CHECK(missouri_dnc_scrub_attested IN (0,1)),
    attested_by TEXT NOT NULL DEFAULT '',
    attested_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_land_deals_rank ON land_deals(organization_id, deal_rank);
CREATE INDEX IF NOT EXISTS idx_land_deals_stage ON land_deals(organization_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_land_deals_followup ON land_deals(organization_id, next_follow_up_date);
CREATE INDEX IF NOT EXISTS idx_land_phones_eligibility ON land_contact_phones(organization_id, callable, textable);
CREATE INDEX IF NOT EXISTS idx_land_sms_permissions_status ON land_sms_permissions(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_land_sms_campaign_members_status ON land_sms_campaign_members(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_land_sms_campaign_variants_active ON land_sms_campaign_variants(campaign_id, active);
CREATE INDEX IF NOT EXISTS idx_land_sms_campaign_variant_assignments ON land_sms_campaign_variant_assignments(campaign_id, variant_id);
CREATE INDEX IF NOT EXISTS idx_land_sms_campaign_queue_due ON land_sms_campaign_queue(campaign_id, status, scheduled_for_utc);
CREATE INDEX IF NOT EXISTS idx_land_sms_campaign_exclusions ON land_sms_campaign_exclusions(campaign_id, contact_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CRMDatabase:
    def __init__(self, path: Path | str = Path("data/leads.db")) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            existing_tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            phase2_applied = False
            phase3_applied = False
            phase4_applied = False
            phase5_applied = False
            phase6_applied = False
            phase10_applied = False
            phase11_applied = False
            phase12_applied = False
            phase7_applied = False
            phase8_applied = False
            if "schema_migrations" in existing_tables:
                phase2_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='002_contacts_properties_imports'"
                ).fetchone())
                phase3_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='003_opportunities_pipelines'"
                ).fetchone())
                phase4_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='004_unified_activity_inbox'"
                ).fetchone())
                phase5_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='005_workflow_automation'"
                ).fetchone())
                phase6_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='006_contracts_esign'"
                ).fetchone())
                phase10_agents_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='010_ai_builders_agents'"
                ).fetchone())
                phase7_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='007_calendar_scheduling'"
                ).fetchone())
                phase8_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='008_project_management'"
                ).fetchone())
                phase9_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='009_communication_providers'"
                ).fetchone())
                phase10_legacy_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='010_ai_builders'"
                ).fetchone())
                phase10_applied = phase10_agents_applied
                phase11_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='011_dashboards_help_forms_pwa'"
                ).fetchone())
                phase12_applied = bool(connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version='012_integration_api_webhooks'"
                ).fetchone())
            if "leads" in existing_tables and not phase2_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase2{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase3_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase3{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase4_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase4{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase5_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase5{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase6_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase6{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase10_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase10{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase11_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase11{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase12_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase12{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase7_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase7{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase8_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase8{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase9_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase9{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            if "leads" in existing_tables and not phase10_applied:
                backup = self.path.with_name(f"{self.path.stem}.before-phase10{self.path.suffix}")
                if not backup.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            connection.executescript(SCHEMA)
            communication_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(communications)")
            }
            if "provider_message_id" not in communication_columns:
                connection.execute(
                    "ALTER TABLE communications ADD COLUMN provider_message_id TEXT NOT NULL DEFAULT ''"
                )
            campaign_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(land_sms_campaigns)")
            }
            campaign_additions = {
                "drip_rate_per_hour": "INTEGER NOT NULL DEFAULT 10",
                "send_window_start": "TEXT NOT NULL DEFAULT '10:00'",
                "send_window_end": "TEXT NOT NULL DEFAULT '16:00'",
                "send_timezone": "TEXT NOT NULL DEFAULT 'America/Chicago'",
                "weekdays_only": "INTEGER NOT NULL DEFAULT 1",
                "provider": "TEXT NOT NULL DEFAULT 'vumber'",
                "sending_number": "TEXT NOT NULL DEFAULT ''",
                "provider_connected": "INTEGER NOT NULL DEFAULT 0",
                "launch_state": "TEXT NOT NULL DEFAULT 'draft'",
            }
            for column_name, definition in campaign_additions.items():
                if column_name not in campaign_columns:
                    connection.execute(
                        f"ALTER TABLE land_sms_campaigns ADD COLUMN {column_name} {definition}"
                    )
            connection.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_communications_provider_message
                    ON communications(organization_id, kind, provider_message_id)
                    WHERE provider_message_id != '';
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES ('013_vumber_sms_openrouter_transcription', CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES ('014_missouri_land_double_close', CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES ('015_land_sms_drip_queue', CURRENT_TIMESTAMP);
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(leads)")}
            for name in (
                "distress_signal", "seller_motivation", "price_expectation",
                "deal_style", "lead_source",
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE leads ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            tenant_tables = (
                "leads", "calls", "processed_messages", "tasks", "communications",
                "message_drafts", "sync_runs",
            )
            needs_tenant_migration = any(
                "organization_id" not in {
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for table in tenant_tables
            )
            if needs_tenant_migration:
                backup = self.path.with_name(f"{self.path.stem}.before-tenancy{self.path.suffix}")
                if not backup.exists() and self.path.exists():
                    destination = sqlite3.connect(backup)
                    try:
                        connection.backup(destination)
                    finally:
                        destination.close()
            for table in tenant_tables:
                table_columns = {
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "organization_id" not in table_columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN organization_id INTEGER NOT NULL DEFAULT 1"
                    )
            self._rebuild_leads_unique_for_tenants(connection)
            connection.executescript("""
                CREATE INDEX IF NOT EXISTS idx_leads_org_activity
                    ON leads(organization_id, latest_activity DESC);
                CREATE INDEX IF NOT EXISTS idx_calls_org_lead
                    ON calls(organization_id, lead_id);
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES ('001_tenant_auth_audit', CURRENT_TIMESTAMP);
            """)
            connection.execute(
                """INSERT OR IGNORE INTO contacts(
                    organization_id,lead_id,full_name,primary_phone_normalized,
                    primary_phone_display,created_at,updated_at
                ) SELECT organization_id,id,owner_name,phone_normalized,phone_display,created_at,updated_at
                  FROM leads"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO properties(
                    organization_id,lead_id,reference_number,owner_name,source,created_at,updated_at
                ) SELECT organization_id,id,reference_number,owner_name,'lead',created_at,updated_at
                  FROM leads WHERE reference_number != ''"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO contact_properties(organization_id,contact_id,property_id,relationship)
                   SELECT c.organization_id,c.id,p.id,'owner'
                   FROM contacts c JOIN properties p
                     ON p.organization_id=c.organization_id AND p.lead_id=c.lead_id"""
            )
            multi_reference_leads = connection.execute(
                """SELECT id,organization_id,reference_number,owner_name,created_at,updated_at
                   FROM leads WHERE reference_number LIKE '%,%'
                      OR reference_number LIKE '%;%'
                      OR reference_number LIKE '%' || char(10) || '%'"""
            ).fetchall()
            for lead in multi_reference_leads:
                references = []
                for part in re.split(r"[,;\n]+", lead["reference_number"]):
                    reference = part.strip().upper()
                    if reference and reference not in references:
                        references.append(reference)
                if len(references) < 2:
                    continue
                connection.execute(
                    """DELETE FROM properties WHERE organization_id=? AND lead_id=?
                       AND reference_number=? AND source='lead'""",
                    (lead["organization_id"], lead["id"], lead["reference_number"]),
                )
                contact = connection.execute(
                    "SELECT id FROM contacts WHERE organization_id=? AND lead_id=?",
                    (lead["organization_id"], lead["id"]),
                ).fetchone()
                for reference in references:
                    connection.execute(
                        """INSERT OR IGNORE INTO properties(
                            organization_id,lead_id,reference_number,owner_name,source,created_at,updated_at
                        ) VALUES (?,?,?,?, 'lead',?,?)""",
                        (
                            lead["organization_id"], lead["id"], reference,
                            lead["owner_name"], lead["created_at"], lead["updated_at"],
                        ),
                    )
                    property_row = connection.execute(
                        "SELECT id FROM properties WHERE organization_id=? AND reference_number=?",
                        (lead["organization_id"], reference),
                    ).fetchone()
                    if contact and property_row:
                        connection.execute(
                            "INSERT OR IGNORE INTO contact_properties VALUES (?,?,?,'owner')",
                            (lead["organization_id"], contact["id"], property_row["id"]),
                        )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('002_contacts_properties_imports', CURRENT_TIMESTAMP)"""
            )
            for organization in connection.execute("SELECT id FROM organizations").fetchall():
                self._ensure_phase3_for_org(connection, int(organization["id"]))
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('003_opportunities_pipelines', CURRENT_TIMESTAMP)"""
            )
            self._backfill_phase4_activity(connection)
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('004_unified_activity_inbox', CURRENT_TIMESTAMP)"""
            )
            for organization in connection.execute("SELECT id FROM organizations").fetchall():
                self._seed_phase5_templates(connection, int(organization["id"]))
            if not phase5_applied:
                connection.execute(
                    """UPDATE workflow_outbox SET status='delivered',delivered_at=?,last_error='Historical migration event'
                       WHERE status='pending'""",
                    (utc_now(),),
                )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('005_workflow_automation', CURRENT_TIMESTAMP)"""
            )
            for organization in connection.execute("SELECT id FROM organizations").fetchall():
                self._seed_phase6_templates(connection, int(organization["id"]))
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('006_contracts_esign', CURRENT_TIMESTAMP)"""
            )
            for organization in connection.execute("SELECT id FROM organizations").fetchall():
                self._seed_phase10_ai(connection, int(organization["id"]))
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('010_ai_builders_agents', CURRENT_TIMESTAMP)"""
            )
            for organization in connection.execute("SELECT id FROM organizations").fetchall():
                self._seed_phase11(connection, int(organization["id"]))
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('011_dashboards_help_forms_pwa', CURRENT_TIMESTAMP)"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('012_integration_api_webhooks', CURRENT_TIMESTAMP)"""
            )
            for organization in connection.execute("SELECT id FROM organizations").fetchall():
                self._seed_phase7_calendars(connection, int(organization["id"]))
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('007_calendar_scheduling', CURRENT_TIMESTAMP)"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('008_project_management', CURRENT_TIMESTAMP)"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('009_communication_providers', CURRENT_TIMESTAMP)"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                   VALUES ('010_ai_builders', CURRENT_TIMESTAMP)"""
            )

    @staticmethod
    def _ensure_phase3_for_org(connection: sqlite3.Connection, organization_id: int) -> int:
        """Seed the default acquisition pipeline and one compatible deal per lead."""
        now = utc_now()
        pipeline = connection.execute(
            "SELECT id FROM pipelines WHERE organization_id=? AND is_default=1",
            (organization_id,),
        ).fetchone()
        if pipeline:
            pipeline_id = int(pipeline["id"])
        else:
            connection.execute(
                """INSERT OR IGNORE INTO pipelines(
                    organization_id,name,kind,is_default,active,created_at,updated_at
                ) VALUES (?,'Acquisitions','acquisition',1,1,?,?)""",
                (organization_id, now, now),
            )
            pipeline = connection.execute(
                "SELECT id FROM pipelines WHERE organization_id=? AND is_default=1",
                (organization_id,),
            ).fetchone()
            pipeline_id = int(pipeline["id"])
        defaults = (
            ("New", "new", 10, "#54778c", "open"),
            ("Contacted", "contacted", 20, "#7a6a9b", "open"),
            ("Qualified", "qualified", 30, "#235d4d", "open"),
            ("Nurture", "nurture", 40, "#b4813d", "open"),
            ("Won", "won", 50, "#247047", "won"),
            ("Lost", "lost", 60, "#9b4b45", "lost"),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO pipeline_stages(
                organization_id,pipeline_id,name,stage_key,position,color,outcome,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (organization_id, pipeline_id, name, key, position, color, outcome, now, now)
                for name, key, position, color, outcome in defaults
            ],
        )
        connection.execute(
            """INSERT OR IGNORE INTO opportunities(
                organization_id,lead_id,contact_id,property_id,pipeline_id,stage_id,
                title,estimated_value,created_at,updated_at
            )
            SELECT l.organization_id,l.id,c.id,
                (SELECT cp.property_id FROM contact_properties cp
                 WHERE cp.organization_id=l.organization_id AND cp.contact_id=c.id
                 ORDER BY cp.property_id LIMIT 1),
                ?,s.id,
                CASE WHEN l.owner_name != '' THEN l.owner_name ELSE l.phone_display END,
                NULL,l.created_at,l.updated_at
            FROM leads l
            LEFT JOIN contacts c ON c.organization_id=l.organization_id AND c.lead_id=l.id
            JOIN pipeline_stages s ON s.pipeline_id=? AND s.stage_key=l.status
            WHERE l.organization_id=?""",
            (pipeline_id, pipeline_id, organization_id),
        )
        return pipeline_id

    @staticmethod
    def _backfill_phase4_activity(connection: sqlite3.Connection) -> None:
        """Build the immutable event projection without changing source records."""
        connection.execute(
            """INSERT OR IGNORE INTO activity_events(
                organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                occurred_at,title,body,source_table,source_id,metadata_json,created_at
            )
            SELECT c.organization_id,c.lead_id,ct.id,o.id,'call','call',
                CASE WHEN c.direction IN ('incoming','outgoing','missed') THEN c.direction ELSE 'unknown' END,
                COALESCE(c.occurred_at,c.created_at),
                CASE c.direction WHEN 'incoming' THEN 'Incoming call' WHEN 'outgoing' THEN 'Outgoing call'
                     WHEN 'missed' THEN 'Missed call' ELSE 'Call' END,
                c.transcript,'calls',c.id,
                json_object('recording',c.attachment_path,'reference',c.call_reference,
                            'vumber_number',c.vumber_number,'time_description',c.time_description),
                c.created_at
            FROM calls c
            LEFT JOIN contacts ct ON ct.organization_id=c.organization_id AND ct.lead_id=c.lead_id
            LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO activity_events(
                organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                occurred_at,title,body,source_table,source_id,metadata_json,created_at
            )
            SELECT m.organization_id,m.lead_id,ct.id,o.id,
                CASE WHEN m.kind='sms' THEN 'sms' ELSE 'crm_call' END,m.kind,'outgoing',
                m.created_at,CASE WHEN m.kind='sms' THEN 'Text sent' ELSE 'CRM call started' END,
                m.message,'communications',m.id,
                json_object('from',m.from_number,'to',m.to_number,'callback',m.callback_number,'status',m.status),
                m.created_at
            FROM communications m
            LEFT JOIN contacts ct ON ct.organization_id=m.organization_id AND ct.lead_id=m.lead_id
            LEFT JOIN opportunities o ON o.organization_id=m.organization_id AND o.lead_id=m.lead_id"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO activity_events(
                organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                occurred_at,title,body,source_table,source_id,metadata_json,created_at
            )
            SELECT t.organization_id,o.lead_id,o.contact_id,o.id,'stage_change','system','internal',
                t.created_at,'Stage changed: ' || COALESCE(fs.name,'Created') || ' → ' || ts.name,
                t.note,'stage_transitions',t.id,
                json_object('from_stage_id',t.from_stage_id,'to_stage_id',t.to_stage_id,'user_id',t.user_id),
                t.created_at
            FROM stage_transitions t JOIN opportunities o ON o.id=t.opportunity_id
            LEFT JOIN pipeline_stages fs ON fs.id=t.from_stage_id
            JOIN pipeline_stages ts ON ts.id=t.to_stage_id"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO activity_events(
                organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                occurred_at,title,body,source_table,source_id,metadata_json,created_at
            )
            SELECT a.organization_id,
                CASE a.entity_type
                    WHEN 'lead' THEN CAST(NULLIF(a.entity_id,'') AS INTEGER)
                    WHEN 'opportunity' THEN (SELECT lead_id FROM opportunities WHERE id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                    WHEN 'contact' THEN (SELECT lead_id FROM contacts WHERE id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                    WHEN 'property' THEN (SELECT lead_id FROM properties WHERE id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                    WHEN 'task' THEN (SELECT lead_id FROM tasks WHERE id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                END,
                CASE a.entity_type
                    WHEN 'contact' THEN CAST(NULLIF(a.entity_id,'') AS INTEGER)
                    WHEN 'lead' THEN (SELECT id FROM contacts WHERE lead_id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                    WHEN 'opportunity' THEN (SELECT contact_id FROM opportunities WHERE id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                END,
                CASE WHEN a.entity_type='opportunity' THEN CAST(NULLIF(a.entity_id,'') AS INTEGER)
                     WHEN a.entity_type='lead' THEN (SELECT id FROM opportunities WHERE lead_id=CAST(a.entity_id AS INTEGER) AND organization_id=a.organization_id)
                END,
                'audit','system','internal',a.created_at,
                'Audit: ' || replace(a.action,'.',' '),a.details_json,
                'audit_events',a.id,json_object('action',a.action,'entity_type',a.entity_type,'entity_id',a.entity_id),a.created_at
            FROM audit_events a"""
        )

    @staticmethod
    def _seed_phase5_templates(connection: sqlite3.Connection, organization_id: int) -> None:
        now = utc_now()
        templates = (
            (
                "Template: New lead review",
                "Create a review task when a new lead enters the CRM.",
                {"trigger": {"type": "lead_created"}, "conditions": {}, "delay_minutes": 0,
                 "actions": [{"type": "create_task", "title": "Review new lead: {name}"}]},
            ),
            (
                "Template: Missed call response",
                "Prepare a reply and task ten minutes after a missed call. Nothing is sent automatically.",
                {"trigger": {"type": "missed_call"}, "conditions": {}, "delay_minutes": 10,
                 "actions": [
                     {"type": "create_task", "title": "Return missed call from {name}"},
                     {"type": "prepare_sms_draft", "angle": "Missed call", "message": "Hi {first_name}, I saw your call about reference {reference}. When is a good time to connect?"},
                 ]},
            ),
            (
                "Template: Qualified seller follow-up",
                "Prepare a three-day follow-up after a deal reaches Qualified.",
                {"trigger": {"type": "stage_changed", "stage_key": "qualified"}, "conditions": {}, "delay_minutes": 4320,
                 "actions": [
                     {"type": "create_task", "title": "Follow up with qualified seller {name}"},
                     {"type": "prepare_sms_draft", "angle": "Qualified follow-up", "message": "Hi {first_name}, I wanted to follow up about {reference}. Are you still open to discussing a simple cash sale?"},
                 ]},
            ),
            (
                "Template: High-priority inbound call",
                "Create a same-day task when a priority 4–5 lead calls.",
                {"trigger": {"type": "incoming_call"}, "conditions": {"min_score": 4}, "delay_minutes": 0,
                 "actions": [{"type": "create_task", "title": "Call high-priority seller {name} back today"}]},
            ),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO workflow_definitions(
                organization_id,name,description,draft_json,enabled,is_template,created_at,updated_at
            ) VALUES (?,?,?,?,0,1,?,?)""",
            [
                (organization_id, name, description, json.dumps(definition, sort_keys=True), now, now)
                for name, description, definition in templates
            ],
        )

    @staticmethod
    def _seed_phase7_calendars(connection: sqlite3.Connection, organization_id: int) -> None:
        now = utc_now()
        cursor = connection.execute(
            """INSERT OR IGNORE INTO calendars(
                organization_id,name,kind,color,active,created_at,updated_at
            ) VALUES (?,'Appointments','appointments','#235d4d',1,?,?)""",
            (organization_id, now, now),
        )
        if cursor.rowcount:
            calendar_id = cursor.lastrowid
            connection.executemany(
                """INSERT OR IGNORE INTO calendar_availability(
                    organization_id,calendar_id,weekday,start_minute,end_minute
                ) VALUES (?,?,?,?,?)""",
                [
                    (organization_id, calendar_id, weekday, 9 * 60, 17 * 60)
                    for weekday in range(5)
                ],
            )

    @staticmethod
    def _seed_phase6_templates(connection: sqlite3.Connection, organization_id: int) -> None:
        now = utc_now()
        body = """VACANT LAND PURCHASE AGREEMENT - DRAFT TEMPLATE

Agreement date: {{ date_today }}
Buyer: {{ organization_name }}
Seller: {{ seller_name }}
Seller mailing address: {{ mailing_address }}

PROPERTY
Reference number: {{ reference_number }}
Property address: {{ property_address }}
County and state: {{ county }}, {{ state }}
Parcel/APN: {{ apn }}
Approximate acreage: {{ acreage }}

PURCHASE TERMS
Purchase price: {{ offer_price }}

Buyer and Seller intend this document to describe the proposed purchase of the property above. Closing, title, taxes, due diligence, access, possession, and other material terms must be reviewed and completed before use.

Seller: ______________________________    Date: ______________

Buyer: _______________________________    Date: ______________

NOTICE: This starter template is not legal advice and is not ready to send until reviewed for the property and applicable law."""
        connection.execute(
            """INSERT OR IGNORE INTO document_templates(
                organization_id,name,description,draft_body,active,created_at,updated_at
            ) VALUES (?,?,?, ?,1,?,?)""",
            (
                organization_id, "Starter: Vacant Land Purchase Agreement",
                "Unpublished starter requiring legal and deal-specific review.", body, now, now,
            ),
        )

    @staticmethod
    def _seed_phase10_ai(connection: sqlite3.Connection, organization_id: int) -> None:
        now = utc_now()
        agents = (
            (
                "Workflow Copilot", "workflow_builder",
                "Turns plain-English requests into validated, disabled workflow proposals for human review.",
                "Create only valid workflow drafts. Never publish, enable, send, call, or modify CRM records.",
                ["read_workflow_schema", "search_knowledge", "create_workflow_proposal"],
                "AI-generated workflow draft. A person must review and publish it before it can run.",
                {"always_human_review": True},
            ),
            (
                "Conversation Capture Reviewer", "conversation_capture",
                "Extracts proposed structured facts from call transcripts with evidence and human approval.",
                "Propose only facts supported by the transcript. Never overwrite CRM data or contact a seller.",
                ["read_lead", "read_call_transcript", "search_knowledge", "create_capture_proposal", "create_handoff"],
                "AI-assisted call review. Extracted facts are proposals until a person accepts them.",
                {"opt_out": "urgent", "legal_threat": "urgent", "uncertain_identity": "high"},
            ),
        )
        for name, agent_type, description, instructions, tools, disclosure, handoff in agents:
            connection.execute(
                """INSERT OR IGNORE INTO ai_agents(
                    organization_id,name,agent_type,description,draft_instructions,tool_allowlist_json,
                    disclosure_text,handoff_rules_json,published_version,enabled,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,1,1,?,?)""",
                (
                    organization_id, name, agent_type, description, instructions,
                    json.dumps(tools), disclosure, json.dumps(handoff, sort_keys=True), now, now,
                ),
            )
            agent = connection.execute(
                "SELECT id FROM ai_agents WHERE organization_id=? AND name=?",
                (organization_id, name),
            ).fetchone()
            connection.execute(
                """INSERT OR IGNORE INTO ai_agent_versions(
                    organization_id,agent_id,version,instructions,tool_allowlist_json,
                    disclosure_text,handoff_rules_json,created_at
                ) VALUES (?,?,1,?,?,?,?,?)""",
                (organization_id, agent["id"], instructions, json.dumps(tools), disclosure, json.dumps(handoff, sort_keys=True), now),
            )
        knowledge_name = "Land acquisition review guide"
        knowledge = (
            "Goal: identify owners who may accept a meaningful discount for a simple cash sale.\n"
            "Price shopping or wanting full market value is not strong motivation.\n"
            "Never exploit distress, invent facts, pressure a seller, or hide that the user is interacting with AI.\n"
            "Treat opt-out requests, legal threats, uncertain identity, and sensitive situations as human handoffs.\n"
            "Reference numbers often contain four letters followed by numeric groups separated by hyphens.\n"
            "All extracted names, references, motivation, distress, price posture, and workflow plans require human review."
        )
        connection.execute(
            """INSERT OR IGNORE INTO ai_knowledge_bases(
                organization_id,name,description,draft_content,published_version,active,created_at,updated_at
            ) VALUES (?,?,?,?,1,1,?,?)""",
            (organization_id, knowledge_name, "Published safety and qualification guidance for assistive AI.", knowledge, now, now),
        )
        base = connection.execute(
            "SELECT id FROM ai_knowledge_bases WHERE organization_id=? AND name=?",
            (organization_id, knowledge_name),
        ).fetchone()
        connection.execute(
            """INSERT OR IGNORE INTO ai_knowledge_versions(
                organization_id,knowledge_base_id,version,content,content_sha256,created_at
            ) VALUES (?,?,1,?,?,?)""",
            (organization_id, base["id"], knowledge, hashlib.sha256(knowledge.encode()).hexdigest(), now),
        )
        cases = (
            ("workflow_builder", "Missed-call draft", {"prompt": "After a missed call wait 10 minutes, create a callback task and prepare a text draft."}, {"trigger": "missed_call", "action": "prepare_sms_draft"}),
            ("workflow_builder", "Qualified three-day follow-up", {"prompt": "When a deal becomes qualified wait 3 days and create a follow-up task."}, {"trigger": "stage_changed", "delay_minutes": 4320}),
            ("conversation_capture", "Spoken reference", {"transcript": "My reference is DEMO-2612-1 and I may sell for a quick cash closing."}, {"reference_number": "DEMO-2612-1"}),
            ("conversation_capture", "Opt-out handoff", {"transcript": "Stop calling me and remove me from your list."}, {"handoff": True}),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO ai_evaluation_cases(
                organization_id,agent_type,name,input_json,expected_json,created_at
            ) VALUES (?,?,?,?,?,?)""",
            [(organization_id, agent_type, name, json.dumps(input_data), json.dumps(expected), now) for agent_type, name, input_data, expected in cases],
        )

    @staticmethod
    def _seed_phase11(connection: sqlite3.Connection, organization_id: int) -> None:
        now = utc_now()
        articles = (
            ("getting-started", "Getting started", "Open a lead to review its complete call history, seller story, property matches, suggested follow-up, and tasks. Use stages and priorities to keep the pipeline current.", "Getting started", 10),
            ("reviewing-ai", "Reviewing AI suggestions", "AI Assist creates proposals only. Check transcript evidence, select only verified fields, and use human handoffs for opt-outs, legal concerns, or uncertain identity.", "AI Assist", 20),
            ("calls-and-texts", "Calls, texts, and consent", "Connect Vumber from settings. Every outbound call or text requires an explicit confirmation. Respect consent suppressions and never contact a seller who requested an opt-out.", "Communication", 30),
            ("forms", "Public seller inquiry form", "The public form creates or updates a lead by phone number and records the submission. It never sends a message; normal safe workflow rules may prepare drafts or review tasks.", "Lead capture", 40),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO help_articles(organization_id,slug,title,body,category,position,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(organization_id, slug, title, body, category, position, now, now) for slug, title, body, category, position in articles],
        )
        fields = [
            {"key": "owner_name", "label": "Your name", "required": True},
            {"key": "phone", "label": "Phone number", "required": True},
            {"key": "reference_number", "label": "Reference number from our letter", "required": False},
            {"key": "property_location", "label": "Property county or location", "required": False},
            {"key": "selling_reason", "label": "What would make selling worthwhile for you?", "required": False, "type": "textarea"},
        ]
        connection.execute(
            """INSERT OR IGNORE INTO public_forms(
                organization_id,name,slug,title,intro_text,fields_json,success_text,lead_source,active,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?, ?,1,?,?)""",
            (
                organization_id, "Seller inquiry", f"seller-inquiry-{organization_id}",
                "Tell us about your property",
                "Share a few details and a member of the acquisition team can review them.",
                json.dumps(fields), "Thank you. Your property information was received for review.",
                "Public seller inquiry", now, now,
            ),
        )

    @staticmethod
    def _rebuild_leads_unique_for_tenants(connection: sqlite3.Connection) -> None:
        indexes = connection.execute("PRAGMA index_list(leads)").fetchall()
        has_old_unique = False
        for index in indexes:
            if not index["unique"]:
                continue
            columns = [
                row["name"] for row in connection.execute(
                    f"PRAGMA index_info({index['name']!r})"
                ).fetchall()
            ]
            if columns == ["phone_normalized"]:
                has_old_unique = True
                break
        if not has_old_unique:
            return
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript("""
            CREATE TABLE leads_tenant_new (
                id INTEGER PRIMARY KEY,
                organization_id INTEGER NOT NULL DEFAULT 1 REFERENCES organizations(id),
                phone_normalized TEXT NOT NULL, phone_display TEXT NOT NULL,
                owner_name TEXT NOT NULL DEFAULT '', reference_number TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '', follow_up_plan TEXT NOT NULL DEFAULT '',
                follow_up_score INTEGER NOT NULL DEFAULT 1 CHECK(follow_up_score BETWEEN 1 AND 5),
                reasoning TEXT NOT NULL DEFAULT '', distress_signal TEXT NOT NULL DEFAULT '',
                seller_motivation TEXT NOT NULL DEFAULT '', price_expectation TEXT NOT NULL DEFAULT '',
                deal_style TEXT NOT NULL DEFAULT '', lead_source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','contacted','qualified','nurture','won','lost')),
                next_follow_up TEXT, notes TEXT NOT NULL DEFAULT '', first_activity TEXT,
                latest_activity TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(organization_id, phone_normalized)
            );
            INSERT INTO leads_tenant_new SELECT
                id, organization_id, phone_normalized, phone_display, owner_name,
                reference_number, summary, follow_up_plan, follow_up_score, reasoning,
                distress_signal, seller_motivation, price_expectation, deal_style,
                lead_source, status, next_follow_up, notes, first_activity,
                latest_activity, created_at, updated_at
            FROM leads;
            DROP TABLE leads;
            ALTER TABLE leads_tenant_new RENAME TO leads;
        """)
        connection.execute("PRAGMA foreign_keys = ON")

    def message_complete(self, message_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM processed_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return bool(row and row["status"] == "complete")

    def mark_message(self, message_id: str, status: str, error: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO processed_messages(message_id, status, error, processed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     status=excluded.status, error=excluded.error, processed_at=excluded.processed_at""",
                (message_id, status, error[:2000], utc_now()),
            )

    def claim_message(self, message_id: str) -> bool:
        """Atomically claim an unseen/failed message for one sync worker."""
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO processed_messages(message_id, status, error, processed_at)
                   VALUES (?, 'processing', '', ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     status='processing', error='', processed_at=excluded.processed_at
                   WHERE processed_messages.status = 'failed'""",
                (message_id, utc_now()),
            )
            return bool(cursor.rowcount)

    def requeue_processing_messages(self) -> int:
        """Make messages left by an interrupted worker available again."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE processed_messages SET status='failed', error='Interrupted worker' WHERE status='processing'"
            )
            return int(cursor.rowcount)

    def requeue_messages_without_calls(self) -> int:
        """Recover messages interrupted after download but before call persistence."""
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE processed_messages
                   SET status='failed', error='Interrupted before CRM persistence'
                   WHERE status='complete'
                     AND message_id NOT IN (SELECT message_id FROM calls)"""
            )
            return int(cursor.rowcount)

    def ensure_lead(
        self, phone_normalized: str, phone_display: str, organization_id: int = 1,
    ) -> int:
        key = phone_normalized or phone_display or "unknown"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO leads(organization_id, phone_normalized, phone_display, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(organization_id, phone_normalized) DO UPDATE SET
                     phone_display=CASE WHEN excluded.phone_display != '' THEN excluded.phone_display ELSE leads.phone_display END,
                     updated_at=excluded.updated_at""",
                (organization_id, key, phone_display or key, now, now),
            )
            row = connection.execute(
                "SELECT id FROM leads WHERE organization_id=? AND phone_normalized = ?",
                (organization_id, key),
            ).fetchone()
            lead_id = int(row["id"])
            connection.execute(
                """INSERT INTO contacts(
                    organization_id,lead_id,primary_phone_normalized,primary_phone_display,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(organization_id, lead_id) DO UPDATE SET
                    primary_phone_normalized=excluded.primary_phone_normalized,
                    primary_phone_display=excluded.primary_phone_display,
                    updated_at=excluded.updated_at""",
                (organization_id, lead_id, key, phone_display or key, now, now),
            )
            self._ensure_phase3_for_org(connection, organization_id)
            self._seed_phase5_templates(connection, organization_id)
            self._seed_phase6_templates(connection, organization_id)
            self._seed_phase10_ai(connection, organization_id)
            self._seed_phase11(connection, organization_id)
            self._seed_phase7_calendars(connection, organization_id)
            return lead_id

    def sync_contact_for_lead(self, lead_id: int) -> Optional[int]:
        """Keep the normalized contact compatibility record aligned with a lead."""
        with self.connect() as connection:
            lead = connection.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not lead:
                return None
            connection.execute(
                """INSERT INTO contacts(
                    organization_id,lead_id,full_name,primary_phone_normalized,
                    primary_phone_display,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(organization_id, lead_id) DO UPDATE SET
                    full_name=excluded.full_name,
                    primary_phone_normalized=excluded.primary_phone_normalized,
                    primary_phone_display=excluded.primary_phone_display,
                    updated_at=excluded.updated_at""",
                (
                    lead["organization_id"], lead_id, lead["owner_name"],
                    lead["phone_normalized"], lead["phone_display"],
                    lead["created_at"], utc_now(),
                ),
            )
            contact = connection.execute(
                "SELECT id FROM contacts WHERE organization_id=? AND lead_id=?",
                (lead["organization_id"], lead_id),
            ).fetchone()
            connection.execute(
                """UPDATE opportunities SET contact_id=?,title=?,updated_at=?
                   WHERE organization_id=? AND lead_id=?""",
                (
                    contact["id"], lead["owner_name"] or lead["phone_display"], utc_now(),
                    lead["organization_id"], lead_id,
                ),
            )
            return int(contact["id"])

    def add_call(self, lead_id: int, call: dict[str, Any]) -> bool:
        source_key = call.get("source_key") or f"{call.get('message_id','')}:{call.get('attachment_path','')}"
        with self.connect() as connection:
            lead = connection.execute(
                "SELECT organization_id FROM leads WHERE id=?", (lead_id,)
            ).fetchone()
            if not lead:
                raise ValueError(f"Lead {lead_id} does not exist")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO calls(
                       organization_id, lead_id, message_id, thread_id, source_key, direction,
                       vumber_number, vumber_label, occurred_at, time_description,
                       transcript, transcript_path, attachment_path, call_reference, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lead["organization_id"], lead_id,
                    call.get("message_id", ""),
                    call.get("thread_id", ""),
                    source_key,
                    call.get("direction", "unknown"),
                    call.get("vumber_number", ""),
                    call.get("vumber_label", ""),
                    call.get("timestamp_iso"),
                    call.get("time_description", ""),
                    call.get("transcript", ""),
                    call.get("transcript_path", ""),
                    call.get("attachment_path", ""),
                    call.get("call_reference", ""),
                    utc_now(),
                ),
            )
            if cursor.rowcount:
                call_id = int(cursor.lastrowid)
                context = connection.execute(
                    """SELECT c.id contact_id,o.id opportunity_id FROM contacts c
                       LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id
                       WHERE c.organization_id=? AND c.lead_id=?""",
                    (lead["organization_id"], lead_id),
                ).fetchone()
                direction = call.get("direction", "unknown")
                title = {
                    "incoming": "Incoming call", "outgoing": "Outgoing call",
                    "missed": "Missed call",
                }.get(direction, "Call")
                connection.execute(
                    """INSERT OR IGNORE INTO activity_events(
                        organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                        occurred_at,title,body,source_table,source_id,metadata_json,created_at
                    ) VALUES (?,?,?,?,?,'call',?,?,?,?,?,?,?,?)""",
                    (
                        lead["organization_id"], lead_id,
                        context["contact_id"] if context else None,
                        context["opportunity_id"] if context else None,
                        "call", direction if direction in {"incoming", "outgoing", "missed"} else "unknown",
                        call.get("timestamp_iso") or utc_now(), title, call.get("transcript", ""),
                        "calls", call_id,
                        json.dumps({
                            "recording": call.get("attachment_path", ""),
                            "reference": call.get("call_reference", ""),
                            "vumber_number": call.get("vumber_number", ""),
                        }, sort_keys=True), utc_now(),
                    ),
                )
                connection.execute(
                    """UPDATE leads SET
                         first_activity = CASE
                           WHEN first_activity IS NULL OR (COALESCE(?, '') != '' AND ? < first_activity) THEN ?
                           ELSE first_activity END,
                         latest_activity = CASE
                           WHEN latest_activity IS NULL OR COALESCE(?, '') > COALESCE(latest_activity, '') THEN ?
                           ELSE latest_activity END,
                         updated_at = ?
                       WHERE id = ?""",
                    (
                        call.get("timestamp_iso"), call.get("timestamp_iso"), call.get("timestamp_iso"),
                        call.get("timestamp_iso"), call.get("timestamp_iso"), utc_now(), lead_id,
                    ),
                )
            return bool(cursor.rowcount)

    def add_inbound_sms(
        self,
        lead_id: int,
        provider_message_id: str,
        from_number: str,
        to_number: str,
        message: str,
        occurred_at: str,
        response: str = "",
    ) -> bool:
        """Persist one Gmail/Vumber SMS and its unified timeline event idempotently."""
        with self.connect() as connection:
            lead = connection.execute(
                "SELECT organization_id FROM leads WHERE id=?", (lead_id,)
            ).fetchone()
            if not lead:
                raise ValueError(f"Lead {lead_id} does not exist")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO communications(
                    organization_id,lead_id,kind,direction,from_number,to_number,message,
                    status,response,provider_message_id,created_at
                ) VALUES (?,?,'sms','incoming',?,?,?,'received',?,?,?)""",
                (
                    lead["organization_id"], lead_id, from_number, to_number, message,
                    response[:4000], provider_message_id, occurred_at,
                ),
            )
            if not cursor.rowcount:
                return False
            communication_id = int(cursor.lastrowid)
            context = connection.execute(
                """SELECT c.id contact_id,o.id opportunity_id FROM contacts c
                   LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id
                   WHERE c.organization_id=? AND c.lead_id=?""",
                (lead["organization_id"], lead_id),
            ).fetchone()
            connection.execute(
                """INSERT OR IGNORE INTO activity_events(
                    organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                    occurred_at,title,body,source_table,source_id,metadata_json,created_at
                ) VALUES (?,?,?,?,?,'sms','incoming',?,?,?,?,?,?,?)""",
                (
                    lead["organization_id"], lead_id,
                    context["contact_id"] if context else None,
                    context["opportunity_id"] if context else None,
                    "sms", occurred_at, "Text received", message, "communications",
                    communication_id,
                    json.dumps({"from": from_number, "to": to_number, "source": "gmail-vumber"}, sort_keys=True),
                    utc_now(),
                ),
            )
            connection.execute(
                """UPDATE leads SET
                    first_activity=CASE WHEN first_activity IS NULL OR ? < first_activity THEN ? ELSE first_activity END,
                    latest_activity=CASE WHEN latest_activity IS NULL OR ? > latest_activity THEN ? ELSE latest_activity END,
                    updated_at=? WHERE id=?""",
                (occurred_at, occurred_at, occurred_at, occurred_at, utc_now(), lead_id),
            )
            return True

    def transcription_candidates(
        self, provider: str, model: str, limit: Optional[int] = None, organization_id: int = 1,
    ) -> list[dict[str, Any]]:
        sql = """SELECT c.* FROM calls c
                 WHERE c.organization_id=? AND c.attachment_path != ''
                   AND NOT EXISTS (
                     SELECT 1 FROM transcription_jobs j
                     WHERE j.organization_id=c.organization_id AND j.call_id=c.id
                       AND j.provider=? AND j.model=? AND j.status='complete'
                   )
                 ORDER BY c.id"""
        params: list[Any] = [organization_id, provider, model]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def begin_transcription_job(
        self, call_id: int, provider: str, model: str, audio_sha256: str,
        organization_id: int = 1,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO transcription_jobs(
                    organization_id,call_id,provider,model,audio_sha256,status,attempts,error,started_at
                ) VALUES (?,?,?,?,?,'processing',1,'',?)
                ON CONFLICT(organization_id,call_id,provider,model,audio_sha256) DO UPDATE SET
                    status='processing',attempts=transcription_jobs.attempts+1,error='',started_at=excluded.started_at,
                    finished_at=NULL""",
                (organization_id, call_id, provider, model, audio_sha256, now),
            )

    def complete_transcription_job(
        self, call_id: int, provider: str, model: str, audio_sha256: str,
        transcript: str, transcript_path: str, organization_id: int = 1,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE calls SET transcript=?,transcript_path=? WHERE id=? AND organization_id=?",
                (transcript, transcript_path, call_id, organization_id),
            )
            connection.execute(
                """UPDATE transcription_jobs SET status='complete',error='',finished_at=?
                   WHERE organization_id=? AND call_id=? AND provider=? AND model=? AND audio_sha256=?""",
                (now, organization_id, call_id, provider, model, audio_sha256),
            )
            call = connection.execute(
                "SELECT lead_id FROM calls WHERE id=? AND organization_id=?",
                (call_id, organization_id),
            ).fetchone()
            if call:
                context = connection.execute(
                    """SELECT c.id contact_id,o.id opportunity_id FROM contacts c
                       LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id
                       WHERE c.organization_id=? AND c.lead_id=?""",
                    (organization_id, call["lead_id"]),
                ).fetchone()
                connection.execute(
                    """INSERT OR IGNORE INTO activity_events(
                        organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                        occurred_at,title,body,source_table,source_id,metadata_json,created_at
                    ) VALUES (?,?,?,?,?,'system','internal',?,?,?,?,?,?,?)""",
                    (
                        organization_id, call["lead_id"],
                        context["contact_id"] if context else None,
                        context["opportunity_id"] if context else None,
                        "transcription_updated", now, "Transcript upgraded", "",
                        "calls", call_id,
                        json.dumps({"provider": provider, "model": model, "audio_sha256": audio_sha256}, sort_keys=True),
                        now,
                    ),
                )

    def fail_transcription_job(
        self, call_id: int, provider: str, model: str, audio_sha256: str, error: str,
        organization_id: int = 1,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE transcription_jobs SET status='failed',error=?,finished_at=?
                   WHERE organization_id=? AND call_id=? AND provider=? AND model=? AND audio_sha256=?""",
                (error[:2000], utc_now(), organization_id, call_id, provider, model, audio_sha256),
            )

    def remap_message_calls(
        self,
        message_id: str,
        phone_normalized: str,
        phone_display: str,
        direction: str,
        vumber_number: str = "",
        vumber_label: str = "",
    ) -> int:
        """Move every recording from one Gmail message to the phone-number lead."""
        # Vumber normally supplies ten-digit US numbers, but international and
        # occasionally spoofed caller IDs can contain up to fifteen digits.
        if not (phone_normalized.isdigit() and 7 <= len(phone_normalized) <= 15):
            return 0
        lead_id = self.ensure_lead(phone_normalized, phone_display)
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE calls SET lead_id=?, direction=?, vumber_number=?, vumber_label=?
                   WHERE message_id=?""",
                (lead_id, direction, vumber_number, vumber_label, message_id),
            )
            return int(cursor.rowcount)

    def finalize_identity_rebuild(self) -> int:
        """Drop empty legacy identities and recompute activity bounds."""
        with self.connect() as connection:
            connection.execute(
                """UPDATE leads SET
                   first_activity=(SELECT MIN(c.occurred_at) FROM calls c WHERE c.lead_id=leads.id),
                   latest_activity=(SELECT MAX(c.occurred_at) FROM calls c WHERE c.lead_id=leads.id),
                   updated_at=?""",
                (utc_now(),),
            )
            cursor = connection.execute(
                "DELETE FROM leads WHERE NOT EXISTS (SELECT 1 FROM calls c WHERE c.lead_id=leads.id)"
            )
            return int(cursor.rowcount)

    def calls_for_lead(self, lead_id: int, limit: Optional[int] = None, organization_id: int = 1) -> list[dict[str, Any]]:
        params: list[Any] = [lead_id, organization_id]
        if limit:
            sql = """SELECT * FROM (
                SELECT * FROM calls WHERE lead_id = ? AND organization_id = ?
                ORDER BY COALESCE(occurred_at, created_at) DESC LIMIT ?
            ) ORDER BY COALESCE(occurred_at, created_at) ASC"""
            params.append(limit)
        else:
            sql = "SELECT * FROM calls WHERE lead_id = ? AND organization_id = ? ORDER BY COALESCE(occurred_at, created_at) ASC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def all_leads(self, organization_id: int = 1) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT l.*, COUNT(c.id) AS call_count
                   FROM leads l LEFT JOIN calls c ON c.lead_id=l.id AND c.organization_id=l.organization_id
                   WHERE l.organization_id=?
                   GROUP BY l.id ORDER BY l.follow_up_score DESC, l.latest_activity DESC""",
                (organization_id,),
            ).fetchall()]

    def update_analysis(self, lead_id: int, analysis: Any) -> None:
        values = analysis.model_dump() if hasattr(analysis, "model_dump") else dict(analysis)
        with self.connect() as connection:
            current = connection.execute("SELECT follow_up_plan,organization_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not current:
                raise ValueError(f"Lead {lead_id} does not exist")
            connection.execute(
                """UPDATE leads SET
                   owner_name=CASE WHEN owner_name='' THEN ? ELSE owner_name END,
                   reference_number=CASE WHEN reference_number='' THEN ? ELSE reference_number END,
                   summary=?, follow_up_plan=?, follow_up_score=?, reasoning=?,
                   distress_signal=?, seller_motivation=?, price_expectation=?, updated_at=?
                   WHERE id=?""",
                (
                    values.get("owner_name") or "", values.get("reference_number") or "",
                    values.get("summary") or "", values.get("follow_up_plan") or "",
                    int(values.get("follow_up_score") or 1), values.get("reasoning") or "",
                    values.get("distress_signal") or "",
                    values.get("seller_motivation") or "",
                    values.get("price_expectation") or "",
                    utc_now(), lead_id,
                ),
            )
            plan = (values.get("follow_up_plan") or "").strip()
            if plan and (not current or current["follow_up_plan"] != plan):
                connection.execute(
                    """INSERT INTO tasks(organization_id, lead_id, title, status, source, created_at)
                       VALUES (?, ?, ?, 'open', 'ai', ?)""",
                    (current["organization_id"], lead_id, plan, utc_now()),
                )

    def start_sync(self, organization_id: int = 1) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(organization_id, started_at, status) VALUES (?, ?, 'running')",
                (organization_id, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_sync(self, run_id: int, status: str, stats: dict[str, int], error: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE sync_runs SET finished_at=?, status=?, messages_seen=?, messages_processed=?,
                   calls_added=?, leads_updated=?, error=? WHERE id=?""",
                (
                    utc_now(), status, stats.get("messages_seen", 0), stats.get("messages_processed", 0),
                    stats.get("calls_added", 0), stats.get("leads_updated", 0), error[:2000], run_id,
                ),
            )

    def import_legacy_export(self, path: Path) -> tuple[int, int]:
        if not path.exists():
            return (0, 0)
        leads_added = calls_added = 0
        for item in json.loads(path.read_text(encoding="utf-8")):
            lead_id = self.ensure_lead(item.get("phone_number_normalized", ""), item.get("phone_number", ""))
            self.update_analysis(lead_id, item)
            leads_added += 1
            for call in item.get("calls", []):
                call["source_key"] = f"legacy:{call.get('message_id','')}:{call.get('attachment_path','')}"
                calls_added += int(self.add_call(lead_id, call))
        return leads_added, calls_added
