param([ValidateSet('VSCode','Desktop','Models')][string]$Target='VSCode')
$ErrorActionPreference='Stop'
try {
    Write-Host '当前采用 IPv4 方案，启动时识别 Clash 节点并核验出口；不会回退到 IPv6 或直连。' -ForegroundColor Cyan
    Write-Host '启动前需要核验出口。应用使用期间请保持此窗口最小化，不要关闭。'
    & (Join-Path $PSScriptRoot 'Start-Antigravity-IPv4.ps1') -Target $Target
    if ($Target -eq 'Models') {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show('当前 IPv4 出口核验通过。官方客户端已成功获取模型列表。检查用代理已停止。','Antigravity IPv4 连接检查') | Out-Null
    }
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    $detail=$_.Exception.Message
    if ($detail -like '*close all VS Code*') { $detail='请保存工作并完全退出所有 VS Code 窗口，再双击此入口。当前已打开的进程不会自动切换代理。' }
    elseif ($detail -like '*Close Antigravity*') { $detail='请保存工作并完全退出独立版 Antigravity，再双击此入口。' }
    elseif ($detail -like '*SkyVPN alone*') { $detail='未连接到 Clash Verge 的 Mihomo 控制管道。请打开 Clash Verge，等内核启动后再试；仅开 SkyVPN 系统代理不能使用这个入口。' }
    [System.Windows.Forms.MessageBox]::Show(('IPv4 启动未完成：'+$detail+[Environment]::NewLine+'没有切换到其他出口、IPv6 或直连。日志：'+$PSScriptRoot+'\ipv4-last-start.log'),'Antigravity IPv4 辅助',0,16) | Out-Null
    exit 1
}
