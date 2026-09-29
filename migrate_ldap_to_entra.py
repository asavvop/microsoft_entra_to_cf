#!/usr/bin/env python3
"""
Tanzu Platform: LDAP to Microsoft Entra ID Live Role Migration Tool

Queries Cloud Foundry and UAA for active role assignments with 'origin: ldap',
translates usernames to Microsoft Entra ID identities via Microsoft Graph,
and provisions new roles under 'origin: EntraSAML' (or custom origin).
"""

import os
import sys
import json
import csv
import time
import argparse
import subprocess
import urllib.request
import urllib.parse

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

def cf_curl(endpoint):
    out = subprocess.check_output(["cf", "curl", endpoint])
    return json.loads(out.decode("utf-8"))

def get_uaa_users(uaa_url):
    cf_oauth = subprocess.check_output(["cf", "oauth-token"]).decode("utf-8").strip()
    cmd = f'curl -s -k -H "Authorization: {cf_oauth}" "{uaa_url.rstrip("/")}/Users?count=5000"'
    out = subprocess.check_output(cmd, shell=True)
    return json.loads(out.decode("utf-8")).get("resources", [])

def main():
    parser = argparse.ArgumentParser(description="Live Tanzu Platform LDAP to Entra ID Role Migration.")
    parser.add_argument("--live", action="store_true", help="Execute live migration against Cloud Foundry (default is Dry-Run)")
    parser.add_argument("--origin", default=os.environ.get("NEW_ORIGIN", "EntraSAML"), help="New identity provider origin key in Tanzu (default: EntraSAML)")
    parser.add_argument("--identity-type", choices=["upn", "email"], default=os.environ.get("ENTRA_IDENTITY_TYPE", "upn"), help="Target identity attribute from Entra (default: upn)")
    parser.add_argument("--cache-file", default=".entra_users_cache.json", help="Path to Entra users cache file (default: .entra_users_cache.json)")
    parser.add_argument("--cache-ttl", type=float, default=4.0, help="Cache Time-To-Live in hours (default: 4.0 hours)")
    parser.add_argument("--refresh-cache", action="store_true", help="Bypass cache and force fresh download from Microsoft Graph")
    parser.add_argument("--no-cache", action="store_true", help="Disable caching entirely (always fetch live, do not save to disk)")
    parser.add_argument("--tenant-id", default=os.environ.get("ENTRA_TENANT_ID"), help="Microsoft Entra Tenant ID (or env ENTRA_TENANT_ID)")
    parser.add_argument("--client-id", default=os.environ.get("ENTRA_CLIENT_ID"), help="App Registration Client ID (or env ENTRA_CLIENT_ID)")
    parser.add_argument("--client-secret", default=os.environ.get("ENTRA_CLIENT_SECRET"), help="App Registration Client Secret (or env ENTRA_CLIENT_SECRET)")
    parser.add_argument("--uaa-url", default=os.environ.get("UAA_URL"), help="Tanzu UAA Base URL (e.g., https://login.sys.example.com)")
    args = parser.parse_args()

    # Auto-detect UAA URL from CF API if not explicitly supplied
    if not args.uaa_url:
        try:
            info = cf_curl("/v2/info")
            args.uaa_url = info.get("token_endpoint") or "https://login.sys.example.com"
        except Exception:
            args.uaa_url = "https://login.sys.example.com"

    dry_run = not args.live

    print("=" * 65)
    print("  Tanzu LDAP -> Entra ID Live Role Migration Tool")
    print(f"  Mode: {'DRY RUN (Preview Only)' if dry_run else 'LIVE EXECUTION'}")
    print(f"  Target Origin: {args.origin}")
    print(f"  Identity Attribute: {args.identity_type.upper()} (UserPrincipalName)" if args.identity_type == "upn" else f"  Identity Attribute: {args.identity_type.upper()} (Email)")
    print("=" * 65)

    # 1. Fetch or Load Entra Users
    print("\n[1/3] Fetching Entra ID user identities...")
    raw_users = None

    if not args.no_cache and not args.refresh_cache:
        is_valid, age_hours, cached_users = check_cache_status(args.cache_file, args.cache_ttl)
        if is_valid and cached_users:
            raw_users = cached_users
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
            print(f"❌ Error connecting to Microsoft Graph: {e}")
            sys.exit(1)

    users = raw_users
    
    user_map = {}
    for u in users:
        if args.identity_type == "email":
            target_id = u.get("mail") or u.get("userPrincipalName")
        else:
            target_id = u.get("userPrincipalName") or u.get("mail")
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

    print(f"  -> Successfully indexed {len(user_map)} Entra user lookup keys.")

    # 2. Fetch CF Roles & UAA Identities
    print("\n[2/3] Querying Cloud Foundry & UAA for active LDAP role assignments...")
    try:
        roles_json = cf_curl("/v3/roles?per_page=5000")
        spaces_json = cf_curl("/v3/spaces?per_page=5000")
        orgs_json = cf_curl("/v3/organizations?per_page=5000")
        uaa_users = get_uaa_users(args.uaa_url)
    except Exception as e:
        print(f"❌ Error querying Cloud Foundry API. Please ensure you ran 'cf login': {e}")
        sys.exit(1)

    user_lookup = {u["id"]: {"username": u.get("userName", ""), "origin": u.get("origin", "")} for u in uaa_users}
    space_lookup = {s["guid"]: {"name": s["name"], "org_guid": s["relationships"]["organization"]["data"]["guid"]} for s in spaces_json.get("resources", [])}
    org_lookup = {o["guid"]: o["name"] for o in orgs_json.get("resources", [])}

    # 3. Match & Migrate
    print("\n[3/3] Processing role translations...")
    migration_results = []
    unmapped_users = []

    role_map_space = {
        "space_developer": "SpaceDeveloper",
        "space_manager": "SpaceManager",
        "space_auditor": "SpaceAuditor",
        "space_supporter": "SpaceSupporter"
    }

    role_map_org = {
        "organization_manager": "OrgManager",
        "organization_auditor": "OrgAuditor",
        "organization_billing_manager": "BillingManager"
    }

    for role in roles_json.get("resources", []):
        u_guid = role["relationships"]["user"]["data"]["guid"]
        user_info = user_lookup.get(u_guid)

        if user_info and user_info.get("origin") == "ldap":
            ldap_user = user_info["username"].lower().strip()
            role_type = role["type"]
            entra_target = user_map.get(ldap_user)

            if not entra_target:
                unmapped_users.append({"ldap_user": ldap_user, "role": role_type})
                continue

            # Space Role
            if role["relationships"].get("space", {}).get("data"):
                s_guid = role["relationships"]["space"]["data"]["guid"]
                space_name = space_lookup[s_guid]["name"]
                org_name = org_lookup[space_lookup[s_guid]["org_guid"]]
                cli_role = role_map_space.get(role_type)

                if cli_role:
                    cmd = f'cf set-space-role "{entra_target}" "{org_name}" "{space_name}" {cli_role} --origin {args.origin}'
                    migration_results.append({
                        "type": "Space", "ldap_user": ldap_user, "entra_user": entra_target,
                        "org": org_name, "space": space_name, "role": cli_role, "command": cmd
                    })

                    if not dry_run:
                        print(f"Running: {cmd}")
                        subprocess.call(cmd, shell=True)

            # Org Role
            elif role["relationships"].get("organization", {}).get("data"):
                o_guid = role["relationships"]["organization"]["data"]["guid"]
                org_name = org_lookup[o_guid]
                cli_role = role_map_org.get(role_type)

                if cli_role:
                    cmd = f'cf set-org-role "{entra_target}" "{org_name}" {cli_role} --origin {args.origin}'
                    migration_results.append({
                        "type": "Org", "ldap_user": ldap_user, "entra_user": entra_target,
                        "org": org_name, "space": "-", "role": cli_role, "command": cmd
                    })

                    if not dry_run:
                        print(f"Running: {cmd}")
                        subprocess.call(cmd, shell=True)

    # 4. Export CSV Reports
    with open("migration_plan.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["type", "ldap_user", "entra_user", "org", "space", "role", "command"])
        writer.writeheader()
        writer.writerows(migration_results)
    print(f"\n📊 Migration plan exported to 'migration_plan.csv' ({len(migration_results)} actionable roles).")

    if unmapped_users:
        with open("unmapped_ldap_users.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["ldap_user", "role"])
            writer.writeheader()
            writer.writerows(unmapped_users)
        print(f"⚠️ Warning: {len(unmapped_users)} roles had unmapped LDAP users (saved to 'unmapped_ldap_users.csv').")

    print("\n✅ Finished!")

if __name__ == "__main__":
    main()
