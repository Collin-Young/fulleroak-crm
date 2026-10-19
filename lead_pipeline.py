import argparse
import base64
import csv
from difflib import SequenceMatcher
import json
import logging
import os
import re
import hashlib
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

_DLL_HANDLES = []
if os.name == "nt":
    # NVIDIA's Windows wheels keep runtime DLLs inside site-packages. Add those
    # private directories without changing the machine-wide PATH.
    nvidia_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    dll_dirs = (
        nvidia_root / "cublas" / "bin",
        nvidia_root / "cudnn" / "bin",
        nvidia_root / "cuda_nvrtc" / "bin",
    )
    for dll_dir in dll_dirs:
        if dll_dir.exists():
            _DLL_HANDLES.append(os.add_dll_directory(str(dll_dir)))
    available_dll_dirs = [str(path) for path in dll_dirs if path.exists()]
    if available_dll_dirs:
        os.environ["PATH"] = os.pathsep.join(available_dll_dirs + [os.environ.get("PATH", "")])

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import requests
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from tqdm import tqdm
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

from crm_db import CRMDatabase

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_QUERY = "{from:donotreply@vumber.com from:sms@vumber.com}"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
MP3_PATTERN = re.compile(r"https?://[^\s\"'<>]+\.mp3", re.IGNORECASE)
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TRANSCRIPTION_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_OPENROUTER_TRANSCRIPTION_MODEL = "openai/whisper-large-v3"

load_dotenv()


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag: str, attrs):
        attr_map = dict(attrs)
        for key in ("href", "src"):
            url = attr_map.get(key)
            if url:
                self.urls.add(url)


@dataclass
class CallMetadata:
    direction: str
    other_party_number: str
    other_party_label: str
    vumber_number: str
    vumber_label: str
    time_description: str
    timestamp: Optional[datetime]
    internal_epoch_ms: Optional[int]
    reference_hint: str = ""


@dataclass
class SmsMetadata:
    other_party_number: str
    vumber_number: str
    vumber_label: str
    message: str
    timestamp: Optional[datetime]
    time_description: str


@dataclass
class CallEvent:
    phone_number: str
    phone_number_normalized: str
    direction: str
    vumber_number: str
    vumber_label: str
    time_description: str
    timestamp_iso: Optional[str]
    internal_epoch_ms: Optional[int]
    transcript: str
    transcript_path: str
    attachment_path: str
    call_reference: str
    message_id: str
    thread_id: str
    metadata: CallMetadata

    def to_dict(self) -> Dict:
        return {
            "phone_number": self.phone_number,
            "phone_number_normalized": self.phone_number_normalized,
            "direction": self.direction,
            "vumber_number": self.vumber_number,
            "vumber_label": self.vumber_label,
            "time_description": self.time_description,
            "timestamp_iso": self.timestamp_iso,
            "internal_epoch_ms": self.internal_epoch_ms,
            "transcript": self.transcript,
            "transcript_path": self.transcript_path,
            "attachment_path": self.attachment_path,
            "call_reference": self.call_reference,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
        }


@dataclass
class LeadRecord:
    phone_number: str
    phone_number_normalized: str
    owner_name: str
    reference_number: str
    summary: str
    follow_up_plan: str
    follow_up_score: int
    reasoning: str
    distress_signal: str
    seller_motivation: str
    price_expectation: str
    call_count: int
    first_activity: Optional[str]
    latest_activity: Optional[str]
    call_direction_summary: str
    calls: List[Dict] = field(default_factory=list)


class LeadAggregate(BaseModel):
    owner_name: Optional[str] = Field("", description="Person or business associated with the lead")
    reference_number: Optional[str] = Field("", description="Lead or ticket identifier if present")
    summary: str = Field(..., description="Narrative summary of the multi-call conversation")
    follow_up_plan: str = Field(..., description="Concrete next steps for follow-up")
    follow_up_score: int = Field(..., ge=1, le=5, description="Priority score 1-5 where 5 means urgent follow-up")
    reasoning: str = Field(..., description="Short justification for the follow-up score")
    distress_signal: str = Field("", description="Specific evidence of financial, property, timing, or personal distress; empty if none")
    seller_motivation: str = Field("", description="Why the owner might sell; empty if no motivation is established")
    price_expectation: str = Field("", description="Seller price posture, especially willingness or unwillingness to accept a discounted cash price")

    @field_validator("distress_signal", "seller_motivation", "price_expectation", mode="before")
    @classmethod
    def stringify_analysis_fields(cls, value):
        return "" if value is None else str(value)


PHONE_DIGIT_PATTERN = re.compile(r"\D+")
# North American display formats plus contiguous international/spoofed caller IDs
# that Vumber occasionally supplies (for example, 912261182300).
PHONE_VALUE = r"(?:(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}|\+?\d{7,15})"
CALL_ID_PATTERN = re.compile(r"recording/([^./]+)", re.IGNORECASE)
CALL_TIME_PATTERN = re.compile(r"Time:\s*(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M)", re.IGNORECASE)
FROM_PATTERN = re.compile(r"From:\s*([0-9()+\- ]+)")
TO_PATTERN = re.compile(r"To:\s*([0-9()+\- ]+)")
VUMBER_PATTERN = re.compile(r"Vumber:\s*([0-9()+\- ]+)")


def html_to_text(html_content: str) -> str:
    if not html_content:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html_content)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = unescape(text)
    text = text.replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def normalize_phone(number: str) -> str:
    digits = re.sub(PHONE_DIGIT_PATTERN, "", number or "")
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits


def extract_call_metadata(payload: Dict) -> Optional[CallMetadata]:
    for body in iter_text_bodies(payload):
        text = html_to_text(body)
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        full_text = " ".join(lines)
        lowered_full = full_text.lower()

        direction = "incoming"  # Default for Vumber notifications
        if "outgoing" in lowered_full or "you made a call" in lowered_full:
            direction = "outgoing"
        elif "missed" in lowered_full:
            direction = "missed"

        # Vumber's current template uses labeled fields. The lead is From on an
        # inbound/missed call and To on an outbound call; the Vumber is our line.
        labeled_pattern = TO_PATTERN if direction == "outgoing" else FROM_PATTERN
        labeled_match = labeled_pattern.search(full_text)

        # Older templates put phone and time on the first line.
        first_line = lines[0]
        parse_pattern = r"^(\(?\d{3}[)\s-]?\d{3}[-\s]?\d{4})\s+Time:\s+(.+?)(?:\s+Listen to your recorded call|$)"
        parse_match = re.match(parse_pattern, first_line)
        other = ""
        time_str = ""
        if labeled_match:
            other = labeled_match.group(1).strip()
        elif parse_match:
            other = parse_match.group(1).strip()
            time_str = parse_match.group(2).strip()
        else:
            # Fallback: extract phone from first line
            phone_match = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", first_line)
            if phone_match:
                other = phone_match.group(0)
            # Fallback time from full text
            time_match = CALL_TIME_PATTERN.search(full_text)
            if time_match:
                time_str = time_match.group(1)

        other_label = "From" if direction in ["incoming", "missed"] else "To"

        vumber_match = VUMBER_PATTERN.search(full_text)
        vumber_phone = vumber_match.group(1).strip() if vumber_match else ""
        vumber_label_match = re.search(
            rf"Vumber:\s*{PHONE_VALUE}\s*([^\n]*?)(?:\s+From:|\s+To:|\s+Time:|$)",
            full_text,
            re.IGNORECASE,
        )
        vumber_label = vumber_label_match.group(1).strip() if vumber_label_match else ""

        timestamp = None
        if time_str:
            try:
                # Handle with timezone if present
                if "Central Standard Time" in time_str:
                    dt_str = re.sub(r"\s+Central Standard Time.*", "", time_str)
                    try:
                        central_timezone = ZoneInfo("America/Chicago")
                    except Exception:
                        central_timezone = timezone(timedelta(hours=-6))
                    timestamp = datetime.strptime(dt_str, "%m/%d/%Y %I:%M:%S %p").replace(
                        tzinfo=central_timezone
                    )
                else:
                    timestamp = datetime.strptime(time_str, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=timezone.utc)
            except ValueError:
                timestamp = None

        # Extract reference_hint from subject or body
        reference_hint = ""
        for pattern in REFERENCE_PATTERNS:
            match = pattern.search(full_text)
            if match:
                reference_hint = match.group(1).strip()
                break

        return CallMetadata(
            direction=direction,
            other_party_number=other,
            other_party_label=other_label,
            vumber_number=normalize_phone(vumber_phone),
            vumber_label=vumber_label,
            time_description=time_str,
            timestamp=timestamp,
            internal_epoch_ms=None,
            reference_hint=reference_hint,
        )
    return None


