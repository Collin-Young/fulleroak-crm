"""Phase 4 unified activity ledger and inbox views."""

from __future__ import annotations

import json

from flask import abort, render_template_string, request, url_for


PHASE4_STYLE = """
.inbox-layout{display:grid;grid-template-columns:minmax(280px,390px) 1fr;gap:18px}.conversation{display:block;padding:14px;border-bottom:1px solid var(--line);text-decoration:none}.conversation:hover{background:#f7f8f5}.conversation-head{display:flex;justify-content:space-between;gap:10px}.conversation-name{font-weight:800}.conversation-time{font-size:12px;color:var(--muted);white-space:nowrap}.conversation-snippet{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:5px}.event{position:relative;border-left:3px solid var(--line);padding:0 0 24px 18px;margin-left:8px}.event:before{content:'';position:absolute;left:-7px;top:4px;width:11px;height:11px;border-radius:50%;background:var(--muted)}.event.call{border-color:#54778c}.event.call:before{background:#54778c}.event.sms{border-color:#235d4d}.event.sms:before{background:#235d4d}.event.system{border-color:#b4813d}.event.system:before{background:#b4813d}.event-head{display:flex;justify-content:space-between;gap:12px}.event-body{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:9px;background:#f7f7f3;border-radius:9px;padding:12px}.channel-icon{display:inline-grid;place-items:center;min-width:28px;height:28px;border-radius:50%;background:#eceeea;font-size:12px;font-weight:800}.pager{display:flex;justify-content:space-between;align-items:center;margin-top:14px}.filter-tabs{display:flex;gap:7px;flex-wrap:wrap}.filter-tabs a{text-decoration:none}.filter-tabs .active{background:var(--accent);color:white}@media(max-width:800px){.inbox-layout{grid-template-columns:1fr}.event-head{display:block}.conversation-time{white-space:normal}}
"""


