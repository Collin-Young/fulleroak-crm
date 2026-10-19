"""Run a team CRM instance with login required and a Windows-compatible WSGI server."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

def main():
    load_dotenv()
    parser=argparse.ArgumentParser(description='Serve the CRM with team login enabled')
    data_dir=Path(os.environ.get('CRM_DATA_DIR','data'))
    local_offers=Path('all_fop_offers.db')
    default_offers=local_offers if local_offers.exists() else data_dir / 'all_fop_offers.db'
    parser.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'))
    parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','5000')))
    parser.add_argument('--database',type=Path,default=Path(os.environ.get('CRM_DATABASE',data_dir / 'leads.db')))
    parser.add_argument('--offers-database',type=Path,default=Path(os.environ.get('CRM_OFFERS_DATABASE',default_offers)))
    args=parser.parse_args()
    from waitress import serve
    from crm_app import create_app
    app=create_app(args.database,args.offers_database,auth_enabled=True)
    serve(app,host=args.host,port=args.port,threads=8)

if __name__=='__main__':
    main()
