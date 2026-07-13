// RigMenuBar — a 🎹 icon in the macOS menu bar; click it to open the rig dashboard.
// Menu-bar-only (LSUIElement, .accessory) so it never shows in the Dock.

import Cocoa

final class Delegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    let url = "http://127.0.0.1:8765"

    func applicationDidFinishLaunching(_ note: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.title = "🎹"
            b.toolTip = "Rig — ouvrir le dashboard d'état"
            b.action = #selector(open)
            b.target = self
        }
    }

    @objc func open() {
        if let u = URL(string: url) { NSWorkspace.shared.open(u) }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
let delegate = Delegate()
app.delegate = delegate
app.run()
