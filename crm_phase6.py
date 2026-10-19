"""Phase 6 document templates, generated contracts, and e-sign evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from flask import abort, flash, g, jsonify, redirect, render_template_string, request, send_file, url_for
from werkzeug.utils import secure_filename

from crm_db import utc_now
from document_service import MERGE_FIELDS, evidence_json, generate_pdf, merge_context, merge_template, template_fields, validate_template, validate_uploaded_pdf


PHASE6_STYLE = """
.document-card{border-left:5px solid var(--line)}.document-card.signed,.document-card.completed{border-left-color:var(--accent)}.document-card.declined,.document-card.expired{border-left-color:var(--urgent)}.merge-token{display:inline-block;background:#edf2ef;border-radius:6px;padding:4px 7px;margin:3px;font:12px ui-monospace,monospace}.document-preview{white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;border:1px solid var(--line);border-radius:10px;padding:22px;min-height:320px}.event-ledger{border-left:2px solid var(--line);margin-left:8px}.event-ledger article{padding:0 0 17px 18px}.legal-note{border-left:4px solid var(--accent2);background:#fff7f1;padding:12px;border-radius:6px}
"""


LIST_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Documents · CRM</title><style>{{style}}{{phase6_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Documents</h1><div class="sub">Versioned templates, agreements, and signature evidence</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="legal-note"><b>Review required:</b> starter language is not legal advice. Preview every merge and have your approved agreement reviewed before sending.</div><div class="grid" style="margin-top:18px"><section><h2>Documents</h2><div class="stack">{% for item in documents %}<article class="panel document-card {{item.status}}"><div class="top"><div><a class="leadlink" href="{{url_for('document_detail',document_id=item.id)}}">{{item.title}}</a><div class="muted">{{item.recipient_name or item.contact_name or 'No recipient'}} · {{item.updated_at}}</div></div><span class="pill {{item.status}}">{{item.status}}</span></div><div class="muted">{{item.deal_title or 'No linked deal'}}{% if item.reference_number %} · {{item.reference_number}}{% endif %}</div></article>{% else %}<div class="panel empty">No documents yet. Open a deal to create one.</div>{% endfor %}</div></section><aside><div class="panel"><h2>Templates</h2>{% for item in templates %}<div style="padding:10px 0;border-bottom:1px solid var(--line)"><a class="leadlink" href="{{url_for('document_template_detail',template_id=item.id)}}">{{item.name}}</a><div class="muted">{{'Version ' ~ item.published_version if item.published_version else 'Unpublished draft'}}</div></div>{% endfor %}{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('new_document_template')}}" style="margin-top:14px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">+ New template</button></form>{% endif %}</div></aside></div></main></body></html>
"""


TEMPLATE_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{template.name}} · Template</title><style>{{style}}{{phase6_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('documents')}}">← Documents</a><h1 style="margin-top:8px">{{template.name}}</h1><div class="sub">{{'Published version ' ~ template.published_version if template.published_version else 'Unpublished draft'}}</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Template name<input name="name" value="{{template.name}}" required></label><label class="field">Description<input name="description" value="{{template.description}}"></label><label class="field">Draft body<textarea name="body" style="min-height:620px;font-family:ui-monospace,monospace">{{template.draft_body}}</textarea></label>{% if current_user.role in ('owner','manager') %}<button class="btn">Save draft</button>{% endif %}</form><aside class="stack"><section class="panel"><h2>Allowed merge fields</h2>{% for field in fields %}<code class="merge-token">{{ '{{ ' ~ field ~ ' }}' }}</code>{% endfor %}</section><section class="panel"><h2>Publish</h2><p class="muted">Publishing makes an immutable version available when creating a document. Existing documents stay pinned to their original version.</p>{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('publish_document_template',template_id=template.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="check"><input type="checkbox" name="confirmed" value="yes" required>I reviewed the merge fields and language.</label><button class="btn">Publish new version</button></form>{% endif %}</section><section class="panel"><h2>Version history</h2>{% for version in versions %}<details><summary>Version {{version.version}} · {{version.created_at}}</summary><div class="document-preview" style="min-height:0;margin-top:8px">{{version.body}}</div></details>{% else %}<div class="muted">No published versions.</div>{% endfor %}</section></aside></div></main></body></html>
"""


NEW_DOCUMENT_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>New document · CRM</title><style>{{style}}{{phase6_style}}</style></head><body><main class="shell" style="max-width:760px"><a class="muted" href="{{url_for('deal_detail',opportunity_id=deal.id)}}">← Deal</a><h1 style="margin-top:8px">Create document</h1><div class="panel"><div class="facts"><div class="fact"><b>Deal</b>{{deal.title}}</div><div class="fact"><b>Seller</b>{{deal.contact_name or deal.phone_display}}</div><div class="fact"><b>Property</b>{{deal.reference_number or deal.property_address or 'Not linked'}}</div></div></div><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Published template<select name="template_id" required><option value="">Choose a template</option>{% for item in templates %}<option value="{{item.id}}">{{item.name}} · version {{item.published_version}}</option>{% endfor %}</select></label><label class="field">Document title<input name="title" value="Purchase Agreement - {{deal.contact_name or deal.title}}" required></label><label class="field">Recipient name<input name="recipient_name" value="{{deal.contact_name}}"></label><label class="field">Recipient email<input type="email" name="recipient_email" value="{{deal.primary_email}}"></label><button class="btn">Create draft and preview</button></form></main></body></html>
"""


DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{document.title}} · Document</title><style>{{style}}{{phase6_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('documents')}}">← Documents</a><h1 style="margin-top:8px">{{document.title}}</h1><div class="sub">Template version {{document.template_version or template.published_version}} · {{document.recipient_name or 'No recipient'}}</div></div><div class="top-actions"><span class="pill {{document.status}}">{{document.status}}</span>{% if document.opportunity_id %}<a class="btn secondary" href="{{url_for('deal_detail',opportunity_id=document.opportunity_id)}}">Open deal</a>{% endif %}</div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><section class="stack"><div class="panel"><h2>Merged preview</h2><div class="legal-note">Check every “Not provided” value and the complete language before generating or sending.</div><div class="document-preview" style="margin-top:12px">{{preview}}</div></div><div class="panel"><h2>Files</h2>{% for file in files %}<div class="actions"><a class="btn secondary" href="{{url_for('download_document_file',document_id=document.id,file_id=file.id)}}">Download {{file.kind}} PDF</a><span class="muted">{{file.size_bytes}} bytes · SHA-256 {{file.sha256[:16]}}…</span></div>{% else %}<div class="muted">No generated files yet.</div>{% endfor %}</div></section><aside class="stack"><section class="panel"><h2>Actions</h2>{% if document.status=='draft' and current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('generate_document',document_id=document.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="check"><input type="checkbox" name="confirmed" value="yes" required>I reviewed the merged preview.</label><button class="btn">Generate immutable PDF</button></form>{% endif %}{% if document.status=='generated' and current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('send_document',document_id=document.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Recipient email<input type="email" name="recipient_email" value="{{document.recipient_email}}" required></label><label class="check"><input type="checkbox" name="confirmed" value="yes" required>I sent or will send this exact PDF externally and want to record the sent state.</label><button class="btn">Record as sent</button></form>{% endif %}{% if document.status in ('sent','delivered','viewed','signed') and current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('upload_signed_document',document_id=document.id)}}" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Completed signed PDF<input type="file" name="file" accept="application/pdf,.pdf" required></label><label class="check"><input type="checkbox" name="confirmed" value="yes" required>I verified this is the completed signed document.</label><button class="btn">Store signed PDF</button></form>{% endif %}</section><section class="panel"><h2>Evidence ledger</h2><div class="event-ledger">{% for event in events %}<article><b>{{event.event_type|replace('_',' ')|title}}</b><div class="muted">{{event.occurred_at}} · {{event.actor_name or event.provider}}</div>{% if event.actor_email %}<div class="muted">{{event.actor_email}}</div>{% endif %}<details><summary class="muted">Evidence</summary><pre class="code-preview">{{event.evidence_json}}</pre></details></article>{% else %}<div class="muted">No events yet.</div>{% endfor %}</div></section></aside></div></main></body></html>
"""

LIST_TEMPLATE = LIST_TEMPLATE.replace(
    "</header>",
    "<a class=\"btn\" href=\"{{url_for('choose_document_deal')}}\">+ Create from deal</a></header>",
    1,
)

CHOOSE_DEAL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Choose deal · Documents</title><style>{{style}}{{phase6_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('documents')}}">← Documents</a><h1 style="margin-top:8px">Choose a deal</h1><div class="sub">Documents remain linked to their seller, property, and deal history</div></div></header><div class="table-wrap"><table><thead><tr><th>Deal</th><th>Seller</th><th>Property</th><th>Stage</th><th></th></tr></thead><tbody>{% for deal in deals %}<tr><td>{{deal.title}}</td><td>{{deal.contact_name or deal.phone_display}}</td><td>{{deal.reference_number or deal.property_address or 'Not linked'}}</td><td>{{deal.stage_name}}</td><td><a class="btn secondary" href="{{url_for('new_document',opportunity_id=deal.id)}}">Create document</a></td></tr>{% else %}<tr><td colspan="5" class="empty">No deals available.</td></tr>{% endfor %}</tbody></table></div></main></body></html>
"""


