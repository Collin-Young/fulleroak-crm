"""Phase 12: scoped API keys and signed, idempotent lead webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Response, abort, flash, g, jsonify, make_response, redirect, render_template_string, request, url_for

from crm_db import utc_now
from lead_pipeline import extract_references_from_text, sanitize_owner_name
from vumber_client import _protect, _unprotect


STYLE12 = """
.secret{font:14px ui-monospace,monospace;background:#18201e;color:white;padding:14px;border-radius:9px;overflow-wrap:anywhere}.endpoint{border-left:4px solid var(--accent)}.api-code{white-space:pre-wrap;background:#f7f7f3;border:1px solid var(--line);padding:12px;border-radius:8px;overflow:auto}.ticket-row{padding:12px 0;border-bottom:1px solid var(--line)}
"""
SETTINGS = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>API & webhooks · CRM</title><style>{{style}}{{style12}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1>API & inbound webhooks</h1><div class="sub">Scoped, revocable, idempotent integration access</div></div></header>
{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<div class="status-note">Secrets are displayed once. API keys belong only in the Authorization header. Webhook signatures expire after five minutes.</div>
<div class="grid"><section class="stack"><div class="panel"><h2>API keys</h2>{% for k in keys %}<div class="ticket-row"><div class="top"><div><b>{{k.name}}</b><div class="muted">{{k.key_prefix}}… · {{k.scopes_json}} · last used {{k.last_used_at or 'never'}}</div></div><span class="pill">{{'active' if k.active else 'revoked'}}</span></div>{% if k.active %}<form method="post" action="{{url_for('revoke_api_key',key_id=k.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Revoke</button></form>{% endif %}</div>{% else %}<div class="muted">No API keys.</div>{% endfor %}</div>
<div class="panel"><h2>Webhook endpoints</h2>{% for e in endpoints %}<div class="ticket-row endpoint"><b>{{e.name}}</b><div class="muted">POST {{url_for('lead_webhook',slug=e.slug,_external=True)}}</div><span class="pill">{{'active' if e.active else 'disabled'}}</span>{% if e.active %}<form method="post" action="{{url_for('toggle_webhook_endpoint',endpoint_id=e.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">Disable</button></form>{% endif %}</div>{% else %}<div class="muted">No endpoints.</div>{% endfor %}</div></section>
<aside class="stack"><form class="panel" method="post" action="{{url_for('create_api_key')}}"><h2>Create API key</h2><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Name<input name="name" required maxlength="100"></label><label class="check"><input type="checkbox" name="scopes" value="read:leads" checked>Read leads</label><label class="check"><input type="checkbox" name="scopes" value="write:leads">Create/update leads</label><button class="btn">Create and show once</button></form>
<form class="panel" method="post" action="{{url_for('create_webhook_endpoint')}}"><h2>Create signed webhook</h2><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Name<input name="name" required maxlength="100"></label><button class="btn">Create and show secret once</button></form>
<div class="panel"><h2>API example</h2><pre class="api-code">GET /api/v1/leads?limit=50
Authorization: Bearer YOUR_KEY

POST /api/v1/leads
Authorization: Bearer YOUR_KEY
Idempotency-Key: unique-event-id
Content-Type: application/json</pre></div></aside></div>
<div class="panel"><h2>Recent integration traffic</h2>{% for x in logs %}<div class="ticket-row"><b>{{x.kind}} · {{x.status}}</b><div class="muted">{{x.label}} · {{x.at}}</div></div>{% else %}<div class="muted">No traffic yet.</div>{% endfor %}</div></main></body></html>
"""
SECRET = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Copy secret · CRM</title><style>{{style}}{{style12}}</style></head><body><main class="shell" style="max-width:780px"><h1>{{title}}</h1><div class="status-note error"><b>Copy this now.</b> It will not be displayed again.</div><div class="secret">{{secret}}</div>{% if endpoint %}<pre class="api-code">signed = timestamp + "." + raw_request_body
signature = HMAC-SHA256(secret, signed)

X-CRM-Event-ID: unique-event-id
X-CRM-Timestamp: current Unix timestamp
X-CRM-Signature: signature</pre>{% endif %}<a class="btn" href="{{url_for('integration_settings')}}">I saved it</a></main></body></html>
"""


def register_phase12(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def error(message, status, code):
        return jsonify({"error": {"code": code, "message": message}}), status

    def parse_phone(value):
        digits = re.sub(r"\D", "", value or "")
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError("phone must contain a valid 10-digit US number")
        return digits, f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    def apply_lead(org_id, payload):
        digits, display = parse_phone(str(payload.get("phone") or ""))
        lead_id = db.ensure_lead(digits, display, org_id)
        name = sanitize_owner_name(str(payload.get("owner_name") or ""))[:240]
        reference = ", ".join(extract_references_from_text(str(payload.get("reference_number") or "")))[:240]
        source = str(payload.get("lead_source") or "API").strip()[:120]
        deal_style = str(payload.get("deal_style") or "").strip()[:240]
        notes = str(payload.get("notes") or "").strip()[:4000]
        with db.connect() as connection:
            connection.execute("""UPDATE leads SET
                owner_name=CASE WHEN ?!='' THEN ? ELSE owner_name END,
                reference_number=CASE WHEN ?!='' THEN ? ELSE reference_number END,
                lead_source=CASE WHEN ?!='' THEN ? ELSE lead_source END,
                deal_style=CASE WHEN ?!='' THEN ? ELSE deal_style END,
                notes=CASE WHEN ?='' THEN notes WHEN notes='' THEN ? ELSE notes||'\n\n'||? END,
                updated_at=? WHERE id=? AND organization_id=?""",
                (name, name, reference, reference, source, source, deal_style, deal_style,
                 notes, notes, notes, utc_now(), lead_id, org_id))
            lead = dict(connection.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        db.sync_contact_for_lead(lead_id)
        return lead

    def api_key_required(scope):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                header = request.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return error("Bearer API key required.", 401, "unauthorized")
                digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
                matches = rows("SELECT * FROM api_keys WHERE secret_sha256=? AND active=1", (digest,))
                if not matches:
                    return error("API key is invalid or revoked.", 401, "unauthorized")
                key = matches[0]
                if key["expires_at"] and key["expires_at"] <= utc_now():
                    return error("API key has expired.", 401, "expired_key")
                if scope not in json.loads(key["scopes_json"]):
                    return error("API key lacks the required scope.", 403, "insufficient_scope")
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
                if rows("SELECT COUNT(*) n FROM api_request_log WHERE api_key_id=? AND created_at>=?", (key["id"], cutoff))[0]["n"] >= 60:
                    return error("Rate limit exceeded.", 429, "rate_limited")
                g.api_key, g.api_organization_id = key, int(key["organization_id"])
                response = make_response(view(*args, **kwargs))
                fp = hashlib.sha256(f"{request.remote_addr or ''}|{request.headers.get('User-Agent','')}".encode()).hexdigest()
                with db.connect() as connection:
                    connection.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (utc_now(), key["id"]))
                    connection.execute("INSERT INTO api_request_log(organization_id,api_key_id,method,path,status_code,request_fingerprint,created_at) VALUES (?,?,?,?,?,?,?)",
                                       (key["organization_id"], key["id"], request.method, request.path, response.status_code, fp, utc_now()))
                return response
            return wrapped
        return decorator

    @app.get("/api/v1/leads")
    @api_key_required("read:leads")
    def api_list_leads():
        after = max(0, request.args.get("after", type=int) or 0)
        limit = min(100, max(1, request.args.get("limit", type=int) or 50))
        items = rows("""SELECT id,phone_display,owner_name,reference_number,status,follow_up_score,
            distress_signal,seller_motivation,price_expectation,lead_source,latest_activity,updated_at
            FROM leads WHERE organization_id=? AND id>? ORDER BY id LIMIT ?""",
            (g.api_organization_id, after, limit + 1))
        more = len(items) > limit
        items = items[:limit]
        return jsonify({"data": items, "next_after": items[-1]["id"] if more and items else None})

    @app.post("/api/v1/leads")
    @api_key_required("write:leads")
    def api_create_lead():
        idem = request.headers.get("Idempotency-Key", "").strip()
        if not 8 <= len(idem) <= 160:
            return error("Idempotency-Key must be 8 to 160 characters.", 400, "idempotency_required")
        request_hash = hashlib.sha256(request.get_data(cache=True)).hexdigest()
        existing = rows("SELECT * FROM api_idempotency WHERE api_key_id=? AND idempotency_key=?",
                        (g.api_key["id"], idem))
        if existing:
            record = existing[0]
            if record["request_sha256"] != request_hash:
                return error("Idempotency key was used with a different payload.", 409, "idempotency_conflict")
            return Response(record["response_json"], record["status_code"], mimetype="application/json",
                            headers={"Idempotency-Replayed": "true"})
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("A JSON object is required.", 400, "invalid_json")
        try:
            lead = apply_lead(g.api_organization_id, payload)
        except ValueError as exc:
            return error(str(exc), 422, "validation_error")
        body = json.dumps({"data": {"id": lead["id"], "phone_display": lead["phone_display"],
                                   "owner_name": lead["owner_name"], "reference_number": lead["reference_number"],
                                   "status": lead["status"]}}, sort_keys=True)
        with db.connect() as connection:
            connection.execute("""INSERT INTO api_idempotency(
                organization_id,api_key_id,idempotency_key,request_sha256,status_code,response_json,
                resource_type,resource_id,created_at) VALUES (?,?,?,?,200,?,'lead',?,?)""",
                (g.api_organization_id, g.api_key["id"], idem, request_hash, body, lead["id"], utc_now()))
        return Response(body, mimetype="application/json")

    @app.post("/webhooks/leads/<slug>")
    def lead_webhook(slug):
        endpoints = rows("SELECT * FROM inbound_webhook_endpoints WHERE slug=? AND active=1", (slug,))
        if not endpoints:
            return error("Webhook endpoint not found.", 404, "not_found")
        endpoint = endpoints[0]
        raw = request.get_data(cache=True)
        event_id = request.headers.get("X-CRM-Event-ID", "").strip()[:200]
        timestamp = request.headers.get("X-CRM-Timestamp", "").strip()
        supplied = request.headers.get("X-CRM-Signature", "").strip().lower()
        if not event_id or not timestamp or not supplied:
            return error("Event ID, timestamp, and signature are required.", 401, "signature_required")
        prior = rows("SELECT response_json FROM inbound_webhook_deliveries WHERE endpoint_id=? AND external_event_id=?",
                     (endpoint["id"], event_id))
        if prior:
            return Response(prior[0]["response_json"], 200, mimetype="application/json",
                            headers={"Idempotency-Replayed": "true"})
        try:
            sent = datetime.fromtimestamp(int(timestamp), timezone.utc)
            if abs((datetime.now(timezone.utc) - sent).total_seconds()) > 300:
                raise ValueError
            expected = hmac.new(_unprotect(endpoint["secret_encrypted"]),
                                timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            valid = hmac.compare_digest(expected, supplied)
        except (ValueError, OSError):
            valid = False
        payload_hash = hashlib.sha256(raw).hexdigest()
        if not valid:
            with db.connect() as connection:
                connection.execute("""INSERT INTO inbound_webhook_deliveries(
                    organization_id,endpoint_id,external_event_id,payload_sha256,signature_valid,status,response_json,received_at)
                    VALUES (?,?,?,?,0,'rejected',?,?)""",
                    (endpoint["organization_id"], endpoint["id"], event_id, payload_hash,
                     '{"error":{"code":"invalid_signature"}}', utc_now()))
            return error("Webhook signature is invalid or expired.", 401, "invalid_signature")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("A JSON object is required.", 400, "invalid_json")
        try:
            lead = apply_lead(int(endpoint["organization_id"]), payload)
            body, delivery_status, code = json.dumps({"data": {"lead_id": lead["id"], "accepted": True}}), "accepted", 200
        except ValueError as exc:
            body = json.dumps({"error": {"code": "validation_error", "message": str(exc)}})
            delivery_status, code = "failed", 422
        try:
            with db.connect() as connection:
                connection.execute("""INSERT INTO inbound_webhook_deliveries(
                    organization_id,endpoint_id,external_event_id,payload_sha256,signature_valid,status,response_json,received_at)
                    VALUES (?,?,?,?,1,?,?,?)""",
                    (endpoint["organization_id"], endpoint["id"], event_id, payload_hash,
                     delivery_status, body, utc_now()))
        except sqlite3.IntegrityError:
            previous = rows("SELECT response_json FROM inbound_webhook_deliveries WHERE endpoint_id=? AND external_event_id=?",
                            (endpoint["id"], event_id))[0]
            return Response(previous["response_json"], 200, mimetype="application/json",
                            headers={"Idempotency-Replayed": "true"})
        return Response(body, code, mimetype="application/json")

    @app.get("/settings/integrations")
    @roles_required("owner", "manager")
    def integration_settings():
        org = organization_id()
        keys = rows("SELECT * FROM api_keys WHERE organization_id=? ORDER BY id DESC", (org,))
        endpoints = rows("SELECT id,name,slug,active,created_at FROM inbound_webhook_endpoints WHERE organization_id=? ORDER BY id DESC", (org,))
        logs = [{"kind": "API", "status": x["status_code"], "label": f"{x['method']} {x['path']}", "at": x["created_at"]}
                for x in rows("SELECT * FROM api_request_log WHERE organization_id=? ORDER BY id DESC LIMIT 20", (org,))]
        logs += [{"kind": "Webhook", "status": x["status"], "label": x["external_event_id"], "at": x["received_at"]}
                 for x in rows("SELECT * FROM inbound_webhook_deliveries WHERE organization_id=? ORDER BY id DESC LIMIT 20", (org,))]
        logs.sort(key=lambda x: x["at"], reverse=True)
        return render_template_string(SETTINGS, style=style, style12=STYLE12, keys=keys,
                                      endpoints=endpoints, logs=logs[:30])

    @app.post("/settings/integrations/api-keys")
    @roles_required("owner", "manager")
    def create_api_key():
        require_csrf()
        name = request.form.get("name", "").strip()[:100]
        scopes = sorted({x for x in request.form.getlist("scopes") if x in {"read:leads", "write:leads"}})
        if not name or not scopes:
            flash("Name the key and select at least one scope.", "error")
            return redirect(url_for("integration_settings"))
        raw_key = "crm_" + secrets.token_urlsafe(32)
        try:
            with db.connect() as connection:
                cursor = connection.execute("""INSERT INTO api_keys(
                    organization_id,name,key_prefix,secret_sha256,scopes_json,created_by,created_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (organization_id(), name, raw_key[:12], hashlib.sha256(raw_key.encode()).hexdigest(),
                     json.dumps(scopes), g.user.get("id"), utc_now()))
                key_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            flash("That key name is already in use.", "error")
            return redirect(url_for("integration_settings"))
        audit("integration.api_key_created", "api_key", key_id, {"name": name, "scopes": scopes})
        return render_template_string(SECRET, style=style, style12=STYLE12, title="New API key",
                                      secret=raw_key, endpoint=False)

    @app.post("/settings/integrations/api-keys/<int:key_id>/revoke")
    @roles_required("owner", "manager")
    def revoke_api_key(key_id):
        require_csrf()
        with db.connect() as connection:
            cursor = connection.execute("""UPDATE api_keys SET active=0,revoked_at=?
                WHERE id=? AND organization_id=? AND active=1""", (utc_now(), key_id, organization_id()))
            if not cursor.rowcount:
                abort(404)
        audit("integration.api_key_revoked", "api_key", key_id)
        return redirect(url_for("integration_settings"))

    @app.post("/settings/integrations/webhooks")
    @roles_required("owner", "manager")
    def create_webhook_endpoint():
        require_csrf()
        name = request.form.get("name", "").strip()[:100]
        if not name:
            flash("Endpoint name is required.", "error")
            return redirect(url_for("integration_settings"))
        signing_secret = secrets.token_bytes(32)
        slug = secrets.token_urlsafe(18)
        try:
            with db.connect() as connection:
                cursor = connection.execute("""INSERT INTO inbound_webhook_endpoints(
                    organization_id,name,slug,secret_encrypted,created_by,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (organization_id(), name, slug, _protect(signing_secret), g.user.get("id"), utc_now(), utc_now()))
                endpoint_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            flash("That endpoint name is already in use.", "error")
            return redirect(url_for("integration_settings"))
        audit("integration.webhook_created", "webhook_endpoint", endpoint_id, {"name": name})
        return render_template_string(SECRET, style=style, style12=STYLE12,
                                      title="New webhook signing secret",
                                      secret=signing_secret.hex(), endpoint=True)

    @app.post("/settings/integrations/webhooks/<int:endpoint_id>/toggle")
    @roles_required("owner", "manager")
    def toggle_webhook_endpoint(endpoint_id):
        require_csrf()
        with db.connect() as connection:
            cursor = connection.execute("""UPDATE inbound_webhook_endpoints SET active=1-active,updated_at=?
                WHERE id=? AND organization_id=?""", (utc_now(), endpoint_id, organization_id()))
            if not cursor.rowcount:
                abort(404)
        audit("integration.webhook_toggled", "webhook_endpoint", endpoint_id)
        return redirect(url_for("integration_settings"))

