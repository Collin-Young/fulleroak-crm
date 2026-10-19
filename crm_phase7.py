"""Phase 7 calendars, availability windows, and appointment scheduling."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from flask import abort, flash, g, jsonify, redirect, render_template_string, request, url_for

from crm_db import utc_now


PHASE7_STYLE = """
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}.cal-grid .dow{color:var(--muted);font-size:12px;text-transform:uppercase;text-align:center;padding:6px 0}.cal-day{background:#fff;border:1px solid var(--line);border-radius:10px;min-height:96px;padding:8px;font-size:13px}.cal-day.other{opacity:.45;background:#faf9f5}.cal-day.today{border-color:var(--accent);box-shadow:0 0 0 2px #235d4d22}.cal-day .daynum{font-weight:700;margin-bottom:6px}.appt{display:block;border-left:4px solid var(--accent);background:#f0f6f2;border-radius:6px;padding:5px 7px;margin:5px 0;text-decoration:none;overflow-wrap:anywhere}.appt.cancelled,.appt.no_show{border-left-color:var(--urgent);background:#fdf1ef;opacity:.75}.appt.completed{border-left-color:#247047}.appt time{display:block;color:var(--muted);font-size:11px}.avail-chip{display:inline-block;background:#edf2ef;border-radius:6px;padding:4px 8px;margin:3px;font-size:12px}.status-pill{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;background:#eceeea;text-transform:capitalize}.status-pill.scheduled{background:#dceee5;color:#14583f}.status-pill.cancelled,.status-pill.no_show{background:#eee1df;color:#7c302c}.status-pill.completed{background:#dceee5;color:#14583f}
"""

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

CALENDAR_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Calendar · CRM</title><style>{{style}}{{phase7_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Calendar</h1><div class="sub">{{month_label}} · {{counts.scheduled}} scheduled · {{counts.completed}} completed</div></div><div class="top-actions"><a class="btn secondary" href="{{url_for('calendar',year=prev.year,month=prev.month)}}">← {{prev.strftime('%B')}}</a><a class="btn secondary" href="{{url_for('calendar',year=next.year,month=next.month)}}">{{next.strftime('%B')}} →</a>{% if current_user.role in ('owner','manager','agent') %}<a class="btn" href="{{url_for('new_appointment')}}">+ Appointment</a>{% endif %}</div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<div class="toolbar"><form method="get" style="display:flex;gap:10px;flex-wrap:wrap"><select name="calendar"><option value="">All calendars</option>{% for cal in calendars %}<option value="{{cal.id}}" {% if selected==cal.id|string %}selected{% endif %}>{{cal.name}}</option>{% endfor %}</select><select name="status"><option value="">All statuses</option>{% for s in ('scheduled','completed','cancelled','no_show') %}<option value="{{s}}" {% if status==s %}selected{% endif %}>{{s|replace('_',' ')|title}}</option>{% endfor %}</select><button class="btn">Filter</button></form></div>
<div class="cal-grid">{% for label in weekday_labels %}<div class="dow">{{label[:3]}}</div>{% endfor %}{% for cell in cells %}<div class="cal-day {{'other' if not cell.in_month else ''}} {{'today' if cell.is_today else ''}}"><div class="daynum">{{cell.day}}</div>{% for appt in cell.appointments %}<a class="appt {{appt.status}}" href="{{url_for('appointment_detail',appointment_id=appt.id)}}"><time>{{appt.start_label}}</time>{{appt.title}}</a>{% endfor %}</div>{% endfor %}</div>
<aside style="margin-top:22px"><div class="panel"><h2>Calendars</h2>{% for cal in calendars %}<div style="padding:8px 0;border-bottom:1px solid var(--line)"><b>{{cal.name}}</b> <span class="pill">{{cal.kind}}</span><div class="muted">{% for chip in cal.availability %}<span class="avail-chip">{{chip}}</span>{% else %}<span class="muted">No availability windows</span>{% endfor %}</div></div>{% endfor %}{% if current_user.role in ('owner','manager') %}<details style="margin-top:12px"><summary class="btn secondary" style="display:inline-block">Edit availability</summary><form method="post" action="{{url_for('update_availability')}}" style="margin-top:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Calendar<select name="calendar_id" required>{% for cal in calendars %}<option value="{{cal.id}}">{{cal.name}}</option>{% endfor %}</select></label><label class="field">Weekday<select name="weekday">{% for label in weekday_labels %}<option value="{{loop.index0}}">{{label}}</option>{% endfor %}</select></label><div class="two-col"><label class="field">Start<input type="time" name="start_time" value="09:00" required></label><label class="field">End<input type="time" name="end_time" value="17:00" required></label></div><button class="btn">Add window</button></form></details>{% endif %}</div></aside></main></body></html>
"""

APPOINTMENT_FORM_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{'Edit' if appointment else 'New'}} appointment · CRM</title><style>{{style}}{{phase7_style}}</style></head><body><main class="shell" style="max-width:760px"><a class="muted" href="{{url_for('calendar')}}">← Calendar</a><h1 style="margin-top:8px">{{'Edit appointment' if appointment else 'New appointment'}}</h1>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<form class="panel wide-form" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Calendar<select name="calendar_id" required>{% for cal in calendars %}<option value="{{cal.id}}" {% if appointment and appointment.calendar_id==cal.id %}selected{% endif %}>{{cal.name}}</option>{% endfor %}</select></label><label class="field">Title<input name="title" value="{{appointment.title if appointment else ''}}" required placeholder="Property walkthrough with seller"></label><div class="two-col"><label class="field">Date<input type="date" name="date" value="{{appointment.starts_at[:10] if appointment else today}}" required></label><div class="two-col"><label class="field">Start<input type="time" name="start_time" value="{{appointment.starts_at[11:16] if appointment else '10:00'}}" required></label><label class="field">End<input type="time" name="end_time" value="{{appointment.ends_at[11:16] if appointment else '11:00'}}" required></label></div></div><label class="field">Link to lead (optional)<select name="lead_id"><option value="">None</option>{% for lead in leads %}<option value="{{lead.id}}" {% if appointment and appointment.lead_id==lead.id %}selected{% endif %}>{{lead.owner_name or lead.phone_display}}{% if lead.reference_number %} · {{lead.reference_number}}{% endif %}</option>{% endfor %}</select></label><label class="field">Location<input name="location" value="{{appointment.location if appointment else ''}}" placeholder="Property address or phone call"></label><label class="field">Notes<textarea name="notes">{{appointment.notes if appointment else ''}}</textarea></label><button class="btn">{{'Save changes' if appointment else 'Create appointment'}}</button></form></main></body></html>
"""

APPOINTMENT_DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{appointment.title}} · Appointment</title><style>{{style}}{{phase7_style}}</style></head><body><main class="shell" style="max-width:820px"><header class="top"><div><a class="muted" href="{{url_for('calendar')}}">← Calendar</a><h1 style="margin-top:8px">{{appointment.title}}</h1><div class="sub">{{appointment.starts_at}} → {{appointment.ends_at}} ({{appointment.timezone}})</div></div><span class="status-pill {{appointment.status}}">{{appointment.status|replace('_',' ')}}</span></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<div class="grid"><section class="stack"><div class="panel"><div class="facts"><div class="fact"><b>Calendar</b>{{calendar_name}}</div><div class="fact"><b>Location</b>{{appointment.location or 'Not set'}}</div><div class="fact"><b>Booked by</b>{{appointment.booked_by or 'Internal'}}</div></div>{% if appointment.notes %}<div style="margin-top:12px"><b>Notes</b><div class="summary-text">{{appointment.notes}}</div></div>{% endif %}{% if lead_id %}<div class="actions"><a class="btn secondary" href="{{url_for('lead_detail',lead_id=appointment.lead_id)}}">Open linked lead</a>{% endif %}{% if appointment.opportunity_id %}<a class="btn secondary" href="{{url_for('deal_detail',opportunity_id=appointment.opportunity_id)}}">Open deal</a>{% endif %}</div></div></section><aside class="stack"><section class="panel"><h2>Actions</h2>{% if current_user.role in ('owner','manager','agent') %}<a class="btn secondary" href="{{url_for('edit_appointment',appointment_id=appointment.id)}}">Edit</a>{% if appointment.status=='scheduled' %}<form method="post" action="{{url_for('appointment_status',appointment_id=appointment.id)}}" style="margin-top:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Set status<select name="status">{% for s in ('completed','cancelled','no_show') %}<option value="{{s}}">{{s|replace('_',' ')|title}}</option>{% endfor %}</select></label><button class="btn">Update</button></form>{% elif appointment.status!='scheduled' %}<form method="post" action="{{url_for('appointment_status',appointment_id=appointment.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="status" value="scheduled"><button class="btn secondary">Reschedule to scheduled</button></form>{% endif %}{% endif %}</section></aside></div></main></body></html>
"""


