import base64
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash

from crm_app import create_app
from crm_db import CRMDatabase
from crm_phase13 import _sms_schedule
from document_service import generate_pdf
from lead_pipeline import (
    choose_reference_number, extract_call_metadata, extract_call_metadata_from_subject,
    extract_reference_digit_groups, extract_references_from_text, extract_sms_metadata,
    normalize_phone, sanitize_owner_name,
)
from workflow_engine import WorkflowEngine


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_lead_call_and_dashboard(self):
        lead_id = self.db.ensure_lead("6125550100", "(612) 555-0100")
        audio_path = Path(self.temp.name) / "call.mp3"
        audio_path.write_bytes(b"ID3test-audio")
        added = self.db.add_call(lead_id, {
            "message_id": "m1", "source_key": "m1:a1", "direction": "incoming",
            "timestamp_iso": "2026-09-22T12:00:00+00:00", "transcript": "I need a quote.",
            "attachment_path": str(audio_path),
        })
        self.assertTrue(added)
        self.assertFalse(self.db.add_call(lead_id, {"message_id": "m1", "source_key": "m1:a1"}))
        client = create_app(self.db_path, auth_enabled=False).test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"(612) 555-0100", response.data)
        self.assertIn(b"Most recent activity", response.data)
        detail = client.get(f"/lead/{lead_id}")
        self.assertIn(b"Full transcript", detail.data)
        self.assertIn(b"Distress signal", detail.data)
        with self.db.connect() as connection:
            call_id = connection.execute("SELECT id FROM calls WHERE lead_id=?", (lead_id,)).fetchone()[0]
        audio = client.get(f"/call/{call_id}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"ID3test-audio")
        audio.close()

    def test_campaign_recovery_schedule_preserves_rate_and_send_window(self):
        central = ZoneInfo("America/Chicago")
        slots = _sms_schedule(datetime(2026, 10, 2, 15, 55, tzinfo=central), 4)
        self.assertEqual(
            slots,
            [
                datetime(2026, 10, 5, 10, 0, tzinfo=central),
                datetime(2026, 10, 5, 10, 6, tzinfo=central),
                datetime(2026, 10, 5, 10, 12, tzinfo=central),
                datetime(2026, 10, 5, 10, 18, tzinfo=central),
            ],
        )

    def test_vumber_sms_parser_and_idempotent_timeline(self):
        body = (
            "You received an SMS message sent to your Vumber.\n"
            "Vumber: (314) 555-0169\nFrom: (612) 555-0126\n"
            "Time: 9/23/2026 3:26:35 PM Central Standard Time\n"
            "Message: This is Maya Sample. My reference is DEMO-2612-1.\n"
            "Please text me tomorrow."
        )
        payload = {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        }
        parsed = extract_sms_metadata(
            "SMS message from (612) 555 0126 to your Vumber (314) 555 0169(St Louis)",
            payload,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(normalize_phone(parsed.other_party_number), "6125550126")
        self.assertEqual(parsed.vumber_number, "3145550169")
        self.assertEqual(parsed.vumber_label, "St Louis")
        self.assertIn("Please text me tomorrow.", parsed.message)
        lead_id = self.db.ensure_lead("6125550126", "(612) 555-0126")
        arguments = dict(
            lead_id=lead_id, provider_message_id="gmail-sms-1",
            from_number="6125550126", to_number="3145550169",
            message=parsed.message,
            occurred_at=parsed.timestamp.isoformat(),
        )
        self.assertTrue(self.db.add_inbound_sms(**arguments))
        self.assertFalse(self.db.add_inbound_sms(**arguments))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM communications").fetchone()[0], 1)
            event = connection.execute(
                "SELECT * FROM activity_events WHERE source_table='communications'"
            ).fetchone()
        self.assertEqual(event["channel"], "sms")
        self.assertEqual(event["body"], parsed.message)

    def test_crm_edits_survive(self):
        lead_id = self.db.ensure_lead("6125550100", "(612) 555-0100")
        client = create_app(self.db_path, auth_enabled=False).test_client()
        response = client.post(f"/lead/{lead_id}", data={
            "owner_name": "Taylor", "status": "qualified",
            "next_follow_up": "2026-09-25", "notes": "Send estimate",
        })
        self.assertEqual(response.status_code, 302)
        with self.db.connect() as connection:
            lead = connection.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        self.assertEqual(lead["status"], "qualified")
        self.assertEqual(lead["notes"], "Send estimate")

    def test_activity_sorting(self):
        older = self.db.ensure_lead("6125550101", "(612) 555-0101")
        newer = self.db.ensure_lead("6125550102", "(612) 555-0102")
        self.db.add_call(older, {
            "message_id": "old", "source_key": "old:1",
            "timestamp_iso": "2026-01-01T12:00:00+00:00",
        })
        self.db.add_call(newer, {
            "message_id": "new", "source_key": "new:1",
            "timestamp_iso": "2026-09-01T12:00:00+00:00",
        })
        client = create_app(self.db_path, auth_enabled=False).test_client()
        recent = client.get("/").data
        oldest = client.get("/?sort=oldest").data
        self.assertLess(recent.index(f"/lead/{newer}".encode()), recent.index(f"/lead/{older}".encode()))
        self.assertLess(oldest.index(f"/lead/{older}".encode()), oldest.index(f"/lead/{newer}".encode()))

    def test_manual_reference_matches_offer_database(self):
        offers_path = Path(self.temp.name) / "offers.db"
        with sqlite3.connect(offers_path) as connection:
            connection.execute("""CREATE TABLE data (
                Reference TEXT, Offer_Price REAL, Owner_Mailing_Name TEXT,
                Lot_Acreage REAL, APN TEXT, County_Name TEXT, State TEXT,
                SITUS_FULL_ADDRESS TEXT, Mail_Address TEXT, Mail_City TEXT,
                Mail_State TEXT, Mail_Zip_Code TEXT, Legal_Description TEXT,
                Parcel_Link TEXT, Google_Map TEXT, Date TEXT, Closing_Date TEXT
            )""")
            connection.execute("""INSERT INTO data VALUES (
                'DEMO-258-1316', 12500, 'Maya Sample', 6.2, 'APN-42',
                'Putnam', 'MO', '10 Farm Road', 'PO Box 1', 'Unionville',
                'MO', '63565', 'Test legal', 'https://example.com/parcel',
                'https://example.com/map', '2026-01-01', '2026-02-01'
            )""")
        connection.close()
        lead_id = self.db.ensure_lead("3145550121", "(314) 555-0121")
        client = create_app(self.db_path, offers_path, auth_enabled=False).test_client()
        response = client.post(f"/lead/{lead_id}", data={
            "owner_name": "Maya Sample", "reference_number": " demo - 258 - 1316 ",
            "status": "qualified", "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        detail = client.get(f"/lead/{lead_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Matched offer details", detail.data)
        self.assertIn(b"Maya Sample", detail.data)
        self.assertIn(b"$12,500.00", detail.data)
        self.assertIn(b"APN-42", detail.data)
        with self.db.connect() as connection:
            reference = connection.execute(
                "SELECT reference_number FROM leads WHERE id=?", (lead_id,)
            ).fetchone()[0]
        self.assertEqual(reference, "DEMO-258-1316")

    def test_vumber_metadata_parsing(self):
        body = "(612) 555 0100 Time: 9/22/2026 1:15:00 PM Central Standard Time Listen to your recorded call"
        payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}
        metadata = extract_call_metadata(payload)
        self.assertIsNotNone(metadata)
        self.assertEqual(normalize_phone(metadata.other_party_number), "6125550100")

    def test_current_vumber_template_uses_lead_number(self):
        body = """You made a call from your Vumber. The details are as follows:
Vumber: (800) 555 0100 St Louis
To: (314) 555 0199
Time: 9/22/2026 1:15:00 PM Central Standard Time"""
        payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}
        metadata = extract_call_metadata(payload)
        self.assertEqual(metadata.direction, "outgoing")
        self.assertEqual(normalize_phone(metadata.other_party_number), "3145550199")
        self.assertEqual(metadata.vumber_number, "8005550100")

    def test_subject_identity_and_reference_parsing(self):
        metadata = extract_call_metadata_from_subject(
            "Call received from (612) 555 0100 to your Vumber (800) 555 0199(St Louis)"
        )
        self.assertEqual(metadata.direction, "incoming")
        self.assertEqual(normalize_phone(metadata.other_party_number), "6125550100")
        self.assertIn("DEMO-2612-1", extract_references_from_text(
            "The reference number is DEMO-2612-1. Please call back."
        ))

    def test_voicemail_and_long_caller_id_subjects(self):
        voicemail = extract_call_metadata_from_subject(
            "Voice mail from (573) 555 0118 to your Vumber (800) 224 7305(CY #)"
        )
        self.assertEqual(voicemail.direction, "incoming")
        self.assertEqual(normalize_phone(voicemail.other_party_number), "5735550118")

        long_number = extract_call_metadata_from_subject(
            "Missed call from 912261182300 to your Vumber (800) 224 7305(CY #)"
        )
        self.assertEqual(long_number.direction, "missed")
        self.assertEqual(normalize_phone(long_number.other_party_number), "912261182300")

    def test_recording_id_is_not_used_as_reference(self):
        self.assertEqual(choose_reference_number("137799598-1381686-3145550169", []), "")
        self.assertEqual(
            choose_reference_number("137799598-1381686-3145550169", ["DEMO-2612-1"]),
            "DEMO-2612-1",
        )
        self.assertEqual(
            choose_reference_number("", ["DEMO-2612-1", "DEMO-258-1316"]),
            "DEMO-2612-1, DEMO-258-1316",
        )

    def test_spoken_reference_formats(self):
        cases = {
            "the reference number was DEMO, DASH, 258, DASH, 1316. 1316. 1316.": "DEMO-258-1316",
            "reference number F-R-V-A-2-2-2-4-2-8-5-8": "FRVA-2224-2858",
            "reference number F-R-V-A. two three one two two three seven three": "FRVA-2312-2373",
            "reference number Delta Echo Mike Oscar dash 2612 dash 1": "DEMO-2612-1",
        }
        for transcript, expected in cases.items():
            self.assertIn(expected, extract_references_from_text(transcript))

    def test_spoken_reference_beyond_short_window(self):
        transcript = (
            "The reference number? Mm-hmm. " + ("We discussed the mailing for a while. " * 8)
            + "Yeah, so that's a d-e-m-o -2618 -853 All right."
        )
        self.assertIn("DEMO-2618-853", extract_references_from_text(transcript))

    def test_digit_group_recovery_from_garbled_prefixes(self):
        cases = {
            "A reference number? Yes. um 0RMO-2612-560. 2612560 Yes. 612-560 you Let's see.": ["2612-560"],
            "A reference number is... C-M-O dash two six one four dash one six seven seven dash 1677. and six": ["2614-1677"],
            "Sure, the reference number on the letter is 2618-853. Thanks.": ["2618-853"],
        }
        for transcript, expected in cases.items():
            self.assertEqual(extract_reference_digit_groups(transcript), expected)

    def test_digit_groups_ignore_recording_ids_and_phones(self):
        self.assertEqual(
            extract_reference_digit_groups("recording 137799598-1381686-3145550169 no refs"),
            [],
        )
        self.assertEqual(
            extract_reference_digit_groups("call me at (573) 225-5335 thanks"),
            [],
        )

    def test_operator_is_not_the_lead_name(self):
        for name in ("Collin Young", "Colin Young", "Collen Young", "Calling Yung", "Colin"):
            self.assertEqual(sanitize_owner_name(name), "")
        self.assertEqual(sanitize_owner_name("Jordan Example"), "Jordan Example")

    def test_vumber_connection_and_explicit_actions(self):
        class FakeVumber:
            def __init__(self):
                self.is_connected = False
                self.texts = []
                self.calls = []

            @property
            def connected(self):
                return self.is_connected

            def status(self):
                return {
                    "connected": self.is_connected, "username": "agent@example.com",
                    "account_no": "1234",
                }

            def connect(self, username, password):
                self.is_connected = True
                return {"username": username, "account_no": "1234"}

            def disconnect(self):
                self.is_connected = False

            def configuration(self, force=False):
                return {
                    "sms_numbers": [{"value": "8005550100", "label": "Sales"}],
                    "call_numbers": [{"value": "8005550100", "label": "Sales"}],
                    "callback_numbers": [{"value": "3145550198", "label": "Cell"}],
                }

            def send_sms(self, recipient, virtual_number, message):
                self.texts.append((recipient, virtual_number, message))
                return {"errorName": 0}

            def make_call(self, recipient, virtual_number, callback_number):
                self.calls.append((recipient, virtual_number, callback_number))
                return {"ok": True}

        fake = FakeVumber()
        lead_id = self.db.ensure_lead("3145550121", "(314) 555-0121")
        client = create_app(self.db_path, vumber=fake, auth_enabled=False).test_client()
        with client.session_transaction() as browser_session:
            browser_session["csrf_token"] = "test-token"

        settings = client.get("/settings/vumber")
        self.assertIn(b"Sign in to Vumber", settings.data)
        connected = client.post("/settings/vumber", data={
            "csrf_token": "test-token", "username": "agent@example.com", "password": "secret",
        })
        self.assertEqual(connected.status_code, 302)
        detail = client.get(f"/lead/{lead_id}")
        self.assertIn(b"Send text now", detail.data)
        self.assertIn(b"Place call now", detail.data)

        no_confirmation = client.post(f"/lead/{lead_id}/vumber/text", data={
            "csrf_token": "test-token", "virtual_number": "8005550100", "message": "Hello",
        })
        self.assertEqual(no_confirmation.status_code, 400)
        sent = client.post(f"/lead/{lead_id}/vumber/text", data={
            "csrf_token": "test-token", "confirmed": "yes",
            "virtual_number": "8005550100", "message": "Hello",
        })
        self.assertEqual(sent.status_code, 302)
        self.assertEqual(fake.texts, [("3145550121", "8005550100", "Hello")])

        called = client.post(f"/lead/{lead_id}/vumber/call", data={
            "csrf_token": "test-token", "confirmed": "yes", "virtual_number": "8005550100",
            "callback_number": "3145550198",
        })
        self.assertEqual(called.status_code, 302)
        self.assertEqual(fake.calls, [("3145550121", "8005550100", "3145550198")])
        with self.db.connect() as connection:
            kinds = [row[0] for row in connection.execute(
                "SELECT kind FROM communications WHERE lead_id=? ORDER BY id", (lead_id,)
            )]
        self.assertEqual(kinds, ["sms", "call"])

    def test_manual_lead_creation_generates_editable_text_suggestions(self):
        client = create_app(self.db_path, auth_enabled=False).test_client()
        with client.session_transaction() as browser_session:
            browser_session["csrf_token"] = "new-lead-token"
        created = client.post("/lead/new", data={
            "csrf_token": "new-lead-token",
            "owner_name": "Maya Sample",
            "phone": "(314) 555-0121",
            "reference_number": "DEMO-2612-1",
            "status": "new",
            "deal_style": "Inherited property",
            "lead_source": "Mailer",
            "notes": "Interested in a simple cash sale but has not named a price.",
        })
        self.assertEqual(created.status_code, 302)
        detail = client.get(created.headers["Location"])
        self.assertIn(b"Suggested texts", detail.data)
        self.assertIn(b"Maya", detail.data)
        self.assertIn(b"DEMO-2612-1", detail.data)
        self.assertIn(b"Use this draft", detail.data)
        with self.db.connect() as connection:
            lead = connection.execute(
                "SELECT * FROM leads WHERE phone_normalized='3145550121'"
            ).fetchone()
            draft_count = connection.execute(
                "SELECT COUNT(*) FROM message_drafts WHERE lead_id=?", (lead["id"],)
            ).fetchone()[0]
        self.assertEqual(lead["deal_style"], "Inherited property")
        self.assertEqual(lead["lead_source"], "Mailer")
        self.assertEqual(draft_count, 3)

    def test_duplicate_manual_lead_opens_existing_record(self):
        lead_id = self.db.ensure_lead("3145550121", "(314) 555-0121")
        client = create_app(self.db_path, auth_enabled=False).test_client()
        with client.session_transaction() as browser_session:
            browser_session["csrf_token"] = "duplicate-token"
        response = client.post("/lead/new", data={
            "csrf_token": "duplicate-token", "owner_name": "Duplicate",
            "phone": "314-555-0121", "status": "new",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/lead/{lead_id}"))


class SecurityAndTenancyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def csrf(client):
        with client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def setup_owner(self):
        self.client.get("/setup")
        response = self.client.post("/setup", data={
            "csrf_token": self.csrf(self.client), "display_name": "Owner One",
            "email": "owner@example.com", "password": "correct horse battery staple",
        })
        self.assertEqual(response.status_code, 302)

    def login(self, client, email, password):
        client.get("/login")
        return client.post("/login", data={
            "csrf_token": self.csrf(client), "email": email, "password": password,
        })

    def test_html_pages_are_not_cached(self):
        response = self.client.get("/setup")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["Expires"], "0")

        self.setup_owner()
        dashboard = self.client.get("/")
        self.assertIn(b"setInterval", dashboard.data)
        self.assertIn(b"30000", dashboard.data)

    def test_missing_browser_asset_does_not_clear_login_csrf(self):
        self.setup_owner()
        self.client.post("/logout", data={"csrf_token": self.csrf(self.client)})
        self.client.get("/login")
        token = self.csrf(self.client)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 404)
        self.assertEqual(self.csrf(self.client), token)

    def test_setup_auth_csrf_roles_and_audit(self):
        self.assertTrue(self.client.get("/").headers["Location"].endswith("/setup"))
        self.setup_owner()
        created = self.client.post("/lead/new", data={
            "csrf_token": self.csrf(self.client), "owner_name": "Seller One",
            "phone": "314-555-0101", "status": "new",
        })
        self.assertEqual(created.status_code, 302)
        lead_id = int(created.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual(self.client.post(f"/lead/{lead_id}", data={"status": "qualified"}).status_code, 400)
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO users(organization_id,email,display_name,password_hash,role,created_at)
                   VALUES (1,?,?,?,?,?)""",
                ("viewer@example.com", "Read Only", generate_password_hash("viewer password 123"), "viewer", "2026-01-01"),
            )
            actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
        self.assertIn("auth.setup", actions)
        self.assertIn("lead.create", actions)
        viewer = self.app.test_client()
        self.assertEqual(self.login(viewer, "viewer@example.com", "viewer password 123").status_code, 302)
        viewer.get("/")
        denied = viewer.post("/lead/new", data={
            "csrf_token": self.csrf(viewer), "owner_name": "No Write",
            "phone": "314-555-0199", "status": "new",
        })
        self.assertEqual(denied.status_code, 403)

    def test_organization_boundaries(self):
        self.setup_owner()
        lead_one = self.db.ensure_lead("3145550101", "(314) 555-0101", 1)
        with self.db.connect() as connection:
            connection.execute("INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Other Co','other-co','2026-01-01')")
            connection.execute(
                """INSERT INTO users(organization_id,email,display_name,password_hash,role,created_at)
                   VALUES (2,?,?,?,?,?)""",
                ("other@example.com", "Other Owner", generate_password_hash("other password 123"), "owner", "2026-01-01"),
            )
        lead_two = self.db.ensure_lead("2125550202", "(212) 555-0202", 2)
        owner_page = self.client.get("/")
        self.assertIn(b"(314) 555-0101", owner_page.data)
        self.assertNotIn(b"(212) 555-0202", owner_page.data)
        self.assertEqual(self.client.get(f"/lead/{lead_two}").status_code, 404)
        other = self.app.test_client()
        self.assertEqual(self.login(other, "other@example.com", "other password 123").status_code, 302)
        other_page = other.get("/")
        self.assertIn(b"(212) 555-0202", other_page.data)
        self.assertNotIn(b"(314) 555-0101", other_page.data)
        self.assertEqual(other.get(f"/lead/{lead_one}").status_code, 404)

    def test_login_rate_limit(self):
        self.setup_owner()
        self.client.post("/logout", data={"csrf_token": self.csrf(self.client)})
        for _ in range(5):
            response = self.login(self.client, "owner@example.com", "wrong password")
            self.assertEqual(response.status_code, 401)
        response = self.login(self.client, "owner@example.com", "wrong password")
        self.assertEqual(response.status_code, 429)


class PhaseTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Phase Owner",
            "email": "phase@example.com", "password": "phase two password 123",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_contact_metadata_tags_custom_fields_and_smart_list(self):
        lead_id = self.db.ensure_lead("3145550101", "(314) 555-0101")
        with self.db.connect() as connection:
            connection.execute("UPDATE leads SET owner_name='Mary Seller',status='qualified' WHERE id=?", (lead_id,))
        contact_id = self.db.sync_contact_for_lead(lead_id)
        tag_response = self.client.post("/settings/fields-tags", data={
            "csrf_token": self.csrf(), "action": "tag", "name": "Warm seller", "color": "#d67545",
        })
        self.assertEqual(tag_response.status_code, 302)
        field_response = self.client.post("/settings/fields-tags", data={
            "csrf_token": self.csrf(), "action": "field", "label": "Seller temperature",
            "entity_type": "contact", "data_type": "choice", "options": "Hot, Warm, Cold",
        })
        self.assertEqual(field_response.status_code, 302)
        with self.db.connect() as connection:
            tag_id = connection.execute("SELECT id FROM tags").fetchone()[0]
            field_id = connection.execute("SELECT id FROM custom_field_definitions").fetchone()[0]
        saved = self.client.post(f"/contacts/{contact_id}", data={
            "csrf_token": self.csrf(), "full_name": "Maya Sample",
            "primary_email": "mary@example.com", "mailing_address": "PO Box 1",
            "tag_ids": str(tag_id), f"field_{field_id}": "Warm",
        })
        self.assertEqual(saved.status_code, 302)
        view = self.client.post("/contacts/filters", data={
            "csrf_token": self.csrf(), "name": "Qualified warm sellers",
            "status": "qualified", "tag": str(tag_id), "shared": "1",
        })
        self.assertEqual(view.status_code, 302)
        page = self.client.get("/contacts")
        self.assertIn(b"Maya Sample", page.data)
        self.assertIn(b"Qualified warm sellers", page.data)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entity_tags").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT value_json FROM custom_field_values").fetchone()[0], '"Warm"')

    def test_csv_preview_validation_commit_errors_and_retry_safety(self):
        payload = (
            "Name,Phone,Email,Reference,County,Tags\n"
            "Mary Import,314-555-0144,mary@example.com,DEMO-2612-1,Putnam,Hot\n"
            "Broken Row,not-a-phone,bad-email,,,\n"
        ).encode()
        uploaded = self.client.post("/imports/upload", data={
            "csrf_token": self.csrf(), "file": (io.BytesIO(payload), "contacts.csv"),
        }, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 302)
        with self.db.connect() as connection:
            batch_id = connection.execute("SELECT id FROM import_batches").fetchone()[0]
        validated = self.client.post(f"/imports/{batch_id}/validate", data={
            "csrf_token": self.csrf(), "map_0": "full_name", "map_1": "phone",
            "map_2": "email", "map_3": "reference_number", "map_4": "county",
            "map_5": "tags", "duplicate_policy": "update",
        })
        self.assertEqual(validated.status_code, 302)
        with self.db.connect() as connection:
            batch = connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        self.assertEqual((batch["valid_rows"], batch["error_rows"]), (1, 1))
        committed = self.client.post(f"/imports/{batch_id}/commit", data={
            "csrf_token": self.csrf(), "confirmed": "yes",
        })
        self.assertEqual(committed.status_code, 302)
        retry = self.client.post(f"/imports/{batch_id}/commit", data={
            "csrf_token": self.csrf(), "confirmed": "yes",
        })
        self.assertEqual(retry.status_code, 302)
        with self.db.connect() as connection:
            batch = connection.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
            contact = connection.execute("SELECT * FROM contacts WHERE primary_phone_normalized='3145550144'").fetchone()
            property_row = connection.execute("SELECT * FROM properties WHERE reference_number='DEMO-2612-1'").fetchone()
            tag = connection.execute("SELECT * FROM tags WHERE name='Hot'").fetchone()
        self.assertEqual(batch["status"], "committed")
        self.assertEqual(batch["created_rows"], 1)
        self.assertEqual(contact["full_name"], "Mary Import")
        self.assertEqual(property_row["county"], "Putnam")
        self.assertIsNotNone(tag)
        properties_page = self.client.get("/properties")
        self.assertEqual(properties_page.status_code, 200)
        self.assertIn(b"DEMO-2612-1", properties_page.data)
        property_page = self.client.get(f"/properties/{property_row['id']}")
        self.assertEqual(property_page.status_code, 200)
        self.assertIn(b"Putnam", property_page.data)

    def test_phase_two_views_are_tenant_scoped(self):
        lead_one = self.db.ensure_lead("3145550101", "(314) 555-0101", 1)
        self.db.sync_contact_for_lead(lead_one)
        with self.db.connect() as connection:
            connection.execute("INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Other','other','2026-01-01')")
        lead_two = self.db.ensure_lead("2125550202", "(212) 555-0202", 2)
        contact_two = self.db.sync_contact_for_lead(lead_two)
        page = self.client.get("/contacts")
        self.assertIn(b"(314) 555-0101", page.data)
        self.assertNotIn(b"(212) 555-0202", page.data)
        self.assertEqual(self.client.get(f"/contacts/{contact_two}").status_code, 404)


class PhaseThreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Pipeline Owner",
            "email": "pipeline@example.com", "password": "phase three password 123",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_backfill_board_detail_and_audited_transition(self):
        lead_id = self.db.ensure_lead("3145550188", "(314) 555-0188")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE leads SET owner_name='Board Seller',distress_signal='Inherited',seller_motivation='Quick close' WHERE id=?",
                (lead_id,),
            )
        self.db.sync_contact_for_lead(lead_id)
        board = self.client.get("/deals")
        self.assertEqual(board.status_code, 200)
        self.assertIn(b"Board Seller", board.data)
        self.assertIn(b"Table view", board.data)
        with self.db.connect() as connection:
            deal = connection.execute("SELECT * FROM opportunities WHERE lead_id=?", (lead_id,)).fetchone()
            qualified = connection.execute(
                "SELECT id FROM pipeline_stages WHERE pipeline_id=? AND stage_key='qualified'",
                (deal["pipeline_id"],),
            ).fetchone()
        moved = self.client.post(f"/deals/{deal['id']}/stage", data={
            "csrf_token": self.csrf(), "stage_id": qualified["id"],
            "expected_version": deal["version"], "note": "Seller confirmed discount",
        })
        self.assertEqual(moved.status_code, 302)
        detail = self.client.get(f"/deals/{deal['id']}")
        self.assertIn(b"Qualified", detail.data)
        self.assertIn(b"Seller confirmed discount", detail.data)
        self.assertIn(b"Inherited", detail.data)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0], "qualified")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stage_transitions WHERE opportunity_id=?", (deal["id"],)).fetchone()[0], 1)
            actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
        self.assertIn("opportunity.stage_change", actions)

    def test_pipeline_configuration_concurrency_and_tenant_isolation(self):
        self.db.ensure_lead("3145550101", "(314) 555-0101", 1)
        created = self.client.post("/settings/pipelines", data={
            "csrf_token": self.csrf(), "action": "pipeline",
            "name": "Dispositions", "kind": "disposition",
        })
        self.assertEqual(created.status_code, 302)
        with self.db.connect() as connection:
            pipeline = connection.execute("SELECT * FROM pipelines WHERE name='Dispositions'").fetchone()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM pipeline_stages WHERE pipeline_id=?", (pipeline["id"],)).fetchone()[0], 3)
            connection.execute("INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Other Org','other-org','2026-01-01')")
        other_lead = self.db.ensure_lead("2125550199", "(212) 555-0199", 2)
        owner_board = self.client.get("/deals")
        self.assertNotIn(b"(212) 555-0199", owner_board.data)
        with self.db.connect() as connection:
            other_deal = connection.execute("SELECT id FROM opportunities WHERE lead_id=?", (other_lead,)).fetchone()
            own_deal = connection.execute("SELECT * FROM opportunities WHERE organization_id=1 ORDER BY id LIMIT 1").fetchone()
            target = connection.execute("SELECT id FROM pipeline_stages WHERE pipeline_id=? AND id!=? ORDER BY position LIMIT 1", (own_deal["pipeline_id"], own_deal["stage_id"])).fetchone()
        self.assertEqual(self.client.get(f"/deals/{other_deal['id']}").status_code, 404)
        stale = self.client.post(f"/deals/{own_deal['id']}/stage", data={
            "csrf_token": self.csrf(), "stage_id": target["id"],
            "expected_version": own_deal["version"] + 1, "response": "json",
        })
        self.assertEqual(stale.status_code, 409)
        self.assertIn(b"another tab", stale.data)


class PhaseFourTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Inbox Owner",
            "email": "inbox@example.com", "password": "phase four password 123",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_unified_inbox_full_transcript_filters_and_immutability(self):
        lead_id = self.db.ensure_lead("3145550177", "(314) 555-0177")
        with self.db.connect() as connection:
            connection.execute("UPDATE leads SET owner_name='Inbox Seller',reference_number='DEMO-2612-1' WHERE id=?", (lead_id,))
        self.db.sync_contact_for_lead(lead_id)
        transcript = "Full seller story " + ("with all transcript words " * 30)
        self.db.add_call(lead_id, {
            "message_id": "inbox-call", "source_key": "inbox-call:1",
            "direction": "incoming", "timestamp_iso": "2026-09-22T14:00:00+00:00",
            "transcript": transcript,
        })
        inbox = self.client.get("/inbox")
        self.assertEqual(inbox.status_code, 200)
        self.assertIn(b"Inbox Seller", inbox.data)
        conversation = self.client.get(f"/inbox/{lead_id}?channel=call")
        self.assertIn(transcript.encode(), conversation.data)
        self.assertIn(b"DEMO-2612-1", conversation.data)
        self.assertIn(b"max-height:none", conversation.data)
        activity = self.client.get("/activity?channel=call&direction=incoming")
        self.assertIn(b"Incoming call", activity.data)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connect() as connection:
                connection.execute("UPDATE activity_events SET title='Changed' WHERE source_table='calls'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.connect() as connection:
                connection.execute("DELETE FROM activity_events WHERE source_table='calls'")

    def test_stage_audit_backfill_pagination_and_tenant_isolation(self):
        lead_id = self.db.ensure_lead("3145550111", "(314) 555-0111")
        with self.db.connect() as connection:
            deal = connection.execute("SELECT * FROM opportunities WHERE lead_id=?", (lead_id,)).fetchone()
            target = connection.execute("SELECT id FROM pipeline_stages WHERE pipeline_id=? AND stage_key='contacted'", (deal["pipeline_id"],)).fetchone()
        moved = self.client.post(f"/deals/{deal['id']}/stage", data={
            "csrf_token": self.csrf(), "stage_id": target["id"], "expected_version": deal["version"],
        })
        self.assertEqual(moved.status_code, 302)
        system = self.client.get(f"/inbox/{lead_id}?channel=system")
        self.assertIn(b"Stage changed", system.data)
        self.assertIn(b"Audit: opportunity stage_change", system.data)
        with self.db.connect() as connection:
            contact = connection.execute("SELECT id FROM contacts WHERE lead_id=?", (lead_id,)).fetchone()
            opportunity = connection.execute("SELECT id FROM opportunities WHERE lead_id=?", (lead_id,)).fetchone()
            for index in range(105):
                connection.execute("""INSERT INTO activity_events(
                    organization_id,lead_id,contact_id,opportunity_id,event_type,channel,direction,
                    occurred_at,title,body,source_table,source_id,created_at
                ) VALUES (1,?,?,?,?, 'note','internal',?,?,?,'test_notes',?,?)""", (
                    lead_id, contact["id"], opportunity["id"], "note",
                    f"2026-09-23T12:{index % 60:02d}:00+00:00", f"Test note {index}", "body", index,
                    "2026-09-23T12:00:00+00:00",
                ))
            connection.execute("INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Private Org','private-org','2026-01-01')")
        other_lead = self.db.ensure_lead("2125550100", "(212) 555-0100", 2)
        self.db.add_call(other_lead, {"message_id": "private", "source_key": "private:1", "transcript": "PRIVATE EVENT"})
        first_page = self.client.get("/activity?channel=note")
        second_page = self.client.get("/activity?channel=note&page=2")
        self.assertIn(b"Older", first_page.data)
        self.assertIn(b"Page 2", second_page.data)
        self.assertNotIn(b"PRIVATE EVENT", self.client.get("/activity").data)
        self.assertEqual(self.client.get(f"/inbox/{other_lead}").status_code, 404)


class PhaseFiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Workflow Owner",
            "email": "workflow@example.com", "password": "phase five password 123",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_disabled_templates_publish_worker_and_idempotent_safe_actions(self):
        page = self.client.get("/automations")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Disabled templates", page.data)
        self.assertIn(b"They cannot send a text", page.data)
        with self.db.connect() as connection:
            templates = connection.execute("SELECT * FROM workflow_definitions WHERE is_template=1").fetchall()
        self.assertEqual(len(templates), 4)
        self.assertTrue(all(not item["enabled"] and item["published_version"] is None for item in templates))

        created = self.client.post("/automations/new", data={"csrf_token": self.csrf()})
        self.assertEqual(created.status_code, 302)
        workflow_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        saved = self.client.post(f"/automations/{workflow_id}", data={
            "csrf_token": self.csrf(), "name": "Review and draft for new leads",
            "description": "Safe follow-up", "trigger_type": "lead_created",
            "stage_key": "", "min_score": "", "condition_status": "",
            "delay_amount": "0", "delay_unit": "minutes",
            "action_0_type": "create_task", "action_0_content": "Review {name} at {phone}",
            "action_0_angle": "", "action_1_type": "prepare_sms_draft",
            "action_1_content": "Hi {first_name}, are you available to discuss {reference}?",
            "action_1_angle": "New lead introduction",
        })
        self.assertEqual(saved.status_code, 302)
        published = self.client.post(f"/automations/{workflow_id}/publish", data={
            "csrf_token": self.csrf(), "confirmed": "yes",
        })
        self.assertEqual(published.status_code, 302)
        lead_id = self.db.ensure_lead("3145550166", "(314) 555-0166")
        with self.db.connect() as connection:
            connection.execute("UPDATE leads SET owner_name='Automation Seller',reference_number='DEMO-2612-1' WHERE id=?", (lead_id,))
        self.db.sync_contact_for_lead(lead_id)
        engine = WorkflowEngine(self.db)
        engine.process_once(100)
        engine.process_once(100)
        with self.db.connect() as connection:
            workflow = connection.execute("SELECT * FROM workflow_definitions WHERE id=?", (workflow_id,)).fetchone()
            runs = connection.execute("SELECT * FROM workflow_runs WHERE workflow_id=?", (workflow_id,)).fetchall()
            tasks = connection.execute("SELECT * FROM tasks WHERE lead_id=? AND source='workflow'", (lead_id,)).fetchall()
            drafts = connection.execute("SELECT * FROM message_drafts WHERE lead_id=? AND provider='workflow'", (lead_id,)).fetchall()
            sends = connection.execute("SELECT COUNT(*) FROM communications WHERE lead_id=?", (lead_id,)).fetchone()[0]
        self.assertEqual((workflow["published_version"], workflow["enabled"]), (1, 1))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "succeeded")
        self.assertEqual(len(tasks), 1)
        self.assertIn("Automation Seller", tasks[0]["title"])
        self.assertEqual(len(drafts), 1)
        self.assertIn("DEMO-2612-1", drafts[0]["message"])
        self.assertEqual(sends, 0)

    def test_delays_failures_and_manual_retry(self):
        lead_id = self.db.ensure_lead("3145550190", "(314) 555-0190")
        with self.db.connect() as connection:
            workflow = connection.execute("SELECT id FROM workflow_definitions WHERE is_template=1 LIMIT 1").fetchone()
            run = connection.execute("""INSERT INTO workflow_runs(
                organization_id,workflow_id,workflow_version,trigger_topic,trigger_event_id,
                lead_id,idempotency_key,status,created_at
            ) VALUES (1,?,1,'test',1,?,'failure-test','queued',?)""", (workflow["id"], lead_id, "2026-01-01")).lastrowid
            connection.execute("""INSERT INTO workflow_jobs(
                organization_id,run_id,action_index,action_type,payload_json,idempotency_key,
                status,available_at,created_at,updated_at
            ) VALUES (1,?,0,'create_task','{}','failure-job','queued','2020-01-01','2020-01-01','2020-01-01')""", (run,))
        engine = WorkflowEngine(self.db)
        for _ in range(3):
            engine.process_jobs(10)
            with self.db.connect() as connection:
                connection.execute("UPDATE workflow_jobs SET available_at='2020-01-01' WHERE run_id=?", (run,))
        with self.db.connect() as connection:
            job = connection.execute("SELECT * FROM workflow_jobs WHERE run_id=?", (run,)).fetchone()
        self.assertEqual(job["status"], "dead")
        failures = self.client.get("/automations")
        self.assertIn(b"Retry", failures.data)
        retried = self.client.post(f"/automations/runs/{run}/retry", data={"csrf_token": self.csrf()})
        self.assertEqual(retried.status_code, 302)
        with self.db.connect() as connection:
            job = connection.execute("SELECT * FROM workflow_jobs WHERE run_id=?", (run,)).fetchone()
        self.assertEqual((job["status"], job["attempts"]), ("queued", 0))


class PhaseSixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Docs Owner",
            "email": "docs@example.com", "password": "phase six password 123",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def _make_deal(self, phone="3145550177", name="Document Seller"):
        lead_id = self.db.ensure_lead(phone, f"({phone[:3]}) {phone[3:6]}-{phone[6:]}")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE leads SET owner_name=?,reference_number='DEMO-2612-9' WHERE id=?",
                (name, lead_id),
            )
        self.db.sync_contact_for_lead(lead_id)
        with self.db.connect() as connection:
            return lead_id, connection.execute(
                "SELECT id FROM opportunities WHERE lead_id=?", (lead_id,)
            ).fetchone()[0]

    def _publish_template(self, body=None):
        with self.db.connect() as connection:
            template = connection.execute(
                "SELECT id FROM document_templates WHERE organization_id=1 LIMIT 1"
            ).fetchone()
            template_id = template["id"]
            if body:
                connection.execute(
                    "UPDATE document_templates SET draft_body=? WHERE id=?",
                    (body, template_id),
                )
        published = self.client.post(
            f"/documents/templates/{template_id}/publish",
            data={"csrf_token": self.csrf(), "confirmed": "yes"},
        )
        self.assertEqual(published.status_code, 302)
        return template_id

    def test_template_draft_publish_and_version_immutability(self):
        page = self.client.get("/documents")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Starter: Vacant Land Purchase Agreement", page.data)
        self.assertIn(b"not legal advice", page.data)

        template_id = self._publish_template()
        with self.db.connect() as connection:
            template = connection.execute(
                "SELECT * FROM document_templates WHERE id=?", (template_id,)
            ).fetchone()
            version = connection.execute(
                "SELECT * FROM document_template_versions WHERE template_id=?",
                (template_id,),
            ).fetchone()
        self.assertEqual(template["published_version"], 1)
        self.assertEqual(version["version"], 1)
        self.assertIn("seller_name", json.loads(version["merge_fields_json"]))

        # Editing the draft must not change the published version.
        edited = self.client.post(f"/documents/templates/{template_id}", data={
            "csrf_token": self.csrf(), "name": "Renamed template",
            "description": "Updated", "body": "DOCUMENT TITLE\n\nSeller: {{ seller_name }}\n\nReviewed language for the new draft that is long enough.",
        })
        self.assertEqual(edited.status_code, 302)
        with self.db.connect() as connection:
            version = connection.execute(
                "SELECT body FROM document_template_versions WHERE template_id=? AND version=1",
                (template_id,),
            ).fetchone()
            template = connection.execute(
                "SELECT published_version FROM document_templates WHERE id=?", (template_id,)
            ).fetchone()
        self.assertIn("VACANT LAND PURCHASE AGREEMENT", version["body"])
        self.assertEqual(template["published_version"], 1)

        # Unknown merge fields are rejected.
        bad = self.client.post(f"/documents/templates/{template_id}", data={
            "csrf_token": self.csrf(), "name": "Bad", "description": "",
            "body": "Hello {{ free_text_field }} this body is definitely long enough to pass.",
        })
        self.assertIn(b"Unknown merge fields", bad.data)

    def test_document_lifecycle_generate_send_sign_and_events(self):
        lead_id, deal_id = self._make_deal()
        template_id = self._publish_template()
        created = self.client.post(f"/deals/{deal_id}/documents/new", data={
            "csrf_token": self.csrf(), "template_id": str(template_id),
            "title": "Purchase Agreement - Document Seller",
            "recipient_name": "Document Seller",
            "recipient_email": "seller@example.com",
        })
        self.assertEqual(created.status_code, 302)
        document_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])

        detail = self.client.get(f"/documents/{document_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Merged preview", detail.data)
        self.assertIn(b"Document Seller", detail.data)

        # Generate requires confirmation and produces a hashed PDF.
        missing_confirm = self.client.post(f"/documents/{document_id}/generate", data={
            "csrf_token": self.csrf(),
        })
        self.assertEqual(missing_confirm.status_code, 400)
        generated = self.client.post(f"/documents/{document_id}/generate", data={
            "csrf_token": self.csrf(), "confirmed": "yes",
        })
        self.assertEqual(generated.status_code, 302)
        with self.db.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            files = connection.execute(
                "SELECT * FROM document_files WHERE document_id=?", (document_id,)
            ).fetchall()
            events = [row["event_type"] for row in connection.execute(
                "SELECT event_type FROM document_events WHERE document_id=? ORDER BY id",
                (document_id,),
            )]
        self.assertEqual(document["status"], "generated")
        self.assertTrue(document["artifact_sha256"])
        self.assertEqual(len(files), 1)
        self.assertEqual(events, ["created", "generated"])
        self.assertTrue((Path(self.temp.name) / document["artifact_path"]).is_file())

        # Send records the manual provider state without transmitting email.
        sent = self.client.post(f"/documents/{document_id}/send", data={
            "csrf_token": self.csrf(), "confirmed": "yes",
            "recipient_email": "seller@example.com",
        })
        self.assertEqual(sent.status_code, 302)
        with self.db.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
        self.assertEqual(document["status"], "sent")
        self.assertTrue(document["provider_envelope_id"])

        # Uploading a signed PDF validates, hashes, and completes the document.
        signed_pdf = Path(self.temp.name) / "signed.pdf"
        digest, _pages = generate_pdf(signed_pdf_path := signed_pdf, "Signed", "Signed agreement body for testing purposes.")
        uploaded = self.client.post(f"/documents/{document_id}/signed-file", data={
            "csrf_token": self.csrf(), "confirmed": "yes",
            "file": (io.BytesIO(signed_pdf.read_bytes()), "signed.pdf"),
        }, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 302)
        with self.db.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            events = [row["event_type"] for row in connection.execute(
                "SELECT event_type FROM document_events WHERE document_id=? ORDER BY id",
                (document_id,),
            )]
            signed_files = connection.execute(
                "SELECT * FROM document_files WHERE document_id=? AND kind='signed'",
                (document_id,),
            ).fetchall()
        self.assertEqual(document["status"], "completed")
        self.assertTrue(document["completed_at"])
        self.assertEqual(events[-2:], ["signed", "completed"])
        self.assertEqual(len(signed_files), 1)
        self.assertEqual(signed_files[0]["sha256"], digest)

        # Download works and activity was projected into the unified timeline.
        downloaded = self.client.get(
            f"/documents/{document_id}/files/{signed_files[0]['id']}"
        )
        self.assertEqual(downloaded.status_code, 200)
        downloaded.close()
        with self.db.connect() as connection:
            activity = connection.execute(
                "SELECT COUNT(*) FROM activity_events WHERE source_table='document_events' AND lead_id=?",
                (lead_id,),
            ).fetchone()[0]
        self.assertGreaterEqual(activity, 4)

        # Document events are immutable.
        with self.db.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM document_events")

    def test_document_tenant_isolation_and_webhook_signature(self):
        lead_id, deal_id = self._make_deal()
        template_id = self._publish_template()
        created = self.client.post(f"/deals/{deal_id}/documents/new", data={
            "csrf_token": self.csrf(), "template_id": str(template_id),
            "title": "Isolation check", "recipient_email": "seller@example.com",
        })
        document_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Private Org','private-org','2026-01-01')"
            )
        self.assertEqual(self.client.get("/documents/2").status_code, 404)

        # Webhook requires configuration and a valid HMAC signature.
        os.environ.pop("CRM_ESIGN_WEBHOOK_SECRET", None)
        unconfigured = self.client.post("/webhooks/esign/acme", json={
            "event_id": "e1", "envelope_id": "env1", "event_type": "viewed",
        })
        self.assertEqual(unconfigured.status_code, 503)

        os.environ["CRM_ESIGN_WEBHOOK_SECRET"] = "test-secret"
        try:
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE documents SET provider='acme',provider_envelope_id='env1' WHERE id=?",
                    (document_id,),
                )
            payload = json.dumps({
                "event_id": "evt-1", "envelope_id": "env1", "event_type": "viewed",
            }).encode()
            signature = hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()
            bad = self.client.post(
                "/webhooks/esign/acme", data=payload,
                headers={"X-CRM-Signature": "bad"}, content_type="application/json",
            )
            self.assertEqual(bad.status_code, 401)
            good = self.client.post(
                "/webhooks/esign/acme", data=payload,
                headers={"X-CRM-Signature": signature}, content_type="application/json",
            )
            self.assertEqual(good.status_code, 200)
            duplicate = self.client.post(
                "/webhooks/esign/acme", data=payload,
                headers={"X-CRM-Signature": signature}, content_type="application/json",
            )
            self.assertEqual(duplicate.status_code, 200)
            self.assertTrue(duplicate.get_json().get("duplicate"))
            with self.db.connect() as connection:
                document = connection.execute(
                    "SELECT status FROM documents WHERE id=?", (document_id,)
                ).fetchone()
                events = connection.execute(
                    "SELECT COUNT(*) FROM document_events WHERE document_id=? AND provider='acme'",
                    (document_id,),
                ).fetchone()[0]
            self.assertEqual(document["status"], "viewed")
            self.assertEqual(events, 1)
        finally:
            os.environ.pop("CRM_ESIGN_WEBHOOK_SECRET", None)


class PhaseSevenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Calendar Owner",
            "email": "calendar@example.com", "password": "phase seven password 12",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def _calendar_id(self):
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT id FROM calendars WHERE organization_id=1 LIMIT 1"
            ).fetchone()[0]

    def _next_monday(self):
        today = date.today()
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)

    def test_default_calendar_seeded_and_month_grid_renders(self):
        with self.db.connect() as connection:
            calendars = connection.execute("SELECT * FROM calendars").fetchall()
            windows = connection.execute("SELECT * FROM calendar_availability").fetchall()
        self.assertEqual(len(calendars), 1)
        self.assertEqual(calendars[0]["name"], "Appointments")
        self.assertEqual(len(windows), 5)
        page = self.client.get("/calendar")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Appointments", page.data)
        self.assertIn(b"Mon", page.data)

    def test_appointment_lifecycle_validation_and_activity(self):
        lead_id = self.db.ensure_lead("3145550211", "(314) 555-0211")
        with self.db.connect() as connection:
            connection.execute("UPDATE leads SET owner_name='Walkthrough Seller' WHERE id=?", (lead_id,))
        self.db.sync_contact_for_lead(lead_id)
        calendar_id = self._calendar_id()
        monday = self._next_monday()

        created = self.client.post("/calendar/appointments/new", data={
            "csrf_token": self.csrf(), "calendar_id": str(calendar_id),
            "title": "Property walkthrough", "date": monday.isoformat(),
            "start_time": "10:00", "end_time": "11:00",
            "lead_id": str(lead_id), "location": "Parcel gate", "notes": "Bring offer letter",
        })
        self.assertEqual(created.status_code, 302)
        appointment_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])

        detail = self.client.get(f"/calendar/appointments/{appointment_id}")
        self.assertIn(b"Property walkthrough", detail.data)
        self.assertIn(b"Parcel gate", detail.data)

        with self.db.connect() as connection:
            appointment = connection.execute(
                "SELECT * FROM appointments WHERE id=?", (appointment_id,)
            ).fetchone()
            activity = connection.execute(
                "SELECT COUNT(*) FROM activity_events WHERE source_table='appointments' AND lead_id=?",
                (lead_id,),
            ).fetchone()[0]
        self.assertEqual(appointment["status"], "scheduled")
        self.assertEqual(appointment["booked_by"], "Calendar Owner")
        self.assertGreaterEqual(activity, 1)

        # End before start is rejected.
        bad = self.client.post("/calendar/appointments/new", data={
            "csrf_token": self.csrf(), "calendar_id": str(calendar_id),
            "title": "Backwards", "date": monday.isoformat(),
            "start_time": "11:00", "end_time": "10:00",
        })
        self.assertIn(b"End must be after start", bad.data)

        # Status transitions are audited and projected to the timeline.
        completed = self.client.post(f"/calendar/appointments/{appointment_id}/status", data={
            "csrf_token": self.csrf(), "status": "completed",
        })
        self.assertEqual(completed.status_code, 302)
        with self.db.connect() as connection:
            status = connection.execute(
                "SELECT status FROM appointments WHERE id=?", (appointment_id,)
            ).fetchone()[0]
            actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
        self.assertEqual(status, "completed")
        self.assertIn("appointment.status", actions)

        # Editing works and unknown appointments 404.
        edited = self.client.post(f"/calendar/appointments/{appointment_id}/edit", data={
            "csrf_token": self.csrf(), "calendar_id": str(calendar_id),
            "title": "Walkthrough rescheduled", "date": monday.isoformat(),
            "start_time": "14:00", "end_time": "15:00", "lead_id": str(lead_id),
        })
        self.assertEqual(edited.status_code, 302)
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Private Org','private-org','2026-01-01')"
            )
        self.assertEqual(self.client.get("/calendar/appointments/999").status_code, 404)

    def test_availability_windows_and_slot_api(self):
        calendar_id = self._calendar_id()
        monday = self._next_monday()
        added = self.client.post("/calendar/availability", data={
            "csrf_token": self.csrf(), "calendar_id": str(calendar_id),
            "weekday": str(monday.weekday()), "start_time": "09:00", "end_time": "12:00",
        })
        self.assertEqual(added.status_code, 302)
        slots = self.client.get(
            f"/api/v1/calendar/available-slots?calendar_id={calendar_id}&date={monday.isoformat()}&duration_minutes=60"
        )
        self.assertEqual(slots.status_code, 200)
        data = slots.get_json()
        self.assertTrue(any(slot.endswith("09:00") for slot in data["slots"]))

        # Book the first slot; it must disappear from availability.
        booked = self.client.post("/calendar/appointments/new", data={
            "csrf_token": self.csrf(), "calendar_id": str(calendar_id),
            "title": "Seller call", "date": monday.isoformat(),
            "start_time": "09:00", "end_time": "10:00",
        })
        self.assertEqual(booked.status_code, 302)
        after = self.client.get(
            f"/api/v1/calendar/available-slots?calendar_id={calendar_id}&date={monday.isoformat()}&duration_minutes=60"
        ).get_json()
        self.assertFalse(any(slot.endswith("09:00") for slot in after["slots"]))
        self.assertTrue(any(slot.endswith("10:00") for slot in after["slots"]))

        # A day without windows returns no slots.
        sunday = monday + timedelta(days=(6 - monday.weekday()) % 7 or 7)
        empty = self.client.get(
            f"/api/v1/calendar/available-slots?calendar_id={calendar_id}&date={sunday.isoformat()}"
        )
        self.assertEqual(empty.get_json()["slots"], [])

        # Invalid slot request is rejected.
        self.assertEqual(self.client.get(
            f"/api/v1/calendar/available-slots?calendar_id={calendar_id}&date=not-a-date"
        ).status_code, 400)


class PhaseEightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Projects Owner",
            "email": "projects@example.com", "password": "phase eight password 1",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def _deal_id(self):
        lead_id = self.db.ensure_lead("3145550233", "(314) 555-0233")
        with self.db.connect() as connection:
            connection.execute("UPDATE leads SET owner_name='Project Seller' WHERE id=?", (lead_id,))
        self.db.sync_contact_for_lead(lead_id)
        with self.db.connect() as connection:
            return lead_id, connection.execute(
                "SELECT id FROM opportunities WHERE lead_id=?", (lead_id,)
            ).fetchone()[0]

    def test_project_from_template_board_and_progress(self):
        lead_id, deal_id = self._deal_id()
        created = self.client.post("/projects/from-template", data={
            "csrf_token": self.csrf(), "template_key": "due_diligence",
            "name": "Parcel 9 due diligence", "opportunity_id": str(deal_id),
        })
        self.assertEqual(created.status_code, 302)
        project_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])

        with self.db.connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            tasks = connection.execute(
                "SELECT * FROM project_tasks WHERE project_id=? ORDER BY position", (project_id,)
            ).fetchall()
        self.assertEqual(project["template_key"], "due_diligence")
        self.assertEqual(project["lead_id"], lead_id)
        self.assertEqual(len(tasks), 8)
        self.assertTrue(all(t["status"] == "todo" for t in tasks))

        detail = self.client.get(f"/projects/{project_id}")
        self.assertIn(b"Parcel 9 due diligence", detail.data)
        self.assertIn(b"Order title search", detail.data)
        self.assertIn(b"In progress", detail.data)

        # Move a task through the board.
        first_task = tasks[0]["id"]
        moved = self.client.post(f"/projects/{project_id}/tasks/{first_task}/status", data={
            "csrf_token": self.csrf(), "status": "done",
        })
        self.assertEqual(moved.status_code, 302)
        with self.db.connect() as connection:
            status = connection.execute(
                "SELECT status FROM project_tasks WHERE id=?", (first_task,)
            ).fetchone()[0]
            actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
        self.assertEqual(status, "done")
        self.assertIn("project.task_status", actions)

        # Comments work and are audited.
        commented = self.client.post(f"/projects/{project_id}/comments", data={
            "csrf_token": self.csrf(), "body": "Title company confirmed clean chain.",
        })
        self.assertEqual(commented.status_code, 302)
        page = self.client.get(f"/projects/{project_id}")
        self.assertIn(b"clean chain", page.data)

        # Project status transitions are audited.
        held = self.client.post(f"/projects/{project_id}/status", data={
            "csrf_token": self.csrf(), "status": "on_hold",
        })
        self.assertEqual(held.status_code, 302)
        with self.db.connect() as connection:
            status = connection.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        self.assertEqual(status, "on_hold")

    def test_blank_project_tenant_isolation_and_validation(self):
        lead_id, deal_id = self._deal_id()
        created = self.client.post("/projects/new", data={
            "csrf_token": self.csrf(), "name": "Custom workspace",
            "description": "Ad hoc", "opportunity_id": str(deal_id),
        })
        self.assertEqual(created.status_code, 302)
        project_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        with self.db.connect() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            connection.execute(
                "INSERT INTO organizations(id,name,slug,created_at) VALUES (2,'Private Org','private-org','2026-01-01')"
            )
        self.assertEqual(project["opportunity_id"], deal_id)
        self.assertEqual(self.client.get("/projects/999").status_code, 404)

        # Add a task, then verify status validation rejects unknown columns.
        added = self.client.post(f"/projects/{project_id}/tasks", data={
            "csrf_token": self.csrf(), "title": "First task",
        })
        self.assertEqual(added.status_code, 302)
        with self.db.connect() as connection:
            task_id = connection.execute(
                "SELECT id FROM project_tasks WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        bad = self.client.post(f"/projects/{project_id}/tasks/{task_id}/status", data={
            "csrf_token": self.csrf(), "status": "exploded",
        })
        self.assertEqual(bad.status_code, 400)

        # Empty comment is rejected.
        self.assertEqual(self.client.post(f"/projects/{project_id}/comments", data={
            "csrf_token": self.csrf(), "body": "   ",
        }).status_code, 400)


class PhaseNineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Comms Owner",
            "email": "comms@example.com", "password": "phase nine password 12",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_settings_page_number_inventory_and_toggle(self):
        page = self.client.get("/settings/communication")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Number inventory", page.data)
        self.assertIn(b"Consent and suppression", page.data)

        added = self.client.post("/settings/communication/numbers", data={
            "csrf_token": self.csrf(), "value": "+13145550123",
            "label": "Main line", "kind": "both", "provider": "vumber",
        })
        self.assertEqual(added.status_code, 302)
        page = self.client.get("/settings/communication")
        self.assertIn(b"+13145550123", page.data)
        self.assertIn(b"Main line", page.data)

        with self.db.connect() as connection:
            number_id = connection.execute("SELECT id FROM phone_numbers").fetchone()[0]
        toggled = self.client.post(f"/settings/communication/numbers/{number_id}/toggle", data={
            "csrf_token": self.csrf(),
        })
        self.assertEqual(toggled.status_code, 302)
        with self.db.connect() as connection:
            active = connection.execute(
                "SELECT active FROM phone_numbers WHERE id=?", (number_id,)
            ).fetchone()[0]
        self.assertEqual(active, 0)

        # Unknown kind is rejected.
        self.assertEqual(self.client.post("/settings/communication/numbers", data={
            "csrf_token": self.csrf(), "value": "+13145550999", "kind": "fax",
        }).status_code, 400)

    def test_suppression_blocks_sms_and_lifting_restores(self):
        lead_id = self.db.ensure_lead("3145550255", "(314) 555-0255")
        suppressed = self.client.post("/settings/communication/suppressions", data={
            "csrf_token": self.csrf(), "lead_id": str(lead_id),
            "channel": "all", "reason": "Do not contact request",
        })
        self.assertEqual(suppressed.status_code, 302)
        page = self.client.get("/settings/communication")
        self.assertIn(b"Do not contact request", page.data)
        self.assertIn(b"Suppressed", page.data)

        with self.db.connect() as connection:
            actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
        self.assertIn("communication.suppress", actions)

        # The guard blocks SMS for the suppressed lead.
        with self.app.test_request_context() as context:
            from flask import g
            g.organization_id = 1
            guard = self.app.extensions["communication"]["is_suppressed"]
            self.assertTrue(guard(lead_id, "sms"))
            self.assertTrue(guard(lead_id, "call"))
            self.assertFalse(guard(999999, "sms"))

        # Lift the suppression; the guard no longer blocks.
        with self.db.connect() as connection:
            suppression_id = connection.execute(
                "SELECT id FROM consent_suppressions WHERE lead_id=?", (lead_id,)
            ).fetchone()[0]
        lifted = self.client.post(
            f"/settings/communication/suppressions/{suppression_id}/toggle",
            data={"csrf_token": self.csrf()},
        )
        self.assertEqual(lifted.status_code, 302)
        with self.app.test_request_context():
            from flask import g
            g.organization_id = 1
            guard = self.app.extensions["communication"]["is_suppressed"]
            self.assertFalse(guard(lead_id, "sms"))

        # Suppression for an unknown lead is rejected.
        self.assertEqual(self.client.post("/settings/communication/suppressions", data={
            "csrf_token": self.csrf(), "lead_id": "999999", "channel": "sms",
        }).status_code, 404)

    def test_delivery_state_recording_and_deduplication(self):
        lead_id = self.db.ensure_lead("3145550266", "(314) 555-0266")
        with self.app.test_request_context():
            from flask import g
            g.organization_id = 1
            with self.db.connect() as connection:
                cursor = connection.execute(
                    """INSERT INTO communications(
                        organization_id,lead_id,kind,from_number,to_number,message,status,created_at
                    ) VALUES (1,?,'sms','+13145550123','+13145550266','Hi','sent',?)""",
                    (lead_id, "2026-09-24T00:00:00+00:00"),
                )
                communication_id = cursor.lastrowid
                record = self.app.extensions["communication"]["record_delivery"]
                record(connection, communication_id, "sms", "sent", provider_message_id="msg-1")
                record(connection, communication_id, "sms", "delivered", provider_message_id="msg-1")
                # Duplicate state is ignored by the unique index.
                record(connection, communication_id, "sms", "sent", provider_message_id="msg-1")
                events = connection.execute(
                    "SELECT state FROM delivery_events WHERE communication_id=? ORDER BY id",
                    (communication_id,),
                ).fetchall()
        self.assertEqual([e["state"] for e in events], ["sent", "delivered"])


class PhaseTenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "AI Owner",
            "email": "ai@example.com", "password": "phase ten password 12",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_local_workflow_proposal_is_safe_and_reviewed(self):
        from ai_builders import generate_workflow_draft

        # Force the deterministic local path regardless of configured keys.
        saved = {key: os.environ.pop(key, None) for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY")}
        try:
            definition, provider = generate_workflow_draft(
                "when we miss a call, wait 10 minutes, then prepare a text and create a follow up task"
            )
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(provider, "local")
        self.assertEqual(definition["trigger"]["type"], "missed_call")
        self.assertEqual(definition["delay_minutes"], 10)
        self.assertEqual(
            {action["type"] for action in definition["actions"]},
            {"prepare_sms_draft", "create_task"},
        )

        # Unknown actions are stripped by validation.
        dangerous = {
            "trigger": {"type": "incoming_call", "stage_key": ""},
            "conditions": {}, "delay_minutes": 0,
            "actions": [{"type": "send_sms", "message": "hi"}],
        }
        with self.assertRaises(ValueError):
            from workflow_engine import validate_definition
            validate_definition(dangerous)

        # The builder route creates a proposal and cannot create a workflow yet.
        with self.db.connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM workflow_definitions").fetchone()[0]
        created = self.client.post("/automations/ai-builder", data={
            "csrf_token": self.csrf(),
            "description": "after a missed call wait 5 minutes then create a follow up task",
        })
        self.assertEqual(created.status_code, 302)
        proposal_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_definitions").fetchone()[0], before)
            proposal = connection.execute("SELECT * FROM ai_workflow_proposals WHERE id=?", (proposal_id,)).fetchone()
        self.assertEqual(proposal["status"], "pending")

        accepted = self.client.post(created.headers["Location"], data={
            "csrf_token": self.csrf(), "decision": "accept",
        })
        self.assertEqual(accepted.status_code, 302)
        with self.db.connect() as connection:
            proposal = connection.execute("SELECT * FROM ai_workflow_proposals WHERE id=?", (proposal_id,)).fetchone()
            workflow = connection.execute("SELECT * FROM workflow_definitions WHERE id=?", (proposal["workflow_id"],)).fetchone()
        self.assertEqual(proposal["status"], "accepted")
        self.assertEqual(workflow["enabled"], 0)
        self.assertIsNone(workflow["published_version"])
        self.assertIn("not published", workflow["description"])

        # Short descriptions are rejected.
        short = self.client.post("/automations/ai-builder", data={
            "csrf_token": self.csrf(), "description": "too short",
        }, follow_redirects=True)
        self.assertIn(b"at least a short sentence", short.data)

    def test_versioned_knowledge_and_search(self):
        page = self.client.get("/settings/knowledge")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Versioned knowledge", page.data)
        with self.db.connect() as connection:
            base = connection.execute("SELECT * FROM ai_knowledge_bases WHERE organization_id=1").fetchone()
        saved = self.client.post(f"/settings/knowledge/{base['id']}", data={
            "csrf_token": self.csrf(), "action": "save",
            "content": "Draft buying criteria focus on parcels over five acres with road access.",
        })
        self.assertEqual(saved.status_code, 302)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT published_version FROM ai_knowledge_bases WHERE id=?", (base["id"],)).fetchone()[0], 1)
        self.client.post(f"/settings/knowledge/{base['id']}", data={
            "csrf_token": self.csrf(), "action": "publish",
            "content": "Published buying criteria focus on five acre parcels with road access.",
        })
        with self.db.connect() as connection:
            versions = connection.execute("SELECT version FROM ai_knowledge_versions WHERE knowledge_base_id=? ORDER BY version", (base["id"],)).fetchall()
        self.assertEqual([row["version"] for row in versions], [1, 2])

        # Search finds the matching active source.
        results = self.client.get("/settings/knowledge?q=acre+parcels+road")
        self.assertIn(b"Published buying criteria", results.data)

        # Too-short content is rejected.
        short = self.client.post(f"/settings/knowledge/{base['id']}", data={
            "csrf_token": self.csrf(), "action": "publish", "content": "short",
        }, follow_redirects=True)
        self.assertIn(b"at least a short paragraph", short.data.lower())

        # Audit trail includes knowledge actions.
        with self.db.connect() as connection:
            actions = {row[0] for row in connection.execute("SELECT action FROM audit_events")}
        self.assertIn("ai.knowledge_published", actions)

    def test_call_capture_requires_selected_human_approval_and_handoff(self):
        lead_id = self.db.ensure_lead("3145550888", "(314) 555-0888")
        with self.db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO calls(organization_id,lead_id,message_id,source_key,direction,transcript,created_at)
                   VALUES (1,?,'ai-call-1','ai-call-1','incoming',?,?)""",
                (lead_id, "My reference is DEMO-2612-1. I inherited the land and want to sell for a quick cash closing.", "2026-09-24T12:00:00+00:00"),
            )
            call_id = cursor.lastrowid
        created = self.client.post(f"/calls/{call_id}/ai-capture", data={"csrf_token": self.csrf()})
        self.assertEqual(created.status_code, 302)
        with self.db.connect() as connection:
            lead = connection.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        self.assertEqual(lead["reference_number"], "")
        review = self.client.get(created.headers["Location"])
        self.assertIn(b"DEMO-2612-1", review.data)
        applied = self.client.post(created.headers["Location"], data={
            "csrf_token": self.csrf(), "decision": "accept",
            "fields": ["reference_number", "seller_motivation"],
        })
        self.assertEqual(applied.status_code, 302)
        with self.db.connect() as connection:
            lead = connection.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        self.assertEqual(lead["reference_number"], "DEMO-2612-1")
        self.assertTrue(lead["seller_motivation"])
        self.assertEqual(lead["distress_signal"], "")

        with self.db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO calls(organization_id,lead_id,message_id,source_key,direction,transcript,created_at)
                   VALUES (1,?,'ai-call-2','ai-call-2','incoming',?,?)""",
                (lead_id, "Stop calling me and remove me from your list.", "2026-09-24T13:00:00+00:00"),
            )
            optout_call = cursor.lastrowid
        optout = self.client.post(f"/calls/{optout_call}/ai-capture", data={"csrf_token": self.csrf()})
        self.assertIn(b"Human handoff recommended", self.client.get(optout.headers["Location"]).data)
        self.client.post(optout.headers["Location"], data={
            "csrf_token": self.csrf(), "decision": "accept", "create_handoff": "yes",
        })
        with self.db.connect() as connection:
            handoff = connection.execute("SELECT * FROM ai_handoffs WHERE lead_id=?", (lead_id,)).fetchone()
        self.assertEqual(handoff["priority"], "urgent")

        evaluated = self.client.post("/ai/evaluations/run", data={"csrf_token": self.csrf()})
        self.assertEqual(evaluated.status_code, 302)
        with self.db.connect() as connection:
            results = connection.execute("SELECT passed FROM ai_evaluation_results").fetchall()
        self.assertEqual(len(results), 4)
        self.assertTrue(all(row["passed"] for row in results))


class PhaseElevenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Workspace Owner",
            "email": "workspace@example.com", "password": "phase eleven password 12",
        })
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self, client=None):
        with (client or self.client).session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_workspace_reports_preferences_and_saved_view(self):
        lead_id = self.db.ensure_lead("3145551111", "(314) 555-1111")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE leads SET owner_name='Motivated Seller',follow_up_score=5,seller_motivation='Quick sale',lead_source='Mailer' WHERE id=?",
                (lead_id,),
            )
        workspace = self.client.get("/workspace")
        self.assertEqual(workspace.status_code, 200)
        self.assertIn(b"My workspace", workspace.data)
        self.assertIn(b"Motivated Seller", workspace.data)

        saved_layout = self.client.post("/workspace/preferences", data={
            "csrf_token": self.csrf(), "widgets": ["pipeline", "tasks"],
        })
        self.assertEqual(saved_layout.status_code, 302)
        with self.db.connect() as connection:
            preference = connection.execute("SELECT widgets_json FROM dashboard_preferences").fetchone()
        self.assertEqual(json.loads(preference["widgets_json"]), ["pipeline", "tasks"])

        reports = self.client.get("/reports?days=30")
        self.assertIn(b"Acquisition reports", reports.data)
        self.assertIn(b"Mailer", reports.data)
        saved = self.client.post("/reports/save", data={
            "csrf_token": self.csrf(), "name": "Monthly pipeline",
            "report_type": "pipeline", "days": "30",
        })
        self.assertEqual(saved.status_code, 302)
        with self.db.connect() as connection:
            report = connection.execute("SELECT * FROM saved_reports WHERE name='Monthly pipeline'").fetchone()
        self.assertEqual(report["report_type"], "pipeline")

    def test_help_ticket_lifecycle_and_tenant_scope(self):
        page = self.client.get("/help")
        self.assertIn(b"Help center", page.data)
        self.assertIn(b"Reviewing AI suggestions", page.data)
        created = self.client.post("/help/tickets/new", data={
            "csrf_token": self.csrf(), "subject": "Transcript needs review",
            "description": "The call has unusual speaker labels and needs a manual check.",
            "priority": "high",
        })
        self.assertEqual(created.status_code, 302)
        ticket_id = int(created.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        updated = self.client.post(created.headers["Location"], data={
            "csrf_token": self.csrf(), "status": "resolved", "resolution": "Reviewed manually.",
        })
        self.assertEqual(updated.status_code, 302)
        with self.db.connect() as connection:
            ticket = connection.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
        self.assertEqual(ticket["status"], "resolved")
        self.assertTrue(ticket["resolved_at"])

    def test_public_form_deduplicates_lead_and_pwa_is_public(self):
        public = self.app.test_client()
        with self.db.connect() as connection:
            form = connection.execute("SELECT * FROM public_forms WHERE organization_id=1").fetchone()
        page = public.get(f"/f/{form['slug']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Tell us about your property", page.data)
        token = self.csrf(public)
        submission = {
            "csrf_token": token, "owner_name": "Maya Sample", "phone": "314-555-0121",
            "reference_number": "DEMO-2612-1", "property_location": "Putnam County",
            "selling_reason": "Inherited and would prefer a simple cash closing.", "website": "",
        }
        sent = public.post(f"/f/{form['slug']}", data=submission)
        self.assertEqual(sent.status_code, 200)
        self.assertIn(b"information was received", sent.data)
        public.post(f"/f/{form['slug']}", data=submission)
        with self.db.connect() as connection:
            leads = connection.execute("SELECT * FROM leads WHERE phone_normalized='3145550121'").fetchall()
            submissions = connection.execute("SELECT * FROM form_submissions WHERE form_id=?", (form["id"],)).fetchall()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["owner_name"], "Maya Sample")
        self.assertEqual(leads[0]["reference_number"], "DEMO-2612-1")
        self.assertEqual(len(submissions), 2)

        manifest = public.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()["display"], "standalone")
        worker = public.get("/service-worker.js")
        self.assertEqual(worker.status_code, 200)
        self.assertNotIn(b"leads.db", worker.data)


class PhaseTwelveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "crm.db"
        self.db = CRMDatabase(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as browser_session:
            token = browser_session["csrf_token"]
        self.client.post("/setup", data={
            "csrf_token": token, "display_name": "Integration Owner",
            "email": "integrations@example.com", "password": "phase twelve password 12",
        })

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_scoped_api_key_idempotency_and_revocation(self):
        created = self.client.post("/settings/integrations/api-keys", data={
            "csrf_token": self.csrf(), "name": "Importer",
            "scopes": ["read:leads", "write:leads"],
        })
        self.assertEqual(created.status_code, 200)
        secret = re.search(rb"crm_[A-Za-z0-9_-]+", created.data).group(0).decode()
        headers = {"Authorization": f"Bearer {secret}", "Idempotency-Key": "source-event-100"}
        payload = {"phone": "314-555-7010", "owner_name": "Maya Sample", "reference_number": "DEMO-2612-1", "lead_source": "Partner API"}
        first = self.client.post("/api/v1/leads", json=payload, headers=headers)
        self.assertEqual(first.status_code, 200)
        lead_id = first.get_json()["data"]["id"]
        replay = self.client.post("/api/v1/leads", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        conflict = self.client.post("/api/v1/leads", json={**payload, "phone": "3145557011"}, headers=headers)
        self.assertEqual(conflict.status_code, 409)
        listed = self.client.get("/api/v1/leads?limit=1", headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["data"][0]["id"], lead_id)
        with self.db.connect() as connection:
            key_id = connection.execute("SELECT id FROM api_keys WHERE name='Importer'").fetchone()[0]
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_idempotency").fetchone()[0], 1)
        self.client.post(f"/settings/integrations/api-keys/{key_id}/revoke", data={"csrf_token": self.csrf()})
        denied = self.client.get("/api/v1/leads", headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(denied.status_code, 401)

    def test_signed_webhook_replay_expiry_and_validation(self):
        created = self.client.post("/settings/integrations/webhooks", data={
            "csrf_token": self.csrf(), "name": "Landing page",
        })
        self.assertEqual(created.status_code, 200)
        secret = bytes.fromhex(re.search(rb">([0-9a-f]{64})<", created.data).group(1).decode())
        with self.db.connect() as connection:
            endpoint = connection.execute("SELECT * FROM inbound_webhook_endpoints WHERE name='Landing page'").fetchone()
        raw = json.dumps({"phone": "3145557020", "owner_name": "Webhook Seller", "reference_number": "DEMO-2612-1"}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(secret, timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-CRM-Event-ID": "evt-100", "X-CRM-Timestamp": timestamp, "X-CRM-Signature": signature}
        accepted = self.client.post(f"/webhooks/leads/{endpoint['slug']}", data=raw, headers=headers)
        self.assertEqual(accepted.status_code, 200)
        replay = self.client.post(f"/webhooks/leads/{endpoint['slug']}", data=raw, headers=headers)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        bad = self.client.post(f"/webhooks/leads/{endpoint['slug']}", data=raw, headers={**headers, "X-CRM-Event-ID": "evt-101", "X-CRM-Signature": "0" * 64})
        self.assertEqual(bad.status_code, 401)
        expired_time = str(int(time.time()) - 600)
        expired_sig = hmac.new(secret, expired_time.encode() + b"." + raw, hashlib.sha256).hexdigest()
        expired = self.client.post(f"/webhooks/leads/{endpoint['slug']}", data=raw, headers={**headers, "X-CRM-Event-ID": "evt-102", "X-CRM-Timestamp": expired_time, "X-CRM-Signature": expired_sig})
        self.assertEqual(expired.status_code, 401)
        with self.db.connect() as connection:
            lead = connection.execute("SELECT * FROM leads WHERE phone_normalized='3145557020'").fetchone()
            deliveries = connection.execute("SELECT * FROM inbound_webhook_deliveries WHERE endpoint_id=?", (endpoint["id"],)).fetchall()
        self.assertEqual(lead["owner_name"], "Webhook Seller")
        self.assertEqual(len(deliveries), 3)


if __name__ == "__main__":
    unittest.main()
