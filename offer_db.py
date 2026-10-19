"""Fast, read-only reference lookups for the FOP offers database."""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


REFERENCE_COLUMN_NAMES = {
    "reference", "reference_1", "reference_2", "reference_1_2",
    "reference_2_2", "ref_num", "ref", "refrence",
}


def normalize_reference(value: Any) -> str:
    text = str(value or "").upper().strip()
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("_", "-")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^A-Z0-9-]", "", text)
    return re.sub(r"-+", "-", text).strip("-")


def parse_references(value: Any) -> list[str]:
    result: list[str] = []
    for part in re.split(r"[,;\n]+", str(value or "")):
        reference = normalize_reference(part)
        if reference and reference not in result:
            result.append(reference)
    return result


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return value
    return ""


def _money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"${float(str(value).replace(',', '').replace('$', '')):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def present_offer(row: dict[str, Any], matched_reference: str) -> dict[str, str]:
    owner = _first(
        row, "Owner_Mailing_Name", "Owner_Mailing_Name_2", "Owner_Mailing_Name_1",
        "OWNER_1_FULL_NAME", "Owner_Full_Name", "Owner", "OWNERNME1",
        "parcel_Owner_Name_s_", "Owner_Name_s_",
    )
    if not owner:
        owner = " ".join(filter(None, [
            str(_first(row, "Owner_First_Name", "OWNER_1_FIRST_NAME") or "").strip(),
            str(_first(row, "Owner_Last_Name", "OWNER_1_LAST_NAME") or "").strip(),
        ]))

    property_address = _first(
        row, "SITUS_FULL_ADDRESS", "Property_Address_Full", "Full_Property_Address",
        "Parcel_Full_Address", "Subject_Property_Address", "Property_Address",
        "Parcel_Address", "parcel_Parcel_Address",
    )
    mailing_address = _first(row, "Mail_Address_Full", "parcel_Mail_Full_Address")
    if not mailing_address:
        mailing_address = ", ".join(filter(None, [
            str(_first(row, "Mail_Address", "Mail_Address_1", "mail_address_2") or "").strip(),
            str(_first(row, "Mail_City", "Mail_City_1", "Mail_City_2", "mail_city_4") or "").strip(),
            " ".join(filter(None, [
                str(_first(row, "Mail_State", "Mail_State_1", "Mail_State_2", "mail_state_4") or "").strip(),
                str(_first(row, "Mail_Zip_Code", "Mail_ZIP", "Mail_Zip_Code_2", "mail_zip_code_3") or "").strip(),
            ])),
        ]))

    result = {
        "reference": matched_reference,
        "owner": str(owner or ""),
        "offer_price": _money(_first(row, "Offer_Price", "Offer_Price_1", "Offer_Price_2", "Offer_Price_1_2")),
        "acreage": str(_first(row, "Lot_Acreage", "Lot_Acreage_1", "LOT_ACREAGE_2", "Lot_Acreage_3", "acreage", "Lot_Acres", "parcel_Acreage") or ""),
        "apn": str(_first(row, "APN", "APN___FORMATTED", "APN_1", "APN_v", "PARCELID", "parcel_APN") or ""),
        "county": str(_first(row, "County_Name", "Property_County_Name", "parcel_County") or ""),
        "state": str(_first(row, "State", "Property_State_Name", "SITUS_STATE", "parcel_State") or ""),
        "property_address": str(property_address or ""),
        "mailing_address": str(mailing_address or ""),
        "legal_description": str(_first(row, "Legal_Description", "Legal_Description_1", "Legal_Description_2", "LEGAL_DESCRIPTION_3", "parcel_Legal_Description") or ""),
        "offer_date": str(_first(row, "Date") or ""),
        "closing_date": str(_first(row, "Closing_Date", "Closing_Date_2") or ""),
        "parcel_link": str(_first(row, "Parcel_Link", "parcel_Parcel_Link", "Candidate___current_parcel_link_URLs") or ""),
        "map_link": str(_first(row, "Google_Map", "parcel_Google_Map", "Candidate___current_li_google_map_URLs") or ""),
    }
    for key in ("parcel_link", "map_link"):
        if result[key] and not result[key].lower().startswith(("http://", "https://")):
            result[key] = ""
    return result


