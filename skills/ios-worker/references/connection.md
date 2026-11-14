# Installation and connection

An owner-generated private client bundle includes `private/connection.json`, `private/admin-connection.json`, and the public server certificate. This is an owner-only distribution. Copy the whole skill directory and keep private files out of Git, public skill registries, and chat context. The public repository contains no connection profiles. Use the provisioner to generate a private client bundle; there is no need to configure MCP or SSH.

For another server, set `IOS_WORKER_CONFIG` to a local connection profile; optional `IOS_WORKER_ADMIN_CONFIG` points to its admin profile. Profile fields are `url` (HTTPS), `token`, `ca_file` (relative to that profile) and `certificate_sha256` (SHA-256 of DER server certificate). Obtain those from the server owner through an authorized channel. Never copy the server private key. Multiple agents can share the skill code and select different profiles through these variables.

Run the entrypoint with no arguments to check connectivity. It loads secrets without displaying them. An agent must have local command execution and a network route to the Mac; uploading this skill into a cloud-only environment does not create a LAN route.

| Failure | Check |
|---|---|
| Connection refused | Is the Mac service listening? Is the Mac awake, user logged in, SSD attached? |
| Timeout | Are client and Mac routable, including different private subnets? Check only the configured host/port. |
| HTTP 401 | Wrong/expired token or mismatched profile; do not print it. |
| HTTP 403 | Client IP allowlist or admin-only operation. Ask the owner for the required narrow change; do not widen exposure automatically. |
| TLS/name/pin failure | Certificate expired/replaced or server address changed. Update verified profile/certificate; never use insecure/skip-certificate flags. |
| HTTP 503 / disk identity | SSD mount/UUID/root marker mismatch. No fallback to internal disk. |
| HTTP 507 | Free-space admission threshold. Inspect storage and preview authorized cache cleanup. |
| Busy simulator / cleanup 409 | Reuse an idle compatible managed simulator, or wait for ongoing work; don't stop others. |
| Required device is unmanaged | Register an existing idle compatible device only within authorized provisioning scope. Other projects' devices remain read-only. |

Network/permissions are separate from skill installation: an installed skill cannot repair a missing route, unlock FileVault, or grant the native launcher external-disk access. Report the actual failed layer and continue work that is independent of it.

This package follows the [Agent Skills format](https://agentskills.io/specification). For Codex, the user skill directory is `~/.agents/skills`; client-specific installation paths may differ.
