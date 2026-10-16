import Cocoa
import WebKit

// Native window around the gauntlet monitor page, plus a menu bar item with a popover of the same page.
// Starts watch.py when nothing listens on the port.
let port = "7777"
let base = URL(string: "http://localhost:\(port)")!
let watch = Bundle.main.object(forInfoDictionaryKey: "GauntletWatch") as! String

final class App: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    let web = WKWebView()
    let mini = WKWebView()
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let pop = NSPopover()

    func applicationDidFinishLaunching(_ n: Notification) {
        startMonitor()
        web.navigationDelegate = self
        web.allowsBackForwardNavigationGestures = true
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Gauntlet"
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.setFrameAutosaveName("Gauntlet")
        window.center()
        window.makeKeyAndOrderFront(nil)
        menu()
        load()
        statusItem()
        NSApp.activate(ignoringOtherApps: true)
    }

    // Menu bar: left click opens the monitor in a popover, right click shows the menu. Title is the running count.
    func statusItem() {
        item.button!.image = mark()
        item.button!.imagePosition = .imageLeading
        item.button!.target = self
        item.button!.action = #selector(clicked)
        item.button!.sendAction(on: [.leftMouseUp, .rightMouseUp])
        mini.navigationDelegate = self
        let vc = NSViewController()
        vc.view = mini
        pop.contentViewController = vc
        pop.contentSize = NSSize(width: 560, height: 720)
        pop.behavior = .transient
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in self.poll() }.fire()
    }

    @objc func clicked() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let m = NSMenu()
            m.addItem(withTitle: "Open Window", action: #selector(show), keyEquivalent: "").target = self
            m.addItem(withTitle: "Quit Gauntlet", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item.menu = m
            item.button!.performClick(nil)
            item.menu = nil
        } else if pop.isShown {
            pop.performClose(nil)
        } else {
            mini.load(URLRequest(url: base))
            pop.show(relativeTo: item.button!.bounds, of: item.button!, preferredEdge: .minY)
            pop.contentViewController!.view.window?.makeKey()
        }
    }

    @objc func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func poll() {
        URLSession.shared.dataTask(with: base.appendingPathComponent("api")) { data, _, _ in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let sessions = (json?["worktrees"] as? [[String: Any]] ?? []).flatMap { $0["sessions"] as? [[String: Any]] ?? [] }
            let running = sessions.filter { ($0["running"] as? Int ?? 0) > 0 }.count
            DispatchQueue.main.async { self.item.button!.title = running > 0 ? " \(running)" : "" }
        }.resume()
    }

    // The app mark in its green: faint ring, solid dot.
    func mark() -> NSImage {
        NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            let green = NSColor(red: 0.25, green: 0.73, blue: 0.31, alpha: 1)
            let ring = NSBezierPath(ovalIn: NSRect(x: 2, y: 2, width: 14, height: 14))
            ring.lineWidth = 1.6
            green.withAlphaComponent(0.5).setStroke(); ring.stroke()
            green.setFill(); NSBezierPath(ovalIn: NSRect(x: 5.5, y: 5.5, width: 7, height: 7)).fill()
            return true
        }
    }

    func startMonitor() {
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        probe.arguments = ["-i", ":\(port)", "-sTCP:LISTEN"]
        probe.standardOutput = FileHandle.nullDevice
        try? probe.run(); probe.waitUntilExit()
        if probe.terminationStatus == 0 { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", watch]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }

    @objc func load() { web.load(URLRequest(url: base)) }
    @objc func reload() { web.reload() }
    @objc func back() { web.goBack() }

    // Server may still be booting on first launch: retry until it answers.
    func webView(_ w: WKWebView, didFailProvisionalNavigation _: WKNavigation!, withError _: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { w.load(URLRequest(url: base)) }
    }

    // GitHub and other external links open in the default browser.
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = a.request.url, u.host != "localhost", u.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(u); decisionHandler(.cancel)
        } else { decisionHandler(.allow) }
    }

    func menu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Gauntlet", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Home", action: #selector(load), keyEquivalent: "1").target = self
        view.addItem(withTitle: "Back", action: #selector(back), keyEquivalent: "[").target = self
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        viewItem.submenu = view
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        NSApp.mainMenu = main
    }

    // Closing the window keeps the menu bar item; clicking the Dock icon brings the window back.
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        show(); return true
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
