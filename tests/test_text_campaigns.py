import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from crm_db import CRMDatabase, utc_now
from crm_text_campaigns import import_campaign, suppressed
from prepare_wild_horse import select_phone
from vumber_client import VumberClient, VumberError
from crm_app import create_app

class TextCampaignTests(unittest.TestCase):
    def test_location_messages_include_property_town_county_state(self):
        from update_campaign_locations import location_clause
        from crm_text_campaigns import render_campaign_message
        clause=location_clause([{'City':'HARTSEL','County':'Park County','State':'CO'}])
        self.assertEqual(clause,' in Hartsel, Park County, Colorado')
        for variant in 'ABCD':
            message=render_campaign_message(variant,'James',clause)
            self.assertIn('Hartsel, Park County, Colorado',message)
            self.assertNotIn('APN',message)
        clause=location_clause([{'City':'','County':'Park County','State':'CO','Latitude':'38.95','Longitude':'-105.8'}])
        self.assertEqual(clause,' near Hartsel, Park County, Colorado')
    def test_multi_parcel_geometry(self):
        from build_campaign_aerials import parcel_ids,geometry_data
        self.assertEqual(parcel_ids('https://app.landinsights.co/data?parcel=1 | https://app.landinsights.co/data?parcel=2'),['1','2'])
        data=geometry_data('<Polygon><outerBoundaryIs><LinearRing><coordinates>-105,38 -105,39 -106,38 -105,38</coordinates></LinearRing></outerBoundaryIs></Polygon>')
        self.assertEqual(data['coordinates'][0][0],(38.0,-105.0))
    def row(self,ref='PACO-2621-1'):
        row={'26.21 Reference':ref,'LI Link':'https://app.landinsights.co/data?parcel=123','APN':'123',
             'Owner 1 First Name':'James','Owner Mailing Name':'James Hyde','Offer':'50000'}
        for i in range(1,7):
            row[f'Phone {i}']=''; row[f'Phone {i} Type']=''
            for flag in ['DNC','State DNC','Litigator']: row[flag if i==1 else f'Phone {i} {flag}']=''
        row.update({'Phone 1':'2135550100','Phone 1 Type':'Mobile','DNC':'Yes','State DNC':'No','Litigator':'No',
                    'Phone 2':'4085550100','Phone 2 Type':'Mobile','Phone 2 DNC':'No','Phone 2 State DNC':'No','Phone 2 Litigator':'No'})
        return row

    def test_fallback_phone_and_unknown_flags(self):
        row=self.row()
        self.assertEqual(select_phone(row),(2,'4085550100'))
        row['Phone 2 DNC']=''
        self.assertEqual(select_phone(row),(None,''))

    def test_import_dedup_optout_and_rerun_preserves_send(self):
        with tempfile.TemporaryDirectory() as temp:
            db=CRMDatabase(Path(temp)/'test.db')
            lead=db.ensure_lead('4085550100','4085550100')
            source=Path(temp)/'source.csv'
            r1=self.row(); r2=self.row('PACO-2621-2')
            with source.open('w',newline='',encoding='utf-8') as f:
                writer=csv.DictWriter(f,fieldnames=r1.keys()); writer.writeheader(); writer.writerows([r1,r2])
            cid=import_campaign(db,source)
            with db.connect() as c:
                members=c.execute('SELECT * FROM text_campaign_recipients ORDER BY id').fetchall()
                self.assertEqual([r['status'] for r in members],['draft','duplicate'])
                self.assertEqual(members[0]['lead_id'],lead)
                self.assertNotIn('APN',members[0]['message'])
                c.execute("UPDATE text_campaign_recipients SET status='sent',provider_message_id='123' WHERE id=?",(members[0]['id'],))
            self.assertEqual(import_campaign(db,source),cid)
            with db.connect() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM text_campaign_recipients").fetchone()[0],2)
                self.assertEqual(c.execute("SELECT provider_message_id FROM text_campaign_recipients WHERE status='sent'").fetchone()[0],'123')

    def test_import_respects_existing_sms_optout(self):
        with tempfile.TemporaryDirectory() as temp:
            db=CRMDatabase(Path(temp)/'test.db'); lead=db.ensure_lead('4085550100','4085550100')
            with db.connect() as c:
                c.execute("INSERT INTO consent_suppressions(organization_id,lead_id,channel,created_at,updated_at) VALUES(1,?,'sms',?,?)",(lead,utc_now(),utc_now()))
            source=Path(temp)/'source.csv'; row=self.row()
            with source.open('w',newline='',encoding='utf-8') as f:
                writer=csv.DictWriter(f,fieldnames=row.keys()); writer.writeheader(); writer.writerow(row)
            import_campaign(db,source)
            with db.connect() as c:
                self.assertEqual(c.execute('SELECT status FROM text_campaign_recipients').fetchone()[0],'suppressed')

    def test_incoming_stop_excludes_campaign_recipient(self):
        with tempfile.TemporaryDirectory() as temp:
            db=CRMDatabase(Path(temp)/'test.db'); lead=db.ensure_lead('4085550100','4085550100')
            with db.connect() as c:
                c.execute("INSERT INTO communications(organization_id,lead_id,kind,direction,message,created_at) VALUES(1,?,'sms','incoming','Stop!',?)",(lead,utc_now()))
                self.assertTrue(suppressed(c,1,'4085550100',lead))

    def test_mms_upload_and_send_contract(self):
        store=Mock(); store.load.return_value={'account_no':'123','access_token':'secret','expires_at':9999999999}
        http=Mock(); upload_response=Mock(ok=True); upload_response.json.return_value={'uploadId':9}
        http.post.return_value=upload_response
        sms_response=Mock(ok=True); sms_response.json.return_value={'messageId':'42','errorName':0}
        http.request.return_value=sms_response
        client=VumberClient(store,http)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'aerial.jpg'; path.write_bytes(b'fake image bytes')
            upload=client.upload_sms_image('3142544288',path)
            kwargs=http.post.call_args.kwargs
            self.assertEqual(kwargs['data'],{'virtualNumber':'3142544288','fileType':'10'})
            self.assertIn('FileName[]',kwargs['files'])
            self.assertNotIn('Content-Type',kwargs['headers'])
            client.send_sms('4085550100','3142544288','Hi James',[upload])
            self.assertEqual(http.request.call_args.kwargs['json']['uploadIds'],'9')
            path.write_bytes(b'x'*498001)
            with self.assertRaises(VumberError): client.upload_sms_image('3142544288',path)

    def test_preview_and_missing_aerial_cannot_send(self):
        with tempfile.TemporaryDirectory() as temp:
            db=CRMDatabase(Path(temp)/'test.db'); source=Path(temp)/'source.csv'; row=self.row()
            with source.open('w',newline='',encoding='utf-8') as f:
                writer=csv.DictWriter(f,fieldnames=row.keys()); writer.writeheader(); writer.writerow(row)
            cid=import_campaign(db,source)
            vumber=Mock(); vumber.configuration.return_value={'sms_numbers':[{'value':'3142544288'}]}
            app=create_app(db.path,auth_enabled=False,vumber=vumber); client=app.test_client()
            response=client.get(f'/sms-campaigns/{cid}')
            self.assertEqual(response.status_code,200)
            self.assertIn(b'Awaiting aerial',response.data)
            response=client.post(f'/sms-campaigns/{cid}/start',follow_redirects=True)
            self.assertIn(b'Each recipient needs its matching property aerial',response.data)
            vumber.send_sms.assert_not_called(); vumber.upload_sms_image.assert_not_called()

if __name__=='__main__': unittest.main()
