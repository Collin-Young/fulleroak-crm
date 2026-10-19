"""Safe merge, PDF generation, hashing, and validation for CRM documents."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


MERGE_FIELDS = {
    "organization_name", "date_today", "deal_title", "seller_name", "seller_phone",
    "seller_email", "mailing_address", "reference_number", "property_address",
    "county", "state", "acreage", "apn", "offer_price",
}
FIELD_PATTERN = re.compile(r"{{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*}}")


def template_fields(body: str) -> list[str]:
    return sorted(set(FIELD_PATTERN.findall(body or "")))


def validate_template(body: str) -> list[str]:
    if not body or len(body.strip()) < 20:
        raise ValueError("Template body is too short.")
    fields = template_fields(body)
    unknown = sorted(set(fields) - MERGE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown merge fields: {', '.join(unknown)}")
    return fields


def merge_template(body: str, values: dict[str, Any]) -> str:
    validate_template(body)

    def replace(match: re.Match[str]) -> str:
        value = values.get(match.group(1), "")
        return str(value if value not in (None, "") else "Not provided")

    return FIELD_PATTERN.sub(replace, body)


def merge_context(connection, organization_id: int, opportunity_id: int) -> tuple[dict, dict]:
    row = connection.execute(
        """SELECT o.*,org.name organization_name,l.owner_name,l.phone_display,l.reference_number lead_reference,
            c.full_name,c.primary_phone_display,c.primary_email,c.mailing_address,
            p.reference_number property_reference,p.property_address,p.county,p.state,p.acreage,p.apn,p.offer_price
           FROM opportunities o JOIN organizations org ON org.id=o.organization_id
           LEFT JOIN leads l ON l.id=o.lead_id AND l.organization_id=o.organization_id
           LEFT JOIN contacts c ON c.id=o.contact_id AND c.organization_id=o.organization_id
           LEFT JOIN properties p ON p.id=o.property_id AND p.organization_id=o.organization_id
           WHERE o.id=? AND o.organization_id=?""",
        (opportunity_id, organization_id),
    ).fetchone()
    if not row:
        raise ValueError("Deal was not found.")
    values = {
        "organization_name": row["organization_name"], "date_today": date.today().isoformat(),
        "deal_title": row["title"], "seller_name": row["full_name"] or row["owner_name"],
        "seller_phone": row["primary_phone_display"] or row["phone_display"],
        "seller_email": row["primary_email"], "mailing_address": row["mailing_address"],
        "reference_number": row["property_reference"] or row["lead_reference"],
        "property_address": row["property_address"], "county": row["county"],
        "state": row["state"], "acreage": row["acreage"], "apn": row["apn"],
        "offer_price": row["offer_price"],
    }
    return dict(row), values


def _page(canvas, document) -> None:
    canvas.saveState()
    width, height = LETTER
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#68736f"))
    canvas.drawString(0.72 * inch, 0.42 * inch, "Fuller Oak CRM document")
    canvas.drawRightString(width - 0.72 * inch, 0.42 * inch, f"Page {document.page}")
    canvas.setFillColor(colors.Color(0.72, 0.25, 0.21, alpha=0.10))
    canvas.setFont("Helvetica-Bold", 38)
    canvas.translate(width / 2, height / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, 0, "UNSIGNED")
    canvas.restoreState()


def generate_pdf(output_path: Path, title: str, rendered_body: str, metadata: dict[str, str] | None = None) -> tuple[str, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ContractTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=17,
        leading=21, alignment=TA_CENTER, textColor=colors.HexColor("#18201e"), spaceAfter=18,
    )
    heading_style = ParagraphStyle(
        "ContractHeading", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=10.5, leading=14, textColor=colors.HexColor("#235d4d"),
        spaceBefore=10, spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "ContractBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=10,
        leading=15, textColor=colors.HexColor("#18201e"), spaceAfter=8,
    )
    document = SimpleDocTemplate(
        str(output_path), pagesize=LETTER, rightMargin=0.72 * inch, leftMargin=0.72 * inch,
        topMargin=0.70 * inch, bottomMargin=0.68 * inch,
        title=title, author=(metadata or {}).get("organization_name", "Fuller Oak CRM"),
        subject="CRM generated agreement draft",
    )
    story = [Paragraph(html.escape(title), title_style)]
    lines = rendered_body.splitlines()
    if lines and lines[0].strip().upper() in title.upper():
        lines = lines[1:]
    for raw in lines:
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 0.09 * inch))
        elif line == "[[PAGE_BREAK]]":
            story.append(PageBreak())
        elif line.isupper() and len(line) <= 80:
            story.append(Paragraph(html.escape(line), heading_style))
        else:
            story.append(Paragraph(html.escape(line), body_style))
    document.build(story, onFirstPage=_page, onLaterPages=_page)
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    reader = PdfReader(str(output_path))
    if reader.is_encrypted or not reader.pages:
        raise ValueError("Generated PDF could not be validated.")
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    if title.split()[0].lower() not in extracted.lower():
        raise ValueError("Generated PDF text validation failed.")
    return digest, len(reader.pages)


def validate_uploaded_pdf(path: Path) -> tuple[str, int, int]:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Signed PDF must be 20 MB or smaller.")
    if not path.read_bytes()[:5] == b"%PDF-":
        raise ValueError("Uploaded file is not a PDF.")
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ValueError("Encrypted signed PDFs are not supported.")
    if not reader.pages:
        raise ValueError("Signed PDF has no pages.")
    return hashlib.sha256(path.read_bytes()).hexdigest(), len(reader.pages), path.stat().st_size


def evidence_json(**values) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"))
