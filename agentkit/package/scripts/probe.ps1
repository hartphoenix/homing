<#
probe.ps1 -- homing-agent-kit environment probe for Windows.

Read-only. Mutates nothing it does not remove again. Never prints a secret value.
Emits exactly one JSON object on stdout, with the same schema as probe.sh;
diagnostics go to stderr. Read references/probe.md before interpreting it.

Every capability is have | denied | absent, never a boolean, and every failure
carries a `signature` naming which refusal layer produced it.
#>

[CmdletBinding()]
param(
    [switch] $NoNetwork,
    [string[]] $Target = @(),
    [int] $Timeout = 4,
    [switch] $Help,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Rest = @()
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

# Accept the POSIX spellings too, so one instruction works on every platform.
$i = 0
while ($i -lt $Rest.Count) {
    switch ($Rest[$i]) {
        '--help'       { $Help = $true }
        '-h'           { $Help = $true }
        '--no-network' { $NoNetwork = $true }
        '--target'     { $i++; if ($i -lt $Rest.Count) { $Target += $Rest[$i] } }
        '--timeout'    { $i++; if ($i -lt $Rest.Count) { $Timeout = [int]$Rest[$i] } }
        default        { [Console]::Error.WriteLine("probe.ps1: unknown option $($Rest[$i]) (try --help)") ; exit 2 }
    }
    $i++
}

if ($Help) {
@'
probe.ps1 -- one read-only look at this machine, as one JSON object on stdout.

Usage:
  powershell -NoProfile -ExecutionPolicy Bypass -File probe.ps1 [options]

Options:
  --no-network / -NoNetwork   Skip every HTTP request. network.verdict is "skipped".
  --target URL / -Target      Probe one extra https URL. Repeatable.
  --timeout N  / -Timeout     Per-request timeout in seconds (default 4).
  --help / -h                 This text.

Guarantees:
  * HTTP only. No DNS, no ping, no raw sockets, and the ambient proxy is never
    stripped -- stripping it manufactures a name-resolution failure.
  * No secret value is ever printed. Sensitive variables report presence only,
    and URL userinfo is stripped before anything else.
  * Writability is measured by touch-probe and the probe file is removed again.
  * Nothing aborts the run: a step that fails records an entry in `errors`.

Typical runtime is 3-10 seconds. Exit status is 0 whenever JSON was written.
'@
    exit 0
}

$Schema        = 1
$ControlUrl    = 'https://example.com'
$HomingOrigin  = '__HOMING_ORIGIN__'
$EgressUrl     = 'https://ifconfig.co/json'
$HonestUa      = "HomingAgent/1.0 (+$HomingOrigin/agent/; user-directed housing search for one person)"
$ProbeTag      = ".homing-wprobe-$PID"
$StartedAt     = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$StartTicks    = [DateTime]::UtcNow

$Errors = New-Object System.Collections.ArrayList
function Note-Error($step, $signature, $detail) {
    [void]$Errors.Add([ordered]@{ step = $step; signature = $signature; detail = $detail })
}

# The four refusal signatures. Collapsing `denied` into `absent` is the most
# expensive misdiagnosis in this whole probe.
function Get-Signature($text) {
    if (-not $text) { return 'none' }
    $t = [string]$text
    if ($t -match 'Permission to use|has been denied|requires approval') { return 'harness_deny' }
    if ($t -match 'operation not permitted')                            { return 'sandbox_deny' }
    if ($t -match 'UnauthorizedAccess|Access is denied|Access to the path') { return 'os_eperm' }
    if ($t -match 'not recognized|CommandNotFound|cannot find the path')    { return 'not_found' }
    return 'unknown'
}

function Redact($value) {
    if ($null -eq $value) { return '' }
    # URL userinfo first: live proxy credentials hide there and no KEY|TOKEN
    # pattern catches them.
    $v = [regex]::Replace([string]$value, '://[^:/@]*:[^@]*@', '://<redacted>@')
    if ($v.Length -gt 120) { $v = $v.Substring(0, 120) }
    return $v
}

# --------------------------------------------------------------------------
# host
# --------------------------------------------------------------------------

$osVersion = 'unknown'
try { $osVersion = (Get-CimInstance Win32_OperatingSystem).Caption } catch { }
if (-not $osVersion -or $osVersion -eq 'unknown') {
    try { $osVersion = [System.Environment]::OSVersion.VersionString } catch { $osVersion = 'unknown' }
}

$arch = $env:PROCESSOR_ARCHITECTURE
if (-not $arch) { $arch = 'unknown' }

$container = 'none'
if ($env:CONTAINER) { $container = [string]$env:CONTAINER }
if (Get-Service -Name 'cexecsvc' -ErrorAction SilentlyContinue) { $container = 'windows-container' }

$guiSession = 'unknown'
try { $guiSession = if ([System.Environment]::UserInteractive) { 'have' } else { 'absent' } } catch { }

$homeDir = $env:USERPROFILE
if (-not $homeDir) { $homeDir = $HOME }
if (-not $homeDir) { $homeDir = (Get-Location).Path }

$hostnameClass = 'unknown'
if ($container -ne 'none')                     { $hostnameClass = 'container' }
elseif ($env:CI -or $env:GITHUB_ACTIONS)       { $hostnameClass = 'ci' }
elseif ($guiSession -eq 'have')                { $hostnameClass = 'personal' }
elseif ($guiSession -eq 'absent')              { $hostnameClass = 'server' }

$ttyState = 'absent'
try { if (-not [Console]::IsOutputRedirected) { $ttyState = 'have' } } catch { }

$hostBlock = [ordered]@{
    os             = 'windows'
    os_raw         = 'Windows_NT'
    version        = [string]$osVersion
    arch           = [string]$arch
    container      = $container
    hostname_class = $hostnameClass
    gui_session    = $guiSession
    home           = [string]$homeDir
    user           = [string]$env:USERNAME
    shell          = 'powershell'
}

# --------------------------------------------------------------------------
# runtime identity -- environment names first, then config dirs.
# Never process ancestry: it cannot see past a PATH shim and dies under sandboxes.
# --------------------------------------------------------------------------

$envNames = @()
try { $envNames = @(Get-ChildItem Env: | ForEach-Object { $_.Name }) } catch {
    Note-Error 'env_enumerate' (Get-Signature $_.Exception.Message) 'could not list environment variable names'
}
function Env-Has($name) { return ($envNames -contains $name) }
function Env-Val($name) { return [System.Environment]::GetEnvironmentVariable($name) }

# Values are emitted only for these names, and only after redaction.
$safeEnv = @('CLAUDECODE','CLAUDE_CODE_ENTRYPOINT','AI_AGENT','SANDBOX_RUNTIME',
             'CODEX_SANDBOX','CI','GITHUB_ACTIONS','CONTAINER')
# These report presence only. Their values never appear anywhere.
$signalEnv = @('CLAUDECODE','CLAUDE_CODE_ENTRYPOINT','CLAUDE_CODE_EXECPATH',
               'CLAUDE_CODE_CHILD_SESSION','AI_AGENT','SANDBOX_RUNTIME','ANTHROPIC_API_KEY',
               'CODEX_HOME','CODEX_SANDBOX','OPENAI_API_KEY','GEMINI_API_KEY','GOOGLE_API_KEY',
               'CURSOR_TRACE_ID','CURSOR_AGENT','WINDSURF_SESSION','AIDER_MODEL','GH_TOKEN',
               'GITHUB_TOKEN','OLLAMA_HOST','CI','GITHUB_ACTIONS',
               'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')

$signals = New-Object System.Collections.ArrayList
foreach ($n in $signalEnv) {
    if (Env-Has $n) {
        if ($safeEnv -contains $n) {
            [void]$signals.Add([ordered]@{ name = $n; state = 'set'; value = (Redact (Env-Val $n)) })
        } else {
            [void]$signals.Add([ordered]@{ name = $n; state = 'set' })
        }
    }
}

$dirMarkers = @('.claude','.codex','.gemini','.cursor','.windsurf','.continue','.copilot',
                '.ollama','.lmstudio','.crush','.roo','.cline','.zed')
$configDirs = New-Object System.Collections.ArrayList
foreach ($d in $dirMarkers) {
    if (Test-Path -LiteralPath (Join-Path $homeDir $d)) { [void]$configDirs.Add($d) }
}

$rtId = 'unknown'; $rtConf = 'none'; $rtVersion = ''
if     ((Env-Has 'CLAUDECODE') -or (Env-Has 'CLAUDE_CODE_ENTRYPOINT')) { $rtId = 'claude-code'; $rtConf = 'high' }
elseif ((Env-Has 'CODEX_HOME') -or (Env-Has 'CODEX_SANDBOX'))          { $rtId = 'codex';       $rtConf = 'high' }
elseif ((Env-Has 'CURSOR_AGENT') -or (Env-Has 'CURSOR_TRACE_ID'))      { $rtId = 'cursor';      $rtConf = 'high' }
elseif ((Env-Has 'GEMINI_API_KEY') -and ($configDirs -contains '.gemini')) { $rtId = 'gemini-cli'; $rtConf = 'low' }
if (Env-Has 'AI_AGENT') { $rtVersion = Redact (Env-Val 'AI_AGENT'); if ($rtConf -eq 'none') { $rtConf = 'low' } }

$candidateMap = @{ '.claude' = 'claude-code'; '.codex' = 'codex'; '.gemini' = 'gemini-cli';
                   '.cursor' = 'cursor'; '.windsurf' = 'windsurf'; '.copilot' = 'copilot-cli';
                   '.cline' = 'cline'; '.roo' = 'roo' }
$candidates = New-Object System.Collections.ArrayList
foreach ($d in $configDirs) { if ($candidateMap.ContainsKey($d)) { [void]$candidates.Add($candidateMap[$d]) } }
if ($rtId -eq 'unknown') {
    if ($candidates.Count -gt 0) { $rtConf = 'low' }
    $rtId = 'generic'
}

$sandboxSignals = New-Object System.Collections.ArrayList
$sandboxDetected = $false
if (Env-Has 'SANDBOX_RUNTIME') { $sandboxDetected = $true; [void]$sandboxSignals.Add('env:SANDBOX_RUNTIME') }
if (Env-Has 'CODEX_SANDBOX')   { $sandboxDetected = $true; [void]$sandboxSignals.Add('env:CODEX_SANDBOX') }
if ($container -ne 'none')     { $sandboxDetected = $true; [void]$sandboxSignals.Add("container:$container") }

$runtimeBlock = [ordered]@{
    id          = $rtId
    version     = $rtVersion
    confidence  = $rtConf
    candidates  = @($candidates)
    signals     = @($signals)
    config_dirs = @($configDirs)
    tty         = $ttyState
    sandbox     = [ordered]@{ detected = $sandboxDetected; signals = @($sandboxSignals) }
}

# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

$toolNames = @('python','python3','py','curl','curl.exe','git','jq','node','npm','npx','bun',
               'deno','uv','uvx','pipx','pip','winget','choco','scoop','perl','unzip','tar',
               'openssl','sqlite3','cmdkey','schtasks','powershell','pwsh','docker','gh',
               'claude','codex','gemini','cursor','ollama')

$tools = New-Object System.Collections.ArrayList
foreach ($t in $toolNames) {
    $cmd = Get-Command -Name $t -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) {
        $p = $cmd.Source
        if (-not $p) { $p = $cmd.Name }
        $entry = [ordered]@{ name = $t; state = 'have'; path = [string]$p }
        if ($cmd.CommandType -ne 'Application') { $entry.signature = 'shell_builtin_or_alias' }
        [void]$tools.Add($entry)
    } else {
        [void]$tools.Add([ordered]@{ name = $t; state = 'absent'; signature = 'not_found' })
    }
}

