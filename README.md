# Antigravity 网络辅助（Windows）

为 VS Code 插件和 Antigravity 独立客户端创建单独的启动入口。这里只分发辅助脚本，不包含客户端、订阅或任何预设出口。

先安装 Python 3.10+、Clash Verge Rev 以及要使用的客户端。开启 Clash 系统代理并关闭 TUN。VS Code 的 code.cmd 应在 PATH 中，或安装在默认目录；未安装 Node.js 时使用 Code.exe 运行节点检测。

在解压目录运行：

    python -m pip install PyYAML
    powershell -NoProfile -ExecutionPolicy Bypass -File .\创建桌面入口.ps1

通过桌面上的「VS Code（Antigravity代理）」或「Antigravity（代理辅助）」启动。首次启用时请核对两个 HTTPS 服务共同测得的 IPv4，再决定是否保存。后续更换出口也会要求确认。「Antigravity连接检查」获取模型列表需要 CLI 已登录。

支持独立的 IPv4 节点；域名节点还需要 Clash 中已有可用的 DNS-BOOT-IP 组（唯一成员为受支持的公网 IPv4 节点）和与辅助脚本兼容的代理 DNS 上游。脚本不会代为创建这组配置。运行期间保持辅助窗口开启；本工具只覆盖经辅助入口启动的应用。
