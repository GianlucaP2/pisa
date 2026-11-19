import Foundation
@testable import PisaAGUI

enum Fixtures {
    static let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("fixtures")
    static func lines(_ rel: String) throws -> [String] {
        try String(contentsOf: root.appendingPathComponent(rel), encoding: .utf8).split(separator: "\n").map(String.init)
    }
    static func frames(_ name: String) throws -> [String] { try lines("streams/\(name).jsonl") }
    static func json(_ rel: String) throws -> JSONValue { try JSONValue.parse(String(contentsOf: root.appendingPathComponent(rel), encoding: .utf8)) }
}

/// A transport that plays scripted connections: each connection answers the hello with its frames, then closes.
final class ScriptedTransport: LiveTransport, @unchecked Sendable {
    final class Conn: LiveConnection, @unchecked Sendable {
        var outgoing: [String]
        var sent: [String] = []
        init(_ frames: [String]) { outgoing = frames }
        func send(_ text: String) async throws { sent.append(text) }
        func receive() async throws -> String {
            guard !outgoing.isEmpty else { throw LiveClient.Failure.closed }
            return outgoing.removeFirst()
        }
        func close() {}
    }
    private var scripts: [[String]]
    private(set) var connections: [Conn] = []
    init(_ scripts: [[String]]) { self.scripts = scripts }
    func connect() async throws -> LiveConnection {
        guard !scripts.isEmpty else { throw URLError(.cannotConnectToHost) }
        let c = Conn(scripts.removeFirst())
        connections.append(c)
        return c
    }
}

final class SleepLog: @unchecked Sendable {
    var delays: [Double] = []
}

func welcome(seq: Int, replayed: Bool = false) -> String {
    #"{"control":"welcome","seq":\#(seq),"sessionId":"sess-1","contract":"2.0","agui":"1.0","a2ui":{"version":"1.0","catalogIds":["urn:pisa:a2ui:catalog:core:1"]},"mcpApps":{"protocolVersion":"2026-01-26"},"rings":[0,1,2],"extensions":{"pisa.voice":1},"replayed":\#(replayed)}"#
}

let device = Hello.Device(deviceId: "00000000-0000-4000-8000-0000000000d1", appVersion: "2.0.0")
