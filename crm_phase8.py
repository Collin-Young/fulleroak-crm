"""Phase 8 CRM-linked projects, task boards, comments, and land templates."""

from __future__ import annotations

import json

from flask import abort, flash, g, redirect, render_template_string, request, url_for

from crm_db import utc_now


PHASE8_STYLE = """
.proj-card{border-left:5px solid var(--accent)}.proj-card.on_hold{border-left-color:#b4813d}.proj-card.completed{border-left-color:#247047}.proj-card.cancelled{border-left-color:var(--urgent)}.board{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.board h3{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}.board-col{background:#f2f1ea;border-radius:12px;padding:10px;min-height:120px}.task-card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px;margin:8px 0;font-size:14px}.task-card.done{opacity:.6}.task-card .muted{font-size:12px}.task-actions{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}.comment{border-top:1px solid var(--line);padding:10px 0}.comment:first-child{border-top:0}.progress{height:8px;background:#e8ece9;border-radius:99px;overflow:hidden;margin:8px 0}.progress div{height:100%;background:var(--accent)}
"""

PROJECT_STATUSES = ("active", "on_hold", "completed", "cancelled")
TASK_COLUMNS = ("todo", "in_progress", "blocked", "done")
COLUMN_LABELS = {"todo": "To do", "in_progress": "In progress", "blocked": "Blocked", "done": "Done"}

PROJECT_TEMPLATES = {
    "due_diligence": {
        "name": "Due diligence",
        "description": "Standard land due-diligence checklist.",
        "tasks": [
            "Order title search / preliminary title report",
            "Verify legal access to the parcel",
            "Confirm zoning and permitted uses with the county",
            "Order survey or review existing plat",
            "Check for liens, back taxes, and HOA dues",
            "Review flood zone and wetlands indicators",
            "Walk the property or commission photos",
            "Set the maximum allowable offer",
        ],
    },
    "closing": {
        "name": "Closing",
        "description": "Purchase-to-record closing checklist.",
        "tasks": [
            "Prepare purchase agreement for signature",
            "Collect seller W-9 and wiring details",
            "Open title order with the title company",
            "Schedule closing date with the seller",
            "Confirm recording with the county",
            "Set up property tax tracking",
        ],
    },
    "marketing": {
        "name": "Marketing launch",
        "description": "List a property for resale.",
        "tasks": [
            "Order signage and install on parcel",
            "Photograph and drone the property",
            "Write the listing description",
            "Publish to land marketplaces",
            "Set up buyer inquiry follow-up tasks",
        ],
    },
}


