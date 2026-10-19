"""Phase 11: personal dashboards, reports, help desk, public forms, and PWA."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from flask import Response, abort, flash, g, redirect, render_template_string, request, url_for

from crm_db import utc_now
from lead_pipeline import extract_references_from_text, sanitize_owner_name


PHASE11_STYLE = """
.widget-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.widget{min-height:150px}.metric-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metric{background:#f7f7f3;border-radius:10px;padding:13px}.metric b{display:block;font-size:25px}.bar{height:9px;background:#e8e7e0;border-radius:99px;overflow:hidden}.bar span{display:block;height:100%;background:var(--accent)}.report-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.public-shell{max-width:700px;margin:5vh auto;padding:20px}.public-card{background:white;border:1px solid var(--line);border-radius:18px;padding:28px}.ticket-row{padding:12px 0;border-bottom:1px solid var(--line)}@media(max-width:850px){.widget-grid,.report-grid,.metric-row{grid-template-columns:1fr}}
"""
FLASHES = """{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}"""

WORKSPACE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Workspace · CRM</title><style>{{style}}{{phase11_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Leads</a><h1>My workspace</h1><div class="sub">A role-aware view for {{current_user.display_name}}</div></div><div class="top-actions"><a class="btn secondary" href="{{url_for('reports')}}">Reports</a><a class="btn secondary" href="{{url_for('help_center')}}">Help</a><a class="btn secondary" href="{{url_for('forms_admin')}}">Forms</a></div></header>""" + FLASHES + """
<div class="metric-row"><div class="metric"><b>{{metrics.active_leads}}</b><span>Active leads</span></div><div class="metric"><b>{{metrics.hot_leads}}</b><span>Priority 4–5</span></div><div class="metric"><b>{{metrics.overdue}}</b><span>Overdue follow-ups</span></div><div class="metric"><b>{{metrics.open_tasks}}</b><span>Open tasks</span></div></div>
<div class="widget-grid" style="margin-top:18px">{% for key in widgets %}{% if key=='hot_leads' %}<section class="panel widget"><h2>Hot leads</h2>{% for x in hot_leads %}<div class="ticket-row"><a class="leadlink" href="{{url_for('lead_detail',lead_id=x.id)}}">{{x.owner_name or x.phone_display}}</a><div class="muted">Priority {{x.follow_up_score}} · {{x.seller_motivation or 'Motivation not captured'}}</div></div>{% else %}<div class="muted">No hot leads.</div>{% endfor %}</section>{% elif key=='tasks' %}<section class="panel widget"><h2>Open tasks</h2>{% for x in tasks %}<div class="ticket-row"><a href="{{url_for('lead_detail',lead_id=x.lead_id)}}">{{x.title}}</a><div class="muted">{{x.due_at or 'No due date'}} · {{x.owner_name or x.phone_display}}</div></div>{% else %}<div class="muted">No open tasks.</div>{% endfor %}</section>{% elif key=='pipeline' %}<section class="panel widget"><h2>Pipeline</h2>{% for x in pipeline %}<div class="ticket-row"><div class="top"><span>{{x.status|title}}</span><b>{{x.count}}</b></div><div class="bar"><span style="width:{{x.percent}}%"></span></div></div>{% endfor %}</section>{% elif key=='appointments' %}<section class="panel widget"><h2>Upcoming appointments</h2>{% for x in appointments %}<div class="ticket-row"><b>{{x.title}}</b><div class="muted">{{x.starts_at}} · {{x.owner_name or x.phone_display or 'Unlinked'}}</div></div>{% else %}<div class="muted">No upcoming appointments.</div>{% endfor %}</section>{% elif key=='tickets' %}<section class="panel widget"><h2>Open help tickets</h2>{% for x in tickets %}<div class="ticket-row"><a href="{{url_for('ticket_detail',ticket_id=x.id)}}">{{x.subject}}</a><div class="muted">{{x.priority}} · {{x.status|replace('_',' ')}}</div></div>{% else %}<div class="muted">No open tickets.</div>{% endfor %}</section>{% elif key=='recent_activity' %}<section class="panel widget"><h2>Recent activity</h2>{% for x in activity %}<div class="ticket-row">{% if x.lead_id %}<a href="{{url_for('lead_detail',lead_id=x.lead_id)}}">{{x.title}}</a>{% else %}<b>{{x.title}}</b>{% endif %}<div class="muted">{{x.occurred_at}} · {{x.channel}}</div></div>{% endfor %}</section>{% endif %}{% endfor %}</div>
<form class="panel" method="post" action="{{url_for('workspace_preferences')}}" style="margin-top:18px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Customize this workspace</h2><div class="actions">{% for key,label in available.items() %}<label class="check"><input type="checkbox" name="widgets" value="{{key}}" {% if key in widgets %}checked{% endif %}>{{label}}</label>{% endfor %}</div><button class="btn">Save layout</button></form></main></body></html>"""

REPORTS = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reports · CRM</title><style>{{style}}{{phase11_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('workspace')}}">← Workspace</a><h1>Acquisition reports</h1><div class="sub">Pipeline, activity, lead source, and follow-up health</div></div><form method="get"><select name="days" onchange="this.form.submit()">{% for d in (7,30,90,365) %}<option value="{{d}}" {% if days==d %}selected{% endif %}>Last {{d}} days</option>{% endfor %}</select></form></header>""" + FLASHES + """
<div class="report-grid"><section class="panel"><h2>Pipeline</h2>{% for x in pipeline %}<div class="ticket-row"><div class="top"><span>{{x.status|title}}</span><b>{{x.count}}</b></div><div class="bar"><span style="width:{{x.percent}}%"></span></div></div>{% endfor %}</section><section class="panel"><h2>Call activity</h2>{% for x in calls %}<div class="ticket-row"><div class="top"><span>{{x.direction|title}}</span><b>{{x.count}}</b></div></div>{% else %}<div class="muted">No calls in this period.</div>{% endfor %}</section><section class="panel"><h2>Lead sources</h2>{% for x in sources %}<div class="ticket-row"><div class="top"><span>{{x.source}}</span><b>{{x.count}}</b></div></div>{% else %}<div class="muted">No source data.</div>{% endfor %}</section></div>
<section class="panel" style="margin-top:16px"><h2>Follow-up health</h2><div class="metric-row"><div class="metric"><b>{{followup.overdue}}</b><span>Overdue</span></div><div class="metric"><b>{{followup.next7}}</b><span>Due next 7 days</span></div><div class="metric"><b>{{followup.unscheduled}}</b><span>Active without date</span></div><div class="metric"><b>{{followup.motivated}}</b><span>Motivation captured</span></div></div></section>
{% if current_user.role in ('owner','manager') %}<form class="panel" method="post" action="{{url_for('save_report')}}" style="margin-top:16px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Save this view</h2><div class="actions"><input name="name" required placeholder="30-day acquisition review"><select name="report_type"><option value="pipeline">Pipeline</option><option value="activity">Activity</option><option value="sources">Sources</option><option value="follow_up">Follow-up</option></select><input type="hidden" name="days" value="{{days}}"><button class="btn">Save report</button></div></form>{% endif %}<div class="panel"><h2>Saved views</h2>{% for x in saved %}<div class="ticket-row"><b>{{x.name}}</b><div class="muted">{{x.report_type|replace('_',' ')}} · {{x.created_at}}</div></div>{% else %}<div class="muted">No saved views.</div>{% endfor %}</div></main></body></html>"""

HELP = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Help · CRM</title><style>{{style}}{{phase11_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('workspace')}}">← Workspace</a><h1>Help center</h1><div class="sub">Local guidance and accountable internal support tickets</div></div><a class="btn" href="{{url_for('new_ticket')}}">New ticket</a></header>""" + FLASHES + """<div class="grid"><section class="stack">{% for a in articles %}<article class="panel"><span class="pill">{{a.category}}</span><h2 style="margin-top:8px">{{a.title}}</h2><div style="white-space:pre-wrap">{{a.body}}</div></article>{% endfor %}</section><aside class="panel"><h2>Tickets</h2>{% for t in tickets %}<div class="ticket-row"><a class="leadlink" href="{{url_for('ticket_detail',ticket_id=t.id)}}">{{t.subject}}</a><div class="muted">{{t.status|replace('_',' ')}} · {{t.priority}}</div></div>{% else %}<div class="muted">No tickets.</div>{% endfor %}</aside></div></main></body></html>"""

TICKET_FORM = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Help ticket · CRM</title><style>{{style}}{{phase11_style}}</style></head><body><main class="shell" style="max-width:760px"><a class="muted" href="{{url_for('help_center')}}">← Help</a><h1>New help ticket</h1>""" + FLASHES + """<form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Subject<input name="subject" required maxlength="160"></label><label class="field">What happened?<textarea name="description" required maxlength="10000"></textarea></label><label class="field">Priority<select name="priority"><option>low</option><option selected>normal</option><option>high</option><option>urgent</option></select></label><label class="field">Related lead ID (optional)<input name="lead_id" inputmode="numeric"></label><button class="btn">Create ticket</button></form></main></body></html>"""

TICKET = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ticket.subject}} · CRM</title><style>{{style}}{{phase11_style}}</style></head><body><main class="shell" style="max-width:850px"><a class="muted" href="{{url_for('help_center')}}">← Help</a><h1>{{ticket.subject}}</h1>""" + FLASHES + """<div class="panel"><div class="actions"><span class="pill">{{ticket.status|replace('_',' ')}}</span><span class="pill">{{ticket.priority}}</span></div><div style="white-space:pre-wrap;margin-top:16px">{{ticket.description}}</div>{% if ticket.lead_id %}<a class="btn secondary" href="{{url_for('lead_detail',lead_id=ticket.lead_id)}}">Open related lead</a>{% endif %}</div>{% if current_user.role in ('owner','manager') %}<form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Status<select name="status">{% for s in ('open','in_progress','resolved','closed') %}<option value="{{s}}" {% if ticket.status==s %}selected{% endif %}>{{s|replace('_',' ')|title}}</option>{% endfor %}</select></label><label class="field">Resolution<textarea name="resolution">{{ticket.resolution}}</textarea></label><button class="btn">Update ticket</button></form>{% endif %}</main></body></html>"""

FORMS_ADMIN = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Forms · CRM</title><style>{{style}}{{phase11_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('workspace')}}">← Workspace</a><h1>Lead capture forms</h1><div class="sub">Local public forms that create reviewable CRM leads</div></div></header>""" + FLASHES + """{% for f in forms %}<form class="panel" method="post" action="{{url_for('edit_public_form',form_id=f.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><div class="top"><div><h2>{{f.name}}</h2><a href="{{url_for('public_lead_form',slug=f.slug)}}" target="_blank">Open public form ↗</a></div><label class="check"><input type="checkbox" name="active" value="yes" {% if f.active %}checked{% endif %}>Active</label></div><label class="field">Title<input name="title" value="{{f.title}}"></label><label class="field">Introduction<textarea name="intro_text">{{f.intro_text}}</textarea></label><label class="field">Success message<textarea name="success_text">{{f.success_text}}</textarea></label><label class="field">Lead source<input name="lead_source" value="{{f.lead_source}}"></label>{% if current_user.role in ('owner','manager') %}<button class="btn">Save form</button>{% endif %}</form>{% endfor %}</main></body></html>"""

PUBLIC_FORM = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{form.title}}</title><style>{{style}}{{phase11_style}}</style></head><body><main class="public-shell"><section class="public-card"><div class="mark">FO</div><h1 style="margin-top:14px">{{form.title}}</h1><p class="muted">{{form.intro_text}}</p>""" + FLASHES + """{% if success %}<div class="status-note good">{{form.success_text}}</div>{% else %}<form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><div style="position:absolute;left:-9999px" aria-hidden="true"><label>Website<input name="website" tabindex="-1" autocomplete="off"></label></div>{% for field in fields %}<label class="field">{{field.label}}{% if field.required %} *{% endif %}{% if field.type=='textarea' %}<textarea name="{{field.key}}" {% if field.required %}required{% endif %} maxlength="2000"></textarea>{% else %}<input name="{{field.key}}" {% if field.required %}required{% endif %} {% if field.key=='phone' %}type="tel"{% endif %} maxlength="240">{% endif %}</label>{% endfor %}<p class="muted">Submitting this form does not opt you into automated calls or texts.</p><button class="btn">Submit property information</button></form>{% endif %}</section></main></body></html>"""


DEFAULT_WIDGETS = {
    "owner": ["hot_leads", "pipeline", "tasks", "appointments", "tickets", "recent_activity"],
    "manager": ["hot_leads", "pipeline", "tasks", "tickets", "recent_activity"],
    "agent": ["hot_leads", "tasks", "appointments", "recent_activity"],
    "viewer": ["pipeline", "recent_activity"],
}
AVAILABLE_WIDGETS = {"hot_leads": "Hot leads", "pipeline": "Pipeline", "tasks": "Tasks", "appointments": "Appointments", "tickets": "Help tickets", "recent_activity": "Recent activity"}


def register_phase11(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def one(sql, params):
        found = rows(sql, params)
        if not found:
            abort(404)
        return found[0]

    def pipeline_rows(org_id):
        values = [dict(x) for x in rows("SELECT status,COUNT(*) count FROM leads WHERE organization_id=? GROUP BY status ORDER BY count DESC", (org_id,))]
        largest = max([x["count"] for x in values] or [1])
        for item in values:
            item["percent"] = round(item["count"] * 100 / largest)
        return values

    @app.get("/workspace")
    @roles_required("owner", "manager", "agent", "viewer")
    def workspace():
        org = organization_id()
        pref = rows("SELECT widgets_json FROM dashboard_preferences WHERE organization_id=? AND user_id=?", (org, g.user.get("id"))) if g.user.get("id") else []
        widgets = json.loads(pref[0]["widgets_json"]) if pref else DEFAULT_WIDGETS.get(g.user["role"], DEFAULT_WIDGETS["viewer"])
        today = datetime.now(timezone.utc).date().isoformat()
        metrics = rows("""SELECT
            COALESCE(SUM(CASE WHEN status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) active_leads,
            COALESCE(SUM(CASE WHEN follow_up_score>=4 AND status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) hot_leads,
            COALESCE(SUM(CASE WHEN next_follow_up<? AND status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) overdue,
            (SELECT COUNT(*) FROM tasks WHERE organization_id=? AND status='open') open_tasks
            FROM leads WHERE organization_id=?""", (today, org, org))[0]
        data = dict(
            hot_leads=rows("SELECT * FROM leads WHERE organization_id=? AND follow_up_score>=4 AND status NOT IN ('won','lost') ORDER BY follow_up_score DESC,latest_activity DESC LIMIT 8", (org,)),
            tasks=rows("""SELECT t.*,l.owner_name,l.phone_display FROM tasks t JOIN leads l ON l.id=t.lead_id WHERE t.organization_id=? AND t.status='open' ORDER BY t.due_at IS NULL,t.due_at LIMIT 8""", (org,)),
            pipeline=pipeline_rows(org),
            appointments=rows("""SELECT a.*,l.owner_name,l.phone_display FROM appointments a LEFT JOIN leads l ON l.id=a.lead_id WHERE a.organization_id=? AND a.status='scheduled' AND a.starts_at>=? ORDER BY a.starts_at LIMIT 8""", (org, utc_now())),
            tickets=rows("SELECT * FROM support_tickets WHERE organization_id=? AND status IN ('open','in_progress') ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,id DESC LIMIT 8", (org,)),
            activity=rows("SELECT * FROM activity_events WHERE organization_id=? ORDER BY occurred_at DESC,id DESC LIMIT 8", (org,)),
        )
        return render_template_string(WORKSPACE, style=style, phase11_style=PHASE11_STYLE, widgets=widgets, available=AVAILABLE_WIDGETS, metrics=metrics, **data)

    @app.post("/workspace/preferences")
    @roles_required("owner", "manager", "agent", "viewer")
    def workspace_preferences():
        require_csrf()
        widgets = [x for x in request.form.getlist("widgets") if x in AVAILABLE_WIDGETS]
        if not widgets:
            flash("Select at least one widget.", "error")
            return redirect(url_for("workspace"))
        if not g.user.get("id"):
            flash("Workspace preferences require a signed-in user.", "error")
            return redirect(url_for("workspace"))
        with db.connect() as connection:
            connection.execute("""INSERT INTO dashboard_preferences(organization_id,user_id,widgets_json,updated_at) VALUES (?,?,?,?) ON CONFLICT(organization_id,user_id) DO UPDATE SET widgets_json=excluded.widgets_json,updated_at=excluded.updated_at""", (organization_id(), g.user["id"], json.dumps(widgets), utc_now()))
        audit("dashboard.preferences_updated", "user", g.user["id"], {"widgets": widgets})
        return redirect(url_for("workspace"))

    @app.get("/reports")
    @roles_required("owner", "manager", "agent", "viewer")
    def reports():
        days = request.args.get("days", type=int) or 30
        days = days if days in {7, 30, 90, 365} else 30
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        org = organization_id()
        calls = rows("SELECT direction,COUNT(*) count FROM calls WHERE organization_id=? AND COALESCE(occurred_at,created_at)>=? GROUP BY direction ORDER BY count DESC", (org, cutoff))
        sources = rows("SELECT COALESCE(NULLIF(lead_source,''),'Unspecified') source,COUNT(*) count FROM leads WHERE organization_id=? AND created_at>=? GROUP BY source ORDER BY count DESC LIMIT 10", (org, cutoff))
        today = datetime.now(timezone.utc).date()
        followup = rows("""SELECT
            COALESCE(SUM(CASE WHEN next_follow_up<? AND status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) overdue,
            COALESCE(SUM(CASE WHEN next_follow_up>=? AND next_follow_up<=? AND status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) next7,
            COALESCE(SUM(CASE WHEN (next_follow_up IS NULL OR next_follow_up='') AND status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) unscheduled,
            COALESCE(SUM(CASE WHEN seller_motivation!='' AND status NOT IN ('won','lost') THEN 1 ELSE 0 END),0) motivated
            FROM leads WHERE organization_id=?""", (today.isoformat(), today.isoformat(), (today + timedelta(days=7)).isoformat(), org))[0]
        saved = rows("SELECT * FROM saved_reports WHERE organization_id=? ORDER BY id DESC", (org,))
        return render_template_string(REPORTS, style=style, phase11_style=PHASE11_STYLE, days=days, pipeline=pipeline_rows(org), calls=calls, sources=sources, followup=followup, saved=saved)

    @app.post("/reports/save")
    @roles_required("owner", "manager")
    def save_report():
        require_csrf()
        name = request.form.get("name", "").strip()[:120]
        report_type = request.form.get("report_type", "")
        if not name or report_type not in {"pipeline", "activity", "sources", "follow_up"}:
            flash("Enter a unique report name and valid report type.", "error")
            return redirect(url_for("reports"))
        try:
            with db.connect() as connection:
                connection.execute("INSERT INTO saved_reports(organization_id,name,report_type,config_json,created_by,created_at) VALUES (?,?,?,?,?,?)", (organization_id(), name, report_type, json.dumps({"days": request.form.get("days", "30")}), g.user.get("id"), utc_now()))
        except Exception:
            flash("That saved report name is already in use.", "error")
        else:
            audit("report.saved", "saved_report", None, {"name": name, "type": report_type})
            flash("Report view saved.", "good")
        return redirect(url_for("reports", days=request.form.get("days", "30")))

    @app.get("/help")
    @roles_required("owner", "manager", "agent", "viewer")
    def help_center():
        return render_template_string(HELP, style=style, phase11_style=PHASE11_STYLE, articles=rows("SELECT * FROM help_articles WHERE organization_id=? ORDER BY position", (organization_id(),)), tickets=rows("SELECT * FROM support_tickets WHERE organization_id=? ORDER BY updated_at DESC LIMIT 30", (organization_id(),)))

    @app.route("/help/tickets/new", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def new_ticket():
        if request.method == "POST":
            require_csrf()
            subject = request.form.get("subject", "").strip()[:160]
            description = request.form.get("description", "").strip()[:10000]
            priority = request.form.get("priority", "normal")
            lead_id = request.form.get("lead_id", "").strip()
            if not subject or not description or priority not in {"low", "normal", "high", "urgent"}:
                flash("Subject, description, and valid priority are required.", "error")
            else:
                linked = None
                if lead_id:
                    candidate = rows("SELECT id FROM leads WHERE id=? AND organization_id=?", (lead_id, organization_id()))
                    if not candidate:
                        flash("Related lead was not found.", "error")
                        return render_template_string(TICKET_FORM, style=style, phase11_style=PHASE11_STYLE), 400
                    linked = int(lead_id)
                with db.connect() as connection:
                    cursor = connection.execute("""INSERT INTO support_tickets(organization_id,subject,description,status,priority,lead_id,created_by,created_at,updated_at) VALUES (?,?,?,'open',?,?,?,?,?)""", (organization_id(), subject, description, priority, linked, g.user.get("id"), utc_now(), utc_now()))
                    ticket_id = cursor.lastrowid
                audit("ticket.created", "support_ticket", ticket_id, {"priority": priority})
                return redirect(url_for("ticket_detail", ticket_id=ticket_id))
        return render_template_string(TICKET_FORM, style=style, phase11_style=PHASE11_STYLE)

    @app.route("/help/tickets/<int:ticket_id>", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent", "viewer")
    def ticket_detail(ticket_id):
        ticket = one("SELECT * FROM support_tickets WHERE id=? AND organization_id=?", (ticket_id, organization_id()))
        if request.method == "POST":
            if g.user["role"] not in ("owner", "manager"):
                abort(403)
            require_csrf()
            status = request.form.get("status", "")
            if status not in {"open", "in_progress", "resolved", "closed"}:
                abort(400)
            resolution = request.form.get("resolution", "").strip()[:10000]
            resolved_at = utc_now() if status in {"resolved", "closed"} else None
            with db.connect() as connection:
                connection.execute("UPDATE support_tickets SET status=?,resolution=?,resolved_at=?,updated_at=? WHERE id=?", (status, resolution, resolved_at, utc_now(), ticket_id))
            audit("ticket.updated", "support_ticket", ticket_id, {"status": status})
            return redirect(request.path)
        return render_template_string(TICKET, style=style, phase11_style=PHASE11_STYLE, ticket=ticket)

    @app.get("/forms")
    @roles_required("owner", "manager", "agent", "viewer")
    def forms_admin():
        return render_template_string(FORMS_ADMIN, style=style, phase11_style=PHASE11_STYLE, forms=rows("SELECT * FROM public_forms WHERE organization_id=? ORDER BY name", (organization_id(),)))

    @app.post("/forms/<int:form_id>")
    @roles_required("owner", "manager")
    def edit_public_form(form_id):
        require_csrf()
        form = one("SELECT * FROM public_forms WHERE id=? AND organization_id=?", (form_id, organization_id()))
        title = request.form.get("title", "").strip()[:160]
        if not title:
            flash("Form title is required.", "error")
            return redirect(url_for("forms_admin"))
        with db.connect() as connection:
            connection.execute("UPDATE public_forms SET title=?,intro_text=?,success_text=?,lead_source=?,active=?,updated_at=? WHERE id=?", (title, request.form.get("intro_text", "").strip()[:2000], request.form.get("success_text", "").strip()[:1000], request.form.get("lead_source", "").strip()[:120] or "Public form", int(request.form.get("active") == "yes"), utc_now(), form_id))
        audit("form.updated", "public_form", form_id, {"slug": form["slug"]})
        return redirect(url_for("forms_admin"))

    @app.route("/f/<slug>", methods=["GET", "POST"])
    def public_lead_form(slug):
        forms = rows("SELECT * FROM public_forms WHERE slug=? AND active=1", (slug,))
        if not forms:
            abort(404)
        form = forms[0]
        fields = json.loads(form["fields_json"])
        success = False
        if request.method == "POST":
            require_csrf()
            if request.form.get("website"):
                return render_template_string(PUBLIC_FORM, style=style, phase11_style=PHASE11_STYLE, form=form, fields=fields, success=True)
            values = {field["key"]: request.form.get(field["key"], "").strip()[:2000] for field in fields}
            missing = [field["label"] for field in fields if field.get("required") and not values.get(field["key"])]
            digits = re.sub(r"\D", "", values.get("phone", ""))
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if missing or len(digits) != 10:
                flash("Complete the required fields and enter a valid 10-digit phone number.", "error")
            else:
                display = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
                lead_id = db.ensure_lead(digits, display, int(form["organization_id"]))
                name = sanitize_owner_name(values.get("owner_name", ""))
                refs = extract_references_from_text(values.get("reference_number", ""))
                reference = ", ".join(refs) or values.get("reference_number", "")[:120].upper()
                notes = "Public form submission"
                if values.get("property_location"):
                    notes += f"\nProperty location: {values['property_location']}"
                if values.get("selling_reason"):
                    notes += f"\nSeller comments: {values['selling_reason']}"
                fingerprint = hashlib.sha256(f"{request.remote_addr or ''}|{request.headers.get('User-Agent','')}".encode()).hexdigest()
                with db.connect() as connection:
                    connection.execute("""UPDATE leads SET owner_name=CASE WHEN ?!='' THEN ? ELSE owner_name END,reference_number=CASE WHEN ?!='' THEN ? ELSE reference_number END,lead_source=?,notes=CASE WHEN notes='' THEN ? ELSE notes||'\n\n'||? END,updated_at=? WHERE id=? AND organization_id=?""", (name, name, reference, reference, form["lead_source"], notes, notes, utc_now(), lead_id, form["organization_id"]))
                    connection.execute("INSERT INTO form_submissions(organization_id,form_id,lead_id,payload_json,request_fingerprint,submitted_at) VALUES (?,?,?,?,?,?)", (form["organization_id"], form["id"], lead_id, json.dumps(values, sort_keys=True), fingerprint, utc_now()))
                db.sync_contact_for_lead(lead_id)
                success = True
        return render_template_string(PUBLIC_FORM, style=style, phase11_style=PHASE11_STYLE, form=form, fields=fields, success=success)

    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        payload = {"name": "Fuller Oak Lead CRM", "short_name": "Lead CRM", "start_url": "/workspace", "scope": "/", "display": "standalone", "background_color": "#f5f3ed", "theme_color": "#235d4d", "icons": [{"src": "/pwa-icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
        return Response(json.dumps(payload), mimetype="application/manifest+json")

    @app.get("/pwa-icon.svg")
    def pwa_icon():
        return Response("""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='100' fill='#235d4d'/><path d='M120 120h272v70H205v65h155v68H205v129h-85z' fill='white'/></svg>""", mimetype="image/svg+xml")

    @app.get("/service-worker.js")
    def service_worker():
        script = """const C='lead-crm-v1';self.addEventListener('install',e=>e.waitUntil(caches.open(C).then(c=>c.addAll(['/manifest.webmanifest','/pwa-icon.svg','/offline']))));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request).then(r=>r||caches.match('/offline'))))});"""
        return Response(script, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/offline")
    def offline_page():
        return Response("<!doctype html><meta name='viewport' content='width=device-width'><title>CRM offline</title><style>body{font:18px system-ui;padding:40px;background:#f5f3ed;color:#18201e}</style><h1>You are offline</h1><p>Reconnect to safely access live CRM data. No seller information is cached for offline use.</p>", mimetype="text/html")

    @app.after_request
    def add_pwa_assets(response):
        if response.content_type and response.content_type.startswith("text/html"):
            body = response.get_data(as_text=True)
            if "manifest.webmanifest" not in body and "</head>" in body:
                body = body.replace("</head>", "<link rel='manifest' href='/manifest.webmanifest'><meta name='theme-color' content='#235d4d'></head>", 1)
            if "serviceWorker.register" not in body and "</body>" in body:
                body = body.replace("</body>", "<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/service-worker.js')}</script></body>", 1)
            response.set_data(body)
        return response
