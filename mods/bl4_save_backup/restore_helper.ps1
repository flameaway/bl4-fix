# BL4 Save Backups: applies a staged restore once the game has closed.
#
# Started hidden by the mod (restore.py) while the game runs. Waits until no Borderlands4 process
# is left (a normal quit saves first: we come after that), then for each <Steam ID> folder under
# -Root with a pending_restore.json:
#   1. claims the plan (renames it to .applying; a second helper finds nothing),
#   2. prepares every file in memory: "file" actions read the backup (SHA-1 checked), "bank"
#      actions open profile.sav, swap its bank lines for the old bank's and encrypt it again in
#      the game's own format (AES-256-ECB over zlib + u32 length + PKCS7; savefile.py has the same
#      splice in Python), then read their own output back,
#   3. if the game was started again meanwhile: puts the plan back and waits for that exit,
#   4. copies every save as it is now into <stamp>-before-restore/ (undo), writes the prepared
#      files, writes last_restore.json (the mod shows it at the next launch), removes the plan.
# Every write is temp file + rename, with the time set to now (Steam Cloud sees a new file).
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Process = 'Borderlands4',
    [int]$SettleSeconds = 2
)
$ErrorActionPreference = 'Stop'

function Test-Game { return [bool](Get-Process -Name $Process -ErrorAction SilentlyContinue) }

$Utf8 = New-Object System.Text.UTF8Encoding($false, $true)  # throws on bytes that aren't UTF-8

function New-Aes([byte[]]$Key) {
    $aes = [System.Security.Cryptography.Aes]::Create()
    $aes.Mode = [System.Security.Cryptography.CipherMode]::ECB
    $aes.Padding = [System.Security.Cryptography.PaddingMode]::None
    $aes.Key = $Key
    return $aes
}

function Get-Adler32([byte[]]$Data) {
    [uint32]$a = 1; [uint32]$b = 0
    $n = $Data.Length; $i = 0
    while ($i -lt $n) {
        $end = [Math]::Min($i + 5552, $n)
        for (; $i -lt $end; $i++) { $a += $Data[$i]; $b += $a }
        $a = $a % 65521; $b = $b % 65521
    }
    return [uint32](($b -shl 16) -bor $a)
}

function Read-Save([byte[]]$Raw, [byte[]]$Key) {
    if ($Raw.Length -eq 0 -or $Raw.Length % 16 -ne 0) { throw 'not a whole save (size)' }
    $aes = New-Aes $Key
    $plain = $aes.CreateDecryptor().TransformFinalBlock($Raw, 0, $Raw.Length)
    $pad = $plain[$plain.Length - 1]
    if ($pad -lt 1 -or $pad -gt 16) { throw 'not a whole save (padding)' }
    for ($i = 1; $i -le $pad; $i++) { if ($plain[$plain.Length - $i] -ne $pad) { throw 'not a whole save (padding)' } }
    $bodyLen = $plain.Length - $pad
    $length = [BitConverter]::ToUInt32($plain, $bodyLen - 4)
    $adler = ([uint32]$plain[$bodyLen - 8] -shl 24) -bor ([uint32]$plain[$bodyLen - 7] -shl 16) -bor ([uint32]$plain[$bodyLen - 6] -shl 8) -bor [uint32]$plain[$bodyLen - 5]
    $in = New-Object System.IO.MemoryStream(, $plain)
    $in.Position = 2  # the zlib header
    $inflate = New-Object System.IO.Compression.DeflateStream($in, [System.IO.Compression.CompressionMode]::Decompress)
    $out = New-Object System.IO.MemoryStream
    $inflate.CopyTo($out); $inflate.Dispose()
    $bytes = $out.ToArray()
    if ($bytes.Length -ne $length) { throw 'not a whole save (length)' }
    if ((Get-Adler32 $bytes) -ne $adler) { throw 'damaged (checksum)' }
    return $Utf8.GetString($bytes)
}

