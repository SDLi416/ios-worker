import AppKit

final class WorkerLauncher: NSObject, NSApplicationDelegate {
    let root: String = {
        guard let value = Bundle.main.object(forInfoDictionaryKey: "IOSWorkerRoot") as? String,
              value.hasPrefix("/") else { fatalError("Build the launcher with an absolute IOSWorkerRoot") }
        return value
    }()
    var process: Process?
    var log: FileHandle?
    var item: NSStatusItem!
    var paused = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "iOS Worker"
        let menu = NSMenu()
        menu.addItem(withTitle: NSLocalizedString("Open Worker Folder", comment: "menu"), action: #selector(openFolder), keyEquivalent: "")
        menu.addItem(withTitle: NSLocalizedString("Pause / Resume Service", comment: "menu"), action: #selector(toggle), keyEquivalent: "")
        for entry in menu.items { entry.target = self }
        item.menu = menu
        start()
        Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.start() }
    }
    func start() {
        guard !paused, process?.isRunning != true else { return }
        do {
            // The app has its own macOS identity for removable-volume consent.
            _ = try FileManager.default.contentsOfDirectory(atPath: root)
            guard FileManager.default.fileExists(atPath: root + "/.worker-root.json") else { return }
            let path = root + "/Service/logs/server.log"
            if !FileManager.default.fileExists(atPath: path) { FileManager.default.createFile(atPath: path, contents: nil) }
            log = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
            let size = try log!.seekToEnd()
            if size > 2_000_000 { try log!.truncate(atOffset: 0) }
            let child = Process()
            child.executableURL = URL(fileURLWithPath: root + "/Service/venv/bin/python")
            child.arguments = ["-B", root + "/Service/src/serve.py"]
            child.currentDirectoryURL = URL(fileURLWithPath: root + "/Service/src")
            var env = ProcessInfo.processInfo.environment
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["TMPDIR"] = root + "/Service/work/"
            child.environment = env
            child.standardOutput = log
            child.standardError = log
            try child.run()
            process = child
            let awake = Process()
            awake.executableURL = URL(fileURLWithPath: "/usr/bin/caffeinate")
            awake.arguments = ["-is", "-w", String(child.processIdentifier)]
            try? awake.run()
            item.button?.title = "iOS Worker"
        } catch {
            item.button?.title = NSLocalizedString("iOS Worker · Permission needed", comment: "status")
        }
    }
    @objc func openFolder() { NSWorkspace.shared.open(URL(fileURLWithPath: root)) }
    @objc func toggle() {
        paused.toggle()
        if paused { process?.terminate(); item.button?.title = NSLocalizedString("iOS Worker · Paused", comment: "status") }
        else { start() }
    }
    func applicationWillTerminate(_ notification: Notification) { process?.terminate() }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = WorkerLauncher()
app.delegate = delegate
app.run()
