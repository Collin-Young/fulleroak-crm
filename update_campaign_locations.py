"""Update unsent campaign copy from the matched parcel export, preserving sent history."""
import json,math,re,sqlite3
from pathlib import Path
from crm_db import utc_now
from crm_text_campaigns import VARIANTS,render_campaign_message

def location_clause(parcels):
    locations=[]
    for p in parcels:
        city=p.get('City','').strip().title()
        county=p.get('County','').strip()
        state={'CO':'Colorado'}.get(p.get('State','').strip(),p.get('State','').strip())
        if not county or not state: raise ValueError('Missing county or state')
        prefix='in '
        if not city:
            # GNIS Hartsel populated-place coordinates. Rural parcels get "near", never an invented situs city.
            lat=float(p['Latitude']); lon=float(p['Longitude'])
            lat0,lon0=39.021659,-105.795842
            a=math.sin(math.radians(lat-lat0)/2)**2+math.cos(math.radians(lat))*math.cos(math.radians(lat0))*math.sin(math.radians(lon-lon0)/2)**2
            miles=3958.8*2*math.asin(math.sqrt(a))
            if state!='Colorado' or county!='Park County' or miles>20: raise ValueError('Missing town needs property review')
            city='Hartsel'; prefix='near '
        place=f'{prefix}{city}, {county}, {state}'
        if place not in locations: locations.append(place)
    return ' '+ ' and '.join(locations)

if __name__=='__main__':
    matches=json.loads(Path('data/wild_horse/parcel_matches.json').read_text(encoding='utf-8'))
    c=sqlite3.connect('data/leads.db'); c.row_factory=sqlite3.Row
    if c.execute('SELECT status FROM text_campaigns WHERE id=1').fetchone()[0]!='paused': raise ValueError('Pause before editing')
    recipients=c.execute("SELECT * FROM text_campaign_recipients WHERE campaign_id=1 AND status IN ('queued','draft')").fetchall()
    updates=[]; locations={}
    for r in recipients:
        parcels=[matches[pid] for pid in re.findall(r'parcel=(\d+)',r['parcel_link'])]
        clause=location_clause(parcels)
        source=json.loads(r['source_json']); source['property_location_clause']=clause
        message=render_campaign_message(r['variant_key'],source['Owner 1 First Name'].strip(),clause)
        assert 'APN' not in message and len(message)<=1600
        updates.append((message,json.dumps(source),utc_now(),r['id']))
        locations[clause]=locations.get(clause,0)+1
    with c:
        c.executemany('UPDATE text_campaign_recipients SET message=?,source_json=?,updated_at=? WHERE id=?',updates)
        c.execute('UPDATE text_campaigns SET variants_json=?,updated_at=? WHERE id=1',(json.dumps(VARIANTS),utc_now()))
    print('updated',len(updates),'locations',locations)
    print('example',updates[0][0])
