"""Plain-English AI workflow drafting and knowledge retrieval.

Generated workflow ASTs are always validated against the same allowlist the
manual builder uses and are only ever saved as disabled drafts that a human
must review and explicitly publish. Without an API key a deterministic local
heuristic produces a safe draft so the feature works offline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

from workflow_engine import ALLOWED_ACTIONS, ALLOWED_TRIGGERS, validate_definition
from lead_pipeline import extract_references_from_text, sanitize_owner_name


load_dotenv()
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

TRIGGER_KEYWORDS = {
    "lead_created": ("new lead", "lead is created", "when a lead", "added to the crm"),
    "incoming_call": ("incoming call", "calls us", "inbound call", "answers"),
    "missed_call": ("missed call", "miss a call", "missed", "voicemail", "doesn't answer", "does not answer"),
    "outgoing_sms": ("text is sent", "after we text", "sms sent", "after texting"),
    "stage_changed": ("stage", "moves to", "qualified", "reaches", "pipeline"),
    "activity_added": ("any activity", "any event", "anything happens"),
}

ACTION_KEYWORDS = {
    "prepare_sms_draft": ("text", "sms", "message", "draft"),
    "create_task": ("task", "reminder", "follow up", "follow-up", "to-do", "todo"),
}

SYSTEM_PROMPT = """You convert plain-English automation requests into a strict JSON workflow.
Allowed trigger types: lead_created, activity_added, incoming_call, missed_call, outgoing_sms, stage_changed.
Allowed action types: create_task (fields: title), prepare_sms_draft (fields: message, angle).
Never include actions that send messages or place calls. Respond with JSON only:
{"name": str, "description": str, "trigger": {"type": str, "stage_key": str}, "delay_minutes": int, "actions": [{"type": str, ...}]}
Use {name}, {first_name}, {phone}, and {reference} placeholders in text. delay_minutes must be 0 or positive."""


def _local_draft(description: str) -> dict[str, Any]:
    """Deterministic heuristic draft used when no AI provider is configured."""
    lowered = description.lower()
    trigger_type = "activity_added"
    for candidate, keywords in TRIGGER_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            trigger_type = candidate
            break
    stage_key = ""
    stage_match = re.search(r"\b(new|contacted|qualified|nurture|won|lost)\b", lowered)
    if trigger_type == "stage_changed" and stage_match:
        stage_key = stage_match.group(1)

    delay_minutes = 0
    delay_match = re.search(r"(?:after|wait) (\d+) (minute|hour|day|week)", lowered)
    if delay_match:
        amount, unit = int(delay_match.group(1)), delay_match.group(2)
        multiplier = {"minute": 1, "hour": 60, "day": 1440, "week": 10080}[unit]
        delay_minutes = amount * multiplier

    actions: list[dict[str, str]] = []
    if any(keyword in lowered for keyword in ACTION_KEYWORDS["prepare_sms_draft"]):
        actions.append({
            "type": "prepare_sms_draft",
            "angle": "AI drafted follow-up",
            "message": "Hi {first_name}, following up about {reference}. Is now a good time to talk?",
        })
    if any(keyword in lowered for keyword in ACTION_KEYWORDS["create_task"]) or not actions:
        actions.append({"type": "create_task", "title": "Review {name} at {phone}"})

    return {
        "name": description.strip()[:80] or "AI drafted automation",
        "description": "Drafted from plain English; review before publishing.",
        "trigger": {"type": trigger_type, "stage_key": stage_key},
        "delay_minutes": delay_minutes,
        "actions": actions,
    }


def generate_workflow_draft(description: str) -> tuple[dict[str, Any], str]:
    """Return (validated_definition, provider) for a plain-English description."""
    if not description or len(description.strip()) < 10:
        raise ValueError("Describe the automation in at least a short sentence.")
    definition: dict[str, Any] | None = None
    provider = "local"

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if openai_key:
        try:
            client = OpenAI(api_key=openai_key)
            response = client.chat.completions.create(
                model=os.environ.get("OPENAI_BUILDER_MODEL", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": description},
                ],
                response_format={"type": "json_object"},
            )
            definition = json.loads(response.choices[0].message.content or "{}")
            provider = "openai"
        except Exception as exc:
            logging.warning("OpenAI workflow drafting unavailable; using fallback: %s", exc)
            definition = None
    if definition is None and openrouter_key:
        try:
            response = requests.post(
                OPENROUTER_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.environ.get("OPENROUTER_BUILDER_MODEL", "openai/gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": description},
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=45,
            )
            response.raise_for_status()
            definition = json.loads(response.json()["choices"][0]["message"]["content"])
            provider = "openrouter"
        except Exception as exc:
            logging.warning("OpenRouter workflow drafting unavailable; using fallback: %s", exc)
            definition = None
    if definition is None:
        definition = _local_draft(description)

    # Enforce the safe allowlist regardless of provider output.
    definition["actions"] = [
        action for action in definition.get("actions", [])
        if action.get("type") in ALLOWED_ACTIONS
    ]
    if not definition["actions"]:
        definition["actions"] = [{"type": "create_task", "title": "Review {name} at {phone}"}]
    definition["trigger"] = {
        "type": definition.get("trigger", {}).get("type"),
        "stage_key": definition.get("trigger", {}).get("stage_key", ""),
    }
    if definition["trigger"]["type"] not in ALLOWED_TRIGGERS:
        definition["trigger"]["type"] = "activity_added"
    try:
        definition["delay_minutes"] = max(0, int(definition.get("delay_minutes", 0)))
    except (TypeError, ValueError):
        definition["delay_minutes"] = 0
    validated = validate_definition(definition)
    return validated, provider


def search_knowledge(sources: list[dict[str, Any]], query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Simple term-overlap retrieval over active knowledge sources."""
    terms = [term for term in re.split(r"\W+", query.lower()) if len(term) > 2]
    scored: list[tuple[int, dict[str, Any]]] = []
    for source in sources:
        content = (source.get("content") or "").lower()
        if not content:
            continue
        score = sum(content.count(term) for term in terms)
        if score:
            scored.append((score, source))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [source for _score, source in scored[:limit]]


