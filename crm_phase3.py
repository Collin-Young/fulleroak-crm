"""Phase 3 opportunity, pipeline, board, and transition views."""

from __future__ import annotations

import re

from flask import abort, flash, g, jsonify, redirect, render_template_string, request, url_for

from crm_db import utc_now


PHASE3_STYLE = """
.board{display:flex;gap:14px;overflow-x:auto;align-items:flex-start;padding-bottom:14px}.lane{flex:0 0 300px;background:#eae9e3;border:1px solid var(--line);border-radius:14px;padding:12px;min-height:240px}.lane-head{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:10px}.lane-title{display:flex;align-items:center;gap:8px;font-weight:800}.stage-dot{width:11px;height:11px;border-radius:50%;display:inline-block}.deal-card{background:#fff;border:1px solid var(--line);border-radius:11px;padding:13px;margin:9px 0;box-shadow:0 3px 12px #1d292310}.deal-card[draggable=true]{cursor:grab}.deal-card.dragging{opacity:.45}.lane.drop-target{outline:3px solid var(--accent);outline-offset:2px}.deal-title{font-weight:800;text-decoration:none}.deal-meta{color:var(--muted);font-size:13px;margin-top:4px}.move-form{display:flex;gap:6px;margin-top:10px}.move-form select{min-width:0;width:100%;padding:7px}.move-form .btn{padding:7px 9px}.pipeline-block{margin-bottom:18px}.stage-row{display:grid;grid-template-columns:22px 1fr 130px 105px auto;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}.stage-row:last-child{border-bottom:0}.history{border-left:2px solid var(--line);margin-left:8px}.history-item{padding:0 0 18px 18px;position:relative}.history-item:before{content:'';position:absolute;width:10px;height:10px;border-radius:50%;background:var(--accent);left:-6px;top:5px}@media(max-width:800px){.stage-row{grid-template-columns:18px 1fr}.stage-row>*:nth-child(n+3){grid-column:2}.board{scroll-snap-type:x mandatory}.lane{flex-basis:86vw;scroll-snap-align:start}}
"""


