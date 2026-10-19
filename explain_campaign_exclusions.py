import csv,collections,sqlite3
from prepare_wild_horse import phone,select_phone

counts=collections.Counter(); flags=collections.Counter(); unknown=0
with open('SKIPPED_26.21_Wild_Horse_texting_extract_2026-10-14.csv',encoding='utf-8-sig',newline='') as f:
    for r in csv.DictReader(f):
        present=[i for i in range(1,7) if phone(r[f'Phone {i}'])]
        mobile=[i for i in present if r[f'Phone {i} Type'].strip().lower()=='mobile']
        if select_phone(r)[1]: counts['eligible_property_records']+=1; continue
        if not present: counts['no_valid_phone']+=1; continue
        if not mobile: counts['no_mobile_phone']+=1; continue
        counts['all_mobiles_excluded']+=1
        rowflags=set()
        for i in mobile:
            for key in ['DNC','State DNC','Litigator']:
                column=key if i==1 else f'Phone {i} {key}'
                value=r[column].strip().lower()
                if value in {'yes','true','1','y'}: rowflags.add(key)
                elif value not in {'no','false','0','n'}: rowflags.add('unknown flags')
        flags.update(rowflags)
print('disjoint_record_counts',dict(counts))
print('excluded_mobile_record_flags_overlap',dict(flags))
c=sqlite3.connect('data/leads.db')
print('campaign_statuses',c.execute('SELECT status,count(*) FROM text_campaign_recipients WHERE campaign_id=1 GROUP BY status').fetchall())
