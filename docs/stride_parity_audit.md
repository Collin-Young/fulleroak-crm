# Land CRM functional-parity audit

Date: 2026-09-23  
Primary specification: attached 20-minute Stride CRM demo transcript  
Secondary public references: [Stride public product page](https://stridecrm.co/), [Stride comparison page](https://stridecrm.co/stridevspebble), and [land-specific configuration overview](https://stridecrm.co/ghl)

## Implementation status

Phase 1 is now implemented. Migration `001_tenant_auth_audit` creates organizations, users, roles, audit events, a migration ledger, and organization ownership on every business table. Existing rows are backfilled to Fuller Oak while retaining their IDs, and the table-rebuild migration creates `data/leads.before-tenancy.db` before changing uniqueness rules. The migration was rehearsed on a copy of the current database: all 692 leads, 1,475 calls, 1,365 tasks, 1 communication, 3 message drafts, 1,530 processed messages, and 18 sync runs were preserved; `PRAGMA foreign_key_check` returned no errors.

The web application now has one-time owner setup, login/logout, a persistent Windows-encrypted session key, owner/manager/agent/viewer enforcement, CSRF checks on mutations, login throttling, tenant-scoped list/detail/update/audio/task/communication/draft/health queries, team administration, and an organization audit viewer. Audited actions include authentication, user changes, lead changes, draft regeneration, Vumber connection changes, SMS sends, callback calls, and task completion. The 17-test suite includes two-organization list/detail isolation, viewer write denial, missing-CSRF rejection, audit generation, and login throttling.

The remaining matrix below is the baseline parity assessment; rows marked missing or partial outside the security foundation remain roadmap work rather than claims of completion.

Phase 2 is now implemented as migration `002_contacts_properties_imports`. It adds tenant-scoped normalized contacts and properties while retaining leads as the compatibility layer; contact/property relationships; typed custom-field definitions and values; tags; saved private/shared filters; and staged import batches/rows. The web UI now includes Contacts, Properties, Fields & tags, saved smart lists, and a CSV import workflow with upload limits, automatic and manual mapping, per-row normalization/errors, duplicate update/skip policy, explicit confirmation, idempotent commit, and audit events.

The Phase 2 migration was rehearsed against a copy of the production-shaped database. It preserved all 692 leads, 1,475 calls, 1,365 tasks, and the existing communication; created 692 linked contacts and 199 normalized reference-backed property records; and produced no foreign-key errors. The suite now contains 26 passing tests, including imports, tenant isolation, pipeline workflows, immutable activity, and durable workflow automation.

Phase 3 is implemented as migration `003_opportunities_pipelines`. It adds tenant-scoped pipelines, ordered stages, opportunities, and immutable stage-transition history. Existing leads are backfilled one-for-one into the default Acquisitions pipeline while the legacy status remains synchronized for compatibility. The Deals area now includes an accessible drag-and-drop board with a select-and-button keyboard fallback, a searchable table, linked seller/property context, editable deal values/ownership, optimistic concurrency checks, and audited transitions. Owners and managers can create pipelines and add, edit, color, classify, and reorder their stages. The production-shaped migration created 692 opportunities across six default stages without foreign-key errors, with a pre-migration backup at `data/leads.before-phase3.db`.

Phase 4 is implemented as migration `004_unified_activity_inbox`. It adds an append-only, tenant-scoped activity ledger with database triggers that reject updates and deletes. Existing calls and complete transcripts, recordings, Vumber texts/calls, deal-stage changes, and audit events are projected into a common schema without altering their source rows. Inbox groups communication by seller and offers call/text/email filters; seller timelines and the organization-wide Activity screen provide complete bodies, recording playback, channel/direction/search filters, and bounded pagination. The live backfill created 1,482 events from 1,475 calls, one text, and six audit records without foreign-key errors, with a backup at `data/leads.before-phase4.db`.

Phase 5 is implemented as migration `005_workflow_automation`. It adds versioned workflow drafts and immutable published versions, disabled land-investing templates, a transactional event outbox, durable runs/jobs/action receipts, delayed scheduling, optimistic worker claiming, bounded retries, stale-lock recovery, dead-letter visibility, and manual retry. Triggers cover new leads, calls, missed calls, sent texts, stage changes, and general activity with score/stage/reference conditions. The action allowlist is deliberately limited to creating tasks and preparing editable SMS drafts; workflows cannot send messages or place calls. Four templates were seeded disabled, existing history was not queued, the worker exposes a heartbeat, and the migration produced no foreign-key errors. Backup: `data/leads.before-phase5.db`.

Phase 6 is implemented as migration `006_contracts_esign`. It adds tenant-scoped document templates with immutable published versions, an allowlisted merge-field set (organization, deal, seller, property, and offer fields), a safe merge engine that rejects unknown fields, generated PDFs with SHA-256 hashes and page counts, a full document lifecycle (draft → generated → sent → delivered/viewed/signed/declined/expired/completed), an append-only document event ledger projected into the unified activity timeline, validated signed-PDF uploads with hash evidence, path-safe file downloads, and an HMAC-signed e-sign provider webhook with idempotent event receipts. A disabled starter vacant-land purchase agreement template is seeded and requires explicit legal review and publication. The suite now contains 29 passing tests, including template version immutability, the complete document lifecycle, event immutability, tenant isolation, and webhook signature/duplicate handling. Backup: `data/leads.before-phase6.db`.

Phase 7 is implemented as migration `007_calendar_scheduling`. It adds tenant-scoped calendars with kinds and colors, per-weekday availability windows, and appointments linked to leads, contacts, and opportunities. The Calendar screen renders a month grid with per-calendar and status filters, month navigation, and availability chips; owners and managers add availability windows. Appointments support create/edit with end-after-start validation, lead linking (which auto-links contact and opportunity), status transitions (scheduled/completed/cancelled/no-show), audit events, and projection into the unified activity timeline. A JSON slot API (`/api/v1/calendar/available-slots`) computes free 30-minute-aligned slots inside availability windows, excluding booked appointments. A default Appointments calendar with weekday 9–5 windows is seeded per organization. The suite now contains 32 passing tests, including calendar seeding, the appointment lifecycle with validation and audit, and availability/slot computation with booking exclusion. Backup: `data/leads.before-phase7.db`.

Phase 8 is implemented as migration `008_project_management`. It adds tenant-scoped projects linked to opportunities, leads, contacts, and properties; hierarchical project tasks with status columns (todo/in-progress/blocked/done), ordering, due dates, and assignees; and project comments. The Projects screen lists projects with progress bars and offers three disabled-by-default land templates (due diligence, closing, marketing launch) that clone into a project with pre-seeded tasks, plus blank projects. The project detail page renders a four-column task board with one-click status moves, subtask creation, comments, and a progress bar; project status transitions and task/comment changes are audited. The suite now contains 34 passing tests, including template cloning with task seeding, board transitions with audit, comments, project status, validation, and tenant isolation. Backup: `data/leads.before-phase8.db`.

Phase 9 is implemented as migration `009_communication_providers`. It formalizes the provider boundary in `communication_providers.py` with SmsProvider/CallProvider protocols and Vumber adapters wrapping the existing encrypted client, so the CRM routes never call provider internals directly. It adds a tenant-scoped number inventory (provider, label, kind, active), consent suppressions per lead and channel (sms/call/email/all) with lift/re-apply, and a deduplicated delivery-event ledger with unified states (queued/sent/delivered/failed/undelivered) recorded on outbound SMS. The Vumber text and call routes now refuse to contact a suppressed lead, and a Communication settings screen exposes inventory, suppressions, and recent delivery state. The suite now contains 37 passing tests, covering number inventory with toggles and validation, suppression enforcement through the guard, suppression lifting, audit events, and delivery deduplication. Backup: `data/leads.before-phase9.db`.

## Executive assessment

This repository is a useful local, single-user lead-intelligence application. It ingests Vumber notifications from Gmail, groups calls by the other party's number, transcribes recordings, creates a consolidated lead story and discount-oriented score, matches offer records, plays recordings, supports manual lead entry, drafts contextual SMS replies, and can initiate Vumber texts and callback calls. It is not yet a multi-user CRM platform.

The most important prerequisite for every team, automation, document, scheduling, and project feature is a real security boundary: organizations, users, roles, tenant-scoped records, persistent authentication, and audit events. The current application has no login, no user identity, no role checks, no tenant key, and only partial CSRF protection. Phase 1 therefore starts with that foundation. Existing records will be assigned to one bootstrap organization so no lead, call, task, transcript, or communication is lost.

Public Stride material corroborates the transcript's broad product direction—unified pipelines and inbox, automated follow-up, land-specific templates, scheduling, phone, analytics, role-based access, API access, mobile support, and e-signatures—but public marketing claims are not treated as implementation specifications.

## Repository evidence

| Area | Evidence | Current conclusion |
|---|---|---|
| Persistence | `crm_db.py`: SQLite tables `leads`, `calls`, `tasks`, `communications`, `message_drafts`, `processed_messages`, `sync_runs` | Durable local data, but no migration ledger, tenants, users, permissions, properties, opportunities, tags, files, calendars, documents, or workflow engine |
| Web UI/API | `crm_app.py`: server-rendered Flask routes for list/detail/new lead, edits, tasks, Vumber settings/actions, audio, and health | Cohesive local UI; no JSON API, webhooks, auth, pagination, board, inbox, admin, or platform navigation |
| Ingestion/intelligence | `lead_pipeline.py`: Gmail OAuth, Vumber parsing, recording download, transcription, identity/reference extraction, AI consolidation | Strong partial ingestion and intelligence; Gmail read-only and batch/CLI based |
| Offer data | `offer_db.py`: read-only normalized reference index over `all_fop_offers.db` | Strong reference lookup; offer row is not a first-class editable property/opportunity |
| Telephony | `vumber_client.py`: Windows-encrypted Vumber session, line discovery, SMS, callback calling | Working provider-specific outbound actions; no routing/number provisioning/browser VoIP/inbound webhook |
| Drafting | `message_drafts.py`: call/offer/lead-aware suggestions with OpenAI/OpenRouter and local fallback | Useful assistive drafting; not autonomous conversation AI |
| Tests | `tests/test_crm.py`: 14 integration/unit tests before this phase | Covers current lead, offer, audio, parsing, drafting and mocked Vumber paths; no security or tenancy tests |

## Feature-parity matrix

Status meanings: **complete** works end to end; **partial** has useful underlying behavior but misses required parts; **missing** has no implementation; **broken** exists but cannot meet its intended behavior; **unknown** cannot be verified from the repository/transcript.

| Feature and subfeatures | Transcript | Status | Repository evidence | Expected behavior and proposed implementation | Dependencies / considerations | Complexity | Priority |
|---|---:|---|---|---|---|---|---:|
| Acquisition/disposition dashboards | 00:34–01:06 | Partial | `crm_app.py` has four fixed counts and lead table | Named dashboard views for acquisitions/dispositions with scoped KPI widgets, date/team filters, pipeline value and health; add widget definitions and saved layouts | Accurate opportunity/value data; expensive queries need indexes/cache | L | 11 |
| User/role dashboards and configurable KPI widgets | 00:47–01:06 | Missing | No users or widget settings | Per-user/team layouts and permitted metrics | Requires tenancy/RBAC first | L | 11 |
| In-app help, tickets, onboarding, learning center | 01:07–01:52 | Missing | README only | Context help, local knowledge pages, ticket records/provider interface; do not claim 24/7 support without staffing | Support provider, retention and attachment rules | M | 11 |
| Contact management | 01:52–03:03 | Partial | `leads` table/list/detail/new | Separate contacts from deals/properties; multiple phones/emails/addresses, ownership relationships, tags, fields, consent/suppression | PII access, deletion/export and do-not-contact | L | 2 |
| CSV import: upload, preview, validation and mapping | 02:00–02:35 | Missing | No upload/import UI | Staged import batches, encoding/delimiter detection, preview, mapping, row errors, dry run and commit | Upload limits, formula injection, malware/PII handling | L | 2 |
| Duplicate handling and custom-field creation during import | 02:14–02:26 | Missing | Phone is globally unique | Tenant-scoped match rules, merge/skip/create choice, typed custom fields | Irreversible merges require preview/audit | L | 2 |
| Custom fields and tags | 02:14–03:01 | Missing | Fixed lead columns | Typed field definitions/values plus tag definitions/assignments, tenant scoped | Index/search strategy and schema validation | M | 2 |
| Saved filters and dynamic smart lists | 02:37–03:03 | Partial | Fixed query filters/sorts only | JSON filter AST over standard/custom/property/activity fields; named private/team lists | Query validation, permission-aware evaluation | L | 2 |
| Custom pipelines/stages | 03:03–04:14 | Partial | Fixed `STAGES` tuple and status check | Tenant-defined acquisition/disposition/owner-finance pipelines, ordered stages, stage rules | Migration from current six statuses | L | 3 |
| Drag-and-drop deal board | 03:43–03:54 | Missing | Table only | Accessible Kanban with optimistic move, server validation and keyboard fallback | Concurrency and ordering | M | 3 |
| Manual/automated stage transitions | 03:43–04:14 | Partial | Manual select only | Transition API, history event, workflow trigger, permissions | Idempotency and race control | M | 3/5 |
| Seller, property and communication context on deal | 03:49–03:54 | Partial | Lead detail, calls, offers, communications | First-class opportunity links contact(s), property(s), offers and unified events | Normalize offer database into app-owned property records | L | 3/4 |
| Workflow engine: triggers, conditions, delays, actions | 04:14–05:30 | Missing | No scheduler/queue | Versioned workflow definitions, enabled flag, runs, jobs, retries, idempotency and dead-letter state | Background worker, transactional outbox, rate limits | XL | 5 |
| Disabled land-specific workflow templates | 04:20–04:58 | Missing | None | Seed reviewed templates disabled; clone/publish explicitly | Prevent accidental outreach; legal review | M | 5 |
| Automated follow-ups and email autoresponders | 05:16–05:24 | Missing | Suggested SMS is manual | Workflow actions with consent/suppression, schedules and provider delivery events | Email/SMS provider; TCPA/CAN-SPAM and quiet hours | L | 5/9 |
| Plain-English AI workflow builder | 05:30–06:03 | Missing | AI only analyzes leads/drafts texts | AI produces a validated draft workflow AST; human review/publish required | Prompt injection, action allowlist, cost, evaluations | L | 10 |
| API/webhook lead ingestion | 06:16–06:30 | Missing | Gmail CLI ingestion only | Tenant API keys, scoped REST endpoint, signed webhooks, idempotency keys and delivery log | Secret rotation, quotas, abuse controls | L | 4/9 |
| Unified inbox: calls/SMS/email/documents/system activity | 06:30–07:20 | Partial | Calls plus CRM-originated SMS/call rows; no unified event model | One chronological event stream with channel filters, direction, bodies, files, delivery/read state, stage/audit events | Provider webhooks and immutable event IDs | L | 4 |
| Chronological contact timeline and full content | 06:43–07:20 | Partial | Call timeline and communication history are separate | Normalize and render all events in one ordered timeline | Pagination and PII controls | M | 4 |
| Email send/delivery/open tracking | 07:00–07:12 | Missing | No email provider | Provider abstraction, send state and webhook events; opens labeled as approximate | Privacy changes make open tracking unreliable | L | 9 |
| Browser calling | 07:32–07:58 | Partial | Vumber callback call rings the user's phone first | Keep callback provider; add optional WebRTC provider behind interface | Microphone permission, emergency-call disclaimer | L | 9 |
| Phone-number management/routing | 08:12–08:34 | Partial | Lists account lines and callback numbers | Number inventory, labels, assignments, schedules, routing rules and provider sync | Provider capability/API availability | L | 9 |
| Recording/transcription and speaker record | 07:58–08:12 | Partial | Recording ingestion, playback, transcription; direction known | Consent configuration, retention policy, recording event metadata and speaker labels where supported | Wiretap/consent laws vary by jurisdiction | M | 9 |
| Voice AI qualification | 14:04–15:42 | Missing | No realtime agent | Provider interface, knowledge base, script/versioning, disclosure, structured capture and handoff | High cost/risk, consent, hallucination, emergency escalation | XL | 10 |
| SMS/web conversation AI | 15:42–17:02 | Partial | Human-reviewed suggested texts only | Opt-in agent with human takeover, safe tools, transcript/events and structured capture | Consent, prompt injection and brand/legal controls | XL | 10 |
| AI knowledge bases | 14:19–14:32, 16:20–16:49 | Missing | Prompt strings in code only | Versioned sources/chunks, retrieval, test set, publish/rollback | File security and poisoning defenses | L | 10 |
| Structured AI capture into contacts/properties/opportunities | 15:02–15:08, 16:06–16:10 | Partial | Call AI updates lead summary/name/reference | Schema-validated extraction into proposed changes with provenance and review | Avoid silent destructive overwrites | L | 10 |
| Contract/document templates and merge fields | 08:34–09:24 | Missing | No document model | Tenant templates, immutable versions, allowlisted seller/buyer/property/transaction merge fields and preview | DOCX/PDF rendering, template sandbox | XL | 6 |
| Generate draft/send from contact or opportunity | 09:24–10:04 | Missing | None | Validate required fields, create immutable rendered artifact, draft vs explicit send | Transactional provider and confirmation | L | 6 |
| Document lifecycle states and viewed events | 08:50–09:01, 10:48–11:06 | Missing | None | Draft/sent/delivered/viewed/signed/declined/expired/completed with event ledger | Webhook signatures and idempotency | L | 6 |
| E-signatures, audit trail and executed storage | 10:14–11:21 | Missing | No files/signatures | Use compliant e-sign provider; store signed artifact hash, signer evidence and provider events | ESIGN/UETA, retention, encryption, legal review | XL | 6 |
| Contract-event automations | 10:31–10:48 | Missing | No workflow engine | Document events enter workflow outbox | Requires phases 5 and 6 | M | 6 |
| Forms and lead-capture widgets | 12:00–12:06 | Missing | Internal add-lead form only | Public form definitions/submissions, spam controls, consent, mapping and embed code | CAPTCHA, rate limiting, privacy notice | L | 10/11 |
| Website/landing-page management | 11:23–12:17 | Missing | Outside current local app scope | Defer; first support externally hosted forms/pages and publishing adapter | Hosting, content security, abuse and backups | XL | 11 |
| Custom domains/publishing | 12:17–12:35 | Missing | Localhost only | Defer until hosted architecture exists | DNS/TLS/domain ownership | XL | 11 |
| Mobile-responsive/PWA | 12:42–13:20 | Partial | Responsive CSS; no manifest/service worker/offline/push | Installable PWA shell, responsive board/inbox, safe caching and push provider | Never cache sensitive records indiscriminately | L | 11 |
| Calendar and Google/Microsoft sync | 13:20–13:40 | Missing | Follow-up date only | Calendar/appointment models, OAuth providers, sync cursors/conflict handling | OAuth verification and token encryption | XL | 7 |
| Multiple calendars, availability, booking, notifications | 13:40–13:58 | Missing | None | Calendar types, rules, time zones, booking pages, reminders/cancellations | DST, double-booking and spam | XL | 7 |
| Project management linked to CRM | 17:07–19:31 | Partial | Flat lead tasks only | Projects/templates linked to contacts/properties/opportunities; project members/status/progress | Requires users/permissions | XL | 8 |
| Project board/list/table/calendar views | 17:49–18:58 | Missing | None | Shared task query plus multiple views and saved preferences | Ordering and large-project performance | L | 8 |
| Subtasks, assignees, dependencies, comments, files, activity, time logs | 17:47–18:43 | Missing | Task title/due/status only | Normalized task graph, comments, attachments, work logs and audit events | File scanning/access and dependency cycles | XL | 8 |
| Land project templates | 17:32–17:39, 18:13–19:05 | Missing | None | Disabled templates for due diligence, closing, subdivision, improvements, construction and marketing | Subject-matter/legal review | M | 8 |

## Proposed information architecture

Primary navigation:

1. **Home** — role-aware acquisitions/dispositions dashboards.
2. **Contacts** — people/companies, tags, custom fields, imports and smart lists.
3. **Deals** — opportunities board, pipeline selector and deal detail.
4. **Inbox** — unified conversation list and chronological event timeline.
5. **Tasks & Projects** — personal tasks, project boards, table/list/calendar and templates.
6. **Documents** — templates, drafts, signature queue and completed documents.
7. **Calendar** — appointments, calendars, availability and booking links.
8. **Automations** — templates, workflow builder, runs and failures.
9. **Forms** — public lead-capture definitions and submissions.
10. **Reports** — dashboards, funnel/pipeline health, sources and team activity.
11. **Settings** — organization/team/roles, custom fields/tags, pipelines, phone/email/calendar providers, API/webhooks, AI knowledge and security.
12. **Help** — product guide, onboarding checklist and support tickets.

The current `leads` page remains available during migration and evolves into a Contacts/Deals compatibility view.

## Required schema changes

Foundation:

- `organizations`, `users`, `organization_memberships` (or a single organization FK initially), `roles/permissions` if roles become customizable.
- `organization_id` and appropriate composite indexes on all business tables.
- `audit_events`, `schema_migrations`, persistent session/auth configuration.

CRM core:

- `contacts`, `contact_points`, `addresses`, `properties`, `property_owners`, `opportunities`, `pipelines`, `pipeline_stages`, `stage_transitions`.
- `custom_field_definitions`, `custom_field_values`, `tags`, `entity_tags`, `saved_filters`.
- `import_batches`, `import_columns`, `import_rows`.
- `activity_events` as the immutable unified timeline; calls/communications retain provider-specific detail.

Later phases:

- Workflows: definitions/versions/triggers/steps/runs/jobs/outbox/dead letters.
- Documents: templates/versions/documents/recipients/events/files.
- Calendars: calendars/availability/appointments/provider connections/sync cursors.
- Projects: projects/members/columns/tasks/dependencies/comments/files/time logs/templates.
- Integrations: encrypted connections, API keys, webhook endpoints/deliveries and rate buckets.

## Required API changes

- Versioned tenant-scoped JSON API (`/api/v1`) with consistent validation/error envelopes.
- Session auth for browser UI; hashed, scoped and rotatable API keys for integrations.
- Idempotency keys for creates and every provider/webhook event.
- Cursor pagination and permission-aware filtering.
- Dedicated endpoints for import preview/commit, board transitions, activity feeds, workflow publishing/runs, document events, bookings and projects.
- Webhook signature verification plus replay prevention; outbound webhooks via a transactional outbox.
- Provider interfaces for telephony/SMS, email, calendar, e-sign, object storage and AI.

## Required UI changes

- Persistent app navigation and authenticated user/organization switcher.
- Accessible tables with pagination, bulk selection and saved views.
- Contact, property and opportunity pages with a shared activity timeline.
- Keyboard-accessible board movement in addition to drag and drop.
- Import wizard with preview/mapping/errors; admin builders for fields/tags/pipelines.
- Explicit loading, empty, error, retry and provider-disconnected states.
- Destructive actions require precise confirmation; outbound messages/contracts always show recipient and final content.
- Responsive layout first; PWA only after safe caching rules are defined.

## Migration and backward compatibility

1. Create a migration ledger and a single default organization.
2. Create a bootstrap owner via one-time setup; do not embed a default password.
3. Backfill every existing row to the default organization while preserving IDs and paths.
4. Keep current `leads`/`calls` behavior and URLs operational.
5. Add tenant filters before exposing additional organizations.
6. Introduce contacts/properties/opportunities alongside `leads`; backfill deterministically, verify counts/hashes, then convert `leads` into a compatibility boundary in a later migration.
7. Never modify `all_fop_offers.db`; continue treating it as a read-only source until explicitly importing selected offer rows.
8. Take timestamped SQLite backups before table-rebuild migrations and provide a dry-run validation report.

## Security, privacy, legal and operational baseline

- Deny by default across organizations; enforce tenant predicates server-side, never from hidden form values.
- Passwords use a modern adaptive hash; secrets/tokens remain encrypted at rest and excluded from logs/backups where appropriate.
- CSRF on browser mutations, secure session cookies, login throttling, output escaping, upload limits/type checks and safe file paths.
- Immutable audit events for authentication, permission changes, exports, merges, stage moves, messages, calls, workflows, documents and deletions.
- Consent/suppression/quiet-hour controls before automated SMS/calls/email. Recording and transcription require jurisdiction-aware consent and retention rules.
- E-sign functionality should use a vetted provider and counsel-reviewed evidence/retention flow; do not market signatures as legally binding solely because a checkbox exists.
- Email-open state is probabilistic and must be labeled accordingly.
- Backups, restore drills, provider retries, idempotency and failure queues are operational requirements, not optional polish.

## Testing strategy

- Unit tests for validators, permission matrix, merge fields, filter/workflow ASTs and state machines.
- Integration tests using two organizations to prove every list/detail/update/download route cannot cross tenants.
- Authentication tests for setup, login/logout, inactive users, throttling, CSRF and role-denied mutations.
- Migration tests on a copy shaped like the current database; verify row counts, IDs, FKs, unique rules and rollback/backups.
- Provider contract tests with fakes for success, validation errors, timeouts, duplicate events, retries and invalid signatures.
- UI tests for core paths: import, contact/property/deal, stage move, inbox reply, document draft/send, booking and projects.
- Accessibility checks for forms, tables, boards and keyboard actions; responsive checks at mobile/tablet/desktop widths.
- Never send a real text/call/email/contract from automated tests.

## Phased roadmap and acceptance criteria

### Phase 1: tenant, identity, permissions and audit foundation

Acceptance: one-time owner setup; login/logout; persistent sessions; owner/manager/agent/viewer policy; all business queries tenant-scoped; two-tenant isolation tests; CSRF and login throttling; audited mutations; existing data preserved under a bootstrap organization.

### Phase 2: contacts, properties, fields, tags, imports and smart lists

Acceptance: normalized contact/property records; typed fields/tags; previewable idempotent CSV import; duplicate merge policy; saved dynamic filters; import/audit/isolation tests.

### Phase 3: opportunities and pipelines

Acceptance: configurable pipelines/stages, accessible board and table, validated/audited transitions, property/contact links and history.

Status: **complete**. Covered by Phase 3 integration tests for backfill, board/detail rendering, lead-status compatibility, transition history/audit, stale-write rejection, pipeline creation, and tenant isolation.

### Phase 4: unified activity and inbox

Acceptance: one immutable chronological feed for existing calls/transcripts, SMS/calls, stage/audit events, later email/documents; pagination and channel filters.

Status: **complete**. Covered by integration tests for full untruncated transcripts, filtering, pagination, transition/audit projection, immutability enforcement, recording-ready metadata, and tenant isolation.

### Phase 5: workflow automation

Acceptance: versioned disabled templates; explicit publish; worker/outbox; triggers/conditions/delays/allowlisted actions; retries/idempotency/failure UI.

Status: **complete**. Covered by integration tests for disabled templates, explicit version publishing, safe allowlisted actions, no automatic sends, delayed jobs, idempotent receipts, retry/dead-letter behavior, manual retry, and workflow audit events.

### Phase 6: contracts and e-signatures

Acceptance: versioned templates, previewed merge, draft/send states, signed provider events, evidence/audit/storage and workflow triggers.

Status: **complete**. Covered by Phase 6 integration tests for template draft/publish immutability, unknown merge-field rejection, the full document lifecycle with hashed PDFs and event ledger, signed-PDF upload validation, activity projection, document event immutability, tenant isolation, and HMAC-signed idempotent webhook handling. Automated sending is deliberately out of scope: the local provider records sent state without transmitting email, and a compliant e-sign provider integration remains the recommended path for legally binding signatures.

### Phase 7: calendar and scheduling

Acceptance: internal calendars/appointments/availability first, then tested Google/Microsoft adapters and booking notifications.

Status: **complete** for the internal-first scope. Covered by Phase 7 integration tests for default calendar seeding, month-grid rendering, appointment create/edit/status with validation and audit, activity projection, and the availability slot API with booking exclusion. Google/Microsoft OAuth sync adapters and external booking notifications remain future work and are not claimed here.

### Phase 8: project management

Acceptance: CRM-linked projects, land templates, tasks/subtasks/dependencies/assignees/comments/files/time logs and board/list/table/calendar/progress views.

Status: **complete** for the core scope. Covered by Phase 8 integration tests for template-based project creation with task seeding, board status transitions with audit, comments, project status changes, input validation, and tenant isolation. File attachments, time logs, cross-task dependency enforcement, and calendar views for project tasks remain future enhancements and are not claimed here.

### Phase 9: communication providers

Acceptance: formal Vumber provider boundary, inbound status/events, email provider, routing/number configuration where supported, consent controls and unified delivery state.

Status: **complete** for the local scope. Covered by Phase 9 integration tests for the provider boundary adapters, number inventory management, consent suppression enforcement and lifting, audited consent changes, and deduplicated delivery-event recording. Inbound provider webhooks, an outbound email provider, and number provisioning/routing beyond inventory labels remain future work; the local Vumber provider records sent state without provider delivery confirmation.

### Phase 10: AI builders and agents

Acceptance: human-reviewed workflow generation and structured conversation capture; versioned knowledge, safe tool allowlists, evaluations, disclosure and handoff.

Status: **complete** for the assistive local scope. Workflow generation produces a pending proposal and can only create a disabled, unpublished draft after explicit review. Conversation capture proposes allowlisted lead fields with evidence and applies only reviewer-selected facts. Agent instructions, disclosures, fixed tool allowlists, and knowledge are versioned; regression evaluations and explicit human handoffs cover key safety cases. Realtime voice agents and autonomous outbound communication are deliberately not enabled.

### Phase 11: dashboards, help, forms/sites and PWA

Acceptance: configurable role dashboards, reporting, help/tickets, public forms, mobile installability. Hosting/custom domains/site builder remain separate architecture decisions and are not implied by a local Flask deployment.

Status: **complete** for the local scope. Users can save role-aware workspace layouts; acquisition reports cover pipeline, calls, sources, and follow-up health with saved views; the help center includes operating articles and audited ticket lifecycles; public seller forms validate, deduplicate, and preserve submissions as CRM leads; and manifest/service-worker assets support standalone installation with a privacy-safe offline page. External hosting, custom domains, and a general-purpose site builder are not claimed.

### Phase 12: integration API and inbound webhooks

Acceptance: hashed, scoped and revocable API keys; tenant-scoped lead reads and creates; required idempotency keys with conflict detection and stored replay responses; signed, timestamp-bounded and deduplicated lead webhooks; validation, request limits, attempt logs, audit events, and secret-safe administration.

Status: **complete** for inbound lead integration. API keys expose only their prefix after one-time creation and webhook secrets are DPAPI-encrypted. Lead creates reuse the existing phone deduplication and reference/name safety logic. Automated outbound webhooks, a broader CRUD surface, and externally hosted ingress remain future work.

## Immediate implementation decision

Begin Phase 1 in this repository. Do not start workflows, documents, calendars, or projects until identity and tenant isolation exist. This is the only sequence that prevents later features from baking single-user assumptions into every table and route.