def register_phase6(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def record_event(connection, document, event_type: str, *, actor_name="", actor_email="", provider="manual", provider_event_id="", evidence=None, occurred_at=None):
        occurred_at = occurred_at or utc_now()
        cursor = connection.execute("""INSERT OR IGNORE INTO document_events(
            organization_id,document_id,event_type,actor_name,actor_email,provider,provider_event_id,evidence_json,occurred_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""", (document["organization_id"], document["id"], event_type, actor_name, actor_email, provider, provider_event_id, evidence_json(**(evidence or {})), occurred_at, utc_now()))
        if not cursor.rowcount:
            return None
        title = f"Document {event_type.replace('_',' ')}: {document['title']}"
        connection.execute("""INSERT OR IGNORE INTO activity_events(
            organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
            occurred_at,title,body,source_table,source_id,metadata_json,created_at
        ) VALUES (?,?,?,?,?,'document','internal',?,?,?,?,?,?,?)""", (
            document["organization_id"], document["lead_id"], document["contact_id"], document["opportunity_id"],
            f"document_{event_type}", occurred_at, title, "", "document_events", cursor.lastrowid,
            evidence_json(document_id=document["id"], provider=provider, event_type=event_type), utc_now(),
        ))
        return cursor.lastrowid

    @app.get("/documents")
    def documents():
        document_rows = rows("""SELECT d.*,o.title deal_title,c.full_name contact_name,p.reference_number
            FROM documents d LEFT JOIN opportunities o ON o.id=d.opportunity_id
            LEFT JOIN contacts c ON c.id=d.contact_id LEFT JOIN properties p ON p.id=d.property_id
            WHERE d.organization_id=? ORDER BY d.updated_at DESC,d.id DESC""", (organization_id(),))
        templates = rows("SELECT * FROM document_templates WHERE organization_id=? AND active=1 ORDER BY name", (organization_id(),))
        return render_template_string(LIST_TEMPLATE, style=style, phase6_style=PHASE6_STYLE, documents=document_rows, templates=templates)

    @app.get("/documents/new")
    @roles_required("owner", "manager", "agent")
    def choose_document_deal():
        deals = rows("""SELECT o.id,o.title,c.full_name contact_name,c.primary_phone_display phone_display,
            p.reference_number,p.property_address,s.name stage_name
            FROM opportunities o JOIN pipeline_stages s ON s.id=o.stage_id
            LEFT JOIN contacts c ON c.id=o.contact_id AND c.organization_id=o.organization_id
            LEFT JOIN properties p ON p.id=o.property_id AND p.organization_id=o.organization_id
            WHERE o.organization_id=? ORDER BY o.updated_at DESC LIMIT 250""", (organization_id(),))
        return render_template_string(CHOOSE_DEAL_TEMPLATE, style=style, phase6_style=PHASE6_STYLE, deals=deals)

    @app.post("/documents/templates/new")
    @roles_required("owner", "manager")
    def new_document_template():
        require_csrf()
        now = utc_now()
        with db.connect() as connection:
            base, suffix, name = "New document template", 2, "New document template"
            while connection.execute("SELECT 1 FROM document_templates WHERE organization_id=? AND name=?", (organization_id(), name)).fetchone():
                name, suffix = f"{base} {suffix}", suffix + 1
            cursor = connection.execute("""INSERT INTO document_templates(organization_id,name,description,draft_body,created_by,created_at,updated_at)
                VALUES (?,?,?,'DOCUMENT TITLE\n\nSeller: {{ seller_name }}\nProperty: {{ reference_number }}\n\nReplace this text with reviewed language.',?,?,?)""", (organization_id(), name, "", g.user.get("id"), now, now))
        audit("document_template.create", "document_template", cursor.lastrowid)
        return redirect(url_for("document_template_detail", template_id=cursor.lastrowid))

    @app.route("/documents/templates/<int:template_id>", methods=["GET", "POST"])
    def document_template_detail(template_id: int):
        found = rows("SELECT * FROM document_templates WHERE id=? AND organization_id=?", (template_id, organization_id()))
        if not found:
            abort(404)
        template = found[0]
        if request.method == "POST":
            if g.user["role"] not in {"owner", "manager"}:
                abort(403)
            require_csrf()
            body = request.form.get("body", "")
            try:
                validate_template(body)
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                name = request.form.get("name", "").strip()[:120]
                if not name:
                    flash("Template name is required.", "error")
                else:
                    with db.connect() as connection:
                        connection.execute("UPDATE document_templates SET name=?,description=?,draft_body=?,updated_at=? WHERE id=? AND organization_id=?", (name, request.form.get("description", "").strip()[:500], body, utc_now(), template_id, organization_id()))
                    audit("document_template.draft_save", "document_template", template_id)
                    flash("Template draft saved. Published versions are unchanged.", "good")
                    return redirect(url_for("document_template_detail", template_id=template_id))
            template = rows("SELECT * FROM document_templates WHERE id=? AND organization_id=?", (template_id, organization_id()))[0]
        versions = rows("SELECT * FROM document_template_versions WHERE template_id=? AND organization_id=? ORDER BY version DESC", (template_id, organization_id()))
        return render_template_string(TEMPLATE_TEMPLATE, style=style, phase6_style=PHASE6_STYLE, template=template, versions=versions, fields=sorted(MERGE_FIELDS))

    @app.post("/documents/templates/<int:template_id>/publish")
    @roles_required("owner", "manager")
    def publish_document_template(template_id: int):
        require_csrf()
        if request.form.get("confirmed") != "yes":
            abort(400)
        with db.connect() as connection:
            template = connection.execute("SELECT * FROM document_templates WHERE id=? AND organization_id=?", (template_id, organization_id())).fetchone()
            if not template:
                abort(404)
            fields = validate_template(template["draft_body"])
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM document_template_versions WHERE template_id=?", (template_id,)).fetchone()[0]
            connection.execute("""INSERT INTO document_template_versions(organization_id,template_id,version,body,merge_fields_json,published_by,created_at)
                VALUES (?,?,?,?,?,?,?)""", (organization_id(), template_id, version, template["draft_body"], json.dumps(fields), g.user.get("id"), utc_now()))
            connection.execute("UPDATE document_templates SET published_version=?,updated_at=? WHERE id=?", (version, utc_now(), template_id))
        audit("document_template.publish", "document_template", template_id, {"version": version})
        flash(f"Template version {version} published.", "good")
        return redirect(url_for("document_template_detail", template_id=template_id))

    @app.route("/deals/<int:opportunity_id>/documents/new", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def new_document(opportunity_id: int):
        deal_rows = rows("""SELECT o.*,c.full_name contact_name,c.primary_email,c.primary_phone_display phone_display,
            p.reference_number,p.property_address FROM opportunities o
            LEFT JOIN contacts c ON c.id=o.contact_id LEFT JOIN properties p ON p.id=o.property_id
            WHERE o.id=? AND o.organization_id=?""", (opportunity_id, organization_id()))
        if not deal_rows:
            abort(404)
        deal = deal_rows[0]
        templates = rows("SELECT * FROM document_templates WHERE organization_id=? AND active=1 AND published_version IS NOT NULL ORDER BY name", (organization_id(),))
        if request.method == "POST":
            require_csrf()
            template_id = request.form.get("template_id", "")
            template = rows("SELECT * FROM document_templates WHERE id=? AND organization_id=? AND published_version IS NOT NULL", (int(template_id), organization_id())) if template_id.isdigit() else []
            if not template:
                abort(400)
            title = request.form.get("title", "").strip()[:160]
            email = request.form.get("recipient_email", "").strip()
            if not title or (email and "@" not in email):
                flash("Enter a title and valid recipient email.", "error")
            else:
                now = utc_now()
                with db.connect() as connection:
                    cursor = connection.execute("""INSERT INTO documents(
                        organization_id,opportunity_id,lead_id,contact_id,property_id,template_id,title,
                        recipient_name,recipient_email,status,created_by,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?, ?,?,'draft',?,?,?)""", (organization_id(), opportunity_id, deal["lead_id"], deal["contact_id"], deal["property_id"], int(template_id), title, request.form.get("recipient_name", "").strip(), email, g.user.get("id"), now, now))
                    document = connection.execute("SELECT * FROM documents WHERE id=?", (cursor.lastrowid,)).fetchone()
                    record_event(connection, document, "created", actor_name=g.user["display_name"], evidence={"template_id": int(template_id)})
                audit("document.create", "document", cursor.lastrowid, {"opportunity_id": opportunity_id})
                return redirect(url_for("document_detail", document_id=cursor.lastrowid))
        return render_template_string(NEW_DOCUMENT_TEMPLATE, style=style, phase6_style=PHASE6_STYLE, deal=deal, templates=templates)

    @app.get("/documents/<int:document_id>")
    def document_detail(document_id: int):
        found = rows("SELECT * FROM documents WHERE id=? AND organization_id=?", (document_id, organization_id()))
        if not found:
            abort(404)
        document = found[0]
        template = rows("SELECT * FROM document_templates WHERE id=? AND organization_id=?", (document["template_id"], organization_id()))[0]
        if document["rendered_body"]:
            preview = document["rendered_body"]
        else:
            version = rows("SELECT * FROM document_template_versions WHERE template_id=? AND version=? AND organization_id=?", (template["id"], template["published_version"], organization_id()))
            if not version:
                preview = "Publish the template before generating this document."
            else:
                with db.connect() as connection:
                    _, values = merge_context(connection, organization_id(), document["opportunity_id"])
                preview = merge_template(version[0]["body"], values)
        files = rows("SELECT * FROM document_files WHERE document_id=? AND organization_id=? ORDER BY id", (document_id, organization_id()))
        events = rows("SELECT * FROM document_events WHERE document_id=? AND organization_id=? ORDER BY occurred_at DESC,id DESC", (document_id, organization_id()))
        return render_template_string(DETAIL_TEMPLATE, style=style, phase6_style=PHASE6_STYLE, document=document, template=template, preview=preview, files=files, events=events)

    @app.post("/documents/<int:document_id>/generate")
    @roles_required("owner", "manager", "agent")
    def generate_document(document_id: int):
        require_csrf()
        if request.form.get("confirmed") != "yes":
            abort(400)
        with db.connect() as connection:
            document = connection.execute("SELECT * FROM documents WHERE id=? AND organization_id=?", (document_id, organization_id())).fetchone()
            if not document or document["status"] != "draft":
                abort(409)
            template = connection.execute("SELECT * FROM document_templates WHERE id=? AND organization_id=?", (document["template_id"], organization_id())).fetchone()
            version = connection.execute("SELECT * FROM document_template_versions WHERE template_id=? AND version=?", (template["id"], template["published_version"])).fetchone()
            if not version:
                abort(409)
            _, values = merge_context(connection, organization_id(), document["opportunity_id"])
            body = merge_template(version["body"], values)
        relative = Path("documents") / str(organization_id()) / str(document_id) / f"generated-v{version['version']}.pdf"
        absolute = db.path.parent / relative
        digest, pages = generate_pdf(absolute, document["title"], body, values)
        with db.connect() as connection:
            connection.execute("""UPDATE documents SET template_version=?,merge_data_json=?,rendered_body=?,artifact_path=?,artifact_sha256=?,status='generated',updated_at=? WHERE id=? AND organization_id=?""", (version["version"], json.dumps(values, sort_keys=True), body, str(relative), digest, utc_now(), document_id, organization_id()))
            connection.execute("""INSERT INTO document_files(organization_id,document_id,kind,file_path,original_name,size_bytes,sha256,uploaded_by,created_at)
                VALUES (?,?,'generated',?,?,?,?,?,?)""", (organization_id(), document_id, str(relative), absolute.name, absolute.stat().st_size, digest, g.user.get("id"), utc_now()))
            updated = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            record_event(connection, updated, "generated", actor_name=g.user["display_name"], evidence={"sha256": digest, "pages": pages, "template_version": version["version"]})
        audit("document.generate", "document", document_id, {"sha256": digest, "pages": pages})
        flash("Immutable PDF generated and hashed.", "good")
        return redirect(url_for("document_detail", document_id=document_id))

    @app.post("/documents/<int:document_id>/send")
    @roles_required("owner", "manager", "agent")
    def send_document(document_id: int):
        require_csrf()
        if request.form.get("confirmed") != "yes":
            abort(400)
        email = request.form.get("recipient_email", "").strip()
        if "@" not in email:
            abort(400)
        with db.connect() as connection:
            document = connection.execute("SELECT * FROM documents WHERE id=? AND organization_id=?", (document_id, organization_id())).fetchone()
            if not document or document["status"] != "generated":
                abort(409)
            envelope = f"manual-{document_id}-{secrets.token_hex(6)}"
            now = utc_now()
            connection.execute("UPDATE documents SET status='sent',recipient_email=?,provider='manual',provider_envelope_id=?,sent_at=?,updated_at=? WHERE id=?", (email, envelope, now, now, document_id))
            updated = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            record_event(connection, updated, "sent", actor_name=g.user["display_name"], actor_email=email, evidence={"artifact_sha256": document["artifact_sha256"], "ip": request.remote_addr or ""})
        audit("document.sent_recorded", "document", document_id, {"recipient": email})
        flash("Sent state recorded. This local provider did not transmit email automatically.", "good")
        return redirect(url_for("document_detail", document_id=document_id))

    @app.post("/documents/<int:document_id>/signed-file")
    @roles_required("owner", "manager", "agent")
    def upload_signed_document(document_id: int):
        require_csrf()
        if request.form.get("confirmed") != "yes":
            abort(400)
        upload = request.files.get("file")
        if not upload or not upload.filename:
            abort(400)
        document_rows = rows("SELECT * FROM documents WHERE id=? AND organization_id=?", (document_id, organization_id()))
        if not document_rows or document_rows[0]["status"] not in {"sent", "delivered", "viewed", "signed"}:
            abort(409)
        folder = db.path.parent / "documents" / str(organization_id()) / str(document_id)
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"signed-{secrets.token_hex(6)}.pdf"
        upload.save(final)
        try:
            digest, pages, size = validate_uploaded_pdf(final)
        except ValueError as exc:
            final.unlink(missing_ok=True)
            flash(str(exc), "error")
            return redirect(url_for("document_detail", document_id=document_id))
        relative = final.relative_to(db.path.parent)
        now = utc_now()
        with db.connect() as connection:
            connection.execute("""INSERT INTO document_files(organization_id,document_id,kind,file_path,original_name,size_bytes,sha256,uploaded_by,created_at)
                VALUES (?,?,'signed',?,?,?,?,?,?)""", (organization_id(), document_id, str(relative), secure_filename(upload.filename), size, digest, g.user.get("id"), now))
            connection.execute("UPDATE documents SET status='completed',completed_at=?,updated_at=? WHERE id=?", (now, now, document_id))
            updated = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            evidence = {"sha256": digest, "pages": pages, "size_bytes": size, "ip": request.remote_addr or ""}
            record_event(connection, updated, "signed", actor_name=g.user["display_name"], evidence=evidence)
            record_event(connection, updated, "completed", actor_name=g.user["display_name"], evidence=evidence)
        audit("document.signed_upload", "document", document_id, {"sha256": digest, "pages": pages})
        flash("Signed PDF validated, hashed, and stored with completion evidence.", "good")
        return redirect(url_for("document_detail", document_id=document_id))

    @app.get("/documents/<int:document_id>/files/<int:file_id>")
    def download_document_file(document_id: int, file_id: int):
        file_rows = rows("SELECT * FROM document_files WHERE id=? AND document_id=? AND organization_id=?", (file_id, document_id, organization_id()))
        if not file_rows:
            abort(404)
        base = db.path.parent.resolve()
        path = (base / file_rows[0]["file_path"]).resolve()
        try:
            path.relative_to(base / "documents")
        except ValueError:
            abort(403)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=file_rows[0]["original_name"])

    @app.post("/webhooks/esign/<provider>")
    def esign_webhook(provider: str):
        secret = os.getenv("CRM_ESIGN_WEBHOOK_SECRET", "")
        if not secret:
            return jsonify(error="E-sign webhook is not configured"), 503
        raw = request.get_data(cache=True)
        supplied = request.headers.get("X-CRM-Signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return jsonify(error="Invalid signature"), 401
        payload = request.get_json(silent=True) or {}
        required = ("event_id", "envelope_id", "event_type")
        if any(not payload.get(key) for key in required):
            return jsonify(error="Missing event fields"), 400
        allowed = {"delivered", "viewed", "signed", "declined", "expired", "completed"}
        if payload["event_type"] not in allowed:
            return jsonify(error="Unsupported event"), 400
        org_id = int(os.getenv("CRM_ESIGN_ORGANIZATION_ID", "1"))
        with db.connect() as connection:
            document = connection.execute("SELECT * FROM documents WHERE organization_id=? AND provider=? AND provider_envelope_id=?", (org_id, provider, payload["envelope_id"])).fetchone()
            if not document:
                return jsonify(error="Envelope not found"), 404
            try:
                connection.execute("""INSERT INTO esign_webhook_events(organization_id,provider,provider_event_id,envelope_id,event_type,signature_valid,payload_sha256,status,received_at)
                    VALUES (?,?,?,?,?,1,?,'accepted',?)""", (org_id, provider, payload["event_id"], payload["envelope_id"], payload["event_type"], hashlib.sha256(raw).hexdigest(), utc_now()))
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    return jsonify(ok=True, duplicate=True)
                raise
            now = payload.get("occurred_at") or utc_now()
            connection.execute("UPDATE documents SET status=?,updated_at=?,completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END WHERE id=?", (payload["event_type"], now, payload["event_type"], now, document["id"]))
            record_event(connection, document, payload["event_type"], actor_name=payload.get("actor_name", ""), actor_email=payload.get("actor_email", ""), provider=provider, provider_event_id=payload["event_id"], evidence={"payload_sha256": hashlib.sha256(raw).hexdigest()}, occurred_at=now)
        return jsonify(ok=True)
