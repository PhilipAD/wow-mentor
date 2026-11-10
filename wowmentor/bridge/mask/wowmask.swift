// wowmask: paints a thin black overlay over the WoWFeed strip so the player does not see it.
// Window capture (ScreenCaptureKit / screencapture -l) reads the game window's own backing store, so a
// window laid on top of the game hides the strip on screen without hiding it from the bridge.
//   stdin lines: "x y w h capW"  (pixels in the captured game window; capW = captured width in pixels)
// Exits when stdin closes (the bridge died).
import AppKit
import CoreGraphics

struct Req { var x: CGFloat; var y: CGFloat; var w: CGFloat; var h: CGFloat; var capW: CGFloat }
var req: Req? = nil
let lock = NSLock()

let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 10, height: 2), styleMask: .borderless, backing: .buffered, defer: false)
win.isOpaque = true
win.backgroundColor = .black
win.hasShadow = false
win.ignoresMouseEvents = true
win.level = .screenSaver
win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
win.isReleasedWhenClosed = false

func gameBounds() -> CGRect? {
    guard let list = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] else { return nil }
    var best: CGRect? = nil
    var area: CGFloat = 0
    for w in list {
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        let layer = w[kCGWindowLayer as String] as? Int ?? -1
        guard owner == "Wow" || owner == "World of Warcraft", layer == 0,
              let d = w[kCGWindowBounds as String] as? NSDictionary,
              let r = CGRect(dictionaryRepresentation: d as CFDictionary) else { continue }
        if r.width * r.height > area { area = r.width * r.height; best = r }
    }
    return best
}

func place() {
    lock.lock(); let r = req; lock.unlock()
    guard let q = r, let g = gameBounds(), q.capW > 0, g.width > 0 else { win.orderOut(nil); return }
    let scale = q.capW / g.width
    let primaryH = NSScreen.screens.first?.frame.height ?? 0
    let wPts = q.w / scale, hPts = max(1, q.h / scale)
    let frame = NSRect(x: g.minX + q.x / scale, y: primaryH - (g.minY + q.y / scale + hPts), width: wPts, height: hPts)
    if win.frame != frame { win.setFrame(frame, display: true) }
    win.orderFrontRegardless()
}

let reader = Thread {
    while let line = readLine() {
        let n = line.split(separator: " ").compactMap { Double($0) }
        if n.count == 5 {
            lock.lock(); req = Req(x: n[0], y: n[1], w: n[2], h: n[3], capW: n[4]); lock.unlock()
        }
    }
    exit(0)
}
reader.start()

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in place() }
app.run()