def extract_call_metadata_from_subject(
    subject: str, internal_epoch_ms: Optional[int] = None
) -> Optional[CallMetadata]:
    """Parse the caller and direction from a Vumber notification subject."""
    subject = subject or ""
    outgoing = re.search(
        rf"Call made from your Vumber\s+(?P<vumber>{PHONE_VALUE})(?:\((?P<label>[^)]*)\))?\s+to\s+(?P<other>{PHONE_VALUE})",
        subject,
        re.IGNORECASE,
    )
    incoming = re.search(
        rf"(?P<kind>Call received|Missed call|Voice\s*mail)\s+from\s+(?P<other>{PHONE_VALUE})\s+to your Vumber\s+(?P<vumber>{PHONE_VALUE})(?:\((?P<label>[^)]*)\))?",
        subject,
        re.IGNORECASE,
    )
    match = outgoing or incoming
    if not match:
        return None
    direction = "outgoing" if outgoing else (
        "missed" if match.groupdict().get("kind", "").lower().startswith("missed") else "incoming"
    )
    occurred = (
        datetime.fromtimestamp(internal_epoch_ms / 1000, tz=timezone.utc)
        if internal_epoch_ms else None
    )
    return CallMetadata(
        direction=direction,
        other_party_number=match.group("other").strip(),
        other_party_label="To" if direction == "outgoing" else "From",
        vumber_number=normalize_phone(match.group("vumber")),
        vumber_label=(match.groupdict().get("label") or "").strip(),
        time_description="",
        timestamp=occurred,
        internal_epoch_ms=internal_epoch_ms,
        reference_hint="",
    )


