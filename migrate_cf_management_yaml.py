#!/usr/bin/env python3
"""
cf-management YAML Config Transformer (LDAP -> Entra ID SAML)

Scans cf-management repository orgConfig.yml & spaceConfig.yml files,
queries Microsoft Graph to translate legacy LDAP usernames (sAMAccountName)
to Microsoft Entra ID identities (UserPrincipalName / Email),
and moves users from 'users' / 'ldap_users' to 'saml_users'.
"""

import os
import sys
import json
import csv
import time
import shutil
import argparse
import urllib.request
import urllib.parse
import yaml

def get_graph_token(tenant_id, client_id, client_secret):
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default"
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]

def get_entra_users(token):
    all_users = []
    url = "https://graph.microsoft.com/v1.0/users?$top=999&$select=id,displayName,userPrincipalName,mail,onPremisesSamAccountName,mailNickname"
    page = 1
    
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            batch = data.get("value", [])
            all_users.extend(batch)
            url = data.get("@odata.nextLink")
            if url:
                page += 1
                print(f"    ... fetched page {page} ({len(all_users)} total users so far)")
                
    return all_users

def check_cache_status(cache_path, ttl_hours):
    """
    Checks if a valid cache file exists and is within TTL.
    Returns (is_valid, age_hours, users_list)
    """
    if not os.path.exists(cache_path):
        return False, 0.0, None
    try:
        mtime = os.path.getmtime(cache_path)
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "users" in data:
            cached_time = data.get("cached_at", mtime)
            users = data.get("users", [])
        elif isinstance(data, list):
            cached_time = mtime
            users = data
        else:
            return False, 0.0, None

        age_seconds = time.time() - cached_time
        age_hours = age_seconds / 3600.0

        is_valid = (age_hours <= ttl_hours)
        return is_valid, age_hours, users
    except Exception as e:
        print(f"  ⚠️ Warning: Failed reading cache file '{cache_path}': {e}")
        return False, 0.0, None

def save_users_cache(cache_path, users, tenant_id=None):
    """
    Saves users to local JSON cache file atomically.
    """
    try:
        cache_data = {
            "cached_at": time.time(),
            "tenant_id": tenant_id,
            "user_count": len(users),
            "users": users
        }
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f)
        os.replace(tmp_path, cache_path)
        print(f"  💾 Saved {len(users)} users to cache: '{cache_path}' (valid for 4 hours)")
    except Exception as e:
        print(f"  ⚠️ Warning: Could not save cache to '{cache_path}': {e}")

def build_user_map_from_users(users, identity_type="samaccountname"):
    user_map = {}
    norm_type = (identity_type or "samaccountname").lower().strip()
    for u in users:
        if norm_type in ("upn", "principal", "userprincipalname"):
            target_id = u.get("userPrincipalName") or u.get("mail") or u.get("onPremisesSamAccountName") or u.get("mailNickname")
        elif norm_type == "email":
            target_id = u.get("mail") or u.get("userPrincipalName") or u.get("onPremisesSamAccountName") or u.get("mailNickname")
        else:  # default: onPremisesSamAccountName / samaccountname
            target_id = u.get("onPremisesSamAccountName") or u.get("mailNickname") or (u.get("userPrincipalName", "").split("@")[0] if u.get("userPrincipalName") else None) or u.get("userPrincipalName") or u.get("mail")
        
        if not target_id:
            continue
        
        # 1. On-Premises sAMAccountName (Legacy AD / Hybrid sync)
        if u.get("onPremisesSamAccountName"):
            user_map[u["onPremisesSamAccountName"].lower().strip()] = target_id
            
        # 2. Mail Nickname (Exchange / Entra alias)
        if u.get("mailNickname"):
            user_map[u["mailNickname"].lower().strip()] = target_id

        # 3. Email prefix & full email
        mail = u.get("mail", "")
        if mail:
            user_map[mail.lower().strip()] = target_id
            if "@" in mail:
                user_map[mail.split("@")[0].lower().strip()] = target_id

        # 4. UPN prefix & full userPrincipalName (Cloud-native / Guest accounts)
        upn = u.get("userPrincipalName", "")
        if upn:
            user_map[upn.lower().strip()] = target_id
            if "@" in upn:
                prefix = upn.split("@")[0].lower().strip()
                clean_prefix = prefix.split("_")[0]  # Handles guest format (e.g. user_external#EXT#)
                user_map[prefix] = target_id
                user_map[clean_prefix] = target_id

    return user_map