# --------------------------------------------------------------------------
# paths -- touch-probe every candidate, then clean up after itself.
# --------------------------------------------------------------------------

function Test-Synced($p) {
    return ($p -match 'OneDrive|Dropbox|Google Drive|Syncthing|iCloud|pCloud')
}

$localAppData = $env:LOCALAPPDATA
if (-not $localAppData) { $localAppData = Join-Path $homeDir 'AppData\Local' }
$cfgDefault   = Join-Path $localAppData 'Homing'
$stateDefault = Join-Path $cfgDefault  'state'
$logDefault   = Join-Path $cfgDefault  'logs'

$paths = New-Object System.Collections.ArrayList
$anyWritable = $false

function Probe-Path($class, $p) {
    $exists = $false; $writable = $false; $state = 'absent'; $sig = 'none'; $real = $p
    try {
        if (Test-Path -LiteralPath $p -PathType Container) {
            $exists = $true
            try { $real = (Resolve-Path -LiteralPath $p).Path } catch { }
            $probe = Join-Path $p $script:ProbeTag
            try {
                New-Item -ItemType File -Path $probe -Force -ErrorAction Stop | Out-Null
                $writable = $true; $state = 'writable'
                Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
            } catch {
                $state = 'denied'; $sig = Get-Signature $_.Exception.Message
            }
        } elseif (Test-Path -LiteralPath $p) {
            $exists = $true; $state = 'denied'; $sig = 'not_a_directory'
        } else {
            $anc = $p
            while ($anc -and -not (Test-Path -LiteralPath $anc -PathType Container)) {
                $parent = Split-Path -Parent $anc
                if (-not $parent -or $parent -eq $anc) { break }
                $anc = $parent
            }
            if ($anc -and (Test-Path -LiteralPath $anc -PathType Container)) {
                $probe = Join-Path $anc $script:ProbeTag
                try {
                    New-Item -ItemType Directory -Path $probe -ErrorAction Stop | Out-Null
                    $writable = $true; $state = 'creatable'
                    Remove-Item -LiteralPath $probe -Force -Recurse -ErrorAction SilentlyContinue
                } catch {
                    $state = 'denied'; $sig = Get-Signature $_.Exception.Message
                }
            }
        }
    } catch {
        $state = 'denied'; $sig = Get-Signature $_.Exception.Message
    }
    if ($writable) { $script:anyWritable = $true }
    [void]$script:paths.Add([ordered]@{
        class = $class; path = [string]$p; exists = $exists; writable = $writable
        state = $state; signature = $sig; synced = [bool](Test-Synced $real)
    })
}

