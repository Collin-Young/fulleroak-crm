import csv, json, re
from pathlib import Path

SOURCE=Path('SKIPPED_26.21_Wild_Horse_texting_extract_2026-10-14.csv')
def yes(value): return str(value).strip().lower() in {'yes','true','1','y'}
def phone(value):
    n=re.sub(r'\D','',value)
    if len(n)==11 and n.startswith('1'): n=n[1:]
    return n if len(n)==10 else ''

def select_phone(row):
    candidates=[]
    for slot in range(1,7):
        number=phone(row[f'Phone {slot}'])
        flag_names=['DNC','State DNC','Litigator'] if slot==1 else [f'Phone {slot} '+f for f in ['DNC','State DNC','Litigator']]
        if number and row[f'Phone {slot} Type'].strip().lower()=='mobile' and all(row[k].strip().lower() in {'no','false','0','n'} for k in flag_names):
            candidates.append((slot,number))
    return candidates[0] if candidates else (None,'')

if __name__=='__main__':
    rows=list(csv.DictReader(SOURCE.open(encoding='utf-8-sig',newline='')))
    selected=[dict(row,selected_slot=select_phone(row)[0],selected_phone=select_phone(row)[1]) for row in rows]
    out=Path('data/wild_horse'); out.mkdir(exist_ok=True)
    (out/'source.json').write_text(json.dumps(selected,indent=2),encoding='utf-8')
    ready=[r for r in selected if r['selected_phone']]
    print('mobile candidates',len(ready),'unique phones',len({r['selected_phone'] for r in ready}))
    print('parcel ids',json.dumps([r['LI Link'].split('parcel=')[-1] for r in ready]))
