#!/usr/bin/env python3
"""
Fetch active listings from CREA DDF and write to listings.json.
"""
import os, json, sys, re, requests
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET

DDF_USERNAME = os.environ.get("DDF_USERNAME", "")
DDF_PASSWORD = os.environ.get("DDF_PASSWORD", "")
CREA_MEMBER_ID = os.environ.get("CREA_MEMBER_ID", "3700")
REALTOR_AGENT_ID = "1571599"
DDF_LOGIN_URL = "https://data.crea.ca/Login.svc/Login"
DDF_SEARCH_URL = "https://data.crea.ca/Search.svc/Search"
AUTH = None

def login():
    global AUTH
    AUTH = HTTPDigestAuth(DDF_USERNAME, DDF_PASSWORD)
    print("Logging in to DDF...")
    r = requests.get(DDF_LOGIN_URL, auth=AUTH, timeout=30)
    print(f"Login: {r.status_code}")
    return r.status_code == 200

def search():
    queries = [
        f"(AgentDetails=|*{CREA_MEMBER_ID}*)",
        f"(AgentDetails=|*{REALTOR_AGENT_ID}*)",
        f"(ListAgentID={CREA_MEMBER_ID})",
        f"(ListAgentID={REALTOR_AGENT_ID})",
        "(ID=*)",
    ]
    for q in queries:
        for fmt in ["STANDARD-XML", "COMPACT"]:
            params = {"SearchType":"Property","Class":"Property","QueryType":"DMQL2","Format":fmt,"Limit":"20","Count":"1","Query":q}
            print(f"\nQuery: {q} | Format: {fmt}")
            try:
                r = requests.get(DDF_SEARCH_URL, params=params, auth=AUTH, timeout=60)
                print(f"Status: {r.status_code} | Size: {len(r.text)} bytes")
                if r.status_code != 200:
                    print(f"Error: {r.text[:300]}")
                    continue
                print(f"Response preview:\n{r.text[:2000]}\n{'...' if len(r.text)>2000 else ''}")
                listings = parse(r.text)
                if listings:
                    return listings
            except Exception as e:
                print(f"Error: {e}")
    return []

def parse(text):
    listings = []
    try:
        root = ET.fromstring(text)
        print(f"Root: {root.tag} | Attribs: {root.attrib}")
        
        # Check RETS reply code
        rc = root.attrib.get('ReplyCode','')
        if rc and rc != '0':
            print(f"RETS error {rc}: {root.attrib.get('ReplyText','')}")
            return []
        
        # Log structure
        tags = set()
        for e in root.iter():
            tags.add(e.tag.split('}')[-1] if '}' in e.tag else e.tag)
        print(f"Tags found: {sorted(tags)[:40]}")
        
        # Try COMPACT first (COLUMNS/DATA)
        cols_el = None
        for e in root.iter():
            t = e.tag.split('}')[-1] if '}' in e.tag else e.tag
            if t.upper() == 'COLUMNS':
                cols_el = e
                break
        
        if cols_el is not None and cols_el.text:
            delim = root.attrib.get('DELIMITER','\t')
            if delim.isdigit(): delim = chr(int(delim))
            cols = [c.strip() for c in cols_el.text.strip(delim).split(delim)]
            print(f"COMPACT columns ({len(cols)}): {cols[:20]}")
            
            data_els = []
            for e in root.iter():
                t = e.tag.split('}')[-1] if '}' in e.tag else e.tag
                if t.upper() == 'DATA' and e.text:
                    data_els.append(e)
            
            print(f"COMPACT data rows: {len(data_els)}")
            for de in data_els:
                vals = de.text.strip(delim).split(delim)
                row = dict(zip(cols, vals))
                if not listings:
                    print(f"Sample row: { {k:v[:60] for k,v in list(row.items())[:15]} }")
                l = row_to_listing(row)
                if l: listings.append(l)
            return listings
        
        # Try XML property elements
        for tag in ['PropertyDetails','Listing','Property','Record']:
            found = [e for e in root.iter() if (e.tag.split('}')[-1] if '}' in e.tag else e.tag) == tag]
            if found:
                print(f"Found {len(found)} <{tag}> elements")
                for prop in found:
                    l = xml_to_listing(prop)
                    if l: listings.append(l)
                return listings
        
        # Nothing recognized - dump structure
        print("Unrecognized format. Structure:")
        for child in list(root)[:5]:
            ct = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            print(f"  {ct}: {(child.text or '')[:100]}")
            for sub in list(child)[:5]:
                st = sub.tag.split('}')[-1] if '}' in sub.tag else sub.tag
                print(f"    {st}: {(sub.text or '')[:100]}")
        
    except ET.ParseError as e:
        print(f"XML parse error: {e}")
        print(f"Raw: {text[:500]}")
    return listings

