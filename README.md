# 🔄 Tanzu Platform: LDAP to Microsoft Entra ID Role Migration Tools

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Cloud Foundry](https://img.shields.io/badge/Cloud%20Foundry-v2%20%2F%20v3-0070BA?logo=cloudfoundry)](https://www.cloudfoundry.org/)
[![Microsoft Entra ID](https://img.shields.io/badge/Microsoft%20Entra%20ID-SAML%202.0%20%2F%20OIDC-0078D4?logo=microsoftazure)](https://entra.microsoft.com/)

Automated toolkit to migrate user roles in **VMware Tanzu Application Service (TAS / Cloud Foundry)** from legacy on-premises **LDAP / Active Directory** accounts to **Microsoft Entra ID (SAML 2.0 / OIDC)**.

---

## 📌 The Challenge

When enterprise Tanzu foundations transition from on-premises Active Directory to Microsoft Entra ID:
1. **Identity Format Mismatch**:
   * **Legacy LDAP**: Users were identified by Windows `sAMAccountName` (e.g., `jsmith`).
   * **Microsoft Entra ID**: Users are identified by email / UserPrincipalName (e.g., `john.smith@company.com`).
2. **Configuration Repositories**:
   * Customers using `cf-management` GitOps have hundreds of YAML files listing raw LDAP usernames with no email addresses.
3. **Foundation Role Bindings**:
   * Existing role assignments in Cloud Foundry belong to `origin: ldap` and must be recreated under `origin: EntraSAML` without downtime or manual reassignment.

---

## 🏗️ Architecture & Migration Workflow

This toolkit queries **Microsoft Graph API** to automatically correlate on-premises `sAMAccountName` attributes with modern Entra ID identities and offers two migration paths:

```
┌────────────────────────────────────────────────────────┐       ┌────────────────────────────────────────────────────────┐
│               On-Premises LDAP / AD                    │       │                   Microsoft Entra ID                   │
│   Identity: sAMAccountName (e.g., "jsmith")            │       │   Identity: UPN / Mail ("john.smith@company.com")      │
└───────────────────────────┬────────────────────────────┘       └───────────────────────────┬────────────────────────────┘
                            │                                                                │
                            └───────────────────────────────┬────────────────────────────────┘
                                                            ▼
                                        [ Step 1: Microsoft Graph Resolution ]
                                         Matches onPremisesSamAccountName ➡️ UPN
                                                            │
                                  ┌─────────────────────────┴─────────────────────────┐
                                  ▼                                                   ▼
              ┌───────────────────────────────────────┐           ┌───────────────────────────────────────┐
              │  Path A: cf-management GitOps YAML    │           │  Path B: Direct Cloud Foundry API     │
              │  (migrate_cf_management_yaml.py)      │           │  (migrate_ldap_to_entra.py / .ps1)    │
              ├───────────────────────────────────────┤           ├───────────────────────────────────────┤
              │ Converts `users` ➡️ `saml_users`       │           │ Queries live `origin: ldap` roles and │
              │ in `orgConfig.yml` & `spaceConfig.yml`│           │ grants `cf set-space-role` with       │
              │ ready for Git commit / CI/CD pipeline │           │ `--origin EntraSAML`                  │
              └───────────────────┬───────────────────┘           └───────────────────┬───────────────────┘
                                  │                                                   │
                                  └─────────────────────────┬─────────────────────────┘
                                                            ▼
                                        [ Step 2: Audit & Reporting Reports ]
                                         - migration_plan.csv (Processed roles)
                                         - unmapped_users.csv (Orphaned accounts)
```

---

## 🔑 Prerequisites

### 1. Microsoft Entra ID (App Registration)
The migration scripts require an App Registration in Microsoft Entra with **Application Permissions** to query user metadata non-interactively:

1. In **Microsoft Entra Admin Center** (`https://entra.microsoft.com`):
   * Go to **Identity** > **Applications** > **App registrations** > **New registration**.
   * Name: `Tanzu-Entra-Migration-Tool`.
2. Under **Certificates & secrets**:
   * Create a **New client secret** and copy the **Value**.
3. Under **API permissions**:
   * Click **Add a permission** > **Microsoft Graph** > **Application permissions** (NOT Delegated).
   * Select **`User.Read.All`** > **Add permissions**.
   * Click **Grant admin consent for <Your-Tenant>** (must display a green checkmark `✓ Granted`).

---

## 🚀 Tool 1: `cf-management` GitOps YAML Transformer

**Best for**: Organizations using [cf-management](https://github.com/cloudfoundry-community/cf-management) to manage platform orgs and spaces declaratively via Git.

### Features
* Recursively scans your `cf-management` Git repository.
* Automatically recognizes all role blocks (`space-developer`, `space-manager`, `space-auditor`, `space-supporter`, `org-manager`, `org-auditor`, `billing-manager`, etc.).
* Scans all candidate user lists: **`users`**, **`ldap_users`**, and **`saml_users`**.
* **Zero Access Loss Guarantee**: Translates mapped users to their target identity (default: **onPremisesSamAccountName / sAMAccountName** or optionally **UserPrincipalName / UPN** with `--use-upn`), and **maintains unmapped users** in `saml_users` so nobody is accidentally locked out.
* Cleans up legacy `users:` and `ldap_users:` fields once consolidated into `saml_users:`.
* **Automatic Backups**: Generates `.bak` backup copies of original files before in-place modification in `--live` mode.
* Generates side-by-side preview files (`.preview.yml`) in Dry-Run mode.
* Built-in diagnostic inspection tools (`--dump-users`, `--search-user`).
* Exports audit CSV reports (`yaml_mapped_users.csv` and `yaml_unmapped_users.csv`).

### Usage

```bash
# 1. Export Microsoft Entra Credentials
export ENTRA_TENANT_ID="<YOUR_TENANT_ID>"
export ENTRA_CLIENT_ID="<YOUR_CLIENT_ID>"
export ENTRA_CLIENT_SECRET="<YOUR_CLIENT_SECRET>"
export ENTRA_IDENTITY_TYPE="samaccountname"   # 'samaccountname' (default), 'upn', or 'email'

# 2. Run in Dry-Run Preview Mode (Generates .preview.yml files & CSV reports)
# Defaults to onPremisesSamAccountName
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config

# To use UserPrincipalName (UPN) instead:
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --use-upn

# 3. Review the preview files and CSV reports
cat /path/to/cf-management-config/config/org/space/spaceConfig.yml.preview.yml
column -t -s, yaml_mapped_users.csv
column -t -s, yaml_unmapped_users.csv

# 4. Apply In-Place Live (Creates .bak backups automatically)
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --live

# 5. Target Identity Options (sAMAccountName vs UPN vs Email)
# Default target identity is onPremisesSamAccountName:
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --identity-type samaccountname
# To target UPN (UserPrincipalName):
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --use-upn
# To target Mail instead:
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --identity-type email

# 6. Caching Options (Optimized for large enterprise tenants with 50k - 100k+ users)
# By default, Entra ID users are cached locally in .entra_users_cache.json for 4.0 hours
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config

# Force refresh the cache from Microsoft Graph
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --refresh-cache

# Custom cache TTL (e.g. 8 hours) or disable cache
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --cache-ttl 8.0
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --no-cache

# 7. Diagnostic / Verification Commands (Optional)
# Dump all Entra users to JSON/CSV and print attribute breakdown
python3 migrate_cf_management_yaml.py --dump-only

# Search specific user attributes in Microsoft Graph
python3 migrate_cf_management_yaml.py --search-user "jsmith" --dump-only
```

---

## ⚡ Tool 2: Direct Foundation Role Migration

**Best for**: Direct execution against the live Cloud Foundry platform via CF CLI / API.

Available in both **Python** (`migrate_ldap_to_entra.py`) and **PowerShell** (`migrate_ldap_to_entra.ps1`).

### Python Version (`migrate_ldap_to_entra.py`)

```bash
# 1. Log in to Cloud Foundry CLI as admin
cf login -a https://api.sys.example.com -u admin -o system -s system

# 2. Export Entra Credentials & Configuration
export ENTRA_TENANT_ID="<YOUR_TENANT_ID>"
export ENTRA_CLIENT_ID="<YOUR_CLIENT_ID>"
export ENTRA_CLIENT_SECRET="<YOUR_CLIENT_SECRET>"
export NEW_ORIGIN="EntraSAML"             # (or your custom provider origin key)
export ENTRA_IDENTITY_TYPE="samaccountname" # 'samaccountname' (default), 'upn', or 'email'

# 3. Run Dry-Run Preview (defaults to onPremisesSamAccountName)
python3 migrate_ldap_to_entra.py

# To run with UPN (UserPrincipalName):
python3 migrate_ldap_to_entra.py --use-upn

# 4. Review Reports
column -t -s, migration_plan.csv
cat unmapped_ldap_users.csv

# 5. Execute Live Migration
python3 migrate_ldap_to_entra.py --live
```

### PowerShell Version (`migrate_ldap_to_entra.ps1`)

```powershell
# 1. Connect to Microsoft Graph
Connect-MgGraph -Scopes "User.Read.All"

# 2. Log in to Cloud Foundry
cf login -a https://api.sys.example.com

# 3. Run Dry-Run Preview (defaults to samaccountname)
./migrate_ldap_to_entra.ps1 -DryRun -NewOrigin "EntraSAML"

# Or with UPN:
./migrate_ldap_to_entra.ps1 -DryRun -NewOrigin "EntraSAML" -UseUPN

# 4. Execute Live Migration
./migrate_ldap_to_entra.ps1 -DryRun:$false -NewOrigin "EntraSAML"
```

---

## 📊 Audit & Reconciliation Reports

Both tools generate standardized CSV reports for compliance and auditing:

### `yaml_mapped_users.csv` / `migration_plan.csv`
| file | role | original_user | entra_user |
| :--- | :--- | :--- | :--- |
| `config/sample-org/dev-space/spaceConfig.yml` | `space-developer` | `jsmith` | `jsmith@company.com` |
| `config/sample-org/orgConfig.yml` | `org-manager` | `adeveloper` | `alice.dev@company.com` |

### `yaml_unmapped_users.csv` / `unmapped_ldap_users.csv`
Identifies orphaned accounts (e.g., users who left the company or exist only in legacy directories):
| file | role | unmapped_user |
| :--- | :--- | :--- |
| `config/sample-org/dev-space/spaceConfig.yml` | `space-auditor` | `oldemployee` |

---

## 💡 Identity Matching Rules & Logic

The tools use a multi-tier matching strategy to map legacy usernames to modern Entra ID identities:

1. **Tier 1: On-Premises sAMAccountName (`onPremisesSamAccountName`)**:
   * Exact match against Windows Active Directory usernames synced to Entra ID via Azure AD Connect / Entra Connect.
2. **Tier 2: Mail Nickname (`mailNickname`)**:
   * Matches against the user's Exchange alias / nickname in Entra ID.
3. **Tier 3: Email Address & Prefix (`mail`)**:
   * Matches against both the full primary email address and the username prefix before the `@` sign.
4. **Tier 4: UserPrincipalName & Prefix (`userPrincipalName`)**:
   * Matches against the full UPN and the prefix before `@`.
5. **Guest & External Accounts**:
   * Automatically strips and normalizes guest accounts (e.g. `external_user_company#EXT#@domain.onmicrosoft.com` ➡️ `external_user`).

---

## 🛡️ License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
