"""Phase 2 contact, property, metadata, saved-view, and CSV import UI."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from flask import abort, flash, g, redirect, render_template_string, request, url_for

from crm_db import utc_now
from offer_db import parse_references


CONTACTS_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Contacts · CRM</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Contacts</h1><div class="sub">People, tags, properties, and reusable smart lists</div></div><div class="top-actions"><a class="btn" href="{{url_for('new_lead')}}">+ Add contact</a><a class="btn secondary" href="{{url_for('properties')}}">Properties</a><a class="btn secondary" href="{{url_for('imports')}}">Import CSV</a>{% if current_user.role in admin_roles %}<a class="btn secondary" href="{{url_for('fields_tags')}}">Fields & tags</a>{% endif %}</div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="toolbar" method="get"><input name="q" value="{{filters.q}}" placeholder="Search name, phone, email, reference..."><select name="status"><option value="">All stages</option>{% for stage in stages %}<option value="{{stage}}" {% if filters.status==stage %}selected{% endif %}>{{stage|title}}</option>{% endfor %}</select><select name="tag"><option value="">All tags</option>{% for tag in tags %}<option value="{{tag.id}}" {% if filters.tag==tag.id|string %}selected{% endif %}>{{tag.name}}</option>{% endfor %}</select><label class="check"><input type="checkbox" name="has_reference" value="1" {% if filters.has_reference %}checked{% endif %}>Has reference</label><button class="btn">Apply</button><a class="btn secondary" href="{{url_for('contacts')}}">Clear</a></form><div class="panel"><div class="draft-head"><h2>Saved smart lists</h2><form method="post" action="{{url_for('save_contact_filter')}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="q" value="{{filters.q}}"><input type="hidden" name="status" value="{{filters.status}}"><input type="hidden" name="tag" value="{{filters.tag}}"><input type="hidden" name="has_reference" value="{{'1' if filters.has_reference else ''}}"><input name="name" required placeholder="Name this view"><label class="check" style="display:inline-flex"><input type="checkbox" name="shared" value="1">Share with team</label><button class="btn secondary">Save current filters</button></form></div><div class="actions">{% for view in saved_filters %}<a class="pill" href="{{url_for('contacts',view=view.id)}}">{{view.name}}{{' · shared' if view.shared else ''}}</a>{% else %}<span class="muted">No saved views yet.</span>{% endfor %}</div></div><div class="table-wrap"><table><thead><tr><th>Contact</th><th>Phone</th><th>Email</th><th>Stage</th><th>Tags</th><th>Properties</th><th>Recent activity</th></tr></thead><tbody>{% for contact in contacts %}<tr><td><a class="leadlink" href="{{url_for('contact_detail',contact_id=contact.id)}}">{{contact.full_name or 'Unknown contact'}}</a></td><td>{{contact.primary_phone_display}}</td><td>{{contact.primary_email or '—'}}</td><td><span class="pill {{contact.status}}">{{contact.status}}</span></td><td>{{contact.tag_names or '—'}}</td><td>{{contact.property_count}}</td><td>{{contact.latest_activity or '—'}}</td></tr>{% else %}<tr><td colspan="7" class="empty">No contacts match this view.</td></tr>{% endfor %}</tbody></table></div></main></body></html>
"""

CONTACTS_TEMPLATE = CONTACTS_TEMPLATE.replace(
    "</main>",
    """<div class="actions">{% if page > 1 %}<a class="btn secondary" href="{{url_for('contacts',page=page-1,q=filters.q,status=filters.status,tag=filters.tag,has_reference='1' if filters.has_reference else '')}}">Previous</a>{% endif %}<span class="muted">Page {{page}}</span>{% if has_more %}<a class="btn secondary" href="{{url_for('contacts',page=page+1,q=filters.q,status=filters.status,tag=filters.tag,has_reference='1' if filters.has_reference else '')}}">Next</a>{% endif %}</div></main>""",
)