function Write-SaveBytes([string]$Text, [byte[]]$Key) {
    $bytes = $Utf8.GetBytes($Text)
    $ms = New-Object System.IO.MemoryStream
    $deflate = New-Object System.IO.Compression.DeflateStream($ms, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    $deflate.Write($bytes, 0, $bytes.Length); $deflate.Dispose()
    $body = New-Object System.Collections.Generic.List[byte]
    $body.Add(0x78); $body.Add(0x9C)
    $body.AddRange($ms.ToArray())
    $adler = Get-Adler32 $bytes
    $body.Add([byte](($adler -shr 24) -band 0xFF)); $body.Add([byte](($adler -shr 16) -band 0xFF))
    $body.Add([byte](($adler -shr 8) -band 0xFF)); $body.Add([byte]($adler -band 0xFF))
    $body.AddRange([BitConverter]::GetBytes([uint32]$bytes.Length))
    $pad = 16 - ($body.Count % 16)
    for ($i = 0; $i -lt $pad; $i++) { $body.Add([byte]$pad) }
    $arr = $body.ToArray()
    $aes = New-Aes $Key
    return , $aes.CreateEncryptor().TransformFinalBlock($arr, 0, $arr.Length)
}

# The keyed lines of a save, as savefile._entries reads them: list items sit at their parent's
# indent, so a "- " item is a "-" key one deeper than its dash and its inline key one deeper still
function Get-Entries([string]$Line) {
    $stripped = $Line.TrimStart(' ')
    if ($stripped.Length -eq 0) { return }
    $indent = $Line.Length - $stripped.Length
    if ($stripped.StartsWith('- ') -or $stripped -eq '-') {
        $rest = if ($stripped.Length -gt 2) { $stripped.Substring(2).Trim() } else { '' }
        $quoted = $rest.StartsWith("'") -or $rest.StartsWith('"')
        [pscustomobject]@{ Indent = $indent + 1; Key = '-' }
        $at = if ($quoted) { -1 } else { $rest.IndexOf(': ') }
        if ($at -ge 0) { [pscustomobject]@{ Indent = $indent + 2; Key = $rest.Substring(0, $at).Trim() } }
        elseif (-not $quoted -and $rest.EndsWith(':')) { [pscustomobject]@{ Indent = $indent + 2; Key = $rest.Substring(0, $rest.Length - 1).Trim() } }
        return
    }
    $at = $stripped.IndexOf(':')
    if ($at -ge 0) { [pscustomobject]@{ Indent = $indent; Key = $stripped.Substring(0, $at).Trim() } }
}

$BankPath = 'domains/local/shared/inventory/items/bank'

function Get-BankSpan([string[]]$Lines) {
    $stack = New-Object System.Collections.Generic.List[object]
    $start = -1; $startIndent = 0
    for ($i = 0; $i -lt $Lines.Length; $i++) {
        foreach ($e in @(Get-Entries $Lines[$i])) {
            if ($start -ge 0) {
                if ($e.Indent -le $startIndent) { return [pscustomobject]@{ Start = $start; End = $i } }
                continue
            }
            while ($stack.Count -gt 0 -and $stack[$stack.Count - 1].Indent -ge $e.Indent) { $stack.RemoveAt($stack.Count - 1) }
            $stack.Add($e)
            if ($stack.Count -eq 6 -and (($stack | ForEach-Object { $_.Key }) -join '/') -eq $BankPath) { $start = $i; $startIndent = $e.Indent }
        }
    }
    if ($start -ge 0) { return [pscustomobject]@{ Start = $start; End = $Lines.Length } }
    return $null
}

function Set-Bank([string]$Text, [string]$Block) {
    $lines = $Text.Split("`n")
    $span = Get-BankSpan $lines
    if ($null -eq $span) { throw 'no bank in the profile now' }
    $new = $Block.Split("`n")
    $want = $new[0].Length - $new[0].TrimStart(' ').Length
    $have = $lines[$span.Start].Length - $lines[$span.Start].TrimStart(' ').Length
    if ($want -ne $have) { throw 'the bank sits at another depth in the profile now' }
    $out = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $span.Start; $i++) { $out.Add($lines[$i]) }
    foreach ($line in $new) { $out.Add($line) }
    for ($i = $span.End; $i -lt $lines.Length; $i++) { $out.Add($lines[$i]) }
    return [string]::Join("`n", $out)
}

function Write-Atomic([string]$Path, [byte[]]$Bytes) {
    $tmp = $Path + '.bl4restore.tmp'
    for ($try = 0; ; $try++) {
        try {
            [System.IO.File]::WriteAllBytes($tmp, $Bytes)
            Move-Item -LiteralPath $tmp -Destination $Path -Force
            (Get-Item -LiteralPath $Path).LastWriteTime = Get-Date
            return
        } catch {
            if ($try -ge 20) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; throw }
            Start-Sleep -Milliseconds 500  # Steam (cloud sync) or a virus scanner holding the file
        }
    }
}