def register_phase8(app, db, style, rows, organization_id, require_csrf, roles_required, audit):
    def project_or_404(project_id: int):
        found = rows(
            """SELECT p.*,o.title deal_title,c.full_name contact_name,pr.reference_number
               FROM projects p
               LEFT JOIN opportunities o ON o.id=p.opportunity_id
               LEFT JOIN contacts c ON c.id=p.contact_id
               LEFT JOIN properties pr ON pr.id=p.property_id
               WHERE p.id=? AND p.organization_id=?""",
            (project_id, organization_id()),
        )
        if not found:
            abort(404)
        return found[0]

    def project_progress(project_id: int) -> tuple[int, int]:
        counts = rows(
            """SELECT COUNT(*) total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done
               FROM project_tasks WHERE project_id=? AND organization_id=?""",
            (project_id, organization_id()),
        )[0]
        total = counts["total"] or 0
        done = counts["done"] or 0
        return done, total

    @app.get("/projects")
    def projects():
        project_rows = rows("""SELECT p.*,o.title deal_title,
            (SELECT COUNT(*) FROM project_tasks t WHERE t.project_id=p.id) task_count,
            (SELECT COUNT(*) FROM project_tasks t WHERE t.project_id=p.id AND t.status='done') done_tasks
            FROM projects p LEFT JOIN opportunities o ON o.id=p.opportunity_id
            WHERE p.organization_id=? ORDER BY p.updated_at DESC""", (organization_id(),))
        return render_template_string(PROJECTS_TEMPLATE, style=style, phase8_style=PHASE8_STYLE, projects=project_rows)

    @app.route("/projects/new", methods=["GET", "POST"])
    @roles_required("owner", "manager", "agent")
    def new_project():
        deals = rows("""SELECT o.id,o.title,c.full_name contact_name,l.id lead_id,p.id property_id
            FROM opportunities o
            LEFT JOIN contacts c ON c.id=o.contact_id AND c.organization_id=o.organization_id
            LEFT JOIN leads l ON l.id=o.lead_id
            LEFT JOIN properties p ON p.id=o.property_id
            WHERE o.organization_id=? ORDER BY o.updated_at DESC LIMIT 200""", (organization_id(),))
        if request.method == "POST":
            require_csrf()
            name = request.form.get("name", "").strip()[:160]
            if not name:
                flash("Project name is required.", "error")
            else:
                deal_id = request.form.get("opportunity_id", "")
                deal = None
                if deal_id.isdigit():
                    deal = rows(
                        """SELECT o.id,o.lead_id,o.contact_id,o.property_id FROM opportunities o
                           WHERE o.id=? AND o.organization_id=?""",
                        (int(deal_id), organization_id()),
                    )
                now = utc_now()
                with db.connect() as connection:
                    cursor = connection.execute(
                        """INSERT INTO projects(
                            organization_id,opportunity_id,lead_id,contact_id,property_id,name,description,
                            status,created_by,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?, 'active',?,?,?)""",
                        (organization_id(),
                         int(deal_id) if deal else None,
                         deal[0]["lead_id"] if deal else None,
                         deal[0]["contact_id"] if deal else None,
                         deal[0]["property_id"] if deal else None,
                         name, request.form.get("description", "").strip()[:2000],
                         g.user.get("id"), now, now),
                    )
                    project_id = cursor.lastrowid
                audit("project.create", "project", project_id)
                flash("Project created.", "good")
                return redirect(url_for("project_detail", project_id=project_id))
        return render_template_string(
            NEW_PROJECT_TEMPLATE, style=style, phase8_style=PHASE8_STYLE,
            deals=deals, templates=PROJECT_TEMPLATES,
        )

    @app.get("/projects/<int:project_id>")
    def project_detail(project_id: int):
        project = project_or_404(project_id)
        tasks = rows("""SELECT t.*,u.display_name assignee_name FROM project_tasks t
            LEFT JOIN users u ON u.id=t.assignee_id
            WHERE t.project_id=? AND t.organization_id=? ORDER BY t.position,t.id""",
            (project_id, organization_id()))
        comments = rows("""SELECT c.*,u.display_name author_name FROM project_comments c
            LEFT JOIN users u ON u.id=c.author_id
            WHERE c.project_id=? AND c.organization_id=? ORDER BY c.created_at DESC,c.id DESC LIMIT 50""",
            (project_id, organization_id()))
        done, total = project_progress(project_id)
        return render_template_string(
            PROJECT_DETAIL_TEMPLATE, style=style, phase8_style=PHASE8_STYLE,
            project=project, tasks=tasks, columns=TASK_COLUMNS,
            column_labels=COLUMN_LABELS, comments=comments,
            done=done, total=total,
        )

    @app.post("/projects/<int:project_id>/tasks")
    @roles_required("owner", "manager", "agent")
    def add_project_task(project_id: int):
        require_csrf()
        project = project_or_404(project_id)
        title = request.form.get("title", "").strip()[:200]
        if not title:
            abort(400)
        parent = request.form.get("parent_task_id", "")
        parent = int(parent) if parent.isdigit() else None
        due = request.form.get("due_date", "").strip() or None
        assignee = request.form.get("assignee_id", "")
        assignee = int(assignee) if assignee.isdigit() else None
        now = utc_now()
        with db.connect() as connection:
            position = connection.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM project_tasks WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO project_tasks(
                    organization_id,project_id,parent_task_id,title,details,status,position,due_date,
                    assignee_id,created_by,created_at,updated_at
                ) VALUES (?,?,?,?,?,'todo',?,?,?,?,?,?)""",
                (organization_id(), project_id, parent, title,
                 request.form.get("details", "").strip()[:2000], position, due, assignee,
                 g.user.get("id"), now, now),
            )
            task_id = cursor.lastrowid
        audit("project.task_create", "project_task", task_id, {"project_id": project_id})
        flash("Task added.", "good")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<int:project_id>/tasks/<int:task_id>/status")
    @roles_required("owner", "manager", "agent")
    def project_task_status(project_id: int, task_id: int):
        require_csrf()
        status = request.form.get("status", "")
        if status not in TASK_COLUMNS:
            abort(400)
        with db.connect() as connection:
            cursor = connection.execute(
                """UPDATE project_tasks SET status=?,updated_at=? WHERE id=? AND project_id=? AND organization_id=?""",
                (status, utc_now(), task_id, project_id, organization_id()),
            )
            if not cursor.rowcount:
                abort(404)
        audit("project.task_status", "project_task", task_id, {"status": status})
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<int:project_id>/comments")
    @roles_required("owner", "manager", "agent")
    def add_project_comment(project_id: int):
        require_csrf()
        body = request.form.get("body", "").strip()[:2000]
        if not body:
            abort(400)
        with db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO project_comments(organization_id,project_id,task_id,author_id,body,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (organization_id(), project_id, None, g.user.get("id"), body, utc_now()),
            )
            comment_id = cursor.lastrowid
        audit("project.comment", "project_comment", comment_id, {"project_id": project_id})
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/<int:project_id>/status")
    @roles_required("owner", "manager", "agent")
    def project_status(project_id: int):
        require_csrf()
        status = request.form.get("status", "")
        if status not in PROJECT_STATUSES:
            abort(400)
        with db.connect() as connection:
            cursor = connection.execute(
                "UPDATE projects SET status=?,updated_at=? WHERE id=? AND organization_id=?",
                (status, utc_now(), project_id, organization_id()),
            )
            if not cursor.rowcount:
                abort(404)
        audit("project.status", "project", project_id, {"status": status})
        flash(f"Project marked {status.replace('_',' ')}.", "good")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.post("/projects/from-template")
    @roles_required("owner", "manager", "agent")
    def project_from_template():
        require_csrf()
        template_key = request.form.get("template_key", "")
        if template_key not in PROJECT_TEMPLATES:
            abort(400)
        template = PROJECT_TEMPLATES[template_key]
        name = request.form.get("name", "").strip()[:160]
        if not name:
            flash("Project name is required.", "error")
            return redirect(url_for("projects"))
        deal_id = request.form.get("opportunity_id", "")
        deal = None
        if deal_id.isdigit():
            deal = rows(
                """SELECT o.id,o.lead_id,o.contact_id,o.property_id FROM opportunities o
                   WHERE o.id=? AND o.organization_id=?""",
                (int(deal_id), organization_id()),
            )
        now = utc_now()
        with db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO projects(
                    organization_id,opportunity_id,lead_id,contact_id,property_id,name,description,
                    status,template_key,created_by,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?, 'active',?,?,?,?)""",
                (organization_id(),
                 int(deal_id) if deal else None,
                 deal[0]["lead_id"] if deal else None,
                 deal[0]["contact_id"] if deal else None,
                 deal[0]["property_id"] if deal else None,
                 name, template["description"], template_key,
                 g.user.get("id"), now, now),
            )
            project_id = cursor.lastrowid
            for index, task_title in enumerate(template["tasks"], start=1):
                connection.execute(
                    """INSERT INTO project_tasks(
                        organization_id,project_id,title,status,position,created_by,created_at,updated_at
                    ) VALUES (?,?,?, 'todo',?,?,?,?)""",
                    (organization_id(), project_id, task_title, index * 10, g.user.get("id"), now, now),
                )
        audit("project.create_from_template", "project", project_id, {"template": template_key})
        flash(f"Project created from the {template['name']} template.", "good")
        return redirect(url_for("project_detail", project_id=project_id))