def _parse_minutes(value: str) -> int:
    try:
        hours, minutes = value.split(":")
        total = int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        abort(400)
    if not 0 <= total <= 1440:
        abort(400)
    return total


def _minutes_label(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _combine(date_text: str, time_text: str) -> str:
    try:
        parsed = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    except ValueError:
        abort(400)
    return parsed.strftime("%Y-%m-%dT%H:%M") + ":00+00:00"


def register_phase7(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def month_bounds(year: int, month: int):
        first = date(year, month, 1)
        following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return first, following - timedelta(days=1)

    def load_appointments(start: str, end: str, calendar_id: str, status: str):
        query = """SELECT a.*,c.name calendar_name,l.owner_name,l.phone_display,l.reference_number
            FROM appointments a JOIN calendars c ON c.id=a.calendar_id
            LEFT JOIN leads l ON l.id=a.lead_id
            WHERE a.organization_id=? AND a.starts_at<? AND a.ends_at>=?"""
        params = [organization_id(), end, start]
        if calendar_id:
            query += " AND a.calendar_id=?"
            params.append(int(calendar_id))
        if status:
            query += " AND a.status=?"
            params.append(status)
        query += " ORDER BY a.starts_at"
        return rows(query, tuple(params))

    @app.get("/calendar")
    def calendar():
        today = date.today()
        try:
            year = int(request.args.get("year", today.year))
            month = int(request.args.get("month", today.month))
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            abort(400)
        selected = request.args.get("calendar", "")
        status = request.args.get("status", "")
        first, last = month_bounds(year, month)
        grid_start = first - timedelta(days=first.weekday())
        grid_end = last + timedelta(days=6 - last.weekday())
        appointments = load_appointments(
            grid_start.isoformat() + "T00:00:00", grid_end.isoformat() + "T23:59:59", selected, status
        )
        by_day = {}
        for appointment in appointments:
            by_day.setdefault(appointment["starts_at"][:10], []).append(appointment)
        cells = []
        cursor_day = grid_start
        while cursor_day <= grid_end:
            key = cursor_day.isoformat()
            cells.append({
                "day": cursor_day.day,
                "in_month": cursor_day.month == month,
                "is_today": cursor_day == today,
                "appointments": by_day.get(key, []),
            })
            cursor_day += timedelta(days=1)
        calendars = rows("SELECT * FROM calendars WHERE organization_id=? AND active=1 ORDER BY name", (organization_id(),))
        for item in calendars:
            windows = rows(
                "SELECT weekday,start_minute,end_minute FROM calendar_availability WHERE calendar_id=? ORDER BY weekday,start_minute",
                (item["id"],),
            )
            item["availability"] = [
                f"{WEEKDAYS[w['weekday']][:3]} {_minutes_label(w['start_minute'])}–{_minutes_label(w['end_minute'])}"
                for w in windows
            ]
        counts = rows("""SELECT
            SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) scheduled,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed
            FROM appointments WHERE organization_id=?""", (organization_id(),))[0]
        previous_month = date(year, month, 1) - timedelta(days=1)
        return render_template_string(
            CALENDAR_TEMPLATE, style=style, phase7_style=PHASE7_STYLE,
            cells=cells, weekday_labels=WEEKDAYS, calendars=calendars,
            selected=selected, status=status, counts=counts,
            month_label=first.strftime("%B %Y"),
            prev=previous_month, next=(last + timedelta(days=1)),
        )

    @app.route("/calendar/appointments/new", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def new_appointment():
        calendars = rows("SELECT * FROM calendars WHERE organization_id=? AND active=1 ORDER BY name", (organization_id(),))
        leads = rows("""SELECT id,owner_name,phone_display,reference_number FROM leads
            WHERE organization_id=? ORDER BY latest_activity DESC LIMIT 200""", (organization_id(),))
        if request.method == "POST":
            require_csrf()
            calendar_id = request.form.get("calendar_id", "")
            if not calendar_id.isdigit() or not rows(
                "SELECT 1 FROM calendars WHERE id=? AND organization_id=?", (int(calendar_id), organization_id())
            ):
                abort(400)
            title = request.form.get("title", "").strip()[:160]
            if not title:
                flash("Title is required.", "error")
            else:
                starts_at = _combine(request.form.get("date", ""), request.form.get("start_time", ""))
                ends_at = _combine(request.form.get("date", ""), request.form.get("end_time", ""))
                if ends_at <= starts_at:
                    flash("End must be after start.", "error")
                else:
                    lead_id = request.form.get("lead_id", "")
                    lead_id = int(lead_id) if lead_id.isdigit() else None
                    contact_id = None
                    opportunity_id = None
                    if lead_id:
                        found = rows(
                            """SELECT c.id contact_id,o.id opportunity_id FROM leads l
                               LEFT JOIN contacts c ON c.lead_id=l.id AND c.organization_id=l.organization_id
                               LEFT JOIN opportunities o ON o.lead_id=l.id AND o.organization_id=l.organization_id
                               WHERE l.id=? AND l.organization_id=?""",
                            (lead_id, organization_id()),
                        )
                        if found:
                            contact_id = found[0]["contact_id"]
                            opportunity_id = found[0]["opportunity_id"]
                    now = utc_now()
                    with db.connect() as connection:
                        cursor = connection.execute(
                            """INSERT INTO appointments(
                                organization_id,calendar_id,lead_id,contact_id,opportunity_id,title,notes,location,
                                starts_at,ends_at,status,booked_by,created_by,created_at,updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,'scheduled',?,?,?,?)""",
                            (organization_id(), int(calendar_id), lead_id, contact_id, opportunity_id,
                             title, request.form.get("notes", "").strip()[:2000],
                             request.form.get("location", "").strip()[:200],
                             starts_at, ends_at, g.user["display_name"], g.user.get("id"), now, now),
                        )
                        appointment_id = cursor.lastrowid
                        if lead_id:
                            connection.execute(
                                """INSERT INTO activity_events(
                                    organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                                    occurred_at,title,body,source_table,source_id,created_at
                                ) VALUES (?,?,?,?, 'appointment_scheduled','system','internal',?,?,?,?,?,?)""",
                                (organization_id(), lead_id, contact_id, opportunity_id, starts_at,
                                 f"Appointment scheduled: {title}", request.form.get("notes", "")[:500],
                                 "appointments", appointment_id, now),
                            )
                    audit("appointment.create", "appointment", appointment_id, {"lead_id": lead_id})
                    flash("Appointment scheduled.", "good")
                    return redirect(url_for("appointment_detail", appointment_id=appointment_id))
        return render_template_string(
            APPOINTMENT_FORM_TEMPLATE, style=style, phase7_style=PHASE7_STYLE,
            calendars=calendars, leads=leads, appointment=None, today=date.today().isoformat(),
        )

    def _appointment_or_404(appointment_id: int):
        found = rows(
            """SELECT a.*,c.name calendar_name FROM appointments a
               JOIN calendars c ON c.id=a.calendar_id
               WHERE a.id=? AND a.organization_id=?""",
            (appointment_id, organization_id()),
        )
        if not found:
            abort(404)
        return found[0]

    @app.route("/calendar/appointments/<int:appointment_id>/edit", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def edit_appointment(appointment_id: int):
        appointment = _appointment_or_404(appointment_id)
        calendars = rows("SELECT * FROM calendars WHERE organization_id=? AND active=1 ORDER BY name", (organization_id(),))
        leads = rows("""SELECT id,owner_name,phone_display,reference_number FROM leads
            WHERE organization_id=? ORDER BY latest_activity DESC LIMIT 200""", (organization_id(),))
        if request.method == "POST":
            require_csrf()
            calendar_id = request.form.get("calendar_id", "")
            if not calendar_id.isdigit() or not rows(
                "SELECT 1 FROM calendars WHERE id=? AND organization_id=?", (int(calendar_id), organization_id())
            ):
                abort(400)
            title = request.form.get("title", "").strip()[:160]
            if not title:
                flash("Title is required.", "error")
            else:
                starts_at = _combine(request.form.get("date", ""), request.form.get("start_time", ""))
                ends_at = _combine(request.form.get("date", ""), request.form.get("end_time", ""))
                if ends_at <= starts_at:
                    flash("End must be after start.", "error")
                else:
                    lead_id = request.form.get("lead_id", "")
                    lead_id = int(lead_id) if lead_id.isdigit() else None
                    with db.connect() as connection:
                        connection.execute(
                            """UPDATE appointments SET calendar_id=?,title=?,notes=?,location=?,starts_at=?,ends_at=?,lead_id=?,updated_at=?
                               WHERE id=? AND organization_id=?""",
                            (int(calendar_id), title, request.form.get("notes", "").strip()[:2000],
                             request.form.get("location", "").strip()[:200], starts_at, ends_at, lead_id,
                             utc_now(), appointment_id, organization_id()),
                        )
                    audit("appointment.update", "appointment", appointment_id)
                    flash("Appointment updated.", "good")
                    return redirect(url_for("appointment_detail", appointment_id=appointment_id))
            appointment = _appointment_or_404(appointment_id)
        return render_template_string(
            APPOINTMENT_FORM_TEMPLATE, style=style, phase7_style=PHASE7_STYLE,
            calendars=calendars, leads=leads, appointment=appointment, today=date.today().isoformat(),
        )

    @app.get("/calendar/appointments/<int:appointment_id>")
    def appointment_detail(appointment_id: int):
        appointment = _appointment_or_404(appointment_id)
        return render_template_string(
            APPOINTMENT_DETAIL_TEMPLATE, style=style, phase7_style=PHASE7_STYLE,
            appointment=appointment, calendar=appointment["calendar_name"],
        )

    @app.post("/calendar/appointments/<int:appointment_id>/status")
    @roles_required("owner", "manager", "agent")
    def appointment_status(appointment_id: int):
        require_csrf()
        appointment = _appointment_or_404(appointment_id)
        status = request.form.get("status", "")
        if status not in {"scheduled", "completed", "cancelled", "no_show"}:
            abort(400)
        with db.connect() as connection:
            connection.execute(
                "UPDATE appointments SET status=?,updated_at=? WHERE id=? AND organization_id=?",
                (status, utc_now(), appointment_id, organization_id()),
            )
            if appointment["lead_id"]:
                connection.execute(
                    """INSERT INTO activity_events(
                        organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                        occurred_at,title,body,source_table,source_id,created_at
                    ) VALUES (?,?,?,?, 'appointment_status','system','internal',?,?,'', 'appointments',?,?)""",
                    (organization_id(), appointment["lead_id"], appointment["contact_id"], appointment["opportunity_id"],
                     utc_now(), f"Appointment {status.replace('_',' ')}: {appointment['title']}",
                     appointment_id, utc_now()),
                )
        audit("appointment.status", "appointment", appointment_id, {"status": status})
        flash(f"Appointment marked {status.replace('_',' ')}.", "good")
        return redirect(url_for("appointment_detail", appointment_id=appointment_id))

    @app.post("/calendar/availability")
    @roles_required("owner", "manager")
    def update_availability():
        require_csrf()
        calendar_id = request.form.get("calendar_id", "")
        if not calendar_id.isdigit() or not rows(
            "SELECT 1 FROM calendars WHERE id=? AND organization_id=?", (int(calendar_id), organization_id())
        ):
            abort(400)
        weekday = request.form.get("weekday", "")
        if not weekday.isdigit() or int(weekday) > 6:
            abort(400)
        start = _parse_minutes(request.form.get("start_time", ""))
        end = _parse_minutes(request.form.get("end_time", ""))
        if end <= start:
            flash("Availability end must be after start.", "error")
            return redirect(url_for("calendar"))
        with db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO calendar_availability(
                    organization_id,calendar_id,weekday,start_minute,end_minute) VALUES (?,?,?,?,?)""",
                (organization_id(), int(calendar_id), int(weekday), start, end),
            )
        audit("calendar.availability", "calendar", int(calendar_id), {"weekday": int(weekday), "start": start, "end": end})
        flash("Availability window added.", "good")
        return redirect(url_for("calendar"))

    @app.get("/api/v1/calendar/available-slots")
    def available_slots():
        calendar_id = request.args.get("calendar_id", "")
        day_text = request.args.get("date", "")
        duration = min(max(int(request.args.get("duration_minutes", "60") or 60), 15), 480)
        if not calendar_id.isdigit() or not day_text:
            abort(400)
        try:
            day = datetime.strptime(day_text, "%Y-%m-%d").date()
        except ValueError:
            abort(400)
        windows = rows(
            """SELECT start_minute,end_minute FROM calendar_availability
               WHERE calendar_id=? AND organization_id=? AND weekday=? ORDER BY start_minute""",
            (int(calendar_id), organization_id(), day.weekday()),
        )
        if not windows:
            return jsonify(date=day_text, slots=[])
        day_start = day.isoformat() + "T00:00:00"
        day_end = day.isoformat() + "T23:59:59"
        booked = rows(
            """SELECT starts_at,ends_at FROM appointments
               WHERE calendar_id=? AND organization_id=? AND status='scheduled'
                 AND starts_at<? AND ends_at>=?""",
            (int(calendar_id), organization_id(), day_end, day_start),
        )
        busy = [(datetime.fromisoformat(b["starts_at"][:19]), datetime.fromisoformat(b["ends_at"][:19])) for b in booked]
        slots = []
        for window in windows:
            slot = datetime(day.year, day.month, day.day) + timedelta(minutes=window["start_minute"])
            window_end = datetime(day.year, day.month, day.day) + timedelta(minutes=window["end_minute"])
            while slot + timedelta(minutes=duration) <= window_end:
                candidate_end = slot + timedelta(minutes=duration)
                if not any(candidate_end > b_start and slot < b_end for b_start, b_end in busy):
                    slots.append(slot.strftime("%Y-%m-%dT%H:%M"))
                slot += timedelta(minutes=30)
        return jsonify(date=day_text, calendar_id=int(calendar_id), slots=slots)
