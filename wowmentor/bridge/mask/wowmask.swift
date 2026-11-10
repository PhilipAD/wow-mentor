// wowmask: paints a thin black overlay over the WoWFeed strip so the player does not see it.
// Window capture (ScreenCaptureKit / screencapture -l) reads the game window's own backing store, so a
// window laid on top of the game hides the strip on screen without hiding it from the bridge.
//   stdin lines: "x y w h capW"  (pixels in the captured game window; capW = captured width in pixels)
//                "R <base64>"    fallback only: one row of RGB pixels taken just below the strip, sent by the bridge
// What the mask shows: the screen row directly below it, re-read 60 times a second (CGWindowListCreateImage of the windows underneath
// the mask) and stretched over it, so it blends into the picture even while the camera pans. When the screen cannot be read from this
// process (no Screen Recording permission) it falls back to the bridge's row, and to black before that arrives.
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
let content = NSView(frame: win.contentRect(forFrameRect: win.frame))
content.wantsLayer = true
content.layer?.backgroundColor = NSColor.black.cgColor
content.layer?.contentsGravity = .resize
content.layer?.magnificationFilter = .linear
content.layer?.minificationFilter = .linear
win.contentView = content

func rowImage(_ b64: String) -> CGImage? {
    guard let d = Data(base64Encoded: b64), d.count >= 6, d.count % 3 == 0 else { return nil }
    let n = d.count / 3
    guard let provider = CGDataProvider(data: d as CFData) else { return nil }
    return CGImage(width: n, height: 1, bitsPerComponent: 8, bitsPerPixel: 24, bytesPerRow: d.count, space: CGColorSpaceCreateDeviceRGB(),
                   bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue), provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
}

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

// CGWindowListCreateImage is marked unavailable in the macOS 15+ SDK but still ships in the system; look it up at run time
// (the bridge does the same from Python). Options: kCGWindowListOptionOnScreenBelowWindow = 1 << 2, kCGWindowImageBestResolution = 1 << 3.
typealias WindowImageFn = @convention(c) (CGRect, UInt32, UInt32, UInt32) -> Unmanaged<CGImage>?
let windowImage: WindowImageFn? = {
    guard let h = dlopen(nil, RTLD_NOW), let sym = dlsym(h, "CGWindowListCreateImage") else { return nil }
    return unsafeBitCast(sym, to: WindowImageFn.self)
}()
var liveOK = 0                     // frames in a row for which the live read worked
func sampleBelow() {
    let f = win.frame
    guard win.isVisible, f.height > 0, f.width > 0 else { return }
    let primaryH = NSScreen.screens.first?.frame.height ?? 0
    let src = CGRect(x: f.minX, y: primaryH - f.maxY + f.height, width: f.width, height: 1)
    guard let fn = windowImage, let img = fn(src, 1 << 2, UInt32(win.windowNumber), 1 << 3)?.takeRetainedValue(),
          img.width > 1, img.height > 0 else {
        if liveOK > 0 { FileHandle.standardError.write("wowmask: screen read failed, using the bridge's row\n".data(using: .utf8)!) }
        liveOK = 0; return
    }
    if liveOK == 0 { FileHandle.standardError.write("wowmask: reading the screen directly\n".data(using: .utf8)!) }
    liveOK = min(liveOK + 1, 1000)
    CATransaction.begin(); CATransaction.setDisableActions(true); content.layer?.contents = img; CATransaction.commit()
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
        if line.hasPrefix("R ") {
            if let img = rowImage(String(line.dropFirst(2))) {
                DispatchQueue.main.async { if liveOK > 3 { return }
                     CATransaction.begin(); CATransaction.setDisableActions(true); content.layer?.contents = img; CATransaction.commit() }
            }
            continue
        }
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
Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in place() }
let live = Timer(timeInterval: 1.0 / 60.0, repeats: true) { _ in sampleBelow() }
RunLoop.main.add(live, forMode: .common)
app.run()