PROJECTS_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Projects · CRM</title><style>{{style}}{{phase8_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="/">← Dashboard</a><h1 style="margin-top:8px">Projects</h1><div class="sub">Due diligence, closing, and marketing workspaces linked to deals</div></div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<div class="grid"><section><h2>Projects</h2><div class="stack">{% for item in projects %}<article class="panel proj-card {{item.status|replace('_','')}}"><div class="top"><div><a class="leadlink" href="{{url_for('project_detail',project_id=item.id)}}">{{item.name}}</a><div class="muted">{{item.deal_title or 'No linked deal'}} · {{item.done_tasks}}/{{item.task_count}} tasks done</div></div><span class="pill">{{item.status|replace('_',' ')}}</span></div><div class="progress"><div style="width:{{(item.done_tasks * 100 // item.task_count) if item.task_count else 0}}%"></div></div></article>{% else %}<div class="panel empty">No projects yet. Create one from a template below.</div>{% endfor %}</div></section>
<aside class="stack"><div class="panel"><h2>Start from a template</h2>{% for key,template in templates.items() %}<details style="padding:10px 0;border-bottom:1px solid var(--line)"><summary><b>{{template.name}}</b> <span class="muted">({{template.tasks|length}} tasks)</span></summary><div class="muted">{{template.description}}</div><form method="post" action="{{url_for('project_from_template')}}" style="margin-top:8px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="template_key" value="{{key}}"><label class="field">Project name<input name="name" value="{{template.name}} project" required></label><label class="field">Link to deal (optional)<select name="opportunity_id"><option value="">None</option>{% for deal in deals %}<option value="{{deal.id}}">{{deal.title}}</option>{% endfor %}</select></label><button class="btn">Create project</button></form></details>{% endfor %}</div><form class="panel" method="post" action="{{url_for('new_project')}}"><h2>Blank project</h2><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Name<input name="name" required></label><label class="field">Description<textarea name="description"></textarea></label><button class="btn">Create project</button></form></aside></div></main></body></html>
"""

NEW_PROJECT_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>New project · CRM</title><style>{{style}}{{phase8_style}}</style></head><body><main class="shell" style="max-width:720px"><a class="muted" href="{{url_for('projects')}}">← Projects</a><h1 style="margin-top:8px">New project</h1><form class="panel wide-form" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Name<input name="name" required></label><label class="field">Description<textarea name="description"></textarea></label><button class="btn">Create project</button></form></main></body></html>
"""

PROJECT_DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{project.name}} · Project</title><style>{{style}}{{phase8_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('projects')}}">← Projects</a><h1 style="margin-top:8px">{{project.name}}</h1><div class="sub">{{project.deal_title or 'No linked deal'}} · {{done}}/{{total}} tasks done</div></div><div class="top-actions"><span class="pill">{{project.status|replace('_',' ')}}</span>{% if current_user.role in ('owner','manager') %}<form method="post" action="{{url_for('project_status',project_id=project.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field"><select name="status" onchange="this.form.submit()">{% for s in ('active','on_hold','completed','cancelled') %}<option value="{{s}}" {% if project.status==s %}selected{% endif %}>{{s|replace('_',' ')|title}}</option>{% endfor %}</select></label></form>{% endif %}</div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<div class="progress" style="max-width:420px"><div style="width:{{(done * 100 // total) if total else 0}}%"></div></div>
<div class="board" style="margin-top:16px">{% for column in columns %}<div class="board-col"><h3>{{column_labels[column]}} ({{tasks|selectattr('status','equalto',column)|list|length}})</h3>{% for task in tasks if task.status==column %}<div class="task-card {{'done' if column=='done'}}"><b>{{task.title}}</b>{% if task.assignee_name %}<div class="muted">Assigned: {{task.assignee_name}}</div>{% endif %}{% if task.due_date %}<div class="muted">Due {{task.due_date}}</div>{% endif %}{% if task.details %}<div class="muted">{{task.details}}</div>{% endif %}<div class="task-actions">{% for next_status in columns %}{% if next_status!=column %}<form method="post" action="{{url_for('project_task_status',project_id=project.id,task_id=task.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><input type="hidden" name="status" value="{{next_status}}"><button class="btn secondary" style="padding:4px 8px;font-size:12px">{{column_labels[next_status]}}</button></form>{% endif %}{% endfor %}</div></div>{% endfor %}</div>{% endfor %}</div>
<div class="grid" style="margin-top:20px"><section><form class="panel" method="post" action="{{url_for('add_project_task',project_id=project.id)}}"><h2>Add task</h2><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field">Title<input name="title" required></label><label class="field">Details<textarea name="details"></textarea></label><div class="two-col"><label class="field">Due date<input type="date" name="due_date"></label><label class="field">Subtask of<select name="parent_task_id"><option value="">None (top-level task)</option>{% for task in tasks %}{% if not task.parent_task_id %}<option value="{{task.id}}">{{task.title}}</option>{% endif %}{% endfor %}</select></label></div><button class="btn">Add task</button></form></section><aside><div class="panel"><h2>Comments</h2>{% for comment in comments %}<div class="comment"><b>{{comment.author_name or 'Team'}}</b><div class="muted">{{comment.created_at}}</div><div>{{comment.body}}</div></div>{% else %}<div class="muted">No comments yet.</div>{% endfor %}{% if current_user.role in ('owner','manager','agent') %}<form method="post" action="{{url_for('add_project_comment',project_id=project.id)}}" style="margin-top:10px"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="field"><textarea name="body" placeholder="Add a project note..." required></textarea></label><button class="btn secondary">Comment</button></form>{% endif %}</div></aside></div></main></body></html>
"""
