<#
.SYNOPSIS
    Automated Migration of Tanzu Space/Org Roles from LDAP to Entra ID (PowerShell).
.DESCRIPTION
    1. Builds lookup map from Microsoft Entra ID (onPremisesSamAccountName -> mail/userPrincipalName).
    2. Queries Cloud Foundry for all roles with origin 'ldap'.
    3. Translates and assigns new roles with origin 'EntraSAML' (or custom).
#>

param (
    [Parameter(Mandatory=$false)]
    [string]$NewOrigin = "EntraSAML",

    [Parameter(Mandatory=$false)]
    [ValidateSet("Email", "UPN")]
    [string]$EntraIdentityType = "Email",

    [Parameter(Mandatory=$false)]
    [switch]$DryRun = $true
)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Tanzu LDAP -> Entra ID User Role Migration Tool" -ForegroundColor Cyan
Write-Host "  Mode: $(if ($DryRun) {'DRY RUN (Preview Only)'} else {'LIVE EXECUTION'})" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

# ---------------------------------------------------------
# Step 1: Extract LDAP -> Entra Mapping from Microsoft Graph
# ---------------------------------------------------------
Write-Host "`n[1/3] Connecting to Entra ID to build user mapping table..." -ForegroundColor Green

try {
    $mgContext = Get-MgContext -ErrorAction SilentlyContinue
    if (-not $mgContext) {
        Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Gray
        Connect-MgGraph -Scopes "User.Read.All" -NoWelcome
    }

    $allEntraUsers = Get-MgUser -All -Property DisplayName, UserPrincipalName, Mail, OnPremisesSamAccountName
    
    $userMap = @{}
    foreach ($u in $allEntraUsers) {
        $targetIdentity = if ($EntraIdentityType -eq "Email" -and $u.Mail) { $u.Mail } else { $u.UserPrincipalName }

        if ($u.OnPremisesSamAccountName) {
            $ldapKey = $u.OnPremisesSamAccountName.ToLower().Trim()
            $userMap[$ldapKey] = $targetIdentity
        }
        
        # Fallback for cloud accounts
        if ($u.UserPrincipalName -match '@') {
            $prefix = ($u.UserPrincipalName -split '@')[0].ToLower().Trim()
            $cleanPrefix = ($prefix -split '_')[0]
            $userMap[$prefix] = $targetIdentity
            $userMap[$cleanPrefix] = $targetIdentity
        }
    }
    Write-Host "  -> Successfully mapped $($userMap.Count) LDAP lookup keys to Entra IDs." -ForegroundColor Gray
} catch {
    Write-Error "Failed to query Entra ID. Please ensure 'Connect-MgGraph' is available."
    return
}

# ---------------------------------------------------------
# Step 2: Fetch Current Tanzu Roles via Cloud Foundry API
# ---------------------------------------------------------
Write-Host "`n[2/3] Querying Cloud Foundry for active role assignments..." -ForegroundColor Green

$rolesJson = cf curl "/v3/roles?per_page=5000" | ConvertFrom-Json
$usersJson = cf curl "/v2/users?results-per-page=100" | ConvertFrom-Json
$spacesJson = cf curl "/v3/spaces?per_page=5000" | ConvertFrom-Json
$orgsJson = cf curl "/v3/organizations?per_page=5000" | ConvertFrom-Json

# Build lookups
$userLookup = @{}
foreach ($u in $usersJson.resources) {
    $userLookup[$u.metadata.guid] = @{
        Username = $u.entity.username
        Origin   = $u.entity.origin
    }
}

$spaceLookup = @{}
foreach ($s in $spacesJson.resources) {
    $spaceLookup[$s.guid] = @{
        Name    = $s.name
        OrgGuid = $s.relationships.organization.data.guid
    }
}

$orgLookup = @{}
foreach ($o in $orgsJson.resources) {
    $orgLookup[$o.guid] = $o.name
}

# ---------------------------------------------------------
# Step 3: Match & Migrate
# ---------------------------------------------------------
Write-Host "`n[3/3] Analyzing and processing role migrations..." -ForegroundColor Green

$migrationResults = @()
$unmappedUsers = @()

foreach ($role in $rolesJson.resources) {
    $userGuid = $role.relationships.user.data.guid
    $userInfo = $userLookup[$userGuid]

    # Only process LDAP users
    if ($userInfo -and $userInfo.Origin -eq "ldap") {
        $ldapUser = $userInfo.Username.ToLower().Trim()
        $roleType = $role.type

        # Find mapped Entra identity
        $entraTarget = $userMap[$ldapUser]

        if (-not $entraTarget) {
            $unmappedUsers += [PSCustomObject]@{
                LDAP_User = $ldapUser
                Role      = $roleType
            }
            continue
        }

        # Process Space Roles
        if ($role.relationships.space.data.guid) {
            $sGuid = $role.relationships.space.data.guid
            $spaceName = $spaceLookup[$sGuid].Name
            $orgName = $orgLookup[$spaceLookup[$sGuid].OrgGuid]

            $cliRole = switch ($roleType) {
                "space_developer" { "SpaceDeveloper" }
                "space_manager"   { "SpaceManager" }
                "space_auditor"   { "SpaceAuditor" }
                "space_supporter" { "SpaceSupporter" }
                Default           { $null }
            }

            if ($cliRole) {
                $cmd = "cf set-space-role `"$entraTarget`" `"$orgName`" `"$spaceName`" $cliRole --origin $NewOrigin"

                $migrationResults += [PSCustomObject]@{
                    Type         = "Space"
                    LDAP_User    = $ldapUser
                    Entra_User   = $entraTarget
                    Org          = $orgName
                    Space        = $spaceName
                    Role         = $cliRole
                    Command      = $cmd
                }

                if (-not $DryRun) {
                    Write-Host "Running: $cmd" -ForegroundColor Gray
                    Invoke-Expression $cmd
                }
            }
        }
        # Process Org Roles
        elseif ($role.relationships.organization.data.guid) {
            $oGuid = $role.relationships.organization.data.guid
            $orgName = $orgLookup[$oGuid]

            $cliRole = switch ($roleType) {
                "organization_manager"         { "OrgManager" }
                "organization_auditor"         { "OrgAuditor" }
                "organization_billing_manager" { "BillingManager" }
                Default                        { $null }
            }

            if ($cliRole) {
                $cmd = "cf set-org-role `"$entraTarget`" `"$orgName`" $cliRole --origin $NewOrigin"

                $migrationResults += [PSCustomObject]@{
                    Type         = "Org"
                    LDAP_User    = $ldapUser
                    Entra_User   = $entraTarget
                    Org          = $orgName
                    Space        = "-"
                    Role         = $cliRole
                    Command      = $cmd
                }

                if (-not $DryRun) {
                    Write-Host "Running: $cmd" -ForegroundColor Gray
                    Invoke-Expression $cmd
                }
            }
        }
    }
}

# ---------------------------------------------------------
# Export Summary Reports
# ---------------------------------------------------------
$migrationResults | Export-Csv -Path "./migration_plan.csv" -NoTypeInformation
Write-Host "`n📊 Migration plan exported to 'migration_plan.csv' ($($migrationResults.Count) total roles)." -ForegroundColor Cyan

if ($unmappedUsers.Count -gt 0) {
    $unmappedUsers | Export-Csv -Path "./unmapped_ldap_users.csv" -NoTypeInformation
    Write-Host "⚠️ Warning: $($unmappedUsers.Count) roles had LDAP users with no Entra match (saved to 'unmapped_ldap_users.csv')." -ForegroundColor Yellow
}

Write-Host "`n✅ Done!" -ForegroundColor Green
