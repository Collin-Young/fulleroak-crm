"""CSV-based CRM campaigns with per-property aerial attachments."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, flash, redirect, render_template_string, request, send_file, url_for
from crm_db import utc_now
from crm_phase13 import _sms_schedule, CENTRAL_TIME
from vumber_client import VumberError
from prepare_wild_horse import select_phone

VARIANTS = {
    'A': "Hi {first_name}, this is Collin with Fulleroak Properties. I'm reaching out about your vacant land{location_clause}. I've attached an aerial of the property. Would you consider selling it? Reply STOP to opt out.",
    'B': "Hi {first_name}, Collin with Fulleroak Properties here. Would you be open to discussing an offer for your land{location_clause}, shown in the attached aerial? Reply STOP to opt out.",
    'C': "Hello {first_name}, this is Collin at Fulleroak Properties. I wanted to ask whether you would consider an offer for your vacant land{location_clause}. The attached aerial shows the property. Reply STOP to opt out.",
    'D': "Hi {first_name}, this is Collin from Fulleroak Properties. Are you interested in selling your land{location_clause}, pictured in this aerial? Reply STOP to opt out.",
}

def render_campaign_message(key,first_name,location_clause=''):
    return VARIANTS[key].format(first_name=first_name or 'there',location_clause=location_clause)

SCHEMA = """
CREATE TABLE IF NOT EXISTS text_campaigns (
 id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES organizations(id),
 name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', sending_number TEXT NOT NULL DEFAULT '3142544288',
 source_sha256 TEXT NOT NULL, variants_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(organization_id,name));
CREATE TABLE IF NOT EXISTS text_campaign_recipients (
 id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES organizations(id),
 campaign_id INTEGER NOT NULL REFERENCES text_campaigns(id), lead_id INTEGER REFERENCES leads(id),
 reference TEXT NOT NULL, full_name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', selected_slot INTEGER,
 parcel_link TEXT NOT NULL, apn TEXT NOT NULL, county TEXT NOT NULL DEFAULT '', acreage TEXT NOT NULL DEFAULT '',
 variant_key TEXT NOT NULL, message TEXT NOT NULL, image_path TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft', reason TEXT NOT NULL DEFAULT '', source_json TEXT NOT NULL,
 scheduled_for TEXT, upload_id TEXT NOT NULL DEFAULT '', provider_message_id TEXT NOT NULL DEFAULT '',
 sent_at TEXT, updated_at TEXT NOT NULL, UNIQUE(campaign_id,reference));