def _evidence(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return ""
    start = max(0, text.rfind(".", 0, match.start()) + 1)
    end = text.find(".", match.end())
    if end < 0:
        end = min(len(text), match.end() + 140)
    return text[start:end + 1].strip()[:360]


def capture_conversation(transcript: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Extract reviewable CRM facts. This function never writes to the CRM."""
    text = (transcript or "").strip()
    if not text:
        raise ValueError("This call does not have a transcript to analyze.")
    proposed: dict[str, Any] = {}
    evidence: dict[str, Any] = {}

    references = extract_references_from_text(text)
    if references:
        proposed["reference_number"] = ", ".join(references)
        evidence["reference_number"] = {
            "quote": _evidence(text, r"reference|" + re.escape(references[0].split("-")[0])),
            "confidence": "high",
        }

    name_match = re.search(
        r"\b(?:my name is|this is|speaking with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b",
        text,
    )
    if name_match:
        name = sanitize_owner_name(name_match.group(1))
        if name:
            proposed["owner_name"] = name
            evidence["owner_name"] = {"quote": name_match.group(0), "confidence": "medium"}

    distress_patterns = (
        (r"\b(?:tax(?:es)? (?:are )?(?:behind|delinquent)|back taxes|foreclos|lien)\b", "Tax or lien pressure"),
        (r"\b(?:inherited|probate|estate|passed away|deceased)\b", "Inherited or estate situation"),
        (r"\b(?:divorce|medical bills?|lost (?:my|their) job|need (?:the )?money|urgent)\b", "Personal or financial pressure"),
        (r"\b(?:overgrown|vacant|unused|can't maintain|cannot maintain|problem property)\b", "Property burden"),
    )
    for pattern, label in distress_patterns:
        quote = _evidence(text, pattern)
        if quote:
            proposed["distress_signal"] = label
            evidence["distress_signal"] = {"quote": quote, "confidence": "medium"}
            break

    motivation_patterns = (
        (r"\b(?:want|ready|need|looking|trying) to sell\b", "Seller expressed interest in selling"),
        (r"\b(?:quick|fast|as soon as possible|cash closing|be done with it)\b", "Values a fast or simple sale"),
        (r"\b(?:don't use|do not use|tired of|no longer want) (?:it|the (?:land|property))\b", "No longer wants or uses the property"),
    )
    for pattern, label in motivation_patterns:
        quote = _evidence(text, pattern)
        if quote:
            proposed["seller_motivation"] = label
            evidence["seller_motivation"] = {"quote": quote, "confidence": "medium"}
            break

    full_market = _evidence(text, r"\b(?:full market|market value|retail|top dollar|what it(?:'s| is) worth)\b")
    price = _evidence(text, r"(?:\$\s?\d[\d,]*|\b\d[\d,]*\s+(?:dollars|thousand))")
    if full_market:
        proposed["price_expectation"] = "Expects full market value / retail pricing"
        evidence["price_expectation"] = {"quote": full_market, "confidence": "high"}
    elif price:
        proposed["price_expectation"] = price[:240]
        evidence["price_expectation"] = {"quote": price, "confidence": "medium"}

    if proposed.get("seller_motivation") and not full_market:
        proposed["follow_up_plan"] = "Review the property and motivation, then make a respectful discounted cash offer if it fits the buy box."
        proposed["follow_up_score"] = 4 if proposed.get("distress_signal") else 3
    elif full_market:
        proposed["follow_up_plan"] = "Confirm price expectations before spending more acquisition time; nurture if the seller only wants retail value."
        proposed["follow_up_score"] = 2

    handoff: dict[str, Any] | None = None
    rules = (
        (r"\b(?:stop calling|do not call|don't call|remove me|opt out|unsubscribe)\b", "Seller requested no further contact", "urgent"),
        (r"\b(?:lawyer|attorney|sue|legal action|cease and desist)\b", "Legal issue or threat mentioned", "urgent"),
        (r"\b(?:wrong person|not the owner|you have the wrong)\b", "Seller identity or ownership is uncertain", "high"),
        (r"\b(?:furious|harassing|harassment|leave me alone)\b", "Hostile or sensitive conversation", "high"),
    )
    for pattern, reason, priority in rules:
        quote = _evidence(text, pattern)
        if quote:
            handoff = {"reason": reason, "priority": priority, "evidence": quote}
            break
    return proposed, evidence, handoff


def evaluate_case(agent_type: str, input_data: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    """Run one deterministic regression case used by the in-product evaluation page."""
    if agent_type == "workflow_builder":
        actual, _provider = generate_workflow_draft(str(input_data.get("prompt") or ""))
        checks = []
        if expected.get("trigger"):
            checks.append(actual.get("trigger", {}).get("type") == expected["trigger"])
        if "delay_minutes" in expected:
            checks.append(actual.get("delay_minutes") == expected["delay_minutes"])
        if expected.get("action"):
            checks.append(expected["action"] in {a.get("type") for a in actual.get("actions", [])})
    else:
        proposed, evidence, handoff = capture_conversation(str(input_data.get("transcript") or ""))
        actual = {**proposed, "handoff": bool(handoff), "evidence": evidence}
        checks = [actual.get(key) == value for key, value in expected.items()]
    passed = bool(checks) and all(checks)
    return passed, actual, "All expectations passed." if passed else "One or more expected fields did not match."


__all__ = [
    "capture_conversation", "evaluate_case", "generate_workflow_draft",
    "search_knowledge", "validate_definition",
]
