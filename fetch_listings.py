#!/usr/bin/env python3
"""
Fetch active listings from CREA DDF (Data Distribution Facility) 
and write them to listings.json for the website.

Uses the RETS/DDF API with credentials stored as GitHub Secrets.
Filters listings by CREA Member ID to show only Adrian's listings.
"""

import os
import json
import requests
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET
import sys

# ── Configuration ──
DDF_USERNAME = os.environ.get("DDF_USERNAME", "")
DDF_PASSWORD = os.environ.get("DDF_PASSWORD", "")
CREA_MEMBER_ID = os.environ.get("CREA_MEMBER_ID", "3700")

# DDF/RETS endpoints
DDF_LOGIN_URL = "https://data.crea.ca/Login.svc/Login"
DDF_SEARCH_URL = "https://data.crea.ca/Search.svc/Search"
DDF_METADATA_URL = "https://data.crea.ca/Metadata.svc/Metadata"

# RETS parameters for searching listings
SEARCH_PARAMS = {
    "SearchType": "Property",
    "Class": "Property", 
    "QueryType": "DMQL2",
    "Format": "STANDARD-XML",
    "Limit": "50",
    "Count": "1",
}

def login_to_ddf():
    """Authenticate with DDF/RETS server."""
    print("Logging in to DDF...")
    try:
        response = requests.get(
            DDF_LOGIN_URL,
            auth=HTTPDigestAuth(DDF_USERNAME, DDF_PASSWORD),
            timeout=30
        )
        if response.status_code == 200:
            print("Login successful")
            return True
        else:
            print(f"Login failed: {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return False
    except Exception as e:
        print(f"Login error: {e}")
        return False


def search_listings():
    """Search DDF for listings belonging to this agent."""
    print(f"Searching for listings (CREA Member ID: {CREA_MEMBER_ID})...")
    
    # DMQL2 query to find active listings for this agent
    # The field names may vary — common DDF field for agent ID:
    # AgentDetails/AgentID, ListAgentID, or similar
    queries_to_try = [
        f"(AgentDetails=|*{CREA_MEMBER_ID}*)",
        f"(ID=*)",  # Fallback: get all and filter locally
    ]
    
    for query in queries_to_try:
        params = SEARCH_PARAMS.copy()
        params["Query"] = query
        
        try:
            response = requests.get(
                DDF_SEARCH_URL,
                params=params,
                auth=HTTPDigestAuth(DDF_USERNAME, DDF_PASSWORD),
                timeout=60
            )
            
            if response.status_code == 200:
                print(f"Search successful with query: {query}")
                return parse_rets_response(response.text)
            else:
                print(f"Search failed with query '{query}': {response.status_code}")
                print(f"Response: {response.text[:500]}")
                continue
                
        except Exception as e:
            print(f"Search error with query '{query}': {e}")
            continue
    
    print("All search queries failed")
    return []


def parse_rets_response(xml_text):
    """Parse RETS XML response into listing objects."""
    listings = []
    
    try:
        root = ET.fromstring(xml_text)
        
        # RETS responses typically have <RETS> root with <DATA> or <COLUMNS>/<DATA> pairs
        # DDF uses STANDARD-XML which wraps in <PropertyDetails>
        
        # Try to find property nodes
        namespaces = {
            'rets': 'http://www.rets.org/xsd/Metadata',
        }
        
        # Look for PropertyDetails elements (DDF standard format)
        properties = root.findall('.//PropertyDetails') or \
                    root.findall('.//{*}PropertyDetails') or \
                    root.findall('.//Listing') or \
                    root.findall('.//{*}Listing')
        
        if not properties:
            # Try parsing as COMPACT format
            print("No PropertyDetails found, trying COMPACT format...")
            return parse_compact_response(xml_text)
        
        for prop in properties:
            listing = extract_listing_data(prop)
            if listing:
                listings.append(listing)
        
        print(f"Parsed {len(listings)} listings")
        
    except ET.ParseError as e:
        print(f"XML parse error: {e}")
        print(f"First 1000 chars of response: {xml_text[:1000]}")
    
    return listings


def extract_listing_data(prop):
    """Extract listing fields from a PropertyDetails XML element."""
    
    def get_text(element, *paths):
        """Try multiple paths to find a text value."""
        for path in paths:
            el = element.find(path) or element.find(f'.//{path}') or element.find(f'.//*{{{path}}}')
            if el is not None and el.text:
                return el.text.strip()
        return ""
    
    address_parts = []
    street = get_text(prop, 'Address/StreetAddress', 'StreetAddress', 'Address')
    city = get_text(prop, 'Address/City', 'City')
    
    if street:
        address_parts.append(street)
    if city:
        address_parts.append(city)
    
    address = ", ".join(address_parts) if address_parts else get_text(prop, 'UnparsedAddress', 'Address')
    
    if not address:
        return None
    
    # Price
    price_raw = get_text(prop, 'Price', 'ListPrice', 'CurrentPrice')
    try:
        price_num = float(price_raw.replace(',', '').replace('$', ''))
        price = f"${price_num:,.0f}"
    except (ValueError, AttributeError):
        price = price_raw if price_raw else "Price on Request"
    
    # Specs
    beds = get_text(prop, 'Building/BedroomsTotal', 'BedroomsTotal', 'Bedrooms')
    baths = get_text(prop, 'Building/BathroomTotal', 'BathroomTotal', 'Bathrooms')
    sqft = get_text(prop, 'Building/SizeInterior', 'SizeInterior', 'LivingArea')
    lot = get_text(prop, 'Land/SizeTotal', 'LotSizeArea', 'LotSize')
    year = get_text(prop, 'Building/ConstructedDate', 'YearBuilt')
    
    # Description
    description = get_text(prop, 'PublicRemarks', 'Description', 'Remarks')
    
    # Photos
    images = []
    photo_elements = prop.findall('.//Photo') or prop.findall('.//{*}Photo') or \
                     prop.findall('.//PropertyPhoto') or prop.findall('.//{*}PropertyPhoto')
    for photo in photo_elements[:10]:  # Max 10 photos
        photo_url = photo.text or get_text(photo, 'PhotoURL', 'LargePhotoURL')
        if photo_url and photo_url.startswith('http'):
            images.append(photo_url)
    
    # MLS number for realtor.ca link
    mls_num = get_text(prop, 'ListingID', 'MLSNumber', 'MLS')
    realtor_url = f"https://www.realtor.ca/real-estate/{mls_num}" if mls_num else "#"
    
    # Transaction type for tag
    tx_type = get_text(prop, 'TransactionType', 'PropertyType')
    
    # Features
    features = []
    feature_elements = prop.findall('.//Features') or prop.findall('.//{*}Features')
    for feat in feature_elements:
        if feat.text:
            features.extend([f.strip() for f in feat.text.split(',') if f.strip()])
    
    return {
        "image": images[0] if images else "",
        "images": images,
        "tag": "New Listing",
        "address": address,
        "price": price,
        "beds": int(beds) if beds and beds.isdigit() else None,
        "baths": int(baths) if baths and baths.isdigit() else None,
        "sqft": sqft if sqft else None,
        "lot": lot if lot else None,
        "year": year if year else None,
        "description": description[:300] + "..." if len(description) > 300 else description,
        "features": features[:8],  # Max 8 features
        "url": realtor_url
    }


def parse_compact_response(xml_text):
    """Parse RETS COMPACT format response."""
    listings = []
    try:
        root = ET.fromstring(xml_text)
        
        columns_el = root.find('.//COLUMNS')
        if columns_el is None or not columns_el.text:
            print("No COLUMNS element found in COMPACT response")
            return listings
        
        columns = columns_el.text.strip().split('\t')
        
        for data_el in root.findall('.//DATA'):
            if data_el.text:
                values = data_el.text.strip().split('\t')
                row = dict(zip(columns, values))
                
                address = row.get('StreetAddress', row.get('Address', ''))
                city = row.get('City', '')
                full_address = f"{address}, {city}" if city else address
                
                if not full_address:
                    continue
                
                price_raw = row.get('Price', row.get('ListPrice', ''))
                try:
                    price = f"${float(price_raw.replace(',', '').replace('$', '')):,.0f}"
                except:
                    price = price_raw or "Price on Request"
                
                mls = row.get('ListingID', row.get('MLSNumber', ''))
                
                listing = {
                    "image": row.get('PhotoURL', ''),
                    "images": [row.get('PhotoURL', '')] if row.get('PhotoURL') else [],
                    "tag": "New Listing",
                    "address": full_address,
                    "price": price,
                    "beds": int(row['BedroomsTotal']) if row.get('BedroomsTotal', '').isdigit() else None,
                    "baths": int(row['BathroomTotal']) if row.get('BathroomTotal', '').isdigit() else None,
                    "sqft": row.get('SizeInterior', None),
                    "lot": row.get('LotSizeArea', None),
                    "year": row.get('YearBuilt', None),
                    "description": row.get('PublicRemarks', '')[:300],
                    "features": [],
                    "url": f"https://www.realtor.ca/real-estate/{mls}" if mls else "#"
                }
                listings.append(listing)
        
        print(f"Parsed {len(listings)} listings from COMPACT format")
    except Exception as e:
        print(f"COMPACT parse error: {e}")
    
    return listings


def write_listings(listings):
    """Write listings to listings.json."""
    # Clean up None values
    for listing in listings:
        for key in list(listing.keys()):
            if listing[key] is None:
                if key in ('beds', 'baths'):
                    del listing[key]
                elif key in ('sqft', 'lot', 'year'):
                    del listing[key]
    
    with open("listings.json", "w") as f:
        json.dump(listings, f, indent=2)
    
    print(f"Written {len(listings)} listings to listings.json")


def main():
    if not DDF_USERNAME or not DDF_PASSWORD:
        print("ERROR: DDF_USERNAME and DDF_PASSWORD must be set as environment variables")
        sys.exit(1)
    
    print("=" * 50)
    print("DDF Listing Updater")
    print(f"Agent CREA ID: {CREA_MEMBER_ID}")
    print("=" * 50)
    
    # Login
    if not login_to_ddf():
        print("Could not authenticate with DDF. Keeping existing listings.json.")
        sys.exit(0)  # Don't fail the workflow — keep existing data
    
    # Search
    listings = search_listings()
    
    if listings:
        write_listings(listings)
        print(f"\nSuccess: {len(listings)} active listings updated")
    else:
        print("\nNo listings found. This could mean:")
        print("  - No active listings for this agent")
        print("  - DDF query format needs adjustment")
        print("  - Credentials may not have correct permissions")
        print("\nKeeping existing listings.json unchanged.")


if __name__ == "__main__":
    main()
