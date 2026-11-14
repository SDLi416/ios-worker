# Persistent sessions and interactive development

Each JSON block is one request to either client. Job operations wait by default; the final object includes `id`, `status`, `result`, `artifacts`. Pass `wait:false` for asynchronous submission. Substitute IDs from actual results.

## Open, explore, update, test, close

```json
{"operation":"session_open","project_id":"my-app","build_job_id":"successful-build-id","simulator_id":"existing-id","label":"Fix settings save"}
```

The session ID is `result.id`, distinct from the outer job ID. Opening requires an idle registered device and one successful simulator app build; optional `app_name` resolves multiple products. Optional `arguments`, `environment` go to the app; optional `ui:false` defers controller startup. A session reserves the device across jobs. Up to three sessions may be reserved, but only one UI controller runs at a time to limit memory use. This is cooperative ownership among trusted clients sharing the worker credential, not adversarial tenant isolation.

```json
{"operation":"ui","command":"tree","session_id":"session-id","limit":60}
```

Returns a bounded visible element list and a full XML artifact. `visible_only:false` includes offscreen elements. Default limit 100, maximum 300. The tree's `name` often represents an accessibility identifier, but confirm by `find`.

```json
{"operation":"ui","command":"find","session_id":"session-id","selector":{"value":"save-button"}}
{"operation":"ui","command":"tap","session_id":"session-id","selector":{"value":"save-button"}}
{"operation":"ui","command":"type","session_id":"session-id","selector":{"value":"name-input"},"text":"New name","clear":true}
{"operation":"ui","command":"clear","session_id":"session-id","selector":{"value":"name-input"}}
{"operation":"ui","command":"swipe","session_id":"session-id","selector":{"using":"predicate string","value":"type == 'XCUIElementTypeScrollView' AND visible == 1"},"direction":"up"}
{"operation":"ui","command":"wait","session_id":"session-id","selector":{"value":"saved-confirmation"},"timeout_seconds":20}
{"operation":"ui","command":"orientation","session_id":"session-id","orientation":"LANDSCAPE"}
{"operation":"capture","session_id":"session-id","name":"saved-state","store":true}
```

Selectors: `accessibility id` (default), `predicate string`, `class name`, `xpath`, `class chain`. `find` returns `element_id` usable in place of a selector; IDs may become stale after navigation. Actions require exactly one match. Coordinate tap is `x`,`y` in points, not screenshot pixels. Swipe direction is up/down/left/right. `scroll` supports a direction with an explicit element selector. Orientation is PORTRAIT, LANDSCAPE, LANDSCAPERIGHT or PORTRAIT_UPSIDEDOWN; app-supported orientations still apply. Wait means an element exists, so use a predicate containing visibility/state when needed. UI typing interacts with the actual keyboard; app-specific focus/submit behavior still matters.

```json
{"operation":"session_launch","session_id":"session-id","arguments":["-ui-testing"],"environment":{"TEST_FIXTURE":"empty-account"}}
{"operation":"session_install","session_id":"session-id","build_job_id":"new-successful-build-id"}
{"operation":"submit","job":{"action":"test","project_id":"my-app","revision":"exact-revision","scheme":"MyApp","session_id":"session-id","only_testing":["MyAppUITests/SettingsTests/testSave"]}}
{"operation":"session_close","session_id":"session-id"}
```

`session_install` enforces the same bundle identifier and relaunches without erasing app data. Launch parameters/environment persist unless replaced. Use an app-supported deterministic fixture argument to reset only its own test state; no generic database reset is implied. XCTest may itself relaunch the app with its own environment. Re-launch through the session before sampling if a test changed the PID.

Close restores changes made to appearance, owned status-bar overrides and orientation while the controller is available, then shuts down the simulator it booted. It never erases the device. A failed job does not automatically release an otherwise active session; close it in task cleanup. If service restart interrupts a session, inspect it and explicitly close; reopen after recovery.

## Runtime diagnostics and test failures

```json
{"operation":"diagnostics","command":"logs","session_id":"session-id","seconds":120}
{"operation":"diagnostics","command":"sample","session_id":"session-id","seconds":3}
{"operation":"diagnostics","command":"crashes","session_id":"session-id"}
{"operation":"diagnostics","command":"controller_log","session_id":"session-id"}
{"operation":"attachments","test_job_id":"failed-test-job-id","only_failures":true}
```

Logs combine process-filtered simulator unified logs and session stdout/stderr, with a 16 KB tail and a downloadable full artifact. Stack sampling checks the current app PID identity before calling macOS `sample`; it is not an LLDB breakpoint debugger. Crash export filters recent `.ips`/`.crash` reports by this app and simulator/PID; OS reporting can lag, so an empty result does not prove no crash. Attachments export uses xcresulttool; the result includes a ZIP plus directly downloadable images when present. `only_failures:false` exports successful-test attachments too; optional `test_id` narrows export.

## Screenshot matrix

```json
{
  "operation":"capture_matrix",
  "project_id":"my-app",
  "build_job_id":"successful-build-id",
  "devices":["existing-phone-id","existing-large-phone-id","existing-ipad-id"],
  "variants":[
    {"name":"home","language":"en","locale":"en_US","appearance":"light","orientation":"PORTRAIT","status_bar":true,
     "arguments":["-ui-testing","-fixture","demo"],
     "steps":[{"operation":"tap","selector":{"value":"home-tab"}}],
     "wait_for":{"value":"home-ready"},"settle_seconds":1},
    {"name":"home","language":"zh-Hans","locale":"zh_CN","appearance":"dark","orientation":"LANDSCAPE",
     "wait_for":{"value":"home-ready"}}
  ]
}
```

At most 4 distinct existing devices and 10 variants, processed sequentially. All target devices must be idle and unreserved; close your exploratory session first. A matrix session installs only its own app, configures each variant, performs at most 50 UI steps, waits, exports and closes. It does not generate translations or implement missing fixture hooks. Status-bar changes refuse to replace preexisting overrides. Optional `environment` is per variant. Orientation requires the UI controller; language/locale use app launch preferences and do not change global simulator language.

Results include RGB PNGs with no alpha, dimensions/format checks, a manifest and `screenshots.zip`. Filenames include variant index, device ID and language. `store_ready_format` checks PNG color mode and a dated dimensions whitelist, not content, localization, supported device classes or App Store approval. Unsupported dimensions are reported; no stretching/cropping is performed. Review the actual images and Apple's current [screenshot specifications](https://developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications).

## Device registration

```json
{"operation":"register_device","simulator_id":"existing-id"}
```

Provisioning only when authorized: idle existing device, verified copy of its data onto worker SSD, original retained as `data.before-ios-worker-v2`, then a symlink to the external copy and registration. No new device/runtime and no original-data deletion. Registration must complete before that device can be used in sessions/matrices. Normal agents should use the already registered pool; don't register other projects' QA devices merely because they are listed.
