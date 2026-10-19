"""Phase 9 communication providers: number inventory, consent, delivery state."""

from __future__ import annotations

import json

from flask import abort, flash, g, redirect, render_template_string, request, url_for

from crm_db import utc_now


PHASE9_STYLE = """
.consent-card{border-left:5px solid var(--urgent)}.consent-card.ok{border-left-color:var(--accent)}.delivery{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;background:#eceeea;text-transform:capitalize}.delivery.delivered{background:#dceee5;color:#14583f}.delivery.failed,.delivery.undelivered{background:#eee1df;color:#7c302c}.delivery.sent{background:#e0e7f5;color:#2a4a7a}
"""

PHASE9_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Communication · CRM</title><style>{{style}}{{phase9_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Communication settings</h1><div class="sub">Number inventory, consent and suppression, and delivery state</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<div class="grid"><section class="stack">
<div class="panel"><h2>Number inventory</h2><div class="table-wrap"><table><thead><tr><th>Number</th><th>Label</th><th>Provider</th><th>Kind</th><th>Status</th><th></th></tr></thead><tbody>{% for number in numbers %}<tr><td><b>{{number.value}}</b></td><td>{{number.label}}</td><td>{{number.provider}}</td><td>{{number.kind}}</td><td>{{'Active' if number.active else 'Disabled'}}</td><td>{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('toggle_phone_number',number_id=number.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">{{'Disable' if number.active else 'Enable'}}</button></form>{% endif %}</td></tr>{% else %}<tr><td colspan="6" class="empty">Numbers sync from the connected Vumber account. Connect Vumber first.</td></tr>{% endfor %}</tbody></table></div>{% if current_user.role in ('owner','manager') %}<details style="margin-top:10px"><summary class="btn secondary" style="display:inline-block">Add number manually</summary><form method="post" action="{{url_for('add_phone_number')}}" style="margin-top:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><div class="two-col"><label class="field">Number<input name="value" placeholder="+13145550123" required></label><label class="field">Label<input name="label" placeholder="Main line"></label></div><div class="two-col"><label class="field">Kind<select name="kind"><option value="sms">SMS</option><option value="call">Calls</option><option value="both">Both</option></select></label><label class="field">Provider<select name="provider"><option value="vumber">Vumber</option><option value="manual">Manual</option></select></label></div><button class="btn">Add number</button></form></details>{% endif %}</div>
<div class="panel"><h2>Recent delivery state</h2><div class="table-wrap"><table><thead><tr><th>When</th><th>Channel</th><th>Provider</th><th>State</th><th>Reference</th></tr></thead><tbody>{% for event in delivery %}<tr><td>{{event.occurred_at}}</td><td>{{event.channel}}</td><td>{{event.provider}}</td><td><span class="delivery {{event.state}}">{{event.state}}</span></td><td>{{event.provider_message_id or ('communication #' ~ event.communication_id if event.communication_id else '—')}}</td></tr>{% else %}<tr><td colspan="5" class="empty">No delivery events yet.</td></tr>{% endfor %}</tbody></table></div><div class="muted" style="margin-top:8px">Delivery state is provider-reported where available; the local Vumber provider records a sent state without delivery confirmation.</div></div>
</section>
<aside class="stack"><div class="panel"><h2>Consent and suppression</h2><div class="muted">Suppressed leads cannot be texted or called until the suppression is lifted. This does not affect emergency manual actions outside the CRM.</div>{% for item in suppressions %}<div class="consent-card {{'ok' if not item.active else ''}} panel" style="margin-top:10px"><b>Lead #{{item.lead_id}}</b> <span class="pill">{{item.channel}}</span> <span class="pill {{'ok' if not item.active else ''}}">{{'Lifted' if not item.active else 'Suppressed'}}</span><div class="muted">{{item.reason or 'No reason recorded'}} · set {{item.created_at}}</div>{% if current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('toggle_suppression',suppression_id=item.id)}}" style="margin-top:6px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn secondary">{{'Lift suppression' if item.active else 'Re-apply'}}</button></form>{% endif %}</div>{% else %}<div class="muted">No suppressions recorded.</div>{% endfor %}{% if current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('add_suppression')}}" style="margin-top:12px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Lead ID<input type="number" name="lead_id" min="1" required></label><label class="field">Channel<select name="channel"><option value="all">All channels</option><option value="sms">SMS only</option><option value="call">Calls only</option><option value="email">Email only</option></select></label><label class="field">Reason<input name="reason" placeholder="Do not contact request"></label><button class="btn">Add suppression</button></form>{% endif %}</div></aside></div></main></body></html>
"""


def register_phase9(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def is_suppressed(lead_id: int, channel: str) -> bool:
        found = rows(
            """SELECT 1 FROM consent_suppressions
               WHERE organization_id=? AND active=1 AND lead_id=?
                 AND channel IN (?, 'all') LIMIT 1""",
            (organization_id(), lead_id, channel),
        )
        return bool(found)

    def record_delivery(
        connection,
        communication_id: int | None,
        channel: str,
        state: str,
        provider: str = "vumber",
        provider_message_id: str = "",
        detail: dict | None = None,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO delivery_events(
                organization_id,communication_id,provider,channel,provider_message_id,state,detail_json,occurred_at,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (organization_id(), communication_id, provider, channel, provider_message_id,
             state, json.dumps(detail or {}, sort_keys=True), utc_now(), utc_now()),
        )

    app.extensions["communication"] = {
        "is_suppressed": is_suppressed,
        "record_delivery": record_delivery,
    }

    @app.get("/settings/communication")
    def communication_settings():
        numbers = rows(
            "SELECT * FROM phone_numbers WHERE organization_id=? ORDER BY provider,value",
            (organization_id(),),
        )
        suppressions = rows(
            """SELECT * FROM consent_suppressions WHERE organization_id=?
               ORDER BY active DESC, created_at DESC LIMIT 100""",
            (organization_id(),),
        )
        delivery = rows(
            """SELECT * FROM delivery_events WHERE organization_id=?
               ORDER BY occurred_at DESC, id DESC LIMIT 50""",
            (organization_id(),),
        )
        return render_template_string(
            PHASE9_TEMPLATE, style=style, phase9_style=PHASE9_STYLE,
            numbers=numbers, suppressions=suppressions, delivery=delivery,
        )

    @app.post("/settings/communication/numbers")
    @roles_required("owner", "manager")
    def add_phone_number():
        require_csrf()
        value = request.form.get("value", "").strip()
        if not value:
            abort(400)
        kind = request.form.get("kind", "sms")
        if kind not in {"sms", "call", "both"}:
            abort(400)
        provider = request.form.get("provider", "vumber")
        if provider not in {"vumber", "manual"}:
            abort(400)
        now = utc_now()
        with db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO phone_numbers(
                    organization_id,provider,value,label,kind,active,created_at,updated_at
                ) VALUES (?,?,?,?,?,1,?,?)""",
                (organization_id(), provider, value,
                 request.form.get("label", "").strip()[:120], kind, now, now),
            )
        audit("communication.number_add", "phone_number", None, {"value": value, "kind": kind})
        flash("Number added to the inventory.", "good")
        return redirect(url_for("communication_settings"))

    @app.post("/settings/communication/numbers/<int:number_id>/toggle")
    @roles_required("owner", "manager")
    def toggle_phone_number(number_id: int):
        require_csrf()
        with db.connect() as connection:
            cursor = connection.execute(
                """UPDATE phone_numbers SET active=1-active,updated_at=?
                   WHERE id=? AND organization_id=?""",
                (utc_now(), number_id, organization_id()),
            )
            if not cursor.rowcount:
                abort(404)
        audit("communication.number_toggle", "phone_number", number_id)
        flash("Number status updated.", "good")
        return redirect(url_for("communication_settings"))

    @app.post("/settings/communication/suppressions")
    @roles_required("owner", "manager", "agent")
    def add_suppression():
        require_csrf()
        lead_id = request.form.get("lead_id", "")
        channel = request.form.get("channel", "all")
        if not lead_id.isdigit() or channel not in {"sms", "call", "email", "all"}:
            abort(400)
        if not rows(
            "SELECT 1 FROM leads WHERE id=? AND organization_id=?",
            (int(lead_id), organization_id()),
        ):
            abort(404)
        now = utc_now()
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO consent_suppressions(
                    organization_id,lead_id,contact_id,channel,reason,active,created_by,created_at,updated_at
                ) SELECT ?,id,
                    (SELECT id FROM contacts WHERE organization_id=? AND lead_id=? LIMIT 1),
                    ?,?,1,?,?,?
                   FROM leads WHERE id=? AND organization_id=?""",
                (organization_id(), organization_id(), int(lead_id), channel,
                 request.form.get("reason", "").strip()[:300], g.user.get("id"), now, now,
                 int(lead_id), organization_id()),
            )
        audit("communication.suppress", "lead", int(lead_id), {"channel": channel})
        flash("Suppression recorded. The lead can no longer be contacted on that channel.", "good")
        return redirect(url_for("communication_settings"))

    @app.post("/settings/communication/suppressions/<int:suppression_id>/toggle")
    @roles_required("owner", "manager", "agent")
    def toggle_suppression(suppression_id: int):
        require_csrf()
        with db.connect() as connection:
            cursor = connection.execute(
                """UPDATE consent_suppressions SET active=1-active,updated_at=?
                   WHERE id=? AND organization_id=?""",
                (utc_now(), suppression_id, organization_id()),
            )
            if not cursor.rowcount:
                abort(404)
        audit("communication.suppression_toggle", "consent_suppression", suppression_id)
        flash("Suppression status updated.", "good")
        return redirect(url_for("communication_settings"))