def row_to_listing(row):
    street = row.get('StreetAddress',row.get('Address',row.get('UnparsedAddress','')))
    city = row.get('City',row.get('CityName',''))
    addr = f"{street}, {city}" if street and city else street or city
    if not addr: return None
    price_raw = row.get('Price',row.get('ListPrice',row.get('CurrentPrice','')))
    try: price = f"${float(re.sub(r'[^0-9.]','',price_raw)):,.0f}"
    except: price = price_raw or "Price on Request"
    mls = row.get('ListingID',row.get('MLSNumber',row.get('ListingKey','')))
    photo = row.get('PhotoURL',row.get('Photo',''))
    return {
        "image":photo,"images":[photo] if photo else [],"tag":"Featured","address":addr,"price":price,
        "beds":int(row['BedroomsTotal']) if row.get('BedroomsTotal','').strip().isdigit() else None,
        "baths":int(row['BathroomTotal']) if row.get('BathroomTotal','').strip().isdigit() else None,
        "sqft":row.get('SizeInterior',row.get('LivingArea',None)),
        "description":row.get('PublicRemarks',row.get('Description',''))[:300],
        "features":[],"url":f"https://www.realtor.ca/real-estate/{mls}" if mls else "#"
    }

def xml_to_listing(prop):
    def ft(*paths):
        for p in paths:
            for e in prop.iter():
                t = e.tag.split('}')[-1] if '}' in e.tag else e.tag
                if t == p and e.text: return e.text.strip()
        return ""
    street = ft('StreetAddress','Address','UnparsedAddress')
    city = ft('City','CityName')
    addr = f"{street}, {city}" if street and city else street or city
    if not addr: return None
    price_raw = ft('Price','ListPrice','CurrentPrice')
    try: price = f"${float(re.sub(r'[^0-9.]','',price_raw)):,.0f}"
    except: price = price_raw or "Price on Request"
    mls = ft('ListingID','MLSNumber','ListingKey')
    imgs = []
    for e in prop.iter():
        if 'photo' in e.tag.lower() or 'image' in e.tag.lower():
            if e.text and e.text.startswith('http'): imgs.append(e.text)
    return {
        "image":imgs[0] if imgs else "","images":imgs[:10],"tag":"Featured","address":addr,"price":price,
        "beds":int(ft('BedroomsTotal','Bedrooms')) if ft('BedroomsTotal','Bedrooms').isdigit() else None,
        "baths":int(ft('BathroomTotal','Bathrooms')) if ft('BathroomTotal','Bathrooms').isdigit() else None,
        "sqft":ft('SizeInterior','LivingArea') or None,
        "description":(ft('PublicRemarks','Description') or "")[:300],
        "features":[],"url":f"https://www.realtor.ca/real-estate/{mls}" if mls else "#"
    }

def main():
    if not DDF_USERNAME or not DDF_PASSWORD:
        print("ERROR: Missing credentials"); sys.exit(1)
    print("="*50)
    print(f"DDF Updater | CREA: {CREA_MEMBER_ID} | Realtor.ca: {REALTOR_AGENT_ID}")
    print("="*50)
    if not login():
        print("Auth failed."); sys.exit(0)
    listings = search()
    if listings:
        cleaned = [{k:v for k,v in l.items() if v is not None} for l in listings]
        with open("listings.json","w") as f: json.dump(cleaned, f, indent=2)
        print(f"\nSuccess: {len(listings)} listings written")
    else:
        print("\nNo listings found. Check logs above. Keeping existing listings.json.")

if __name__ == "__main__":
    main()