CONTACT_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{contact.full_name or contact.primary_phone_display}} · Contact</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('contacts')}}">← Contacts</a><h1 style="margin-top:8px">{{contact.full_name or contact.primary_phone_display}}</h1><div class="sub">{{contact.primary_phone_display}}{% if contact.lead_id %} · <a href="{{url_for('lead_detail',lead_id=contact.lead_id)}}">Open calls and lead intelligence</a>{% endif %}</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Contact details</h2><label class="field">Name<input name="full_name" value="{{contact.full_name}}"></label><label class="field">Phone<input value="{{contact.primary_phone_display}}" disabled></label><label class="field">Email<input type="email" name="primary_email" value="{{contact.primary_email}}"></label><label class="field">Mailing address<textarea name="mailing_address">{{contact.mailing_address}}</textarea></label><h3>Tags</h3><div class="actions">{% for tag in tags %}<label class="check"><input type="checkbox" name="tag_ids" value="{{tag.id}}" {% if tag.id in selected_tag_ids %}checked{% endif %}><span class="pill" style="border-left:4px solid {{tag.color}}">{{tag.name}}</span></label>{% else %}<span class="muted">Create tags in Fields & tags.</span>{% endfor %}</div>{% if fields %}<h3 style="margin-top:18px">Custom fields</h3>{% for field in fields %}<label class="field">{{field.label}}{% if field.data_type=='boolean' %}<select name="field_{{field.id}}"><option value="">—</option><option value="true" {% if values.get(field.id)=='true' %}selected{% endif %}>Yes</option><option value="false" {% if values.get(field.id)=='false' %}selected{% endif %}>No</option></select>{% elif field.data_type=='choice' %}<select name="field_{{field.id}}"><option value="">—</option>{% for option in field.options %}<option value="{{option}}" {% if values.get(field.id)==option %}selected{% endif %}>{{option}}</option>{% endfor %}</select>{% else %}<input type="{{'number' if field.data_type=='number' else field.data_type if field.data_type=='date' else 'text'}}" name="field_{{field.id}}" value="{{values.get(field.id,'')}}"></label>{% endif %}</label>{% endfor %}{% endif %}<button class="btn">Save contact</button></form><aside class="stack"><div class="panel"><h2>Properties</h2>{% for property in properties %}<article class="offer"><span class="pill">{{property.reference_number or 'No reference'}}</span><h3 style="margin-top:8px">{{property.property_address or property.county or 'Property details pending'}}</h3><div class="muted">{{property.county}}{% if property.state %}, {{property.state}}{% endif %}{% if property.acreage %} · {{property.acreage}} acres{% endif %}</div>{% if property.offer_price %}<b>{{property.offer_price}}</b>{% endif %}</article>{% else %}<div class="muted">No linked properties.</div>{% endfor %}</div></aside></div></main></body></html>
"""

PROPERTIES_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Properties · CRM</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('contacts')}}">← Contacts</a><h1 style="margin-top:8px">Properties</h1><div class="sub">Normalized property records linked to sellers and references</div></div></header><form class="toolbar"><input name="q" value="{{q}}" placeholder="Search reference, owner, address, APN, county..."><button class="btn">Search</button><a class="btn secondary" href="{{url_for('properties')}}">Clear</a></form><div class="table-wrap"><table><thead><tr><th>Reference</th><th>Owner</th><th>Location</th><th>Acreage</th><th>APN</th><th>Offer</th></tr></thead><tbody>{% for property in properties %}<tr><td><a class="leadlink" href="{{url_for('property_detail',property_id=property.id)}}">{{property.reference_number or 'Open property'}}</a></td><td>{% if property.contact_id %}<a class="leadlink" href="{{url_for('contact_detail',contact_id=property.contact_id)}}">{{property.owner_name or property.contact_name or 'Unknown'}}</a>{% else %}{{property.owner_name or 'Unknown'}}{% endif %}</td><td>{{property.property_address or property.county}}{% if property.state %}, {{property.state}}{% endif %}</td><td>{{property.acreage or '—'}}</td><td>{{property.apn or '—'}}</td><td>{{property.offer_price or '—'}}</td></tr>{% else %}<tr><td colspan="6" class="empty">No properties match.</td></tr>{% endfor %}</tbody></table></div></main></body></html>
"""

PROPERTY_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{property.reference_number or 'Property'}} · CRM</title><style>{{style}}</style></head><body><main class="shell" style="max-width:920px"><header class="top"><div><a class="muted" href="{{url_for('properties')}}">← Properties</a><h1 style="margin-top:8px">{{property.reference_number or 'Property'}}</h1><div class="sub">{{property.owner_name or 'Owner not set'}}{% if contact %} · <a href="{{url_for('contact_detail',contact_id=contact.id)}}">Open contact</a>{% endif %}</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><div class="two-col"><label class="field">Reference<input name="reference_number" value="{{property.reference_number}}"></label><label class="field">Owner<input name="owner_name" value="{{property.owner_name}}"></label></div><label class="field">Property address<input name="property_address" value="{{property.property_address}}"></label><div class="facts"><label class="field">County<input name="county" value="{{property.county}}"></label><label class="field">State<input name="state" value="{{property.state}}"></label><label class="field">Acreage<input name="acreage" value="{{property.acreage}}"></label><label class="field">APN<input name="apn" value="{{property.apn}}"></label><label class="field">Offer price<input name="offer_price" value="{{property.offer_price}}"></label></div><h3>Tags</h3><div class="actions">{% for tag in tags %}<label class="check"><input type="checkbox" name="tag_ids" value="{{tag.id}}" {% if tag.id in selected_tag_ids %}checked{% endif %}><span class="pill" style="border-left:4px solid {{tag.color}}">{{tag.name}}</span></label>{% endfor %}</div>{% if fields %}<h3 style="margin-top:18px">Custom fields</h3>{% for field in fields %}<label class="field">{{field.label}}{% if field.data_type=='boolean' %}<select name="field_{{field.id}}"><option value="">—</option><option value="true" {% if values.get(field.id)=='true' %}selected{% endif %}>Yes</option><option value="false" {% if values.get(field.id)=='false' %}selected{% endif %}>No</option></select>{% elif field.data_type=='choice' %}<select name="field_{{field.id}}"><option value="">—</option>{% for option in field.options %}<option value="{{option}}" {% if values.get(field.id)==option %}selected{% endif %}>{{option}}</option>{% endfor %}</select>{% else %}<input type="{{'number' if field.data_type=='number' else field.data_type if field.data_type=='date' else 'text'}}" name="field_{{field.id}}" value="{{values.get(field.id,'')}}">{% endif %}</label>{% endfor %}{% endif %}<button class="btn">Save property</button></form></main></body></html>
"""

SETTINGS_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Fields & tags · CRM</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('contacts')}}">← Contacts</a><h1 style="margin-top:8px">Fields & tags</h1><div class="sub">Organization-wide metadata for contacts and properties</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><section class="panel"><h2>Custom fields</h2><table><thead><tr><th>Label</th><th>Applies to</th><th>Type</th></tr></thead><tbody>{% for field in fields %}<tr><td>{{field.label}}</td><td>{{field.entity_type}}</td><td>{{field.data_type}}</td></tr>{% else %}<tr><td colspan="3">No custom fields.</td></tr>{% endfor %}</tbody></table><form method="post" style="margin-top:18px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="action" value="field"><label class="field">Label<input name="label" required placeholder="Seller temperature"></label><div class="two-col"><label class="field">Entity<select name="entity_type"><option value="contact">Contact</option><option value="property">Property</option></select></label><label class="field">Type<select name="data_type"><option value="text">Text</option><option value="number">Number</option><option value="date">Date</option><option value="boolean">Yes / no</option><option value="choice">Choice list</option></select></label></div><label class="field">Choices, comma separated<input name="options" placeholder="Hot, Warm, Cold"></label><button class="btn">Create field</button></form></section><section class="panel"><h2>Tags</h2><div class="actions">{% for tag in tags %}<span class="pill" style="border-left:5px solid {{tag.color}}">{{tag.name}}</span>{% else %}<span class="muted">No tags yet.</span>{% endfor %}</div><form method="post" style="margin-top:18px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="action" value="tag"><label class="field">Tag name<input name="name" required></label><label class="field">Color<input type="color" name="color" value="#235d4d"></label><button class="btn">Create tag</button></form></section></div></main></body></html>
"""

