// audiolevel — measure whether audio SIGNAL is flowing, without hearing it.
//
// Uses the macOS 14.4+ CoreAudio process-tap API to tap a process's audio output
// (a target process by bundle-id substring, default "ableton" — passed as arg 2 — so it
// device is routed), accumulate mean RMS over a short window, and print it. The rig
// tool compares it to a threshold: > threshold = sound is passing, ~0 = silence.
//
//   audiolevel [seconds] [bundle-id-substring]
//   audiolevel 1.5 ableton      # 1.5s window, tap processes matching 'ableton' (else global)
//
// Prints one line:  RMS <mean> TARGET <what-was-tapped> FRAMES <n>
// Exit 0 always (RMS 0 on failure), so the caller can treat "no signal" uniformly.

import CoreAudio
import Foundation

let seconds = CommandLine.arguments.count > 1 ? (Double(CommandLine.arguments[1]) ?? 1.5) : 1.5
let match = (CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "ableton").lowercased()

func fail(_ target: String) -> Never { print("RMS 0.000000 TARGET \(target) FRAMES 0"); exit(0) }

// Find audio processes whose bundle id contains `match`.
func matchingProcesses(_ needle: String) -> [AudioObjectID] {
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

let procs = matchingProcesses(match)
let target = procs.isEmpty ? "global" : match
let desc = procs.isEmpty
    ? CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    : CATapDescription(stereoMixdownOfProcesses: procs)
desc.name = "rig-audiolevel-tap"
desc.isPrivate = true

var tapID = AudioObjectID(kAudioObjectUnknown)
if AudioHardwareCreateProcessTap(desc, &tapID) != noErr { fail(target) }

let aggDict: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: "rig-audiolevel-agg",
    kAudioAggregateDeviceUIDKey as String: "rig-audiolevel-\(UUID().uuidString)",
    kAudioAggregateDeviceIsPrivateKey as String: true,
    kAudioAggregateDeviceTapAutoStartKey as String: true,
    kAudioAggregateDeviceTapListKey as String: [[kAudioSubTapUIDKey as String: desc.uuid.uuidString]],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
if AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggID) != noErr {
    AudioHardwareDestroyProcessTap(tapID); fail(target)
}

var sumSq: Double = 0
var frames: Int = 0
let lock = NSLock()
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
    lock.lock(); sumSq += ls; frames += ln; lock.unlock()
}
if cst != noErr || procID == nil {
    AudioHardwareDestroyAggregateDevice(aggID); AudioHardwareDestroyProcessTap(tapID); fail(target)
}

AudioDeviceStart(aggID, procID!)
Thread.sleep(forTimeInterval: seconds)
AudioDeviceStop(aggID, procID!)
AudioDeviceDestroyIOProcID(aggID, procID!)
AudioHardwareDestroyAggregateDevice(aggID)
AudioHardwareDestroyProcessTap(tapID)

lock.lock(); let ss = sumSq; let fr = frames; lock.unlock()
let rms = fr > 0 ? (ss / Double(fr)).squareRoot() : 0
print(String(format: "RMS %.6f TARGET %@ FRAMES %d", rms, target, fr))
