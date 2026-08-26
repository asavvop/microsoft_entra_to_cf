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
* Translates usernames across all role blocks (`developer`, `manager`, `auditor`, `billing-manager`, `supporter`).
* Moves mapped users from `users` / `ldap_users` to `saml_users`.
* Generates side-by-side preview files (`.preview.yml`) in Dry-Run mode.
* Exports audit CSV reports of all changes and unmapped accounts.

### Usage

```bash
# 1. Export Microsoft Entra Credentials
export ENTRA_TENANT_ID="<YOUR_TENANT_ID>"
export ENTRA_CLIENT_ID="<YOUR_CLIENT_ID>"
export ENTRA_CLIENT_SECRET="<YOUR_CLIENT_SECRET>"

# 2. Run in Dry-Run Preview Mode
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config

# 3. Review the preview files and CSV reports
cat /path/to/cf-management-config/org/space/spaceConfig.yml.preview.yml
column -t -s, yaml_migration_plan.csv

# 4. Apply In-Place Live
python3 migrate_cf_management_yaml.py --dir /path/to/cf-management-config --live
```

---

## ⚡ Tool 2: Direct Foundation Role Migration

**Best for**: Direct execution against the live Cloud Foundry platform via CF CLI / API.

Available in both **Python** (`migrate_ldap_to_entra.py`) and **PowerShell** (`migrate_ldap_to_entra.ps1`).

### Python Version (`migrate_ldap_to_entra.py`)

```bash
# 1. Log in to Cloud Foundry CLI as admin
cf login -a https://api.sys.example.com -u admin -o system -s system

# 2. Export Entra Credentials
export ENTRA_TENANT_ID="<YOUR_TENANT_ID>"
export ENTRA_CLIENT_ID="<YOUR_CLIENT_ID>"
export ENTRA_CLIENT_SECRET="<YOUR_CLIENT_SECRET>"
export NEW_ORIGIN="EntraSAML"   # (or your custom provider origin key)

# 3. Run Dry-Run Preview
python3 migrate_ldap_to_entra.py

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

# 3. Run Dry-Run Preview
./migrate_ldap_to_entra.ps1 -DryRun -NewOrigin "EntraSAML"

# 4. Execute Live Migration
./migrate_ldap_to_entra.ps1 -DryRun:$false -NewOrigin "EntraSAML"
```

---

## 📊 Audit & Reconciliation Reports

Both tools generate standardized CSV reports for compliance and auditing:

### `migration_plan.csv`
| type | ldap_user | entra_user | org | space | role | command |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Space** | `jsmith` | `john.smith@company.com` | `production-org` | `payments-space` | `SpaceDeveloper` | `cf set-space-role ...` |
| **Org** | `adeveloper` | `alice.dev@company.com` | `production-org` | `-` | `OrgManager` | `cf set-org-role ...` |

### `unmapped_users.csv`
Identifies orphaned accounts (e.g., users who left the company and exist in legacy LDAP but no longer exist in Entra ID):
| file / user | role | ldap_user |
| :--- | :--- | :--- |
| `dev-space/spaceConfig.yml` | `auditor` | `legacy_contractor` |

---

## 💡 Identity Matching Logic

The tools use a multi-tier matching strategy:

1. **Primary Match (Enterprise Sync)**:
   * Matches `onPremisesSamAccountName` from Microsoft Graph directly against the LDAP username.
2. **Fallback Match (Cloud / Lab Accounts)**:
   * Matches against the UserPrincipalName / Email prefix (e.g. `jsmith` from `jsmith@company.com`).
3. **Guest / External Accounts**:
   * Normalizes Azure AD guest accounts (e.g. `external_user#EXT#@domain.com` ➡️ `external_user`).

---

## 🛡️ License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
