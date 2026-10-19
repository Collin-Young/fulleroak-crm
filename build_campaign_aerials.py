"""Join the full LandInsights export by exact parcel link and reuse the report-card Esri renderer."""
import argparse, csv, json, re, zipfile
import io,math,requests
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import property_map_generator as maps

def parcel_id(link):
    match=re.search(r'parcel=(\d+)',str(link))
    return match.group(1) if match else ''

def parcel_ids(link):
    return list(dict.fromkeys(re.findall(r'parcel=(\d+)',str(link))))

def geometry_data(text):
    if not str(text).strip(): raise ValueError('Missing parcel_KML Geometry')
    root=ET.fromstring(text)
    rings=[]
    for node in root.iter():
        if node.tag.split('}')[-1]=='coordinates' and node.text:
            ring=[]
            for point in node.text.split():
                values=point.split(',')
                if len(values)>=2: ring.append((float(values[1]),float(values[0])))
            if len(ring)>=3: rings.append(ring)
    if not rings: raise ValueError('No polygon coordinates in geometry')
    return {'coordinates':rings}

def tile_aerial(geometry,acreage):
    """Fallback for Esri export errors, using the same imagery in its native tile grid."""
    from PIL import Image
    bbox=maps._padded_bbox_for_image(geometry['coordinates'],1200,840,acreage)
    origin=20037508.342789244
    z=19; span=2*origin/(2**z)
    xmin,ymin,xmax,ymax=bbox
    left=math.floor((xmin+origin)/span); right=math.floor((xmax+origin)/span)
    top=math.floor((origin-ymax)/span); bottom=math.floor((origin-ymin)/span)
    canvas=Image.new('RGB',((right-left+1)*256,(bottom-top+1)*256))
    for y in range(top,bottom+1):
        for x in range(left,right+1):
            response=requests.get(f'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',timeout=30)
            response.raise_for_status()
            tile=Image.open(io.BytesIO(response.content)).convert('RGB')
            if tile.size!=(256,256): raise ValueError('Unexpected imagery tile size')
            canvas.paste(tile,((x-left)*256,(y-top)*256))
    crop=((xmin+origin-left*span)*256/span,(origin-ymax-top*span)*256/span,
          (xmax+origin-left*span)*256/span,(origin-ymin-top*span)*256/span)
    image=canvas.resize((1200,840),Image.Resampling.LANCZOS,box=crop)
    maps._draw_boundary_overlay(image,geometry['coordinates'],bbox)
    return image

def load_export(path,ids):
    csv.field_size_limit(20_000_000)
    path=Path(path)
    found={}
    def scan(handle):
        reader=csv.DictReader(handle)
        geometry=next((k for k in (reader.fieldnames or []) if k.lower().replace('_',' ').endswith('kml geometry')),None)
        if not geometry: raise ValueError('Export has no KML Geometry column')
        links=[k for k in reader.fieldnames if k.lower().replace('_',' ').endswith('parcel link')]
        for row in reader:
            pid=next((parcel_id(row.get(k,'')) for k in links if parcel_id(row.get(k,'')) in ids),'')
            if pid:
                candidate=dict(row,geometry=row[geometry])
                if pid in found and found[pid]['geometry']!=candidate['geometry']:
                    raise ValueError(f'Conflicting geometry for parcel {pid}')
                found[pid]=candidate
    if path.suffix=='.zip':
        import io
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith('.csv'):
                    with z.open(name) as f: scan(io.TextIOWrapper(f,encoding='utf-8-sig',newline=''))
    else:
        with path.open(encoding='utf-8-sig',newline='') as f: scan(f)
    return found

def build(db,export_path, campaign_id=1):
    from PIL import Image,ImageDraw
    from crm_db import utc_now
    with db.connect() as c:
        members=[dict(r) for r in c.execute('SELECT * FROM text_campaign_recipients WHERE campaign_id=?',(campaign_id,))]
    ids={pid for r in members for pid in parcel_ids(r['parcel_link'])}
    export=load_export(export_path,ids)
    root=db.path.parent/'wild_horse'/'aerials'; root.mkdir(parents=True,exist_ok=True)
    (root.parent/'parcel_matches.json').write_text(json.dumps(export,indent=2),encoding='utf-8')
    print(f'Exact parcel matches: {len(export)}/{len(ids)}',flush=True)
    targets=[r for r in members if r['status'] not in {'excluded','duplicate','suppressed'}]
    def generate(r):
        pids=parcel_ids(r['parcel_link']); parcels=[export.get(pid) for pid in pids]
        row=parcels[0] if parcels else None
        if not parcels or any(not p for p in parcels): return (r['id'],None,'No exact parcel match in export',row)
        normalize=lambda apn: re.sub(r'[^A-Z0-9]','',apn.upper())
        expected={normalize(apn) for apn in re.split(r'[,|]',r['apn']) if apn.strip()}
        actual={normalize(p.get('APN') or p.get('parcel_APN') or '') for p in parcels}
        if expected!=actual:
            return (r['id'],None,'Parcel link matched but APN differs; review required',row)
        out=root/f'{r["reference"]}.jpg'
        if not out.exists():
            try:
                geometry={'coordinates':[ring for p in parcels for ring in geometry_data(p['geometry'])['coordinates']]}
                acreage=sum(float(p.get('Acreage') or p.get('parcel_Acreage') or 0) for p in parcels)
                try:
                    maps.generate_esri_property_map({'parcel_Acreage':acreage},geometry,str(out),width=1200,height=840)
                except Exception:
                    bbox=maps._padded_bbox_for_image(geometry['coordinates'],1200,840,acreage)
                    image=tile_aerial(geometry,acreage)
                    image.save(out)
                with Image.open(out) as src: image=src.convert('RGB')
                draw=ImageDraw.Draw(image)
                draw.rectangle((0,image.height-24,image.width,image.height),fill=(255,255,255))
                draw.text((8,image.height-19),'Imagery: Esri | Parcel boundary: LandInsights | Approximate boundary',fill=(40,40,40))
                for quality in (88,80,70,60,50):
                    image.save(out,format='JPEG',quality=quality,optimize=True)
                    if out.stat().st_size<498000: break
                if out.stat().st_size>=498000: raise ValueError('Image too large for MMS')
            except Exception as exc: return (r['id'],None,str(exc),row)
        return (r['id'],str(out.resolve()),'',row)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rid,path,error,row in pool.map(generate,targets):
            with db.connect() as c:
                c.execute('UPDATE text_campaign_recipients SET image_path=?,reason=?,county=?,acreage=?,updated_at=? WHERE id=?',
                    (path or '',error,(row or {}).get('County') or (row or {}).get('parcel_County') or '',str((row or {}).get('Acreage') or (row or {}).get('parcel_Acreage') or ''),utc_now(),rid))
            print(f'{rid}: {"aerial ready" if path else error}',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('export'); parser.add_argument('--campaign-id',type=int,default=1)
    args=parser.parse_args()
    from crm_db import CRMDatabase
    build(CRMDatabase(),args.export,args.campaign_id)