Probe-Path 'skill'     (Join-Path $homeDir '.agents\skills')
Probe-Path 'skill'     (Join-Path $homeDir '.claude\skills')
Probe-Path 'skill'     (Join-Path $homeDir '.codex\skills')
Probe-Path 'skill'     (Join-Path $homeDir '.gemini\skills')
Probe-Path 'skill'     (Join-Path (Get-Location).Path '.claude\skills')
Probe-Path 'scheduler' $cfgDefault
Probe-Path 'config'    $cfgDefault
Probe-Path 'state'     $stateDefault
Probe-Path 'logs'      $logDefault

$capFileWrite = if ($anyWritable) { 'have' } else { 'denied' }
$capWebFetch  = 'have'   # Invoke-WebRequest is built in; reachability is measured below.

# --------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------

$scheduler = New-Object System.Collections.ArrayList
$schedState = 'absent'; $schedNote = ''
if (Get-Command -Name 'Register-ScheduledTask' -ErrorAction SilentlyContinue) {
    $schedState = 'have'
    $schedNote  = 'Register-ScheduledTask: S4U, RunLevel Limited, MultipleInstances IgnoreNew, ExecutionTimeLimit'
} elseif (Get-Command -Name 'schtasks' -ErrorAction SilentlyContinue) {
    $schedState = 'have'
    $schedNote  = 'schtasks.exe fallback; /np for S4U, /rl LIMITED'
}
[void]$scheduler.Add([ordered]@{
    kind = 'schtasks'; state = $schedState; dir = ''; dir_writable = $true
    durable = $true; note = $schedNote
})

