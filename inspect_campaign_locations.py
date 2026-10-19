import json,sqlite3,re
from pathlib import Path
matches=json.loads(Path('data/wild_horse/parcel_matches.json').read_text(encoding='utf-8'))
c=sqlite3.connect('data/leads.db'); c.row_factory=sqlite3.Row
locations={}; missing=[]
for r in c.execute("SELECT * FROM text_campaign_recipients WHERE campaign_id=1 AND status IN ('queued','sent','sending')"):
    ids=re.findall(r'parcel=(\d+)',r['parcel_link'])
    for pid in ids:
        p=matches[pid]; key=(p['City'],p['County'],p['State']); locations[key]=locations.get(key,0)+1
        if not all(key): missing.append((r['reference'],key,p['Parcel Address']))
print('locations',locations,'missing',missing)
