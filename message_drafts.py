"""Generate safe, editable SMS suggestions without ever sending them."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field


load_dotenv()
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_disabled_providers: set[str] = set()


class Draft(BaseModel):
    angle: str = Field(description="Short label for the approach")
    message: str = Field(min_length=1, max_length=800)


class DraftBundle(BaseModel):
    drafts: list[Draft] = Field(min_length=3, max_length=3)


def context_hash(
    lead: dict[str, Any], calls: list[dict[str, Any]],
    communications: list[dict[str, Any]], offers: list[dict[str, Any]],
) -> str:
    context = _context(lead, calls, communications, offers)
    encoded = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generate_drafts(
    lead: dict[str, Any], calls: list[dict[str, Any]],
    communications: list[dict[str, Any]], offers: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], str]:
    context = _context(lead, calls, communications, offers)
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if openai_key and "openai" not in _disabled_providers:
        try:
            client = OpenAI(api_key=openai_key)
            response = client.responses.parse(
                model=os.environ.get("OPENAI_DRAFT_MODEL", "gpt-4o-mini"),
                input=[
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                text_format=DraftBundle,
            )
            if response.output_parsed:
                return [draft.model_dump() for draft in response.output_parsed.drafts], "openai"
        except Exception as exc:
            _disabled_providers.add("openai")
            logging.warning("OpenAI message drafting unavailable; using fallback: %s", exc)
    if openrouter_key and "openrouter" not in _disabled_providers:
        try:
            response = requests.post(
                OPENROUTER_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://127.0.0.1:5000"),
                    "X-Title": os.environ.get("OPENROUTER_APP_NAME", "Vumber Lead CRM"),
                },
                json={
                    "model": os.environ.get("OPENROUTER_DRAFT_MODEL", "openai/gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": _system_prompt()},
                        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            response.raise_for_status()
            parsed = DraftBundle.model_validate_json(response.json()["choices"][0]["message"]["content"])
            return [draft.model_dump() for draft in parsed.drafts], "openrouter"
        except Exception as exc:
            _disabled_providers.add("openrouter")
            logging.warning("OpenRouter message drafting unavailable; using fallback: %s", exc)
    return _local_drafts(context), "local"


def _context(
    lead: dict[str, Any], calls: list[dict[str, Any]],
    communications: list[dict[str, Any]], offers: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "lead": {
            key: lead.get(key, "") for key in (
                "owner_name", "phone_display", "reference_number", "status", "deal_style",
                "lead_source", "summary", "seller_motivation", "distress_signal",
                "price_expectation", "follow_up_plan", "notes",
            )
        },
        "offers": offers[:3],
        "calls_oldest_to_newest": [
            {
                "direction": call.get("direction", ""),
                "occurred_at": call.get("occurred_at", ""),
                "transcript": (call.get("transcript") or "")[-5000:],
            }
            for call in calls[-8:]
        ],
        "prior_outbound_texts_oldest_to_newest": [
            communication.get("message", "") for communication in reversed(communications[:12])
            if communication.get("kind") == "sms" and communication.get("direction") == "outgoing"
        ],
    }


def _system_prompt() -> str:
    return (
        "Write exactly three suggested SMS messages for a land-acquisition lead. Use the complete context, "
        "including prior conversations, previous outbound texting style, property/offer details, deal style, "
        "motivation, distress, price posture, and current stage. The business seeks properties at a meaningful "
        "discount, so qualify price flexibility tactfully without claiming a price or fact that is not supplied. "
        "Do not exploit distress, sound predatory, pressure the seller, or mention internal scoring. Never pretend "
        "there was a prior conversation if none exists. Keep each message natural, specific, and under 500 characters. "
        "Identify the sender as Collin with Fuller Oak when an introduction is appropriate. Return JSON with a "
        "top-level 'drafts' array; each item has 'angle' and 'message'."
    )


def _local_drafts(context: dict[str, Any]) -> list[dict[str, str]]:
    lead = context["lead"]
    offers = context["offers"]
    first_name = (lead.get("owner_name") or "").split(" ")[0]
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    offer = offers[0] if offers else {}
    property_bits = []
    if offer.get("acreage"):
        property_bits.append(f"the {offer['acreage']}-acre property")
    if offer.get("county"):
        property_bits.append(f"in {offer['county']} County")
    property_phrase = " ".join(property_bits) or "your property"
    reference = lead.get("reference_number")
    reference_phrase = f" (reference {reference})" if reference else ""
    prior = bool(context["calls_oldest_to_newest"] or context["prior_outbound_texts_oldest_to_newest"])
    opening = "following up about" if prior else "reaching out about"
    motivation = lead.get("seller_motivation")
    motivation_line = ""
    if motivation and motivation.lower() not in {"none identified", "none", "unknown"}:
        motivation_line = " I wanted to check whether selling still makes sense for you."
    return [
        {
            "angle": "Warm follow-up" if prior else "Warm introduction",
            "message": f"{greeting} this is Collin with Fuller Oak. I'm {opening} {property_phrase}{reference_phrase}.{motivation_line} Is there a good time for a quick call?",
        },
        {
            "angle": "Price flexibility",
            "message": f"{greeting} Collin with Fuller Oak here. We're reviewing {property_phrase}{reference_phrase}. If a simple cash sale with us covering normal closing costs could work, what price range would you seriously consider?",
        },
        {
            "angle": "Low-pressure check-in",
            "message": f"{greeting} I wanted to check in on {property_phrase}{reference_phrase}. No pressure—are you still open to an offer, or would you prefer that I close out the file for now? – Collin, Fuller Oak",
        },
    ]
