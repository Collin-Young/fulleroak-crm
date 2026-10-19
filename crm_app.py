"""Local browser CRM for Vumber leads."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template_string, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from auth_security import ADMIN_ROLES, ROLES, WRITE_ROLES, LoginRateLimiter, load_or_create_secret
from crm_phase2 import register_phase2
from crm_phase3 import register_phase3
from crm_phase4 import register_phase4
from crm_phase5 import register_phase5
from crm_phase6 import register_phase6
from crm_phase7 import register_phase7
from crm_phase8 import register_phase8
from crm_phase9 import register_phase9
from crm_phase10 import register_phase10
from crm_phase11 import register_phase11
from crm_phase12 import register_phase12
from crm_phase13 import register_phase13
from crm_text_campaigns import register_text_campaigns
from crm_db import CRMDatabase, utc_now
from lead_pipeline import extract_reference_digit_groups
from message_drafts import context_hash as draft_context_hash, generate_drafts
from offer_db import OfferDatabase, parse_references
from vumber_client import VumberClient, VumberError, VumberSessionStore


STAGES = ("new", "contacted", "qualified", "nurture", "won", "lost")
SORTS = {
    "recent": "COALESCE(l.latest_activity, '') DESC, l.id DESC",
    "oldest": "l.latest_activity IS NULL, l.latest_activity ASC, l.id ASC",
    "priority": "l.follow_up_score DESC, COALESCE(l.latest_activity, '') DESC",
    "calls": "call_count DESC, COALESCE(l.latest_activity, '') DESC",
    "name": "l.owner_name='', l.owner_name COLLATE NOCASE, l.phone_display",
}
RECENCY = {"7": "-7 days", "30": "-30 days", "90": "-90 days", "365": "-365 days"}

BASE_STYLE = """
:root{--ink:#18201e;--muted:#68736f;--paper:#f5f3ed;--card:#fff;--line:#deddd5;--accent:#235d4d;--accent2:#d67545;--urgent:#b63f36}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}a{color:inherit}.shell{max-width:1320px;margin:auto;padding:28px}.top{display:flex;align-items:center;gap:18px;justify-content:space-between;margin-bottom:24px}h1{font:700 30px/1.1 Georgia,serif;margin:0}h2{font:700 22px/1.2 Georgia,serif;margin:0 0 14px}h3{margin:0 0 8px}.sub,.muted{color:var(--muted)}.brand{display:flex;align-items:center;gap:12px}.mark{width:38px;height:38px;border-radius:12px;background:var(--accent);color:white;display:grid;place-items:center;font-weight:800}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}.stat,.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 4px 18px #1d29230b}.stat b{display:block;font-size:24px}.stat span{color:var(--muted);font-size:13px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.toolbar input,.toolbar select,input,select,textarea{border:1px solid var(--line);border-radius:9px;padding:10px 12px;background:#fff;font:inherit}.toolbar input{min-width:270px}.btn{border:0;border-radius:9px;background:var(--accent);color:white;padding:10px 14px;text-decoration:none;cursor:pointer;font-weight:650}.btn.secondary{background:#e8ece9;color:var(--ink)}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:14px;overflow:hidden;border:1px solid var(--line)}th{text-align:left;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em;background:#faf9f5}th,td{padding:13px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border:0}tr:hover td{background:#fbfcfa}.leadlink{font-weight:750;text-decoration:none}.score{display:inline-grid;place-items:center;width:29px;height:29px;border-radius:50%;background:#e6efe9;color:var(--accent);font-weight:800}.score.s5,.score.s4{background:#fae6e3;color:var(--urgent)}.pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#eceeea;font-size:12px;text-transform:capitalize}.pill.qualified,.pill.won{background:#dceee5;color:#14583f}.pill.lost{background:#eee1df;color:#7c302c}.plan{max-width:420px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}.stack{display:grid;gap:18px}.field{display:grid;gap:5px;margin-bottom:12px}.field textarea{min-height:110px;resize:vertical}.call{border-top:1px solid var(--line);padding:15px 0}.call:first-of-type{border-top:0}.call summary{cursor:pointer;font-weight:700}.transcript,.summary-text{white-space:pre-wrap;overflow:visible;max-height:none;overflow-wrap:anywhere}.transcript{background:#f7f7f3;padding:13px;border-radius:9px;margin-top:10px}.signal-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0}.signal{background:#f7f7f3;border-radius:10px;padding:12px}.signal b{display:block;margin-bottom:5px}.call audio{display:block;width:min(100%,560px);margin-top:10px}.task{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line)}.task:last-child{border:0}.empty{text-align:center;padding:42px;color:var(--muted)}
.offer{border:1px solid var(--line);border-radius:11px;padding:14px;margin-top:12px;background:#fbfcfa}.offer-head{display:flex;gap:12px;justify-content:space-between;align-items:flex-start}.offer-price{font-size:22px;font-weight:800;color:var(--accent);white-space:nowrap}.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px}.fact{min-width:0;overflow-wrap:anywhere}.fact b{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.status-note{border-left:4px solid var(--accent2);padding:9px 11px;background:#fff7f1;border-radius:5px;margin:10px 0}.status-note.good{border-color:var(--accent);background:#f0f8f3}.status-note.error{border-color:var(--urgent);background:#fff0ee}.flash{margin:0 0 12px}.check{display:flex;gap:8px;align-items:flex-start;margin:10px 0}.check input{margin-top:4px}.comm{border-top:1px solid var(--line);padding:11px 0}.comm:first-of-type{border:0}.comm-message{white-space:pre-wrap;margin-top:5px}.top-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.draft{border:1px solid var(--line);border-radius:10px;padding:12px;margin:10px 0;background:#fbfcfa}.draft-message{white-space:pre-wrap;margin:7px 0}.draft-head{display:flex;justify-content:space-between;gap:8px}.wide-form{max-width:720px}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:800px){.shell{padding:16px}.stats{grid-template-columns:1fr 1fr}.grid,.signal-grid,.facts{grid-template-columns:1fr}.table-wrap{overflow:auto}.top{align-items:flex-start;flex-direction:column}.offer-head{display:block}.offer-price{margin-top:8px}}
"""

AUTH_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} · CRM</title><style>{{style}}</style></head><body><main class="shell" style="max-width:520px"><header class="top"><div class="brand"><div class="mark">VL</div><div><h1>{{title}}</h1><div class="sub">{{subtitle}}</div></div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}">{% if setup %}<label class="field">Your name<input name="display_name" autocomplete="name" required autofocus></label>{% endif %}<label class="field">Email<input type="email" name="email" autocomplete="username" required {% if not setup %}autofocus{% endif %}></label><label class="field">Password<input type="password" name="password" autocomplete="{{'new-password' if setup else 'current-password'}}" minlength="12" required></label><button class="btn">{{button}}</button></form></main></body></html>
"""

TEAM_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Team · CRM</title><style>{{style}}</style></head><body><main class="shell" style="max-width:980px"><header class="top"><div><a class="muted" href="/">← All leads</a><h1 style="margin-top:8px">Team access</h1><div class="sub">People who can access this organization's CRM</div></div><a class="btn secondary" href="{{url_for('audit_log')}}">Audit log</a></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><section><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>{% for member in members %}<tr><td>{{member.display_name}}</td><td>{{member.email}}</td><td>{{member.role}}</td><td>{{'Active' if member.active else 'Disabled'}}</td><td>{% if user.role == 'owner' and member.id != user.id %}<form method="post" action="{{url_for('toggle_user',user_id=member.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">{{'Disable' if member.active else 'Enable'}}</button></form>{% endif %}</td></tr>{% endfor %}</tbody></table></section><form class="panel" method="post"><h2>Add team member</h2><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Name<input name="display_name" required></label><label class="field">Email<input type="email" name="email" required></label><label class="field">Temporary password<input type="password" name="password" minlength="12" required></label><label class="field">Role<select name="role">{% for role in roles %}{% if role != 'owner' or user.role == 'owner' %}<option value="{{role}}">{{role|title}}</option>{% endif %}{% endfor %}</select></label><button class="btn">Create user</button></form></div></main></body></html>
"""

AUDIT_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Audit log · CRM</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← All leads</a><h1 style="margin-top:8px">Audit log</h1><div class="sub">Recent security and data changes for this organization</div></div><a class="btn secondary" href="{{url_for('team_settings')}}">Team access</a></header><div class="table-wrap"><table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Record</th><th>Details</th><th>IP</th></tr></thead><tbody>{% for event in events %}<tr><td>{{event.created_at}}</td><td>{{event.display_name or 'System'}}</td><td>{{event.action}}</td><td>{{event.entity_type}} {{event.entity_id}}</td><td>{{event.details_json}}</td><td>{{event.ip_address}}</td></tr>{% else %}<tr><td colspan="6" class="empty">No audit events yet.</td></tr>{% endfor %}</tbody></table></div></main></body></html>
"""

LIST_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Vumber Lead CRM</title><style>{{style}}</style></head><body><main class="shell">
<header class="top"><div class="brand"><div class="mark">VL</div><div><h1>Vumber Lead CRM</h1><div class="sub">Calls, context, and the next best action{% if current_user %} · {{current_user.display_name}} ({{current_user.role}}){% endif %}</div></div></div><div class="top-actions"><a class="btn secondary" href="{{url_for('workspace')}}">Workspace</a><a class="btn secondary" href="{{url_for('contacts')}}">Contacts</a><a class="btn secondary" href="{{url_for('properties')}}">Properties</a><a class="btn secondary" href="{{url_for('deals_board')}}">Deals</a><a class="btn secondary" href="{{url_for('inbox')}}">Inbox</a><a class="btn secondary" href="{{url_for('automations')}}">Automations</a><a class="btn secondary" href="{{url_for('ai_dashboard')}}">AI Assist</a><a class="btn secondary" href="{{url_for('documents')}}">Documents</a><a class="btn secondary" href="{{url_for('calendar')}}">Calendar</a><a class="btn secondary" href="{{url_for('projects')}}">Projects</a><a class="btn secondary" href="{{url_for('communication_settings')}}">Communication</a>{% if current_user.role in ('owner','manager','agent') %}<a class="btn" href="{{url_for('new_lead')}}">+ Add lead</a>{% endif %}{% if current_user.role in admin_roles %}<a class="btn secondary" href="{{url_for('vumber_settings')}}">Vumber connection</a><a class="btn secondary" href="{{url_for('integration_settings')}}">API & webhooks</a><a class="btn secondary" href="{{url_for('team_settings')}}">Team & audit</a>{% endif %}<a class="btn secondary" href="{{url_for('health')}}">System status</a><form method="post" action="{{url_for('logout')}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Sign out</button></form></div></header>
<section class="stats"><div class="stat"><b>{{stats.total}}</b><span>Total leads</span></div><div class="stat"><b>{{stats.hot}}</b><span>Priority 4-5</span></div><div class="stat"><b>{{stats.open_tasks}}</b><span>Open follow-ups</span></div><div class="stat"><b>{{stats.calls}}</b><span>Calls captured</span></div></section>
<form class="toolbar" method="get"><input name="q" value="{{q}}" placeholder="Search name, phone, reference, motivation..."><select name="status"><option value="">All stages</option>{% for s in stages %}<option value="{{s}}" {% if status==s %}selected{% endif %}>{{s|title}}</option>{% endfor %}</select><select name="score"><option value="">All priorities</option>{% for n in range(5,0,-1) %}<option value="{{n}}" {% if score==n|string %}selected{% endif %}>Score {{n}}</option>{% endfor %}</select><select name="recency"><option value="">Any activity date</option><option value="7" {% if recency=='7' %}selected{% endif %}>Active in last 7 days</option><option value="30" {% if recency=='30' %}selected{% endif %}>Active in last 30 days</option><option value="90" {% if recency=='90' %}selected{% endif %}>Active in last 90 days</option><option value="365" {% if recency=='365' %}selected{% endif %}>Active in last year</option></select><select name="sort"><option value="recent" {% if sort=='recent' %}selected{% endif %}>Most recent activity</option><option value="oldest" {% if sort=='oldest' %}selected{% endif %}>Oldest activity</option><option value="priority" {% if sort=='priority' %}selected{% endif %}>Highest priority</option><option value="calls" {% if sort=='calls' %}selected{% endif %}>Most calls</option><option value="name" {% if sort=='name' %}selected{% endif %}>Name A-Z</option></select><button class="btn">Apply</button><a class="btn secondary" href="/">Clear</a></form>
<div class="table-wrap"><table><thead><tr><th>Priority</th><th>Lead</th><th>Phone number</th><th>Stage</th><th>Activity</th><th>Next action</th></tr></thead><tbody>{% for lead in leads %}<tr><td><span class="score s{{lead.follow_up_score}}">{{lead.follow_up_score}}</span></td><td><a class="leadlink" href="{{url_for('lead_detail',lead_id=lead.id)}}">{{lead.owner_name or 'Unknown lead'}}</a><div class="muted">{{lead.call_count}} call{{'' if lead.call_count==1 else 's'}}{% if lead.reference_number %} &middot; Ref {{lead.reference_number}}{% endif %}</div></td><td><a class="leadlink" href="tel:{{lead.phone_normalized}}">{{lead.phone_display}}</a></td><td><span class="pill {{lead.status}}">{{lead.status}}</span></td><td>{{lead.latest_activity or 'Unknown'}}{% if lead.next_follow_up %}<div class="muted">Follow up: {{lead.next_follow_up}}</div>{% endif %}</td><td class="plan">{{lead.follow_up_plan or 'Review this lead'}}</td></tr>{% else %}<tr><td colspan="6" class="empty">No leads match these filters.</td></tr>{% endfor %}</tbody></table></div>
</main><script>
setInterval(function () {
  if (document.visibilityState === "visible" &&
      !document.querySelector("input:focus, textarea:focus, select:focus")) {
    window.location.reload();
  }
}, 30000);
</script></body></html>
"""

LIST_TEMPLATE = LIST_TEMPLATE.replace(
    '<a class="btn secondary" href="{{url_for(\'deals_board\')}}">Deals</a>',
    '<a class="btn secondary" href="{{url_for(\'deals_board\')}}">Deals</a><a class="btn secondary" href="{{url_for(\'land_deals\')}}">Land Deals</a>',
)
LIST_TEMPLATE = LIST_TEMPLATE.replace(
    '<a class="btn secondary" href="{{url_for(\'land_deals\')}}">Land Deals</a>',
    '<a class="btn secondary" href="{{url_for(\'land_deals\')}}">Land Deals</a><a class="btn secondary" href="{{url_for(\'text_campaign_list\')}}">SMS Campaigns</a>',
)

DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{lead.owner_name or lead.phone_display}} · CRM</title><style>{{style}}</style></head><body><main class="shell">
<header class="top"><div><a class="muted" href="/">← All leads</a><h1 style="margin-top:8px">{{lead.owner_name or lead.phone_display}}</h1><div class="sub"><a href="tel:{{lead.phone_normalized}}">{{lead.phone_display}}</a> · {{lead.call_count}} calls ({{lead.incoming_count}} incoming, {{lead.outgoing_count}} outgoing, {{lead.missed_count}} missed) · Priority {{lead.follow_up_score}}{% if lead.reference_number %} · Ref {{lead.reference_number}}{% endif %}</div></div><div class="top-actions"><a class="btn secondary" href="{{url_for('inbox_conversation',lead_id=lead.id)}}">Unified timeline</a><span class="pill {{lead.status}}">{{lead.status}}</span></div></header>
{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<div class="grid"><section class="stack">
<div class="panel"><h2>Lead intelligence</h2><h3>Complete story</h3><div class="summary-text">{{lead.summary or 'No analysis yet.'}}</div><div class="signal-grid"><div class="signal"><b>Distress signal</b>{{lead.distress_signal or 'None identified'}}</div><div class="signal"><b>Motivation to sell</b>{{lead.seller_motivation or 'None identified'}}</div><div class="signal"><b>Price posture</b>{{lead.price_expectation or 'Not established'}}</div></div><h3>Recommended follow-up</h3><div class="summary-text">{{lead.follow_up_plan or 'Review call history and choose a next action.'}}</div><div class="muted" style="margin-top:10px">{{lead.reasoning}}</div></div>
<div class="panel"><h2>Matched offer details</h2>{% if offer_error %}<div class="status-note">{{offer_error}}</div>{% elif offers %}{% for offer in offers %}<article class="offer"><div class="offer-head"><div><span class="pill">{{offer.reference}}</span><h3 style="margin-top:8px">{{offer.owner or 'Owner not listed'}}</h3><div class="muted">{{offer.property_address or 'Property address not listed'}}</div></div><div class="offer-price">{{offer.offer_price or 'No price'}}</div></div><div class="facts"><div class="fact"><b>Acreage</b>{{offer.acreage or '-'}}</div><div class="fact"><b>APN</b>{{offer.apn or '-'}}</div><div class="fact"><b>County / State</b>{{offer.county or '-'}}{% if offer.state %}, {{offer.state}}{% endif %}</div><div class="fact"><b>Mailing address</b>{{offer.mailing_address or '-'}}</div><div class="fact"><b>Offer date</b>{{offer.offer_date or '-'}}</div><div class="fact"><b>Closing date</b>{{offer.closing_date or '-'}}</div></div>{% if offer.legal_description %}<div style="margin-top:12px"><b>Legal description</b><div class="muted">{{offer.legal_description}}</div></div>{% endif %}<div class="actions">{% if offer.parcel_link %}<a class="btn secondary" target="_blank" rel="noopener" href="{{offer.parcel_link}}">Open parcel record</a>{% endif %}{% if offer.map_link %}<a class="btn secondary" target="_blank" rel="noopener" href="{{offer.map_link}}">Open map</a>{% endif %}</div></article>{% endfor %}{% elif lead.reference_number %}<div class="status-note">No offer record matches {{lead.reference_number}}. You can correct the reference in Manage lead.</div>{% else %}<div class="muted">Enter a reference number in Manage lead to find its offer record.</div>{% endif %}</div>
<div class="panel"><h2>Complete call timeline</h2>{% for call in calls|reverse %}<details class="call"><summary>{{call.occurred_at or call.time_description or 'Unknown time'}} · {% if call.direction == 'outgoing' %}We called the lead{% elif call.direction == 'incoming' %}Lead called us{% elif call.direction == 'missed' %}Missed call from lead{% else %}{{call.direction|title}}{% endif %}</summary><div class="muted">Lead reference: {{lead.reference_number or 'Not found'}}</div>{% if call.attachment_path %}<audio controls preload="none" src="{{url_for('call_audio',call_id=call.id)}}">Your browser cannot play this recording.</audio>{% endif %}<h3 style="margin-top:12px">Full transcript</h3><div class="transcript">{{call.transcript or 'No transcript available.'}}</div></details>{% else %}<div class="empty">No calls stored for this lead.</div>{% endfor %}</div>
{% if communications %}<div class="panel"><h2>CRM communication history</h2>{% for item in communications %}<div class="comm"><b>{{item.kind|upper}} · {{item.status|title}}</b><div class="muted">{{item.created_at}} · {{item.from_number}} → {{item.to_number}}</div>{% if item.callback_number %}<div class="muted">Callback phone: {{item.callback_number}}</div>{% endif %}{% if item.message %}<div class="comm-message">{{item.message}}</div>{% endif %}</div>{% endfor %}</div>{% endif %}
</section><aside class="stack">
<div class="panel"><div class="draft-head"><h2>Suggested texts</h2><form method="post" action="{{url_for('regenerate_drafts',lead_id=lead.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Regenerate</button></form></div><div class="muted">Based on this lead, deal style, offer data, and prior conversations. Review before sending.</div>{% if draft_error %}<div class="status-note error">{{draft_error}}</div>{% endif %}{% for draft in drafts %}<div class="draft"><b>{{draft.angle}}</b><div class="draft-message" id="draft-{{draft.id}}">{{draft.message}}</div><button class="btn secondary" type="button" onclick="useDraft({{draft.id}})">Use this draft</button></div>{% else %}<div class="muted" style="margin-top:10px">No suggestions are available yet.</div>{% endfor %}</div>
<div class="panel"><h2>Call or text with Vumber</h2><div><b>{{lead.phone_display}}</b></div>{% if vumber_error %}<div class="status-note error">{{vumber_error}}</div>{% endif %}{% if vumber_status.connected and vumber_config %}<div class="status-note good">Connected as {{vumber_status.username}}</div><form method="post" action="{{url_for('send_vumber_sms',lead_id=lead.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">From your Vumber<select name="virtual_number" required>{% for number in vumber_config.sms_numbers %}<option value="{{number.value}}">{{number.label}}</option>{% endfor %}</select></label><label class="field">Text message<textarea id="vumber-message" name="message" maxlength="1600" required placeholder="Write a message to {{lead.owner_name or lead.phone_display}}"></textarea></label><label class="check"><input type="checkbox" name="confirmed" value="yes" required><span>Send this text now to {{lead.phone_display}}.</span></label><button class="btn" {% if not vumber_config.sms_numbers %}disabled{% endif %}>Send text now</button></form><hr style="border:0;border-top:1px solid var(--line);margin:18px 0"><form method="post" action="{{url_for('make_vumber_call',lead_id=lead.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Call from<select name="virtual_number" required>{% for number in vumber_config.call_numbers %}<option value="{{number.value}}">{{number.label}}</option>{% endfor %}</select></label><label class="field">Ring me at<select name="callback_number" required>{% for number in vumber_config.callback_numbers %}<option value="{{number.value}}">{{number.label}}</option>{% endfor %}</select></label><label class="check"><input type="checkbox" name="confirmed" value="yes" required><span>Start the call now. Vumber will ring me first, then connect {{lead.phone_display}}.</span></label><button class="btn" {% if not vumber_config.call_numbers or not vumber_config.callback_numbers %}disabled{% endif %}>Place call now</button></form><div class="actions"><a class="btn secondary" href="{{url_for('vumber_settings')}}">Connection settings</a></div>{% else %}<div class="status-note">Connect this CRM to Vumber to text or call directly.</div><div class="actions"><a class="btn" href="{{url_for('vumber_settings')}}">Connect Vumber</a><button class="btn secondary" type="button" onclick="openVumber('https://app.vumber.com/calls/all')">Open Vumber</button><a class="btn secondary" href="tel:{{lead.phone_normalized}}">Call from this device</a></div>{% endif %}</div>
{% if current_user.role in ('owner','manager','agent') %}<form class="panel" method="post" action="{{url_for('update_lead',lead_id=lead.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Manage lead</h2><label class="field">Name<input name="owner_name" value="{{lead.owner_name}}"></label><label class="field">Reference number<input name="reference_number" value="{{lead.reference_number}}" placeholder="Example: DEMO-2612-1"><span class="muted">Use commas for multiple properties. Saving immediately checks the offers database.</span></label><label class="field">Deal style<input name="deal_style" value="{{lead.deal_style}}" placeholder="Inherited land, tax delinquent, cold lead..."></label><label class="field">Lead source<input name="lead_source" value="{{lead.lead_source}}" placeholder="Mailer, referral, website..."></label><label class="field">Stage<select name="status">{% for s in stages %}<option value="{{s}}" {% if lead.status==s %}selected{% endif %}>{{s|title}}</option>{% endfor %}</select></label><label class="field">Next follow-up<input type="date" name="next_follow_up" value="{{lead.next_follow_up or ''}}"></label><label class="field">Notes<textarea name="notes">{{lead.notes}}</textarea></label><button class="btn">Save changes</button></form>{% endif %}
<div class="panel"><h2>Follow-up tasks</h2>{% for task in tasks %}<div class="task">{% if current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('complete_task',task_id=task.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary" title="Mark done">✓</button></form>{% endif %}<div>{{task.title}}{% if task.due_at %}<div class="muted">Due {{task.due_at}}</div>{% endif %}</div></div>{% else %}<div class="muted">No open tasks.</div>{% endfor %}</div>
</aside></div></main><script>function openVumber(url){if(navigator.clipboard){navigator.clipboard.writeText({{lead.phone_normalized|tojson}});}window.open(url,'_blank','noopener');}function useDraft(id){const text=document.getElementById('draft-'+id).innerText;const box=document.getElementById('vumber-message');if(box){box.value=text;box.focus();box.scrollIntoView({behavior:'smooth',block:'center'});}else if(navigator.clipboard){navigator.clipboard.writeText(text);}}</script></body></html>
"""

NEW_LEAD_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Add lead · CRM</title><style>{{style}}</style></head><body><main class="shell" style="max-width:820px"><header class="top"><div><a class="muted" href="/">← All leads</a><h1 style="margin-top:8px">Add a lead</h1><div class="sub">Create a CRM record and its first suggested follow-up texts</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="panel wide-form" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><div class="two-col"><label class="field">Lead name<input name="owner_name" required autofocus></label><label class="field">Phone number<input name="phone" type="tel" required placeholder="(314) 555-0123"></label></div><div class="two-col"><label class="field">Reference number<input name="reference_number" placeholder="Example: DEMO-2612-1"></label><label class="field">Stage<select name="status">{% for stage in stages %}<option value="{{stage}}">{{stage|title}}</option>{% endfor %}</select></label></div><div class="two-col"><label class="field">Deal style<input name="deal_style" list="deal-styles" placeholder="Choose or describe"><datalist id="deal-styles"><option value="Mailer response"><option value="Inherited property"><option value="Tax delinquent"><option value="Estate / probate"><option value="Absentee owner"><option value="Price-shopping lead"><option value="Warm referral"></datalist></label><label class="field">Lead source<input name="lead_source" placeholder="Mailer, referral, website..."></label></div><label class="field">What do we know about this lead?<textarea name="notes" placeholder="Property details, seller situation, asking price, motivation, last conversation, and anything the draft should know."></textarea></label><button class="btn">Add lead and create suggestions</button></form></main></body></html>
"""

VUMBER_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Vumber connection · CRM</title><style>{{style}}</style></head><body><main class="shell" style="max-width:760px">
<header class="top"><div><a class="muted" href="/">← All leads</a><h1 style="margin-top:8px">Vumber connection</h1><div class="sub">Secure connection for calls and texts inside the CRM</div></div></header>
{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
{% if status.connected %}<section class="panel"><h2>Connected</h2><div class="status-note good">Signed in as {{status.username}} · account {{status.account_no}}</div><p>The saved access tokens are encrypted for this CRM host. Your Vumber password is not stored.</p><form method="post" action="{{url_for('vumber_disconnect')}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Disconnect Vumber</button></form></section>{% else %}<form class="panel" method="post"><h2>Sign in to Vumber</h2><p class="muted">Enter your Vumber login here. The CRM sends it directly to Vumber once and stores only encrypted session tokens.</p><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Email or username<input type="text" name="username" autocomplete="username" required></label><label class="field">Password<input type="password" name="password" autocomplete="current-password" required></label><button class="btn">Connect Vumber</button></form>{% endif %}
</main></body></html>
"""


def create_app(
    database_path: Path | str = Path("data/leads.db"),
    offers_path: Path | str = Path("all_fop_offers.db"),
    vumber: VumberClient | None = None,
    auth_enabled: bool = True,
) -> Flask:
    app = Flask(__name__)
    if os.environ.get("RENDER") or os.environ.get("CRM_PROXY_HEADERS", "").lower() in {"1", "true", "yes"}:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db = CRMDatabase(database_path)
    app.secret_key = load_or_create_secret(db.path.parent / "flask_secret.bin")
    app.config.update(
        SESSION_COOKIE_NAME="fulleroak_crm_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CRM_SECURE_COOKIES", "").lower() in {"1", "true", "yes"},
        MAX_CONTENT_LENGTH=21 * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
    )

    @app.after_request
    def prevent_private_page_caching(response):
        """Keep authenticated pages and CSRF-bearing forms out of browser caches."""
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    offers_db = OfferDatabase(offers_path, db.path.parent / "offer_reference_index.db")
    vumber = vumber or VumberClient(VumberSessionStore(db.path.parent / "vumber_session.bin"))
    limiter = LoginRateLimiter()

    def rows(sql: str, params=()):
        with db.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def require_csrf() -> None:
        if not auth_enabled:
            return
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            abort(400)

    @app.errorhandler(400)
    def recover_stale_login(error):
        if request.endpoint == "login" and request.method == "POST":
            next_path = request.args.get("next", "")
            session.clear()
            flash("Your sign-in page expired. Please sign in again.", "error")
            return redirect(url_for("login", next=next_path))
        return error

    app.jinja_env.globals["csrf_token"] = csrf_token

    def organization_id() -> int:
        return int(g.organization_id)

    def audit(action: str, entity_type: str = "", entity_id: object = "", details=None) -> None:
        occurred_at = utc_now()
        with db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO audit_events(
                    organization_id,user_id,action,entity_type,entity_id,
                    details_json,ip_address,created_at
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    organization_id(), g.user.get("id"), action, entity_type,
                    str(entity_id or ""), json.dumps(details or {}, sort_keys=True)[:4000],
                    request.remote_addr or "", occurred_at,
                ),
            )
            lead_id = contact_id = opportunity_id = None
            numeric_id = int(entity_id) if str(entity_id or "").isdigit() else None
            if numeric_id and entity_type == "lead":
                lead_id = numeric_id
                linked = connection.execute(
                    """SELECT c.id contact_id,o.id opportunity_id FROM contacts c
                       LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id
                       WHERE c.organization_id=? AND c.lead_id=?""",
                    (organization_id(), lead_id),
                ).fetchone()
                if linked:
                    contact_id, opportunity_id = linked["contact_id"], linked["opportunity_id"]
            elif numeric_id and entity_type == "opportunity":
                linked = connection.execute(
                    "SELECT lead_id,contact_id,id opportunity_id FROM opportunities WHERE id=? AND organization_id=?",
                    (numeric_id, organization_id()),
                ).fetchone()
                if linked:
                    lead_id, contact_id, opportunity_id = linked["lead_id"], linked["contact_id"], linked["opportunity_id"]
            elif numeric_id and entity_type == "contact":
                linked = connection.execute(
                    "SELECT lead_id,id contact_id FROM contacts WHERE id=? AND organization_id=?",
                    (numeric_id, organization_id()),
                ).fetchone()
                if linked:
                    lead_id, contact_id = linked["lead_id"], linked["contact_id"]
            connection.execute(
                """INSERT OR IGNORE INTO activity_events(
                    organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                    occurred_at,title,body,source_table,source_id,metadata_json,created_at
                ) VALUES (?,?,?,?,?,'system','internal',?,?,?,?,?,?,?)""",
                (
                    organization_id(), lead_id, contact_id, opportunity_id, "audit",
                    occurred_at, f"Audit: {action.replace('.', ' ')}",
                    json.dumps(details or {}, sort_keys=True)[:4000], "audit_events", cursor.lastrowid,
                    json.dumps({"action": action, "entity_type": entity_type, "entity_id": str(entity_id or "")}, sort_keys=True),
                    occurred_at,
                ),
            )

    def roles_required(*allowed_roles):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if g.user["role"] not in allowed_roles:
                    abort(403)
                return view(*args, **kwargs)
            return wrapped
        return decorator

    @app.before_request
    def load_current_user():
        if not auth_enabled:
            g.user = {"id": None, "organization_id": 1, "display_name": "Local owner", "role": "owner"}
            g.organization_id = 1
            return None
        # Let Flask return a normal 404 for unknown assets such as /favicon.ico.
        # Clearing the session here invalidates a CSRF token that may have just
        # been rendered into the login form by a parallel browser request.
        if request.endpoint is None:
            return None
        user_count = rows("SELECT COUNT(*) count FROM users")[0]["count"]
        if not user_count:
            if request.endpoint != "setup":
                return redirect(url_for("setup"))
            return None
        if request.endpoint in {"login", "static", "esign_webhook", "public_lead_form", "pwa_manifest", "pwa_icon", "service_worker", "offline_page", "api_list_leads", "api_create_lead", "lead_webhook"}:
            return None
        user_id = session.get("user_id")
        user = rows("SELECT * FROM users WHERE id=? AND active=1", (user_id,)) if user_id else []
        if not user:
            session.clear()
            return redirect(url_for("login", next=request.path))
        g.user = user[0]
        g.organization_id = int(user[0]["organization_id"])

    @app.context_processor
    def inject_identity():
        return {"current_user": getattr(g, "user", None), "admin_roles": ADMIN_ROLES}

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if rows("SELECT COUNT(*) count FROM users")[0]["count"]:
            return redirect(url_for("login"))
        if request.method == "POST":
            require_csrf()
            email = request.form.get("email", "").strip().lower()
            name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            if not name or "@" not in email or len(password) < 12:
                flash("Enter your name, a valid email, and a password of at least 12 characters.", "error")
                return render_template_string(AUTH_TEMPLATE, style=BASE_STYLE, title="Create the owner account", subtitle="One-time setup for Fuller Oak", button="Create owner", setup=True), 400
            with db.connect() as connection:
                cursor = connection.execute(
                    """INSERT INTO users(organization_id,email,display_name,password_hash,role,created_at)
                       VALUES (1,?,?,?,?,?)""",
                    (email, name, generate_password_hash(password), "owner", utc_now()),
                )
                user_id = cursor.lastrowid
            session.clear()
            session.update(user_id=user_id, organization_id=1, csrf_token=secrets.token_urlsafe(32))
            g.user = {"id": user_id, "organization_id": 1, "display_name": name, "role": "owner"}
            g.organization_id = 1
            audit("auth.setup", "user", user_id, {"email": email})
            return redirect(url_for("index"))
        return render_template_string(AUTH_TEMPLATE, style=BASE_STYLE, title="Create the owner account", subtitle="One-time setup for Fuller Oak", button="Create owner", setup=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth_enabled:
            return redirect(url_for("index"))
        if request.method == "POST":
            require_csrf()
            email = request.form.get("email", "").strip().lower()
            key = f"{request.remote_addr or ''}:{email}"
            if not limiter.allowed(key):
                flash("Too many sign-in attempts. Try again in 15 minutes.", "error")
                return render_template_string(AUTH_TEMPLATE, style=BASE_STYLE, title="Sign in", subtitle="Vumber Lead CRM", button="Sign in", setup=False), 429
            matches = rows("SELECT * FROM users WHERE email=? AND active=1 ORDER BY id LIMIT 1", (email,))
            if not matches or not check_password_hash(matches[0]["password_hash"], request.form.get("password", "")):
                limiter.fail(key)
                flash("Invalid email or password.", "error")
                return render_template_string(AUTH_TEMPLATE, style=BASE_STYLE, title="Sign in", subtitle="Vumber Lead CRM", button="Sign in", setup=False), 401
            user = matches[0]
            limiter.reset(key)
            session.clear()
            session.update(user_id=user["id"], organization_id=user["organization_id"], csrf_token=secrets.token_urlsafe(32))
            g.user = user
            g.organization_id = int(user["organization_id"])
            with db.connect() as connection:
                connection.execute("UPDATE users SET last_login_at=? WHERE id=?", (utc_now(), user["id"]))
            audit("auth.login", "user", user["id"])
            return redirect(url_for("index"))
        return render_template_string(AUTH_TEMPLATE, style=BASE_STYLE, title="Sign in", subtitle="Vumber Lead CRM", button="Sign in", setup=False)

    @app.post("/logout")
    def logout():
        require_csrf()
        audit("auth.logout", "user", g.user["id"])
        session.clear()
        return redirect(url_for("login"))

    def normalized_phone(value: str) -> tuple[str, str]:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError("Enter a valid 10-digit US phone number.")
        return digits, f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    def ensure_drafts(lead, calls, communications, offers, force=False):
        fingerprint = draft_context_hash(lead, calls, communications, offers)
        existing = rows(
            "SELECT * FROM message_drafts WHERE organization_id=? AND lead_id=? AND context_hash=? ORDER BY id",
            (organization_id(), lead["id"], fingerprint),
        )
        if existing and not force:
            return existing
        generated, provider = generate_drafts(lead, calls, communications, offers)
        with db.connect() as connection:
            connection.execute("DELETE FROM message_drafts WHERE organization_id=? AND lead_id=?", (organization_id(), lead["id"]))
            connection.executemany(
                """INSERT INTO message_drafts(
                    organization_id,lead_id,angle,message,provider,context_hash,created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                [
                    (organization_id(), lead["id"], item["angle"], item["message"], provider, fingerprint, utc_now())
                    for item in generated
                ],
            )
        return rows("SELECT * FROM message_drafts WHERE organization_id=? AND lead_id=? ORDER BY id", (organization_id(), lead["id"]))

    def sync_offer_properties(lead: dict, offers: list[dict]) -> None:
        if not offers:
            return
        contact = rows(
            "SELECT id FROM contacts WHERE organization_id=? AND lead_id=?",
            (organization_id(), lead["id"]),
        )
        if not contact:
            contact_id = db.sync_contact_for_lead(lead["id"])
        else:
            contact_id = contact[0]["id"]
        if not contact_id:
            return
        with db.connect() as connection:
            for offer in offers:
                connection.execute(
                    """INSERT INTO properties(
                        organization_id,lead_id,reference_number,owner_name,property_address,
                        county,state,acreage,apn,offer_price,source,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,'offers_db',?,?)
                    ON CONFLICT(organization_id,reference_number) WHERE reference_number != ''
                    DO UPDATE SET lead_id=excluded.lead_id,owner_name=excluded.owner_name,
                        property_address=excluded.property_address,county=excluded.county,
                        state=excluded.state,acreage=excluded.acreage,apn=excluded.apn,
                        offer_price=excluded.offer_price,source='offers_db',updated_at=excluded.updated_at""",
                    (
                        organization_id(), lead["id"], offer.get("reference", ""),
                        offer.get("owner", ""), offer.get("property_address", ""),
                        offer.get("county", ""), offer.get("state", ""),
                        offer.get("acreage", ""), offer.get("apn", ""),
                        offer.get("offer_price", ""), utc_now(), utc_now(),
                    ),
                )
                property_row = connection.execute(
                    "SELECT id FROM properties WHERE organization_id=? AND reference_number=?",
                    (organization_id(), offer.get("reference", "")),
                ).fetchone()
                if property_row:
                    connection.execute(
                        "INSERT OR IGNORE INTO contact_properties VALUES (?,?,?,'owner')",
                        (organization_id(), contact_id, property_row["id"]),
                    )
                    connection.execute(
                        """UPDATE opportunities SET property_id=?,updated_at=?,version=version+1
                           WHERE organization_id=? AND lead_id=? AND property_id IS NULL""",
                        (property_row["id"], utc_now(), organization_id(), lead["id"]),
                    )

    @app.route("/lead/new", methods=["GET", "POST"])
    @roles_required(*WRITE_ROLES)
    def new_lead():
        if request.method == "POST":
            require_csrf()
            try:
                phone_normalized, phone_display = normalized_phone(request.form.get("phone", ""))
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template_string(NEW_LEAD_TEMPLATE, style=BASE_STYLE, stages=STAGES), 400
            existing = rows("SELECT id FROM leads WHERE organization_id=? AND phone_normalized=?", (organization_id(), phone_normalized))
            if existing:
                flash("That phone number is already in the CRM. I opened the existing lead.", "good")
                return redirect(url_for("lead_detail", lead_id=existing[0]["id"]))
            lead_id = db.ensure_lead(phone_normalized, phone_display, organization_id())
            status = request.form.get("status", "new")
            if status not in STAGES:
                status = "new"
            references = ", ".join(parse_references(request.form.get("reference_number", "")))
            with db.connect() as connection:
                connection.execute(
                    """UPDATE leads SET owner_name=?, reference_number=?, status=?, deal_style=?,
                       lead_source=?, notes=?, updated_at=? WHERE id=? AND organization_id=?""",
                    (
                        request.form.get("owner_name", "").strip(), references, status,
                        request.form.get("deal_style", "").strip(),
                        request.form.get("lead_source", "").strip(),
                        request.form.get("notes", "").strip(), utc_now(), lead_id, organization_id(),
                    ),
                )
            db.sync_contact_for_lead(lead_id)
            audit("lead.create", "lead", lead_id, {"phone": phone_display})
            flash("Lead added. Suggested messages were created from the details you entered.", "good")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        return render_template_string(NEW_LEAD_TEMPLATE, style=BASE_STYLE, stages=STAGES)

    @app.get("/")
    def index():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip()
        score = request.args.get("score", "").strip()
        recency = request.args.get("recency", "").strip()
        sort = request.args.get("sort", "recent").strip()
        if sort not in SORTS:
            sort = "recent"
        where, params = ["l.organization_id = ?"], [organization_id()]
        if q:
            where.append("(l.owner_name LIKE ? OR l.phone_display LIKE ? OR l.reference_number LIKE ? OR l.summary LIKE ? OR l.follow_up_plan LIKE ? OR l.distress_signal LIKE ? OR l.seller_motivation LIKE ? OR l.price_expectation LIKE ?)")
            params.extend([f"%{q}%"] * 8)
        if status in STAGES:
            where.append("l.status = ?")
            params.append(status)
        if score in {"1", "2", "3", "4", "5"}:
            where.append("l.follow_up_score = ?")
            params.append(int(score))
        if recency in RECENCY:
            where.append("datetime(l.latest_activity) >= datetime('now', ?)")
            params.append(RECENCY[recency])
        clause = "WHERE " + " AND ".join(where) if where else ""
        leads = rows(f"""SELECT l.*, COUNT(DISTINCT c.id) call_count
            FROM leads l LEFT JOIN calls c ON c.lead_id=l.id AND c.organization_id=l.organization_id {clause}
            GROUP BY l.id ORDER BY {SORTS[sort]}""", params)
        stats = rows("""SELECT COUNT(*) total,
            SUM(CASE WHEN follow_up_score >= 4 AND status NOT IN ('won','lost') THEN 1 ELSE 0 END) hot
            FROM leads WHERE organization_id=?""", (organization_id(),))[0]
        stats.update(rows("SELECT COUNT(*) calls FROM calls WHERE organization_id=?", (organization_id(),))[0])
        stats.update(rows("SELECT COUNT(*) open_tasks FROM tasks WHERE organization_id=? AND status='open'", (organization_id(),))[0])
        return render_template_string(
            LIST_TEMPLATE, style=BASE_STYLE, leads=leads, stats=stats, stages=STAGES,
            q=q, status=status, score=score, recency=recency, sort=sort,
        )

    @app.get("/call/<int:call_id>/audio")
    def call_audio(call_id: int):
        call_rows = rows("SELECT attachment_path FROM calls WHERE id=? AND organization_id=?", (call_id, organization_id()))
        if not call_rows or not call_rows[0]["attachment_path"]:
            abort(404)
        project_root = db.path.resolve().parent.parent
        audio_path = Path(call_rows[0]["attachment_path"])
        if not audio_path.is_absolute():
            audio_path = project_root / audio_path
        audio_path = audio_path.resolve()
        try:
            audio_path.relative_to(project_root)
        except ValueError:
            abort(403)
        if not audio_path.is_file():
            abort(404)
        return send_file(audio_path, conditional=True, mimetype="audio/mpeg")

    @app.get("/lead/<int:lead_id>")
    def lead_detail(lead_id: int):
        lead_rows = rows("""SELECT l.*, COUNT(c.id) call_count,
            SUM(CASE WHEN c.direction='incoming' THEN 1 ELSE 0 END) incoming_count,
            SUM(CASE WHEN c.direction='outgoing' THEN 1 ELSE 0 END) outgoing_count,
            SUM(CASE WHEN c.direction='missed' THEN 1 ELSE 0 END) missed_count FROM leads l
            LEFT JOIN calls c ON c.lead_id=l.id AND c.organization_id=l.organization_id
            WHERE l.id=? AND l.organization_id=? GROUP BY l.id""", (lead_id, organization_id()))
        if not lead_rows:
            abort(404)
        calls = rows("SELECT * FROM calls WHERE organization_id=? AND lead_id=? ORDER BY COALESCE(occurred_at, created_at)", (organization_id(), lead_id))
        tasks = rows("SELECT * FROM tasks WHERE organization_id=? AND lead_id=? AND status='open' ORDER BY due_at IS NULL, due_at, id DESC", (organization_id(), lead_id))
        communications = rows("SELECT * FROM communications WHERE organization_id=? AND lead_id=? ORDER BY id DESC", (organization_id(), lead_id))
        vumber_numbers = rows("""SELECT DISTINCT vumber_number, vumber_label FROM calls
            WHERE organization_id=? AND lead_id=? AND vumber_number != '' ORDER BY vumber_number""", (organization_id(), lead_id))
        matched_offers, offer_error = [], ""
        if lead_rows[0]["reference_number"] and not offers_db.available:
            offer_error = f"Offer database not found: {offers_db.source_path}"
        else:
            try:
                matched_offers = offers_db.lookup(lead_rows[0]["reference_number"])
                if not matched_offers:
                    # Fuzzy fallback: transcription often garbles the letter
                    # prefix, but the digit groups stay unique in the offers DB.
                    matched_offers = offers_db.lookup_by_digits(
                        extract_reference_digit_groups(lead_rows[0]["reference_number"])
                    )
                if not matched_offers and lead_rows[0]["owner_name"]:
                    matched_offers = offers_db.lookup_by_owner(lead_rows[0]["owner_name"])
                sync_offer_properties(lead_rows[0], matched_offers)
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                offer_error = f"Offer database lookup is unavailable: {exc}"
        drafts, draft_error = [], ""
        try:
            drafts = ensure_drafts(
                lead_rows[0], calls, communications, matched_offers,
            )
        except Exception as exc:
            draft_error = f"Suggestions could not be generated: {exc}"
        vumber_status, vumber_config, vumber_error = {"connected": False}, None, ""
        try:
            vumber_status = vumber.status()
            if vumber_status["connected"]:
                vumber_config = vumber.configuration()
        except VumberError as exc:
            vumber_error = str(exc)
        return render_template_string(
            DETAIL_TEMPLATE, style=BASE_STYLE, lead=lead_rows[0], calls=calls,
            tasks=tasks, stages=STAGES, offers=matched_offers,
            offer_error=offer_error, vumber_numbers=vumber_numbers,
            communications=communications, vumber_status=vumber_status,
            vumber_config=vumber_config, vumber_error=vumber_error,
            drafts=drafts, draft_error=draft_error,
        )

    @app.post("/lead/<int:lead_id>/drafts/regenerate")
    @roles_required(*WRITE_ROLES)
    def regenerate_drafts(lead_id: int):
        require_csrf()
        lead = rows("SELECT * FROM leads WHERE id=? AND organization_id=?", (lead_id, organization_id()))
        if not lead:
            abort(404)
        calls = rows(
            "SELECT * FROM calls WHERE organization_id=? AND lead_id=? ORDER BY COALESCE(occurred_at, created_at)",
            (organization_id(), lead_id),
        )
        communications = rows(
            "SELECT * FROM communications WHERE organization_id=? AND lead_id=? ORDER BY id DESC", (organization_id(), lead_id)
        )
        offers = []
        if lead[0]["reference_number"] and offers_db.available:
            offers = offers_db.lookup(lead[0]["reference_number"])
        try:
            ensure_drafts(lead[0], calls, communications, offers, force=True)
            audit("draft.regenerate", "lead", lead_id)
            flash("Suggested texts regenerated from the latest lead context.", "good")
        except Exception as exc:
            flash(f"Suggestions could not be regenerated: {exc}", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.route("/settings/vumber", methods=["GET", "POST"])
    @roles_required(*ADMIN_ROLES)
    def vumber_settings():
        if request.method == "POST":
            require_csrf()
            try:
                info = vumber.connect(
                    request.form.get("username", ""), request.form.get("password", ""),
                )
                audit("vumber.connect", details={"username": info.get("username", "")})
                flash(f"Vumber connected as {info['username']}.", "good")
                return redirect(url_for("vumber_settings"))
            except VumberError as exc:
                flash(str(exc), "error")
        try:
            status = vumber.status()
            if status.get('connected'):
                try:
                    vumber.configuration()
                except VumberError as exc:
                    status = {**status, 'connected': False}
                    flash(f'{exc} Sign in below to reconnect.', 'error')
        except VumberError as exc:
            status = {"connected": False}
            flash(str(exc), "error")
        return render_template_string(VUMBER_TEMPLATE, style=BASE_STYLE, status=status)

    @app.post("/settings/vumber/disconnect")
    @roles_required(*ADMIN_ROLES)
    def vumber_disconnect():
        require_csrf()
        vumber.disconnect()
        audit("vumber.disconnect")
        flash("Vumber disconnected. The saved session tokens were removed.", "good")
        return redirect(url_for("vumber_settings"))

    def lead_for_communication(lead_id: int) -> dict:
        lead = rows("SELECT * FROM leads WHERE id=? AND organization_id=?", (lead_id, organization_id()))
        if not lead:
            abort(404)
        return lead[0]

    @app.post("/lead/<int:lead_id>/vumber/text")
    @roles_required(*WRITE_ROLES)
    def send_vumber_sms(lead_id: int):
        require_csrf()
        lead = lead_for_communication(lead_id)
        if request.form.get("confirmed") != "yes":
            abort(400)
        if communication["is_suppressed"](lead_id, "sms"):
            flash("This lead is suppressed for SMS. Lift the suppression in Communication settings first.", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        message = request.form.get("message", "").strip()
        if not message or len(message) > 1600:
            flash("Enter a text message of 1–1600 characters.", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        from_number = request.form.get("virtual_number", "")
        try:
            allowed = {item["value"] for item in vumber.configuration(force=True)["sms_numbers"]}
            if from_number not in allowed:
                raise VumberError("Choose a Vumber line from the current list.")
            result = vumber.send_sms(lead["phone_normalized"], from_number, message)
            with db.connect() as connection:
                cursor = connection.execute("""INSERT INTO communications(
                    organization_id,lead_id,kind,from_number,to_number,message,status,response,created_at
                ) VALUES (?,?,'sms',?,?,?,'sent',?,?)""", (
                    organization_id(), lead_id, from_number, lead["phone_normalized"], message,
                    json.dumps(result)[:4000], utc_now(),
                ))
                context = connection.execute("""SELECT c.id contact_id,o.id opportunity_id FROM contacts c
                    LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id
                    WHERE c.organization_id=? AND c.lead_id=?""", (organization_id(), lead_id)).fetchone()
                connection.execute("""INSERT INTO activity_events(
                    organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                    occurred_at,title,body,source_table,source_id,metadata_json,created_at
                ) VALUES (?,?,?,?,?,'sms','outgoing',?,?,?,?,?,?,?)""", (
                    organization_id(), lead_id, context["contact_id"] if context else None,
                    context["opportunity_id"] if context else None, "sms", utc_now(), "Text sent",
                    message, "communications", cursor.lastrowid,
                    json.dumps({"from": from_number, "to": lead["phone_normalized"], "status": "sent"}), utc_now(),
                ))
                communication["record_delivery"](
                    connection, cursor.lastrowid, "sms", "sent",
                    provider_message_id=str(result.get("id", "")),
                    detail={"from": from_number, "to": lead["phone_normalized"]},
                )
            audit("sms.sent", "lead", lead_id, {"from": from_number, "to": lead["phone_normalized"], "characters": len(message)})
            flash(f"Text sent to {lead['phone_display']}.", "good")
        except VumberError as exc:
            flash(str(exc), "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/vumber/call")
    @roles_required(*WRITE_ROLES)
    def make_vumber_call(lead_id: int):
        require_csrf()
        lead = lead_for_communication(lead_id)
        if request.form.get("confirmed") != "yes":
            abort(400)
        if communication["is_suppressed"](lead_id, "call"):
            flash("This lead is suppressed for calls. Lift the suppression in Communication settings first.", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        from_number = request.form.get("virtual_number", "")
        callback_number = request.form.get("callback_number", "")
        try:
            config = vumber.configuration(force=True)
            if from_number not in {item["value"] for item in config["call_numbers"]}:
                raise VumberError("Choose a Vumber line from the current list.")
            if callback_number not in {item["value"] for item in config["callback_numbers"]}:
                raise VumberError("Choose a callback phone from the current list.")
            result = vumber.make_call(lead["phone_normalized"], from_number, callback_number)
            with db.connect() as connection:
                cursor = connection.execute("""INSERT INTO communications(
                    organization_id,lead_id,kind,from_number,to_number,callback_number,status,response,created_at
                ) VALUES (?,?,'call',?,?,?,'started',?,?)""", (
                    organization_id(), lead_id, from_number, lead["phone_normalized"], callback_number,
                    json.dumps(result)[:4000], utc_now(),
                ))
                context = connection.execute("""SELECT c.id contact_id,o.id opportunity_id FROM contacts c
                    LEFT JOIN opportunities o ON o.organization_id=c.organization_id AND o.lead_id=c.lead_id
                    WHERE c.organization_id=? AND c.lead_id=?""", (organization_id(), lead_id)).fetchone()
                connection.execute("""INSERT INTO activity_events(
                    organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                    occurred_at,title,body,source_table,source_id,metadata_json,created_at
                ) VALUES (?,?,?,?,?,'call','outgoing',?,?,?,?,?,?,?)""", (
                    organization_id(), lead_id, context["contact_id"] if context else None,
                    context["opportunity_id"] if context else None, "crm_call", utc_now(), "CRM call started",
                    "", "communications", cursor.lastrowid,
                    json.dumps({"from": from_number, "to": lead["phone_normalized"], "callback": callback_number, "status": "started"}), utc_now(),
                ))
            audit("call.started", "lead", lead_id, {"from": from_number, "to": lead["phone_normalized"]})
            flash(f"Vumber is calling {callback_number}; it will then connect {lead['phone_display']}.", "good")
        except VumberError as exc:
            flash(str(exc), "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>")
    @roles_required(*WRITE_ROLES)
    def update_lead(lead_id: int):
        require_csrf()
        status = request.form.get("status", "new")
        if status not in STAGES:
            abort(400)
        references = ", ".join(parse_references(request.form.get("reference_number", "")))
        with db.connect() as connection:
            cursor = connection.execute(
                """UPDATE leads SET owner_name=?, reference_number=?, status=?, deal_style=?,
                   lead_source=?, next_follow_up=?, notes=?, updated_at=? WHERE id=? AND organization_id=?""",
                (request.form.get("owner_name", "").strip(), references, status,
                 request.form.get("deal_style", "").strip(),
                 request.form.get("lead_source", "").strip(),
                 request.form.get("next_follow_up") or None,
                 request.form.get("notes", "").strip(), utc_now(), lead_id, organization_id()),
            )
            if not cursor.rowcount:
                abort(404)
            opportunity = connection.execute(
                """SELECT o.id,o.stage_id FROM opportunities o
                   JOIN pipelines p ON p.id=o.pipeline_id AND p.is_default=1
                   WHERE o.organization_id=? AND o.lead_id=?""",
                (organization_id(), lead_id),
            ).fetchone()
            destination = connection.execute(
                """SELECT s.id FROM pipeline_stages s JOIN pipelines p ON p.id=s.pipeline_id
                   WHERE s.organization_id=? AND p.is_default=1 AND s.stage_key=? AND s.active=1""",
                (organization_id(), status),
            ).fetchone()
            if opportunity and destination and opportunity["stage_id"] != destination["id"]:
                connection.execute(
                    "UPDATE opportunities SET stage_id=?,version=version+1,updated_at=? WHERE id=?",
                    (destination["id"], utc_now(), opportunity["id"]),
                )
                occurred_at = utc_now()
                transition = connection.execute(
                    """INSERT INTO stage_transitions(
                        organization_id,opportunity_id,from_stage_id,to_stage_id,user_id,note,created_at
                    ) VALUES (?,?,?,?,?,'Changed from lead page',?)""",
                    (
                        organization_id(), opportunity["id"], opportunity["stage_id"],
                        destination["id"], g.user.get("id"), occurred_at,
                    ),
                )
                stage_names = connection.execute(
                    """SELECT
                        (SELECT name FROM pipeline_stages WHERE id=?) from_name,
                        (SELECT name FROM pipeline_stages WHERE id=?) to_name""",
                    (opportunity["stage_id"], destination["id"]),
                ).fetchone()
                context = connection.execute(
                    "SELECT contact_id FROM opportunities WHERE id=?",
                    (opportunity["id"],),
                ).fetchone()
                connection.execute(
                    """INSERT INTO activity_events(
                        organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                        occurred_at,title,body,source_table,source_id,metadata_json,created_at
                    ) VALUES (?,?,?,?,?,'system','internal',?,?,?,?,?,?,?)""",
                    (
                        organization_id(), lead_id, context["contact_id"] if context else None,
                        opportunity["id"], "stage_change", occurred_at,
                        f"Stage changed: {stage_names['from_name']} → {stage_names['to_name']}",
                        "Changed from lead page", "stage_transitions", transition.lastrowid,
                        "{}", occurred_at,
                    ),
                )
        db.sync_contact_for_lead(lead_id)
        audit("lead.update", "lead", lead_id, {"status": status, "reference_number": references})
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/task/<int:task_id>/done")
    @roles_required(*WRITE_ROLES)
    def complete_task(task_id: int):
        require_csrf()
        with db.connect() as connection:
            task = connection.execute("SELECT lead_id FROM tasks WHERE id=? AND organization_id=?", (task_id, organization_id())).fetchone()
            if not task:
                abort(404)
            connection.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=? AND organization_id=?", (utc_now(), task_id, organization_id()))
        audit("task.complete", "task", task_id, {"lead_id": task["lead_id"]})
        return redirect(url_for("lead_detail", lead_id=task["lead_id"]))

    @app.route("/settings/team", methods=["GET", "POST"])
    @roles_required(*ADMIN_ROLES)
    def team_settings():
        if request.method == "POST":
            require_csrf()
            email = request.form.get("email", "").strip().lower()
            name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "agent")
            if role not in ROLES or (role == "owner" and g.user["role"] != "owner"):
                abort(403)
            if not name or "@" not in email or len(password) < 12:
                flash("Enter a name, valid email, and password of at least 12 characters.", "error")
            else:
                try:
                    with db.connect() as connection:
                        cursor = connection.execute(
                            """INSERT INTO users(organization_id,email,display_name,password_hash,role,created_at)
                               VALUES (?,?,?,?,?,?)""",
                            (organization_id(), email, name, generate_password_hash(password), role, utc_now()),
                        )
                        new_user_id = cursor.lastrowid
                    audit("user.create", "user", new_user_id, {"email": email, "role": role})
                    flash("Team member created.", "good")
                    return redirect(url_for("team_settings"))
                except sqlite3.IntegrityError:
                    flash("That email already has an account in this organization.", "error")
        members = rows("SELECT * FROM users WHERE organization_id=? ORDER BY active DESC, display_name", (organization_id(),))
        return render_template_string(TEAM_TEMPLATE, style=BASE_STYLE, members=members, roles=ROLES, user=g.user)

    @app.post("/settings/team/<int:user_id>/toggle")
    @roles_required("owner")
    def toggle_user(user_id: int):
        require_csrf()
        if user_id == g.user["id"]:
            abort(400)
        target = rows("SELECT * FROM users WHERE id=? AND organization_id=?", (user_id, organization_id()))
        if not target:
            abort(404)
        new_active = 0 if target[0]["active"] else 1
        with db.connect() as connection:
            connection.execute("UPDATE users SET active=? WHERE id=? AND organization_id=?", (new_active, user_id, organization_id()))
        audit("user.enable" if new_active else "user.disable", "user", user_id)
        return redirect(url_for("team_settings"))

    @app.get("/settings/audit")
    @roles_required(*ADMIN_ROLES)
    def audit_log():
        events = rows("""SELECT a.*,u.display_name FROM audit_events a
            LEFT JOIN users u ON u.id=a.user_id
            WHERE a.organization_id=? ORDER BY a.id DESC LIMIT 200""", (organization_id(),))
        return render_template_string(AUDIT_TEMPLATE, style=BASE_STYLE, events=events)

    @app.get("/health")
    def health():
        sync = rows("SELECT * FROM sync_runs WHERE organization_id=? ORDER BY id DESC LIMIT 1", (organization_id(),))
        workflow_worker = rows("SELECT * FROM workflow_worker_state WHERE worker_name='default'")
        workflow_counts = rows("""SELECT
            (SELECT COUNT(*) FROM workflow_definitions WHERE organization_id=? AND enabled=1) enabled,
            (SELECT COUNT(*) FROM workflow_outbox WHERE organization_id=? AND status IN ('pending','processing','failed')) pending_events,
            (SELECT COUNT(*) FROM workflow_jobs WHERE organization_id=? AND status IN ('queued','waiting','processing','failed')) pending_jobs,
            (SELECT COUNT(*) FROM workflow_jobs WHERE organization_id=? AND status='dead') dead_jobs""",
            (organization_id(),) * 4)[0]
        counts = rows("""SELECT
            (SELECT COUNT(*) FROM leads WHERE organization_id=?) leads,
            (SELECT COUNT(*) FROM calls WHERE organization_id=?) calls,
            (SELECT COUNT(*) FROM processed_messages WHERE organization_id=? AND status='complete') messages""",
            (organization_id(), organization_id(), organization_id()))[0]
        return {
            "ok": True, "database": str(db.path.resolve()),
            "offers_database": str(offers_db.source_path.resolve()),
            "offers_available": offers_db.available, "vumber_direct_api": vumber.connected,
            "counts": counts, "last_sync": sync[0] if sync else None,
            "workflows": workflow_counts,
            "workflow_worker": workflow_worker[0] if workflow_worker else None,
        }

    register_phase2(
        app, db, BASE_STYLE, STAGES, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase3(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase4(app, db, BASE_STYLE, rows, organization_id)
    register_phase5(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit, STAGES,
    )
    register_phase6(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase7(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase8(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    communication = {}
    register_phase9(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    communication.update(app.extensions["communication"])
    register_phase10(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase11(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase12(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit,
    )
    register_phase13(
        app, db, BASE_STYLE, rows, organization_id, require_csrf,
        roles_required, audit, vumber,
    )
    register_text_campaigns(app, db, BASE_STYLE, organization_id, require_csrf, roles_required, audit, vumber)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Vumber Lead CRM")
    parser.add_argument("--database", type=Path, default=Path("data/leads.db"))
    parser.add_argument("--offers-database", type=Path, default=Path("all_fop_offers.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    # This entry point is strictly loopback-only by default, so local desktop use
    # does not require a browser login session. Programmatic/deployed callers can
    # still enable authentication through create_app(auth_enabled=True).
    create_app(args.database, args.offers_database, auth_enabled=False).run(
        host=args.host, port=args.port, debug=False,
    )


if __name__ == "__main__":
    main()
