import argparse, sqlite3
from pathlib import Path
from crm_db import CRMDatabase
from crm_text_campaigns import import_campaign

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('csv'); parser.add_argument('--database',default='data/leads.db'); parser.add_argument('--name',default='26.21 Wild Horse')
    args=parser.parse_args()
    path=Path(args.database)
    backup=path.with_name(path.stem+'.before-wild-horse.db')
    if path.exists() and not backup.exists():
        with sqlite3.connect(path) as source, sqlite3.connect(backup) as target: source.backup(target)
    db=CRMDatabase(path)
    campaign_id=import_campaign(db,args.csv,args.name)
    with db.connect() as c:
        print('Campaign',campaign_id)
        print([dict(r) for r in c.execute('SELECT status,count(*) count FROM text_campaign_recipients WHERE campaign_id=? GROUP BY status',(campaign_id,))])
