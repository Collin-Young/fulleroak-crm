"""Missouri vacant-land double-close opportunities and compliant outreach tasks."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from zoneinfo import ZoneInfo

from flask import abort, flash, g, redirect, render_template_string, request, url_for

from crm_db import utc_now


PIPELINE_NAME = "Missouri Vacant Land Double Close"
CENTRAL_TIME = ZoneInfo("America/Chicago")
SMS_WINDOW_START_HOUR = 10
SMS_WINDOW_END_HOUR = 16
CAMPAIGN_SENDING_NUMBER = "3142544288"
STAGES = (
    ("New opportunity", "new_opportunity", "open"),
    ("Seller outreach", "seller_outreach", "open"),
    ("Seller contacted", "seller_contacted", "open"),
    ("Seller interested", "seller_interested", "open"),
    ("Seller price obtained", "seller_price_obtained", "open"),
    ("Buyer outreach", "buyer_outreach", "open"),
    ("Buyer contacted", "buyer_contacted", "open"),
    ("Buyer interested", "buyer_interested", "open"),
    ("Buyer price obtained", "buyer_price_obtained", "open"),
    ("Spread confirmed", "spread_confirmed", "open"),
    ("Under contract", "under_contract", "open"),
    ("Closing scheduled", "closing_scheduled", "open"),
    ("Closed", "closed", "won"),
    ("Dead lead", "dead_lead", "lost"),
)


def _next_allowed_sms_slot(moment: datetime) -> datetime:
    """Return the next six-minute slot inside the weekday Central send window."""
    moment = moment.astimezone(CENTRAL_TIME).replace(second=0, microsecond=0)
    if moment.minute % 6:
        moment += timedelta(minutes=6 - (moment.minute % 6))
    while True:
        if moment.weekday() >= 5:
            moment = (moment + timedelta(days=1)).replace(
                hour=SMS_WINDOW_START_HOUR, minute=0,
            )
            continue
        day_start = moment.replace(hour=SMS_WINDOW_START_HOUR, minute=0)
        day_end = moment.replace(hour=SMS_WINDOW_END_HOUR, minute=0)
        if moment < day_start:
            return day_start
        if moment >= day_end:
            moment = (moment + timedelta(days=1)).replace(
                hour=SMS_WINDOW_START_HOUR, minute=0,
            )
            continue
        return moment


def _sms_schedule(start: datetime, count: int) -> list[datetime]:
    """Build a rate-limited schedule, carrying overflow into later weekdays."""
    slots: list[datetime] = []
    cursor = start
    for _ in range(count):
        slot = _next_allowed_sms_slot(cursor)
        slots.append(slot)
        cursor = slot + timedelta(minutes=6)
    return slots


def _truth(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "t"}


def _number(value: object):
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _integer(value: object):
    number = _number(value)
    return int(number) if number is not None else None


def _normalize_identity(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _normalize_phone(value: object) -> tuple[str, str]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not 7 <= len(digits) <= 15:
        return "", ""
    display = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else digits
    return digits, display


def _value(row: dict, *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def _contact_identity(row: dict, side: str) -> tuple[str, str, str, str]:
    if side == "seller":
        match_id = _value(row, "seller_seller_match_id")
        name = _value(row, "seller_seller_name", "seller_Owner Name(s)")
        mailing = _value(row, "seller_seller_mail", "seller_Mail Full Address", "seller_Mail Address")
    else:
        match_id = _value(row, "buyer_buyer_match_id")
        name = _value(row, "buyer_best_buyer_name", "buyer_Owner Name(s)")
        mailing = _value(row, "buyer_best_buyer_mail", "buyer_Mail Full Address", "buyer_Mail Address")
    dedupe = f"id:{match_id}" if match_id else f"fallback:{_normalize_identity(name)}|{_normalize_identity(mailing)}"
    return dedupe, match_id, name, mailing


def _phone_flags(row: dict, side: str, slot: int) -> tuple[bool, bool, bool]:
    if slot == 1:
        return (
            _truth(row.get(f"{side}_skip_DNC")),
            _truth(row.get(f"{side}_skip_State DNC")),
            _truth(row.get(f"{side}_skip_Litigator")),
        )
    return (
        _truth(row.get(f"{side}_skip_Phone {slot} DNC")),
        _truth(row.get(f"{side}_skip_Phone {slot} State DNC")),
        _truth(row.get(f"{side}_skip_Phone {slot} Litigator")),
    )


def ensure_land_pipeline(connection, organization_id: int) -> tuple[int, dict[str, int]]:
    now = utc_now()
    pipeline = connection.execute(
        "SELECT id FROM pipelines WHERE organization_id=? AND name=?",
        (organization_id, PIPELINE_NAME),
    ).fetchone()
    if pipeline:
        pipeline_id = int(pipeline["id"])
    else:
        pipeline_id = int(connection.execute(
            """INSERT INTO pipelines(organization_id,name,kind,is_default,active,created_at,updated_at)
               VALUES (?,?,'other',0,1,?,?)""",
            (organization_id, PIPELINE_NAME, now, now),
        ).lastrowid)
    colors = ("#315f52", "#466f61", "#5c7e6f", "#718e7d", "#879e8c", "#88745d", "#987f62", "#aa8b67", "#bc976d", "#2f6f57", "#356a85", "#4e7192", "#2d7a4f", "#9b4540")
    for position, ((name, key, outcome), color) in enumerate(zip(STAGES, colors), 10):
        connection.execute(
            """INSERT INTO pipeline_stages(organization_id,pipeline_id,name,stage_key,position,color,outcome,active,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(pipeline_id,stage_key) DO UPDATE SET
                 name=excluded.name,position=excluded.position,color=excluded.color,outcome=excluded.outcome,active=1,updated_at=excluded.updated_at""",
            (organization_id, pipeline_id, name, key, position * 10, color, outcome, now, now),
        )
    stage_ids = {
        row["stage_key"]: int(row["id"])
        for row in connection.execute(
            "SELECT id,stage_key FROM pipeline_stages WHERE pipeline_id=?", (pipeline_id,)
        ).fetchall()
    }
    return pipeline_id, stage_ids


def _upsert_contact(connection, organization_id: int, row: dict, side: str, now: str) -> int:
    dedupe, match_id, name, mailing = _contact_identity(row, side)
    email = _value(row, f"{side}_skip_Email")
    raw = {key: value for key, value in row.items() if key.startswith(f"{side}_")}
    connection.execute(
        """INSERT INTO land_contacts(
             organization_id,role,dedupe_key,external_match_id,full_name,mailing_address,email,raw_source_json,created_at,updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(organization_id,role,dedupe_key) DO UPDATE SET
             external_match_id=excluded.external_match_id,full_name=excluded.full_name,
             mailing_address=excluded.mailing_address,email=excluded.email,
             raw_source_json=excluded.raw_source_json,updated_at=excluded.updated_at""",
        (organization_id, side, dedupe, match_id, name, mailing, email, json.dumps(raw, ensure_ascii=False), now, now),
    )
    contact_id = int(connection.execute(
        "SELECT id FROM land_contacts WHERE organization_id=? AND role=? AND dedupe_key=?",
        (organization_id, side, dedupe),
    ).fetchone()["id"])
    for slot in range(1, 7):
        normalized, display = _normalize_phone(row.get(f"{side}_skip_Phone {slot}"))
        if not normalized:
            connection.execute("DELETE FROM land_contact_phones WHERE contact_id=? AND slot=?", (contact_id, slot))
            continue
        phone_type = _value(row, f"{side}_skip_Phone {slot} Type")
        dnc, state_dnc, litigator = _phone_flags(row, side, slot)
        allowed = not (dnc or state_dnc or litigator)
        textable = allowed and phone_type.lower() == "mobile"
        connection.execute(
            """INSERT INTO land_contact_phones(
                 organization_id,contact_id,slot,phone_normalized,phone_display,phone_type,
                 dnc,state_dnc,litigator,callable,textable,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(contact_id,slot) DO UPDATE SET
                 phone_normalized=excluded.phone_normalized,phone_display=excluded.phone_display,
                 phone_type=excluded.phone_type,dnc=excluded.dnc,state_dnc=excluded.state_dnc,
                 litigator=excluded.litigator,callable=excluded.callable,textable=excluded.textable,
                 updated_at=excluded.updated_at""",
            (organization_id, contact_id, slot, normalized, display, phone_type,
             int(dnc), int(state_dnc), int(litigator), int(allowed), int(textable), now, now),
        )
    return contact_id


def import_land_csv(db, csv_path: Path | str, organization_id: int = 1) -> dict:
    path = Path(csv_path)
    payload = path.read_bytes()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        records = list(csv.DictReader(handle))
    if len(records) != 664:
        raise ValueError(f"Expected 664 data rows; found {len(records)}")
    external_ids = [_value(row, "vacant_parcel_group_id") for row in records]
    if not all(external_ids) or len(set(external_ids)) != 664:
        raise ValueError("vacant_parcel_group_id must contain exactly 664 unique, nonblank values")
    created = updated = 0
    now = utc_now()
    with db.connect() as connection:
        pipeline_id, stage_ids = ensure_land_pipeline(connection, organization_id)
        new_stage_id = stage_ids["new_opportunity"]
        for row in records:
            external_id = _value(row, "vacant_parcel_group_id")
            seller_id = _upsert_contact(connection, organization_id, row, "seller", now)
            buyer_id = _upsert_contact(connection, organization_id, row, "buyer", now)
            existing = connection.execute(
                "SELECT id FROM land_deals WHERE organization_id=? AND external_id=?",
                (organization_id, external_id),
            ).fetchone()
            values = (
                seller_id, buyer_id, _integer(row.get("deal_rank")),
                _number(_value(row, "seller_deal_score", "buyer_deal_score")),
                _value(row, "seller_County", "buyer_county_2", "buyer_County"),
                _value(row, "seller_target_apn", "seller_APN"),
                _number(_value(row, "seller_target_acres", "seller_Acreage", "seller_Calculated Acreage")),
                _value(row, "seller_Parcel Address"),
                _value(row, "seller_target_purchase_date", "seller_Last Sale Date"),
                _value(row, "seller_target_sale_year_status", "seller_target_date_status"),
                _value(row, "buyer_target_purchase_date", "buyer_Last Sale Date", "buyer_best_buyer_recent_sale"),
                _number(_value(row, "seller_shared_boundary_ft_approx", "buyer_shared_boundary_ft_approx")),
                _number(_value(row, "seller_reference_value_unverified", "buyer_reference_value_unverified")),
                _number(_value(row, "seller_illustrative_purchase_40pct", "buyer_illustrative_purchase_40pct")),
                _number(_value(row, "seller_illustrative_resale_75pct", "buyer_illustrative_resale_75pct")),
                _number(_value(row, "seller_illustrative_costs", "buyer_illustrative_costs")),
                _number(_value(row, "seller_illustrative_margin", "buyer_illustrative_margin")),
                _value(row, "seller_review_flags", "buyer_review_flags"),
                _value(row, "seller_target_parcel_link", "seller_Parcel Link"),
                _value(row, "seller_target_map_link", "seller_Google Map", "seller_Google Earth"),
                _integer(_value(row, "seller_buyer_mo_parcels", "buyer_buyer_mo_parcels")),
                _integer(_value(row, "seller_buyer_adjoining_parcels", "buyer_buyer_adjoining_parcels")),
                _integer(_value(row, "seller_eligible_buyer_identities", "buyer_eligible_buyer_identities")),
                json.dumps(row, ensure_ascii=False), now,
            )
            if existing:
                connection.execute(
                    """UPDATE land_deals SET seller_contact_id=?,buyer_contact_id=?,deal_rank=?,deal_score=?,county=?,
                       target_apn=?,target_acreage=?,target_parcel_address=?,seller_purchase_date=?,seller_purchase_status=?,
                       buyer_purchase_date=?,shared_boundary_distance=?,sale_reference_value=?,illustrative_acquisition_price=?,
                       illustrative_resale_price=?,illustrative_costs=?,illustrative_margin=?,review_flags=?,parcel_link=?,map_link=?,
                       buyer_mo_parcel_count=?,buyer_adjoining_count=?,eligible_buyer_identities=?,source_fields_json=?,updated_at=?
                       WHERE id=?""",
                    values + (int(existing["id"]),),
                )
                updated += 1
            else:
                connection.execute(
                    """INSERT INTO land_deals(
                       organization_id,external_id,pipeline_id,stage_id,seller_contact_id,buyer_contact_id,deal_rank,deal_score,
                       county,target_apn,target_acreage,target_parcel_address,seller_purchase_date,seller_purchase_status,
                       buyer_purchase_date,shared_boundary_distance,sale_reference_value,illustrative_acquisition_price,
                       illustrative_resale_price,illustrative_costs,illustrative_margin,review_flags,parcel_link,map_link,
                       buyer_mo_parcel_count,buyer_adjoining_count,eligible_buyer_identities,source_fields_json,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (organization_id, external_id, pipeline_id, new_stage_id) + values[:-1] + (now, now),
                )
                created += 1
        summary = land_import_summary(connection, organization_id)
        connection.execute(
            """INSERT INTO land_import_runs(organization_id,filename,source_sha256,deals_created,deals_updated,
               unique_sellers,unique_buyers,summary_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (organization_id, path.name, hashlib.sha256(payload).hexdigest(), created, updated,
             summary["unique_sellers"], summary["unique_buyers"], json.dumps(summary), now),
        )
    return {**summary, "deals_created": created, "deals_updated": updated, "unmatched_or_failed": 0}


def land_import_summary(connection, organization_id: int) -> dict:
    row = connection.execute(
        """SELECT
          COUNT(*) deals_imported,
          COUNT(DISTINCT seller_contact_id) unique_sellers,
          COUNT(DISTINCT buyer_contact_id) unique_buyers,
          SUM(EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.seller_contact_id AND p.callable=1)) callable_seller_deals,
          SUM(EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.buyer_contact_id AND p.callable=1)) callable_buyer_deals,
          SUM(EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.seller_contact_id AND p.callable=1)
              AND EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.buyer_contact_id AND p.callable=1)) both_callable_deals,
          SUM(EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.seller_contact_id AND p.textable=1)
              AND EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.buyer_contact_id AND p.textable=1)) both_textable_deals,
          SUM(EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id IN (d.seller_contact_id,d.buyer_contact_id)
              AND (p.dnc=1 OR p.state_dnc=1 OR p.litigator=1))) dnc_review_deals
          FROM land_deals d WHERE d.organization_id=?""",
        (organization_id,),
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def _create_outreach_task(connection, organization_id: int, deal_id: int, side: str) -> None:
    deal = connection.execute(
        """SELECT d.*,c.id contact_id,c.full_name FROM land_deals d
           JOIN land_contacts c ON c.id=CASE WHEN ?='seller' THEN d.seller_contact_id ELSE d.buyer_contact_id END
           WHERE d.id=? AND d.organization_id=?""",
        (side, deal_id, organization_id),
    ).fetchone()
    if not deal:
        return
    phone = connection.execute(
        """SELECT * FROM land_contact_phones WHERE contact_id=? AND textable=1 ORDER BY slot LIMIT 1""",
        (deal["contact_id"],),
    ).fetchone()
    now = utc_now()
    if phone:
        if side == "seller":
            draft = f"Hi {deal['full_name'] or 'there'}, I’m reaching out about the vacant parcel in {deal['county']} (APN {deal['target_apn']}). Would you be open to discussing an offer?"
        else:
            draft = f"Hi {deal['full_name'] or 'there'}, I’m reaching out about a vacant parcel adjoining property you own in {deal['county']}. Would you have interest in reviewing it?"
        task_type = "cold_text_review"
        title = f"Review {side} cold-text draft"
        phone_id = phone["id"]
    else:
        draft = ""
        task_type = "dnc_review"
        title = f"Review {side} contact eligibility before outreach"
        phone_id = None
    connection.execute(
        """INSERT OR IGNORE INTO land_outreach_tasks(
           organization_id,deal_id,side,task_type,phone_id,title,draft_text,status,created_at
           ) VALUES (?,?,?,?,?,?,?,'open',?)""",
        (organization_id, deal_id, side, task_type, phone_id, title, draft, now),
    )


def _ensure_seller_sms_campaign(connection, organization_id: int) -> int:
    """Create and refresh a consent-gated audience without authorizing any sends."""
    now = utc_now()
    name = "Missouri vacant-land seller interest"
    business_identity = "Fulleroak Properties"
    variants = {
        "A": (
            "Hi {first_name}, this is Collin with Fulleroak Properties. I'm reaching out about the "
            "vacant land you own in {county} County, APN {apn}. Would you consider selling it? "
            "Reply STOP to opt out."
        ),
        "B": (
            "Hi {first_name}, Collin with Fulleroak Properties here. Would you be open to discussing "
            "an offer for your vacant property in {county} County, APN {apn}? Reply STOP to opt out."
        ),
        "C": (
            "Hello {first_name}, this is Collin at Fulleroak Properties. I wanted to ask whether you "
            "would consider an offer for your vacant land in {county} County, APN {apn}. "
            "Reply STOP to opt out."
        ),
        "D": (
            "Hi {first_name}, this is Collin from Fulleroak Properties. Are you interested in selling "
            "the vacant parcel you own in {county} County, APN {apn}? Reply STOP to opt out."
        ),
    }
    message = variants["A"]
    connection.execute(
        """INSERT INTO land_sms_campaigns(
             organization_id,name,audience_key,message_template,status,automated_sending,
             requires_written_consent,business_identity,created_at,updated_at
           ) VALUES (?,?, 'seller_textable',?,'blocked',0,1,?,?,?)
           ON CONFLICT(organization_id,name) DO UPDATE SET
             message_template=excluded.message_template,business_identity=excluded.business_identity,
             updated_at=excluded.updated_at""",
        (organization_id, name, message, business_identity, now, now),
    )
    campaign_id = int(connection.execute(
        "SELECT id FROM land_sms_campaigns WHERE organization_id=? AND name=?",
        (organization_id, name),
    ).fetchone()["id"])
    prior_schedule = connection.execute(
        """SELECT send_window_start,send_window_end,send_timezone,launch_state
           FROM land_sms_campaigns WHERE id=? AND organization_id=?""",
        (campaign_id, organization_id),
    ).fetchone()
    schedule_changed = bool(
        prior_schedule
        and (
            prior_schedule["send_window_start"] != "10:00"
            or prior_schedule["send_window_end"] != "16:00"
            or prior_schedule["send_timezone"] != "America/Chicago"
        )
    )
    connection.execute(
        """UPDATE land_sms_campaigns
           SET drip_rate_per_hour=10,send_window_start='10:00',send_window_end='16:00',
               send_timezone='America/Chicago',weekdays_only=1,provider='vumber',
               sending_number=?,provider_connected=1,
               launch_state=CASE WHEN launch_state='active' THEN 'active' ELSE 'ready_to_send' END,
               automated_sending=CASE WHEN launch_state='active' THEN automated_sending ELSE 0 END,
               updated_at=?
           WHERE id=? AND organization_id=?""",
        (CAMPAIGN_SENDING_NUMBER, now, campaign_id, organization_id),
    )
    connection.execute(
        """INSERT OR IGNORE INTO land_sms_campaign_exclusions(
             organization_id,campaign_id,contact_id,reason,created_at
           ) SELECT ?,?,id,'Removed from this campaign by owner request',?
             FROM land_contacts
            WHERE organization_id=? AND lower(full_name)=lower('First Central Bank')""",
        (organization_id, campaign_id, now, organization_id),
    )
    for variant_key, variant_message in variants.items():
        connection.execute(
            """INSERT INTO land_sms_campaign_variants(
                 organization_id,campaign_id,variant_key,message_template,weight,active,created_at,updated_at
               ) VALUES (?,?,?,?,25,1,?,?)
               ON CONFLICT(campaign_id,variant_key) DO UPDATE SET
                 message_template=excluded.message_template,weight=25,active=1,updated_at=excluded.updated_at""",
            (organization_id, campaign_id, variant_key, variant_message, now, now),
        )
    campaign_record = connection.execute(
        """SELECT requires_written_consent,legal_review_name,legal_review_date
           FROM land_sms_campaigns WHERE id=?""",
        (campaign_id,),
    ).fetchone()
    legal_override = bool(
        campaign_record["requires_written_consent"] == 0
        and campaign_record["legal_review_name"]
        and campaign_record["legal_review_date"]
    )
    connection.execute(
        """INSERT INTO land_sms_campaign_compliance(
             organization_id,campaign_id,federal_dnc_scrub_attested,missouri_dnc_scrub_attested,
             attested_by,attested_at,notes,created_at,updated_at
           ) VALUES (?,?,1,1,'Collin',?,
             'Campaign owner states federal and Missouri DNC scrubs were completed before import; not independently verified.',?,?)
           ON CONFLICT(organization_id,campaign_id) DO UPDATE SET
             federal_dnc_scrub_attested=1,missouri_dnc_scrub_attested=1,
             attested_by='Collin',attested_at=COALESCE(land_sms_campaign_compliance.attested_at,excluded.attested_at),
             notes=excluded.notes,updated_at=excluded.updated_at""",
        (organization_id, campaign_id, now, now, now),
    )
    candidates = connection.execute(
        """SELECT d.id deal_id,d.seller_contact_id contact_id,
                  (SELECT p.id FROM land_contact_phones p
                   WHERE p.contact_id=d.seller_contact_id AND p.textable=1
                   ORDER BY p.slot LIMIT 1) phone_id
           FROM land_deals d
           JOIN (SELECT seller_contact_id,MIN(COALESCE(deal_rank,999999)) best_rank
                 FROM land_deals WHERE organization_id=? GROUP BY seller_contact_id) best
             ON best.seller_contact_id=d.seller_contact_id
            AND COALESCE(d.deal_rank,999999)=best.best_rank
           WHERE d.organization_id=?
             AND EXISTS(SELECT 1 FROM land_contact_phones p
                        WHERE p.contact_id=d.seller_contact_id AND p.textable=1)
           GROUP BY d.seller_contact_id""",
        (organization_id, organization_id),
    ).fetchall()
    for item in candidates:
        permission = connection.execute(
            "SELECT status FROM land_sms_permissions WHERE organization_id=? AND phone_id=?",
            (organization_id, item["phone_id"]),
        ).fetchone()
        permission_status = permission["status"] if permission else "unknown"
        excluded = connection.execute(
            """SELECT reason FROM land_sms_campaign_exclusions
               WHERE campaign_id=? AND contact_id=?""",
            (campaign_id, item["contact_id"]),
        ).fetchone()
        if excluded:
            status = "suppressed"
            reason = excluded["reason"] or "Removed from this campaign by owner request."
        elif permission_status in {"opted_out", "do_not_contact"}:
            status, reason = "suppressed", "Recipient is opted out or on the internal do-not-contact list."
        elif permission_status == "express_written_consent":
            status, reason = "ready", "Written consent is recorded; final campaign gates still apply."
        elif legal_override:
            status, reason = "ready", "Campaign owner attests counsel approved this automated acquisition outreach without recipient consent."
        else:
            status, reason = "blocked_no_consent", "No prior express written consent evidence is recorded."
        connection.execute(
            """INSERT INTO land_sms_campaign_members(
                 organization_id,campaign_id,deal_id,contact_id,phone_id,status,
                 eligibility_reason,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(campaign_id,contact_id) DO UPDATE SET
                 deal_id=excluded.deal_id,phone_id=excluded.phone_id,status=excluded.status,
                 eligibility_reason=excluded.eligibility_reason,updated_at=excluded.updated_at""",
            (organization_id, campaign_id, item["deal_id"], item["contact_id"], item["phone_id"],
             status, reason, now, now),
        )
    variant_ids = [row["id"] for row in connection.execute(
        """SELECT id FROM land_sms_campaign_variants
           WHERE campaign_id=? AND organization_id=? AND active=1
           ORDER BY variant_key""",
        (campaign_id, organization_id),
    ).fetchall()]
    for position, item in enumerate(sorted(candidates, key=lambda row: int(row["contact_id"]))):
        variant_id = variant_ids[position % len(variant_ids)]
        connection.execute(
            """INSERT INTO land_sms_campaign_variant_assignments(
                 organization_id,campaign_id,contact_id,variant_id,created_at,updated_at
               ) VALUES (?,?,?,?,?,?)
               ON CONFLICT(campaign_id,contact_id) DO NOTHING""",
            (organization_id, campaign_id, item["contact_id"], variant_id, now, now),
        )
    central = CENTRAL_TIME

    def normalize_slot(moment: datetime) -> datetime:
        moment = moment.replace(second=0, microsecond=0)
        if moment.minute % 6:
            moment += timedelta(minutes=6 - (moment.minute % 6))
        while True:
            if moment.weekday() >= 5:
                moment = (moment + timedelta(days=1)).replace(hour=SMS_WINDOW_START_HOUR, minute=0)
                continue
            day_start = moment.replace(hour=SMS_WINDOW_START_HOUR, minute=0)
            day_end = moment.replace(hour=SMS_WINDOW_END_HOUR, minute=0)
            if moment < day_start:
                return day_start
            if moment >= day_end:
                moment = (moment + timedelta(days=1)).replace(hour=SMS_WINDOW_START_HOUR, minute=0)
                continue
            return moment

    latest_slot = connection.execute(
        """SELECT scheduled_for_local FROM land_sms_campaign_queue
           WHERE campaign_id=? ORDER BY scheduled_for_utc DESC LIMIT 1""",
        (campaign_id,),
    ).fetchone()
    if latest_slot:
        cursor = datetime.fromisoformat(latest_slot["scheduled_for_local"]) + timedelta(minutes=6)
    else:
        cursor = datetime.now(central)
    cursor = normalize_slot(cursor.astimezone(central))
    queue_members = connection.execute(
        """SELECT m.id member_id,m.contact_id,d.deal_rank,d.county,d.target_apn,
                  c.full_name,v.id variant_id,v.message_template
           FROM land_sms_campaign_members m
           JOIN land_deals d ON d.id=m.deal_id
           JOIN land_contacts c ON c.id=m.contact_id
           JOIN land_sms_campaign_variant_assignments a
             ON a.campaign_id=m.campaign_id AND a.contact_id=m.contact_id
           JOIN land_sms_campaign_variants v ON v.id=a.variant_id
           LEFT JOIN land_sms_campaign_queue q
             ON q.campaign_id=m.campaign_id AND q.member_id=m.id
           WHERE m.campaign_id=? AND m.organization_id=? AND m.status='ready' AND q.id IS NULL
           ORDER BY d.deal_rank IS NULL,d.deal_rank,c.full_name""",
        (campaign_id, organization_id),
    ).fetchall()
    for member in queue_members:
        county = member["county"] or ""
        if county.lower().endswith(" county"):
            county = county[:-7]
        first_name = (member["full_name"] or "there").split()[0]
        rendered = member["message_template"].format(
            first_name=first_name, county=county, apn=member["target_apn"] or "",
        )
        local_slot = normalize_slot(cursor)
        utc_slot = local_slot.astimezone(timezone.utc)
        connection.execute(
            """INSERT INTO land_sms_campaign_queue(
                 organization_id,campaign_id,member_id,variant_id,scheduled_for_utc,
                 scheduled_for_local,rendered_message,provider,sending_number,status,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,'ready',?,?)""",
            (organization_id, campaign_id, member["member_id"], member["variant_id"],
             utc_slot.isoformat(timespec="minutes"), local_slot.isoformat(timespec="minutes"),
             rendered, "vumber", CAMPAIGN_SENDING_NUMBER, now, now),
        )
        cursor = local_slot + timedelta(minutes=6)
    connection.execute(
        """UPDATE land_sms_campaign_queue
              SET sending_number=?,updated_at=?
            WHERE campaign_id=? AND organization_id=?""",
        (CAMPAIGN_SENDING_NUMBER, now, campaign_id, organization_id),
    )
    connection.execute(
        """UPDATE land_sms_campaign_queue
              SET status='suppressed',last_error='Removed from this campaign by owner request',updated_at=?
            WHERE campaign_id=? AND organization_id=?
              AND status IN ('ready','held')
              AND member_id IN (
                  SELECT m.id FROM land_sms_campaign_members m
                  JOIN land_sms_campaign_exclusions e
                    ON e.campaign_id=m.campaign_id AND e.contact_id=m.contact_id
                  WHERE m.campaign_id=?
              )""",
        (now, campaign_id, organization_id, campaign_id),
    )
    if schedule_changed and prior_schedule["launch_state"] != "active":
        cursor = normalize_slot(datetime.now(central) + timedelta(minutes=1))
        queued = connection.execute(
            """SELECT id FROM land_sms_campaign_queue
               WHERE campaign_id=? AND organization_id=? AND status='ready'
               ORDER BY scheduled_for_utc,id""",
            (campaign_id, organization_id),
        ).fetchall()
        for item in queued:
            slot = normalize_slot(cursor)
            connection.execute(
                """UPDATE land_sms_campaign_queue
                   SET scheduled_for_local=?,scheduled_for_utc=?,updated_at=?
                   WHERE id=?""",
                (slot.isoformat(timespec="minutes"),
                 slot.astimezone(timezone.utc).isoformat(timespec="minutes"),
                 now, item["id"]),
            )
            cursor = slot + timedelta(minutes=6)
    return campaign_id


PHASE13_STYLE = """
.land-stats{grid-template-columns:repeat(7,1fr)}.land-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.warning{color:#8a342f;font-weight:700}.ok{color:#235d4d;font-weight:700}.source-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:4px 18px}.source-grid div{padding:6px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.blocked-card{border-left:5px solid #9b4540}.campaign-checks{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.campaign-check{padding:12px;border:1px solid var(--line);border-radius:10px;background:#fafaf7}.campaign-check b{display:block;margin-bottom:4px}.variant-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.variant-card{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fafaf7}.variant-meta{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
@media(max-width:1000px){.land-stats{grid-template-columns:repeat(3,1fr)}.land-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:650px){.land-grid,.source-grid,.variant-grid{grid-template-columns:1fr}}
"""


LIST_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Missouri Land Deals · CRM</title><style>{{style}}{{phase_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('deals_board')}}">← Deals</a><h1 style="margin-top:8px">Missouri vacant-land opportunities</h1><div class="sub">Double-close research, seller/buyer outreach, and phone compliance</div></div><a class="btn" href="{{url_for('land_seller_sms_campaign')}}">Seller SMS campaign</a></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<section class="stats land-stats"><div class="stat"><b>{{summary.deals_imported}}</b><span>Deals</span></div><div class="stat"><b>{{summary.unique_sellers}}</b><span>Unique sellers</span></div><div class="stat"><b>{{summary.unique_buyers}}</b><span>Unique buyers</span></div><div class="stat"><b>{{summary.callable_seller_deals}}</b><span>Seller callable</span></div><div class="stat"><b>{{summary.callable_buyer_deals}}</b><span>Buyer callable</span></div><div class="stat"><b>{{summary.both_callable_deals}}</b><span>Both callable</span></div><div class="stat"><b>{{summary.both_textable_deals}}</b><span>Both textable</span></div><div class="stat"><b>{{summary.dnc_review_deals}}</b><span>DNC review</span></div></section><div class="panel"><h2>Filtered views</h2><div class="actions">{% for key,label in views.items() %}<a class="pill" href="{{url_for('land_deals',view=key)}}">{{label}}</a>{% endfor %}<a class="pill" href="{{url_for('land_deals')}}">All</a></div></div><form class="toolbar" method="get"><input type="hidden" name="view" value="{{active_view}}"><input name="q" value="{{q}}" placeholder="Search APN, names, county, address..."><button class="btn">Apply</button><a class="btn secondary" href="{{url_for('land_deals',view=active_view)}}">Clear search</a></form><div class="table-wrap"><table><thead><tr><th>Rank</th><th>Opportunity</th><th>Seller</th><th>Buyer</th><th>Stage</th><th>Eligibility</th><th>Illustrative margin</th><th>Follow-up</th></tr></thead><tbody>{% for deal in deals %}<tr><td><span class="score">{{deal.deal_rank or '—'}}</span><div class="muted">{{deal.deal_score or '—'}}</div></td><td><a class="leadlink" href="{{url_for('land_deal_detail',deal_id=deal.id)}}">{{deal.external_id}}</a><div>{{deal.county}} County · {{deal.target_acreage or '—'}} ac</div><div class="muted">APN {{deal.target_apn}}</div></td><td>{{deal.seller_name}}<div class="muted">{{deal.seller_outreach_status}}</div></td><td>{{deal.buyer_name}}<div class="muted">{{deal.buyer_outreach_status}}</div></td><td><span class="pill">{{deal.stage_name}}</span></td><td><span class="{{'ok' if deal.seller_textable else 'warning'}}">Seller text {{'yes' if deal.seller_textable else 'no'}}</span> · <span class="{{'ok' if deal.buyer_textable else 'warning'}}">Buyer text {{'yes' if deal.buyer_textable else 'no'}}</span>{% if deal.needs_dnc_review %}<div class="warning">DNC review</div>{% endif %}</td><td>{{money(deal.illustrative_margin)}}</td><td>{{deal.next_follow_up_date or '—'}}</td></tr>{% else %}<tr><td colspan="8" class="empty">No opportunities match this view.</td></tr>{% endfor %}</tbody></table></div><div class="actions">{% if page>1 %}<a class="btn secondary" href="{{url_for('land_deals',view=active_view,q=q,page=page-1)}}">Previous</a>{% endif %}<span class="muted">Page {{page}}</span>{% if has_more %}<a class="btn secondary" href="{{url_for('land_deals',view=active_view,q=q,page=page+1)}}">Next</a>{% endif %}</div></main></body></html>
"""

CAMPAIGN_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Seller SMS campaign · CRM</title><style>{{style}}{{phase_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('land_deals')}}">← Missouri land deals</a><h1 style="margin-top:8px">Seller SMS campaign</h1><div class="sub">Automated vacant-land seller outreach with compliance controls</div></div><span class="pill {{'ok' if campaign.status=='ready' else 'warning'}}">{{'Ready — review before sending' if campaign.status=='ready' else 'Blocked — not sending'}}</span></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}{% if campaign.status=='ready' %}<section class="panel"><h2>Campaign unlocked</h2><p>Collin Young attests that counsel approved this specific automated land-acquisition campaign. The audience is ready for final batch review. Unlocking did not transmit or schedule any messages.</p></section>{% else %}<section class="panel blocked-card"><h2>Campaign created, but automation is locked</h2><p>A skip-traced mobile number that is not marked DNC is only technically reachable. No member can enter the ready queue until consent or another approved legal basis is documented.</p></section>{% endif %}<section class="stats"><div class="stat"><b>{{stats.total}}</b><span>Unique textable sellers</span></div><div class="stat"><b>{{stats.ready}}</b><span>Ready after compliance review</span></div><div class="stat"><b>{{stats.blocked}}</b><span>Blocked</span></div><div class="stat"><b>0</b><span>Messages sent</span></div></section><div class="grid"><section class="stack"><div class="panel"><h2>{{campaign.name}}</h2><div class="muted">Status: {{campaign.status|title}} · Automated sending: Off · Audience: one best unflagged mobile per unique seller</div><h3>Draft first message</h3><div class="draft-message">{{campaign.message_template}}</div><p class="muted">Business identity: Collin Young · Fulleroak Properties. “Reply STOP” is included and all opt-outs must be suppressed immediately.</p></div><div class="panel"><h2>Audience preview</h2><div class="table-wrap"><table><thead><tr><th>Rank</th><th>Seller</th><th>Property</th><th>Phone</th><th>Status</th></tr></thead><tbody>{% for member in members %}<tr><td>{{member.deal_rank}}</td><td>{{member.full_name}}</td><td>{{member.county}} County · APN {{member.target_apn}}</td><td>{{member.phone_display}}</td><td class="{{'ok' if member.status=='ready' else 'warning'}}">{{member.status|replace('_',' ')|title}}</td></tr>{% endfor %}</tbody></table></div></div></section><aside class="stack"><div class="panel"><h2>Activation checklist</h2><div class="campaign-checks"><div class="campaign-check"><b class="ok">Owner attested</b>Counsel approved this specific automated campaign · {{campaign.legal_review_date}}</div><div class="campaign-check"><b class="ok">Recorded</b>Collin Young · Fulleroak Properties</div><div class="campaign-check"><b class="warning">Pending verification</b>Registered provider campaign and verified sending number</div><div class="campaign-check"><b class="ok">Owner attested</b>Federal and Missouri DNC scrubs completed · {{compliance.attested_at[:10]}}</div><div class="campaign-check"><b class="ok">Enabled</b>DNC/state-DNC/litigator exclusions</div><div class="campaign-check"><b class="ok">Enabled</b>Seller deduplication and one selected mobile</div></div><p class="muted" style="margin-top:10px">Legal and DNC status were supplied by {{compliance.attested_by}} and have not been independently verified by the CRM.</p></div><div class="panel"><h2>Required controls before sending</h2><ul><li>Process STOP and other revocations immediately into a permanent suppression list.</li><li>Refresh federal and Missouri DNC scrubs before sending.</li><li>Enforce permitted local-time windows and frequency limits.</li><li>Log message content, sender number, delivery result, and opt-outs.</li></ul><p class="muted">This workflow is a technical safeguard, not legal advice.</p></div></aside></div></main></body></html>
"""

# The readable campaign layout supersedes the original single-draft layout above.
CAMPAIGN_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seller SMS campaign · CRM</title><style>{{style}}{{phase_style}}</style></head><body><main class="shell">
<header class="top"><div><a class="muted" href="{{url_for('land_deals')}}">← Missouri land deals</a>
<h1 style="margin-top:8px">Seller SMS campaign</h1><div class="sub">Automated vacant-land seller outreach with compliance controls</div></div>
<span class="pill {{'ok' if campaign.status=='ready' else 'warning'}}">{{'Campaign active' if campaign.launch_state=='active' else ('Ready to send' if campaign.status=='ready' else 'Blocked — not sending')}}</span></header>
{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}
{% if campaign.status=='ready' %}<section class="panel"><h2>{{'Campaign active' if campaign.launch_state=='active' else 'Drip queue ready'}}</h2><p>All eligible sellers have a fixed message variant and scheduled send slot. {% if campaign.launch_state=='active' %}The sender is processing due messages within the approved window.{% else %}The launch has not started and no messages were transmitted.{% endif %}</p>
{% if campaign.launch_state!='active' %}<form method="post" action="{{url_for('start_land_seller_sms_campaign')}}"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><label class="check"><input type="checkbox" name="confirmed" value="yes" required><span>I understand that starting will transmit these {{queue_stats.ready}} seller texts through Vumber.</span></label><button class="btn" style="margin-top:10px">Start drip campaign</button></form>{% endif %}</section>
{% else %}<section class="panel blocked-card"><h2>Campaign created, but automation is locked</h2><p>No member can enter the ready queue until consent or another approved legal basis is documented.</p></section>{% endif %}
<section class="stats"><div class="stat"><b>{{stats.total}}</b><span>Unique textable sellers</span></div><div class="stat"><b>{{queue_stats.ready}}</b><span>Queued and ready</span></div><div class="stat"><b>{{stats.blocked}}</b><span>Consent blocked</span></div><div class="stat"><b>{{stats.suppressed}}</b><span>Suppressed / opt-outs</span></div><div class="stat"><b>{{queue_stats.sent}}</b><span>Messages sent</span></div></section>
<div class="grid"><section class="stack"><div class="panel"><h2>{{campaign.name}}</h2>
<div class="muted">Status: {{'Active' if campaign.launch_state=='active' else 'Ready to send'}} · Launch: {{'started' if campaign.launch_state=='active' else 'not started'}} · Audience: one best unflagged mobile per unique seller</div>
<h3>Drip schedule</h3><div class="campaign-checks"><div class="campaign-check"><b>Rate limit</b>{{campaign.drip_rate_per_hour}} texts per hour · one every 6 minutes</div><div class="campaign-check"><b>Send window</b>10:00 AM–4:00 PM Central · weekdays only</div><div class="campaign-check"><b>First slot</b>{{queue_stats.first_slot}}</div><div class="campaign-check"><b>Projected completion</b>{{queue_stats.last_slot}}</div></div>
<h3>Spinner message variants</h3><p class="muted">Each seller receives one stable, equal-weight variant. Refreshing the campaign will not change that seller's assignment.</p>
<div class="variant-grid">{% for variant in variants %}<article class="variant-card"><div class="variant-meta"><b>Variant {{variant.variant_key}}</b><span class="pill">{{variant.weight}}% · {{variant.assigned_count}} sellers</span></div><div class="draft-message">{{variant.message_template}}</div></article>{% endfor %}</div>
<p class="muted">Sender identity: Collin · Fulleroak Properties. “Reply STOP” is included and all opt-outs must be suppressed immediately.</p></div>
<div class="panel"><h2>Queue preview</h2><div class="table-wrap"><table><thead><tr><th>Rank</th><th>Seller</th><th>Property</th><th>Phone</th><th>Variant</th><th>Scheduled (Central)</th><th>Status</th></tr></thead><tbody>
{% for member in members %}<tr><td>{{member.deal_rank}}</td><td>{{member.full_name}}</td><td>{{member.county}} County · APN {{member.target_apn}}</td><td>{{member.phone_display}}</td><td><span class="pill">{{member.variant_key}}</span></td><td>{{member.scheduled_display}}</td><td class="{{'warning' if member.status=='suppressed' else ('ok' if member.queue_status=='ready' else '')}}">{{'Suppressed / opt-out' if member.status=='suppressed' else member.queue_status|title}}</td></tr>{% endfor %}
</tbody></table></div></div></section><aside class="stack"><div class="panel"><h2>Activation checklist</h2><div class="campaign-checks">
<div class="campaign-check"><b class="ok">Owner attested</b>Counsel approved this specific automated campaign · {{campaign.legal_review_date}}</div>
<div class="campaign-check"><b class="ok">Recorded</b>Collin · Fulleroak Properties</div>
<div class="campaign-check"><b class="ok">Connected</b>Vumber SMS line (314) 254-4288</div>
<div class="campaign-check"><b class="ok">Owner attested</b>Federal and Missouri DNC scrubs completed · {{compliance.attested_at[:10]}}</div>
<div class="campaign-check"><b class="ok">Enabled</b>DNC/state-DNC/litigator exclusions</div><div class="campaign-check"><b class="ok">Enabled</b>Seller deduplication and one selected mobile</div></div>
<p class="muted" style="margin-top:10px">Legal and DNC status were supplied by {{compliance.attested_by}} and have not been independently verified by the CRM.</p></div>
<div class="panel"><h2>Required controls before sending</h2><ul><li>Process STOP and other revocations immediately into a permanent suppression list.</li><li>Refresh federal and Missouri DNC scrubs before sending.</li><li>Enforce permitted local-time windows and frequency limits.</li><li>Log message content, sender number, delivery result, and opt-outs.</li></ul><p class="muted">This workflow is a technical safeguard, not legal advice.</p></div></aside></div>
</main></body></html>
"""


DETAIL_TEMPLATE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{deal.external_id}} · CRM</title><style>{{style}}{{phase_style}}</style></head><body><main class="shell"><header class="top"><div><a class="muted" href="{{url_for('land_deals')}}">← Missouri land deals</a><h1 style="margin-top:8px">#{{deal.deal_rank}} · {{deal.county}} County · {{deal.target_acreage or '—'}} acres</h1><div class="sub">{{deal.external_id}} · {{deal.stage_name}}</div></div><div class="actions">{% if deal.parcel_link %}<a class="btn secondary" target="_blank" rel="noopener" href="{{deal.parcel_link}}">Parcel</a>{% endif %}{% if deal.map_link %}<a class="btn secondary" target="_blank" rel="noopener" href="{{deal.map_link}}">Map</a>{% endif %}</div></header>{% for category,message in get_flashed_messages(with_categories=true) %}<div class="status-note flash {{category}}">{{message}}</div>{% endfor %}<section class="panel"><h2>Opportunity research</h2><div class="land-grid"><div class="fact"><b>Deal score</b>{{deal.deal_score or '—'}}</div><div class="fact"><b>Target APN</b>{{deal.target_apn}}</div><div class="fact"><b>Parcel address</b>{{deal.target_parcel_address or '—'}}</div><div class="fact"><b>Shared boundary</b>{{deal.shared_boundary_distance or '—'}} ft</div><div class="fact"><b>Seller purchase</b>{{deal.seller_purchase_date or '—'}} · {{deal.seller_purchase_status or '—'}}</div><div class="fact"><b>Buyer purchase</b>{{deal.buyer_purchase_date or '—'}}</div><div class="fact"><b>Buyer MO parcels</b>{{deal.buyer_mo_parcel_count or 0}}</div><div class="fact"><b>Buyer adjoining parcels</b>{{deal.buyer_adjoining_count or 0}}</div><div class="fact"><b>Sale-reference value</b>{{money(deal.sale_reference_value)}}</div><div class="fact"><b>Illustrative acquisition</b>{{money(deal.illustrative_acquisition_price)}}</div><div class="fact"><b>Illustrative resale</b>{{money(deal.illustrative_resale_price)}}</div><div class="fact"><b>Illustrative margin</b>{{money(deal.illustrative_margin)}}</div></div><div class="status-note">Illustrative prices and margin are research estimates, not confirmed offers.</div>{% if deal.review_flags %}<div><b>Review flags:</b> {{deal.review_flags}}</div>{% endif %}</section><div class="grid"><section class="stack">{% for side,contact,phones in contact_blocks %}<div class="panel"><h2>{{side|title}} contact</h2><h3>{{contact.full_name}}</h3><div>{{contact.mailing_address}}</div><div>{{contact.email or 'No email returned'}}</div><div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Slot</th><th>Phone</th><th>Type</th><th>DNC</th><th>State DNC</th><th>Litigator</th><th>Callable</th><th>Textable</th></tr></thead><tbody>{% for phone in phones %}<tr><td>{{phone.slot}}</td><td>{{phone.phone_display}}</td><td>{{phone.phone_type or '—'}}</td><td>{{'Yes' if phone.dnc else 'No'}}</td><td>{{'Yes' if phone.state_dnc else 'No'}}</td><td>{{'Yes' if phone.litigator else 'No'}}</td><td class="{{'ok' if phone.callable else 'warning'}}">{{'Yes' if phone.callable else 'No'}}</td><td class="{{'ok' if phone.textable else 'warning'}}">{{'Yes' if phone.textable else 'No'}}</td></tr>{% else %}<tr><td colspan="8">No phone returned.</td></tr>{% endfor %}</tbody></table></div></div>{% endfor %}<div class="panel"><h2>Outreach tasks</h2>{% for task in tasks %}<article class="offer"><b>{{task.title}}</b><div class="muted">{{task.side|title}} · {{task.task_type|replace('_',' ')}} · {{task.status}}</div>{% if task.phone_display %}<div>Eligible number: {{task.phone_display}}</div>{% endif %}{% if task.draft_text %}<div class="draft-message">{{task.draft_text}}</div>{% endif %}</article>{% else %}<div class="muted">Tasks are created when the deal enters Seller outreach or Buyer outreach. Drafts are review-only and are never sent automatically.</div>{% endfor %}</div></section><aside class="stack"><form class="panel" method="post"><input type="hidden" name="csrf_token" value="{{csrf_token()}}"><h2>Deal workflow</h2><label class="field">Stage<select name="stage_id">{% for stage in stages %}<option value="{{stage.id}}" {% if stage.id==deal.stage_id %}selected{% endif %}>{{stage.name}}</option>{% endfor %}</select></label><label class="field">Seller outreach status<select name="seller_outreach_status">{% for item in outreach_statuses %}<option {% if item==deal.seller_outreach_status %}selected{% endif %}>{{item}}</option>{% endfor %}</select></label><label class="field">Buyer outreach status<select name="buyer_outreach_status">{% for item in outreach_statuses %}<option {% if item==deal.buyer_outreach_status %}selected{% endif %}>{{item}}</option>{% endfor %}</select></label><label class="field">Seller asking price<input type="number" step="0.01" min="0" name="seller_asking_price" value="{{deal.seller_asking_price if deal.seller_asking_price is not none else ''}}"></label><label class="field">Proposed acquisition price<input type="number" step="0.01" min="0" name="proposed_acquisition_price" value="{{deal.proposed_acquisition_price if deal.proposed_acquisition_price is not none else ''}}"></label><label class="field">Buyer indicated price<input type="number" step="0.01" min="0" name="buyer_indicated_price" value="{{deal.buyer_indicated_price if deal.buyer_indicated_price is not none else ''}}"></label><label class="field">Estimated closing and funding costs<input type="number" step="0.01" min="0" name="estimated_closing_funding_costs" value="{{deal.estimated_closing_funding_costs if deal.estimated_closing_funding_costs is not none else ''}}"></label><div class="stat"><b>{{money(deal.expected_spread)}}</b><span>Expected spread = buyer price − acquisition − costs</span></div><label class="field">Seller motivation<textarea name="seller_motivation">{{deal.seller_motivation}}</textarea></label><label class="field">Seller response<textarea name="seller_response">{{deal.seller_response}}</textarea></label><label class="field">Buyer response<textarea name="buyer_response">{{deal.buyer_response}}</textarea></label><label class="field">Next follow-up<input type="date" name="next_follow_up_date" value="{{deal.next_follow_up_date or ''}}"></label><label class="field">Assigned user<select name="assigned_user_id"><option value="">Unassigned</option>{% for user in users %}<option value="{{user.id}}" {% if user.id==deal.assigned_user_id %}selected{% endif %}>{{user.display_name}}</option>{% endfor %}</select></label><label class="field">Notes<textarea name="notes">{{deal.notes}}</textarea></label><label class="field">Stage note<input name="stage_note" placeholder="Why is this moving?"></label><button class="btn">Save deal</button></form><div class="panel"><h2>Stage history</h2>{% for item in history %}<div class="call"><b>{{item.from_name or 'Created'}} → {{item.to_name}}</b><div class="muted">{{item.created_at}} · {{item.display_name or 'System'}}</div>{% if item.note %}<div>{{item.note}}</div>{% endif %}</div>{% else %}<div class="muted">No stage changes yet.</div>{% endfor %}</div></aside></div><details class="panel" style="margin-top:18px"><summary><b>All 414 preserved source fields</b></summary><div class="source-grid" style="margin-top:12px">{% for key,value in source_fields.items() %}<div><b>{{key}}</b><br>{{value or '—'}}</div>{% endfor %}</div></details></main></body></html>
"""


VIEWS = {
    "highest_ranked_new": "Highest-ranked new opportunities",
    "both_callable": "Both parties have callable phone numbers",
    "both_textable": "Both parties eligible for text",
    "seller_contacted_buyer_not": "Seller contacted but buyer not contacted",
    "seller_interested": "Seller interested",
    "buyer_interested": "Buyer interested",
    "both_interested": "Both parties interested",
    "spread_confirmed": "Spread confirmed",
    "followup_due": "Follow-up due",
    "dnc_review": "DNC review required",
    "multiple_buyers": "Multiple eligible neighboring buyers",
    "largest_margin": "Largest illustrative margin",
}
OUTREACH_STATUSES = ("Not contacted", "Outreach queued", "Attempted", "Contacted", "Interested", "Not interested", "Do not contact")


def register_phase13(app, db, style, rows, organization_id, require_csrf, roles_required, audit, vumber):
    def money(value):
        return "—" if value is None else f"${float(value):,.0f}"

    worker_lock = Lock()
    running_workers: set[int] = set()

    def reschedule_ready_queue(connection, campaign_id: int, org_id: int, start: datetime) -> int:
        queued = connection.execute(
            """SELECT id FROM land_sms_campaign_queue
               WHERE campaign_id=? AND organization_id=? AND status='ready'
               ORDER BY scheduled_for_utc,id""",
            (campaign_id, org_id),
        ).fetchall()
        now = utc_now()
        for item, slot in zip(queued, _sms_schedule(start, len(queued))):
            connection.execute(
                """UPDATE land_sms_campaign_queue
                   SET scheduled_for_local=?,scheduled_for_utc=?,updated_at=?
                   WHERE id=? AND status='ready'""",
                (slot.isoformat(timespec="minutes"),
                 slot.astimezone(timezone.utc).isoformat(timespec="minutes"),
                 now, item["id"]),
            )
        return len(queued)

    def run_campaign_worker(campaign_id: int, org_id: int) -> None:
        try:
            while True:
                with db.connect() as connection:
                    campaign = connection.execute(
                        """SELECT automated_sending,launch_state,sending_number
                           FROM land_sms_campaigns WHERE id=? AND organization_id=?""",
                        (campaign_id, org_id),
                    ).fetchone()
                    if not campaign or not campaign["automated_sending"] or campaign["launch_state"] != "active":
                        return
                    item = connection.execute(
                        """SELECT q.id,q.scheduled_for_utc,q.rendered_message,q.sending_number,
                                  p.phone_normalized,p.textable,p.dnc,p.state_dnc,p.litigator,
                                  m.status member_status,COALESCE(sp.status,'unknown') permission_status,
                                  EXISTS(SELECT 1 FROM land_sms_campaign_exclusions e
                                         WHERE e.campaign_id=q.campaign_id AND e.contact_id=m.contact_id) excluded
                           FROM land_sms_campaign_queue q
                           JOIN land_sms_campaign_members m ON m.id=q.member_id
                           JOIN land_contact_phones p ON p.id=m.phone_id
                           LEFT JOIN land_sms_permissions sp
                             ON sp.organization_id=q.organization_id AND sp.phone_id=p.id
                           WHERE q.campaign_id=? AND q.organization_id=? AND q.status='ready'
                           ORDER BY q.scheduled_for_utc LIMIT 1""",
                        (campaign_id, org_id),
                    ).fetchone()
                    if not item:
                        connection.execute(
                            """UPDATE land_sms_campaigns
                               SET automated_sending=0,launch_state='completed',updated_at=?
                               WHERE id=? AND organization_id=?""",
                            (utc_now(), campaign_id, org_id),
                        )
                        return
                    if (item["excluded"] or item["member_status"] != "ready"
                            or not item["textable"] or item["dnc"] or item["state_dnc"]
                            or item["litigator"] or item["permission_status"] in {"opted_out", "do_not_contact"}):
                        connection.execute(
                            """UPDATE land_sms_campaign_queue
                               SET status='suppressed',last_error='Eligibility changed before send',updated_at=?
                               WHERE id=?""",
                            (utc_now(), item["id"]),
                        )
                        continue
                    due = datetime.fromisoformat(item["scheduled_for_utc"])
                seconds_until_due = (due - datetime.now(timezone.utc)).total_seconds()
                if seconds_until_due > 0:
                    time.sleep(min(seconds_until_due, 20))
                    continue
                try:
                    result = vumber.send_sms(
                        item["phone_normalized"], item["sending_number"], item["rendered_message"],
                    )
                    provider_id = str(
                        result.get("messageId") or result.get("id") or result.get("message_id") or ""
                    )
                    with db.connect() as connection:
                        connection.execute(
                            """UPDATE land_sms_campaign_queue
                               SET status='sent',provider_message_id=?,sent_at=?,last_error='',updated_at=?
                               WHERE id=? AND status='ready'""",
                            (provider_id, utc_now(), utc_now(), item["id"]),
                        )
                except Exception as exc:
                    with db.connect() as connection:
                        connection.execute(
                            """UPDATE land_sms_campaign_queue
                               SET status='failed',last_error=?,updated_at=? WHERE id=?""",
                            (str(exc)[:500], utc_now(), item["id"]),
                        )
        finally:
            with worker_lock:
                running_workers.discard(campaign_id)

    def start_campaign_worker(campaign_id: int, org_id: int) -> None:
        with worker_lock:
            if campaign_id in running_workers:
                return
            running_workers.add(campaign_id)
        Thread(
            target=run_campaign_worker,
            args=(campaign_id, org_id),
            name=f"land-sms-campaign-{campaign_id}",
            daemon=True,
        ).start()

    def resume_active_campaigns() -> None:
        """Restore active workers after a web-process restart without bursting stale sends."""
        with db.connect() as connection:
            active = connection.execute(
                """SELECT id,organization_id,sending_number
                   FROM land_sms_campaigns
                   WHERE automated_sending=1 AND launch_state='active'"""
            ).fetchall()
        if not active:
            return
        try:
            allowed_numbers = {
                item["value"] for item in vumber.configuration(force=True)["sms_numbers"]
            }
        except Exception as exc:
            app.logger.warning("Active SMS campaigns were not resumed: %s", exc)
            return
        for campaign in active:
            if campaign["sending_number"] not in allowed_numbers:
                app.logger.warning(
                    "SMS campaign %s was not resumed because sender %s is unavailable",
                    campaign["id"], campaign["sending_number"],
                )
                continue
            with db.connect() as connection:
                first_ready = connection.execute(
                    """SELECT scheduled_for_utc FROM land_sms_campaign_queue
                       WHERE campaign_id=? AND organization_id=? AND status='ready'
                       ORDER BY scheduled_for_utc,id LIMIT 1""",
                    (campaign["id"], campaign["organization_id"]),
                ).fetchone()
                if not first_ready:
                    connection.execute(
                        """UPDATE land_sms_campaigns
                           SET automated_sending=0,launch_state='completed',updated_at=?
                           WHERE id=? AND organization_id=?""",
                        (utc_now(), campaign["id"], campaign["organization_id"]),
                    )
                    continue
                due = datetime.fromisoformat(first_ready["scheduled_for_utc"])
                if due <= datetime.now(timezone.utc):
                    reschedule_ready_queue(
                        connection, campaign["id"], campaign["organization_id"],
                        datetime.now(CENTRAL_TIME) + timedelta(minutes=1),
                    )
            start_campaign_worker(campaign["id"], campaign["organization_id"])

    app.extensions["land_sms_campaign"] = {
        "resume_active_campaigns": resume_active_campaigns,
        "reschedule_ready_queue": reschedule_ready_queue,
    }
    resume_active_campaigns()

    @app.get("/land-deals")
    def land_deals():
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        page_size = 100
        q = request.args.get("q", "").strip()
        active_view = request.args.get("view", "highest_ranked_new")
        where = ["d.organization_id=?"]
        params: list[object] = [organization_id()]
        if q:
            where.append("(d.external_id LIKE ? OR d.target_apn LIKE ? OR d.county LIKE ? OR d.target_parcel_address LIKE ? OR sc.full_name LIKE ? OR bc.full_name LIKE ?)")
            params.extend([f"%{q}%"] * 6)
        extra = {
            "highest_ranked_new": "AND stage_key='new_opportunity'",
            "both_callable": "AND seller_callable=1 AND buyer_callable=1",
            "both_textable": "AND seller_textable=1 AND buyer_textable=1",
            "seller_contacted_buyer_not": "AND seller_outreach_status IN ('Contacted','Interested') AND buyer_outreach_status NOT IN ('Contacted','Interested')",
            "seller_interested": "AND seller_outreach_status='Interested'",
            "buyer_interested": "AND buyer_outreach_status='Interested'",
            "both_interested": "AND seller_outreach_status='Interested' AND buyer_outreach_status='Interested'",
            "spread_confirmed": "AND stage_key='spread_confirmed'",
            "followup_due": "AND next_follow_up_date IS NOT NULL AND next_follow_up_date<=date('now')",
            "dnc_review": "AND needs_dnc_review=1",
            "multiple_buyers": "AND COALESCE(eligible_buyer_identities,0)>1",
            "largest_margin": "",
        }.get(active_view, "")
        order = "illustrative_margin DESC,deal_rank" if active_view == "largest_margin" else "deal_rank IS NULL,deal_rank,id"
        inner = f"""SELECT d.*,ps.name stage_name,ps.stage_key,sc.full_name seller_name,bc.full_name buyer_name,
            EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.seller_contact_id AND p.callable=1) seller_callable,
            EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.buyer_contact_id AND p.callable=1) buyer_callable,
            EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.seller_contact_id AND p.textable=1) seller_textable,
            EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id=d.buyer_contact_id AND p.textable=1) buyer_textable,
            EXISTS(SELECT 1 FROM land_contact_phones p WHERE p.contact_id IN (d.seller_contact_id,d.buyer_contact_id) AND (p.dnc=1 OR p.state_dnc=1 OR p.litigator=1)) needs_dnc_review
            FROM land_deals d JOIN pipeline_stages ps ON ps.id=d.stage_id
            JOIN land_contacts sc ON sc.id=d.seller_contact_id JOIN land_contacts bc ON bc.id=d.buyer_contact_id
            WHERE {' AND '.join(where)}"""
        deals = rows(f"SELECT * FROM ({inner}) WHERE 1=1 {extra} ORDER BY {order} LIMIT ? OFFSET ?", params + [page_size + 1, (page - 1) * page_size])
        has_more = len(deals) > page_size
        deals = deals[:page_size]
        for item in deals:
            if item["county"].lower().endswith(" county"):
                item["county"] = item["county"][:-7]
        with db.connect() as connection:
            summary = land_import_summary(connection, organization_id())
        return render_template_string(LIST_TEMPLATE, style=style, phase_style=PHASE13_STYLE, deals=deals,
            summary=summary, views=VIEWS, active_view=active_view, q=q, page=page, has_more=has_more, money=money)

    @app.get("/land-deals/seller-sms-campaign")
    def land_seller_sms_campaign():
        with db.connect() as connection:
            campaign_id = _ensure_seller_sms_campaign(connection, organization_id())
            campaign = dict(connection.execute(
                "SELECT * FROM land_sms_campaigns WHERE id=? AND organization_id=?",
                (campaign_id, organization_id()),
            ).fetchone())
            compliance = dict(connection.execute(
                "SELECT * FROM land_sms_campaign_compliance WHERE campaign_id=? AND organization_id=?",
                (campaign_id, organization_id()),
            ).fetchone())
            stats_row = connection.execute(
                """SELECT SUM(status='ready') total,
                          SUM(status='ready') ready,
                          SUM(status='blocked_no_consent') blocked,
                          SUM(status='suppressed') suppressed
                   FROM land_sms_campaign_members
                   WHERE campaign_id=? AND organization_id=?""",
                (campaign_id, organization_id()),
            ).fetchone()
            stats = {key: int(stats_row[key] or 0) for key in stats_row.keys()}
            queue_row = connection.execute(
                """SELECT SUM(status='ready' OR sent_at IS NOT NULL) total,
                          SUM(status='ready') ready,SUM(sent_at IS NOT NULL) sent,
                          MIN(CASE WHEN status='ready' OR sent_at IS NOT NULL THEN scheduled_for_local END) first_slot,
                          MAX(CASE WHEN status='ready' OR sent_at IS NOT NULL THEN scheduled_for_local END) last_slot
                   FROM land_sms_campaign_queue
                   WHERE campaign_id=? AND organization_id=?""",
                (campaign_id, organization_id()),
            ).fetchone()
            queue_stats = {key: queue_row[key] for key in queue_row.keys()}
            for key in ("total", "ready", "sent"):
                queue_stats[key] = int(queue_stats[key] or 0)
            for key in ("first_slot", "last_slot"):
                if queue_stats[key]:
                    queue_stats[key] = datetime.fromisoformat(queue_stats[key]).strftime(
                        "%a %b %d, %I:%M %p CT"
                    ).replace(" 0", " ")
                else:
                    queue_stats[key] = "—"
            variants = [dict(row) for row in connection.execute(
                """SELECT v.variant_key,v.message_template,v.weight,
                          SUM(CASE WHEN m.status='ready' THEN 1 ELSE 0 END) assigned_count
                   FROM land_sms_campaign_variants v
                   LEFT JOIN land_sms_campaign_variant_assignments a
                     ON a.variant_id=v.id AND a.campaign_id=v.campaign_id
                   LEFT JOIN land_sms_campaign_members m
                     ON m.campaign_id=a.campaign_id AND m.contact_id=a.contact_id
                   WHERE v.campaign_id=? AND v.organization_id=? AND v.active=1
                   GROUP BY v.id,v.variant_key,v.message_template,v.weight
                   ORDER BY v.variant_key""",
                (campaign_id, organization_id()),
            ).fetchall()]
            members = [dict(row) for row in connection.execute(
                """SELECT m.status,d.deal_rank,d.county,d.target_apn,c.full_name,p.phone_display,
                          v.variant_key,q.status queue_status,q.scheduled_for_local
                   FROM land_sms_campaign_members m
                   JOIN land_deals d ON d.id=m.deal_id
                   JOIN land_contacts c ON c.id=m.contact_id
                   JOIN land_contact_phones p ON p.id=m.phone_id
                   LEFT JOIN land_sms_campaign_variant_assignments a
                     ON a.campaign_id=m.campaign_id AND a.contact_id=m.contact_id
                   LEFT JOIN land_sms_campaign_variants v ON v.id=a.variant_id
                   JOIN land_sms_campaign_queue q
                     ON q.campaign_id=m.campaign_id AND q.member_id=m.id
                   WHERE m.campaign_id=? AND m.organization_id=?
                   ORDER BY d.deal_rank IS NULL,d.deal_rank,c.full_name LIMIT 100""",
                (campaign_id, organization_id()),
            ).fetchall()]
        for member in members:
            if member["county"].lower().endswith(" county"):
                member["county"] = member["county"][:-7]
            member["scheduled_display"] = datetime.fromisoformat(
                member["scheduled_for_local"]
            ).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
        return render_template_string(
            CAMPAIGN_TEMPLATE, style=style, phase_style=PHASE13_STYLE,
            campaign=campaign, compliance=compliance, stats=stats, queue_stats=queue_stats,
            variants=variants, members=members,
        )

    @app.post("/land-deals/seller-sms-campaign/start")
    @roles_required("owner", "manager")
    def start_land_seller_sms_campaign():
        require_csrf()
        if request.form.get("confirmed") != "yes":
            flash("Confirm that you want to start transmitting the scheduled texts.", "error")
            return redirect(url_for("land_seller_sms_campaign"))
        org_id = organization_id()
        with db.connect() as connection:
            campaign_id = _ensure_seller_sms_campaign(connection, org_id)
            campaign = connection.execute(
                """SELECT * FROM land_sms_campaigns
                   WHERE id=? AND organization_id=?""",
                (campaign_id, org_id),
            ).fetchone()
            if not campaign or campaign["status"] != "ready":
                flash("The campaign is not eligible to launch.", "error")
                return redirect(url_for("land_seller_sms_campaign"))
            ready_count = int(connection.execute(
                """SELECT COUNT(*) count FROM land_sms_campaign_queue
                   WHERE campaign_id=? AND organization_id=? AND status='ready'""",
                (campaign_id, org_id),
            ).fetchone()["count"])
            if ready_count == 0:
                flash("There are no ready messages in the queue.", "error")
                return redirect(url_for("land_seller_sms_campaign"))
            try:
                allowed_numbers = {
                    item["value"] for item in vumber.configuration(force=True)["sms_numbers"]
                }
            except Exception as exc:
                flash(f"Vumber connection check failed: {str(exc)[:200]}", "error")
                return redirect(url_for("land_seller_sms_campaign"))
            if campaign["sending_number"] not in allowed_numbers:
                flash("The selected Vumber number is not currently available for SMS.", "error")
                return redirect(url_for("land_seller_sms_campaign"))
            connection.execute(
                """UPDATE land_sms_campaign_queue
                   SET sending_number=?,updated_at=?
                   WHERE campaign_id=? AND organization_id=? AND status='ready'""",
                (campaign["sending_number"], utc_now(), campaign_id, org_id),
            )
            reschedule_ready_queue(
                connection, campaign_id, org_id,
                datetime.now(CENTRAL_TIME) + timedelta(minutes=1),
            )
            connection.execute(
                """UPDATE land_sms_campaigns
                   SET automated_sending=1,launch_state='active',updated_at=?
                   WHERE id=? AND organization_id=?""",
                (utc_now(), campaign_id, org_id),
            )
        audit("land_sms_campaign.started", "land_sms_campaign", campaign_id, {
            "queued": ready_count, "rate_per_hour": 10,
            "sending_number": CAMPAIGN_SENDING_NUMBER, "cutoff": "16:00 America/Chicago",
        })
        start_campaign_worker(campaign_id, org_id)
        flash(f"Drip campaign started with {ready_count} messages queued.", "success")
        return redirect(url_for("land_seller_sms_campaign"))

    @app.route("/land-deals/<int:deal_id>", methods=["GET", "POST"])
    def land_deal_detail(deal_id: int):
        found = rows("""SELECT d.*,ps.name stage_name FROM land_deals d JOIN pipeline_stages ps ON ps.id=d.stage_id
            WHERE d.id=? AND d.organization_id=?""", (deal_id, organization_id()))
        if not found:
            abort(404)
        deal = found[0]
        if deal["county"].lower().endswith(" county"):
            deal["county"] = deal["county"][:-7]
        if request.method == "POST":
            if g.user["role"] not in {"owner", "manager", "agent"}:
                abort(403)
            require_csrf()
            stage_raw = request.form.get("stage_id", "")
            if not stage_raw.isdigit():
                abort(400)
            new_stage_id = int(stage_raw)
            stages = rows("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND organization_id=? AND active=1", (deal["pipeline_id"], organization_id()))
            stage_map = {item["id"]: item for item in stages}
            if new_stage_id not in stage_map:
                abort(400)
            def optional_money(name):
                raw = request.form.get(name, "").strip()
                if not raw:
                    return None
                value = float(raw)
                if value < 0:
                    raise ValueError
                return value
            try:
                asking = optional_money("seller_asking_price")
                acquisition = optional_money("proposed_acquisition_price")
                buyer_price = optional_money("buyer_indicated_price")
                costs = optional_money("estimated_closing_funding_costs")
            except ValueError:
                flash("Prices and costs must be non-negative numbers.", "error")
                return redirect(url_for("land_deal_detail", deal_id=deal_id))
            expected = buyer_price - acquisition - costs if None not in (buyer_price, acquisition, costs) else None
            assigned_raw = request.form.get("assigned_user_id", "")
            assigned = int(assigned_raw) if assigned_raw.isdigit() else None
            if assigned and not rows("SELECT id FROM users WHERE id=? AND organization_id=? AND active=1", (assigned, organization_id())):
                abort(400)
            seller_status = request.form.get("seller_outreach_status", "Not contacted")
            buyer_status = request.form.get("buyer_outreach_status", "Not contacted")
            if seller_status not in OUTREACH_STATUSES or buyer_status not in OUTREACH_STATUSES:
                abort(400)
            followup = request.form.get("next_follow_up_date", "").strip() or None
            if followup:
                date.fromisoformat(followup)
            now = utc_now()
            with db.connect() as connection:
                connection.execute("""UPDATE land_deals SET stage_id=?,seller_outreach_status=?,buyer_outreach_status=?,
                    seller_asking_price=?,proposed_acquisition_price=?,buyer_indicated_price=?,estimated_closing_funding_costs=?,
                    expected_spread=?,seller_motivation=?,seller_response=?,buyer_response=?,next_follow_up_date=?,assigned_user_id=?,
                    notes=?,updated_at=? WHERE id=? AND organization_id=?""", (
                    new_stage_id, seller_status, buyer_status, asking, acquisition, buyer_price, costs, expected,
                    request.form.get("seller_motivation", "").strip(), request.form.get("seller_response", "").strip(),
                    request.form.get("buyer_response", "").strip(), followup, assigned,
                    request.form.get("notes", "").strip(), now, deal_id, organization_id(),
                ))
                if new_stage_id != deal["stage_id"]:
                    connection.execute("""INSERT INTO land_deal_stage_history(
                        organization_id,deal_id,from_stage_id,to_stage_id,user_id,note,created_at
                        ) VALUES (?,?,?,?,?,?,?)""", (organization_id(), deal_id, deal["stage_id"], new_stage_id,
                        g.user.get("id"), request.form.get("stage_note", "").strip()[:500], now))
                    key = stage_map[new_stage_id]["stage_key"]
                    if key == "seller_outreach":
                        _create_outreach_task(connection, organization_id(), deal_id, "seller")
                    elif key == "buyer_outreach":
                        _create_outreach_task(connection, organization_id(), deal_id, "buyer")
            audit("land_deal.update", "land_deal", deal_id, {"stage_id": new_stage_id})
            flash("Land deal saved. Expected spread was recalculated.", "good")
            return redirect(url_for("land_deal_detail", deal_id=deal_id))
        seller = rows("SELECT * FROM land_contacts WHERE id=? AND organization_id=?", (deal["seller_contact_id"], organization_id()))[0]
        buyer = rows("SELECT * FROM land_contacts WHERE id=? AND organization_id=?", (deal["buyer_contact_id"], organization_id()))[0]
        seller_phones = rows("SELECT * FROM land_contact_phones WHERE contact_id=? ORDER BY slot", (seller["id"],))
        buyer_phones = rows("SELECT * FROM land_contact_phones WHERE contact_id=? ORDER BY slot", (buyer["id"],))
        stages = rows("SELECT * FROM pipeline_stages WHERE pipeline_id=? AND organization_id=? AND active=1 ORDER BY position,id", (deal["pipeline_id"], organization_id()))
        users = rows("SELECT id,display_name FROM users WHERE organization_id=? AND active=1 ORDER BY display_name", (organization_id(),))
        history = rows("""SELECT h.*,fs.name from_name,ts.name to_name,u.display_name FROM land_deal_stage_history h
            LEFT JOIN pipeline_stages fs ON fs.id=h.from_stage_id JOIN pipeline_stages ts ON ts.id=h.to_stage_id
            LEFT JOIN users u ON u.id=h.user_id WHERE h.deal_id=? AND h.organization_id=? ORDER BY h.id DESC""", (deal_id, organization_id()))
        tasks = rows("""SELECT t.*,p.phone_display FROM land_outreach_tasks t LEFT JOIN land_contact_phones p ON p.id=t.phone_id
            WHERE t.deal_id=? AND t.organization_id=? ORDER BY t.id DESC""", (deal_id, organization_id()))
        return render_template_string(DETAIL_TEMPLATE, style=style, phase_style=PHASE13_STYLE, deal=deal,
            contact_blocks=(("seller", seller, seller_phones), ("buyer", buyer, buyer_phones)), stages=stages,
            users=users, history=history, tasks=tasks, source_fields=json.loads(deal["source_fields_json"]),
            outreach_statuses=OUTREACH_STATUSES, money=money)