def dump_entra_users_report(users, json_file="entra_users_dump.json", csv_file="entra_users_dump.csv"):
    with open(json_file, "w") as f:
        json.dump(users, f, indent=2)

    with open(csv_file, "w", newline="") as f:
        fieldnames = ["displayName", "userPrincipalName", "onPremisesSamAccountName", "mail", "mailNickname", "id"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(users)

    sam_count = sum(1 for u in users if u.get("onPremisesSamAccountName"))
    mail_count = sum(1 for u in users if u.get("mail"))
    upn_count = sum(1 for u in users if u.get("userPrincipalName"))

    print("\n" + "=" * 70)
    print("📋 Entra Users Attribute Breakdown:")
    print(f"  - Total Entra Users Fetched: {len(users)}")
    print(f"  - Users with onPremisesSamAccountName: {sam_count}")
    print(f"  - Users with mail: {mail_count}")
    print(f"  - Users with userPrincipalName: {upn_count}")
    print(f"  - Full JSON dump saved to: '{json_file}'")
    print(f"  - CSV dump saved to: '{csv_file}'")
    print("=" * 70)

    print("\n🔍 Sample User Properties (First 5):")
    for i, u in enumerate(users[:5], 1):
        print(f"\n  [{i}] DisplayName: {u.get('displayName')}")
        print(f"      userPrincipalName:        {u.get('userPrincipalName')}")
        print(f"      onPremisesSamAccountName: {u.get('onPremisesSamAccountName')}")
        print(f"      mail:                     {u.get('mail')}")
        print(f"      mailNickname:             {u.get('mailNickname')}")

def process_yaml_file(file_path, user_map, dry_run=True):
    with open(file_path, "r") as f:
        try:
            content = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"  ❌ Error parsing YAML file {file_path}: {e}")
            return [], []

    file_migrations = []
    file_unmapped = []
    modified = False

    for role, role_block in content.items():
        if isinstance(role_block, dict) and any(field in role_block for field in ("users", "ldap_users", "saml_users")):
            raw_users = role_block.get("users", []) or []
            raw_ldap = role_block.get("ldap_users", []) or []
            raw_saml = role_block.get("saml_users", []) or []

            # Check if there were legacy entries that need clearing
            had_legacy = bool(raw_users or raw_ldap)

            # Gather all candidate users across users, ldap_users, and existing saml_users
            all_candidates = []
            for item in raw_users + raw_ldap + raw_saml:
                if isinstance(item, str) and item.strip() and item.strip() not in all_candidates:
                    all_candidates.append(item.strip())

            new_saml_users = []

            for u in all_candidates:
                ldap_key = u.lower().strip()
                entra_target = user_map.get(ldap_key)

                if entra_target:
                    if not any(existing.lower() == entra_target.lower() for existing in new_saml_users):
                        new_saml_users.append(entra_target)
                    file_migrations.append({
                        "file": file_path,
                        "role": role,
                        "original_user": u,
                        "entra_user": entra_target
                    })
                    if u.strip() != entra_target.strip() or had_legacy:
                        modified = True
                else:
                    # Unmapped user: maintain in saml_users so no access is lost
                    if not any(existing.lower() == u.lower() for existing in new_saml_users):
                        new_saml_users.append(u)
                    file_unmapped.append({
                        "file": file_path,
                        "role": role,
                        "unmapped_user": u
                    })
                    if had_legacy:
                        modified = True

            # Update YAML role block
            role_block["saml_users"] = sorted(list(set(new_saml_users)))
            if "users" in role_block:
                role_block["users"] = []
            if "ldap_users" in role_block:
                role_block["ldap_users"] = []

    if modified:
        if dry_run:
            preview_path = file_path + ".preview.yml"
            with open(preview_path, "w") as f:
                yaml.dump(content, f, default_flow_style=False, sort_keys=False)
        else:
            # Create backup file of the original before overwriting
            backup_path = file_path + ".bak"
            shutil.copy2(file_path, backup_path)
            with open(file_path, "w") as f:
                yaml.dump(content, f, default_flow_style=False, sort_keys=False)

    return file_migrations, file_unmapped