function Get-Sha1([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA1]::Create()
    return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Hex([string]$Text) {
    $b = New-Object byte[] ($Text.Length / 2)
    for ($i = 0; $i -lt $b.Length; $i++) { $b[$i] = [Convert]::ToByte($Text.Substring($i * 2, 2), 16) }
    return , $b
}

function Invoke-Plan($account) {
    # $true: done (or nothing to do); $false: the game came back, the plan waits for its next exit
    $plan = Join-Path $account.FullName 'pending_restore.json'
    $claimed = Join-Path $account.FullName 'pending_restore.applying'
    if (-not (Test-Path -LiteralPath $plan)) { return $true }
    try { Move-Item -LiteralPath $plan -Destination $claimed -Force } catch { return $true }
    $results = New-Object System.Collections.ArrayList
    $before = $null
    $p = $null
    try {
        $p = [System.IO.File]::ReadAllText($claimed, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $saveDir = $p.save_dir

        # Prepare everything first: nothing is written until every file is ready
        $ready = New-Object System.Collections.ArrayList
        foreach ($a in $p.actions) {
            $target = Join-Path $saveDir $a.target
            try {
                if ($a.kind -eq 'file') {
                    $bytes = [System.IO.File]::ReadAllBytes($a.source)
                    if ((Get-Sha1 $bytes) -ne $a.sha1) { throw 'the backup file changed since you picked it' }
                } elseif ($a.kind -eq 'bank') {
                    $key = Hex $p.key
                    $text = Read-Save ([System.IO.File]::ReadAllBytes($target)) $key
                    $newText = Set-Bank $text ($a.block)
                    $bytes = Write-SaveBytes $newText $key
                    if ((Read-Save $bytes $key) -ne $newText) { throw 'the new profile did not read back the same' }
                } else {
                    throw ('unknown action ' + $a.kind)
                }
                [void]$ready.Add([pscustomobject]@{ Action = $a; Target = $target; Bytes = $bytes })
            } catch {
                [void]$results.Add(@{ what = $a.what; target = $a.target; ok = $false; error = $_.Exception.Message })
            }
        }

        if (Test-Game) {
            Move-Item -LiteralPath $claimed -Destination $plan -Force
            return $false
        }

        # Undo: every save as it is now
        $stamp = (Get-Date).ToString('yyyy-MM-dd_HH-mm-ss') + '-before-restore'
        $partial = Join-Path $account.FullName ($stamp + '.partial')
        New-Item -ItemType Directory -Path $partial -Force | Out-Null
        foreach ($f in (Get-ChildItem -LiteralPath $saveDir -Filter '*.sav' -File)) {
            Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $partial $f.Name) -Force
        }
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        [System.IO.File]::WriteAllText((Join-Path $partial 'info.json'), "{`"v`": 1, `"created`": $now, `"label`": `"before_restore`", `"keep`": false, `"files`": {}}")
        Rename-Item -LiteralPath $partial -NewName $stamp
        $before = $stamp

        foreach ($r in $ready) {
            try {
                Write-Atomic $r.Target $r.Bytes
                if ((Get-Sha1 ([System.IO.File]::ReadAllBytes($r.Target))) -ne (Get-Sha1 $r.Bytes)) { throw 'the file on disk did not match after writing' }
                [void]$results.Add(@{ what = $r.Action.what; target = $r.Action.target; ok = $true })
            } catch {
                [void]$results.Add(@{ what = $r.Action.what; target = $r.Action.target; ok = $false; error = $_.Exception.Message })
            }
        }
    } catch {
        [void]$results.Add(@{ what = 'restore'; ok = $false; error = $_.Exception.Message })
    }
    $report = @{ id = $(if ($p) { $p.id } else { '' }); applied = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); before = $before; results = $results.ToArray() }
    $json = ConvertTo-Json $report -Depth 5
    $out = Join-Path $account.FullName 'last_restore.json'
    [System.IO.File]::WriteAllText($out + '.tmp', $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath ($out + '.tmp') -Destination $out -Force
    Remove-Item -LiteralPath $claimed -Force -ErrorAction SilentlyContinue
    return $true
}

do {
    while (Test-Game) { Start-Sleep -Seconds 2 }
    Start-Sleep -Seconds $SettleSeconds
    $done = $true
    foreach ($account in (Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue)) {
        if (-not (Invoke-Plan $account)) { $done = $false }
    }
} until ($done)
