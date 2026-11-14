# Contributing

[English](README.md) · [简体中文](README.zh-CN.md)

Keep changes focused and describe the resulting behavior and relevant validation. Update both README languages when their user-facing instructions change. Keep project-specific simulator requirements in the actual project; generic Worker behavior must not assume a person's device inventory or network.

## Local checks

Use Python 3.11+ on macOS for the server suite. Put the virtual environment, pip cache and `TMPDIR` on external storage if internal space is limited. Install `requirements.lock` into that environment, then run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server python3 -B -m unittest discover -s server/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

The tests use temporary directories and a simulated external-volume boundary. They do not start a simulator, invoke a real iOS build or connect to a configured Worker. Client-only tests also run with Python 3.9+ on Windows/Linux:

```sh
python3 -B -m unittest discover -s tests -p test_clients.py -v
```

Stage intended source/documentation files, inspect them, then run:

```sh
python3 -B scripts/check_public.py
gitleaks git --redact
```

The first check detects runtime/credential files, personal home paths, private LAN literals and common credential patterns in tracked files. Gitleaks is a separate tool; get it from its official project. Neither check proves all information is non-sensitive, so review diffs and generated archives too. Never commit private profiles, certificate material, real device IDs, runtime state, logs, local screenshots or business app sources. Use example.test domains, RFC 5737 documentation IPs and clearly nonfunctional placeholders.

When testing iOS behavior manually, first list all devices and booted devices, reuse compatible idle devices, only change your own app, and close only sessions your task owns. Don't erase/delete shared devices to clean up a test.

## 简体中文

请保持改动聚焦，在 PR 中说明行为变化与实际验证。用户操作步骤变化时同时更新中英文 README。测试使用临时目录和模拟磁盘边界，不启动模拟器；真实 iOS 验证应先查看设备与占用情况，复用空闲设备，只操作本任务 App，并由本任务关闭自己的会话。

提交前检查暂存内容，运行公开内容检查和独立密钥扫描。配置、证书、设备 ID、运行记录、业务源码和截图均应留在私有环境；用保留域名、文档 IP 和占位符描述示例。
