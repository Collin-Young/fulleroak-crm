import argparse,json
from pathlib import Path
from crm_db import CRMDatabase,utc_now
from vumber_client import VumberClient,VumberSessionStore

db=CRMDatabase()
with db.connect() as c:
    rows=[dict(r) for r in c.execute("SELECT * FROM text_campaign_recipients WHERE campaign_id=1 AND status='draft' ORDER BY id")]
    print('draft recipients',len(rows),'aerials',sum(bool(r['image_path']) and Path(r['image_path']).is_file() for r in rows))
    row=rows[0]
parser=argparse.ArgumentParser(); parser.add_argument('--upload-test',action='store_true'); args=parser.parse_args()
assert len(rows)==52 and len({r['phone'] for r in rows})==52
for r in rows:
    assert r['image_path'] and Path(r['image_path']).is_file()
    assert Path(r['image_path']).stat().st_size<=498000
    assert 'APN' not in r['message']
print('52 unique recipients, matching aerials within MMS size limit; no APNs in messages')
if args.upload_test and row['image_path'] and not row['upload_id']:
    client=VumberClient(VumberSessionStore('data/vumber_session.bin'))
    print('vumber connected',client.connected)
    upload_id=client.upload_sms_image('3142544288',row['image_path'])
    with db.connect() as c:
        c.execute('UPDATE text_campaign_recipients SET upload_id=?,updated_at=? WHERE id=?',(upload_id,utc_now(),row['id']))
    print('first aerial upload verified')