def extract_sms_metadata(
    subject: str, payload: Dict, internal_epoch_ms: Optional[int] = None,
) -> Optional[SmsMetadata]:
    """Parse Vumber's inbound SMS notification email."""
    match = re.search(
        rf"SMS message from\s+(?P<other>{PHONE_VALUE})\s+to your Vumber\s+"
        rf"(?P<vumber>{PHONE_VALUE})(?:\((?P<label>[^)]*)\))?",
        subject or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    message_text = ""
    time_description = ""
    timestamp = None
    for raw_body in iter_text_bodies(payload):
        text = html_to_text(raw_body)
        if not text:
            continue
        time_match = re.search(r"(?im)^Time:\s*(.+?)\s*$", text)
        if time_match:
            time_description = time_match.group(1).strip()
        message_match = re.search(r"(?ims)^Message:\s*(.*)$", text)
        if message_match:
            message_text = message_match.group(1).strip()
            break
    if time_description:
        cleaned_time = re.sub(r"\s+Central (?:Standard|Daylight) Time.*$", "", time_description)
        try:
            try:
                central_timezone = ZoneInfo("America/Chicago")
            except Exception:
                central_timezone = timezone(timedelta(hours=-6))
            timestamp = datetime.strptime(cleaned_time, "%m/%d/%Y %I:%M:%S %p").replace(
                tzinfo=central_timezone
            )
        except ValueError:
            timestamp = None
    if timestamp is None and internal_epoch_ms:
        timestamp = datetime.fromtimestamp(internal_epoch_ms / 1000, tz=timezone.utc)
    return SmsMetadata(
        other_party_number=match.group("other").strip(),
        vumber_number=normalize_phone(match.group("vumber")),
        vumber_label=(match.groupdict().get("label") or "").strip(),
        message=message_text,
        timestamp=timestamp,
        time_description=time_description,
    )


REFERENCE_PATTERNS = [
    re.compile(r"\b(?:reference|ref\.?|ticket)(?:\s*(?:number|no\.?|#))?\s*(?:is|:|#|-)?\s*([A-Z][A-Z0-9]*-\d+(?:-\d+)*)\b", re.IGNORECASE),
    re.compile(r"\b([A-Z]{2,12}-\d{2,}(?:-\d+)*)\b", re.IGNORECASE),
]

SPOKEN_DIGITS = {
    "ZERO": "0", "OH": "0", "O": "0", "ONE": "1", "WON": "1",
    "TWO": "2", "TO": "2", "TOO": "2", "THREE": "3", "FOUR": "4",
    "FOR": "4", "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8",
    "ATE": "8", "NINE": "9",
}
PHONETIC_LETTERS = {
    "ALPHA": "A", "APPLE": "A", "ADAM": "A",
    "BRAVO": "B", "BOY": "B", "BAKER": "B",
    "CHARLIE": "C", "CAT": "C",
    "DELTA": "D", "DOG": "D", "DAVID": "D",
    "ECHO": "E", "EDWARD": "E", "EVERYONE": "E",
    "FOXTROT": "F", "FRANK": "F",
    "GOLF": "G", "GEORGE": "G",
    "HOTEL": "H", "HENRY": "H",
    "INDIA": "I", "IDA": "I",
    "JULIET": "J", "JOHN": "J",
    "KILO": "K", "KING": "K",
    "LIMA": "L", "LINCOLN": "L",
    "MIKE": "M", "MARY": "M", "ME": "M",
    "NOVEMBER": "N", "NANCY": "N", "NONE": "N",
    "OSCAR": "O",
    "PAPA": "P", "PETER": "P",
    "QUEBEC": "Q", "QUEEN": "Q",
    "ROMEO": "R", "ROBERT": "R", "RAYMOND": "R",
    "SIERRA": "S", "SAM": "S",
    "TANGO": "T", "TOM": "T",
    "UNIFORM": "U", "UNION": "U",
    "VICTOR": "V", "VICTORY": "V",
    "WHISKEY": "W", "WILLIAM": "W",
    "XRAY": "X", "X-RAY": "X",
    "YANKEE": "Y", "YOUNG": "Y",
    "ZULU": "Z", "ZEBRA": "Z",
}
REFERENCE_STOPWORDS = {
    "THE", "WAS", "IS", "IT", "LIKE", "WITH", "AND", "NUMBER", "REFERENCE",
    "TOP", "LETTER", "COUNTY", "PROPERTY", "THAT", "THIS", "YOU", "YOUR", "UM",
    "UH", "SAY", "SAYS", "SOME", "GOT", "HAVE", "HAS", "AT", "OF", "IN", "ON",
}
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def canonicalize_reference(value: str) -> str:
    value = (value or "").upper().strip().replace("—", "-").replace("–", "-")
    value = re.sub(r"\s+", "", value)
    match = re.fullmatch(r"([A-Z]{4,5})-(\d{3,4})-(\d{1,5})", value)
    if not match:
        return ""
    prefix, middle, final = match.groups()
    if len(prefix) == 5:
        collapsed = re.sub(r"(.)\1", r"\1", prefix)
        if len(collapsed) == 4 and collapsed[-2:] in US_STATE_CODES:
            prefix = collapsed
        elif prefix[-4:][-2:] in US_STATE_CODES:
            prefix = prefix[-4:]
        elif prefix[:4][-2:] in US_STATE_CODES:
            prefix = prefix[:4]
        else:
            return ""
    if len(prefix) != 4 or prefix[-2:] not in US_STATE_CODES:
        return ""
    return f"{prefix}-{middle}-{final}"


def format_display_phone(number: str) -> str:
    digits = normalize_phone(number)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 7:
        return f"{digits[:3]}-{digits[3:]}"
    return number.strip()


def extract_reference_from_text(text: str) -> str:
    if not text:
        return ""
    for pattern in REFERENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            reference = canonicalize_reference(match.group(1))
            if reference:
                return reference
    return ""


def extract_references_from_text(text: str) -> List[str]:
    found: List[str] = []
    for pattern in REFERENCE_PATTERNS:
        for match in pattern.finditer(text or ""):
            value = canonicalize_reference(match.group(1))
            if not value:
                continue
            if value not in found:
                found.append(value)
    for value in extract_spoken_references(text):
        if value not in found:
            found.append(value)
    return found


def _canonical_spoken_reference(prefix: str, groups: List[str]) -> str:
    prefix = re.sub(r"[^A-Z]", "", prefix.upper())
    groups = [re.sub(r"\D", "", group) for group in groups]
    groups = [group for group in groups if group]
    digits = "".join(groups)
    if not (4 <= len(prefix) <= 8 and 5 <= len(digits) <= 9):
        return ""
    # Whisper often inserts a hyphen between every spoken digit. Campaign
    # references conventionally use a four-digit middle segment.
    if not groups or all(len(group) == 1 for group in groups):
        groups = [digits[:4], digits[4:]]
    elif len(groups) == 1 and len(digits) > 4:
        groups = [digits[:4], digits[4:]]
    elif len(groups) > 2 and all(len(group) <= 2 for group in groups):
        groups = [digits[:4], digits[4:]]
    if len(groups) != 2 or not all(groups):
        return ""
    return canonicalize_reference(f"{prefix}-{groups[0]}-{groups[1]}")


def extract_spoken_references(text: str) -> List[str]:
    """Recover references dictated as letters/phonetics, digits, and “dash”."""
    if not text:
        return []
    windows: List[str] = []
    for marker in re.finditer(r"\breference(?:\s+(?:number|no\.?))?\b", text, re.IGNORECASE):
        # Callers often chat before dictating the number, so keep a wide window.
        windows.append(text[marker.end():marker.end() + 700])

    results: List[str] = []
    for window in windows:
        normalized = window.upper().replace("X RAY", "XRAY")
        normalized = re.sub(
            r"\b([A-Z])\s+(?:IS|AS|LIKE)\s+(?:IN|AN)\s+[A-Z]+\b", r" \1 ", normalized
        )
        normalized = re.sub(r"\b(?:DASH|HYPHEN)\b", " - ", normalized)
        tokens = re.findall(r"[A-Z]+|\d+|-", normalized)
        for index, token in enumerate(tokens):
            prefix = ""
            prefix_from_compact = False
            cursor = index
            if (
                3 <= len(token) <= 8
                and token not in REFERENCE_STOPWORDS
                and token not in PHONETIC_LETTERS
            ):
                prefix = token
                prefix_from_compact = True
                cursor += 1
            else:
                letters: List[str] = []
                while cursor < len(tokens) and len(letters) < 8:
                    current = tokens[cursor]
                    letter = current if len(current) == 1 and current.isalpha() else PHONETIC_LETTERS.get(current)
                    if letter:
                        letters.append(letter)
                        cursor += 1
                        if cursor < len(tokens) and tokens[cursor] == "-":
                            cursor += 1
                        continue
                    break
                if len(letters) >= 3:
                    prefix = "".join(letters)
                else:
                    continue

            had_leading_dash = cursor < len(tokens) and tokens[cursor] == "-"
            while cursor < len(tokens) and tokens[cursor] in {"-", "IS", "IN", "AS", "LIKE"}:
                cursor += 1
            if prefix_from_compact and not had_leading_dash:
                continue
            groups: List[str] = []
            current_digits = ""
            saw_separator = False
            last_numeric_was_multi = False
            while cursor < len(tokens):
                current = tokens[cursor]
                digit_value = current if current.isdigit() else SPOKEN_DIGITS.get(current)
                if digit_value is not None:
                    current_is_multi = current.isdigit() and len(current) > 1
                    if current_digits and (last_numeric_was_multi or current_is_multi):
                        break
                    current_digits += digit_value
                    last_numeric_was_multi = current_is_multi
                    cursor += 1
                    continue
                if current == "-" and current_digits:
                    groups.append(current_digits)
                    current_digits = ""
                    last_numeric_was_multi = False
                    saw_separator = True
                    cursor += 1
                    continue
                break
            if current_digits:
                groups.append(current_digits)
            candidate = _canonical_spoken_reference(prefix, groups)
            if candidate and candidate not in results:
                results.append(candidate)
            # A compact prefix without a dash still needs a substantial digit run.
            if not saw_separator and candidate and len("".join(groups)) < 7:
                results.remove(candidate)
    return results


def extract_reference_digit_groups(text: str) -> List[str]:
    """Recover reference digit groups even when the letter prefix is garbled.

    Callers often dictate “P as in Papa, U, M as in Mary, O-2618-853” or the
    transcriber mangles the prefix (“0RMO-2612-560”, “C-M-O dash two six one
    four dash one six seven seven”). The digit groups alone (e.g. 2618-853)
    are usually unique in the offers database, so return them as fallback
    candidates in the form “2618-853”.
    """
    if not text:
        return []
    normalized = (text or "").upper().replace("–", "-").replace("—", "-")
    # Remove phone numbers so their digit runs are never mistaken for refs.
    normalized = re.sub(
        rf"(?<!\d){PHONE_VALUE}(?!\d)", " ", normalized
    )
    normalized = re.sub(r"\b(?:DASH|HYPHEN)\b", "-", normalized)
    results: List[str] = []
    # Literal digit groups: 2618-853, 2614-1677, 2612560, 612-560.
    for match in re.finditer(r"\b(\d{2,4})\s*-\s*(\d{2,5})\b", normalized):
        middle, final = match.group(1), match.group(2)
        if len(middle) < 2 or len(final) < 2:
            continue
        candidate = f"{middle}-{final}"
        if candidate not in results:
            results.append(candidate)
    # Spoken digit runs: “two six one four dash one six seven seven”.
    for marker in re.finditer(r"\breference(?:\s+(?:number|no\.?))?\b", normalized, re.IGNORECASE):
        window = normalized[marker.end():marker.end() + 700]
        tokens = re.findall(r"[A-Z]+|\d+|-", window)
        digits: List[str] = []
        for token in tokens:
            if token.isdigit():
                digits.append(token)
            elif token in SPOKEN_DIGITS:
                digits.append(SPOKEN_DIGITS[token])
            elif token == "-":
                digits.append("-")
            elif digits and digits[-1] != "-":
                break
        run = "".join(digits).strip("-")
        groups = [group for group in run.split("-") if group]
        # The tail of a letter-by-letter prefix leaks in as a lone digit
        # (“C-M-O dash …” → O reads as 0), so drop leading single digits.
        while groups and len(groups[0]) == 1:
            groups.pop(0)
        if len(groups) >= 2:
            middle, final = groups[0], groups[1]
            if 2 <= len(middle) <= 4 and 2 <= len(final) <= 5:
                candidate = f"{middle}-{final}"
                if candidate not in results:
                    results.append(candidate)
    # Drop fragments fully contained inside a longer fragment: callers often
    # repeat partial groups (“2612-560 … 612-560”) and only the full pair helps.
    kept: List[str] = []
    for candidate in results:
        digits = candidate.replace("-", "")
        if any(
            other != candidate and digits in other.replace("-", "")
            for other in results
        ):
            continue
        if candidate not in kept:
            kept.append(candidate)
    return kept


def sanitize_owner_name(name: str) -> str:
    """Never identify the operator, Collin Young, as the external lead."""
    value = (name or "").strip()
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    tokens = normalized.split()
    collin_like = any(
        max(
            SequenceMatcher(None, token, "collin").ratio(),
            SequenceMatcher(None, token, "colin").ratio(),
            SequenceMatcher(None, token, "calling").ratio(),
        ) >= 0.72
        for token in tokens
    )
    return "" if collin_like else value


def infer_owner_name(model_name: str, summary: str, events: List[CallEvent]) -> str:
    """Use explicit self-identification when the model leaves the owner unnamed."""
    cleaned = sanitize_owner_name(model_name)
    if cleaned:
        return cleaned
    sources = [summary or ""] + [event.transcript or "" for event in events]
    patterns = (
        r"\b(?:my name is|this is|speaking with|hey[, ]+)\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b",
        r",\s*([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?:called|contacted|reached|returned)\b",
    )
    for source in sources:
        for pattern in patterns:
            match = re.search(pattern, source)
            if match:
                candidate = sanitize_owner_name(match.group(1).strip())
                if candidate:
                    return candidate
    return ""


def choose_reference_number(model_value: str, candidates: List[str]) -> str:
    """Keep only reference-shaped values; never mistake recording IDs for lead refs."""
    verified: List[str] = []
    for candidate in candidates:
        value = canonicalize_reference(candidate)
        if value and value not in verified:
            verified.append(value)
    if verified:
        return ", ".join(verified)
    extracted = extract_reference_from_text(model_value or "")
    return extracted.upper() if extracted else ""


MAX_TRANSCRIPT_CHARS = 12000

def create_openai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is required for transcription and analysis.")
    return OpenAI(api_key=api_key)


def create_local_transcriber(model_size: str, device: str, compute_type: str):
    if WhisperModel is None:
        raise RuntimeError("Install faster-whisper to use local transcription (pip install faster-whisper).")
    logging.info("Loading faster-whisper model %s (device=%s, compute=%s)", model_size, device, compute_type)
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                logging.warning("Saved Gmail authorization expired; opening Google authorization again")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def gmail_service(credentials: Credentials):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def iter_messages(service, user_id: str, query: str, max_results: Optional[int]) -> Iterable[Dict]:
    try:
        request = service.users().messages().list(userId=user_id, q=query, maxResults=max_results)
        while request is not None:
            response = request.execute()
            for msg in response.get("messages", []):
                yield msg
            request = service.users().messages().list_next(request, response)
    except HttpError as exc:
        logging.error("Failed to list messages: %s", exc)
        raise


def rebuild_lead_identities(
    service, db: CRMDatabase, message_metas: List[Dict]
) -> Dict[str, int]:
    """Reassign stored calls using the lead phone found in Vumber subjects."""
    stats = {"messages": 0, "calls_remapped": 0, "unparsed": 0}
    for meta in tqdm(message_metas, desc="Grouping calls by lead phone"):
        message_id = meta["id"]
        message = None
        for attempt in range(6):
            try:
                message = service.users().messages().get(
                    userId="me", id=message_id, format="metadata", metadataHeaders=["Subject"]
                ).execute()
                break
            except HttpError as exc:
                if exc.resp.status not in (403, 429) or attempt == 5:
                    logging.error("Could not fetch Gmail metadata for %s: %s", message_id, exc)
                    break
                time.sleep(min(30, 2 ** attempt))
        if not message:
            continue
        stats["messages"] += 1
        headers = {
            header.get("name", "").lower(): header.get("value", "")
            for header in message.get("payload", {}).get("headers", [])
        }
        epoch = int(message.get("internalDate", "0") or 0) or None
        metadata = extract_call_metadata_from_subject(headers.get("subject", ""), epoch)
        if not metadata:
            stats["unparsed"] += 1
            continue
        normalized = normalize_phone(metadata.other_party_number)
        stats["calls_remapped"] += db.remap_message_calls(
            message_id=message_id,
            phone_normalized=normalized,
            phone_display=format_display_phone(metadata.other_party_number),
            direction=metadata.direction,
            vumber_number=metadata.vumber_number,
            vumber_label=metadata.vumber_label,
        )
        time.sleep(0.04)
    removed = db.finalize_identity_rebuild()
    logging.info(
        "Identity rebuild: %d messages, %d calls remapped, %d unparsed, %d obsolete leads removed",
        stats["messages"], stats["calls_remapped"], stats["unparsed"], removed,
    )
    return stats


def iter_attachment_parts(payload: Dict) -> Iterable[Dict]:
    if not payload:
        return
    filename = payload.get('filename')
    if filename:
        yield payload
    for child in payload.get('parts', []) or []:
        yield from iter_attachment_parts(child)


def iter_text_bodies(payload: Dict) -> Iterable[str]:
    if not payload:
        return
    filename = payload.get('filename')
    mime_type = (payload.get('mimeType') or '').lower()
    body = payload.get('body') or {}
    data = body.get('data')
    if not filename and data and mime_type in {'text/plain', 'text/html'}:
        try:
            yield base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
        except (ValueError, UnicodeDecodeError):
            logging.debug('Failed to decode message body segment: mime=%s', mime_type)
    for child in payload.get('parts', []) or []:
        yield from iter_text_bodies(child)


def extract_vumber_urls(payload: Dict) -> List[str]:
    urls: set[str] = set()
    for body in iter_text_bodies(payload):
        parser = LinkExtractor()
        parser.feed(body)
        urls.update(parser.urls)
        urls.update(match.group(0).rstrip(').,') for match in URL_PATTERN.finditer(body))
    return sorted(urls)


def resolve_audio_urls(urls: Iterable[str], session: requests.Session) -> List[str]:
    resolved: List[str] = []
    seen: set[str] = set()
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'}:
            continue
        hostname = (parsed.netloc or '').lower()
        if 'vumber.com' not in hostname:
            continue
        if parsed.path.lower().endswith('.mp3'):
            candidate_urls = [url]
        else:
            try:
                response = session.get(url, timeout=30)
            except requests.RequestException as exc:
                logging.warning('Failed to resolve %s: %s', url, exc)
                continue
            final_url = response.url or url
            final_path = urlparse(final_url).path or ''
            content_type = (response.headers.get('content-type') or '').lower()
            if final_path.lower().endswith('.mp3') or 'audio' in content_type:
                candidate_urls = [final_url]
            else:
                mp3_matches = MP3_PATTERN.findall(response.text or '')
                if mp3_matches:
                    candidate_urls = mp3_matches
                else:
                    continue
        for resolved_url in candidate_urls:
            if resolved_url not in seen:
                seen.add(resolved_url)
                resolved.append(resolved_url)
    return resolved


def download_vumber_audio_links(payload: Dict, session: requests.Session, output_dir: Path, message_id: str) -> List[Path]:
    result: List[Path] = []
    urls = resolve_audio_urls(extract_vumber_urls(payload), session)
    if not urls:
        return result
    ensure_directory(output_dir)
    for index, url in enumerate(urls, start=1):
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.warning('Failed to download audio %s: %s', url, exc)
            continue
        content_type = (response.headers.get('content-type') or '').lower()
        if 'audio' not in content_type and not url.lower().endswith('.mp3'):
            logging.debug('Skipping non-audio url %s (content-type %s)', url, content_type)
            continue
        filename = Path(urlparse(url).path).name or f'audio_{index}.mp3'
        if not filename.lower().endswith('.mp3'):
            filename = f'{filename}.mp3'
        destination = output_dir / f"{message_id}_{filename}"
        if destination.exists():
            logging.info('Skipping existing remote audio: %s', destination)
        else:
            destination.write_bytes(response.content)
            logging.info('Saved remote audio: %s', destination)
        result.append(destination)
    return result


def download_mp3_attachments(service, user_id: str, message: Dict, output_dir: Path) -> List[Path]:
    result_paths: List[Path] = []
    message_id = message.get('id', '')
    payload = message.get('payload', {})
    for part in iter_attachment_parts(payload):
        filename = part.get('filename', '')
        if not filename.lower().endswith('.mp3'):
            continue
        body = part.get('body', {})
        data = body.get('data')
        if not data:
            attachment_id = body.get('attachmentId')
            if not attachment_id:
                continue
            attachment = service.users().messages().attachments().get(userId=user_id, messageId=message_id, id=attachment_id).execute()
            data = attachment.get('data')
        if not data:
            continue
        file_bytes = base64.urlsafe_b64decode(data)
        ensure_directory(output_dir)
        destination = output_dir / f"{message_id}_{filename}"
        if destination.exists():
            logging.info('Skipping existing attachment: %s', destination)
        else:
            destination.write_bytes(file_bytes)
            logging.info('Saved attachment: %s', destination)
        result_paths.append(destination)
    return result_paths


def transcribe_audio(
    audio_path: Path,
    provider: str,
    openai_client: Optional[OpenAI],
    openai_model: str,
    local_model,
    local_beam_size: int,
    openrouter_key: Optional[str] = None,
    http_session: Optional[requests.Session] = None,
) -> str:
    if provider == "openai":
        if openai_client is None:
            raise RuntimeError("OpenAI client is not initialized")
        with audio_path.open("rb") as fh:
            response = openai_client.audio.transcriptions.create(model=openai_model, file=fh)
        text = response.text.strip()
    elif provider == "local":
        if local_model is None:
            raise RuntimeError("Local transcription model is not initialized")
        segments, _ = local_model.transcribe(
            str(audio_path), beam_size=local_beam_size, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    elif provider == "openrouter":
        if not openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter transcription")
        audio_format = audio_path.suffix.lower().lstrip(".") or "mp3"
        payload = {
            "model": openai_model,
            "input_audio": {
                "data": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
                "format": audio_format,
            },
        }
        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://openrouter.ai"),
            "X-Title": os.environ.get("OPENROUTER_APP_NAME", "Fuller Oak CRM"),
        }
        response = (http_session or requests.Session()).post(
            OPENROUTER_TRANSCRIPTION_ENDPOINT, headers=headers, json=payload, timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        text = str(data.get("text") or "").strip()
        if not text:
            raise RuntimeError("OpenRouter transcription returned no text")
    else:
        raise ValueError(f"Unknown transcription provider: {provider}")
    logging.debug("Transcript for %s: %s", audio_path.name, text[:120])
    return text


def summarize_lead(
    provider: str,
    model: str,
    events: List[CallEvent],
    phone_number: str,
    openai_client: Optional[OpenAI],
    openrouter_key: Optional[str],
    http_session: requests.Session,
) -> LeadAggregate:
    prompt = (
        "You are a sales operations assistant creating one unified lead story from the complete chronological call history. "
        "Each call entry includes the direction (incoming/outgoing/missed), the business Vumber line used, any reference hints, "
        "and a transcript excerpt. Treat incoming as the lead calling us and outgoing as us calling the lead. "
        "The summary must narrate how the relationship progressed from the first call through the latest call, including promises, "
        "questions, attempted callbacks, decisions, and unresolved items. Do not summarize each call in isolation. "
        "Return a single JSON object (not a list) with fields owner_name, reference_number, summary, follow_up_plan, follow_up_score, reasoning, distress_signal, seller_motivation, price_expectation. "
        "If owner_name or reference_number cannot be determined, use empty string \"\". "
        "Collin Young (including transcription variants such as Colin, Collen, Calling, or Colen Young) is our operator and must never be owner_name. "
        "References are often spoken letter-by-letter or with phonetic words (Alpha, Bravo, Charlie) and the word dash; reconstruct them with hyphens. "
        "We buy land only when there is room for a heavy discount below full market value. Explicitly state any distress signal, the owner's actual motivation to sell, and their price posture. "
        "Curiosity, asking what we would pay, or demanding full market value is not strong motivation and should normally score 1 or 2. "
        "Score 5 only for clear distress or urgency plus strong evidence the owner may accept a deeply discounted cash price; score 4 for meaningful motivation and real price flexibility; score 3 when openness exists but motivation or discount is unproven; score 2 for price shopping/full-market expectations; score 1 for no interest, removal requests, or hostility. "
        "Use 'None identified' when no distress or motivation is supported. Do not fabricate information."
    )

    event_payload = []
    reference_candidates: List[str] = []
    for event in events:
        truncated = event.transcript
        truncated_flag = False
        if truncated and len(truncated) > MAX_TRANSCRIPT_CHARS:
            truncated = truncated[:MAX_TRANSCRIPT_CHARS] + " ... [truncated]"
            truncated_flag = True
        event_payload.append(
            {
                "timestamp": event.time_description or event.timestamp_iso or "",
                "direction": event.direction,
                "vumber_label": event.vumber_label,
                "reference_hint": event.metadata.reference_hint,
                "call_reference": event.call_reference,
                "transcript_excerpt": truncated,
                "transcript_truncated": truncated_flag,
                "transcript_path": event.transcript_path,
                "attachment_path": event.attachment_path,
            }
        )
        for source in (event.transcript, event.metadata.reference_hint, event.call_reference):
            for reference in extract_references_from_text(source):
                if reference not in reference_candidates:
                    reference_candidates.append(reference)

    content = (
        "Phone number: {phone}\n"
        "Call events (oldest to newest) in JSON: \n"
        "{events}\n"
        "Reference numbers found deterministically across calls: {references}\n"
        "Respond ONLY with valid JSON conforming to the specified fields."
    ).format(
        phone=phone_number or 'unknown',
        events=json.dumps(event_payload, ensure_ascii=False),
        references=json.dumps(reference_candidates),
    )

    user_message = {
        "role": "user",
        "content": content,
    }


    if provider == "openai":
        if openai_client is None:
            raise RuntimeError("OpenAI client is not initialized for analysis.")
        response = openai_client.responses.parse(
            model=model,
            input=[{"role": "system", "content": prompt}, user_message],
            text_format=LeadAggregate,
        )
        if response.output_parsed is None:
            raise RuntimeError("Lead analysis returned no structured output")
        parsed_result = response.output_parsed
    elif provider == "openrouter":
        if not openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY environment variable is required for OpenRouter analysis.")
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": prompt}, user_message],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://openrouter.ai"),
            "X-Title": os.environ.get("OPENROUTER_APP_NAME", "lead-pipeline"),
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = http_session.post(OPENROUTER_ENDPOINT, headers=headers, json=payload, timeout=300)
                response.raise_for_status()
                data = response.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                    break
                except (KeyError, IndexError) as exc:
                    raise RuntimeError(f"Unexpected OpenRouter response: {data}") from exc
            except requests.RequestException as exc:
                logging.warning(f"OpenRouter attempt {attempt + 1} failed: {exc}")
                if attempt == max_retries - 1:
                    raise RuntimeError(f"OpenRouter analysis failed after {max_retries} attempts: {exc}") from exc
                time.sleep(2 ** attempt)  # Exponential backoff
    else:
        raise ValueError(f"Unknown analysis provider: {provider}")

    if provider == "openrouter":
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list) and len(parsed) > 0:
                parsed = parsed[0]
            parsed_result = LeadAggregate.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            logging.error("Failed to parse lead analysis response: %s", content)
            raise RuntimeError("Lead analysis failed") from exc

    parsed_result.reference_number = choose_reference_number(
        parsed_result.reference_number or "", reference_candidates
    )
    parsed_result.owner_name = infer_owner_name(
        parsed_result.owner_name or "", parsed_result.summary, events
    )
    return parsed_result


