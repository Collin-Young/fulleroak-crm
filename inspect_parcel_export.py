import csv,zipfile,io
from pathlib import Path
path=Path(r'C:\Users\Collin\Downloads\Park-County-CO-batch-1-0-99999-40773.csv.zip')
csv.field_size_limit(20000000)
with zipfile.ZipFile(path) as z:
    print('archive files',z.namelist())
    with z.open(z.namelist()[0]) as f:
        reader=csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig',newline=''))
        print('columns',reader.fieldnames)
        found=[r for r in reader if '24440482' in r.get('Parcel Link','') or '24440482' in r.get('parcel_Parcel Link','')]
        for r in found:
            print('first property',{k:v for k,v in r.items() if k in ['APN','County','Acreage','Parcel Link','KML Geometry']})
import sqlite3,json
c=sqlite3.connect('data/leads.db'); c.row_factory=sqlite3.Row
matches=json.loads(Path('data/wild_horse/parcel_matches.json').read_text(encoding='utf-8'))
for r in c.execute("SELECT * FROM text_campaign_recipients WHERE status='draft' AND image_path='' ORDER BY id"):
    pid=r['parcel_link'].split('parcel=')[-1]; full=matches.get(pid,{})
    print('unresolved',r['id'],r['reference'],r['apn'],'export apn',full.get('APN'),full.get('Parcel Alt APN'),'link',r['parcel_link'],'reason',r['reason'])
    if full:
        from build_campaign_aerials import geometry_data,maps
        geom=geometry_data(full['geometry'])
        print('bbox',maps._padded_bbox_for_image(geom['coordinates'],1200,840,full['Acreage']))