def main():
    parser = argparse.ArgumentParser(description="Transform cf-management YAML configs from LDAP to Entra SAML.")
    parser.add_argument("--dir", default="./sample_cf_management_repo", help="Path to cf-management config directory (default: ./sample_cf_management_repo)")
    parser.add_argument("--live", action="store_true", help="Modify YAML files in-place with .bak backups (default is Dry-Run)")
    parser.add_argument("--dump-users", action="store_true", help="Export all fetched Entra users to entra_users_dump.json & .csv with sample prints")
    parser.add_argument("--dump-only", action="store_true", help="Only dump/search Entra users without running YAML transformation")
    parser.add_argument("--search-user", help="Search and display raw Entra ID attributes for a specific user")
    parser.add_argument("--cache-file", default=".entra_users_cache.json", help="Path to Entra users cache file (default: .entra_users_cache.json)")
    parser.add_argument("--cache-ttl", type=float, default=4.0, help="Cache Time-To-Live in hours (default: 4.0 hours)")
    parser.add_argument("--refresh-cache", action="store_true", help="Bypass cache and force fresh download from Microsoft Graph")
    parser.add_argument("--no-cache", action="store_true", help="Disable caching entirely (always fetch live, do not save to disk)")
    parser.add_argument("--use-upn", "--upn", action="store_true", dest="use_upn", help="Use UserPrincipalName (UPN) instead of onPremisesSamAccountName as the output target identity")
    parser.add_argument("--identity-type", choices=["samaccountname", "upn", "email", "onPremisesSamAccountName", "sam"], default=os.environ.get("ENTRA_IDENTITY_TYPE", "samaccountname"), help="Target identity attribute from Entra: 'samaccountname' (default), 'upn', or 'email'")
    parser.add_argument("--tenant-id", default=os.environ.get("ENTRA_TENANT_ID"), help="Microsoft Entra Tenant ID (or env ENTRA_TENANT_ID)")
    parser.add_argument("--client-id", default=os.environ.get("ENTRA_CLIENT_ID"), help="App Registration Client ID (or env ENTRA_CLIENT_ID)")
    parser.add_argument("--client-secret", default=os.environ.get("ENTRA_CLIENT_SECRET"), help="App Registration Client Secret (or env ENTRA_CLIENT_SECRET)")
    args = parser.parse_args()

    # Determine target identity type: --use-upn / --upn overrides --identity-type
    raw_identity_type = "upn" if args.use_upn else args.identity_type.lower()
    if raw_identity_type in ("samaccountname", "sam", "onpremisessamaccountname"):
        identity_type = "samaccountname"
        ident_desc = "onPremisesSamAccountName (sAMAccountName)"
    elif raw_identity_type in ("upn", "principal", "userprincipalname"):
        identity_type = "upn"
        ident_desc = "UserPrincipalName (UPN)"
    elif raw_identity_type == "email":
        identity_type = "email"
        ident_desc = "Email (mail)"
    else:
        identity_type = "samaccountname"
        ident_desc = "onPremisesSamAccountName (sAMAccountName)"

    dry_run = not args.live

    print("=" * 70)
    print("  🚀 cf-management YAML Config Transformer (LDAP ➡️ Entra ID SAML)")
    if not args.dump_only:
        print(f"  Mode: {'DRY RUN (Generates .preview.yml files)' if dry_run else 'LIVE IN-PLACE MODIFICATION (Creates .bak backups)'}")
        print(f"  Target Directory: {args.dir}")
        print(f"  Identity Attribute: {ident_desc}")
    print("=" * 70)

    # 1. Fetch or Load Entra Users
    print("\n[1/3] Fetching Entra ID user identities...")
    raw_users = None
    use_cache = False

    if not args.no_cache and not args.refresh_cache:
        is_valid, age_hours, cached_users = check_cache_status(args.cache_file, args.cache_ttl)
        if is_valid and cached_users:
            raw_users = cached_users
            use_cache = True
            print(f"  📦 Loaded {len(raw_users)} users from local cache: '{args.cache_file}'")
            print(f"     (Age: {age_hours:.1f}h / TTL: {args.cache_ttl}h | Use --refresh-cache to force update)")
        elif cached_users is not None and not is_valid:
            print(f"  ⏳ Cache '{args.cache_file}' expired (Age: {age_hours:.1f}h > TTL {args.cache_ttl}h). Fetching fresh from Graph...")

    if raw_users is None:
        if not args.tenant_id or not args.client_id or not args.client_secret:
            print("❌ Error: Microsoft Entra credentials are required to fetch fresh data from Graph API.")
            print("Please provide --tenant-id, --client-id, and --client-secret, or export environment variables:")
            print("  export ENTRA_TENANT_ID='<YOUR_TENANT_ID>'")
            print("  export ENTRA_CLIENT_ID='<YOUR_CLIENT_ID>'")
            print("  export ENTRA_CLIENT_SECRET='<YOUR_CLIENT_SECRET>'")
            sys.exit(1)

        print("  🌐 Connecting to Microsoft Graph API...")
        try:
            token = get_graph_token(args.tenant_id, args.client_id, args.client_secret)
            raw_users = get_entra_users(token)
            print(f"  -> Successfully fetched {len(raw_users)} users from Microsoft Graph.")
            if not args.no_cache:
                save_users_cache(args.cache_file, raw_users, tenant_id=args.tenant_id)
        except Exception as e:
            print(f"  ❌ Error querying Microsoft Graph: {e}")
            sys.exit(1)

    user_map = build_user_map_from_users(raw_users, identity_type=identity_type)
    print(f"  -> Indexed {len(user_map)} unique lookup keys for mapping.")

    if args.dump_users or args.dump_only:
        dump_entra_users_report(raw_users)

    if args.search_user:
        query = args.search_user.lower().strip()
        matches = [u for u in raw_users if query in json.dumps(u).lower()]
        print("\n" + "=" * 70)
        print(f"🔎 Search results for '{args.search_user}' ({len(matches)} match{'es' if len(matches) != 1 else ''}):")
        for m in matches:
            print(json.dumps(m, indent=2))
        print("=" * 70)

    if args.dump_only:
        print("\n🏁 Dump complete. Exiting without modifying YAML files.")
        return

    # 2. Find YAML Configs
    print("\n[2/3] Scanning cf-management YAML configuration files...")
    yaml_files = []
    for root, _, files in os.walk(args.dir):
        for file in files:
            if file.endswith((".yml", ".yaml")) and not file.endswith((".preview.yml", ".bak")):
                yaml_files.append(os.path.join(root, file))

    print(f"  -> Found {len(yaml_files)} YAML config files.")

    # 3. Transform Files
    print("\n[3/3] Transforming YAML role blocks...")
    all_migrations = []
    all_unmapped = []

    for yf in yaml_files:
        rel_path = os.path.relpath(yf, args.dir)
        migrations, unmapped = process_yaml_file(yf, user_map, dry_run=dry_run)
        if migrations or unmapped:
            print(f"\n📄 {rel_path}:")
            for m in migrations:
                print(f"   ✅ [{m['role']}] {m['original_user']} ➡️  {m['entra_user']} (Updated in saml_users)")
            for u in unmapped:
                print(f"   ⚠️  [{u['role']}] {u['unmapped_user']} (No Entra match - maintained in saml_users)")
            all_migrations.extend(migrations)
            all_unmapped.extend(unmapped)

    # 4. Export CSV Reports
    with open("yaml_mapped_users.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "role", "original_user", "entra_user"])
        writer.writeheader()
        writer.writerows(all_migrations)

    with open("yaml_unmapped_users.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "role", "unmapped_user"])
        writer.writeheader()
        writer.writerows(all_unmapped)

    print("\n" + "=" * 70)
    print(f"📊 Summary:")
    print(f"  - Total Mapped Users:   {len(all_migrations)} (saved to 'yaml_mapped_users.csv')")
    print(f"  - Total Unmapped Users: {len(all_unmapped)} (saved to 'yaml_unmapped_users.csv')")
    if dry_run:
        print("  - Preview files created with '.preview.yml' extension. Review before applying --live.")
    else:
        print("  - All YAML files updated in-place (original backups created with '.bak' extension)!")
    print("=" * 70)

if __name__ == "__main__":
    main()