class OfferDatabase:
    """Indexes reference fields locally while leaving the source DB untouched."""

    def __init__(self, source_path: Path | str, index_path: Path | str) -> None:
        self.source_path = Path(source_path)
        self.index_path = Path(index_path)
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.source_path.is_file()

    def _connect_source(self) -> sqlite3.Connection:
        uri = f"file:{self.source_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _signature(self) -> str:
        stat = self.source_path.stat()
        return f"{self.source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"

    def _reference_columns(self, source: sqlite3.Connection) -> list[str]:
        return [
            row[1] for row in source.execute("PRAGMA table_info(data)")
            if row[1].lower() in REFERENCE_COLUMN_NAMES
        ]

    def _owner_columns(self, source: sqlite3.Connection) -> list[str]:
        owner_names = {
            "owner_mailing_name", "owner_mailing_name_2", "owner_mailing_name_1",
            "owner_1_full_name", "owner_full_name", "owner", "ownernme1",
            "owner_2_full_name", "owner_name_s_", "parcel_owner_name_s_",
        }
        return [
            row[1] for row in source.execute("PRAGMA table_info(data)")
            if row[1].lower() in owner_names
        ]

    def ensure_index(self) -> None:
        if not self.available:
            return
        with self._lock:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            index = sqlite3.connect(self.index_path, timeout=30)
            try:
                index.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS offer_refs (
                        reference TEXT NOT NULL, offer_rowid INTEGER NOT NULL,
                        PRIMARY KEY(reference, offer_rowid)
                    );
                    CREATE INDEX IF NOT EXISTS idx_offer_refs_reference ON offer_refs(reference);
                    CREATE TABLE IF NOT EXISTS offer_ref_digits (
                        digits TEXT NOT NULL, offer_rowid INTEGER NOT NULL,
                        PRIMARY KEY(digits, offer_rowid)
                    );
                    CREATE INDEX IF NOT EXISTS idx_offer_ref_digits ON offer_ref_digits(digits);
                    CREATE TABLE IF NOT EXISTS offer_owner_tokens (
                        token TEXT NOT NULL, offer_rowid INTEGER NOT NULL,
                        PRIMARY KEY(token, offer_rowid)
                    );
                    CREATE INDEX IF NOT EXISTS idx_offer_owner_tokens ON offer_owner_tokens(token);
                """)
                signature = self._signature()
                existing = index.execute(
                    "SELECT value FROM metadata WHERE key='source_signature'"
                ).fetchone()
                version = index.execute(
                    "SELECT value FROM metadata WHERE key='index_version'"
                ).fetchone()
                if existing and existing[0] == signature and version and version[0] == "2":
                    return

                with closing(self._connect_source()) as source:
                    columns = self._reference_columns(source)
                    if not columns:
                        raise RuntimeError("No reference columns were found in the offers database")
                    owner_columns = self._owner_columns(source)
                    quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
                    owner_quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in owner_columns)
                    pending: list[tuple[str, int]] = []
                    digit_pending: list[tuple[str, int]] = []
                    owner_pending: list[tuple[str, int]] = []
                    index.execute("DELETE FROM offer_refs")
                    index.execute("DELETE FROM offer_ref_digits")
                    index.execute("DELETE FROM offer_owner_tokens")
                    if owner_columns:
                        cursor = source.execute(f"SELECT rowid, {quoted}, {owner_quoted} FROM data")
                    else:
                        cursor = source.execute(f"SELECT rowid, {quoted} FROM data")
                    for row in cursor:
                        rowid = int(row["rowid"])
                        for reference in {normalize_reference(row[name]) for name in columns}:
                            if not reference:
                                continue
                            pending.append((reference, rowid))
                            digits = re.sub(r"[^0-9]", "", reference)
                            if 4 <= len(digits) <= 9:
                                digit_pending.append((digits, rowid))
                        if owner_columns:
                            owner_text = " ".join(
                                str(row[name] or "") for name in owner_columns
                            ).upper()
                            tokens = {token for token in re.findall(r"[A-Z]{2,}", owner_text)}
                            for token in tokens:
                                owner_pending.append((token, rowid))
                        if len(pending) >= 10_000:
                            index.executemany("INSERT OR IGNORE INTO offer_refs VALUES (?, ?)", pending)
                            index.executemany("INSERT OR IGNORE INTO offer_ref_digits VALUES (?, ?)", digit_pending)
                            index.executemany("INSERT OR IGNORE INTO offer_owner_tokens VALUES (?, ?)", owner_pending)
                            pending.clear()
                            digit_pending.clear()
                            owner_pending.clear()
                    if pending:
                        index.executemany("INSERT OR IGNORE INTO offer_refs VALUES (?, ?)", pending)
                        index.executemany("INSERT OR IGNORE INTO offer_ref_digits VALUES (?, ?)", digit_pending)
                        index.executemany("INSERT OR IGNORE INTO offer_owner_tokens VALUES (?, ?)", owner_pending)
                index.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('source_signature', ?)",
                    (signature,),
                )
                index.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('index_version', '2')",
                )
                index.commit()
            finally:
                index.close()

    def lookup(self, references: Iterable[str] | str) -> list[dict[str, str]]:
        if not self.available:
            return []
        parsed = parse_references(references) if isinstance(references, str) else [
            normalize_reference(value) for value in references if normalize_reference(value)
        ]
        if not parsed:
            return []
        self.ensure_index()
        placeholders = ",".join("?" for _ in parsed)
        index = sqlite3.connect(self.index_path, timeout=30)
        try:
            matches = index.execute(
                f"SELECT reference, offer_rowid FROM offer_refs WHERE reference IN ({placeholders}) ORDER BY reference, offer_rowid",
                parsed,
            ).fetchall()
        finally:
            index.close()
        if not matches:
            return []

        results: list[dict[str, str]] = []
        with closing(self._connect_source()) as source:
            for reference, rowid in matches:
                row = source.execute("SELECT * FROM data WHERE rowid=?", (rowid,)).fetchone()
                if row:
                    results.append(present_offer(dict(row), reference))
        return results

    def _canonical_reference_for_row(self, source: sqlite3.Connection, rowid: int) -> str:
        """Return the first non-empty reference value stored on an offer row."""
        columns = self._reference_columns(source)
        if not columns:
            return ""
        quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
        row = source.execute(f"SELECT {quoted} FROM data WHERE rowid=?", (rowid,)).fetchone()
        if not row:
            return ""
        for name in columns:
            reference = normalize_reference(row[name])
            if reference:
                return reference
        return ""

    def lookup_by_digits(self, digit_groups: Iterable[str]) -> list[dict[str, str]]:
        """Fuzzy fallback: match offers by digit groups alone (e.g. 2614-1677).

        The letter prefix is often garbled by transcription (“0RMO”, “C-M-O”),
        but the digit groups are effectively unique in the offers database.
        Only unambiguous single-row matches are returned.
        """
        if not self.available:
            return []
        keys: list[str] = []
        for value in digit_groups:
            digits = re.sub(r"[^0-9]", "", str(value or ""))
            if 4 <= len(digits) <= 9 and digits not in keys:
                keys.append(digits)
        if not keys:
            return []
        self.ensure_index()
        index = sqlite3.connect(self.index_path, timeout=30)
        try:
            placeholders = ",".join("?" for _ in keys)
            matches = index.execute(
                f"""SELECT digits, offer_rowid FROM offer_ref_digits
                    WHERE digits IN ({placeholders}) ORDER BY digits, offer_rowid""",
                keys,
            ).fetchall()
        finally:
            index.close()
        by_digits: dict[str, set[int]] = {}
        for digits, rowid in matches:
            by_digits.setdefault(digits, set()).add(rowid)
        # Keep only digit keys that resolve to exactly one offer row.
        unique_rowids: dict[int, str] = {}
        for digits, rowids in by_digits.items():
            if len(rowids) == 1:
                unique_rowids[next(iter(rowids))] = digits
        if not unique_rowids:
            return []
        results: list[dict[str, str]] = []
        with closing(self._connect_source()) as source:
            for rowid, digits in unique_rowids.items():
                row = source.execute("SELECT * FROM data WHERE rowid=?", (rowid,)).fetchone()
                if not row:
                    continue
                reference = self._canonical_reference_for_row(source, rowid)
                offer = present_offer(dict(row), reference or digits)
                offer["match_reason"] = f"digits {digits}"
                results.append(offer)
        return results

    def lookup_by_owner(self, owner_name: str) -> list[dict[str, str]]:
        """Fallback: find offers whose owner tokens match the lead owner name.

        Requires at least two distinct name tokens (e.g. first + last name) to
        hit the same offer row, and only returns unambiguous single-row matches.
        """
        if not self.available:
            return []
        tokens = {token for token in re.findall(r"[A-Z]{2,}", str(owner_name or "").upper())}
        if len(tokens) < 2:
            return []
        # Never match our own company name to an offer; owner rows for our
        # outbound calls (“Fuller Oak Properties”) are self-calls, not leads.
        if tokens & {"FULLER", "OAK", "PROPERTIES", "INVESTMENTS"}:
            return []
        self.ensure_index()
        index = sqlite3.connect(self.index_path, timeout=30)
        try:
            placeholders = ",".join("?" for _ in tokens)
            matches = index.execute(
                f"""SELECT offer_rowid, COUNT(DISTINCT token) AS hits FROM offer_owner_tokens
                    WHERE token IN ({placeholders})
                    GROUP BY offer_rowid HAVING hits >= 2 ORDER BY hits DESC, offer_rowid""",
                tuple(tokens),
            ).fetchall()
        finally:
            index.close()
        if len(matches) != 1:
            return []
        rowid = matches[0][0]
        with closing(self._connect_source()) as source:
            row = source.execute("SELECT * FROM data WHERE rowid=?", (rowid,)).fetchone()
            if not row:
                return []
            reference = self._canonical_reference_for_row(source, rowid)
            offer = present_offer(dict(row), reference)
            offer["match_reason"] = "owner name"
            return [offer] if reference else []
