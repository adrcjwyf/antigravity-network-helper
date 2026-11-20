$ErrorActionPreference='Stop'
$policyFile = Join-Path $PSScriptRoot 'ipv4-policy.json'
if (!(Test-Path -LiteralPath $policyFile)) {
  [IO.File]::WriteAllText($policyFile, '{"expected_ipv4":null}')
}
$shell=New-Object -ComObject WScript.Shell
$desk=[Environment]::GetFolderPath('Desktop')
foreach($pair in @(@('VS Code（Antigravity代理）','VSCode'),@('Antigravity（代理辅助）','Desktop'),@('Antigravity连接检查','Models'))){
  $link=$shell.CreateShortcut((Join-Path $desk ($pair[0]+'.lnk')))
  $link.TargetPath="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $link.Arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot '桌面启动入口.ps1')+'" -Target '+$pair[1]
  $link.WorkingDirectory=$PSScriptRoot
  $link.Description='启动时识别 Clash 节点并核验 IPv4 出口'
  $link.WindowStyle=7
  $link.Save()
}