IMPORTS_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Imports · CRM</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('contacts')}}">← Contacts</a><h1 style="margin-top:8px">Import contacts</h1><div class="sub">Upload, map, validate, preview, then explicitly commit</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="panel" method="post" action="{{url_for('upload_import')}}" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Stage a CSV</h2><p class="muted">UTF-8 CSV, up to 5 MB and 10,000 data rows. Nothing is added until after validation and confirmation.</p><label class="field">CSV file<input type="file" name="file" accept=".csv,text/csv" required></label><button class="btn">Upload and preview</button></form><div class="panel"><h2>Import history</h2><table><thead><tr><th>File</th><th>Status</th><th>Rows</th><th>Created</th><th>Updated</th><th>Errors</th></tr></thead><tbody>{% for batch in batches %}<tr><td><a href="{{url_for('import_preview',batch_id=batch.id)}}">{{batch.filename}}</a></td><td>{{batch.status}}</td><td>{{batch.total_rows}}</td><td>{{batch.created_rows}}</td><td>{{batch.updated_rows}}</td><td>{{batch.error_rows}}</td></tr>{% else %}<tr><td colspan="6">No imports yet.</td></tr>{% endfor %}</tbody></table></div></main></body></html>
"""

IMPORT_PREVIEW_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Import preview · CRM</title><style>{{style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('imports')}}">← Imports</a><h1 style="margin-top:8px">{{batch.filename}}</h1><div class="sub">{{batch.total_rows}} rows · {{batch.status}}</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}{% if batch.status != 'committed' %}<form class="panel" method="post" action="{{url_for('validate_import',batch_id=batch.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Column mapping</h2><p class="muted">Phone is required and is the duplicate key. Unmapped columns are ignored.</p><div class="facts">{% for header in headers %}<label class="field">{{header}}<select name="map_{{loop.index0}}"><option value="">Ignore</option>{% for value,label in targets %}<option value="{{value}}" {% if mapping.get(header)==value %}selected{% endif %}>{{label}}</option>{% endfor %}</select></label>{% endfor %}</div><label class="field">When the phone already exists<select name="duplicate_policy"><option value="update" {% if batch.duplicate_policy=='update' %}selected{% endif %}>Update blank/contact fields</option><option value="skip" {% if batch.duplicate_policy=='skip' %}selected{% endif %}>Skip duplicate rows</option></select></label><button class="btn">Validate mapping and rows</button></form>{% endif %}{% if batch.status=='validated' %}<form class="panel" method="post" action="{{url_for('commit_import',batch_id=batch.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Validation result</h2><div class="stats"><div class="stat"><b>{{batch.valid_rows}}</b><span>Valid</span></div><div class="stat"><b>{{batch.error_rows}}</b><span>Errors</span></div><div class="stat"><b>{{duplicate_count}}</b><span>Existing phones</span></div><div class="stat"><b>{{batch.total_rows}}</b><span>Total</span></div></div><label class="check"><input type="checkbox" name="confirmed" value="yes" required><span>Commit these validated rows using the selected duplicate policy.</span></label><button class="btn">Commit import</button></form>{% endif %}<div class="panel"><h2>Row preview</h2><div class="table-wrap"><table><thead><tr><th>Row</th><th>Status</th><th>Normalized result</th><th>Error</th></tr></thead><tbody>{% for row in rows %}<tr><td>{{row.row_number}}</td><td>{{row.status}}</td><td><pre style="white-space:pre-wrap;margin:0">{{row.normalized_json}}</pre></td><td>{{row.error}}</td></tr>{% endfor %}</tbody></table></div>{% if batch.total_rows > 100 %}<div class="muted">Showing the first 100 rows.</div>{% endif %}</div></main></body></html>
"""


STANDARD_TARGETS = (
    ("full_name", "Contact name"), ("phone", "Phone (required)"),
    ("email", "Email"), ("mailing_address", "Mailing address"),
    ("reference_number", "Property reference"), ("property_address", "Property address"),
    ("county", "County"), ("state", "State"), ("acreage", "Acreage"),
    ("apn", "APN"), ("offer_price", "Offer price"), ("tags", "Tags"),
)


def _field_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]


