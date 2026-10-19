# Vumber Lead CRM

For a fresh checkout or shared team instance, see [Team setup](docs/team-setup.md). The repository contains application code; customer records and service credentials are supplied separately. Use `serve_crm.py` for a shared instance with login required.

This project turns Vumber call and SMS emails into a durable local CRM:

1. Gmail is searched with read-only OAuth access.
2. MP3 attachments (or Vumber playback links) are downloaded once.
3. Calls are transcribed with OpenRouter Whisper Large v3, OpenAI, or local `faster-whisper`.
4. Inbound Vumber SMS notifications are attached to the same phone-number lead and unified timeline.
5. Each phone number's history becomes a lead with a concrete follow-up task.
6. Leads, calls, texts, tasks, stages, dates, and notes live in `data/leads.db` and a browser dashboard.

Syncs are incremental. Completed Gmail message IDs and calls are checkpointed in SQLite, so normal runs only process new mail and preserve CRM edits.

## Setup

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Put a Google Desktop OAuth file in this folder as `credentials.json`, or leave a single `client_secret*.json` file here for automatic detection. The first run opens Google authorization and stores `token.json`. Gmail permission is read-only.

Set provider keys in `.env`:

```dotenv
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_TRANSCRIPTION_MODEL=openai/whisper-large-v3
TRANSCRIPTION_PROVIDER=openrouter
ANALYSIS_PROVIDER=openrouter
ANALYSIS_MODEL=openai/gpt-4o-mini
```

OAuth files, API keys, recordings, transcripts, and the CRM database are excluded by `.gitignore`.

## Sync Gmail

Recommended OpenRouter transcription and analysis (also imports `sms@vumber.com`):

```powershell
.\.venv\Scripts\python.exe lead_pipeline.py
```

Free local transcription with OpenAI analysis:

```powershell
.\.venv\Scripts\python.exe lead_pipeline.py --transcription-provider local
```

On a Windows PC with an NVIDIA CUDA 12 driver, install the optional project-local GPU runtime and use CUDA:

```powershell
pip install -r requirements-gpu.txt
.\.venv\Scripts\python.exe lead_pipeline.py --transcription-provider local --local-device cuda --local-compute-type float16
```

Local transcription with OpenRouter analysis:

```powershell
.\.venv\Scripts\python.exe lead_pipeline.py `
  --transcription-provider local `
  --analysis-provider openrouter `
  --analysis-model deepseek/deepseek-chat
