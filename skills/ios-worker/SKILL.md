---
name: ios-worker
description: Develop, build and test iOS apps on a private Mac from Windows, macOS or Linux. Use for incremental source sync, Xcode/XCTest, persistent simulator sessions, interactive UI exploration, runtime diagnostics, multi-device screenshots and worker storage management.
metadata:
  version: "2.0.0"
---

# iOS Worker

Use authenticated HTTPS to the configured Mac. The client needs no Xcode, SSH, MCP registration or third-party Python package. Any agent able to read this skill, run local commands and reach the worker network can use it. A cloud-only agent cannot automatically reach a private LAN.

## Entry point

Resolve paths from this skill's actual installation directory, never a fixed username or OS:

- Python 3.9+: `python3 -B <skill>/scripts/invoke.py --request <request.json>`; Windows can use `python` or `py -3`.
- Windows PowerShell 5.1 / .NET 4.8 or PowerShell 7: `powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/invoke.ps1 -RequestFile <request.json>`; use `pwsh` when available. ExecutionPolicy applies only to that process.

Omit the request argument for compact `status`. Do this first. Helpers load `private/connection.json`, `admin-connection.json` and the pinned certificate. Never dump credentials into chat, source uploads or repositories. A public checkout has no profiles. Obtain an owner-generated private client bundle, or configure local profiles from the examples before connecting. Override profiles with `IOS_WORKER_CONFIG` / `IOS_WORKER_ADMIN_CONFIG` for a different worker.

Read only the relevant reference: [build/sync/storage](references/operations.md), [sessions/UI/diagnostics/screenshots](references/development.md), or [connection troubleshooting](references/connection.md).

## Normal development loop

1. Read project instructions and choose its actual repository and shared scheme.
2. Use `sync` on the current Git working tree, including modified and non-ignored untracked files. It hashes files locally and uploads only missing content; no change reuses the revision. No commit/stash is needed. Snapshot ZIP upload remains a fallback for reviewed source layouts. Do not silently omit uncommitted work, submodules, LFS or required ignored files.
3. Build the exact returned revision. The worker updates a stable external-SSD workspace, preserves unchanged file timestamps and reuses project caches. XcodeGen preparation and SwiftPM resolution are available; don't assume Pods, Flutter or npm dependencies were prepared.
4. For exploration, open a **development session** from the successful build on an idle managed simulator. Retain its session ID. Read the tree, find a precise element, tap/type/swipe, capture, inspect runtime logs. Prefer accessibility identifiers; if multiple elements match, refine the selector instead of guessing coordinates.
5. Reproduce a bug, add a useful assertion, sync the fix, build, then `session_install`. Run `test` with that same `session_id` to reuse the owned booted device. The worker suspends its UI controller for XCTest; the next UI request restarts it. Report actual passed/failed test counts, not just submission success.
6. Download useful artifacts with SHA-256 verification. Close the session when finished; this shuts down only the device it started. Do not leave an undisclosed reservation.

Job-producing operations wait internally by default, returning a compact final result. For long jobs use `wait:false`, record the job ID, then `wait` or `job`; do not repeatedly paste full logs/status. Wait timeout does **not** cancel a job. After an uncertain POST/network timeout, inspect recent jobs before resubmitting. `log` returns an error-focused tail; request `full:true` only when necessary. This keeps token usage bounded.

## Simulator agreement

Project rules take precedence. Before testing/opening a session, use `status` or `simulators`; the server runs both `xcrun simctl list devices` and `xcrun simctl list devices booted`. Client-side Windows `xcrun` is unnecessary.

- Reuse existing compatible `worker_managed:true` devices. A booted device is occupied unless its session belongs to this task. Never create a simulator per app/task.
- The API rejects taking over another reservation or an already booted unreserved device. Close only your session; never erase/delete a device or alter another app's data. Raw legacy operations also require the owning `session_id` for reserved devices.
- Unregistered devices are visible but read-only. `register_device` requires admin access and explicit provisioning scope: it copies and verifies an idle device's data onto the external disk, preserves the original as a backup and registers it. Do not automatically register occupied devices or broaden the pool.
- Respect project-specific device/runtime compatibility rules. Inspect the actual inventory rather than assuming a model, runtime or device identifier.
- Standalone tests require an idle device and shut down their own booted device. Session tests keep the session alive. Service restart leaves interrupted reservations visible: review then close; don't silently steal them.

Interactive UI works through a Mac-local XCTest controller. It is not a live remote desktop or a substitute for real-device hardware/performance validation. Language/seed data and page state still require the app's own launch arguments or UI steps. Use screenshot matrices sequentially and validate app content in addition to image format.

## Storage and boundaries

Use `storage` then a concrete preview. Cache cleanup and source-history retention are separate operations. Both require the reviewed plan ID, admin credentials, unchanged paths and no active jobs/uploads/sessions. Present paths/estimated bytes; execute only within the user's explicit deletion authorization. Existing authorization need not be requested again, but never infer permission to delete history from a request to inspect space.

Cache cleanup handles worker-owned caches/results/temp/job/session logs. Source retention protects the newest revisions, latest source, archive-referenced revisions and in-progress sync content. Archives, simulator data and original projects are excluded. APFS clones can share blocks: reported allocated-size sums are upper bounds, not guaranteed reclaimed space. No automatic deletion is enabled.

Build phases execute as the Mac user: only trusted code. Do not bypass TLS, publish the service, widen network access, modify signing identities or delete arbitrary files to work around a failure. Signing, LLDB/Instruments, real devices and live human remote control require separate setup and actual validation.