# --------------------------------------------------------------------------
# secret_store -- presence only. Never read a value.
# --------------------------------------------------------------------------

$secretStore = New-Object System.Collections.ArrayList
[void]$secretStore.Add([ordered]@{
    kind = 'windows-dpapi'; binary = 'ConvertFrom-SecureString'; state = 'have'
    note = 'DPAPI is keyed to this user on this machine; an S4U task has no DPAPI user key, so pair it with a store that survives S4U or say the job runs while signed in'
})
if (Get-Command -Name 'cmdkey' -ErrorAction SilentlyContinue) {
    [void]$secretStore.Add([ordered]@{ kind = 'credential-manager'; binary = 'cmdkey'; state = 'have'; note = '' })
}
if (Get-Command -Name 'op' -ErrorAction SilentlyContinue) {
    [void]$secretStore.Add([ordered]@{ kind = '1password'; binary = 'op'; state = 'have'
        note = 'only when already signed in and unattended access is already configured' })
}
[void]$secretStore.Add([ordered]@{ kind = 'file-0600'; binary = ''; state = 'have'
    note = 'always available; an ACL-restricted file outside every synced folder' })

# --------------------------------------------------------------------------
# mcp -- every runtime's config found, not just this one.
# --------------------------------------------------------------------------

$mcp = New-Object System.Collections.ArrayList
$ccJson = Join-Path $homeDir '.claude.json'
if (Test-Path -LiteralPath $ccJson) {
    try {
        $d = Get-Content -LiteralPath $ccJson -Raw | ConvertFrom-Json
        $names = New-Object System.Collections.Generic.HashSet[string]
        if ($d.mcpServers) { foreach ($k in $d.mcpServers.PSObject.Properties.Name) { [void]$names.Add($k) } }
        if ($d.projects)   { foreach ($pr in $d.projects.PSObject.Properties.Value) {
                                if ($pr.mcpServers) { foreach ($k in $pr.mcpServers.PSObject.Properties.Name) { [void]$names.Add($k) } } } }
        foreach ($n in $names) { [void]$mcp.Add([ordered]@{ runtime = 'claude-code'; server = $n; transport = 'unknown'; config = $ccJson }) }
    } catch { Note-Error 'mcp_claude' (Get-Signature $_.Exception.Message) 'could not parse .claude.json' }
}
$cxToml = Join-Path $homeDir '.codex\config.toml'
if (Test-Path -LiteralPath $cxToml) {
    try {
        Select-String -LiteralPath $cxToml -Pattern '^\[mcp_servers\.(.+)\]' | ForEach-Object {
            [void]$mcp.Add([ordered]@{ runtime = 'codex'; server = $_.Matches[0].Groups[1].Value; transport = 'stdio'; config = $cxToml })
        }
    } catch { }
}