BOARD_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Deals · CRM</title><style>{{style}}{{phase3_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← All leads</a><h1 style="margin-top:8px">Deals</h1><div class="sub">Move opportunities through {{pipeline.name}}</div></div><div class="top-actions"><a class="btn secondary" href="{{url_for('deals_table',pipeline=pipeline.id)}}">Table view</a>{% if current_user.role in ('owner','manager') %}<a class="btn secondary" href="{{url_for('pipeline_settings')}}">Configure pipelines</a>{% endif %}</div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="toolbar" method="get"><label>Pipeline <select name="pipeline" onchange="this.form.submit()">{% for item in pipelines %}<option value="{{item.id}}" {% if item.id==pipeline.id %}selected{% endif %}>{{item.name}}</option>{% endfor %}</select></label><noscript><button class="btn">Open</button></noscript></form><div class="stats"><div class="stat"><b>{{totals.total}}</b><span>Deals</span></div><div class="stat"><b>{{totals.open}}</b><span>Open</span></div><div class="stat"><b>{{totals.won}}</b><span>Won</span></div><div class="stat"><b>{{totals.value}}</b><span>Estimated value</span></div></div><section class="board" aria-label="Deal board">{% for stage in stages %}<div class="lane" data-stage-id="{{stage.id}}" aria-label="{{stage.name}} stage"><div class="lane-head"><div class="lane-title"><span class="stage-dot" style="background:{{stage.color}}"></span>{{stage.name}}</div><span class="pill">{{stage.deals|length}}</span></div><div class="cards">{% for deal in stage.deals %}<article class="deal-card" draggable="{{'true' if current_user.role in ('owner','manager','agent') else 'false'}}" data-deal-id="{{deal.id}}" data-version="{{deal.version}}"><a class="deal-title" href="{{url_for('deal_detail',opportunity_id=deal.id)}}">{{deal.title}}</a><div class="deal-meta">{{deal.phone_display or 'No phone'}}{% if deal.reference_number %} · {{deal.reference_number}}{% endif %}</div>{% if deal.property_address %}<div class="deal-meta">{{deal.property_address}}</div>{% endif %}<div class="deal-meta">Priority {{deal.follow_up_score}}{% if deal.estimated_value is not none %} · ${{'{:,.0f}'.format(deal.estimated_value)}}{% endif %}</div>{% if current_user.role in ('owner','manager','agent') %}<form class="move-form" method="post" action="{{url_for('move_deal',opportunity_id=deal.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="expected_version" value="{{deal.version}}"><select name="stage_id" aria-label="Move {{deal.title}} to stage">{% for option in stages %}<option value="{{option.id}}" {% if option.id==stage.id %}selected{% endif %}>{{option.name}}</option>{% endfor %}</select><button class="btn secondary">Move</button></form>{% endif %}</article>{% endfor %}</div></div>{% endfor %}</section></main>{% if current_user.role in ('owner','manager','agent') %}<script>
const token={{csrf_token()|tojson}};let dragged=null;
document.querySelectorAll('.deal-card[draggable="true"]').forEach(card=>{card.addEventListener('dragstart',()=>{dragged=card;card.classList.add('dragging')});card.addEventListener('dragend',()=>{card.classList.remove('dragging');document.querySelectorAll('.lane').forEach(x=>x.classList.remove('drop-target'))})});
document.querySelectorAll('.lane').forEach(lane=>{lane.addEventListener('dragover',e=>{e.preventDefault();lane.classList.add('drop-target')});lane.addEventListener('dragleave',()=>lane.classList.remove('drop-target'));lane.addEventListener('drop',async e=>{e.preventDefault();lane.classList.remove('drop-target');if(!dragged||dragged.closest('.lane')===lane)return;const body=new FormData();body.set('csrf_token',token);body.set('stage_id',lane.dataset.stageId);body.set('expected_version',dragged.dataset.version);body.set('response','json');try{const response=await fetch('/deals/'+dragged.dataset.dealId+'/stage',{method:'POST',body});const result=await response.json();if(!response.ok)throw new Error(result.error||'Move failed');dragged.dataset.version=result.version;dragged.querySelector('[name="expected_version"]').value=result.version;dragged.querySelector('[name="stage_id"]').value=lane.dataset.stageId;lane.querySelector('.cards').prepend(dragged);location.reload()}catch(error){alert(error.message);location.reload()}})});
</script>{% endif %}</body></html>
"""


TABLE_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Deals table · CRM</title><style>{{style}}{{phase3_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('deals_board',pipeline=pipeline.id)}}">← Board view</a><h1 style="margin-top:8px">{{pipeline.name}} deals</h1></div></header><form class="toolbar" method="get"><input type="hidden" name="pipeline" value="{{pipeline.id}}"><input name="q" value="{{q}}" placeholder="Search seller, phone, reference, property..."><select name="stage"><option value="">All stages</option>{% for item in stages %}<option value="{{item.id}}" {% if stage_filter==item.id|string %}selected{% endif %}>{{item.name}}</option>{% endfor %}</select><button class="btn">Apply</button></form><div class="table-wrap"><table><thead><tr><th>Deal</th><th>Seller</th><th>Property</th><th>Stage</th><th>Value</th><th>Activity</th></tr></thead><tbody>{% for deal in deals %}<tr><td><a class="leadlink" href="{{url_for('deal_detail',opportunity_id=deal.id)}}">{{deal.title}}</a></td><td>{{deal.contact_name or 'Unknown'}}<div class="muted">{{deal.phone_display}}</div></td><td>{{deal.reference_number or '—'}}<div class="muted">{{deal.property_address}}</div></td><td><span class="pill" style="border-left:4px solid {{deal.stage_color}}">{{deal.stage_name}}</span></td><td>{% if deal.estimated_value is not none %}${{'{:,.0f}'.format(deal.estimated_value)}}{% else %}—{% endif %}</td><td>{{deal.latest_activity or deal.updated_at}}</td></tr>{% else %}<tr><td colspan="6" class="empty">No deals match these filters.</td></tr>{% endfor %}</tbody></table></div></main></body></html>
"""


DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{deal.title}} · Deal</title><style>{{style}}{{phase3_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('deals_board',pipeline=deal.pipeline_id)}}">← {{deal.pipeline_name}} board</a><h1 style="margin-top:8px">{{deal.title}}</h1><div class="sub">{{deal.stage_name}} · Updated {{deal.updated_at}}</div></div>{% if deal.lead_id %}<a class="btn secondary" href="{{url_for('lead_detail',lead_id=deal.lead_id)}}">Open lead & calls</a>{% endif %}</header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><section class="stack"><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Deal details</h2><label class="field">Deal title<input name="title" value="{{deal.title}}" required></label><label class="field">Estimated acquisition value<input type="number" step="0.01" min="0" name="estimated_value" value="{{deal.estimated_value if deal.estimated_value is not none else ''}}"></label><label class="field">Seller contact<select name="contact_id"><option value="">No contact</option>{% for item in contacts %}<option value="{{item.id}}" {% if item.id==deal.contact_id %}selected{% endif %}>{{item.full_name or item.primary_phone_display}}</option>{% endfor %}</select></label><label class="field">Property<select name="property_id"><option value="">No property</option>{% for item in properties %}<option value="{{item.id}}" {% if item.id==deal.property_id %}selected{% endif %}>{{item.reference_number or item.property_address or ('Property #' ~ item.id)}}</option>{% endfor %}</select></label><label class="field">Assigned to<select name="assigned_user_id"><option value="">Unassigned</option>{% for item in users %}<option value="{{item.id}}" {% if item.id==deal.assigned_user_id %}selected{% endif %}>{{item.display_name}}</option>{% endfor %}</select></label><label class="field">Deal notes<textarea name="notes">{{deal.notes}}</textarea></label>{% if current_user.role in ('owner','manager','agent') %}<button class="btn">Save deal</button>{% endif %}</form><section class="panel"><h2>Linked context</h2><div class="facts"><div class="fact"><b>Seller</b>{% if deal.contact_id %}<a href="{{url_for('contact_detail',contact_id=deal.contact_id)}}">{{deal.contact_name or deal.phone_display}}</a>{% else %}Not linked{% endif %}</div><div class="fact"><b>Phone</b>{{deal.phone_display or '—'}}</div><div class="fact"><b>Property</b>{% if deal.property_id %}<a href="{{url_for('property_detail',property_id=deal.property_id)}}">{{deal.reference_number or deal.property_address or 'Open property'}}</a>{% else %}Not linked{% endif %}</div><div class="fact"><b>Distress signal</b>{{deal.distress_signal or 'None identified'}}</div><div class="fact"><b>Motivation</b>{{deal.seller_motivation or 'None identified'}}</div><div class="fact"><b>Price posture</b>{{deal.price_expectation or 'Not established'}}</div></div></section></section><aside class="stack"><form class="panel" method="post" action="{{url_for('move_deal',opportunity_id=deal.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="expected_version" value="{{deal.version}}"><h2>Move stage</h2><label class="field">Stage<select name="stage_id">{% for item in stages %}<option value="{{item.id}}" {% if item.id==deal.stage_id %}selected{% endif %}>{{item.name}}</option>{% endfor %}</select></label><label class="field">Transition note<input name="note" placeholder="Why is this moving?"></label>{% if current_user.role in ('owner','manager','agent') %}<button class="btn">Move deal</button>{% endif %}</form><section class="panel"><h2>Stage history</h2><div class="history">{% for item in history %}<div class="history-item"><b>{{item.from_name or 'Created'}} → {{item.to_name}}</b><div class="muted">{{item.created_at}} · {{item.display_name or 'System'}}</div>{% if item.note %}<div>{{item.note}}</div>{% endif %}</div>{% else %}<div class="muted" style="padding-left:18px">No stage changes yet.</div>{% endfor %}</div></section></aside></div></main></body></html>
"""


SETTINGS_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pipelines · Settings</title><style>{{style}}{{phase3_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('deals_board')}}">← Deals</a><h1 style="margin-top:8px">Pipelines and stages</h1><div class="sub">Configure the workflow your team uses to move deals</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><section>{% for pipeline in pipelines %}<div class="panel pipeline-block"><div class="top"><div><h2>{{pipeline.name}}</h2><div class="muted">{{pipeline.kind|replace('_',' ')|title}}{% if pipeline.is_default %} · Default{% endif %}</div></div><a class="btn secondary" href="{{url_for('deals_board',pipeline=pipeline.id)}}">Open board</a></div>{% for stage in pipeline.stages %}<form class="stage-row" method="post" action="{{url_for('update_pipeline_stage',stage_id=stage.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><span class="stage-dot" style="background:{{stage.color}}"></span><input name="name" value="{{stage.name}}" aria-label="Stage name"><select name="outcome"><option value="open" {% if stage.outcome=='open' %}selected{% endif %}>Open</option><option value="won" {% if stage.outcome=='won' %}selected{% endif %}>Won</option><option value="lost" {% if stage.outcome=='lost' %}selected{% endif %}>Lost</option></select><input type="color" name="color" value="{{stage.color}}" aria-label="Stage color"><span><button class="btn secondary" name="direction" value="up" title="Move up">↑</button><button class="btn secondary" name="direction" value="down" title="Move down">↓</button><button class="btn secondary" name="direction" value="save">Save</button></span></form>{% endfor %}<form method="post" action="{{url_for('pipeline_settings')}}" style="margin-top:14px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="action" value="stage"><input type="hidden" name="pipeline_id" value="{{pipeline.id}}"><div class="toolbar"><input name="name" placeholder="New stage name" required><select name="outcome"><option value="open">Open</option><option value="won">Won</option><option value="lost">Lost</option></select><input type="color" name="color" value="#235d4d"><button class="btn">Add stage</button></div></form></div>{% endfor %}</section><aside><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="action" value="pipeline"><h2>New pipeline</h2><label class="field">Name<input name="name" required></label><label class="field">Type<select name="kind"><option value="acquisition">Acquisition</option><option value="disposition">Disposition</option><option value="owner_finance">Owner finance</option><option value="other">Other</option></select></label><button class="btn">Create pipeline</button></form></aside></div></main></body></html>
"""


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:40] or "stage"


def register_phase3(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def pipelines_with_stages():
        pipeline_rows = rows("SELECT * FROM pipelines WHERE organization_id=? AND active=1 ORDER BY is_default DESC,id", (organization_id(),))
        for pipeline in pipeline_rows:
            pipeline["stages"] = rows("SELECT * FROM pipeline_stages WHERE organization_id=? AND pipeline_id=? AND active=1 ORDER BY position,id", (organization_id(), pipeline["id"]))
        return pipeline_rows

    def selected_pipeline(pipeline_id: str | None):
        available = pipelines_with_stages()
        if not available:
            abort(404)
        if pipeline_id and str(pipeline_id).isdigit():
            for pipeline in available:
                if pipeline["id"] == int(pipeline_id):
                    return available, pipeline
        return available, available[0]

    @app.get("/deals")
    def deals_board():
        pipelines, pipeline = selected_pipeline(request.args.get("pipeline"))
        deal_rows = rows("""SELECT o.*,l.phone_display,l.reference_number,l.follow_up_score,l.latest_activity,
            p.property_address FROM opportunities o
            LEFT JOIN leads l ON l.id=o.lead_id AND l.organization_id=o.organization_id
            LEFT JOIN properties p ON p.id=o.property_id AND p.organization_id=o.organization_id
            WHERE o.organization_id=? AND o.pipeline_id=? ORDER BY o.updated_at DESC,o.id DESC""", (organization_id(), pipeline["id"]))
        by_stage = {stage["id"]: [] for stage in pipeline["stages"]}
        for deal in deal_rows:
            by_stage.setdefault(deal["stage_id"], []).append(deal)
        for stage in pipeline["stages"]:
            stage["deals"] = by_stage.get(stage["id"], [])
        total_value = sum(float(item["estimated_value"] or 0) for item in deal_rows)
        outcomes = {stage["id"]: stage["outcome"] for stage in pipeline["stages"]}
        totals = {
            "total": len(deal_rows),
            "open": sum(outcomes.get(item["stage_id"]) == "open" for item in deal_rows),
            "won": sum(outcomes.get(item["stage_id"]) == "won" for item in deal_rows),
            "value": f"${total_value:,.0f}",
        }
        return render_template_string(BOARD_TEMPLATE, style=style, phase3_style=PHASE3_STYLE, pipelines=pipelines, pipeline=pipeline, stages=pipeline["stages"], totals=totals)

    @app.get("/deals/table")
    def deals_table():
        _, pipeline = selected_pipeline(request.args.get("pipeline"))
        q = request.args.get("q", "").strip()
        stage_filter = request.args.get("stage", "").strip()
        where = ["o.organization_id=?", "o.pipeline_id=?"]
        params = [organization_id(), pipeline["id"]]
        valid_stage_ids = {stage["id"] for stage in pipeline["stages"]}
        if stage_filter.isdigit() and int(stage_filter) in valid_stage_ids:
            where.append("o.stage_id=?")
            params.append(int(stage_filter))
        if q:
            where.append("(o.title LIKE ? OR c.full_name LIKE ? OR c.primary_phone_display LIKE ? OR p.reference_number LIKE ? OR p.property_address LIKE ?)")
            params.extend([f"%{q}%"] * 5)
        deals = rows(f"""SELECT o.*,s.name stage_name,s.color stage_color,c.full_name contact_name,
            c.primary_phone_display phone_display,p.reference_number,p.property_address,l.latest_activity
            FROM opportunities o JOIN pipeline_stages s ON s.id=o.stage_id
            LEFT JOIN contacts c ON c.id=o.contact_id AND c.organization_id=o.organization_id
            LEFT JOIN properties p ON p.id=o.property_id AND p.organization_id=o.organization_id
            LEFT JOIN leads l ON l.id=o.lead_id AND l.organization_id=o.organization_id
            WHERE {' AND '.join(where)} ORDER BY o.updated_at DESC,o.id DESC""", params)
        return render_template_string(TABLE_TEMPLATE, style=style, phase3_style=PHASE3_STYLE, pipeline=pipeline, stages=pipeline["stages"], deals=deals, q=q, stage_filter=stage_filter)

    @app.route("/deals/<int:opportunity_id>", methods=["GET", "POST"])
    def deal_detail(opportunity_id: int):
        found = rows("""SELECT o.*,pl.name pipeline_name,s.name stage_name,c.full_name contact_name,
            c.primary_phone_display phone_display,p.reference_number,p.property_address,
            l.distress_signal,l.seller_motivation,l.price_expectation
            FROM opportunities o JOIN pipelines pl ON pl.id=o.pipeline_id
            JOIN pipeline_stages s ON s.id=o.stage_id
            LEFT JOIN contacts c ON c.id=o.contact_id AND c.organization_id=o.organization_id
            LEFT JOIN properties p ON p.id=o.property_id AND p.organization_id=o.organization_id
            LEFT JOIN leads l ON l.id=o.lead_id AND l.organization_id=o.organization_id
            WHERE o.id=? AND o.organization_id=?""", (opportunity_id, organization_id()))
        if not found:
            abort(404)
        deal = found[0]
        if request.method == "POST":
            if g.user["role"] not in {"owner", "manager", "agent"}:
                abort(403)
            require_csrf()
            title = request.form.get("title", "").strip()[:160]
            if not title:
                flash("Deal title is required.", "error")
            else:
                try:
                    estimated = request.form.get("estimated_value", "").strip()
                    estimated_value = float(estimated) if estimated else None
                    if estimated_value is not None and estimated_value < 0:
                        raise ValueError
                except ValueError:
                    flash("Estimated value must be a positive number.", "error")
                else:
                    selections = {}
                    for field, table in (("contact_id", "contacts"), ("property_id", "properties"), ("assigned_user_id", "users")):
                        raw = request.form.get(field, "").strip()
                        selections[field] = int(raw) if raw.isdigit() else None
                        if selections[field] and not rows(f"SELECT id FROM {table} WHERE id=? AND organization_id=?", (selections[field], organization_id())):
                            abort(400)
                    with db.connect() as connection:
                        connection.execute("""UPDATE opportunities SET title=?,estimated_value=?,contact_id=?,property_id=?,assigned_user_id=?,notes=?,updated_at=?,version=version+1 WHERE id=? AND organization_id=?""", (title, estimated_value, selections["contact_id"], selections["property_id"], selections["assigned_user_id"], request.form.get("notes", "").strip(), utc_now(), opportunity_id, organization_id()))
                    audit("opportunity.update", "opportunity", opportunity_id)
                    flash("Deal saved.", "good")
                    return redirect(url_for("deal_detail", opportunity_id=opportunity_id))
        stages = rows("SELECT * FROM pipeline_stages WHERE organization_id=? AND pipeline_id=? AND active=1 ORDER BY position,id", (organization_id(), deal["pipeline_id"]))
        contacts = rows("SELECT id,full_name,primary_phone_display FROM contacts WHERE organization_id=? ORDER BY full_name,primary_phone_display", (organization_id(),))
        properties = rows("SELECT id,reference_number,property_address FROM properties WHERE organization_id=? ORDER BY reference_number,property_address", (organization_id(),))
        users = rows("SELECT id,display_name FROM users WHERE organization_id=? AND active=1 ORDER BY display_name", (organization_id(),))
        history = rows("""SELECT t.*,fs.name from_name,ts.name to_name,u.display_name
            FROM stage_transitions t LEFT JOIN pipeline_stages fs ON fs.id=t.from_stage_id
            JOIN pipeline_stages ts ON ts.id=t.to_stage_id LEFT JOIN users u ON u.id=t.user_id
            WHERE t.organization_id=? AND t.opportunity_id=? ORDER BY t.id DESC""", (organization_id(), opportunity_id))
        return render_template_string(DETAIL_TEMPLATE, style=style, phase3_style=PHASE3_STYLE, deal=deal, stages=stages, contacts=contacts, properties=properties, users=users, history=history)

    @app.post("/deals/<int:opportunity_id>/stage")
    @roles_required("owner", "manager", "agent")
    def move_deal(opportunity_id: int):
        require_csrf()
        raw_stage = request.form.get("stage_id", "")
        raw_version = request.form.get("expected_version", "")
        if not raw_stage.isdigit() or not raw_version.isdigit():
            abort(400)
        stage_id, expected_version = int(raw_stage), int(raw_version)
        error = None
        new_version = expected_version
        with db.connect() as connection:
            deal = connection.execute("SELECT * FROM opportunities WHERE id=? AND organization_id=?", (opportunity_id, organization_id())).fetchone()
            stage = connection.execute("SELECT * FROM pipeline_stages WHERE id=? AND organization_id=? AND active=1", (stage_id, organization_id())).fetchone()
            if not deal or not stage:
                abort(404)
            if stage["pipeline_id"] != deal["pipeline_id"]:
                abort(400)
            if deal["version"] != expected_version:
                error = "This deal changed in another tab. The board has been refreshed."
            elif deal["stage_id"] != stage_id:
                cursor = connection.execute("""UPDATE opportunities SET stage_id=?,updated_at=?,version=version+1
                    WHERE id=? AND organization_id=? AND version=?""", (stage_id, utc_now(), opportunity_id, organization_id(), expected_version))
                if not cursor.rowcount:
                    error = "This deal changed in another tab. The board has been refreshed."
                else:
                    new_version = expected_version + 1
                    note = request.form.get("note", "").strip()[:500]
                    occurred_at = utc_now()
                    transition = connection.execute("""INSERT INTO stage_transitions(organization_id,opportunity_id,from_stage_id,to_stage_id,user_id,note,created_at)
                        VALUES (?,?,?,?,?,?,?)""", (organization_id(), opportunity_id, deal["stage_id"], stage_id, g.user.get("id"), note, occurred_at))
                    from_stage = connection.execute("SELECT name FROM pipeline_stages WHERE id=?", (deal["stage_id"],)).fetchone()
                    connection.execute("""INSERT INTO activity_events(
                        organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                        occurred_at,title,body,source_table,source_id,metadata_json,created_at
                    ) VALUES (?,?,?,?,?,'system','internal',?,?,?,?,?,?,?)""", (
                        organization_id(), deal["lead_id"], deal["contact_id"], opportunity_id,
                        "stage_change", occurred_at,
                        f"Stage changed: {from_stage['name'] if from_stage else 'Created'} → {stage['name']}",
                        note, "stage_transitions", transition.lastrowid,
                        '{}', occurred_at,
                    ))
                    if deal["lead_id"] and stage["stage_key"] in {"new", "contacted", "qualified", "nurture", "won", "lost"}:
                        connection.execute("UPDATE leads SET status=?,updated_at=? WHERE id=? AND organization_id=?", (stage["stage_key"], utc_now(), deal["lead_id"], organization_id()))
        wants_json = request.form.get("response") == "json"
        if error:
            if wants_json:
                return jsonify(error=error), 409
            flash(error, "error")
        elif new_version != expected_version:
            audit("opportunity.stage_change", "opportunity", opportunity_id, {"to_stage_id": stage_id})
            if wants_json:
                return jsonify(ok=True, version=new_version)
            flash("Deal stage updated.", "good")
        elif wants_json:
            return jsonify(ok=True, version=new_version)
        return redirect(request.referrer or url_for("deals_board"))

    @app.route("/settings/pipelines", methods=["GET", "POST"])
    @roles_required("owner", "manager")
    def pipeline_settings():
        if request.method == "POST":
            require_csrf()
            action = request.form.get("action")
            name = request.form.get("name", "").strip()[:80]
            if not name:
                flash("A name is required.", "error")
            elif action == "pipeline":
                kind = request.form.get("kind", "other")
                if kind not in {"acquisition", "disposition", "owner_finance", "other"}:
                    abort(400)
                try:
                    with db.connect() as connection:
                        cursor = connection.execute("""INSERT INTO pipelines(organization_id,name,kind,is_default,active,created_at,updated_at)
                            VALUES (?,?,?,0,1,?,?)""", (organization_id(), name, kind, utc_now(), utc_now()))
                        pipeline_id = cursor.lastrowid
                        for position, (stage_name, outcome, color) in enumerate((("New", "open", "#54778c"), ("Won", "won", "#247047"), ("Lost", "lost", "#9b4b45")), 1):
                            connection.execute("""INSERT INTO pipeline_stages(organization_id,pipeline_id,name,stage_key,position,color,outcome,created_at,updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?)""", (organization_id(), pipeline_id, stage_name, _slug(stage_name), position * 10, color, outcome, utc_now(), utc_now()))
                    audit("pipeline.create", "pipeline", pipeline_id, {"name": name})
                    flash("Pipeline created with New, Won, and Lost stages.", "good")
                except Exception as exc:
                    if "UNIQUE constraint" not in str(exc):
                        raise
                    flash("A pipeline with that name already exists.", "error")
            elif action == "stage":
                pipeline_id = request.form.get("pipeline_id", "")
                outcome = request.form.get("outcome", "open")
                if not pipeline_id.isdigit() or outcome not in {"open", "won", "lost"}:
                    abort(400)
                pipeline = rows("SELECT id FROM pipelines WHERE id=? AND organization_id=?", (int(pipeline_id), organization_id()))
                if not pipeline:
                    abort(404)
                with db.connect() as connection:
                    position = connection.execute("SELECT COALESCE(MAX(position),0)+10 FROM pipeline_stages WHERE pipeline_id=?", (int(pipeline_id),)).fetchone()[0]
                    base = _slug(name)
                    key, suffix = base, 2
                    while connection.execute("SELECT 1 FROM pipeline_stages WHERE pipeline_id=? AND stage_key=?", (int(pipeline_id), key)).fetchone():
                        key, suffix = f"{base}_{suffix}", suffix + 1
                    cursor = connection.execute("""INSERT INTO pipeline_stages(organization_id,pipeline_id,name,stage_key,position,color,outcome,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (organization_id(), int(pipeline_id), name, key, position, request.form.get("color", "#235d4d"), outcome, utc_now(), utc_now()))
                audit("pipeline_stage.create", "pipeline_stage", cursor.lastrowid, {"pipeline_id": int(pipeline_id)})
                flash("Stage added.", "good")
            return redirect(url_for("pipeline_settings"))
        return render_template_string(SETTINGS_TEMPLATE, style=style, phase3_style=PHASE3_STYLE, pipelines=pipelines_with_stages())

    @app.post("/settings/pipelines/stages/<int:stage_id>")
    @roles_required("owner", "manager")
    def update_pipeline_stage(stage_id: int):
        require_csrf()
        stage_rows = rows("SELECT * FROM pipeline_stages WHERE id=? AND organization_id=?", (stage_id, organization_id()))
        if not stage_rows:
            abort(404)
        stage = stage_rows[0]
        direction = request.form.get("direction", "save")
        name = request.form.get("name", "").strip()[:80]
        outcome = request.form.get("outcome", "open")
        if not name or outcome not in {"open", "won", "lost"}:
            abort(400)
        with db.connect() as connection:
            if direction in {"up", "down"}:
                operator, ordering = ("<", "DESC") if direction == "up" else (">", "ASC")
                neighbor = connection.execute(f"SELECT id,position FROM pipeline_stages WHERE pipeline_id=? AND active=1 AND position {operator} ? ORDER BY position {ordering},id {ordering} LIMIT 1", (stage["pipeline_id"], stage["position"])).fetchone()
                if neighbor:
                    connection.execute("UPDATE pipeline_stages SET position=? WHERE id=?", (neighbor["position"], stage_id))
                    connection.execute("UPDATE pipeline_stages SET position=? WHERE id=?", (stage["position"], neighbor["id"]))
            connection.execute("UPDATE pipeline_stages SET name=?,outcome=?,color=?,updated_at=? WHERE id=? AND organization_id=?", (name, outcome, request.form.get("color", "#235d4d"), utc_now(), stage_id, organization_id()))
        audit("pipeline_stage.update", "pipeline_stage", stage_id, {"direction": direction})
        flash("Stage updated.", "good")
        return redirect(url_for("pipeline_settings"))
