import XCTest
@testable import PisaAGUI

/// The gaps closed in package A (MAPPING.md §6), read from the shared fixtures the Python kit checks.
final class GapTests: XCTestCase {
    func testG4TalkEndedIsTheLastRunsResult() throws {
        let frames = try Fixtures.frames("talk_gated").map { try Wire.decoder.decode(ServerFrame.self, from: Data($0.utf8)) }
        guard case .event(_, _, _, let last) = frames.last!, case .runFinished(let fin) = last.payload else { return XCTFail("no RUN_FINISHED") }
        XCTAssertEqual(TalkEndedResult(result: fin.result)?.talkEnded, .owner)
        XCTAssertNil(TalkEndedResult(result: .object(["talkEnded": .string("bored")])))
        XCTAssertNil(TalkEndedResult(result: nil))
        for name in ["g4_talk_ended_owner", "g4_talk_ended_route", "g4_talk_ended_fallback"] {
            let fx = try Fixtures.json("gaps/valid/\(name).json")
            XCTAssertNotNil(TalkEndedResult(result: fx["frame"]?["event"]?["result"]), name)
        }
    }

    func testG5VoiceGateDecodesInTheTalk() throws {
        let gates = try Fixtures.frames("talk_gated").compactMap { line -> String? in
            guard case .event(_, _, _, let ev) = try Wire.decoder.decode(ServerFrame.self, from: Data(line.utf8)),
                  case .custom(let c) = ev.payload, case .voiceGate(let g) = try PisaServerEvent(c) else { return nil }
            return g.heard
        }
        XCTAssertEqual(gates, ["owner", "other"])
    }

    func testG5EnrolmentFramesDecodeAndRoundTrip() throws {
        for file in ["server_ok", "server_missing"] {
            var seq = 0
            for line in try Fixtures.lines("enrol/\(file).jsonl") {
                let f = try Wire.decoder.decode(Enrol.ServerFrame.self, from: Data(line.utf8))
                seq += 1
                XCTAssertEqual(f.seq, seq)
                if case .unknown(let n, _) = f.event { XCTFail("unknown \(n)") }
                let original = try JSONValue.parse(line)["event"]!["value"]!
                XCTAssertEqual(f.event.custom.value, original, file)
            }
        }
        let done = try Fixtures.lines("enrol/server_missing.jsonl").last!
        guard case .done(let d) = try Wire.decoder.decode(Enrol.ServerFrame.self, from: Data(done.utf8)).event else { return XCTFail() }
        XCTAssertEqual(d.reason, "missing")
        XCTAssertEqual(d.missing, ["B", "C", "D"])
    }

    func testG5EnrolFrameWithAThreadIsRefused() throws {
        let fx = try Fixtures.json("gaps/invalid/g5_enrol_frame_with_thread.json")
        let data = try Wire.encoder.encode(fx["frame"]!)
        XCTAssertThrowsError(try Wire.decoder.decode(Enrol.ServerFrame.self, from: data))
    }

    func testG5EnrolClientFramesMatchTheRecordedSession() throws {
        var sent: [Enrol.ClientFrame] = []
        for b in ["A", "B", "C", "D", "E"] { sent += [.block(b, start: true), .block(b, start: false)] }
        sent += [.test(start: true), .test(start: false), .finish]
        let recorded = try Fixtures.lines("enrol/client_session.jsonl").map { try JSONValue.parse($0) }
        XCTAssertEqual(try sent.map { try JSONValue.from($0) }, recorded)
    }

    func testG2RoutesAndG6ExpiryAreCarried() throws {
        let route = try Fixtures.json("gaps/valid/g2_presence_route_airpods.json")
        XCTAssertEqual(route["frame"]?["value"]?["state"]?["route"]?.string, "airpods")
        let surface = try Fixtures.json("gaps/valid/g6_surface_expires.json")
        let data = try Wire.encoder.encode(surface["frame"]!)
        guard case .event(_, _, _, let ev) = try Wire.decoder.decode(ServerFrame.self, from: data),
              case .activitySnapshot(let s) = ev.payload else { return XCTFail() }
        XCTAssertNotNil(s.content["pisa"]?["expiresAt"]?.string)
    }

    func testEnrolReasonsAreAClosedSetWithAFixtureEach() throws {
        XCTAssertEqual(Enrol.Reason.allCases.map(\.rawValue), ["missing", "consent", "no_speech", "timeout", "not_reliable", "error"])
        for reason in Enrol.Reason.allCases {
            let fx = try Fixtures.json("gaps/valid/enrol_reason_\(reason.rawValue).json")
            let f = try Wire.decoder.decode(Enrol.ServerFrame.self, from: try Wire.encoder.encode(fx["frame"]!))
            guard case .done(let d) = f.event else { return XCTFail(reason.rawValue) }
            XCTAssertEqual(d.reasonCode, reason)
            if reason == .notReliable { XCTAssertNotNil(d.bySnr?.clean) }
        }
    }
}