# --------------------------------------------------------------------------
# network -- HTTP only, always paired with the control URL, proxy never stripped.
# --------------------------------------------------------------------------

$proxyVars = New-Object System.Collections.ArrayList
$proxyEndpoint = ''
foreach ($pv in @('HTTPS_PROXY','https_proxy','HTTP_PROXY','http_proxy','ALL_PROXY','all_proxy')) {
    if (Env-Has $pv) {
        [void]$proxyVars.Add($pv)
        if (-not $proxyEndpoint) { $proxyEndpoint = Redact (Env-Val $pv) }
    }
}
$proxyState = if ($proxyVars.Count -gt 0) { 'have' } else { 'absent' }

function Get-HttpStatus($url) {
    $code = '000'; $sig = 'none'
    try {
        $r = Invoke-WebRequest -Uri $url -Method Get -UseBasicParsing -MaximumRedirection 3 `
                -TimeoutSec $script:Timeout -UserAgent $script:HonestUa -ErrorAction Stop
        $code = [string][int]$r.StatusCode
    } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode) { $code = [string][int]$resp.StatusCode }
        else { $sig = Get-Signature $_.Exception.Message }
    }
    return @{ code = $code; signature = $sig }
}

$targets      = New-Object System.Collections.ArrayList
$controlCode  = '000'; $controlSig = 'skipped'
$homingCode   = '000'; $homingSig  = 'skipped'; $homingReachable = $false
$egressClass  = 'unknown'; $egressOrg = ''; $egressCity = ''; $egressCountry = ''
$netVerdict   = 'skipped'

if (-not $NoNetwork) {
    $c = Get-HttpStatus $ControlUrl
    $controlCode = $c.code; $controlSig = $c.signature

    $homingUrl = "$HomingOrigin/api/v1/me/projects"
    $h = Get-HttpStatus $homingUrl
    $homingCode = $h.code; $homingSig = $h.signature
    if ($homingCode -match '^(2\d\d|3\d\d|401|403)$') { $homingReachable = $true }
    [void]$targets.Add([ordered]@{ url = $homingUrl; http = $homingCode; signature = $homingSig; role = 'homing' })

    foreach ($u in $Target) {
        $t = Get-HttpStatus $u
        [void]$targets.Add([ordered]@{ url = $u; http = $t.code; signature = $t.signature; role = 'extra' })
    }

    # Egress class is a politeness input, never a permission slip.
    try {
        $j = Invoke-RestMethod -Uri $EgressUrl -TimeoutSec $Timeout -ErrorAction Stop
        $egressOrg = [string]$j.asn_org; $egressCity = [string]$j.city; $egressCountry = [string]$j.country
    } catch { }
    if (-not $egressOrg) { $egressClass = 'unknown' }
    elseif ($egressOrg -match 'Amazon|AWS|Google|Microsoft|Azure|Hetzner|DigitalOcean|OVH|Linode|Akamai|Cloudflare|Oracle|Vultr|Scaleway|Contabo|Alibaba|Tencent|Equinix|Leaseweb|M247|Choopa|Datacamp|Hosting|Datacenter') { $egressClass = 'datacenter' }
    elseif ($egressCity) { $egressClass = 'residential' }

    # Name the layer. This is the single most misread signal in the whole probe.
    if ($controlSig -eq 'harness_deny' -or $controlSig -eq 'sandbox_deny') { $netVerdict = 'denied' }
    elseif ($controlCode -eq '000' -and $homingCode -eq '000')             { $netVerdict = 'no_egress' }
    elseif ($controlCode -ne '000' -and $homingCode -eq '000')             { $netVerdict = 'homing_unreachable' }
    elseif ($homingReachable)                                              { $netVerdict = 'ok' }
    else                                                                   { $netVerdict = 'homing_error' }
}

$networkBlock = [ordered]@{
    control = [ordered]@{ url = $ControlUrl; http = $controlCode; signature = $controlSig }
    targets = @($targets)
    egress  = [ordered]@{ class = $egressClass; asn_org = $egressOrg; city = $egressCity; country = $egressCountry }
    proxy   = [ordered]@{ state = $proxyState; vars = @($proxyVars); endpoint = $proxyEndpoint
                          no_proxy_set = [bool]((Env-Has 'NO_PROXY') -or (Env-Has 'no_proxy')) }
    client  = 'invoke-webrequest'
    verdict = $netVerdict
}

$homingBlock = [ordered]@{
    origin = $HomingOrigin; http = $homingCode; reachable = $homingReachable; signature = $homingSig
}

# --------------------------------------------------------------------------
# prior_install -- the phase that collapses the question count.
# --------------------------------------------------------------------------

$piFound = $false
$piSched = New-Object System.Collections.ArrayList
$piState = New-Object System.Collections.ArrayList
$piLock  = New-Object System.Collections.ArrayList
$piSecret = 'unknown'
$piLastRun = ''
$piManifest = ''

foreach ($sd in @($cfgDefault, $stateDefault, $logDefault)) {
    if (Test-Path -LiteralPath $sd -PathType Container) { $piFound = $true; [void]$piState.Add($sd) }
}
foreach ($mf in @((Join-Path $stateDefault 'install-manifest.json'), (Join-Path $cfgDefault 'install-manifest.json'))) {
    if ((Test-Path -LiteralPath $mf) -and -not $piManifest) { $piFound = $true; $piManifest = $mf }
}
$lastRunPath = Join-Path $stateDefault 'last-run.json'
if (Test-Path -LiteralPath $lastRunPath) {
    try { $piLastRun = [string](Get-Content -LiteralPath $lastRunPath -Raw | ConvertFrom-Json).at } catch { }
}

# A lock older than twice the expected run time is stale, and a job that exits 0
# on a stale lock looks healthy while doing nothing at all.
foreach ($lk in @((Join-Path $stateDefault 'run.lock'), (Join-Path $cfgDefault 'run.lock'))) {
    if (Test-Path -LiteralPath $lk) {
        $piFound = $true
        $stale = $false
        try { $stale = ((Get-Item -LiteralPath $lk).LastWriteTimeUtc -lt [DateTime]::UtcNow.AddMinutes(-120)) } catch { }
        [void]$piLock.Add([ordered]@{ path = $lk; stale_suspect = $stale })
    }
}

try {
    $tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match 'Homing' -or $_.TaskPath -match 'Homing' }
    foreach ($t in $tasks) {
        $piFound = $true
        [void]$piSched.Add([ordered]@{ kind = 'schtasks'; label = "$($t.TaskPath)$($t.TaskName)"
                                       path = ''; source = 'task-scheduler'; state = [string]$t.State })
    }
} catch { Note-Error 'prior_scheduler' (Get-Signature $_.Exception.Message) 'could not enumerate scheduled tasks' }

# Presence only, never a value. `cmdkey /list` prints target names, not secrets.
if (Get-Command -Name 'cmdkey' -ErrorAction SilentlyContinue) {
    try {
        $out = & cmdkey /list 2>&1 | Out-String
        $piSecret = if ($out -match 'homing') { 'present' } else { 'absent' }
    } catch { $piSecret = 'denied' }
}
$dpapiFile = Join-Path $cfgDefault 'token.dpapi'
if (Test-Path -LiteralPath $dpapiFile) { $piSecret = 'present'; $piFound = $true }

$priorBlock = [ordered]@{
    found = $piFound; scheduler_records = @($piSched); state_dirs = @($piState)
    secret_item = $piSecret; last_run_at = $piLastRun; lock = @($piLock); manifest = $piManifest
}

# --------------------------------------------------------------------------
# isolation -- evidence for the rung, never a claim beyond the evidence.
# --------------------------------------------------------------------------

$isoRung = 0
$isoEvidence = New-Object System.Collections.ArrayList
if ($sandboxDetected) { $isoRung = 1; [void]$isoEvidence.Add('sandbox signals present') }
$ccSettings = Join-Path $homeDir '.claude\settings.json'
if (Test-Path -LiteralPath $ccSettings) {
    $s = Get-Content -LiteralPath $ccSettings -Raw
    if ($s -match '"allowedDomains"') {
        if ($isoRung -lt 3) { $isoRung = 3 }
        [void]$isoEvidence.Add('an egress allowlist is configured outside the model')
    }
    if ($s -match '"allowUnsandboxedCommands"\s*:\s*false') { [void]$isoEvidence.Add('unsandboxed commands are disabled') }
}
if ($container -ne 'none') {
    if ($isoRung -lt 5) { $isoRung = 5 }
    [void]$isoEvidence.Add("container: $container")
}
if ($proxyState -eq 'have') {
    [void]$isoEvidence.Add('egress is proxied; a proxy is an enforcement point only when the user controls it')
}

# --------------------------------------------------------------------------
# browser -- presence only, never launched.
# --------------------------------------------------------------------------

$browsers = New-Object System.Collections.ArrayList
$browserPaths = @(
    @{ n = 'Google Chrome';  p = 'C:\Program Files\Google\Chrome\Application\chrome.exe' },
    @{ n = 'Google Chrome';  p = 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe' },
    @{ n = 'Microsoft Edge'; p = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe' },
    @{ n = 'Firefox';        p = 'C:\Program Files\Mozilla Firefox\firefox.exe' }
)
foreach ($b in $browserPaths) {
    if ((Test-Path -LiteralPath $b.p) -and -not ($browsers -contains $b.n)) { [void]$browsers.Add($b.n) }
}

# --------------------------------------------------------------------------
# emit
# --------------------------------------------------------------------------

$duration = [int]([DateTime]::UtcNow - $StartTicks).TotalSeconds

$out = [ordered]@{
    schema           = $Schema
    generated_at     = $StartedAt
    duration_seconds = $duration
    host             = $hostBlock
    runtime          = $runtimeBlock
    capabilities     = [ordered]@{ shell = 'have'; file_write = $capFileWrite; web_fetch = $capWebFetch
                                   subprocess = 'have'; background = 'have' }
    tools            = @($tools)
    paths            = @($paths)
    scheduler        = @($scheduler)
    secret_store     = @($secretStore)
    mcp              = @($mcp)
    network          = $networkBlock
    homing           = $homingBlock
    prior_install    = $priorBlock
    isolation        = [ordered]@{ rung = $isoRung; evidence = @($isoEvidence) }
    browser          = @($browsers)
    errors           = @($Errors)
}

$out | ConvertTo-Json -Depth 12 -Compress
exit 0
