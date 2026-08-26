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
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]

def get_entra_users(token):
    url = "https://graph.microsoft.com/v1.0/users?$select=id,displayName,userPrincipalName,mail,onPremisesSamAccountName,mailNickname"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8")).get("value", [])

def build_entra_lookup_map(tenant_id, client_id, client_secret):
    token = get_graph_token(tenant_id, client_id, client_secret)
    users = get_entra_users(token)
    
    user_map = {}
    for u in users:
        target_id = u.get("mail") or u.get("userPrincipalName")
        
        # 1. Primary match: On-Premises sAMAccountName (for Hybrid AD sync)
        if u.get("onPremisesSamAccountName"):
            user_map[u["onPremisesSamAccountName"].lower().strip()] = target_id
            
        # 2. Fallback: UPN prefix & MailNickname (for Cloud-native / Lab accounts)
        upn = u.get("userPrincipalName", "")
        if "@" in upn:
            prefix = upn.split("@")[0].lower().strip()
            clean_prefix = prefix.split("_")[0]  # Handles guest accounts format (e.g., user_external#EXT#)
            user_map[prefix] = target_id
            user_map[clean_prefix] = target_id

    return user_map

def process_yaml_file(file_path, user_map, dry_run=True):
    with open(file_path, "r") as f:
        try:
            content = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"  ❌ Error parsing YAML file {file_path}: {e}")
            return [], []

    roles = ["developer", "manager", "auditor", "billing-manager", "supporter"]
    file_migrations = []
    file_unmapped = []
    modified = False

    for role in roles:
        if role in content and isinstance(content[role], dict):
            role_block = content[role]
            legacy_users = role_block.get("users", []) or []
            ldap_users = role_block.get("ldap_users", []) or []
            saml_users = role_block.get("saml_users", []) or []

            all_ldap_candidates = list(set(legacy_users + ldap_users))
            remaining_unmapped = []

            for u in all_ldap_candidates:
                if not isinstance(u, str):
                    continue
                ldap_key = u.lower().strip()
                entra_target = user_map.get(ldap_key)

                if entra_target:
                    if entra_target not in saml_users:
                        saml_users.append(entra_target)
                    file_migrations.append({
                        "file": file_path,
                        "role": role,
                        "ldap_user": u,
                        "entra_user": entra_target
                    })
                    modified = True
                else:
                    remaining_unmapped.append(u)
                    file_unmapped.append({
                        "file": file_path,
                        "role": role,
                        "ldap_user": u
                    })

            # Update YAML role block
            role_block["saml_users"] = sorted(saml_users)
            role_block["users"] = sorted(remaining_unmapped)
            if "ldap_users" in role_block:
                role_block["ldap_users"] = []

    if modified:
        if dry_run:
            preview_path = file_path + ".preview.yml"
            with open(preview_path, "w") as f:
                yaml.dump(content, f, default_flow_style=False, sort_keys=False)
        else:
            with open(file_path, "w") as f:
                yaml.dump(content, f, default_flow_style=False, sort_keys=False)

    return file_migrations, file_unmapped

def main():
    parser = argparse.ArgumentParser(description="Transform cf-management YAML configs from LDAP to Entra SAML.")
    parser.add_argument("--dir", default="./sample_cf_management_repo", help="Path to cf-management config directory (default: ./sample_cf_management_repo)")
    parser.add_argument("--live", action="store_true", help="Modify YAML files in-place (default is Dry-Run)")
    parser.add_argument("--tenant-id", default=os.environ.get("ENTRA_TENANT_ID"), help="Microsoft Entra Tenant ID (or env ENTRA_TENANT_ID)")
    parser.add_argument("--client-id", default=os.environ.get("ENTRA_CLIENT_ID"), help="App Registration Client ID (or env ENTRA_CLIENT_ID)")
    parser.add_argument("--client-secret", default=os.environ.get("ENTRA_CLIENT_SECRET"), help="App Registration Client Secret (or env ENTRA_CLIENT_SECRET)")
    args = parser.parse_args()

    if not args.tenant_id or not args.client_id or not args.client_secret:
        print("❌ Error: Microsoft Entra credentials are required.")
        print("Please provide --tenant-id, --client-id, and --client-secret, or export environment variables:")
        print("  export ENTRA_TENANT_ID='<YOUR_TENANT_ID>'")
        print("  export ENTRA_CLIENT_ID='<YOUR_CLIENT_ID>'")
        print("  export ENTRA_CLIENT_SECRET='<YOUR_CLIENT_SECRET>'")
        sys.exit(1)

    dry_run = not args.live

    print("=" * 70)
    print("  🚀 cf-management YAML Config Transformer (LDAP ➡️ Entra ID SAML)")
    print(f"  Mode: {'DRY RUN (Generates .preview.yml files)' if dry_run else 'LIVE IN-PLACE MODIFICATION'}")
    print(f"  Target Directory: {args.dir}")
    print("=" * 70)

    # 1. Fetch Entra Users
    print("\n[1/3] Fetching users from Microsoft Graph...")
    try:
        user_map = build_entra_lookup_map(args.tenant_id, args.client_id, args.client_secret)
        print(f"  -> Successfully indexed {len(user_map)} Entra user lookup keys.")
    except Exception as e:
        print(f"  ❌ Error querying Microsoft Graph: {e}")
        sys.exit(1)

    # 2. Find YAML Configs
    print("\n[2/3] Scanning cf-management YAML configuration files...")
    yaml_files = []
    for root, _, files in os.walk(args.dir):
        for file in files:
            if file.endswith((".yml", ".yaml")) and not file.endswith(".preview.yml"):
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
                print(f"   ✅ [{m['role']}] {m['ldap_user']} ➡️  {m['entra_user']} (Added to saml_users)")
            for u in unmapped:
                print(f"   ⚠️  [{u['role']}] {u['ldap_user']} (No Entra match - kept in unmapped)")
            all_migrations.extend(migrations)
            all_unmapped.extend(unmapped)

    # 4. Export CSV Reports
    with open("yaml_migration_plan.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "role", "ldap_user", "entra_user"])
        writer.writeheader()
        writer.writerows(all_migrations)

    with open("yaml_unmapped_users.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "role", "ldap_user"])
        writer.writeheader()
        writer.writerows(all_unmapped)

    print("\n" + "=" * 70)
    print(f"📊 Summary:")
    print(f"  - Total Roles Migrated: {len(all_migrations)} (saved to 'yaml_migration_plan.csv')")
    print(f"  - Total Unmapped Roles: {len(all_unmapped)} (saved to 'yaml_unmapped_users.csv')")
    if dry_run:
        print("  - Preview files created with '.preview.yml' extension. Review before applying --live.")
    else:
        print("  - All YAML files updated in-place!")
    print("=" * 70)

if __name__ == "__main__":
    main()
