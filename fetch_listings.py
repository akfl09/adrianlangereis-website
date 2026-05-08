#!/usr/bin/env python3
"""
Fetch Adrian Langereis's active listings from CREA DDF and write listings.json.

CREA DDF specifics (learned from the live feed):
  * The search resource only exposes 3 searchable fields (ID, LastUpdated,
    DestinationID). None of them filter to a specific agent, so we have to
    scan the (CREA-narrowed) feed and match client-side on ListAgentKey.
  * (DestinationID=N) is a *mode switch*, not a filter — it tells the server
    to return full property records (~230 columns) instead of an ID-only
    manifest. The N value is ignored.
  * Per-request Limit cap is ~100; pagination is via Offset (1-indexed).
  * Login sets cookies (X-SESSIONID, ASP.NET_SessionId, ARRAffinity*) that
    must be reused on every Search/GetMetadata/GetObject call, otherwise the
    server replies 20701 "Not Logged In" even with valid digest auth.
  * COMPACT rows are tab-delimited with a *leading and trailing* tab; trim
    only those single outer tabs, never .strip("\t"), or trailing empty
    fields silently disappear and shift every column.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Optional

import requests
from requests.auth import HTTPDigestAuth

DDF_USERNAME = os.environ.get("DDF_USERNAME", "")
DDF_PASSWORD = os.environ.get("DDF_PASSWORD", "")
AGENT_KEY    = os.environ.get("DDF_AGENT_KEY", "1571599")

LOGIN_URL = "https://data.crea.ca/Login.svc/Login"

HTTP_HEADERS = {
    "User-Agent":   "AdrianLangereisWebsite/1.0",
    "RETS-Version": "RETS/1.7.2",
    "Accept":       "*/*",
}

PAGE_SIZE = 100


def login(session: requests.Session) -> dict[str, str]:
    """Authenticate and return the capability URL map from the RETS response."""
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    rc = re.search(r'ReplyCode="(\d+)"', r.text)
    if not rc or rc.group(1) != "0":
        raise RuntimeError(f"Login RETS error: {r.text[:300]}")
    caps: dict[str, str] = {}
    for line in r.text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("<"):
            k, _, v = line.partition("=")
            caps[k.strip()] = v.strip()
    for needed in ("Search", "GetMetadata", "GetObject"):
        if needed not in caps:
            raise RuntimeError(f"Login response missing capability URL: {needed}")
    return caps


def parse_compact(xml_text: str) -> tuple[list[str], list[list[str]]]:
    """Parse a RETS COMPACT response, preserving trailing empty fields."""
    cols_m = re.search(r"<COLUMNS>(.*?)</COLUMNS>", xml_text, re.DOTALL)
    if not cols_m:
        return [], []
    raw = cols_m.group(1)
    if raw.startswith("\t"):
        raw = raw[1:]
    if raw.endswith("\t"):
        raw = raw[:-1]
    cols = raw.split("\t")
    rows: list[list[str]] = []
    for m in re.finditer(r"<DATA>(.*?)</DATA>", xml_text, re.DOTALL):
        d = m.group(1)
        if d.startswith("\t"):
            d = d[1:]
        if d.endswith("\t"):
            d = d[:-1]
        row = d.split("\t")
        while len(row) < len(cols):
            row.append("")
        rows.append(row)
    return cols, rows


def rets_status(xml_text: str) -> tuple[str, str]:
    rc = re.search(r'ReplyCode="(\d+)"', xml_text)
    rt = re.search(r'ReplyText="([^"]*)"', xml_text)
    return (rc.group(1) if rc else "?", rt.group(1) if rt else "")


def get_total_records(session: requests.Session, search_url: str) -> int:
    """Return the total number of records visible to this account."""
    r = session.get(search_url, params={
        "SearchType": "Property", "Class": "Property", "QueryType": "DMQL2",
        "Query": "(DestinationID=1)", "Format": "COMPACT",
        "Limit": "1", "Count": "1",
    }, timeout=60)
    r.raise_for_status()
    code, text = rets_status(r.text)
    if code == "20201":
        return 0
    if code != "0":
        raise RuntimeError(f"Total-count query failed: {code} {text}")
    m = re.search(r'Records="(\d+)"', r.text)
    return int(m.group(1)) if m else 0


def scan_for_matches(session: requests.Session, search_url: str, total: int) -> list[dict]:
    """Page through the feed and return rows where the agent/office matches."""
    matches: list[dict] = []
    last_log = time.time()
    started = time.time()
    for offset in range(1, total + 1, PAGE_SIZE):
        r = session.get(search_url, params={
            "SearchType": "Property", "Class": "Property", "QueryType": "DMQL2",
            "Query": "(DestinationID=1)", "Format": "COMPACT",
            "Limit": str(PAGE_SIZE), "Offset": str(offset), "Count": "1",
        }, timeout=120)
        r.raise_for_status()
        code, text = rets_status(r.text)
        if code == "20201":
            break
        if code != "0":
            print(f"  RETS error at offset={offset}: {code} {text}", file=sys.stderr)
            break
        cols, rows = parse_compact(r.text)
        if not rows:
            break
        try:
            i_agent = cols.index("ListAgentKey")
        except ValueError as e:
            raise RuntimeError(f"Expected column missing from feed: {e}")
        for row in rows:
            if row[i_agent] == AGENT_KEY:
                matches.append(dict(zip(cols, row)))
        if time.time() - last_log > 10:
            elapsed = time.time() - started
            print(f"  scanned {offset:>6}/{total} ({100.0*offset/total:5.1f}%) "
                  f"matches={len(matches)} elapsed={elapsed:.0f}s")
            last_log = time.time()
        if len(rows) < PAGE_SIZE:
            break
    return matches


def fetch_photos(session: requests.Session, object_url: str, listing_id: str) -> list[str]:
    """Return ordered list of high-res photo URLs for a listing."""
    try:
        r = session.get(object_url, params={
            "Resource": "Property", "Type": "LargePhoto",
            "ID": f"{listing_id}:*", "Location": "1",
        }, timeout=60)
    except requests.RequestException as e:
        print(f"  photo fetch failed for {listing_id}: {e}", file=sys.stderr)
        return []
    if r.status_code != 200:
        return []
    code, _ = rets_status(r.text)
    if code != "0":
        return []
    cols, rows = parse_compact(r.text)
    if not rows:
        return []
    try:
        i_url = cols.index("MediaUrl")
        i_ord = cols.index("Order")
    except ValueError:
        return []
    ordered = sorted(rows, key=lambda row: int(row[i_ord]) if row[i_ord].isdigit() else 999)
    return [row[i_url] for row in ordered if row[i_url].startswith("http")]


def format_price(raw: str) -> str:
    if not raw:
        return "Price on Request"
    try:
        n = float(re.sub(r"[^0-9.]", "", raw))
        if n <= 0:
            return "Price on Request"
        return f"${n:,.0f}"
    except ValueError:
        return raw


def to_int(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def build_listing(row: dict, photos: list[str]) -> dict:
    street = (row.get("StreetAddress") or row.get("UnparsedAddress") or "").strip()
    city   = (row.get("City") or "").strip()
    if street and city:
        address = f"{street}, {city}"
    else:
        address = street or city or "Address available on request"

    sqft_val   = (row.get("BuildingAreaTotal") or "").strip()
    sqft_units = (row.get("BuildingAreaUnits") or "").strip()
    sqft = f"{sqft_val} {sqft_units}".strip() if sqft_val else None

    listing_key = row.get("ListingKey") or row.get("ListingId") or ""
    url = f"https://www.realtor.ca/real-estate/{listing_key}" if listing_key else "#"

    return {
        "image":       photos[0] if photos else "",
        "images":      photos[:15],
        "tag":         "Featured",
        "address":     address,
        "price":       format_price(row.get("ListPrice", "")),
        "beds":        to_int(row.get("BedroomsTotal", "")),
        "baths":       to_int(row.get("BathroomsTotal", "")),
        "sqft":        sqft,
        "description": (row.get("PublicRemarks") or "").strip()[:600],
        "features":    [],
        "url":         url,
    }


def main() -> int:
    if not DDF_USERNAME or not DDF_PASSWORD:
        print("ERROR: DDF_USERNAME and DDF_PASSWORD must be set", file=sys.stderr)
        return 1

    session = requests.Session()
    session.auth = HTTPDigestAuth(DDF_USERNAME, DDF_PASSWORD)
    session.headers.update(HTTP_HEADERS)
    # CREA serves UTF-8 but its Content-Type header doesn't say so, which
    # makes requests fall back to Latin-1 and turns smart quotes / accents
    # into mojibake. Force UTF-8 on every response.
    session.hooks["response"] = [lambda r, *a, **k: setattr(r, "encoding", "utf-8") or r]

    print(f"Login as {DDF_USERNAME[:4]}...")
    caps = login(session)
    print(f"  {caps.get('MemberName', '?')} (Broker={caps.get('Broker', '?')})")

    total = get_total_records(session, caps["Search"])
    print(f"Feed size: {total} records")
    if total == 0:
        # Empty feed — could be a CREA-side reprovision. Don't blow away
        # listings.json: leave it as-is so the website keeps showing whatever
        # was there yesterday until the feed is back.
        print("Feed currently empty. Leaving listings.json untouched.")
        return 0

    print(f"Scanning for ListAgentKey={AGENT_KEY}...")
    matches = scan_for_matches(session, caps["Search"], total)
    print(f"Matched {len(matches)} listing(s).")

    if not matches:
        print("No matches in feed today. Leaving listings.json untouched.")
        return 0

    listings: list[dict] = []
    for row in matches:
        lid = row.get("ListingKey") or row.get("ListingId") or ""
        photos = fetch_photos(session, caps["GetObject"], lid) if lid else []
        listings.append(build_listing(row, photos))

    # Strip None values so the JSON stays compact + matches the website schema.
    cleaned = [{k: v for k, v in listing.items() if v is not None} for listing in listings]
    with open("listings.json", "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote listings.json with {len(cleaned)} listing(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
