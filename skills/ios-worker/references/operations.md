# Request format

Write one UTF-8 JSON object per request file and pass it to `invoke.py --request` or `invoke.ps1 -RequestFile`. Paths may be absolute or relative to that file. Both clients return JSON without credentials. Exit code 2 means a terminal failed/cancelled/interrupted job; 1 means request/transport error. Submission waits by default (up to 900 seconds, adjustable `wait_seconds`, maximum 7200); set `wait:false` to return a job ID immediately. A wait timeout leaves the job running.

## Read-only operations

```json
{"operation":"status"}
{"operation":"simulators"}
{"operation":"sessions"}
{"operation":"toolchain"}
{"operation":"projects"}
{"operation":"jobs"}
{"operation":"storage"}
{"operation":"job","job_id":"returned-id"}
{"operation":"wait","job_id":"returned-id","wait_seconds":120}
{"operation":"log","job_id":"returned-id"}
```

`status` includes health/default simulator, only managed or booted devices, total device count and active/interrupted sessions. `simulators` returns the complete inventory. `jobs` returns 20 compact recent jobs. `job`/`log` return bounded summaries; `full:true` requests the old full result/log (or up to 100 jobs). Avoid reading everything to poll one job. `toolchain` checks actual Xcode/SDKs, available generator/package tools and prebuilt UI controller, without installing anything.

## Incremental working-tree sync and build

```json
{"operation":"sync","project_id":"my-app","source":"/path/to/current/git/repo"}
{"operation":"submit","job":{"action":"inspect","project_id":"my-app","revision":"returned-revision","container":"MyApp.xcodeproj"}}
{"operation":"submit","job":{"action":"build","project_id":"my-app","revision":"returned-revision","container":"MyApp.xcodeproj","scheme":"MyApp","configuration":"Debug"}}
{"operation":"submit","job":{"action":"test","project_id":"my-app","revision":"returned-revision","scheme":"MyApp","simulator_id":"existing-id","only_testing":["MyAppTests"],"timeout_seconds":1800}}
{"operation":"cancel","job_id":"returned-id"}
```

Sync includes tracked and non-ignored untracked files, including uncommitted edits. It hashes current file bytes, transfers missing content only, checks SHA-256 on the server, then creates an immutable revision. Changed files upload in full; this is file-level deduplication, not byte-level patching. A repeated manifest returns the same revision and uploads zero source bytes. Hashing and the manifest still cost local disk reads/network bytes. Git is required for automatic working-tree selection; LFS pointers, submodules, symlinks, credential-like files and case collisions are rejected for explicit review.

Project IDs use lowercase letters/digits/hyphens/underscores (1–64). Build the exact sync revision to avoid accidentally selecting another task's upload. `container` is relative to the source root and can be `.xcodeproj` or `.xcworkspace`; omit only when exactly one top-level container exists. `scheme` is required for build/test/archive. `configuration` defaults Debug; command timeout is 30–7200 seconds. Tests support `test_plan`, `only_testing`, `keep_simulator_running` and owned `session_id` (see development reference).

The stable worker workspace retains unchanged files and generated project outputs while updating source files. It also reuses project DerivedData and SwiftPM caches. Dependencies/build setting changes may still trigger full compilation. Concurrent jobs are serialized; revisions remain immutable. APFS file clones share physical blocks where supported, otherwise copy. Stable workspaces deliberately omit `.git`; projects needing Git metadata during build must provide version information or use a reviewed complete Git import with `stable_workspace:false`.

## ZIP and Git fallbacks

```json
{"operation":"upload","project_id":"my-app","zip_path":"source.zip"}
```

For a normal Git working tree, create the ZIP with `snapshot.py --source <git-root> --output <zip>` or `snapshot.ps1 -Source <git-root> -OutputFile <zip>`. Put it outside the repository. The ZIP root must contain the project. Uploading identical ZIP bytes reuses the revision, but still transfers the ZIP; prefer sync for routine edits. Non-Git/ignored-input layouts need a reviewed complete ZIP. Server source extraction also refuses unsafe paths, symlinks and Archives.

