# Counter demo / 计数器示例

An intentionally small SwiftUI app with a unit test, UI test, stable accessibility IDs and an app log. It supports iPhone/iPad and provides enough scrolling content for exploratory UI requests. Its strings are English; changing language launch parameters does not create missing translations.

Copy this folder into a separate Git repository on your preferred development machine, initialize Git and add the files. Use the installed Skill to sync that repository as project `counter-demo`, then prepare it:

```json
{"operation":"prepare","project_id":"counter-demo","revision":"returned-revision","generate":"xcodegen","spec":"project.yml","scheme":"CounterDemo"}
```

The Mac must already have XcodeGen. Build/test Scheme `CounterDemo`; choose an existing idle registered device from the actual inventory. Use a development session to tap `increment`, type into `name`, read `counter` / `greeting`, then close your session.

For a reproducible bug exercise, change `Counter.increment` to add 2, reproduce it via UI and run the tests. Then fix it, incrementally sync, build, install in the same session and re-test. Only edit your own copy.

For a local build without booting a simulator, run XcodeGen in this directory and use a generic simulator destination. Keep DerivedData/module cache/result paths on external storage; do not commit the generated project or artifacts.

简体中文：这是带单元测试、UI 测试、稳定元素 ID 和日志的小型 SwiftUI 示例。把此目录复制为单独的 Git 仓库后，用 Skill 同步、执行上面的 XcodeGen 准备、构建并开启开发会话。可临时把加 1 改成加 2，练习“重现 → 测试失败 → 修复 → 增量上传 → 原会话安装重测”。复用已有空闲设备，结束时关闭自己的会话。示例不包含翻译，不提交生成工程与构建产物。
