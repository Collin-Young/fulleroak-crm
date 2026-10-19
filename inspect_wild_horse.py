import csv, json, sqlite3
from pathlib import Path

rows = list(csv.DictReader(open('SKIPPED_26.21_Wild_Horse_texting_extract_2026-10-14.csv', encoding='utf-8-sig')))
print('rows', len(rows), 'properties', len({r['LI Link'] for r in rows}))
for path in ['all_fop_offers.db','data/offers_index.db','data/leads.db']:
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(path, tables if path != 'data/leads.db' else [t for t in tables if 'campaign' in t])
    if path == 'all_fop_offers.db':
        cols=[r[1] for r in c.execute('PRAGMA table_info(data)')]
        print('source relevant columns', [x for x in cols if any(k in x.lower() for k in ['kml','geomet','reference','parcel','county','acre','lat','lon'])])
        for col in cols:
            if 'reference' == col.lower():
                found = c.execute(f'SELECT * FROM data WHERE "{col}"=?', (rows[0]['26.21 Reference'],)).fetchone()
                print('first match', {k:v for k,v in dict(found or {}).items() if v and any(s in k.lower() for s in ['kml','geomet','parcel','county','acre','lat','lon','reference'])})
    if path == 'data/leads.db':
        for t in [t for t in tables if 'campaign' in t]:
            print(t, c.execute(f'SELECT count(*) FROM {t}').fetchone()[0])
        print('existing campaigns', [dict(r) for r in c.execute('SELECT * FROM land_sms_campaigns')])
    c.close()
root=Path(r'C:\Users\Collin\Documents\Prop Report Card')
ids={r['LI Link'].split('parcel=')[-1] for r in rows}
print('existing matching kml', len([p for p in (root/'kml').glob('*.kml') if any(p.name.startswith('parcel_'+i+'_') or p.name=='parcel_'+i+'.kml' for i in ids)]))
src=sqlite3.connect('file:all_fop_offers.db?mode=ro',uri=True); src.row_factory=sqlite3.Row
for col in ['Reference','Reference_1','Reference_2','Reference_1_2','Reference_2_2','parcel_Parcel_Link','Parcel_Link']:
    match=src.execute(f'SELECT * FROM data WHERE "{col}"=?',(rows[0]['LI Link'] if 'Link' in col else rows[0]['26.21 Reference'],)).fetchone()
    if match: print('match via',col,{k:v for k,v in dict(match).items() if v and any(t in k.lower() for t in ['kml','geomet','parcel','county','acre','reference'])})