Git import action: `git`, `project_id`, `url`, full 40-character `commit`. It uses existing Mac credentials without prompts; it does not fetch submodules/LFS automatically. No client commits/stashes are made by this skill.

## Engineering preparation

```json
{"operation":"prepare","project_id":"my-app","revision":"returned-revision","generate":"xcodegen","spec":"project.yml","scheme":"MyApp"}
```

Requires the existing XcodeGen tool and a source-controlled spec. The worker remembers that project preparation and re-runs generation with its external cache on subsequent build/test/archive jobs, so files included by the spec can be added without hand-editing project references. Omitting `generate` resolves SwiftPM for an existing project. `locked_packages:true` requests Package.resolved versions. Prefer a reproducible checked-in spec; this API does not invent targets/schemes or fix an arbitrary pbxproj.

SwiftPM downloads/caches, DerivedData, result bundles, working copies and job temporary files live on the SSD. Other dependency systems require project-specific preparation. OS-managed simulator runtimes, Xcode and small system logs remain under macOS control; do not promise zero internal-disk writes.

## Artifacts

```json
{"operation":"download","job_id":"returned-id","name":"test-summary.json","output_path":"artifacts/test-summary.json"}
```

Choose names from the completed job's artifact list. Download checks SHA-256 and refuses to overwrite an existing local file. Simulator `.app` ZIPs, test summary, exported attachments/screenshots and diagnostics work from any OS; interactive xcresult browsing still needs Xcode. `.app`, unsigned Archive and signed IPA are different results. See the development reference for sessions and screenshots.

Legacy simulator action is still available through `submit`: `boot`, `install` (`build_job_id`, optional `app_name`), `launch`/`terminate`/`uninstall` (`bundle_id`), `screenshot`, `video` (`duration_seconds` 1–60), `openurl` (`url`), `location` (`latitude`,`longitude`), `push` (`bundle_id`,`payload`), `privacy` (`bundle_id`,`mode`,`service`), `shutdown`. Always specify the managed `simulator_id`; include owning `session_id` if reserved. Prefer sessions to manual boot/shutdown. Only your app and a device your task started may be changed. No creation/erase/delete.

## Archive/export

```json
{"operation":"submit","job":{"action":"archive","project_id":"my-app","revision":"returned-revision","scheme":"MyApp","configuration":"Release","signing":false}}
```

`signing:true` uses existing Mac signing settings. `export` requires `archive_job_id` and valid project-specific `export_options`; signing/provisioning are not configured or verified by this upgrade. Do not alter identities or automatically sign in. The user's release workflow can remain with Xcode Cloud.

## Cache cleanup and source history

```json
{"operation":"cleanup_preview","categories":["derived_data","temporary","session_logs"],"older_than_days":14}
{"operation":"cleanup_execute","plan_id":"reviewed-id","confirmation":"reviewed-id"}
{"operation":"retention_preview","project_id":"my-app","keep_last":10,"older_than_days":14}
{"operation":"retention_execute","plan_id":"reviewed-id","confirmation":"reviewed-id"}
```

Preview first and check deletion authorization against the exact plan. No automatic cleanup is enabled. Only execute the reviewed ID; it expires after 15 minutes, cannot be replayed, and changed candidates abort. Jobs, uploads and active/interrupted sessions block all cleanup.

Cache categories: `derived_data`, `source_packages`, `package_cache`, `results`, `temporary`, `job_logs`, `session_logs`. Only owned directories are eligible, with `.keep` and `.xcarchive` protection. Preserve wanted products, xcresults, crash/sample files and screenshots before approving deletion of Results/session logs.

Source retention is separate from cache cleanup. It retains at least `keep_last` newest revisions (1–1000, default 10), the latest revision, any revision referenced by an Archive job, and content referenced by surviving revisions or uncommitted unexpired sync plans. Only older eligible revision copies and unreferenced content objects appear. Worker Archives and original local projects are never candidates. The stable current workspace is retained. Preview sizes count APFS allocated blocks and may double-count shared blocks; actual reclaimed bytes can be smaller. Do not infer that every saved revision consumes a full additional project copy.