INBOX_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Inbox · CRM</title><style>{{style}}{{phase4_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Inbox</h1><div class="sub">Every seller conversation in one place</div></div><a class="btn secondary" href="{{url_for('activity_feed')}}">All activity</a></header><form class="toolbar" method="get"><input name="q" value="{{q}}" placeholder="Search seller, phone, or reference..."><select name="channel"><option value="">Calls and texts</option><option value="call" {% if channel=='call' %}selected{% endif %}>Calls</option><option value="sms" {% if channel=='sms' %}selected{% endif %}>Texts</option><option value="email" {% if channel=='email' %}selected{% endif %}>Email</option></select><button class="btn">Apply</button><a class="btn secondary" href="{{url_for('inbox')}}">Clear</a></form><section class="panel" style="padding:0;overflow:hidden"><div>{% for item in conversations %}<a class="conversation" href="{{url_for('inbox_conversation',lead_id=item.lead_id)}}"><div class="conversation-head"><span class="conversation-name">{{item.owner_name or item.phone_display}}</span><span class="conversation-time">{{item.occurred_at}}</span></div><div class="muted">{{item.phone_display}}{% if item.reference_number %} · {{item.reference_number}}{% endif %} · {{item.event_count}} events</div><div class="conversation-snippet"><span class="pill">{{item.channel}}</span> {{item.title}}{% if item.body %} — {{item.body|replace('\n',' ')}}{% endif %}</div></a>{% else %}<div class="empty">No conversations match these filters.</div>{% endfor %}</div></section><div class="pager">{% if page>1 %}<a class="btn secondary" href="{{url_for('inbox',page=page-1,q=q,channel=channel)}}">← Previous</a>{% else %}<span></span>{% endif %}<span class="muted">Page {{page}}</span>{% if has_more %}<a class="btn secondary" href="{{url_for('inbox',page=page+1,q=q,channel=channel)}}">Next →</a>{% endif %}</div></main></body></html>
"""


CONVERSATION_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{lead.owner_name or lead.phone_display}} · Inbox</title><style>{{style}}{{phase4_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('inbox')}}">← Inbox</a><h1 style="margin-top:8px">{{lead.owner_name or lead.phone_display}}</h1><div class="sub">{{lead.phone_display}}{% if lead.reference_number %} · {{lead.reference_number}}{% endif %}</div></div><div class="top-actions"><a class="btn secondary" href="{{url_for('lead_detail',lead_id=lead.id)}}">Lead, calls & Vumber</a>{% if opportunity %}<a class="btn secondary" href="{{url_for('deal_detail',opportunity_id=opportunity.id)}}">Open deal</a>{% endif %}</div></header><nav class="filter-tabs" aria-label="Activity channel filters">{% for value,label in channels %}<a class="btn secondary {% if channel==value %}active{% endif %}" href="{{url_for('inbox_conversation',lead_id=lead.id,channel=value)}}">{{label}}</a>{% endfor %}</nav><section class="panel" style="margin-top:16px"><h2>Complete timeline</h2>{% for event in events %}<article class="event {{event.channel}}"><div class="event-head"><div><span class="channel-icon">{{event.channel[:1]|upper}}</span> <b>{{event.title}}</b> <span class="pill">{{event.direction}}</span></div><span class="muted">{{event.occurred_at}}</span></div>{% if event.body %}<div class="event-body">{{event.body}}</div>{% endif %}{% if event.source_table=='calls' and event.metadata.get('recording') %}<audio controls preload="none" style="width:min(100%,560px);margin-top:10px" src="{{url_for('call_audio',call_id=event.source_id)}}">Your browser cannot play this recording.</audio>{% endif %}</article>{% else %}<div class="empty">No activity in this channel.</div>{% endfor %}</section><div class="pager">{% if page>1 %}<a class="btn secondary" href="{{url_for('inbox_conversation',lead_id=lead.id,page=page-1,channel=channel)}}">← Newer</a>{% else %}<span></span>{% endif %}<span class="muted">Page {{page}}</span>{% if has_more %}<a class="btn secondary" href="{{url_for('inbox_conversation',lead_id=lead.id,page=page+1,channel=channel)}}">Older →</a>{% endif %}</div></main></body></html>
"""


ACTIVITY_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Activity · CRM</title><style>{{style}}{{phase4_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('inbox')}}">← Inbox</a><h1 style="margin-top:8px">All activity</h1><div class="sub">Immutable organization-wide event history</div></div></header><form class="toolbar" method="get"><input name="q" value="{{q}}" placeholder="Search event, seller, phone, reference..."><select name="channel"><option value="">All channels</option>{% for value,label in channels[1:] %}<option value="{{value}}" {% if channel==value %}selected{% endif %}>{{label}}</option>{% endfor %}</select><select name="direction"><option value="">Any direction</option>{% for value in ('incoming','outgoing','missed','internal','unknown') %}<option value="{{value}}" {% if direction==value %}selected{% endif %}>{{value|title}}</option>{% endfor %}</select><button class="btn">Apply</button><a class="btn secondary" href="{{url_for('activity_feed')}}">Clear</a></form><section class="panel">{% for event in events %}<article class="event {{event.channel}}"><div class="event-head"><div><span class="channel-icon">{{event.channel[:1]|upper}}</span> <b>{{event.title}}</b> <span class="pill">{{event.channel}}</span> <span class="pill">{{event.direction}}</span></div><span class="muted">{{event.occurred_at}}</span></div>{% if event.lead_id %}<div class="muted"><a href="{{url_for('inbox_conversation',lead_id=event.lead_id)}}">{{event.owner_name or event.phone_display}}</a>{% if event.reference_number %} · {{event.reference_number}}{% endif %}</div>{% endif %}{% if event.body %}<details><summary class="muted" style="cursor:pointer;margin-top:7px">Show full details</summary><div class="event-body">{{event.body}}</div></details>{% endif %}</article>{% else %}<div class="empty">No activity matches these filters.</div>{% endfor %}</section><div class="pager">{% if page>1 %}<a class="btn secondary" href="{{url_for('activity_feed',page=page-1,q=q,channel=channel,direction=direction)}}">← Newer</a>{% else %}<span></span>{% endif %}<span class="muted">Page {{page}}</span>{% if has_more %}<a class="btn secondary" href="{{url_for('activity_feed',page=page+1,q=q,channel=channel,direction=direction)}}">Older →</a>{% endif %}</div></main></body></html>
"""


CHANNELS = (("", "All"), ("call", "Calls"), ("sms", "Texts"), ("email", "Email"), ("document", "Documents"), ("system", "System"), ("note", "Notes"))


def register_phase4(app, db, style, rows, organization_id):
    def page_number():
        try:
            return max(1, int(request.args.get("page", "1")))
        except ValueError:
            return 1

    @app.get("/inbox")
    def inbox():
        page, size = page_number(), 50
        q = request.args.get("q", "").strip()
        channel = request.args.get("channel", "").strip()
        allowed = {"call", "sms", "email"}
        params = [organization_id()]
        filters = ["a.organization_id=?", "a.lead_id IS NOT NULL"]
        if channel in allowed:
            filters.append("a.channel=?")
            params.append(channel)
        else:
            channel = ""
            filters.append("a.channel IN ('call','sms','email')")
        query_params = list(params)
        q_clause = ""
        if q:
            q_clause = " AND (l.owner_name LIKE ? OR l.phone_display LIKE ? OR l.reference_number LIKE ?)"
            query_params.extend([f"%{q}%"] * 3)
        query_params.extend([size + 1, (page - 1) * size])
        conversations = rows(f"""WITH ranked AS (
            SELECT a.*,COUNT(*) OVER(PARTITION BY a.lead_id) event_count,
                ROW_NUMBER() OVER(PARTITION BY a.lead_id ORDER BY a.occurred_at DESC,a.id DESC) sequence
            FROM activity_events a WHERE {' AND '.join(filters)}
        ) SELECT r.*,l.owner_name,l.phone_display,l.reference_number
          FROM ranked r JOIN leads l ON l.id=r.lead_id AND l.organization_id=r.organization_id
          WHERE r.sequence=1 {q_clause}
          ORDER BY r.occurred_at DESC,r.id DESC LIMIT ? OFFSET ?""", query_params)
        has_more = len(conversations) > size
        return render_template_string(INBOX_TEMPLATE, style=style, phase4_style=PHASE4_STYLE, conversations=conversations[:size], page=page, has_more=has_more, q=q, channel=channel)

    @app.get("/inbox/<int:lead_id>")
    def inbox_conversation(lead_id: int):
        found = rows("SELECT * FROM leads WHERE id=? AND organization_id=?", (lead_id, organization_id()))
        if not found:
            abort(404)
        page, size = page_number(), 50
        channel = request.args.get("channel", "").strip()
        valid_channels = {item[0] for item in CHANNELS}
        if channel not in valid_channels:
            channel = ""
        clause, params = "", [organization_id(), lead_id]
        if channel:
            clause = " AND channel=?"
            params.append(channel)
        params.extend([size + 1, (page - 1) * size])
        events = rows(f"""SELECT * FROM activity_events WHERE organization_id=? AND lead_id=? {clause}
            ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?""", params)
        has_more = len(events) > size
        events = events[:size]
        for event in events:
            try:
                event["metadata"] = json.loads(event["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                event["metadata"] = {}
        opportunity = rows("SELECT id FROM opportunities WHERE organization_id=? AND lead_id=?", (organization_id(), lead_id))
        return render_template_string(CONVERSATION_TEMPLATE, style=style, phase4_style=PHASE4_STYLE, lead=found[0], opportunity=opportunity[0] if opportunity else None, events=events, page=page, has_more=has_more, channel=channel, channels=CHANNELS)

    @app.get("/activity")
    def activity_feed():
        page, size = page_number(), 100
        q = request.args.get("q", "").strip()
        channel = request.args.get("channel", "").strip()
        direction = request.args.get("direction", "").strip()
        where, params = ["a.organization_id=?"], [organization_id()]
        if channel in {item[0] for item in CHANNELS if item[0]}:
            where.append("a.channel=?")
            params.append(channel)
        else:
            channel = ""
        if direction in {"incoming", "outgoing", "missed", "internal", "unknown"}:
            where.append("a.direction=?")
            params.append(direction)
        else:
            direction = ""
        if q:
            where.append("(a.title LIKE ? OR a.body LIKE ? OR l.owner_name LIKE ? OR l.phone_display LIKE ? OR l.reference_number LIKE ?)")
            params.extend([f"%{q}%"] * 5)
        events = rows(f"""SELECT a.*,l.owner_name,l.phone_display,l.reference_number
            FROM activity_events a LEFT JOIN leads l ON l.id=a.lead_id AND l.organization_id=a.organization_id
            WHERE {' AND '.join(where)} ORDER BY a.occurred_at DESC,a.id DESC LIMIT ? OFFSET ?""", [*params, size + 1, (page - 1) * size])
        has_more = len(events) > size
        return render_template_string(ACTIVITY_TEMPLATE, style=style, phase4_style=PHASE4_STYLE, events=events[:size], page=page, has_more=has_more, q=q, channel=channel, direction=direction, channels=CHANNELS)