CREATE INDEX IF NOT EXISTS idx_text_campaign_due ON text_campaign_recipients(campaign_id,status,scheduled_for);
"""

def ensure_schema(db):
    with db.connect() as c:
        c.executescript(SCHEMA)

def suppressed(c, org, phone, lead_id):
    return c.execute("""SELECT 1 FROM consent_suppressions s
        LEFT JOIN contacts ct ON ct.id=s.contact_id AND ct.organization_id=s.organization_id
        WHERE s.organization_id=? AND s.active=1 AND s.channel IN ('sms','all')
        AND (s.lead_id=? OR ct.primary_phone_normalized=?) LIMIT 1""", (org,lead_id,phone)).fetchone() is not None or c.execute("""
        SELECT 1 FROM land_sms_permissions p JOIN land_contact_phones lp ON lp.id=p.phone_id
        WHERE p.organization_id=? AND lp.phone_normalized=? AND p.status IN ('opted_out','do_not_contact')
        LIMIT 1""", (org,phone)).fetchone() is not None or c.execute("""
        SELECT 1 FROM communications WHERE organization_id=? AND lead_id=? AND kind='sms' AND direction='incoming'
        AND upper(trim(message,' .!?')) IN ('STOP','STOPALL','UNSUBSCRIBE','CANCEL','END','QUIT') LIMIT 1
        """,(org,lead_id)).fetchone() is not None

def import_campaign(db, path, name='26.21 Wild Horse', org=1):
    """Import deterministically into a draft; reruns preserve delivery state."""
    ensure_schema(db)
    path=Path(path)
    with path.open(encoding='utf-8-sig',newline='') as handle:
        source=list(csv.DictReader(handle))
    required={'26.21 Reference','LI Link','APN','Owner 1 First Name','Owner Mailing Name'}
    if not source or not required.issubset(source[0]):
        raise ValueError('The file is missing required Wild Horse columns.')
    references=[r['26.21 Reference'].strip() for r in source]
    if any(not r for r in references) or len(set(references))!=len(references):
        raise ValueError('Campaign references must be present and unique.')
    now=utc_now()
    with db.connect() as c:
        c.execute("""INSERT INTO text_campaigns(organization_id,name,source_sha256,variants_json,created_at,updated_at)
         VALUES(?,?,?,?,?,?) ON CONFLICT(organization_id,name) DO NOTHING""",
         (org,name,hashlib.sha256(path.read_bytes()).hexdigest(),json.dumps(VARIANTS),now,now))
        campaign=dict(c.execute('SELECT * FROM text_campaigns WHERE organization_id=? AND name=?',(org,name)).fetchone())
        if campaign['status']=='active': raise ValueError('Pause the campaign before changing its audience.')
    used=set()
    for index,row in enumerate(source):
        slot,phone=select_phone(row)
        variant='ABCD'[index%4]
        first=row['Owner 1 First Name'].strip() or 'there'
        message=render_campaign_message(variant,first)
        full_name=row['Owner Mailing Name'].strip() or (first+' '+row.get('Owner 1 Last Name','')).strip()
        lead_id=None
        status='excluded'; reason='No mobile number with explicit No DNC, State DNC, and Litigator flags.'
        if phone:
            lead_id=db.ensure_lead(phone,f'({phone[:3]}) {phone[3:6]}-{phone[6:]}',org)
            with db.connect() as c:
                lead=c.execute('SELECT * FROM leads WHERE id=? AND organization_id=?',(lead_id,org)).fetchone()
                refs=[r.strip() for r in lead['reference_number'].split(',') if r.strip()]
                if row['26.21 Reference'] not in refs: refs.append(row['26.21 Reference'])
                c.execute("""UPDATE leads SET owner_name=CASE WHEN owner_name='' THEN ? ELSE owner_name END,
                   reference_number=?, lead_source=CASE WHEN lead_source='' THEN '26.21 Wild Horse skip trace' ELSE lead_source END,
                   updated_at=? WHERE id=? AND organization_id=?""",(full_name,', '.join(refs),now,lead_id,org))
            db.sync_contact_for_lead(lead_id)
            with db.connect() as c:
                c.execute("""INSERT INTO properties(organization_id,lead_id,reference_number,owner_name,apn,offer_price,source,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'26.21 Wild Horse',?,?) ON CONFLICT(organization_id,reference_number) WHERE reference_number!=''
                    DO NOTHING""",(org,lead_id,row['26.21 Reference'],full_name,row['APN'],row.get('Offer',''),now,now))
                prop=c.execute('SELECT id FROM properties WHERE organization_id=? AND reference_number=?',(org,row['26.21 Reference'])).fetchone()
                contact=c.execute('SELECT id FROM contacts WHERE organization_id=? AND lead_id=?',(org,lead_id)).fetchone()
                c.execute('INSERT OR IGNORE INTO contact_properties VALUES(?,?,?,?)',(org,contact['id'],prop['id'],'owner'))
                blocked=suppressed(c,org,phone,lead_id)
            if phone in used: status='duplicate'; reason='Another property already uses this number; one opening message per number.'
            elif blocked: status='suppressed'; reason='Existing CRM opt-out or suppression.'
            else: status='draft'; reason='Awaiting property aerial.'
            used.add(phone)
        with db.connect() as c:
            c.execute("""INSERT INTO text_campaign_recipients(organization_id,campaign_id,lead_id,reference,full_name,phone,
               selected_slot,parcel_link,apn,variant_key,message,status,reason,source_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(campaign_id,reference) DO NOTHING""",
               (org,campaign['id'],lead_id,row['26.21 Reference'],full_name,phone,slot,row['LI Link'],row['APN'],variant,message,status,reason,json.dumps(row),now))
    return campaign['id']

TEMPLATE="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{campaign.name}} · SMS campaign</title><style>{{style}}
.aerial{width:260px;max-width:100%;border-radius:8px}.campaign-table td{vertical-align:top}.message{max-width:440px;white-space:pre-wrap}</style></head><body><main class="shell">
<header class="top"><div><a href="/">← CRM</a><h1>{{campaign.name}}</h1><div class="sub">SMS campaign with property aerials · {{campaign.status|title}}</div></div><a class="btn secondary" href="{{url_for('land_seller_sms_campaign')}}">Other seller campaign</a></header>
{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
<section class="stats"><div class="stat"><b>{{stats.total}}</b><span>Property records</span></div><div class="stat"><b>{{stats.candidates}}</b><span>Unique mobile recipients</span></div><div class="stat"><b>{{stats.images}}</b><span>Aerials ready</span></div><div class="stat"><b>{{stats.sent}}</b><span>Messages sent</span></div></section>
{% if connection_error %}<section class="panel"><b>Reconnect Vumber before sending</b><p>{{connection_error}}</p><a class="btn" href="{{url_for('vumber_settings')}}">Reconnect Vumber</a></section>{% endif %}
<section class="panel"><h2>Spinner messages</h2><div class="grid">{% for key,message in variants.items() %}<div><b>Version {{key}}</b><p>{{message}}</p></div>{% endfor %}</div><p class="muted">One opening text per unique number. 10 texts per hour, weekdays, 10 AM–4 PM Central. Sender: (314) 254-4288.</p>
{% if stats.missing %}<p>{{stats.missing}} recipients are waiting for a property aerial from the full parcel export.</p>{% endif %}
{% if current_user.role in ('owner','manager') %}{% if campaign.status=='active' %}<form method="post" action="{{url_for('pause_text_campaign',campaign_id=campaign.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn">Pause campaign</button></form>{% else %}<form method="post" action="{{url_for('start_text_campaign',campaign_id=campaign.id)}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><button class="btn" {% if stats.missing or not stats.pending %}disabled{% endif %}>Start {{stats.pending}} messages with aerials</button></form>{% endif %}{% endif %}</section>
<section class="panel"><h2>Audience and message preview</h2><p><a href="{{url_for('text_campaign',campaign_id=campaign.id)}}">Recipients</a> · <a href="{{url_for('text_campaign',campaign_id=campaign.id,view='excluded')}}">Excluded / duplicate records</a> · Page {{page}}</p><div class="table-wrap"><table class="campaign-table"><thead><tr><th>Owner / reference</th><th>Number</th><th>Aerial</th><th>Text</th><th>Status</th></tr></thead><tbody>{% for r in members %}<tr><td>{% if r.lead_id %}<a href="{{url_for('lead_detail',lead_id=r.lead_id)}}">{{r.full_name}}</a>{% else %}{{r.full_name}}{% endif %}<div class="muted">{{r.reference}}{% if r.county %} · {{r.county}}{% endif %}</div></td><td>{{r.phone or '—'}}{% if r.selected_slot %}<div class="muted">Phone {{r.selected_slot}}</div>{% endif %}</td><td>{% if r.image_path %}<a href="{{url_for('text_campaign_aerial',recipient_id=r.id)}}"><img class="aerial" src="{{url_for('text_campaign_aerial',recipient_id=r.id)}}" alt="Property aerial for {{r.reference}}"></a>{% else %}<span class="muted">{{'Awaiting aerial' if r.status=='draft' else '—'}}</span>{% endif %}</td><td class="message">{% if r.status not in ('excluded','duplicate') %}{{r.message}}{% else %}—{% endif %}</td><td>{{r.status|title}}<div class="muted">{{r.reason}}</div></td></tr>{% endfor %}</tbody></table></div><div class="actions">{% if page>1 %}<a class="btn secondary" href="{{url_for('text_campaign',campaign_id=campaign.id,page=page-1,view=view)}}">Previous</a>{% endif %}{% if has_more %}<a class="btn secondary" href="{{url_for('text_campaign',campaign_id=campaign.id,page=page+1,view=view)}}">Next</a>{% endif %}</div></section></main></body></html>"""

def register_text_campaigns(app,db,style,org_id,require_csrf,roles_required,audit,vumber):
    ensure_schema(db)
    running=set(); lock=threading.Lock()

    @app.get('/sms-campaigns')
    def text_campaign_list():
        with db.connect() as c:
            campaigns=[dict(r) for r in c.execute('SELECT * FROM text_campaigns WHERE organization_id=? ORDER BY id DESC',(org_id(),))]
        return render_template_string('''<!doctype html><html><head><meta charset="utf-8"><title>SMS campaigns · CRM</title><style>{{style}}</style></head><body><main class="shell"><a href="/">← CRM</a><h1>SMS campaigns</h1>{% for c in campaigns %}<div class="panel"><a class="leadlink" href="{{url_for('text_campaign',campaign_id=c.id)}}">{{c.name}}</a><p>{{c.status|title}} · Property aerials</p></div>{% endfor %}<div class="panel"><a href="{{url_for('land_seller_sms_campaign')}}">Missouri vacant-land seller interest</a></div></main></body></html>''',style=style,campaigns=campaigns)

    def worker(campaign_id,org):
        try:
            while True:
                with db.connect() as c:
                    campaign=c.execute('SELECT * FROM text_campaigns WHERE id=? AND organization_id=?',(campaign_id,org)).fetchone()
                    if not campaign or campaign['status']!='active': return
                    item=c.execute("SELECT * FROM text_campaign_recipients WHERE campaign_id=? AND organization_id=? AND status='queued' ORDER BY scheduled_for,id LIMIT 1",(campaign_id,org)).fetchone()
                    if not item:
                        c.execute("UPDATE text_campaigns SET status='completed',updated_at=? WHERE id=? AND organization_id=?",(utc_now(),campaign_id,org)); return
                    item=dict(item)
                    if suppressed(c,org,item['phone'],item['lead_id']):
                        c.execute("UPDATE text_campaign_recipients SET status='suppressed',reason='CRM opt-out before send',updated_at=? WHERE id=?",(utc_now(),item['id'])); continue
                    slot,_phone=select_phone(json.loads(item['source_json']))
                    if _phone!=item['phone'] or slot!=item['selected_slot']:
                        c.execute("UPDATE text_campaign_recipients SET status='suppressed',reason='Phone eligibility changed',updated_at=? WHERE id=?",(utc_now(),item['id'])); continue
                now=datetime.now(timezone.utc)
                due=datetime.fromisoformat(item['scheduled_for'])
                if now<due: time.sleep(min((due-now).total_seconds(),10)); continue
                local=now.astimezone(CENTRAL_TIME)
                if local.weekday()>4 or not 10<=local.hour<16:
                    time.sleep(10); continue
                # Claim once. An ambiguous send failure is held for manual review, never retried automatically.
                with db.connect() as c:
                    active=c.execute("SELECT 1 FROM text_campaigns WHERE id=? AND organization_id=? AND status='active'",(campaign_id,org)).fetchone()
                    if not active: return
                    if suppressed(c,org,item['phone'],item['lead_id']): continue
                    claimed=c.execute("UPDATE text_campaign_recipients SET status='sending',updated_at=? WHERE id=? AND status='queued'",(utc_now(),item['id'])).rowcount
                if not claimed: continue
                try:
                    image=Path(item['image_path'])
                    if not image.is_file(): raise ValueError('The property aerial is missing.')
                    upload_id=item['upload_id'] or vumber.upload_sms_image(campaign['sending_number'],image)
                    with db.connect() as c:
                        c.execute('UPDATE text_campaign_recipients SET upload_id=? WHERE id=?',(upload_id,item['id']))
                        if suppressed(c,org,item['phone'],item['lead_id']):
                            c.execute("UPDATE text_campaign_recipients SET status='suppressed',reason='Opt-out before send' WHERE id=?",(item['id'],)); continue
                        if not c.execute("SELECT 1 FROM text_campaigns WHERE id=? AND status='active'",(campaign_id,)).fetchone():
                            c.execute("UPDATE text_campaign_recipients SET status='queued' WHERE id=?",(item['id'],)); return
                    result=vumber.send_sms(item['phone'],campaign['sending_number'],item['message'],[upload_id])
                    provider_id=str(result.get('messageId') or result.get('id') or '')
                    if not provider_id and result.get('model'): provider_id=str(result['model'][0].get('messageId',''))
                    now=utc_now()
                    with db.connect() as c:
                        c.execute("UPDATE text_campaign_recipients SET status='sent',provider_message_id=?,sent_at=?,reason='',updated_at=? WHERE id=?",(provider_id,now,now,item['id']))
                        communication=c.execute("""INSERT INTO communications(organization_id,lead_id,kind,from_number,to_number,message,status,response,provider_message_id,created_at)
                            VALUES(?,?,'sms',?,?,?,'sent',?,?,?)""",(org,item['lead_id'],campaign['sending_number'],item['phone'],item['message'],json.dumps(result),provider_id,now)).lastrowid
                        c.execute("""INSERT INTO activity_events(organization_id,lead_id,event_type,channel,direction,occurred_at,title,body,source_table,source_id,metadata_json,created_at)
                            VALUES(?,?,'sms','sms','outgoing',?,'Campaign text with aerial',?,'communications',?,?,?)""",(org,item['lead_id'],now,item['message'],communication,json.dumps({'campaign_id':campaign_id,'image_path':str(image),'upload_id':upload_id}),now))
                        # Preserve six minutes between actual transmissions after a delayed slot.
                        remaining=c.execute("SELECT id FROM text_campaign_recipients WHERE campaign_id=? AND status='queued' ORDER BY scheduled_for,id",(campaign_id,)).fetchall()
                        from datetime import timedelta
                        for row,slot in zip(remaining,_sms_schedule(datetime.now(timezone.utc)+timedelta(minutes=6),len(remaining))):
                            c.execute('UPDATE text_campaign_recipients SET scheduled_for=? WHERE id=?',(slot.astimezone(timezone.utc).isoformat(),row['id']))
                except Exception as exc:
                    with db.connect() as c:
                        c.execute("UPDATE text_campaign_recipients SET status='held',reason=?,updated_at=? WHERE id=?",(str(exc)[:500],utc_now(),item['id']))
                        c.execute("UPDATE text_campaigns SET status='paused',updated_at=? WHERE id=?",(utc_now(),campaign_id))
                    return
        finally:
            with lock: running.discard(campaign_id)

    @app.get('/sms-campaigns/<int:campaign_id>')
    def text_campaign(campaign_id):
        with db.connect() as c:
            campaign=c.execute('SELECT * FROM text_campaigns WHERE id=? AND organization_id=?',(campaign_id,org_id())).fetchone()
            if not campaign: abort(404)
            members=[dict(r) for r in c.execute("SELECT * FROM text_campaign_recipients WHERE campaign_id=? AND organization_id=? ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'queued' THEN 1 WHEN 'sent' THEN 2 ELSE 3 END,id",(campaign_id,org_id()))]
        candidates=[r for r in members if r['status'] not in {'excluded','duplicate','suppressed'}]
        pending=[r for r in candidates if r['status'] in {'draft','queued'}]
        stats=dict(total=len(members),candidates=len(candidates),images=sum(bool(r['image_path']) for r in candidates),sent=sum(r['status']=='sent' for r in members),pending=len(pending),missing=sum(not r['image_path'] or not Path(r['image_path']).is_file() for r in pending))
        connection_error=''
        try:
            vumber.configuration()
        except VumberError as exc:
            connection_error=str(exc)
        view=request.args.get('view','recipients')
        visible=[r for r in members if (r['status'] in {'excluded','duplicate','suppressed'})==(view=='excluded')]
        try: page=max(1,int(request.args.get('page','1')))
        except ValueError: page=1
        offset=(page-1)*15
        return render_template_string(TEMPLATE,style=style,campaign=dict(campaign),members=visible[offset:offset+15],stats=stats,variants=json.loads(campaign['variants_json']),connection_error=connection_error,page=page,view=view,has_more=offset+15<len(visible))

    @app.get('/sms-campaigns/aerial/<int:recipient_id>')
    def text_campaign_aerial(recipient_id):
        with db.connect() as c:
            r=c.execute('SELECT image_path FROM text_campaign_recipients WHERE id=? AND organization_id=?',(recipient_id,org_id())).fetchone()
        if not r or not r['image_path'] or not Path(r['image_path']).is_file(): abort(404)
        return send_file(r['image_path'],mimetype='image/jpeg')

    @app.post('/sms-campaigns/<int:campaign_id>/start')
    @roles_required('owner','manager')
    def start_text_campaign(campaign_id):
        require_csrf(); org=org_id()
        with lock:
            if campaign_id in running: return redirect(url_for('text_campaign',campaign_id=campaign_id))
            with db.connect() as c:
                campaign=c.execute('SELECT * FROM text_campaigns WHERE id=? AND organization_id=?',(campaign_id,org)).fetchone()
                if not campaign: abort(404)
                pending=[dict(r) for r in c.execute("SELECT * FROM text_campaign_recipients WHERE campaign_id=? AND organization_id=? AND status IN ('draft','queued') ORDER BY id",(campaign_id,org))]
                if not pending or any(not r['image_path'] or not Path(r['image_path']).is_file() for r in pending):
                    flash('Each recipient needs its matching property aerial before launch.','error'); return redirect(url_for('text_campaign',campaign_id=campaign_id))
                try:
                    allowed={re.sub(r'\D','',r['value'])[-10:] for r in vumber.configuration(force=True)['sms_numbers']}
                except VumberError as exc:
                    flash(str(exc),'error'); return redirect(url_for('text_campaign',campaign_id=campaign_id))
                if campaign['sending_number'] not in allowed: abort(400,'The campaign sending line is unavailable.')
                eligible=[]
                for row in pending:
                    if suppressed(c,org,row['phone'],row['lead_id']):
                        c.execute("UPDATE text_campaign_recipients SET status='suppressed',reason='CRM opt-out' WHERE id=?",(row['id'],))
                    else: eligible.append(row)
                for row,slot in zip(eligible,_sms_schedule(datetime.now(timezone.utc),len(eligible))):
                    c.execute("UPDATE text_campaign_recipients SET status='queued',reason='',scheduled_for=?,updated_at=? WHERE id=?",(slot.astimezone(timezone.utc).isoformat(),utc_now(),row['id']))
                c.execute("UPDATE text_campaigns SET status='active',updated_at=? WHERE id=? AND organization_id=?",(utc_now(),campaign_id,org))
            running.add(campaign_id)
            threading.Thread(target=worker,args=(campaign_id,org),daemon=True).start()
        audit('text_campaign.started','text_campaign',campaign_id,{'recipients':len(eligible)})
        flash(f'Campaign started with {len(eligible)} messages and matching aerials.','success')
        return redirect(url_for('text_campaign',campaign_id=campaign_id))

    @app.post('/sms-campaigns/<int:campaign_id>/pause')
    @roles_required('owner','manager')
    def pause_text_campaign(campaign_id):
        require_csrf()
        with db.connect() as c:
            if not c.execute('SELECT 1 FROM text_campaigns WHERE id=? AND organization_id=?',(campaign_id,org_id())).fetchone(): abort(404)
            c.execute("UPDATE text_campaigns SET status='paused',updated_at=? WHERE id=? AND organization_id=?",(utc_now(),campaign_id,org_id()))
        audit('text_campaign.paused','text_campaign',campaign_id)
        return redirect(url_for('text_campaign',campaign_id=campaign_id))

    # Restarting a server never silently launches a CSV campaign or retries an uncertain send.
    with db.connect() as c:
        c.execute("UPDATE text_campaigns SET status='paused',updated_at=? WHERE status='active'",(utc_now(),))
        c.execute("UPDATE text_campaign_recipients SET status='held',reason='Server restarted during send; check provider history before retrying',updated_at=? WHERE status='sending'",(utc_now(),))