```

Useful options:

- `--query "from:donotreply@vumber.com newer_than:30d"` changes the Gmail search.
- `--max-messages 20` processes the newest unprocessed messages as a test batch.
- `--force` re-reads checkpointed messages; calls remain deduplicated.
- `--rebuild-identities` repairs an older database by regrouping every stored call under the lead phone number found in its Vumber subject, then refreshes the combined lead stories.
- `--reanalyze-all --analysis-workers 12` refreshes every stored lead's multi-call story and follow-up plan in parallel.
- `--retranscribe-downloaded --retranscribe-workers 8` safely upgrades downloaded recordings, checkpoints each call, and resumes by skipping completed audio.
- `--import-legacy` imports the old `exports/leads.json` file.
- `--database PATH` selects another SQLite CRM file.
- `--log-level DEBUG` enables verbose diagnostics.

Portable `exports/leads.json` and `exports/leads.csv` files refresh after a successful sync. SQLite remains the source of truth for user-edited CRM fields.

## Open the CRM

```powershell
.\.venv\Scripts\python.exe crm_app.py
```

Open <http://127.0.0.1:5000>. The dashboard supports search, stage/priority/activity-date filters, sorting by most recent activity, editable lead details, next-follow-up dates, notes, tasks, complete call transcripts, and in-browser call playback. Lead intelligence includes distress signals, seller motivation, price posture, and discount-oriented follow-up scoring.

The first browser visit opens a one-time owner setup screen. Create the owner account there; the app never ships with a default password. Later, owners and managers can use **Team & audit** to create owner/manager/agent/viewer accounts and inspect security and mutation events. Every CRM record is organization-scoped, browser mutations use CSRF protection, and repeated failed logins are throttled. Existing records are migrated into the Fuller Oak organization automatically, with a pre-migration backup at `data/leads.before-tenancy.db`.

The session-signing key is encrypted for the current Windows user in `data/flask_secret.bin`. For a hosted deployment, set a long random `CRM_SECRET_KEY`, enable secure cookies behind HTTPS, and move the in-process login limiter to a shared store before running multiple server processes.

Use **+ Add lead** to create a lead manually from a name and phone number, with optional reference, deal style, source, and notes. The lead page automatically maintains three suggested SMS drafts using the current lead story, calls, prior outbound texts, offer record, motivation, distress, price posture, and deal stage. **Use this draft** places a suggestion into the Vumber composer for editing; it never sends automatically. **Regenerate** refreshes the suggestions after the situation changes. If `OPENAI_API_KEY` or `OPENROUTER_API_KEY` is configured, the drafts use that model; otherwise the CRM provides context-aware local drafts without an API call.

## Contacts, properties, fields, tags, and imports

**Contacts** is the normalized people view layered over the existing lead records. It supports contact search, stage and tag filters, reference-only filtering, organization tags, typed custom fields, linked properties, and private or team-shared saved smart lists. Existing lead URLs and call histories continue to work.

**Properties** contains normalized records linked to contacts by reference. Opening a lead with an offer-database match refreshes its property details without modifying `all_fop_offers.db`.

Owners and managers can create typed contact/property fields and tags under **Fields & tags**. Supported field types are text, number, date, yes/no, and constrained choice lists.

The **Import CSV** workflow stages UTF-8 CSV files up to 5 MB and 10,000 rows. It provides explicit column mapping, validates every row, shows errors before writing, and requires a separate confirmation to commit. Phone is the duplicate key; existing contacts can be updated or skipped. A committed batch is idempotent and cannot run twice. Every stage, validation, and commit is recorded in the audit log.

## Deals, pipelines, and stages

**Deals** opens the acquisitions board. Every existing lead is represented by one opportunity linked to its seller contact and, when known, its property. Cards show phone, reference, priority, property, and estimated value. Drag a card between stages, or use the stage selector and **Move** button for a keyboard-accessible alternative. Every change is validated, protected against stale updates from another tab, written to stage history, and recorded in the organization audit log.

Use **Table view** to search and filter opportunities. A deal page provides editable value, assignment, notes, seller/property links, lead intelligence, and complete stage history, with a direct link back to calls and communications. Owners and managers can use **Configure pipelines** to create acquisition, disposition, owner-finance, or other pipelines and add, rename, color, classify, and reorder stages.

The original lead status remains synchronized with the default pipeline so existing dashboard filters and integrations continue to work. The Phase 3 migration creates `data/leads.before-phase3.db` before backfilling opportunities.

## Inbox and unified activity

**Inbox** groups all seller conversations by lead and orders them by most recent activity. Search by seller, phone, or reference and filter conversations to calls, texts, or future email events. Opening a conversation shows the complete chronological history with channel filters for calls, texts, email, documents, system events, and notes. Call events retain the full transcript and in-browser recording playback.

**All activity** is the organization-wide event ledger. It supports text search, channel and direction filters, and 100-event pagination. Calls, Vumber texts/callbacks, deal-stage transitions, and audit activity are added automatically. The schema is ready for later email and document providers without changing the timeline UI.

Activity records are append-only: database triggers reject updates and deletions, while source call, communication, transition, and audit rows remain intact. The Phase 4 migration creates `data/leads.before-phase4.db` before backfilling history.

## Workflow automation

**Automations** provides a reviewed draft-and-publish workflow builder. Triggers include new leads, incoming or missed calls, sent texts, stage changes, and any CRM activity. Workflows can require a minimum priority, a particular lead stage, or a matched reference, and can wait from minutes to 30 days before acting.

Allowed actions are intentionally limited to creating follow-up tasks and preparing editable SMS drafts. An automation cannot send a message or place a call. Four land-investing templates are included but disabled; clone a template, review its fields, and explicitly publish it to create an immutable enabled version. Later draft edits do not alter the published version until you publish again.

The workflow engine uses a transactional outbox, durable jobs, idempotent action receipts, stale-lock recovery, three bounded action attempts, a failed-run display, and manual retry. Run the worker beside the CRM:

```powershell
.\.venv\Scripts\python.exe workflow_worker.py
```

For one processing pass during maintenance or testing:

```powershell
.\.venv\Scripts\python.exe workflow_worker.py --once
```

The Automations screen and `/health` show worker state and pending/failure counts. The Phase 5 migration creates `data/leads.before-phase5.db`; historical activity is marked delivered and is never replayed into newly published workflows.

## Documents and e-sign evidence

**Documents** provides versioned agreement templates with an allowlisted merge-field set (organization, deal, seller, property, and offer fields). Owners and managers edit a draft, review the allowed fields, and explicitly publish an immutable version; later draft edits never change what existing documents were built from. A starter vacant-land purchase agreement is included but unpublished — review it with counsel before use.

From any deal, create a document to see a merged preview with every missing value marked. Generating produces a PDF with a SHA-256 hash and page count, stored as an immutable artifact. Sending records the sent state and recipient without transmitting email automatically. A validated signed PDF can be uploaded to complete the document with hash evidence. Every lifecycle step (created, generated, sent, delivered, viewed, signed, declined, expired, completed) is written to an append-only event ledger that also appears in the unified activity timeline.

An e-sign provider webhook is available at `/webhooks/esign/<provider>` when `CRM_ESIGN_WEBHOOK_SECRET` is set (optionally scoped with `CRM_ESIGN_ORGANIZATION_ID`). Events must carry a valid HMAC-SHA256 `X-CRM-Signature` header and are deduplicated by provider event ID. For legally binding signatures, use a vetted e-sign provider; this local flow records evidence but is not a substitute for counsel-reviewed signature processes. The Phase 6 migration creates `data/leads.before-phase6.db`.

## Calendar and scheduling

**Calendar** renders a month grid of appointments with per-calendar and status filters and month navigation. A default Appointments calendar is created with weekday 9–5 availability windows; owners and managers can add more windows per weekday. Appointments link to a lead (which auto-links its contact and deal), support location and notes, and validate that the end is after the start. Status transitions (completed, cancelled, no-show) are audited and appear in the lead's unified activity timeline.

A JSON slot API is available for integrations: `/api/v1/calendar/available-slots?calendar_id=1&date=2026-10-05&duration_minutes=60` returns free 30-minute-aligned slots inside that day's availability windows, excluding booked appointments. Google/Microsoft calendar sync is not yet implemented. The Phase 7 migration creates `data/leads.before-phase7.db`.

## Projects

**Projects** are CRM-linked workspaces for due diligence, closing, and marketing work. Start from one of three land templates — due diligence, closing, or marketing launch — which clone into a project with pre-seeded checklists, or create a blank project. Projects can be linked to a deal, which connects the seller, property, and lead automatically.

The project page shows a four-column task board (To do / In progress / Blocked / Done) with one-click status moves, task details, due dates, subtasks, and a progress bar. Team members can leave project comments, and owners/managers can set the project status (active, on hold, completed, cancelled). Every task move, comment, and status change is recorded in the audit log. The Phase 8 migration creates `data/leads.before-phase8.db`.

## Communication settings

**Communication** (in the header) centralizes telephony configuration. The number inventory lists every line with its provider, label, and kind (SMS, calls, or both); owners and managers can add numbers manually or disable lines. Consent suppressions record do-not-contact requests per lead and channel — a suppressed lead cannot be texted or called from the CRM until the suppression is lifted, and every suppression change is audited. The delivery-state ledger shows recent outbound events (queued/sent/delivered/failed/undelivered); the local Vumber provider records sent state without provider delivery confirmation.

Outbound SMS and calls go through a formal provider boundary (`communication_providers.py`): the CRM routes call provider adapters, never Vumber internals, so an alternative telephony provider can be added by implementing the same small protocol. The Phase 9 migration creates `data/leads.before-phase9.db`.

## AI Assist

**AI Assist** is a human-review workspace, not an autonomous caller. A plain-English workflow request creates a pending proposal; accepting it creates a disabled, unpublished workflow draft. Call transcripts can be analyzed into proposed reference, motivation, distress, price-expectation, follow-up, and score fields with supporting transcript evidence. No lead field changes until a reviewer selects and accepts them. Opt-out requests, legal language, hostile calls, and uncertain identity can be placed in a separate human-handoff queue.

Agent instructions, disclosures, tool allowlists, and land-buying knowledge are versioned. Tool lists deliberately exclude message sending, calling, workflow publishing, and direct lead mutation. The built-in evaluation set checks missed-call workflow generation, delayed qualified-lead follow-up, spoken reference extraction, and opt-out escalation. The Phase 10 migration creates `data/leads.before-phase10.db`.

## Workspace, reports, help, forms, and mobile install

**Workspace** provides role-aware widgets for hot leads, pipeline stages, open tasks, appointments, help tickets, and recent activity. Each signed-in user can save their own layout. **Reports** summarizes pipeline distribution, call activity, lead sources, overdue and upcoming follow-ups, and motivation coverage over selectable date windows; owners and managers can save named report views.

**Help** contains local operating guidance and audited internal tickets with priority, related-lead links, status, and resolution. **Forms** manages a seller inquiry page at `/f/<slug>` that validates and deduplicates by phone, stores every submission, and creates a reviewable CRM lead without granting consent for automated contact. The CRM includes a web manifest, service worker, icon, standalone display metadata, and a privacy-safe offline page so it can be installed from a supported browser on localhost. CRM records are not cached for offline access. The Phase 11 migration creates `data/leads.before-phase11.db`; public hosting, custom domains, and a full site builder remain separate deployment work.

## API and inbound lead webhooks

Owners and managers can open **API & webhooks** to create scoped API keys (`read:leads` and `write:leads`) or signed inbound webhook endpoints. Secrets are displayed once; API keys are stored only as SHA-256 digests, while webhook signing secrets are protected with Windows DPAPI. Keys can be revoked immediately and every authenticated request is tenant-scoped, rate-limited, and logged.

`GET /api/v1/leads` provides bounded cursor pagination. `POST /api/v1/leads` requires a write-scoped bearer key and an `Idempotency-Key`; replaying the same request returns the stored response while reusing its key for different content returns a conflict. `/webhooks/leads/<slug>` requires `X-CRM-Event-ID`, a current `X-CRM-Timestamp`, and `X-CRM-Signature`, calculated as HMAC-SHA256 over `timestamp + "." + raw_body`. Event IDs are deduplicated and accepted/rejected attempts appear in the integration log. The Phase 12 migration creates `data/leads.before-phase12.db`.

If `all_fop_offers.db` is in the project folder, each lead reference is matched across all reference fields in that database. Matched owner, price, acreage, APN, location, address, dates, and parcel links appear on the lead page. You can correct or manually enter one or more references in **Manage lead**; the source offer database stays read-only, while a rebuildable lookup index is stored at `data/offer_reference_index.db`. Use `--offers-database PATH` to select another offer file.

Open **Vumber connection** from the CRM header to sign in once. The password is sent directly to Vumber and is never stored; only Vumber session tokens are retained, encrypted with Windows DPAPI for the current Windows user. Once connected, every lead page can send a text from an available Vumber line or start a callback call (Vumber rings your selected phone first). Both actions require an explicit confirmation checkbox and button click, and completed actions are recorded in the lead's CRM communication history. The fallback Vumber/device shortcuts remain available while disconnected.

`/health` shows database availability, counts, the Vumber connection state, and the last sync. The server binds to localhost by default. If the Vumber web app changes its private dashboard API, disconnect and reconnect first; the CRM reports a safe error instead of retrying a send or call automatically.

## Automation

Use Windows Task Scheduler to run the sync command at a chosen interval with this project as its working directory. Scheduled runs use the already-authorized `token.json`; keep provider keys in `.env`, not in the task command.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite includes two-organization isolation, role denial, CSRF, audit, contacts/imports, pipeline workflows, immutable activity, complete transcript rendering, and durable workflow publishing/execution/retry safety. The full Stride feature audit and phased delivery plan is in [`docs/stride_parity_audit.md`](docs/stride_parity_audit.md).

## Main files

- `lead_pipeline.py` — Gmail ingestion, audio download, transcription, and AI consolidation.
- `crm_db.py` — SQLite schema, deduplication, calls, leads, tasks, and sync state.
- `crm_app.py` — local CRM dashboard.
- `crm_phase2.py` — normalized contacts/properties, metadata, smart lists, and staged imports.
- `crm_phase3.py` — opportunities, configurable pipelines, deal board/table, and transition history.
- `crm_phase4.py` — unified activity ledger, seller inbox, channel filters, and pagination.
- `crm_phase5.py` — workflow builder, publishing, templates, run history, and retry UI.
- `crm_phase6.py` / `document_service.py` — document templates, safe merge, PDF generation, lifecycle events, and e-sign webhook.
- `crm_phase7.py` — calendars, availability windows, appointments, and the slot API.
- `crm_phase8.py` — CRM-linked projects, task boards, comments, and land templates.
- `crm_phase9.py` / `communication_providers.py` — number inventory, consent suppressions, delivery state, and the provider boundary.
- `crm_phase10.py` / `ai_builders.py` — reviewed AI proposals, structured call capture, versioned knowledge, evaluations, and handoffs.
- `crm_phase11.py` — personal workspaces, acquisition reports, help tickets, public seller forms, and PWA assets.
- `crm_phase12.py` — scoped REST lead access, idempotent creates, signed inbound webhooks, rate limits, and integration logs.
- `workflow_engine.py` / `workflow_worker.py` — transactional outbox, delayed jobs, retries, and safe action execution.
- `offer_db.py` — read-only offer matching and the normalized lookup index.
- `data/leads.db` — private CRM database (created automatically).

## Data privacy

This repository contains application source and synthetic test fixtures only. Customer databases, lead lists, recordings, transcripts, exported files, media, OAuth tokens, and service credentials are excluded from version control.
