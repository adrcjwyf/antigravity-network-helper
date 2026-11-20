function Wait-AntigravityApplication {
    param(
        [Parameter(Mandatory=$true)][string]$ProcessName,
        [object]$Worker=$null,
        [int]$GracePolls=20,
        [int]$PollMilliseconds=500
    )
    $emptyPolls=0
    $seen=$false
    while($emptyPolls -lt $GracePolls) {
        if($Worker -and $Worker.HasExited) {
            throw 'IPv4 worker stopped during use. No alternate exit was used. Check ipv4-last-start.log.'
        }
        $running=@(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
        if($running.Count -gt 0) {
            $seen=$true
            $emptyPolls=0
        } else {
            $emptyPolls++
        }
        if($emptyPolls -lt $GracePolls) { Start-Sleep -Milliseconds $PollMilliseconds }
    }
    if(-not $seen) { throw 'Application did not start; proxy is stopping.' }
}