def export_leads(leads: List[LeadRecord], output_dir: Path) -> None:
    ensure_directory(output_dir)
    json_path = output_dir / "leads.json"
    csv_path = output_dir / "leads.csv"
    sorted_leads = sorted(leads, key=lambda lead: lead.follow_up_score, reverse=True)
    json_data = [asdict(lead) for lead in sorted_leads]
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = [
        "phone_number",
        "phone_number_normalized",
        "owner_name",
        "reference_number",
        "call_count",
        "first_activity",
        "latest_activity",
        "call_direction_summary",
        "follow_up_score",
        "summary",
        "follow_up_plan",
        "reasoning",
        "distress_signal",
        "seller_motivation",
        "price_expectation",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for lead in sorted_leads:
            row = {
                "phone_number": lead.phone_number,
                "phone_number_normalized": lead.phone_number_normalized,
                "owner_name": lead.owner_name,
                "reference_number": lead.reference_number,
                "call_count": lead.call_count,
                "first_activity": lead.first_activity,
                "latest_activity": lead.latest_activity,
                "call_direction_summary": lead.call_direction_summary,
                "follow_up_score": lead.follow_up_score,
                "summary": lead.summary,
                "follow_up_plan": lead.follow_up_plan,
                "reasoning": lead.reasoning,
                "distress_signal": lead.distress_signal,
                "seller_motivation": lead.seller_motivation,
                "price_expectation": lead.price_expectation,
            }
            writer.writerow(row)
    logging.info("Saved lead summaries to %s and %s", json_path, csv_path)


def event_from_db_row(row: Dict, phone_number: str, phone_normalized: str) -> CallEvent:
    """Rehydrate a stored call so refreshed analysis includes the full CRM history."""
    metadata = CallMetadata(
        direction=row.get("direction", "unknown"),
        other_party_number=phone_number,
        other_party_label="",
        vumber_number=row.get("vumber_number", ""),
        vumber_label=row.get("vumber_label", ""),
        time_description=row.get("time_description", ""),
        timestamp=None,
        internal_epoch_ms=None,
        reference_hint=row.get("call_reference", ""),
    )
    return CallEvent(
        phone_number=phone_number,
        phone_number_normalized=phone_normalized,
        direction=row.get("direction", "unknown"),
        vumber_number=row.get("vumber_number", ""),
        vumber_label=row.get("vumber_label", ""),
        time_description=row.get("time_description", ""),
        timestamp_iso=row.get("occurred_at"),
        internal_epoch_ms=None,
        transcript=row.get("transcript", ""),
        transcript_path=row.get("transcript_path", ""),
        attachment_path=row.get("attachment_path", ""),
        call_reference=row.get("call_reference", ""),
        message_id=row.get("message_id", ""),
        thread_id=row.get("thread_id", ""),
        metadata=metadata,
    )


def export_crm(db: CRMDatabase, output_dir: Path) -> None:
    """Keep portable JSON/CSV exports in sync with the SQLite source of truth."""
    records: List[LeadRecord] = []
    for lead in db.all_leads():
        calls = db.calls_for_lead(lead["id"])
        records.append(LeadRecord(
            phone_number=lead["phone_display"],
            phone_number_normalized=lead["phone_normalized"],
            owner_name=lead["owner_name"],
            reference_number=lead["reference_number"],
            summary=lead["summary"],
            follow_up_plan=lead["follow_up_plan"],
            follow_up_score=lead["follow_up_score"],
            reasoning=lead["reasoning"],
            distress_signal=lead["distress_signal"],
            seller_motivation=lead["seller_motivation"],
            price_expectation=lead["price_expectation"],
            call_count=lead["call_count"],
            first_activity=lead["first_activity"],
            latest_activity=lead["latest_activity"],
            call_direction_summary="; ".join(
                f"{call.get('occurred_at') or call.get('time_description') or 'unknown time'} ({call.get('direction','unknown')})"
                for call in calls
            ),
            calls=[{
                "phone_number": lead["phone_display"],
                "phone_number_normalized": lead["phone_normalized"],
                "direction": call["direction"], "vumber_number": call["vumber_number"],
                "vumber_label": call["vumber_label"], "time_description": call["time_description"],
                "timestamp_iso": call["occurred_at"], "internal_epoch_ms": None,
                "transcript": call["transcript"], "transcript_path": call["transcript_path"],
                "attachment_path": call["attachment_path"], "call_reference": call["call_reference"],
                "message_id": call["message_id"], "thread_id": call["thread_id"],
            } for call in calls],
        ))
    if records:
        export_leads(records, output_dir)


def analyze_crm_database(
    db: CRMDatabase,
    provider: str,
    model: str,
    openai_client: Optional[OpenAI],
    openrouter_key: Optional[str],
    workers: int = 4,
    only_missing: bool = False,
) -> int:
    """Analyze stored leads independently from Gmail ingestion."""
    candidates = [lead for lead in db.all_leads() if lead["call_count"]]
    if only_missing:
        candidates = [
            lead for lead in candidates
            if not lead["summary"]
            or not (lead.get("distress_signal") or lead.get("seller_motivation") or lead.get("price_expectation"))
        ]

    def analyze_one(lead: Dict) -> int:
        call_rows = db.calls_for_lead(lead["id"], limit=50)
        events = [
            event_from_db_row(row, lead["phone_display"], lead["phone_normalized"])
            for row in call_rows
        ]
        if not any(event.transcript.strip() for event in events):
            return 0
        aggregate = summarize_lead(
            provider=provider,
            model=model,
            events=events,
            phone_number=lead["phone_display"],
            openai_client=openai_client,
            openrouter_key=openrouter_key,
            http_session=requests.Session(),
        )
        db.update_analysis(lead["id"], aggregate)
        return 1

    updated = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(analyze_one, lead): lead for lead in candidates}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Analyzing CRM leads"):
            lead = futures[future]
            try:
                updated += future.result()
            except Exception as exc:
                logging.error("Analysis failed for %s: %s", lead["phone_display"], exc)
    return updated


