"""Phase 5 workflow automation builder, publishing, and operations UI."""

from __future__ import annotations

import json

from flask import abort, flash, g, redirect, render_template_string, request, url_for

from crm_db import utc_now
from workflow_engine import ALLOWED_TRIGGERS, validate_definition


PHASE5_STYLE = """
.automation-card{border-left:5px solid var(--line)}.automation-card.enabled{border-left-color:var(--accent)}.automation-card.template{border-left-color:#7a6a9b}.automation-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}.automation-meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}.run-row.failed td{background:#fff1ef}.code-preview{background:#f7f7f3;border-radius:9px;padding:12px;white-space:pre-wrap}.action-builder{border:1px solid var(--line);border-radius:10px;padding:13px;margin:10px 0}.safety{border-left:4px solid var(--accent);padding:12px;background:#f0f8f3;border-radius:6px;margin:14px 0}@media(max-width:800px){.automation-head{display:block}}
"""


LIST_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Automations · CRM</title><style>{{style}}{{phase5_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Automations</h1><div class="sub">Versioned, reviewable follow-up workflows</div></div>{% if current_user.role in ('owner','manager') %}<a class="btn" href="{{url_for('new_workflow')}}">+ New workflow</a><a class="btn secondary" href="{{url_for('ai_workflow_builder')}}">AI builder</a>{% endif %}</header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="safety"><b>Human-review safety:</b> automations can create tasks and prepare editable text drafts. They cannot send a text or place a call.</div><section class="stats"><div class="stat"><b>{{stats.enabled}}</b><span>Enabled workflows</span></div><div class="stat"><b>{{stats.pending}}</b><span>Pending events/jobs</span></div><div class="stat"><b>{{stats.succeeded}}</b><span>Successful runs</span></div><div class="stat"><b>{{stats.failed}}</b><span>Failed runs/jobs</span></div></section><div class="grid"><section class="stack"><h2>Your workflows</h2>{% for item in workflows if not item.is_template %}<article class="panel automation-card {{'enabled' if item.enabled else ''}}"><div class="automation-head"><div><a class="leadlink" href="{{url_for('workflow_detail',workflow_id=item.id)}}">{{item.name}}</a><div class="muted">{{item.description or 'No description'}}</div><div class="automation-meta"><span class="pill">{{item.trigger_label}}</span><span class="pill">{{'Version ' ~ item.published_version if item.published_version else 'Draft only'}}</span><span class="pill">{{'Enabled' if item.enabled else 'Disabled'}}</span></div></div><a class="btn secondary" href="{{url_for('workflow_detail',workflow_id=item.id)}}">Open</a></div></article>{% else %}<div class="panel empty">No custom workflows yet. Clone a safe template or create one.</div>{% endfor %}</section><aside class="stack"><h2>Disabled templates</h2>{% for item in workflows if item.is_template %}<article class="panel automation-card template"><b>{{item.name}}</b><div class="muted">{{item.description}}</div><div class="automation-meta"><span class="pill">Disabled</span><span class="pill">{{item.trigger_label}}</span></div>{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('clone_workflow',workflow_id=item.id)}}" style="margin-top:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Clone as draft</button></form>{% endif %}</article>{% endfor %}</aside></div><section class="panel" style="margin-top:18px"><h2>Recent runs</h2><div class="table-wrap"><table><thead><tr><th>Time</th><th>Workflow</th><th>Lead</th><th>Status</th><th>Version</th><th>Error</th><th></th></tr></thead><tbody>{% for run in runs %}<tr class="{{'failed' if run.status=='failed' else ''}}"><td>{{run.created_at}}</td><td>{{run.workflow_name}}</td><td>{% if run.lead_id %}<a href="{{url_for('lead_detail',lead_id=run.lead_id)}}">{{run.owner_name or run.phone_display}}</a>{% else %}—{% endif %}</td><td><span class="pill">{{run.status}}</span></td><td>{{run.workflow_version}}</td><td>{{run.error or run.job_error}}</td><td>{% if run.status=='failed' and current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('retry_workflow_run',run_id=run.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Retry</button></form>{% endif %}</td></tr>{% else %}<tr><td colspan="7" class="empty">No workflow runs yet.</td></tr>{% endfor %}</tbody></table></div></section></main></body></html>
"""


DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{workflow.name}} · Automation</title><style>{{style}}{{phase5_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('automations')}}">← Automations</a><h1 style="margin-top:8px">{{workflow.name}}</h1><div class="sub">{{'Published version ' ~ workflow.published_version if workflow.published_version else 'Unpublished draft'}} · {{'Enabled' if workflow.enabled else 'Disabled'}}</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="safety">Publishing never authorizes outbound sending. Text actions only prepare a draft for review on the lead page.</div><div class="grid"><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Draft definition</h2><label class="field">Name<input name="name" value="{{workflow.name}}" required></label><label class="field">Description<textarea name="description">{{workflow.description}}</textarea></label><div class="two-col"><label class="field">Trigger<select name="trigger_type">{% for value,label in triggers %}<option value="{{value}}" {% if definition.trigger.type==value %}selected{% endif %}>{{label}}</option>{% endfor %}</select></label><label class="field">Required destination stage<select name="stage_key"><option value="">Any stage</option>{% for value in stages %}<option value="{{value}}" {% if definition.trigger.get('stage_key')==value %}selected{% endif %}>{{value|title}}</option>{% endfor %}</select></label></div><div class="two-col"><label class="field">Minimum priority<select name="min_score"><option value="">Any</option>{% for score in range(1,6) %}<option value="{{score}}" {% if definition.conditions.get('min_score')==score %}selected{% endif %}>{{score}}</option>{% endfor %}</select></label><label class="field">Lead stage condition<select name="condition_status"><option value="">Any stage</option>{% for value in stages %}<option value="{{value}}" {% if definition.conditions.get('status')==value %}selected{% endif %}>{{value|title}}</option>{% endfor %}</select></label></div><label class="check"><input type="checkbox" name="has_reference" value="1" {% if definition.conditions.get('has_reference') %}checked{% endif %}>Only run when a reference number exists</label><div class="two-col"><label class="field">Delay amount<input type="number" min="0" max="720" name="delay_amount" value="{{delay_amount}}"></label><label class="field">Delay unit<select name="delay_unit"><option value="minutes" {% if delay_unit=='minutes' %}selected{% endif %}>Minutes</option><option value="hours" {% if delay_unit=='hours' %}selected{% endif %}>Hours</option><option value="days" {% if delay_unit=='days' %}selected{% endif %}>Days</option></select></label></div><h2>Actions</h2>{% for index in (0,1) %}{% set action=definition.actions[index] if definition.actions|length>index else {} %}<div class="action-builder"><label class="field">Action {{index+1}}<select name="action_{{index}}_type"><option value="" {% if not action %}selected{% endif %}>No action</option><option value="create_task" {% if action.type=='create_task' %}selected{% endif %}>Create follow-up task</option><option value="prepare_sms_draft" {% if action.type=='prepare_sms_draft' %}selected{% endif %}>Prepare text draft</option></select></label><label class="field">Task title or text message<textarea name="action_{{index}}_content" placeholder="Use {name}, {first_name}, {phone}, {reference}, or {status}">{{action.title if action.type=='create_task' else action.message if action else ''}}</textarea></label><label class="field">Draft angle (text drafts only)<input name="action_{{index}}_angle" value="{{action.angle if action.type=='prepare_sms_draft' else ''}}"></label></div>{% endfor %}{% if current_user.role in ('owner','manager') %}<button class="btn">Save draft</button>{% endif %}</form><aside class="stack"><section class="panel"><h2>Publish controls</h2><p class="muted">Saving changes only updates the draft. Publishing creates an immutable version and enables it explicitly.</p>{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('publish_workflow',workflow_id=workflow.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="check"><input type="checkbox" name="confirmed" value="yes" required>I reviewed this workflow and want to publish and enable it.</label><button class="btn">Publish new version</button></form>{% if workflow.published_version %}<form method="post" action="{{url_for('toggle_workflow',workflow_id=workflow.id)}}" style="margin-top:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">{{'Disable' if workflow.enabled else 'Enable published version'}}</button></form>{% endif %}{% endif %}</section><section class="panel"><h2>Published versions</h2>{% for version in versions %}<details><summary>Version {{version.version}} · {{version.created_at}}</summary><pre class="code-preview">{{version.pretty}}</pre></details>{% else %}<div class="muted">Not published yet.</div>{% endfor %}</section></aside></div></main></body></html>
"""


TRIGGERS = (
    ("lead_created", "New lead added"), ("incoming_call", "Incoming call"),
    ("missed_call", "Missed call"), ("sms_sent", "Text sent"),
    ("stage_changed", "Deal stage changed"), ("activity_added", "Any activity"),
    ("document_sent", "Document sent"), ("document_viewed", "Document viewed"),
    ("document_signed", "Document signed"), ("document_declined", "Document declined"),
)


def _definition_from_form() -> dict:
    amount = max(0, int(request.form.get("delay_amount") or 0))
    multiplier = {"minutes": 1, "hours": 60, "days": 1440}.get(request.form.get("delay_unit"), 1)
    conditions = {}
    if request.form.get("min_score", "").isdigit():
        conditions["min_score"] = int(request.form["min_score"])
    if request.form.get("condition_status"):
        conditions["status"] = request.form["condition_status"]
    if request.form.get("has_reference") == "1":
        conditions["has_reference"] = True
    actions = []
    for index in (0, 1):
        action_type = request.form.get(f"action_{index}_type", "")
        content = request.form.get(f"action_{index}_content", "").strip()
        if action_type == "create_task" and content:
            actions.append({"type": action_type, "title": content})
        elif action_type == "prepare_sms_draft" and content:
            actions.append({"type": action_type, "angle": request.form.get(f"action_{index}_angle", "").strip(), "message": content})
    return validate_definition({
        "trigger": {"type": request.form.get("trigger_type"), "stage_key": request.form.get("stage_key", "")},
        "conditions": conditions, "delay_minutes": amount * multiplier, "actions": actions,
    })


def register_phase5(app, db, style, rows, organization_id, require_csrf, roles_required, audit, stages):
    @app.get("/automations")
    def automations():
        workflow_rows = rows("SELECT * FROM workflow_definitions WHERE organization_id=? ORDER BY is_template,name", (organization_id(),))
        trigger_labels = dict(TRIGGERS)
        for item in workflow_rows:
            try:
                item["trigger_label"] = trigger_labels.get(json.loads(item["draft_json"])["trigger"]["type"], "Unknown trigger")
            except (KeyError, TypeError, json.JSONDecodeError):
                item["trigger_label"] = "Invalid draft"
        recent = rows("""SELECT r.*,w.name workflow_name,l.owner_name,l.phone_display,
            (SELECT last_error FROM workflow_jobs WHERE run_id=r.id AND last_error!='' ORDER BY id LIMIT 1) job_error
            FROM workflow_runs r JOIN workflow_definitions w ON w.id=r.workflow_id
            LEFT JOIN leads l ON l.id=r.lead_id AND l.organization_id=r.organization_id
            WHERE r.organization_id=? ORDER BY r.id DESC LIMIT 100""", (organization_id(),))
        stats = rows("""SELECT
            (SELECT COUNT(*) FROM workflow_definitions WHERE organization_id=? AND enabled=1) enabled,
            (SELECT COUNT(*) FROM workflow_outbox WHERE organization_id=? AND status IN ('pending','processing','failed'))+
            (SELECT COUNT(*) FROM workflow_jobs WHERE organization_id=? AND status IN ('queued','waiting','processing')) pending,
            (SELECT COUNT(*) FROM workflow_runs WHERE organization_id=? AND status='succeeded') succeeded,
            (SELECT COUNT(*) FROM workflow_runs WHERE organization_id=? AND status='failed')+
            (SELECT COUNT(*) FROM workflow_jobs WHERE organization_id=? AND status='dead') failed""",
            (organization_id(),) * 6)[0]
        worker = rows("SELECT * FROM workflow_worker_state WHERE worker_name='default'")
        if worker:
            flash(f"Workflow worker {worker[0]['status']}; last check {worker[0]['last_seen_at']}.", "good")
        else:
            flash("Workflow worker is not detected. Start workflow_worker.py before enabling workflows.", "error")
        return render_template_string(LIST_TEMPLATE, style=style, phase5_style=PHASE5_STYLE, workflows=workflow_rows, runs=recent, stats=stats)

    @app.route("/automations/new", methods=["GET", "POST"])
    @roles_required("owner", "manager")
    def new_workflow():
        if request.method == "GET":
            return render_template_string(
                """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>New workflow · CRM</title><style>{{style}}</style></head><body><main class="shell" style="max-width:620px"><a class="muted" href="{{url_for('automations')}}">← Automations</a><form class="panel" method="post" style="margin-top:18px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h1>New workflow</h1><p class="muted">This creates a disabled draft. It cannot run until you review and publish it.</p><button class="btn">Create disabled draft</button></form></main></body></html>""",
                style=style,
            )
        require_csrf()
        now = utc_now()
        definition = {"trigger": {"type": "lead_created"}, "conditions": {}, "delay_minutes": 0, "actions": [{"type": "create_task", "title": "Review new lead: {name}"}]}
        with db.connect() as connection:
            base, suffix = "New workflow", 2
            name = base
            while connection.execute("SELECT 1 FROM workflow_definitions WHERE organization_id=? AND name=?", (organization_id(), name)).fetchone():
                name, suffix = f"{base} {suffix}", suffix + 1
            cursor = connection.execute("""INSERT INTO workflow_definitions(
                organization_id,name,description,draft_json,enabled,is_template,created_by,created_at,updated_at
            ) VALUES (?,?,?, ?,0,0,?,?,?)""", (organization_id(), name, "", json.dumps(definition, sort_keys=True), g.user.get("id"), now, now))
        audit("workflow.create", "workflow", cursor.lastrowid)
        return redirect(url_for("workflow_detail", workflow_id=cursor.lastrowid))

    @app.route("/automations/<int:workflow_id>", methods=["GET", "POST"])
    def workflow_detail(workflow_id: int):
        found = rows("SELECT * FROM workflow_definitions WHERE id=? AND organization_id=? AND is_template=0", (workflow_id, organization_id()))
        if not found:
            abort(404)
        workflow = found[0]
        if request.method == "POST":
            if g.user["role"] not in {"owner", "manager"}:
                abort(403)
            require_csrf()
            try:
                definition = _definition_from_form()
            except (ValueError, TypeError) as exc:
                flash(str(exc), "error")
            else:
                name = request.form.get("name", "").strip()[:100]
                if not name:
                    flash("Workflow name is required.", "error")
                else:
                    try:
                        with db.connect() as connection:
                            connection.execute("""UPDATE workflow_definitions SET name=?,description=?,draft_json=?,updated_at=?
                                WHERE id=? AND organization_id=?""", (name, request.form.get("description", "").strip()[:500], json.dumps(definition, sort_keys=True), utc_now(), workflow_id, organization_id()))
                        audit("workflow.draft_save", "workflow", workflow_id)
                        flash("Draft saved. The published version is unchanged.", "good")
                        return redirect(url_for("workflow_detail", workflow_id=workflow_id))
                    except Exception as exc:
                        if "UNIQUE constraint" not in str(exc):
                            raise
                        flash("A workflow with that name already exists.", "error")
            workflow = rows("SELECT * FROM workflow_definitions WHERE id=? AND organization_id=?", (workflow_id, organization_id()))[0]
        definition = validate_definition(json.loads(workflow["draft_json"]))
        versions = rows("SELECT * FROM workflow_versions WHERE workflow_id=? AND organization_id=? ORDER BY version DESC", (workflow_id, organization_id()))
        for version in versions:
            version["pretty"] = json.dumps(json.loads(version["definition_json"]), indent=2)
        delay = definition["delay_minutes"]
        if delay and delay % 1440 == 0:
            delay_amount, delay_unit = delay // 1440, "days"
        elif delay and delay % 60 == 0:
            delay_amount, delay_unit = delay // 60, "hours"
        else:
            delay_amount, delay_unit = delay, "minutes"
        return render_template_string(DETAIL_TEMPLATE, style=style, phase5_style=PHASE5_STYLE, workflow=workflow, definition=definition, versions=versions, triggers=TRIGGERS, stages=stages, delay_amount=delay_amount, delay_unit=delay_unit)

    @app.post("/automations/<int:workflow_id>/publish")
    @roles_required("owner", "manager")
    def publish_workflow(workflow_id: int):
        require_csrf()
        if request.form.get("confirmed") != "yes":
            abort(400)
        with db.connect() as connection:
            workflow = connection.execute("SELECT * FROM workflow_definitions WHERE id=? AND organization_id=? AND is_template=0", (workflow_id, organization_id())).fetchone()
            if not workflow:
                abort(404)
            definition = validate_definition(json.loads(workflow["draft_json"]))
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM workflow_versions WHERE workflow_id=?", (workflow_id,)).fetchone()[0]
            connection.execute("""INSERT INTO workflow_versions(organization_id,workflow_id,version,definition_json,published_by,created_at)
                VALUES (?,?,?,?,?,?)""", (organization_id(), workflow_id, version, json.dumps(definition, sort_keys=True), g.user.get("id"), utc_now()))
            connection.execute("UPDATE workflow_definitions SET published_version=?,enabled=1,updated_at=? WHERE id=?", (version, utc_now(), workflow_id))
        audit("workflow.publish", "workflow", workflow_id, {"version": version})
        flash(f"Version {version} published and enabled.", "good")
        return redirect(url_for("workflow_detail", workflow_id=workflow_id))

    @app.post("/automations/<int:workflow_id>/toggle")
    @roles_required("owner", "manager")
    def toggle_workflow(workflow_id: int):
        require_csrf()
        with db.connect() as connection:
            workflow = connection.execute("SELECT * FROM workflow_definitions WHERE id=? AND organization_id=? AND is_template=0", (workflow_id, organization_id())).fetchone()
            if not workflow:
                abort(404)
            if not workflow["published_version"]:
                abort(400)
            enabled = int(not workflow["enabled"])
            connection.execute("UPDATE workflow_definitions SET enabled=?,updated_at=? WHERE id=?", (enabled, utc_now(), workflow_id))
        audit("workflow.enable" if enabled else "workflow.disable", "workflow", workflow_id)
        flash("Workflow enabled." if enabled else "Workflow disabled.", "good")
        return redirect(url_for("workflow_detail", workflow_id=workflow_id))

    @app.post("/automations/<int:workflow_id>/clone")
    @roles_required("owner", "manager")
    def clone_workflow(workflow_id: int):
        require_csrf()
        with db.connect() as connection:
            source = connection.execute("SELECT * FROM workflow_definitions WHERE id=? AND organization_id=?", (workflow_id, organization_id())).fetchone()
            if not source:
                abort(404)
            base = source["name"].replace("Template: ", "").strip() + " copy"
            name, suffix = base, 2
            while connection.execute("SELECT 1 FROM workflow_definitions WHERE organization_id=? AND name=?", (organization_id(), name)).fetchone():
                name, suffix = f"{base} {suffix}", suffix + 1
            cursor = connection.execute("""INSERT INTO workflow_definitions(
                organization_id,name,description,draft_json,enabled,is_template,created_by,created_at,updated_at
            ) VALUES (?,?,?,?,0,0,?,?,?)""", (organization_id(), name, source["description"], source["draft_json"], g.user.get("id"), utc_now(), utc_now()))
        audit("workflow.clone", "workflow", cursor.lastrowid, {"source_id": workflow_id})
        flash("Template cloned as a disabled draft. Review it before publishing.", "good")
        return redirect(url_for("workflow_detail", workflow_id=cursor.lastrowid))

    @app.post("/automations/runs/<int:run_id>/retry")
    @roles_required("owner", "manager")
    def retry_workflow_run(run_id: int):
        require_csrf()
        with db.connect() as connection:
            run = connection.execute("SELECT * FROM workflow_runs WHERE id=? AND organization_id=?", (run_id, organization_id())).fetchone()
            if not run:
                abort(404)
            connection.execute("""UPDATE workflow_jobs SET status='queued',attempts=0,available_at=?,locked_at=NULL,last_error='',updated_at=?
                WHERE run_id=? AND status IN ('failed','dead')""", (utc_now(), utc_now(), run_id))
            connection.execute("UPDATE workflow_runs SET status='queued',error='',finished_at=NULL WHERE id=?", (run_id,))
        audit("workflow.run_retry", "workflow_run", run_id)
        flash("Failed actions queued for retry.", "good")
        return redirect(url_for("automations"))