def _phone(value: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not (7 <= len(digits) <= 15):
        raise ValueError("Phone must contain 7–15 digits")
    display = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits
    return digits, display


def _validate_custom(field: dict, value: str):
    value = value.strip()
    if not value:
        return None
    if field["data_type"] == "number":
        return float(value)
    if field["data_type"] == "date":
        return date.fromisoformat(value).isoformat()
    if field["data_type"] == "boolean":
        lowered = value.lower()
        if lowered not in {"true", "false", "yes", "no", "1", "0"}:
            raise ValueError(f"{field['label']} must be yes or no")
        return lowered in {"true", "yes", "1"}
    if field["data_type"] == "choice":
        options = json.loads(field["options_json"])
        if value not in options:
            raise ValueError(f"{field['label']} must be one of: {', '.join(options)}")
    return value


def register_phase2(app, db, style, stages, rows, organization_id, require_csrf, roles_required, audit):
    def fields(entity_type: str):
        result = rows(
            "SELECT * FROM custom_field_definitions WHERE organization_id=? AND entity_type=? AND active=1 ORDER BY label",
            (organization_id(), entity_type),
        )
        for field in result:
            field["options"] = json.loads(field["options_json"])
        return result

    @app.get("/contacts")
    def contacts():
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 100
        view_id = request.args.get("view", "")
        active = {"q": request.args.get("q", "").strip(), "status": request.args.get("status", "").strip(), "tag": request.args.get("tag", "").strip(), "has_reference": request.args.get("has_reference") == "1"}
        if view_id.isdigit():
            view = rows("SELECT * FROM saved_filters WHERE id=? AND organization_id=? AND entity_type='contact' AND (user_id=? OR shared=1)", (int(view_id), organization_id(), g.user["id"]))
            if view:
                active.update(json.loads(view[0]["definition_json"]))
        where, params = ["c.organization_id=?"], [organization_id()]
        if active["q"]:
            where.append("(c.full_name LIKE ? OR c.primary_phone_display LIKE ? OR c.primary_email LIKE ? OR l.reference_number LIKE ?)")
            params.extend([f"%{active['q']}%"] * 4)
        if active["status"] in stages:
            where.append("l.status=?")
            params.append(active["status"])
        if str(active["tag"]).isdigit():
            where.append("EXISTS(SELECT 1 FROM entity_tags et WHERE et.organization_id=c.organization_id AND et.entity_type='contact' AND et.entity_id=c.id AND et.tag_id=?)")
            params.append(int(active["tag"]))
        if active["has_reference"]:
            where.append("COALESCE(l.reference_number,'') != ''")
        contacts_data = rows(f"""SELECT c.*,l.status,l.latest_activity,l.reference_number,
            COUNT(DISTINCT cp.property_id) property_count,
            GROUP_CONCAT(DISTINCT t.name) tag_names
            FROM contacts c LEFT JOIN leads l ON l.id=c.lead_id AND l.organization_id=c.organization_id
            LEFT JOIN contact_properties cp ON cp.contact_id=c.id AND cp.organization_id=c.organization_id
            LEFT JOIN entity_tags et ON et.organization_id=c.organization_id AND et.entity_type='contact' AND et.entity_id=c.id
            LEFT JOIN tags t ON t.id=et.tag_id
            WHERE {' AND '.join(where)} GROUP BY c.id
            ORDER BY COALESCE(l.latest_activity,c.updated_at) DESC,c.id DESC
            LIMIT ? OFFSET ?""", [*params, page_size + 1, (page - 1) * page_size])
        has_more = len(contacts_data) > page_size
        contacts_data = contacts_data[:page_size]
        tags = rows("SELECT * FROM tags WHERE organization_id=? ORDER BY name", (organization_id(),))
        saved = rows("SELECT * FROM saved_filters WHERE organization_id=? AND entity_type='contact' AND (user_id=? OR shared=1) ORDER BY name", (organization_id(), g.user["id"]))
        return render_template_string(CONTACTS_TEMPLATE, style=style, contacts=contacts_data, tags=tags, saved_filters=saved, filters=active, stages=stages, page=page, has_more=has_more)

    @app.post("/contacts/filters")
    @roles_required("owner", "manager", "agent")
    def save_contact_filter():
        require_csrf()
        name = request.form.get("name", "").strip()[:80]
        if not name:
            abort(400)
        definition = {"q": request.form.get("q", "").strip(), "status": request.form.get("status", "").strip(), "tag": request.form.get("tag", "").strip(), "has_reference": request.form.get("has_reference") == "1"}
        with db.connect() as connection:
            connection.execute("""INSERT INTO saved_filters(organization_id,user_id,name,entity_type,definition_json,shared,created_at,updated_at)
                VALUES (?,?,?,'contact',?,?,?,?) ON CONFLICT(organization_id,user_id,entity_type,name) DO UPDATE SET definition_json=excluded.definition_json,shared=excluded.shared,updated_at=excluded.updated_at""",
                (organization_id(), g.user["id"], name, json.dumps(definition), int(request.form.get("shared") == "1"), utc_now(), utc_now()))
        audit("saved_filter.save", "saved_filter", details={"name": name})
        flash("Smart list saved.", "good")
        return redirect(url_for("contacts"))

    @app.route("/contacts/<int:contact_id>", methods=["GET", "POST"])
    def contact_detail(contact_id: int):
        contact = rows("SELECT * FROM contacts WHERE id=? AND organization_id=?", (contact_id, organization_id()))
        if not contact:
            abort(404)
        contact = contact[0]
        contact_fields = fields("contact")
        if request.method == "POST":
            if g.user["role"] not in {"owner", "manager", "agent"}:
                abort(403)
            require_csrf()
            email = request.form.get("primary_email", "").strip()
            if email and "@" not in email:
                flash("Enter a valid email address.", "error")
            else:
                parsed_values = {}
                try:
                    for field in contact_fields:
                        parsed_values[field["id"]] = _validate_custom(field, request.form.get(f"field_{field['id']}", ""))
                except ValueError as exc:
                    flash(str(exc), "error")
                else:
                    with db.connect() as connection:
                        connection.execute("UPDATE contacts SET full_name=?,primary_email=?,mailing_address=?,updated_at=? WHERE id=? AND organization_id=?", (request.form.get("full_name", "").strip(), email, request.form.get("mailing_address", "").strip(), utc_now(), contact_id, organization_id()))
                        if contact["lead_id"]:
                            connection.execute("UPDATE leads SET owner_name=?,updated_at=? WHERE id=? AND organization_id=?", (request.form.get("full_name", "").strip(), utc_now(), contact["lead_id"], organization_id()))
                        connection.execute(
                            """UPDATE opportunities SET title=?,updated_at=?,version=version+1
                               WHERE organization_id=? AND contact_id=?""",
                            (request.form.get("full_name", "").strip() or contact["primary_phone_display"], utc_now(), organization_id(), contact_id),
                        )
                        connection.execute("DELETE FROM entity_tags WHERE organization_id=? AND entity_type='contact' AND entity_id=?", (organization_id(), contact_id))
                        for value in request.form.getlist("tag_ids"):
                            if value.isdigit():
                                connection.execute("INSERT OR IGNORE INTO entity_tags VALUES (?,?, 'contact', ?, ?)", (organization_id(), int(value), contact_id, utc_now()))
                        for field_id, value in parsed_values.items():
                            if value is None:
                                connection.execute("DELETE FROM custom_field_values WHERE organization_id=? AND definition_id=? AND entity_type='contact' AND entity_id=?", (organization_id(), field_id, contact_id))
                            else:
                                connection.execute("""INSERT INTO custom_field_values(organization_id,definition_id,entity_type,entity_id,value_json,updated_at)
                                    VALUES (?,?,'contact',?,?,?) ON CONFLICT(organization_id,definition_id,entity_type,entity_id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""", (organization_id(), field_id, contact_id, json.dumps(value), utc_now()))
                    audit("contact.update", "contact", contact_id)
                    flash("Contact saved.", "good")
                    return redirect(url_for("contact_detail", contact_id=contact_id))
        tag_data = rows("SELECT * FROM tags WHERE organization_id=? ORDER BY name", (organization_id(),))
        selected = {row["tag_id"] for row in rows("SELECT tag_id FROM entity_tags WHERE organization_id=? AND entity_type='contact' AND entity_id=?", (organization_id(), contact_id))}
        raw_values = rows("SELECT definition_id,value_json FROM custom_field_values WHERE organization_id=? AND entity_type='contact' AND entity_id=?", (organization_id(), contact_id))
        values = {row["definition_id"]: str(json.loads(row["value_json"])).lower() if isinstance(json.loads(row["value_json"]), bool) else str(json.loads(row["value_json"])) for row in raw_values}
        property_data = rows("""SELECT p.* FROM properties p JOIN contact_properties cp ON cp.property_id=p.id AND cp.organization_id=p.organization_id WHERE cp.contact_id=? AND p.organization_id=? ORDER BY p.id""", (contact_id, organization_id()))
        return render_template_string(CONTACT_TEMPLATE, style=style, contact=contact, tags=tag_data, selected_tag_ids=selected, fields=contact_fields, values=values, properties=property_data)

    @app.get("/properties")
    def properties():
        q = request.args.get("q", "").strip()
        params = [organization_id()]
        clause = ""
        if q:
            clause = " AND (p.reference_number LIKE ? OR p.owner_name LIKE ? OR p.property_address LIKE ? OR p.apn LIKE ? OR p.county LIKE ?)"
            params.extend([f"%{q}%"] * 5)
        property_data = rows(f"""SELECT p.*,c.id contact_id,c.full_name contact_name FROM properties p
            LEFT JOIN contact_properties cp ON cp.property_id=p.id AND cp.organization_id=p.organization_id
            LEFT JOIN contacts c ON c.id=cp.contact_id AND c.organization_id=p.organization_id
            WHERE p.organization_id=? {clause} ORDER BY p.updated_at DESC,p.id DESC""", params)
        return render_template_string(PROPERTIES_TEMPLATE, style=style, properties=property_data, q=q)

    @app.route("/properties/<int:property_id>", methods=["GET", "POST"])
    def property_detail(property_id: int):
        found = rows("SELECT * FROM properties WHERE id=? AND organization_id=?", (property_id, organization_id()))
        if not found:
            abort(404)
        property_row = found[0]
        property_fields = fields("property")
        if request.method == "POST":
            if g.user["role"] not in {"owner", "manager", "agent"}:
                abort(403)
            require_csrf()
            parsed_values = {}
            try:
                for field in property_fields:
                    parsed_values[field["id"]] = _validate_custom(field, request.form.get(f"field_{field['id']}", ""))
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                reference = ", ".join(parse_references(request.form.get("reference_number", "")))
                try:
                    with db.connect() as connection:
                        connection.execute("""UPDATE properties SET reference_number=?,owner_name=?,property_address=?,county=?,state=?,acreage=?,apn=?,offer_price=?,updated_at=? WHERE id=? AND organization_id=?""", (
                            reference, request.form.get("owner_name", "").strip(), request.form.get("property_address", "").strip(),
                            request.form.get("county", "").strip(), request.form.get("state", "").strip(), request.form.get("acreage", "").strip(),
                            request.form.get("apn", "").strip(), request.form.get("offer_price", "").strip(), utc_now(), property_id, organization_id(),
                        ))
                        connection.execute("DELETE FROM entity_tags WHERE organization_id=? AND entity_type='property' AND entity_id=?", (organization_id(), property_id))
                        for value in request.form.getlist("tag_ids"):
                            if value.isdigit():
                                connection.execute("INSERT OR IGNORE INTO entity_tags VALUES (?,?,'property',?,?)", (organization_id(), int(value), property_id, utc_now()))
                        for field_id, value in parsed_values.items():
                            if value is None:
                                connection.execute("DELETE FROM custom_field_values WHERE organization_id=? AND definition_id=? AND entity_type='property' AND entity_id=?", (organization_id(), field_id, property_id))
                            else:
                                connection.execute("""INSERT INTO custom_field_values(organization_id,definition_id,entity_type,entity_id,value_json,updated_at)
                                    VALUES (?,?,'property',?,?,?) ON CONFLICT(organization_id,definition_id,entity_type,entity_id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""", (organization_id(), field_id, property_id, json.dumps(value), utc_now()))
                    audit("property.update", "property", property_id, {"reference_number": reference})
                    flash("Property saved.", "good")
                    return redirect(url_for("property_detail", property_id=property_id))
                except sqlite3.IntegrityError:
                    flash("That reference number is already assigned to another property.", "error")
        tag_data = rows("SELECT * FROM tags WHERE organization_id=? ORDER BY name", (organization_id(),))
        selected = {row["tag_id"] for row in rows("SELECT tag_id FROM entity_tags WHERE organization_id=? AND entity_type='property' AND entity_id=?", (organization_id(), property_id))}
        raw_values = rows("SELECT definition_id,value_json FROM custom_field_values WHERE organization_id=? AND entity_type='property' AND entity_id=?", (organization_id(), property_id))
        values = {}
        for row in raw_values:
            value = json.loads(row["value_json"])
            values[row["definition_id"]] = str(value).lower() if isinstance(value, bool) else str(value)
        contact = rows("""SELECT c.* FROM contacts c JOIN contact_properties cp ON cp.contact_id=c.id AND cp.organization_id=c.organization_id WHERE cp.property_id=? AND c.organization_id=? ORDER BY c.id LIMIT 1""", (property_id, organization_id()))
        return render_template_string(PROPERTY_TEMPLATE, style=style, property=property_row, contact=contact[0] if contact else None, tags=tag_data, selected_tag_ids=selected, fields=property_fields, values=values)

    @app.route("/settings/fields-tags", methods=["GET", "POST"])
    @roles_required("owner", "manager")
    def fields_tags():
        if request.method == "POST":
            require_csrf()
            action = request.form.get("action")
            try:
                with db.connect() as connection:
                    if action == "tag":
                        name = request.form.get("name", "").strip()[:60]
                        color = request.form.get("color", "#235d4d")
                        if not name or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                            abort(400)
                        cursor = connection.execute("INSERT INTO tags(organization_id,name,color,created_at) VALUES (?,?,?,?)", (organization_id(), name, color, utc_now()))
                        entity_id = cursor.lastrowid
                        event = "tag.create"
                    elif action == "field":
                        label = request.form.get("label", "").strip()[:80]
                        entity_type = request.form.get("entity_type", "")
                        data_type = request.form.get("data_type", "")
                        if not label or entity_type not in {"contact", "property"} or data_type not in {"text", "number", "date", "boolean", "choice"}:
                            abort(400)
                        options = [item.strip() for item in request.form.get("options", "").split(",") if item.strip()]
                        if data_type == "choice" and not options:
                            raise ValueError("Choice fields need at least one option.")
                        cursor = connection.execute("""INSERT INTO custom_field_definitions(organization_id,entity_type,field_key,label,data_type,options_json,created_at)
                            VALUES (?,?,?,?,?,?,?)""", (organization_id(), entity_type, _field_key(label), label, data_type, json.dumps(options), utc_now()))
                        entity_id = cursor.lastrowid
                        event = "custom_field.create"
                    else:
                        abort(400)
                audit(event, action, entity_id)
                flash("Created successfully.", "good")
                return redirect(url_for("fields_tags"))
            except ValueError as exc:
                flash(str(exc), "error")
            except sqlite3.IntegrityError:
                flash("That name is already in use.", "error")
        return render_template_string(SETTINGS_TEMPLATE, style=style, fields=rows("SELECT * FROM custom_field_definitions WHERE organization_id=? ORDER BY entity_type,label", (organization_id(),)), tags=rows("SELECT * FROM tags WHERE organization_id=? ORDER BY name", (organization_id(),)))

    @app.get("/imports")
    @roles_required("owner", "manager")
    def imports():
        batches = rows("SELECT * FROM import_batches WHERE organization_id=? ORDER BY id DESC LIMIT 50", (organization_id(),))
        return render_template_string(IMPORTS_TEMPLATE, style=style, batches=batches)

    @app.post("/imports/upload")
    @roles_required("owner", "manager")
    def upload_import():
        require_csrf()
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename or Path(uploaded.filename).suffix.lower() != ".csv":
            flash("Choose a CSV file.", "error")
            return redirect(url_for("imports"))
        payload = uploaded.read(5 * 1024 * 1024 + 1)
        if len(payload) > 5 * 1024 * 1024:
            flash("CSV files must be 5 MB or smaller.", "error")
            return redirect(url_for("imports"))
        try:
            text = payload.decode("utf-8-sig")
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            headers = [str(item or "").strip() for item in (reader.fieldnames or [])]
            raw_rows = list(reader)
        except (UnicodeDecodeError, csv.Error) as exc:
            flash(f"CSV could not be read: {exc}", "error")
            return redirect(url_for("imports"))
        if not headers or len(raw_rows) > 10000:
            flash("CSV needs a header row and no more than 10,000 data rows.", "error")
            return redirect(url_for("imports"))
        aliases = {"name": "full_name", "full_name": "full_name", "owner": "full_name", "owner_name": "full_name", "phone": "phone", "phone_number": "phone", "mobile": "phone", "email": "email", "mailing_address": "mailing_address", "address": "mailing_address", "reference": "reference_number", "reference_number": "reference_number", "property_address": "property_address", "county": "county", "state": "state", "acreage": "acreage", "apn": "apn", "offer_price": "offer_price", "tags": "tags"}
        mapping = {header: aliases.get(_field_key(header), "") for header in headers}
        with db.connect() as connection:
            cursor = connection.execute("""INSERT INTO import_batches(organization_id,uploaded_by,filename,status,headers_json,mapping_json,total_rows,created_at)
                VALUES (?,?,?,'staged',?,?,?,?)""", (organization_id(), g.user["id"], Path(uploaded.filename).name[:200], json.dumps(headers), json.dumps(mapping), len(raw_rows), utc_now()))
            batch_id = cursor.lastrowid
            connection.executemany("INSERT INTO import_rows(organization_id,batch_id,row_number,data_json) VALUES (?,?,?,?)", [(organization_id(), batch_id, number, json.dumps({header: row.get(header, "") for header in headers})) for number, row in enumerate(raw_rows, 2)])
        audit("import.stage", "import_batch", batch_id, {"filename": Path(uploaded.filename).name, "rows": len(raw_rows)})
        return redirect(url_for("import_preview", batch_id=batch_id))

    def get_batch(batch_id):
        batch = rows("SELECT * FROM import_batches WHERE id=? AND organization_id=?", (batch_id, organization_id()))
        if not batch:
            abort(404)
        return batch[0]

    @app.get("/imports/<int:batch_id>")
    @roles_required("owner", "manager")
    def import_preview(batch_id):
        batch = get_batch(batch_id)
        headers = json.loads(batch["headers_json"])
        mapping = json.loads(batch["mapping_json"])
        custom = fields("contact")
        targets = list(STANDARD_TARGETS) + [(f"custom:{field['id']}", f"Custom · {field['label']}") for field in custom]
        preview = rows("SELECT * FROM import_rows WHERE batch_id=? AND organization_id=? ORDER BY row_number LIMIT 100", (batch_id, organization_id()))
        duplicate_count = sum(1 for row in preview if json.loads(row["normalized_json"] or "{}").get("duplicate")) if batch["status"] == "validated" else 0
        return render_template_string(IMPORT_PREVIEW_TEMPLATE, style=style, batch=batch, headers=headers, mapping=mapping, targets=targets, rows=preview, duplicate_count=duplicate_count)

    @app.post("/imports/<int:batch_id>/validate")
    @roles_required("owner", "manager")
    def validate_import(batch_id):
        require_csrf()
        batch = get_batch(batch_id)
        if batch["status"] == "committed":
            abort(409)
        headers = json.loads(batch["headers_json"])
        mapping = {header: request.form.get(f"map_{index}", "") for index, header in enumerate(headers)}
        targets = [value for value in mapping.values() if value]
        allowed = {value for value, _ in STANDARD_TARGETS} | {f"custom:{field['id']}" for field in fields("contact")}
        if "phone" not in targets or len(targets) != len(set(targets)) or not set(targets).issubset(allowed):
            flash("Map exactly one column to Phone and do not map two columns to the same field.", "error")
            return redirect(url_for("import_preview", batch_id=batch_id))
        policy = request.form.get("duplicate_policy", "update")
        if policy not in {"update", "skip"}:
            abort(400)
        definitions = {str(field["id"]): field for field in fields("contact")}
        staged = rows("SELECT * FROM import_rows WHERE batch_id=? AND organization_id=? ORDER BY row_number", (batch_id, organization_id()))
        valid = errors = 0
        with db.connect() as connection:
            for item in staged:
                raw = json.loads(item["data_json"])
                normalized, error = {}, ""
                try:
                    for header, target in mapping.items():
                        if target:
                            normalized[target] = str(raw.get(header, "") or "").strip()
                    normalized["phone"], normalized["phone_display"] = _phone(normalized.get("phone", ""))
                    normalized["reference_number"] = ", ".join(parse_references(normalized.get("reference_number", "")))
                    if normalized.get("email") and "@" not in normalized["email"]:
                        raise ValueError("Invalid email")
                    for target, value in list(normalized.items()):
                        if target.startswith("custom:"):
                            normalized[target] = _validate_custom(definitions[target.split(":", 1)[1]], value)
                    existing = connection.execute("SELECT id FROM contacts WHERE organization_id=? AND primary_phone_normalized=?", (organization_id(), normalized["phone"])).fetchone()
                    normalized["duplicate"] = bool(existing)
                    normalized["duplicate_contact_id"] = existing["id"] if existing else None
                    valid += 1
                    status = "valid"
                except (ValueError, KeyError) as exc:
                    error = str(exc)
                    errors += 1
                    status = "error"
                connection.execute("UPDATE import_rows SET normalized_json=?,status=?,error=? WHERE id=? AND organization_id=?", (json.dumps(normalized), status, error, item["id"], organization_id()))
            connection.execute("UPDATE import_batches SET status='validated',duplicate_policy=?,mapping_json=?,valid_rows=?,error_rows=?,error='' WHERE id=? AND organization_id=?", (policy, json.dumps(mapping), valid, errors, batch_id, organization_id()))
        audit("import.validate", "import_batch", batch_id, {"valid": valid, "errors": errors, "policy": policy})
        return redirect(url_for("import_preview", batch_id=batch_id))

    @app.post("/imports/<int:batch_id>/commit")
    @roles_required("owner", "manager")
    def commit_import(batch_id):
        require_csrf()
        batch = get_batch(batch_id)
        if batch["status"] == "committed":
            flash("This batch was already committed; no rows were run twice.", "good")
            return redirect(url_for("import_preview", batch_id=batch_id))
        if batch["status"] != "validated" or request.form.get("confirmed") != "yes":
            abort(400)
        staged = rows("SELECT * FROM import_rows WHERE batch_id=? AND organization_id=? AND status='valid' ORDER BY row_number", (batch_id, organization_id()))
        created = updated = skipped = 0
        with db.connect() as connection:
            for item in staged:
                data = json.loads(item["normalized_json"])
                contact = connection.execute("SELECT * FROM contacts WHERE organization_id=? AND primary_phone_normalized=?", (organization_id(), data["phone"])).fetchone()
                if contact and batch["duplicate_policy"] == "skip":
                    skipped += 1
                    connection.execute("UPDATE import_rows SET status='skipped',entity_id=? WHERE id=?", (contact["id"], item["id"]))
                    continue
                now = utc_now()
                if contact:
                    contact_id, lead_id = contact["id"], contact["lead_id"]
                    connection.execute("""UPDATE contacts SET full_name=CASE WHEN ?!='' THEN ? ELSE full_name END,primary_email=CASE WHEN ?!='' THEN ? ELSE primary_email END,mailing_address=CASE WHEN ?!='' THEN ? ELSE mailing_address END,updated_at=? WHERE id=?""", (data.get("full_name", ""), data.get("full_name", ""), data.get("email", ""), data.get("email", ""), data.get("mailing_address", ""), data.get("mailing_address", ""), now, contact_id))
                    updated += 1
                    row_status = "updated"
                else:
                    cursor = connection.execute("""INSERT INTO leads(organization_id,phone_normalized,phone_display,owner_name,reference_number,lead_source,created_at,updated_at)
                        VALUES (?,?,?,?,?,'CSV import',?,?)""", (organization_id(), data["phone"], data["phone_display"], data.get("full_name", ""), data.get("reference_number", ""), now, now))
                    lead_id = cursor.lastrowid
                    cursor = connection.execute("""INSERT INTO contacts(organization_id,lead_id,full_name,primary_phone_normalized,primary_phone_display,primary_email,mailing_address,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (organization_id(), lead_id, data.get("full_name", ""), data["phone"], data["phone_display"], data.get("email", ""), data.get("mailing_address", ""), now, now))
                    contact_id = cursor.lastrowid
                    created += 1
                    row_status = "created"
                if lead_id:
                    connection.execute("UPDATE leads SET owner_name=CASE WHEN ?!='' THEN ? ELSE owner_name END,reference_number=CASE WHEN ?!='' THEN ? ELSE reference_number END,updated_at=? WHERE id=? AND organization_id=?", (data.get("full_name", ""), data.get("full_name", ""), data.get("reference_number", ""), data.get("reference_number", ""), now, lead_id, organization_id()))
                reference = data.get("reference_number", "")
                if reference or any(data.get(key) for key in ("property_address", "county", "state", "acreage", "apn", "offer_price")):
                    if reference:
                        connection.execute("""INSERT INTO properties(organization_id,lead_id,reference_number,owner_name,property_address,county,state,acreage,apn,offer_price,source,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,'csv',?,?) ON CONFLICT(organization_id,reference_number) WHERE reference_number != '' DO UPDATE SET lead_id=excluded.lead_id,owner_name=excluded.owner_name,property_address=excluded.property_address,county=excluded.county,state=excluded.state,acreage=excluded.acreage,apn=excluded.apn,offer_price=excluded.offer_price,updated_at=excluded.updated_at""", (organization_id(), lead_id, reference, data.get("full_name", ""), data.get("property_address", ""), data.get("county", ""), data.get("state", ""), data.get("acreage", ""), data.get("apn", ""), data.get("offer_price", ""), now, now))
                        property_id = connection.execute("SELECT id FROM properties WHERE organization_id=? AND reference_number=?", (organization_id(), reference)).fetchone()["id"]
                    else:
                        cursor = connection.execute("""INSERT INTO properties(organization_id,lead_id,owner_name,property_address,county,state,acreage,apn,offer_price,source,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,'csv',?,?)""", (organization_id(), lead_id, data.get("full_name", ""), data.get("property_address", ""), data.get("county", ""), data.get("state", ""), data.get("acreage", ""), data.get("apn", ""), data.get("offer_price", ""), now, now))
                        property_id = cursor.lastrowid
                    connection.execute("INSERT OR IGNORE INTO contact_properties VALUES (?,?,?,'owner')", (organization_id(), contact_id, property_id))
                for name in [part.strip() for part in re.split(r"[,;]", data.get("tags", "")) if part.strip()]:
                    connection.execute("INSERT OR IGNORE INTO tags(organization_id,name,created_at) VALUES (?,?,?)", (organization_id(), name[:60], now))
                    tag_id = connection.execute("SELECT id FROM tags WHERE organization_id=? AND name=?", (organization_id(), name[:60])).fetchone()["id"]
                    connection.execute("INSERT OR IGNORE INTO entity_tags VALUES (?,?,'contact',?,?)", (organization_id(), tag_id, contact_id, now))
                for target, value in data.items():
                    if target.startswith("custom:") and value is not None:
                        field_id = int(target.split(":", 1)[1])
                        connection.execute("""INSERT INTO custom_field_values(organization_id,definition_id,entity_type,entity_id,value_json,updated_at)
                            VALUES (?,?,'contact',?,?,?) ON CONFLICT(organization_id,definition_id,entity_type,entity_id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""", (organization_id(), field_id, contact_id, json.dumps(value), now))
                connection.execute("UPDATE import_rows SET status=?,entity_id=? WHERE id=?", (row_status, contact_id, item["id"]))
            connection.execute("UPDATE import_batches SET status='committed',created_rows=?,updated_rows=?,skipped_rows=?,committed_at=? WHERE id=? AND organization_id=?", (created, updated, skipped, utc_now(), batch_id, organization_id()))
        audit("import.commit", "import_batch", batch_id, {"created": created, "updated": updated, "skipped": skipped})
        flash(f"Import complete: {created} created, {updated} updated, {skipped} skipped.", "good")
        return redirect(url_for("import_preview", batch_id=batch_id))
