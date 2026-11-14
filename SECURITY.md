# Security model

This worker accepts trusted source and runs Xcode build phases as the logged-in Mac user. It is intended for a single owner and a small set of trusted clients on a private LAN/VPN. It does not sandbox arbitrary build scripts, isolate mutually hostile tenants or provide an Internet-facing build farm.

## Credentials and transport

Use independently generated build/admin tokens and HTTPS with hostname validation plus the configured certificate pin. Restrict TCP peers through `allowed_networks`; proxy headers are disabled. Generated profiles and keys are owner-readable files. Keep the deployment, private client bundles, backups, logs, screenshots and real project code outside the public checkout. A build profile can execute code indirectly through projects; an admin profile can additionally execute reviewed cleanup and register devices.

Local UI-controller HTTP/MJPEG listeners bind to loopback. Clients use the Worker wrapper, not an unauthenticated controller port. A session ID is a cooperative ownership handle among clients sharing a credential, not a separate security principal.

## Data and deletion

The server checks external volume identity and root ownership. Cleanup requires a preview, a matching unexpired confirmation, unchanged candidate fingerprints, correct ownership markers and no active jobs/uploads/sessions. Source-history retention is separate and protects latest/recent/Archive-referenced revisions. Archives, original local projects and simulator data are outside cache cleanup.

These guards reduce operational mistakes; they are not a security audit or a substitute for backups. A trusted build script still has the Mac user's filesystem access. Review source before building it.

## Reporting a vulnerability

Use the repository's private vulnerability-reporting form when GitHub offers **Security → Report a vulnerability**. If private reporting is unavailable, open a minimal issue requesting a private contact channel, without exploit details, credentials, IPs, device IDs or logs. Do not post a real client profile or runtime directory as a reproduction.

## 简体中文

本服务面向单个所有者及少量可信客户端。构建脚本以 Mac 用户身份运行，不提供不可信多租户隔离。请使用独立随机 Token、HTTPS 证书固定校验和窄范围网络白名单，私下保存客户端配置及备份。清理采用先预览再执行的保护规则，历史 Archive、原项目和模拟器数据不属于缓存清理范围。

报告安全问题时优先使用 GitHub 私密漏洞报告；不可用时只提交请求私下联系的最小 issue，不要附带漏洞利用细节、凭证或真实环境数据。
