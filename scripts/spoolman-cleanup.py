#!/usr/bin/env python3
"""
Spoolman Cleanup Tool

This tool finds and removes duplicate entries in Spoolman database.

Usage: python3 spoolman-cleanup.py [url] [--dry-run]

Default URL: http://localhost:7912

Features:
- Finds duplicate spools by NFC ID
- Finds duplicate filaments by vendor + material + color
- Finds duplicate vendors by name
- Keeps the oldest (original) entry in each group and deletes newer duplicates —
  preserves references from other Spoolman objects (e.g. spools referencing a
  filament, filaments referencing a vendor) and from external systems
  (Klipper save_variables, AFC lane assignments)
- Color output: green for KEEP, red for DELETE
- Interactive prompt for each group
- Dry-run mode to preview changes
"""

import requests
import sys

def get_all_spools(url):
    response = requests.get(f"{url}/api/v1/spool")
    response.raise_for_status()
    return response.json()

def get_all_filaments(url):
    response = requests.get(f"{url}/api/v1/filament")
    response.raise_for_status()
    return response.json()

def get_all_vendors(url):
    response = requests.get(f"{url}/api/v1/vendor")
    response.raise_for_status()
    return response.json()

def strip_quotes(s):
    if s and s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s

def find_duplicate_spools(spools):
    groups = {}
    for spool in spools:
        nfc_id = strip_quotes(spool.get('extra', {}).get('nfc_id', ''))
        if not nfc_id:
            continue
        if nfc_id not in groups:
            groups[nfc_id] = []
        groups[nfc_id].append(spool)
    return {k: v for k, v in groups.items() if len(v) > 1}

def find_duplicate_filaments(filaments):
    groups = {}
    for filament in filaments:
        vendor = filament.get('vendor', {}).get('name', '').lower()
        material = filament.get('material', '').lower()
        color = filament.get('color_hex', '').lower()
        key = f"{vendor}::{material}::{color}"
        if not key:
            continue
        if key not in groups:
            groups[key] = []
        groups[key].append(filament)
    return {k: v for k, v in groups.items() if len(v) > 1}

def find_duplicate_vendors(vendors):
    groups = {}
    for vendor in vendors:
        name = vendor.get('name', '').lower()
        if not name:
            continue
        if name not in groups:
            groups[name] = []
        groups[name].append(vendor)
    return {k: v for k, v in groups.items() if len(v) > 1}

def sort_by_registered(items):
    # Oldest first so the group[0] is the original entry we keep.
    # Deleting the newer duplicates preserves references held by other
    # Spoolman objects and external systems (Klipper, AFC) (#68).
    return sorted(items, key=lambda x: x.get('registered', ''))

def format_item(item, entity_type):
    """Format an item for display based on entity type."""
    reg = item.get('registered', 'N/A')[:10] if item.get('registered') else 'N/A'
    if entity_type == "Spool":
        fil = item.get('filament', {}) or {}
        vendor = (fil.get('vendor', {}) or {}).get('name', 'N/A')
        nfc_id = strip_quotes(item.get('extra', {}).get('nfc_id', 'N/A'))
        remaining = item.get('remaining_weight')
        weight = f"{remaining:.0f}g" if remaining is not None else "N/A"
        return f"ID {item['id']:>4d} | {vendor:>15s} | {fil.get('material', 'N/A'):>6s} | {weight:>6s} | nfc_id={nfc_id} | {reg}"
    elif entity_type == "Filament":
        vendor = (item.get('vendor', {}) or {}).get('name', 'N/A')
        return f"ID {item['id']:>4d} | {vendor:>15s} | {item.get('material', 'N/A'):>6s} | #{item.get('color_hex', 'N/A'):>6s} | name={item.get('name', 'N/A')} | {reg}"
    else:  # Vendor
        return f"ID {item['id']:>4d} | {item.get('name', 'N/A')} | {reg}"

def print_group(items, keep_index, entity_type):
    print(f"\n{entity_type} group:")
    for i, item in enumerate(items):
        label = "\033[92mKEEP  \033[0m" if i == keep_index else "\033[91mDELETE\033[0m"
        print(f"  {label} {format_item(item, entity_type)}")

def get_user_choice():
    while True:
        choice = input("\nDelete this group? (y=delete, n=skip, a=delete all, q=quit): ").lower()
        if choice in ['y', 'n', 'a', 'q']:
            return choice

def delete_entity(url, entity_type, entity_id):
    response = requests.delete(f"{url}/api/v1/{entity_type}/{entity_id}")
    response.raise_for_status()

def main():
    dry_run = '--dry-run' in sys.argv
    url = "http://localhost:7912"
    for arg in sys.argv[1:]:
        if arg.startswith("http"):
            url = arg.rstrip("/")
        elif arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)

    try:
        print(f"Fetching data from {url}...")
        spools = get_all_spools(url)
        filaments = get_all_filaments(url)
        vendors = get_all_vendors(url)

        print(f"Found {len(spools)} spools, {len(filaments)} filaments, {len(vendors)} vendors")

        def process_duplicates(groups, entity_type, api_name):
            if not groups:
                print(f"\nNo duplicate {entity_type.lower()}s found.")
                return
            total = sum(len(g) - 1 for g in groups.values())
            print(f"\n=== DUPLICATE {entity_type.upper()}S ({total} in {len(groups)} groups) ===")
            delete_all = False
            for key, group in groups.items():
                sorted_group = sort_by_registered(group)
                print_group(sorted_group, 0, entity_type)
                if dry_run:
                    continue
                if not delete_all:
                    choice = get_user_choice()
                    if choice == 'q':
                        return
                    if choice == 'a':
                        delete_all = True
                    elif choice == 'n':
                        continue
                # Delete (choice was y, a, or delete_all from earlier)
                for i, item in enumerate(sorted_group):
                    if i != 0:
                        try:
                            delete_entity(url, api_name, item['id'])
                            print(f"  \033[92m✓\033[0m Deleted {api_name} ID {item['id']}")
                        except Exception as e:
                            print(f"  \033[91m✗\033[0m Failed to delete {api_name} ID {item['id']}: {e}")

        process_duplicates(find_duplicate_spools(spools), "Spool", "spool")
        process_duplicates(find_duplicate_filaments(filaments), "Filament", "filament")
        process_duplicates(find_duplicate_vendors(vendors), "Vendor", "vendor")

        if dry_run:
            print("\n[DRY RUN] No deletions were made.")
        else:
            print("\nCleanup completed.")

    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Spoolman: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
