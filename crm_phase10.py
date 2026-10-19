"""Phase 10: human-reviewed AI workflow and conversation capture."""

from __future__ import annotations

import hashlib
import json

from flask import abort, flash, g, redirect, render_template_string, request, url_for

from ai_builders import capture_conversation, evaluate_case, generate_workflow_draft, search_knowledge
from crm_db import utc_now
from lead_pipeline import sanitize_owner_name

PHASE10_STYLE = """
.ai-note{border-left:4px solid #6d5a91;background:#f5f1fb;padding:12px;border-radius:7px}.danger-note{border-left-color:#b63f36;background:#fff1ef}.ai-grid{display:grid;grid-template-columns:1.35fr .65fr;gap:18px}.codebox{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f7f3;border:1px solid var(--line);border-radius:8px;padding:12px}.fact{padding:12px 0;border-bottom:1px solid var(--line)}.tools{display:flex;gap:5px;flex-wrap:wrap}.tools code{font-size:11px;background:#eeeaf5;padding:3px 6px;border-radius:99px}@media(max-width:850px){.ai-grid{grid-template-columns:1fr}}
"""
FLASHES = """{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}"""

DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Assist · CRM</title><style>{{style}}{{phase10_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1>AI Assist</h1><div class="sub">Reviewed suggestions, versioned instructions, and human handoffs</div></div><div class="top-actions"><a class="btn" href="{{url_for('ai_workflow_builder')}}">Draft workflow</a><a class="btn secondary" href="{{url_for('knowledge_settings')}}">Knowledge</a></div></header>""" + FLASHES + """
<div class="ai-note"><b>Assistive only.</b> AI cannot send a message, place a call, publish a workflow, or silently change a lead. Suggestions remain pending until a team member approves them.</div>
<div class="ai-grid" style="margin-top:18px"><section class="stack"><div class="panel"><div class="top"><h2>Pending review</h2><span class="pill">{{workflow_proposals|length + capture_proposals|length}}</span></div><h3>Workflow proposals</h3>{% for p in workflow_proposals %}<div class="fact"><a class="leadlink" href="{{url_for('ai_workflow_proposal',proposal_id=p.id)}}">{{p.prompt[:100]}}</a><div class="muted">{{p.provider}} · {{p.created_at}}</div></div>{% else %}<div class="muted">No workflow proposals waiting.</div>{% endfor %}<h3>Call capture proposals</h3>{% for p in capture_proposals %}<div class="fact"><a class="leadlink" href="{{url_for('ai_capture_proposal',proposal_id=p.id)}}">{{p.owner_name or p.phone_display}} · call {{p.call_id}}</a><div class="muted">{{p.created_at}}</div></div>{% else %}<div class="muted">No call proposals waiting.</div>{% endfor %}</div>
<div class="panel"><h2>Recent calls to review</h2>{% for c in calls %}<div class="fact"><div class="top"><div><a class="leadlink" href="{{url_for('lead_detail',lead_id=c.lead_id)}}">{{c.owner_name or c.phone_display}}</a><div class="muted">{{c.occurred_at or c.created_at}} · {{c.direction}}</div></div>{% if c.transcript %}<form method="post" action="{{url_for('create_capture_proposal',call_id=c.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Analyze transcript</button></form>{% else %}<span class="muted">No transcript</span>{% endif %}</div></div>{% else %}<div class="muted">No calls available.</div>{% endfor %}</div></section>
<aside class="stack"><div class="panel"><h2>Agents</h2>{% for a in agents %}<div class="fact"><a class="leadlink" href="{{url_for('ai_agent_detail',agent_id=a.id)}}">{{a.name}}</a><div class="muted">{{a.agent_type|replace('_',' ')}} · version {{a.published_version or 'draft'}}</div><div class="tools">{% for tool in a.tools %}<code>{{tool}}</code>{% endfor %}</div></div>{% endfor %}</div><div class="panel"><h2>Human handoffs</h2>{% for h in handoffs %}<div class="fact"><b>{{h.priority|upper}}</b> · <a href="{{url_for('lead_detail',lead_id=h.lead_id)}}">{{h.owner_name or h.phone_display}}</a><div>{{h.reason}}</div><form method="post" action="{{url_for('resolve_ai_handoff',handoff_id=h.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Resolve</button></form></div>{% else %}<div class="muted">No open handoffs.</div>{% endfor %}</div><div class="panel"><h2>Evaluations</h2><div class="muted">Latest: {{eval_passed}} / {{eval_total}} passed</div>{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('run_ai_evaluations')}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Run safety checks</button></form>{% endif %}</div></aside></div></main></body></html>"""

BUILDER = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Workflow proposal · CRM</title><style>{{style}}{{phase10_style}}</style></head><body><main class="shell" style="max-width:850px"><a class="muted" href="{{url_for('ai_dashboard')}}">← AI Assist</a><h1>Plain-English workflow proposal</h1>""" + FLASHES + """<div class="ai-note">This creates a pending proposal—not a running workflow. Acceptance creates a disabled editable draft; publishing is a separate manual action.</div><form class="panel" method="post" style="margin-top:16px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">What should happen?<textarea name="description" required placeholder="After a missed call wait 10 minutes, prepare a text draft and create a callback task.">{{description}}</textarea></label><button class="btn">Generate proposal</button></form></main></body></html>"""

WORKFLOW_REVIEW = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Review workflow · CRM</title><style>{{style}}{{phase10_style}}</style></head><body><main class="shell" style="max-width:900px"><a class="muted" href="{{url_for('ai_dashboard')}}">← AI Assist</a><h1>Review workflow proposal</h1>""" + FLASHES + """<div class="ai-note">Provider: {{proposal.provider}} · status: <b>{{proposal.status}}</b>. Accepting creates a disabled draft only.</div><div class="panel"><h2>Request</h2><p>{{proposal.prompt}}</p><h2>Validated proposal</h2><pre class="codebox">{{pretty}}</pre>{% if proposal.status=='pending' %}<div class="actions"><form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="decision" value="accept"><button class="btn">Accept as disabled draft</button></form><form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="decision" value="reject"><button class="btn secondary">Reject</button></form></div>{% elif proposal.workflow_id %}<a class="btn" href="{{url_for('workflow_detail',workflow_id=proposal.workflow_id)}}">Open workflow draft</a>{% endif %}</div></main></body></html>"""

CAPTURE_REVIEW = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Review call capture · CRM</title><style>{{style}}{{phase10_style}}</style></head><body><main class="shell" style="max-width:980px"><a class="muted" href="{{url_for('ai_dashboard')}}">← AI Assist</a><h1>Review call capture</h1>""" + FLASHES + """<div class="ai-note">No lead fields have changed. Select only facts you personally verified against the evidence.</div>{% if handoff %}<div class="ai-note danger-note"><b>Human handoff recommended ({{handoff.priority}}):</b> {{handoff.reason}}<div class="muted">{{handoff.evidence}}</div></div>{% endif %}<div class="ai-grid" style="margin-top:16px"><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Proposed fields</h2>{% for field,value in proposed.items() %}<label class="fact check"><input type="checkbox" name="fields" value="{{field}}"><span><b>{{field|replace('_',' ')|title}}</b><div>{{value}}</div>{% if evidence.get(field) %}<div class="muted">Evidence: “{{evidence[field].quote}}” · {{evidence[field].confidence}}</div>{% endif %}</span></label>{% else %}<div class="muted">No structured facts found.</div>{% endfor %}{% if handoff %}<label class="check"><input type="checkbox" name="create_handoff" value="yes"><span>Create this human handoff</span></label>{% endif %}{% if proposal.status=='pending' %}<div class="actions"><button class="btn" name="decision" value="accept">Apply selected fields</button><button class="btn secondary" name="decision" value="reject">Reject proposal</button></div>{% else %}<div class="status-note">This proposal was {{proposal.status}}.</div>{% endif %}</form><aside class="panel"><h2>Full transcript</h2><div class="codebox" style="max-height:650px;overflow:auto">{{call.transcript}}</div></aside></div></main></body></html>"""

AGENT = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{agent.name}} · CRM</title><style>{{style}}{{phase10_style}}</style></head><body><main class="shell" style="max-width:900px"><a class="muted" href="{{url_for('ai_dashboard')}}">← AI Assist</a><h1>{{agent.name}}</h1>""" + FLASHES + """<div class="ai-note">Published versions are immutable. Tool access is restricted to the fixed allowlist shown below.</div><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Instructions<textarea name="instructions" style="min-height:180px">{{agent.draft_instructions}}</textarea></label><label class="field">Disclosure<textarea name="disclosure">{{agent.disclosure_text}}</textarea></label><div class="tools">{% for tool in tools %}<code>{{tool}}</code>{% endfor %}</div>{% if current_user.role in ('owner','manager') %}<div class="actions"><button class="btn secondary" name="action" value="save">Save draft</button><button class="btn" name="action" value="publish">Publish new version</button></div>{% endif %}</form><div class="panel"><h2>Version history</h2>{% for v in versions %}<div class="fact">Version {{v.version}} · {{v.created_at}}</div>{% endfor %}</div></main></body></html>"""

KNOWLEDGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI knowledge · CRM</title><style>{{style}}{{phase10_style}}</style></head><body><main class="shell"><a class="muted" href="{{url_for('ai_dashboard')}}">← AI Assist</a><h1>Versioned knowledge</h1>""" + FLASHES + """<div class="ai-grid"><section class="stack">{% for item in sources %}<form class="panel" method="post" action="{{url_for('edit_knowledge_base',base_id=item.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>{{item.name}} <span class="pill">v{{item.published_version or 'draft'}}</span></h2><label class="field">Draft content<textarea name="content" style="min-height:240px">{{item.draft_content}}</textarea></label>{% if current_user.role in ('owner','manager') %}<div class="actions"><button class="btn secondary" name="action" value="save">Save draft</button><button class="btn" name="action" value="publish">Publish version</button></div>{% endif %}</form>{% endfor %}</section><aside><form class="panel" method="get"><h2>Search published knowledge</h2><label class="field">Query<input name="q" value="{{query}}"></label><button class="btn secondary">Search</button>{% for r in results %}<div class="fact"><b>{{r.name}}</b><div>{{r.content[:500]}}</div></div>{% endfor %}</form></aside></div></main></body></html>"""


def register_phase10(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def one(sql, params):
        found = rows(sql, params)
        if not found:
            abort(404)
        return found[0]

    @app.get("/ai")
    @roles_required("owner", "manager", "agent", "viewer")
    def ai_dashboard():
        agents = [dict(a) for a in rows("SELECT * FROM ai_agents WHERE organization_id=? ORDER BY agent_type", (organization_id(),))]
        for agent in agents:
            agent["tools"] = json.loads(agent["tool_allowlist_json"] or "[]")
        workflow_proposals = rows("SELECT * FROM ai_workflow_proposals WHERE organization_id=? AND status='pending' ORDER BY id DESC", (organization_id(),))
        capture_proposals = rows("""SELECT p.*,l.owner_name,l.phone_display FROM ai_capture_proposals p JOIN leads l ON l.id=p.lead_id WHERE p.organization_id=? AND p.status='pending' ORDER BY p.id DESC""", (organization_id(),))
        calls = rows("""SELECT c.*,l.owner_name,l.phone_display FROM calls c JOIN leads l ON l.id=c.lead_id WHERE c.organization_id=? ORDER BY COALESCE(c.occurred_at,c.created_at) DESC LIMIT 12""", (organization_id(),))
        handoffs = rows("""SELECT h.*,l.owner_name,l.phone_display FROM ai_handoffs h JOIN leads l ON l.id=h.lead_id WHERE h.organization_id=? AND h.status='open' ORDER BY CASE h.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,h.id""", (organization_id(),))
        latest = rows("SELECT passed FROM ai_evaluation_results WHERE organization_id=? ORDER BY id DESC LIMIT 4", (organization_id(),))
        return render_template_string(DASHBOARD, style=style, phase10_style=PHASE10_STYLE, agents=agents, workflow_proposals=workflow_proposals, capture_proposals=capture_proposals, calls=calls, handoffs=handoffs, eval_passed=sum(int(r["passed"]) for r in latest), eval_total=len(latest))

    @app.route("/automations/ai-builder", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def ai_workflow_builder():
        description = request.form.get("description", "").strip()
        if request.method == "POST":
            require_csrf()
            try:
                definition, provider = generate_workflow_draft(description)
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                agent = one("SELECT * FROM ai_agents WHERE organization_id=? AND agent_type='workflow_builder' AND enabled=1", (organization_id(),))
                with db.connect() as connection:
                    cursor = connection.execute("""INSERT INTO ai_workflow_proposals(organization_id,agent_id,agent_version,prompt,proposed_json,provider,status,created_by,created_at) VALUES (?,?,?,?,?,?,'pending',?,?)""", (organization_id(), agent["id"], agent["published_version"], description, json.dumps(definition, sort_keys=True), provider, g.user.get("id"), utc_now()))
                    proposal_id = cursor.lastrowid
                audit("ai.workflow_proposed", "ai_workflow_proposal", proposal_id, {"provider": provider})
                return redirect(url_for("ai_workflow_proposal", proposal_id=proposal_id))
        return render_template_string(BUILDER, style=style, phase10_style=PHASE10_STYLE, description=description)

    @app.route("/ai/workflow-proposals/<int:proposal_id>", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def ai_workflow_proposal(proposal_id):
        proposal = one("SELECT * FROM ai_workflow_proposals WHERE id=? AND organization_id=?", (proposal_id, organization_id()))
        definition = json.loads(proposal["proposed_json"])
        if request.method == "POST":
            require_csrf()
            if proposal["status"] != "pending":
                flash("This proposal was already reviewed.", "error")
                return redirect(request.path)
            decision = request.form.get("decision")
            with db.connect() as connection:
                workflow_id = None
                if decision == "accept":
                    base = (proposal["prompt"][:70] or "AI workflow draft").strip()
                    name, suffix = base, 2
                    while connection.execute("SELECT 1 FROM workflow_definitions WHERE organization_id=? AND name=?", (organization_id(), name)).fetchone():
                        name, suffix = f"{base} {suffix}", suffix + 1
                    cursor = connection.execute("""INSERT INTO workflow_definitions(organization_id,name,description,draft_json,enabled,is_template,created_by,created_at,updated_at) VALUES (?,?,?, ?,0,0,?,?,?)""", (organization_id(), name, "AI proposal accepted for manual review; not published.", json.dumps(definition, sort_keys=True), g.user.get("id"), utc_now(), utc_now()))
                    workflow_id, status = cursor.lastrowid, "accepted"
                elif decision == "reject":
                    status = "rejected"
                else:
                    abort(400)
                connection.execute("UPDATE ai_workflow_proposals SET status=?,workflow_id=?,reviewed_by=?,reviewed_at=? WHERE id=?", (status, workflow_id, g.user.get("id"), utc_now(), proposal_id))
            audit(f"ai.workflow_{status}", "ai_workflow_proposal", proposal_id, {"workflow_id": workflow_id})
            flash("Proposal reviewed. The workflow remains disabled and unpublished." if workflow_id else "Proposal rejected.", "good")
            return redirect(request.path)
        return render_template_string(WORKFLOW_REVIEW, style=style, phase10_style=PHASE10_STYLE, proposal=proposal, pretty=json.dumps(definition, indent=2))

    @app.post("/calls/<int:call_id>/ai-capture")
    @roles_required("owner", "manager", "agent")
    def create_capture_proposal(call_id):
        require_csrf()
        call = one("SELECT * FROM calls WHERE id=? AND organization_id=?", (call_id, organization_id()))
        try:
            proposed, evidence, handoff = capture_conversation(call["transcript"])
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("ai_dashboard"))
        agent = one("SELECT * FROM ai_agents WHERE organization_id=? AND agent_type='conversation_capture' AND enabled=1", (organization_id(),))
        payload = {"fields": proposed, "handoff": handoff, "agent_id": agent["id"], "agent_version": agent["published_version"], "provider": "local"}
        with db.connect() as connection:
            cursor = connection.execute("""INSERT INTO ai_capture_proposals(organization_id,lead_id,call_id,proposal_json,provenance_json,status,created_at) VALUES (?,?,?,?,?,'pending',?)""", (organization_id(), call["lead_id"], call_id, json.dumps(payload, sort_keys=True), json.dumps(evidence, sort_keys=True), utc_now()))
            proposal_id = cursor.lastrowid
        audit("ai.capture_proposed", "ai_capture_proposal", proposal_id, {"call_id": call_id})
        return redirect(url_for("ai_capture_proposal", proposal_id=proposal_id))

    @app.route("/ai/capture-proposals/<int:proposal_id>", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def ai_capture_proposal(proposal_id):
        proposal = one("SELECT * FROM ai_capture_proposals WHERE id=? AND organization_id=?", (proposal_id, organization_id()))
        call = one("SELECT * FROM calls WHERE id=? AND organization_id=?", (proposal["call_id"], organization_id()))
        payload = json.loads(proposal["proposal_json"])
        proposed, handoff = payload.get("fields", {}), payload.get("handoff")
        evidence = json.loads(proposal["provenance_json"] or "{}")
        allowed_fields = {"owner_name", "reference_number", "distress_signal", "seller_motivation", "price_expectation", "follow_up_plan", "follow_up_score"}
        if request.method == "POST":
            require_csrf()
            if proposal["status"] != "pending":
                flash("This proposal was already reviewed.", "error")
                return redirect(request.path)
            decision = request.form.get("decision")
            with db.connect() as connection:
                if decision == "accept":
                    selected = [f for f in request.form.getlist("fields") if f in allowed_fields and f in proposed]
                    updates, values = [], []
                    for field in selected:
                        value = proposed[field]
                        if field == "owner_name":
                            value = sanitize_owner_name(str(value))
                            if not value:
                                continue
                        if field == "follow_up_score":
                            value = max(1, min(5, int(value)))
                        updates.append(f"{field}=?")
                        values.append(value)
                    if updates:
                        values.extend([utc_now(), proposal["lead_id"], organization_id()])
                        connection.execute(f"UPDATE leads SET {','.join(updates)},updated_at=? WHERE id=? AND organization_id=?", values)
                    if request.form.get("create_handoff") == "yes" and handoff:
                        connection.execute("""INSERT INTO ai_handoffs(organization_id,lead_id,capture_proposal_id,reason,priority,status,created_at) VALUES (?,?,?,?,?,'open',?)""", (organization_id(), proposal["lead_id"], proposal_id, handoff["reason"], handoff["priority"], utc_now()))
                    status = "applied"
                elif decision == "reject":
                    status, selected = "rejected", []
                else:
                    abort(400)
                connection.execute("UPDATE ai_capture_proposals SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", (status, g.user.get("id"), utc_now(), proposal_id))
            if decision == "accept" and "owner_name" in selected:
                db.sync_contact_for_lead(proposal["lead_id"])
            audit(f"ai.capture_{status}", "ai_capture_proposal", proposal_id, {"fields": selected})
            flash("Selected facts applied." if status == "applied" else "Proposal rejected.", "good")
            return redirect(request.path)
        return render_template_string(CAPTURE_REVIEW, style=style, phase10_style=PHASE10_STYLE, proposal=proposal, call=call, proposed=proposed, evidence=evidence, handoff=handoff)

    @app.post("/ai/handoffs/<int:handoff_id>/resolve")
    @roles_required("owner", "manager", "agent")
    def resolve_ai_handoff(handoff_id):
        require_csrf()
        with db.connect() as connection:
            cursor = connection.execute("UPDATE ai_handoffs SET status='resolved',resolved_at=? WHERE id=? AND organization_id=? AND status='open'", (utc_now(), handoff_id, organization_id()))
            if not cursor.rowcount:
                abort(404)
        audit("ai.handoff_resolved", "ai_handoff", handoff_id)
        return redirect(url_for("ai_dashboard"))

    @app.route("/ai/agents/<int:agent_id>", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent", "viewer")
    def ai_agent_detail(agent_id):
        agent = one("SELECT * FROM ai_agents WHERE id=? AND organization_id=?", (agent_id, organization_id()))
        tools = json.loads(agent["tool_allowlist_json"] or "[]")
        if request.method == "POST":
            if g.user["role"] not in ("owner", "manager"):
                abort(403)
            require_csrf()
            instructions = request.form.get("instructions", "").strip()[:12000]
            disclosure = request.form.get("disclosure", "").strip()[:2000]
            if not instructions or not disclosure:
                flash("Instructions and disclosure are required.", "error")
                return redirect(request.path)
            with db.connect() as connection:
                connection.execute("UPDATE ai_agents SET draft_instructions=?,disclosure_text=?,updated_at=? WHERE id=?", (instructions, disclosure, utc_now(), agent_id))
                if request.form.get("action") == "publish":
                    version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM ai_agent_versions WHERE agent_id=?", (agent_id,)).fetchone()[0]
                    connection.execute("""INSERT INTO ai_agent_versions(organization_id,agent_id,version,instructions,tool_allowlist_json,disclosure_text,handoff_rules_json,published_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)""", (organization_id(), agent_id, version, instructions, agent["tool_allowlist_json"], disclosure, agent["handoff_rules_json"], g.user.get("id"), utc_now()))
                    connection.execute("UPDATE ai_agents SET published_version=? WHERE id=?", (version, agent_id))
            audit("ai.agent_published" if request.form.get("action") == "publish" else "ai.agent_saved", "ai_agent", agent_id)
            return redirect(request.path)
        versions = rows("SELECT * FROM ai_agent_versions WHERE agent_id=? AND organization_id=? ORDER BY version DESC", (agent_id, organization_id()))
        return render_template_string(AGENT, style=style, phase10_style=PHASE10_STYLE, agent=agent, tools=tools, versions=versions)

    @app.get("/settings/knowledge")
    @roles_required("owner", "manager", "agent", "viewer")
    def knowledge_settings():
        query = request.args.get("q", "").strip()
        sources = rows("SELECT * FROM ai_knowledge_bases WHERE organization_id=? ORDER BY name", (organization_id(),))
        published = [dict(item) for item in rows("""SELECT b.name,v.content FROM ai_knowledge_bases b JOIN ai_knowledge_versions v ON v.knowledge_base_id=b.id AND v.version=b.published_version WHERE b.organization_id=? AND b.active=1""", (organization_id(),))]
        results = search_knowledge(published, query, 5) if query else []
        return render_template_string(KNOWLEDGE, style=style, phase10_style=PHASE10_STYLE, sources=sources, query=query, results=results)

    @app.post("/settings/knowledge/<int:base_id>")
    @roles_required("owner", "manager")
    def edit_knowledge_base(base_id):
        require_csrf()
        one("SELECT * FROM ai_knowledge_bases WHERE id=? AND organization_id=?", (base_id, organization_id()))
        content = request.form.get("content", "").strip()[:30000]
        if len(content) < 20:
            flash("Provide at least a short paragraph.", "error")
            return redirect(url_for("knowledge_settings"))
        with db.connect() as connection:
            connection.execute("UPDATE ai_knowledge_bases SET draft_content=?,updated_at=? WHERE id=?", (content, utc_now(), base_id))
            if request.form.get("action") == "publish":
                version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM ai_knowledge_versions WHERE knowledge_base_id=?", (base_id,)).fetchone()[0]
                connection.execute("""INSERT INTO ai_knowledge_versions(organization_id,knowledge_base_id,version,content,content_sha256,published_by,created_at) VALUES (?,?,?,?,?,?,?)""", (organization_id(), base_id, version, content, hashlib.sha256(content.encode()).hexdigest(), g.user.get("id"), utc_now()))
                connection.execute("UPDATE ai_knowledge_bases SET published_version=? WHERE id=?", (version, base_id))
        audit("ai.knowledge_published" if request.form.get("action") == "publish" else "ai.knowledge_saved", "ai_knowledge_base", base_id)
        return redirect(url_for("knowledge_settings"))

    @app.post("/ai/evaluations/run")
    @roles_required("owner", "manager")
    def run_ai_evaluations():
        require_csrf()
        cases = rows("SELECT * FROM ai_evaluation_cases WHERE organization_id=? AND active=1 ORDER BY id", (organization_id(),))
        passed_count = 0
        with db.connect() as connection:
            for case in cases:
                passed, actual, details = evaluate_case(case["agent_type"], json.loads(case["input_json"]), json.loads(case["expected_json"]))
                agent = connection.execute("SELECT * FROM ai_agents WHERE organization_id=? AND agent_type=?", (organization_id(), case["agent_type"])).fetchone()
                connection.execute("""INSERT INTO ai_evaluation_results(organization_id,case_id,agent_id,agent_version,passed,actual_json,details,created_at) VALUES (?,?,?,?,?,?,?,?)""", (organization_id(), case["id"], agent["id"] if agent else None, agent["published_version"] if agent else None, int(passed), json.dumps(actual, sort_keys=True), details, utc_now()))
                passed_count += int(passed)
        audit("ai.evaluations_run", "ai_evaluation", None, {"passed": passed_count, "total": len(cases)})
        flash(f"Safety evaluations complete: {passed_count} of {len(cases)} passed.", "good" if passed_count == len(cases) else "error")
        return redirect(url_for("ai_dashboard"))