def process_messages(
    credentials_path: Path,
    token_path: Path,
    download_dir: Path,
    transcript_dir: Path,
    exports_dir: Path,
    gmail_query: str,
    max_messages: Optional[int],
    transcription_provider: str,
    transcription_model: str,
    analysis_provider: str,
    analysis_model: str,
    local_model_size: str,
    local_device: str,
    local_compute_type: str,
    local_beam_size: int,
    offline_mode: bool = False,
    database_path: Path = Path("data/leads.db"),
    force: bool = False,
    import_legacy: bool = False,
    reanalyze_all: bool = False,
    analysis_workers: int = 4,
    rebuild_identities: bool = False,
) -> None:
    db = CRMDatabase(database_path)
    if import_legacy:
        imported_leads, imported_calls = db.import_legacy_export(exports_dir / "leads.json")
        logging.info("Imported %d legacy leads and %d calls", imported_leads, imported_calls)
    if max_messages == 0 and not offline_mode:
        export_crm(db, exports_dir)
        logging.info("Import/export-only run complete")
        return
    run_id = db.start_sync()
    stats = {"messages_seen": 0, "messages_processed": 0, "calls_added": 0, "leads_updated": 0, "sms_added": 0}

    service = None
    if offline_mode:
        message_metas = []
    else:
        if credentials_path is None:
            raise RuntimeError("--credentials is required unless --offline-mode is used")
        creds = load_credentials(credentials_path, token_path)
        service = gmail_service(creds)
        message_metas = list(iter_messages(service, "me", gmail_query, None))
        stats["messages_seen"] = len(message_metas)
        if rebuild_identities:
            rebuild_lead_identities(service, db, message_metas)
            reanalyze_all = True
            message_metas = []
        if not force:
            message_metas = [meta for meta in message_metas if not db.message_complete(meta["id"])]
        if max_messages is not None:
            message_metas = message_metas[:max_messages]
        # Gmail returns newest first; process the selected batch chronologically.
        message_metas.reverse()
        if not message_metas:
            logging.info("CRM is up to date; no new Gmail messages matched '%s'", gmail_query)
            if reanalyze_all:
                openai_client = create_openai_client() if analysis_provider == "openai" else None
                openrouter_key = os.environ.get("OPENROUTER_API_KEY") if analysis_provider == "openrouter" else None
                updated = analyze_crm_database(
                    db, analysis_provider, analysis_model, openai_client, openrouter_key,
                    workers=analysis_workers,
                )
                stats["leads_updated"] = updated
            export_crm(db, exports_dir)
            db.finish_sync(run_id, "complete", stats)
            return

    openai_client: Optional[OpenAI] = None
    if analysis_provider == "openai" or (
        transcription_provider == "openai" and os.environ.get("OPENAI_API_KEY")
    ):
        openai_client = create_openai_client()

    local_model = None
    if transcription_provider == "local" and not offline_mode:
        local_model = create_local_transcriber(local_model_size, local_device, local_compute_type)

    openrouter_key: Optional[str] = None
    if analysis_provider == "openrouter" or transcription_provider == "openrouter":
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        if not openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY environment variable is required for OpenRouter.")

    audio_dir = download_dir / "audio"
    ensure_directory(audio_dir)
    ensure_directory(transcript_dir)

    events: List[CallEvent] = load_events_from_transcripts(transcript_dir, audio_dir) if offline_mode else []
    session = requests.Session()

    for meta in tqdm(message_metas, desc="Processing messages"):
        message_id = meta["id"]
        if not db.claim_message(message_id):
            continue
        logging.info("Processing message %s", message_id)
        message_had_error = False
        full_message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        internal_epoch_ms = int(full_message.get("internalDate", "0")) if full_message.get("internalDate") else None
        payload = full_message.get("payload", {})
        headers = {h.get("name"): h.get("value") for h in payload.get("headers", []) if h.get("name") and h.get("value")}
        subject = headers.get("Subject", "")
        sms = extract_sms_metadata(subject, payload, internal_epoch_ms)
        if sms:
            normalized_phone = normalize_phone(sms.other_party_number)
            display_phone = format_display_phone(sms.other_party_number)
            occurred_at = (sms.timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
            lead_id = db.ensure_lead(normalized_phone, display_phone)
            added = db.add_inbound_sms(
                lead_id=lead_id,
                provider_message_id=message_id,
                from_number=normalized_phone,
                to_number=sms.vumber_number,
                message=sms.message,
                occurred_at=occurred_at,
                response=json.dumps({"gmail_message_id": message_id, "subject": subject}),
            )
            if added:
                stats["sms_added"] += 1
                references = extract_references_from_text(sms.message)
                owner_name = infer_owner_name("", sms.message, [])
                with db.connect() as connection:
                    connection.execute(
                        """UPDATE leads SET
                            owner_name=CASE WHEN owner_name='' AND ?!='' THEN ? ELSE owner_name END,
                            reference_number=CASE WHEN reference_number='' AND ?!='' THEN ? ELSE reference_number END,
                            updated_at=? WHERE id=?""",
                        (
                            owner_name, owner_name,
                            references[0] if references else "", references[0] if references else "",
                            datetime.now(timezone.utc).isoformat(timespec="seconds"), lead_id,
                        ),
                    )
            db.mark_message(message_id, "complete")
            stats["messages_processed"] += 1
            continue
        metadata = extract_call_metadata(payload) or CallMetadata(
            direction="unknown",
            other_party_number="",
            other_party_label="",
            vumber_number="",
            vumber_label="",
            time_description="",
            timestamp=None,
            internal_epoch_ms=internal_epoch_ms,
            reference_hint="",
        )
        metadata.internal_epoch_ms = internal_epoch_ms

        attachments = download_mp3_attachments(service, "me", full_message, audio_dir)
        # Vumber often includes both an MP3 attachment and a playback link for the
        # same recording. Only resolve the remote link when no attachment exists.
        link_audio = [] if attachments else download_vumber_audio_links(payload, session, audio_dir, message_id)
        audio_sources = attachments + link_audio

        thread_id = full_message.get("threadId", "")
        if subject and not metadata.reference_hint and "ref" in subject.lower():
            metadata.reference_hint = subject

        normalized_phone = normalize_phone(metadata.other_party_number)
        display_phone = format_display_phone(metadata.other_party_number or normalized_phone or metadata.other_party_label)

        if not audio_sources:
            if metadata.direction == "missed":
                events.append(
                    CallEvent(
                        phone_number=display_phone,
                        phone_number_normalized=normalized_phone,
                        direction=metadata.direction,
                        vumber_number=metadata.vumber_number,
                        vumber_label=metadata.vumber_label,
                        time_description=metadata.time_description,
                        timestamp_iso=metadata.timestamp.isoformat() if metadata.timestamp else None,
                        internal_epoch_ms=metadata.internal_epoch_ms,
                        transcript="",
                        transcript_path="",
                        attachment_path="",
                        call_reference=metadata.reference_hint or f"missed-{message_id}",
                        message_id=message_id,
                        thread_id=thread_id,
                        metadata=metadata,
                    )
                )
                missed_event = events[-1]
                missed_lead_id = db.ensure_lead(
                    missed_event.phone_number_normalized or missed_event.call_reference,
                    missed_event.phone_number,
                )
                missed_call = missed_event.to_dict()
                missed_call["source_key"] = f"{missed_event.message_id}:{missed_event.call_reference}"
                stats["calls_added"] += int(db.add_call(missed_lead_id, missed_call))
            else:
                logging.info("No audio sources found in message %s", message_id)
            db.mark_message(message_id, "complete")
            stats["messages_processed"] += 1
            continue

        for attachment_path in audio_sources:
            transcript_file = transcript_dir / f"{Path(attachment_path).stem}.txt"
            if transcript_file.exists():
                transcript_text = transcript_file.read_text(encoding="utf-8")
            else:
                try:
                    if transcription_provider == "openai" and openai_client is None:
                        openai_client = create_openai_client()
                    transcript_text = transcribe_audio(
                        attachment_path,
                        transcription_provider,
                        openai_client,
                        transcription_model,
                        local_model,
                        local_beam_size,
                        openrouter_key=openrouter_key,
                        http_session=session,
                    )
                except Exception as exc:
                    logging.error("Transcription failed for %s: %s", attachment_path, exc)
                    message_had_error = True
                    continue

                transcript_file.write_text(transcript_text, encoding="utf-8")

            if not transcript_text.strip():
                logging.warning("Empty transcript for %s, skipping event", attachment_path.name)
                continue

            filename = Path(attachment_path).name
            parts = filename.split("_", 1)
            original_filename = parts[1] if len(parts) > 1 else parts[0]
            call_uuid = original_filename.replace(".mp3", "")

            metadata_reference = extract_reference_from_text(metadata.reference_hint) if metadata.reference_hint else ""
            transcript_reference = extract_reference_from_text(transcript_text)

            final_reference = transcript_reference or metadata_reference or call_uuid

            reference_parts = []
            if metadata.reference_hint:
                reference_parts.append(metadata.reference_hint)
            if transcript_reference and transcript_reference not in reference_parts:
                reference_parts.append(transcript_reference)
            if metadata_reference and metadata_reference not in reference_parts:
                reference_parts.append(metadata_reference)
            if not reference_parts:
                reference_parts.append(call_uuid)
            reference_hint = " | ".join(dict.fromkeys(reference_parts))

            event_metadata = CallMetadata(
                direction=metadata.direction,
                other_party_number=metadata.other_party_number,
                other_party_label=metadata.other_party_label,
                vumber_number=metadata.vumber_number,
                vumber_label=metadata.vumber_label,
                time_description=metadata.time_description,
                timestamp=metadata.timestamp,
                internal_epoch_ms=metadata.internal_epoch_ms,
                reference_hint=reference_hint,
            )

            events.append(
                CallEvent(
                    phone_number=display_phone,
                    phone_number_normalized=normalized_phone,
                    direction=metadata.direction,
                    vumber_number=metadata.vumber_number,
                    vumber_label=metadata.vumber_label,
                    time_description=metadata.time_description,
                    timestamp_iso=metadata.timestamp.isoformat() if metadata.timestamp else (
                        datetime.fromtimestamp(internal_epoch_ms / 1000, tz=timezone.utc).isoformat()
                        if internal_epoch_ms else None
                    ),
                    internal_epoch_ms=metadata.internal_epoch_ms,
                    transcript=transcript_text,
                    transcript_path=str(transcript_file),
                    attachment_path=str(attachment_path),
                    call_reference=final_reference,
                    message_id=message_id,
                    thread_id=thread_id,
                    metadata=event_metadata,
                )
            )
            stored_event = events[-1]
            stored_lead_id = db.ensure_lead(
                stored_event.phone_number_normalized or stored_event.call_reference,
                stored_event.phone_number,
            )
            stored_call = stored_event.to_dict()
            stored_call["source_key"] = f"{stored_event.message_id}:{stored_event.attachment_path or stored_event.call_reference}"
            stats["calls_added"] += int(db.add_call(stored_lead_id, stored_call))

        if message_had_error:
            db.mark_message(message_id, "failed", "One or more recordings could not be transcribed")
        else:
            db.mark_message(message_id, "complete")
            stats["messages_processed"] += 1

    if not events:
        logging.warning("No call events were processed for the provided query.")
        export_crm(db, exports_dir)
        db.finish_sync(run_id, "complete", stats)
        return

    grouped: Dict[str, List[CallEvent]] = defaultdict(list)
    for event in events:
        key = event.phone_number_normalized or normalize_phone(event.phone_number) or event.call_reference or event.message_id
        grouped[key].append(event)

    leads: List[LeadRecord] = []
    for key, lead_events in grouped.items():
        lead_events.sort(key=lambda e: (e.internal_epoch_ms or 0, e.timestamp_iso or ""))
        display_phone = lead_events[0].phone_number or format_display_phone(lead_events[0].phone_number_normalized or key)
        lead_id = db.ensure_lead(lead_events[0].phone_number_normalized or key, display_phone)
        for event in lead_events:
            call_data = event.to_dict()
            call_data["source_key"] = f"{event.message_id}:{event.attachment_path or event.call_reference}"
            stats["calls_added"] += int(db.add_call(lead_id, call_data))

        # Analyze stored history, not only the newest batch. Cap at 50 calls to keep
        # prompts bounded while retaining the most relevant recent history.
        stored_rows = db.calls_for_lead(lead_id, limit=50)
        lead_events = [
            event_from_db_row(row, display_phone, lead_events[0].phone_number_normalized or key)
            for row in stored_rows
        ]
        # Only analyze if there is at least one non-empty transcript
        has_transcript = any(e.transcript.strip() for e in lead_events)
        if not has_transcript:
            logging.warning("No transcripts for lead %s, skipping analysis", display_phone)
            continue
        aggregate = summarize_lead(
            provider=analysis_provider,
            model=analysis_model,
            events=lead_events,
            phone_number=display_phone,
            openai_client=openai_client,
            openrouter_key=openrouter_key,
            http_session=session,
        )
        db.update_analysis(lead_id, aggregate)
        stats["leads_updated"] += 1
        first_event = lead_events[0]
        last_event = lead_events[-1]
        direction_summary = "; ".join(
            f"{ev.time_description or ev.timestamp_iso or 'unknown time'} ({ev.direction})" for ev in lead_events
        )
        lead_record = LeadRecord(
            phone_number=display_phone,
            phone_number_normalized=lead_events[0].phone_number_normalized or key,
            owner_name=aggregate.owner_name,
            reference_number=aggregate.reference_number,
            summary=aggregate.summary,
            follow_up_plan=aggregate.follow_up_plan,
            follow_up_score=aggregate.follow_up_score,
            reasoning=aggregate.reasoning,
            distress_signal=aggregate.distress_signal,
            seller_motivation=aggregate.seller_motivation,
            price_expectation=aggregate.price_expectation,
            call_count=len(lead_events),
            first_activity=first_event.time_description or first_event.timestamp_iso,
            latest_activity=last_event.time_description or last_event.timestamp_iso,
            call_direction_summary=direction_summary,
            calls=[event.to_dict() for event in lead_events],
        )
        leads.append(lead_record)

    if not leads:
        logging.warning("No leads were extracted. Check your query or attachments.")
        return

    export_crm(db, exports_dir)
    db.finish_sync(run_id, "complete", stats)
    logging.info("Top follow-up candidates:")
    for lead in sorted(leads, key=lambda item: item.follow_up_score, reverse=True)[:5]:
        logging.info(
            "Score %s | %s | %s | Ref: %s",
            lead.follow_up_score,
            lead.owner_name or "(unknown)",
            lead.phone_number,
            lead.reference_number,
        )


def retranscribe_downloaded_audio(
    database_path: Path,
    provider: str,
    model: str,
    openrouter_key: Optional[str],
    limit: Optional[int] = None,
    local_model_size: str = "base",
    local_device: str = "cpu",
    local_compute_type: str = "int8",
    local_beam_size: int = 5,
    workers: int = 4,
) -> dict[str, int]:
    """Upgrade stored call transcripts safely; completed jobs are skipped on resume."""
    db = CRMDatabase(database_path)
    backup_path = database_path.with_name(f"{database_path.stem}.before-openrouter-transcription{database_path.suffix}")
    if not backup_path.exists():
        destination = sqlite3.connect(backup_path)
        try:
            with db.connect() as source:
                source.backup(destination)
        finally:
            destination.close()
        logging.info("Created database backup at %s", backup_path)

    openai_client = create_openai_client() if provider == "openai" else None
    local_model = (
        create_local_transcriber(local_model_size, local_device, local_compute_type)
        if provider == "local" else None
    )
    candidates = db.transcription_candidates(provider, model, limit=limit)
    stats = {"candidates": len(candidates), "completed": 0, "failed": 0, "missing": 0}

    def transcribe_one(call: Dict) -> str:
        audio_path = Path(call["attachment_path"])
        if not audio_path.is_absolute():
            audio_path = Path.cwd() / audio_path
        if not audio_path.exists():
            logging.warning("Call %s audio file is missing: %s", call["id"], audio_path)
            return "missing"
        audio_sha256 = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        db.begin_transcription_job(call["id"], provider, model, audio_sha256)
        try:
            transcript = ""
            last_error: Optional[Exception] = None
            session = requests.Session()
            for attempt in range(3):
                try:
                    transcript = transcribe_audio(
                        audio_path, provider, openai_client, model, local_model, local_beam_size,
                        openrouter_key=openrouter_key, http_session=session,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            if not transcript:
                raise RuntimeError(f"Transcription failed after 3 attempts: {last_error}")

            transcript_path = Path(call.get("transcript_path") or "")
            if not transcript_path.name:
                transcript_path = Path("transcripts") / f"{audio_path.stem}.txt"
            if not transcript_path.is_absolute():
                transcript_path = Path.cwd() / transcript_path
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            if transcript_path.exists():
                history_dir = transcript_path.parent / "history"
                history_dir.mkdir(parents=True, exist_ok=True)
                history_path = history_dir / f"{transcript_path.stem}.before-{provider}.txt"
                if not history_path.exists():
                    shutil.copy2(transcript_path, history_path)
            transcript_path.write_text(transcript, encoding="utf-8")
            stored_path = str(transcript_path.relative_to(Path.cwd())) if transcript_path.is_relative_to(Path.cwd()) else str(transcript_path)
            db.complete_transcription_job(
                call["id"], provider, model, audio_sha256, transcript, stored_path,
            )
            return "completed"
        except Exception as exc:
            logging.error("Call %s transcription failed: %s", call["id"], exc)
            db.fail_transcription_job(call["id"], provider, model, audio_sha256, str(exc))
            return "failed"

    effective_workers = 1 if provider == "local" else max(1, workers)
    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
        futures = [executor.submit(transcribe_one, call) for call in candidates]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Re-transcribing downloaded calls"):
            result = future.result()
            stats[result] += 1
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Vumber mp3 emails, transcribe, and rank leads")
    default_transcription_provider = os.environ.get(
        "TRANSCRIPTION_PROVIDER", "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "openai",
    )
    default_transcription_model = os.environ.get(
        "TRANSCRIPTION_MODEL",
        DEFAULT_OPENROUTER_TRANSCRIPTION_MODEL if default_transcription_provider == "openrouter" else "gpt-4o-mini-transcribe",
    )
    default_analysis_provider = os.environ.get(
        "ANALYSIS_PROVIDER", "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "openai",
    )
    default_analysis_model = os.environ.get(
        "ANALYSIS_MODEL", "openai/gpt-4o-mini" if default_analysis_provider == "openrouter" else "gpt-4o-mini",
    )
    parser.add_argument("--credentials", required=False, type=Path, help="Path to Gmail OAuth client credentials JSON")
    parser.add_argument("--token", default=Path("token.json"), type=Path, help="Path to store OAuth tokens")
    parser.add_argument("--download-dir", default=Path("data"), type=Path, help="Directory to store downloaded audio")
    parser.add_argument("--transcript-dir", default=Path("transcripts"), type=Path, help="Directory to store transcripts")
    parser.add_argument("--exports-dir", default=Path("exports"), type=Path, help="Directory for lead exports")
    parser.add_argument("--database", default=Path("data/leads.db"), type=Path, help="SQLite CRM database")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    parser.add_argument("--max-messages", type=int, default=None, help="Optional limit on number of messages to process")
    parser.add_argument("--transcription-provider", choices=["openai", "openrouter", "local"], default=default_transcription_provider, help="Choose OpenAI, OpenRouter, or local faster-whisper for transcription")
    parser.add_argument("--transcription-model", default=default_transcription_model, help="Model for audio transcription")
    parser.add_argument("--analysis-provider", choices=["openai", "openrouter"], default=default_analysis_provider, help="Model provider for lead extraction")
    parser.add_argument("--analysis-model", default=default_analysis_model, help="Model name for lead extraction (per provider)")
    parser.add_argument("--local-model-size", default="base", help="faster-whisper model size when using local transcription (tiny, base, small, medium, large-v3)")
    parser.add_argument("--local-device", default="cpu", help="Device for faster-whisper (cpu, cuda, auto)")
    parser.add_argument("--local-compute-type", default="int8", help="Compute type for faster-whisper (int8, int8_float16, float16, float32)")
    parser.add_argument("--local-beam-size", type=int, default=5, help="Beam size for local transcription decoding")
    parser.add_argument("--offline-mode", action="store_true", help="Skip Gmail fetching; analyze existing transcripts only")
    parser.add_argument("--force", action="store_true", help="Re-read Gmail messages already marked complete")
    parser.add_argument("--import-legacy", action="store_true", help="Import the existing exports/leads.json before syncing")
    parser.add_argument("--reanalyze-all", action="store_true", help="Refresh AI summaries for every stored CRM lead")
    parser.add_argument("--analysis-workers", type=int, default=4, help="Parallel requests used by --reanalyze-all")
    parser.add_argument("--rebuild-identities", action="store_true", help="Regroup stored calls by lead phone from Vumber subjects")
    parser.add_argument("--retranscribe-downloaded", action="store_true", help="Re-transcribe existing downloaded recordings and update CRM calls")
    parser.add_argument("--retranscribe-limit", type=int, default=None, help="Optional number of downloaded recordings to upgrade")
    parser.add_argument("--retranscribe-workers", type=int, default=4, help="Concurrent API requests for downloaded recordings")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    if args.transcription_provider == "openrouter" and args.transcription_model == "gpt-4o-mini-transcribe":
        args.transcription_model = os.environ.get(
            "OPENROUTER_TRANSCRIPTION_MODEL", DEFAULT_OPENROUTER_TRANSCRIPTION_MODEL,
        )
    if args.retranscribe_downloaded:
        openrouter_key = os.environ.get("OPENROUTER_API_KEY") if args.transcription_provider == "openrouter" else None
        if args.transcription_provider == "openrouter" and not openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter transcription")
        stats = retranscribe_downloaded_audio(
            database_path=args.database,
            provider=args.transcription_provider,
            model=args.transcription_model,
            openrouter_key=openrouter_key,
            limit=args.retranscribe_limit,
            local_model_size=args.local_model_size,
            local_device=args.local_device,
            local_compute_type=args.local_compute_type,
            local_beam_size=args.local_beam_size,
            workers=args.retranscribe_workers,
        )
        logging.info("Re-transcription result: %s", stats)
        return

    if args.credentials is None and not args.offline_mode:
        candidates = sorted(Path.cwd().glob("client_secret*.json"))
        if len(candidates) == 1:
            args.credentials = candidates[0]
            logging.info("Using detected Gmail credentials: %s", args.credentials)

    process_messages(
        credentials_path=args.credentials,
        token_path=args.token,
        download_dir=args.download_dir,
        transcript_dir=args.transcript_dir,
        exports_dir=args.exports_dir,
        gmail_query=args.query,
        max_messages=args.max_messages,
        transcription_provider=args.transcription_provider,
        transcription_model=args.transcription_model,
        analysis_provider=args.analysis_provider,
        analysis_model=args.analysis_model,
        local_model_size=args.local_model_size,
        local_device=args.local_device,
        local_compute_type=args.local_compute_type,
        local_beam_size=args.local_beam_size,
        offline_mode=args.offline_mode,
        database_path=args.database,
        force=args.force,
        import_legacy=args.import_legacy,
        reanalyze_all=args.reanalyze_all,
        analysis_workers=args.analysis_workers,
        rebuild_identities=args.rebuild_identities,
    )


def load_events_from_transcripts(transcript_dir: Path, audio_dir: Path) -> List[CallEvent]:
    events = []
    for transcript_file in transcript_dir.glob("*.txt"):
        # Parse filename: e.g., "1997c4ed17e4b342_133041584-1381686-8002247305.txt"
        stem = transcript_file.stem
        parts = stem.split("_")
        if len(parts) < 2:
            logging.warning("Skipping malformed transcript filename: %s", transcript_file.name)
            continue
        message_id = parts[0]
        filename_rest = parts[1]
        # Parse timestamp from filename_rest, e.g., "133041584-1381686-8002247305"
        time_parts = filename_rest.split("-")
        if len(time_parts) >= 3:
            timestamp_str = time_parts[0]
            vumber = time_parts[2]
            # Infer time_description from timestamp (assume Unix ms or similar)
            try:
                timestamp_ms = int(timestamp_str)
                timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                time_description = timestamp.strftime("%m/%d/%Y %I:%M:%S %p")
            except ValueError:
                time_description = ""
                timestamp = None
        else:
            time_description = ""
            timestamp = None

        # Infer phone from vumber or content
        transcript_text = transcript_file.read_text(encoding="utf-8")
        phone_match = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", transcript_text)
        phone = phone_match.group(0) if phone_match else "unknown"
        normalized_phone = normalize_phone(phone)
        display_phone = format_display_phone(phone)

        # Infer direction from content (e.g., "incoming call", "outgoing")
        lowered = transcript_text.lower()
        direction = "incoming"
        if "outgoing" in lowered or "you made a call" in lowered:
            direction = "outgoing"
        elif "missed" in lowered:
            direction = "missed"

        # Corresponding audio file
        audio_file = audio_dir / f"{stem}.mp3"
        attachment_path = str(audio_file) if audio_file.exists() else ""

        # Reference from filename or content
        call_reference = stem.split("_")[-1] if "_" in stem else stem

        # Metadata
        metadata = CallMetadata(
            direction=direction,
            other_party_number=phone,
            other_party_label="From" if direction == "incoming" else "To",
            vumber_number=normalize_phone(vumber),
            vumber_label=format_display_phone(vumber),
            time_description=time_description,
            timestamp=timestamp,
            internal_epoch_ms=None,
            reference_hint=call_reference,
        )

        events.append(
            CallEvent(
                phone_number=display_phone,
                phone_number_normalized=normalized_phone,
                direction=direction,
                vumber_number=normalize_phone(vumber),
                vumber_label=format_display_phone(vumber),
                time_description=time_description,
                timestamp_iso=time_description if time_description else None,
                internal_epoch_ms=None,
                transcript=transcript_text,
                transcript_path=str(transcript_file),
                attachment_path=attachment_path,
                call_reference=call_reference,
                message_id=message_id,
                thread_id=message_id,  # Approximate
                metadata=metadata,
            )
        )
        logging.info("Loaded offline event from %s", transcript_file.name)

    logging.info("Loaded %d events from transcripts in offline mode", len(events))
    return events


if __name__ == "__main__":
    main()
