// audiolevel — measure whether audio SIGNAL is flowing, without hearing it.
//
// Uses the macOS 14.4+ CoreAudio process-tap API to tap audio output (a target process
// by bundle-id substring, else the global mix), accumulate mean RMS, and report it. The
// caller compares it to a threshold: > threshold = sound is passing, ~0 = silence.
//
// Two modes:
//
//   audiolevel [seconds] [bundle]                 # ONE-SHOT: measure `seconds`, print one line, exit
//   audiolevel --daemon [interval] [outfile] [bundle]   # DAEMON: keep the tap open, every
//                                                   # `interval`s write "<rms> <epoch>" to `outfile`, loop forever
//
// One-shot prints:  RMS <mean> TARGET <what-was-tapped> FRAMES <n>   (exit 0 always)
//
// Why the daemon mode exists — TCC (macOS audio permission) is granted per-APP, and a
// subprocess spawned by the dashboard does NOT inherit it, so the tap comes back silent.
// The daemon runs as its OWN long-lived process (a LaunchAgent the user authorises ONCE);
// it publishes the level to a file that the generic engine reads through an ordinary check.
// This keeps zero macOS-specific code in the engine — the file is just a data source.

import CoreAudio
import Foundation

// --- shared accumulator, written by the realtime IOProc, read+reset by the main thread ---
final class Meter {
    private var sumSq: Double = 0
    private var frames: Int = 0
    private let lock = NSLock()
    func add(_ ss: Double, _ n: Int) { lock.lock(); sumSq += ss; frames += n; lock.unlock() }
    // Atomically read the RMS over what accumulated since the last call, and reset.
    func drainRMS() -> Double {
        lock.lock(); let ss = sumSq; let fr = frames; sumSq = 0; frames = 0; lock.unlock()
        return fr > 0 ? (ss / Double(fr)).squareRoot() : 0
    }
}

// --- args -------------------------------------------------------------------------------
let args = CommandLine.arguments
let daemon = args.contains("--daemon")
var positional = Array(args.dropFirst()).filter { $0 != "--daemon" }
let interval = daemon ? (positional.count > 0 ? (Double(positional[0]) ?? 1.0) : 1.0)
                      : (positional.count > 0 ? (Double(positional[0]) ?? 1.5) : 1.5)
let outfile  = daemon ? (positional.count > 1 ? positional[1] : NSString(string: "~/.cache/readyset/audiolevel").expandingTildeInPath) : ""
let match    = (daemon ? (positional.count > 2 ? positional[2] : "") : (positional.count > 1 ? positional[1] : "ableton")).lowercased()

func failOneShot(_ target: String) -> Never { print("RMS 0.000000 TARGET \(target) FRAMES 0"); exit(0) }
func die(_ msg: String) -> Never { FileHandle.standardError.write(Data((msg + "\n").utf8)); exit(1) }

// Find audio processes whose bundle id contains `needle` (empty needle → none → global tap).
func matchingProcesses(_ needle: String) -> [AudioObjectID] {
    if needle.isEmpty { return [] }
    let sys = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(sys, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    var procs = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(sys, &addr, 0, nil, &size, &procs) == noErr else { return [] }
    var out: [AudioObjectID] = []
    for pid in procs {
        var ba = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyBundleID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var bsize = UInt32(MemoryLayout<CFString?>.size)
        var cf: CFString? = nil
        if AudioObjectGetPropertyData(pid, &ba, 0, nil, &bsize, &cf) == noErr,
           let bundle = cf as String?, bundle.lowercased().contains(needle) {
            out.append(pid)
        }
    }
    return out
}

// --- build the tap + aggregate device; returns (aggID, tapID, procID) or nil on failure ---
func openTap(_ meter: Meter) -> (AudioObjectID, AudioObjectID, AudioDeviceIOProcID)? {
    let procs = matchingProcesses(match)
    let desc = procs.isEmpty
        ? CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        : CATapDescription(stereoMixdownOfProcesses: procs)
    desc.name = "rig-audiolevel-tap"
    desc.isPrivate = true

    var tapID = AudioObjectID(kAudioObjectUnknown)
    if AudioHardwareCreateProcessTap(desc, &tapID) != noErr { return nil }

    let aggDict: [String: Any] = [
        kAudioAggregateDeviceNameKey as String: "rig-audiolevel-agg",
        kAudioAggregateDeviceUIDKey as String: "rig-audiolevel-\(UUID().uuidString)",
        kAudioAggregateDeviceIsPrivateKey as String: true,
        kAudioAggregateDeviceTapAutoStartKey as String: true,
        kAudioAggregateDeviceTapListKey as String: [[kAudioSubTapUIDKey as String: desc.uuid.uuidString]],
    ]
    var aggID = AudioObjectID(kAudioObjectUnknown)
    if AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggID) != noErr {
        AudioHardwareDestroyProcessTap(tapID); return nil
    }

    var procID: AudioDeviceIOProcID?
    let cst = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { (_, inData, _, _, _) in
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
        var ls: Double = 0, ln = 0
        for buf in abl {
            let n = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
            if n > 0, let p = buf.mData?.assumingMemoryBound(to: Float.self) {
                for i in 0..<n { ls += Double(p[i]) * Double(p[i]) }
                ln += n
            }
        }
        meter.add(ls, ln)
    }
    if cst != noErr || procID == nil {
        AudioHardwareDestroyAggregateDevice(aggID); AudioHardwareDestroyProcessTap(tapID); return nil
    }
    return (aggID, tapID, procID!)
}

func closeTap(_ agg: AudioObjectID, _ tap: AudioObjectID, _ proc: AudioDeviceIOProcID) {
    AudioDeviceStop(agg, proc)
    AudioDeviceDestroyIOProcID(agg, proc)
    AudioHardwareDestroyAggregateDevice(agg)
    AudioHardwareDestroyProcessTap(tap)
}

// Atomically publish "<rms> <epoch>" to `path` (write temp + rename, so readers never see a half-written file).
func publish(_ rms: Double, to path: String) {
    let dir = (path as NSString).deletingLastPathComponent
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    let line = String(format: "%.6f %.0f\n", rms, Date().timeIntervalSince1970)
    let tmp = path + ".tmp"
    if (try? line.write(toFile: tmp, atomically: false, encoding: .utf8)) != nil {
        try? FileManager.default.removeItem(atPath: path)
        try? FileManager.default.moveItem(atPath: tmp, toPath: path)
    }
}

// --- run --------------------------------------------------------------------------------
let meter = Meter()

if daemon {
    // Keep the tap open for the whole process lifetime; re-open if it ever drops.
    var handles = openTap(meter)
    if let (agg, _, proc) = handles { AudioDeviceStart(agg, proc) }
    while true {
        if handles == nil {                      // tap failed (e.g. no permission yet) — retry, publish 0 meanwhile
            publish(0, to: outfile)
            Thread.sleep(forTimeInterval: interval)
            handles = openTap(meter)
            if let (agg, _, proc) = handles { AudioDeviceStart(agg, proc) }
            continue
        }
        Thread.sleep(forTimeInterval: interval)
        publish(meter.drainRMS(), to: outfile)
    }
} else {
    guard let (agg, tap, proc) = openTap(meter) else { failOneShot(match.isEmpty ? "global" : match) }
    AudioDeviceStart(agg, proc)
    Thread.sleep(forTimeInterval: interval)
    let rms = meter.drainRMS()
    closeTap(agg, tap, proc)
    print(String(format: "RMS %.6f TARGET %@ FRAMES %d", rms, match.isEmpty ? "global" : match, rms > 0 ? 1 : 0))
}
