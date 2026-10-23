// Bluetooth Classic RFCOMM bridge. stdin: raw bytes; stdout: JSON events.
// Packaged source, compiled locally against Apple's IOBluetooth framework.
import Foundation
import IOBluetooth
import Darwin

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data + Data([10]))
}

func fail(_ message: String) -> Never {
    emit(["type": "error", "message": message])
    exit(1)
}

func info(_ device: IOBluetoothDevice) -> [String: Any] {
    return ["name": device.name ?? "Unnamed", "address": device.addressString ?? "", "connected": device.isConnected()]
}

final class Bridge: NSObject, IOBluetoothRFCOMMChannelDelegate {
    var channel: IOBluetoothRFCOMMChannel?
    var sdpDone = false
    var sdpStatus: IOReturn?
    var openStatus: IOReturn?
    var closing = false
    var closeDone = false

    @objc(sdpQueryComplete:status:)
    func sdpQueryComplete(_ device: IOBluetoothDevice!, status: IOReturn) {
        sdpStatus = status
        sdpDone = true
    }

    func rfcommChannelData(_ rfcommChannel: IOBluetoothRFCOMMChannel!, data pointer: UnsafeMutableRawPointer!, length: Int) {
        emit(["type": "data", "base64": Data(bytes: pointer, count: length).base64EncodedString()])
    }

    func rfcommChannelClosed(_ rfcommChannel: IOBluetoothRFCOMMChannel!) {
        closeDone = true
        if !closing { fail("Bluetooth connection closed; recording has a capture gap.") }
    }

    func rfcommChannelOpenComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, status error: IOReturn) {
        openStatus = error
    }

    func cachedChannel(_ device: IOBluetoothDevice, uuid: IOBluetoothSDPUUID) -> BluetoothRFCOMMChannelID? {
        guard let service = device.getServiceRecord(for: uuid) else { return nil }
        var channelID: BluetoothRFCOMMChannelID = 0
        guard service.getRFCOMMChannelID(&channelID) == kIOReturnSuccess, channelID > 0 else { return nil }
        return channelID
    }

    func resolveChannel(_ device: IOBluetoothDevice, uuid: IOBluetoothSDPUUID) -> (BluetoothRFCOMMChannelID, String) {
        // macOS already discovers SPP while pairing. A fresh asynchronous query
        // must not be a prerequisite when the cached record is usable.
        if let channelID = cachedChannel(device, uuid: uuid) { return (channelID, "cached_sdp") }
        let status = device.performSDPQuery(self)
        guard status == kIOReturnSuccess else { fail("Cannot start service lookup (\(status)). Check Bluetooth permission, power and pairing.") }
        let deadline = Date(timeIntervalSinceNow: 20)
        while !sdpDone && Date() < deadline {
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.02))
            // Some OS versions populate the cache before delivering the callback.
            if let channelID = cachedChannel(device, uuid: uuid) { return (channelID, "fresh_sdp") }
        }
        guard sdpDone else {
            fail("Serial service lookup timed out: no completion callback received. Ensure MX+ is powered and unused by other apps.")
        }
        guard sdpStatus == kIOReturnSuccess else { fail("Serial service lookup failed (\(sdpStatus!)). Ensure MX+ is powered and unused by other apps.") }
        guard let channelID = cachedChannel(device, uuid: uuid) else { fail("Device does not advertise a usable Bluetooth Serial Port service.") }
        return (channelID, "fresh_sdp")
    }

    func connect(_ device: IOBluetoothDevice) {
        let uuid = IOBluetoothSDPUUID.uuid16(0x1101)!
        let (channelID, serviceSource) = resolveChannel(device, uuid: uuid)
        emit(["type": "status", "stage": "opening_rfcomm", "channel": Int(channelID), "service_source": serviceSource])
        let started = device.openRFCOMMChannelAsync(&channel, withChannelID: channelID, delegate: self)
        guard started == kIOReturnSuccess else { fail("Cannot start RFCOMM connection (\(started)). Close other OBD apps and try again.") }
        // macOS may need roughly 19 seconds to report three failed baseband
        // attempts. Do not terminate just before its real error callback.
        let deadline = Date(timeIntervalSinceNow: 25)
        while openStatus == nil && Date() < deadline {
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.02))
        }
        guard let opened = openStatus else { fail("Bluetooth data connection timed out; pairing exists but the adapter did not establish an RFCOMM link. Check range, power and other connected hosts.") }
        guard opened == kIOReturnSuccess, channel != nil else { fail("Cannot open RFCOMM channel (\(opened)). Close other OBD apps and try again.") }
        emit(["type": "ready", "device": info(device), "channel": Int(channelID), "service_source": serviceSource])
    }

    func run() {
        // Keep all IOBluetooth calls and callbacks on the main run loop.
        let input = FileHandle.standardInput.fileDescriptor
        let currentFlags = fcntl(input, F_GETFL)
        _ = fcntl(input, F_SETFL, currentFlags | O_NONBLOCK)
        var buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            let count = Darwin.read(input, &buffer, buffer.count)
            if count > 0 {
                let status = buffer.withUnsafeMutableBytes {
                    channel!.writeSync($0.baseAddress, length: UInt16(count))
                }
                if status != kIOReturnSuccess { fail("RFCOMM write failed (\(status)).") }
            } else if count == 0 {
                closing = true
                _ = channel?.close()
                let deadline = Date(timeIntervalSinceNow: 1)
                while !closeDone && Date() < deadline {
                    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01))
                }
                return
            } else if errno != EAGAIN && errno != EINTR {
                fail("Cannot read bridge input.")
            }
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.002))
        }
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
let devices = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
if arguments.first == "list" {
    let all = arguments.contains("--all")
    emit(["type": "devices", "devices": devices.filter { all || ($0.name ?? "").localizedCaseInsensitiveContains("OBD") }.map(info)])
} else if arguments.count == 2 && arguments[0] == "connect" {
    let query = arguments[1]
    let matches = devices.filter {
        ($0.name ?? "").caseInsensitiveCompare(query) == .orderedSame ||
        ($0.addressString ?? "").replacingOccurrences(of: ":", with: "-").caseInsensitiveCompare(query.replacingOccurrences(of: ":", with: "-")) == .orderedSame
    }
    guard matches.count == 1 else { fail("No unique paired device matches '\(query)'. Pair in System Settings > Bluetooth, then run devices.") }
    let bridge = Bridge()
    bridge.connect(matches[0])
    bridge.run()
} else {
    fail("Usage: BluetoothBridge list [--all] | connect <paired-device-name-or-address>")
}
