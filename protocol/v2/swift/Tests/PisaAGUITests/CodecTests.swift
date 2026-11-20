import XCTest
@testable import PisaAGUI

final class CodecTests: XCTestCase {
    func testEveryAGUI10EventTypeRoundTrips() throws {
        let lines = try Fixtures.lines("events/agui_all_types.jsonl")
        var types = Set<String>()
        for line in lines {
            let ev = try Wire.decoder.decode(AGUIEvent.self, from: Data(line.utf8))
            if case .unknown = ev.payload { XCTFail("decoded as unknown: \(line)") }
            types.insert(ev.type)
            let back = try JSONValue.parse(Wire.string(ev))
            XCTAssertEqual(back, try JSONValue.parse(line), line)
        }
        XCTAssertEqual(types, AGUIEvent.knownTypes)
        XCTAssertEqual(types.count, 31)
    }

    func testEveryPisaServerEventDecodesAndRoundTrips() throws {
        var names = Set<String>()
        for line in try Fixtures.lines("events/pisa_server_events.jsonl") {
            let ev = try Wire.decoder.decode(AGUIEvent.self, from: Data(line.utf8))
            guard case .custom(let c) = ev.payload else { return XCTFail("not CUSTOM") }
            let p = try PisaServerEvent(c)
            if case .unknown = p { XCTFail("unknown \(c.name)") }
            names.insert(c.name)
            XCTAssertEqual(p.custom.value, c.value, c.name)
        }
        XCTAssertEqual(names, Set(PisaServerEvent.names))
    }

    func testLockScreenPreviewIsGenericForHealthAndFinanceByDefault() throws {
        let health = try JSONValue.parse(#"{"noticeId":"44444444-4444-4444-8444-444444444444","kind":"reminder","priority":"normal","lang":"en-US","text":"Take the evening medicine","privacy":"health","genericText":"A reminder from Pisa"}"#).decode(PisaServerEvent.Notice.self)
        XCTAssertEqual(health.preview(fullPreviewsAllowedFor: []), "A reminder from Pisa")
        XCTAssertEqual(health.preview(fullPreviewsAllowedFor: ["health"]), "Take the evening medicine")
        var plain = health; plain.privacy = "none"
        XCTAssertEqual(plain.preview(fullPreviewsAllowedFor: []), "Take the evening medicine")
        var place = plain; place.sensitive = true
        XCTAssertEqual(place.preview(fullPreviewsAllowedFor: ["health", "finance"]), "A reminder from Pisa")
    }

    func testNewerEventTypeIsToleratedAndKeptVerbatim() throws {
        let line = #"{"type":"FUTURE_EVENT","timestamp":1,"anything":{"a":[1,2]}}"#
        let ev = try Wire.decoder.decode(AGUIEvent.self, from: Data(line.utf8))
        guard case .unknown = ev.payload else { return XCTFail() }
        XCTAssertEqual(try JSONValue.parse(Wire.string(ev)), try JSONValue.parse(line))
        let custom = try PisaServerEvent(Custom(name: "pisa.future.thing", value: .object([:])))
        XCTAssertEqual(custom, .unknown(name: "pisa.future.thing", value: .object([:])))
    }

    func testEveryRecordedStreamFrameDecodes() throws {
        for name in ["turn_text", "turn_places", "approval", "cancelled", "proactive", "drawings", "today_section", "unknown_component"] {
            for line in try Fixtures.frames(name) {
                let f = try Wire.decoder.decode(ServerFrame.self, from: Data(line.utf8))
                guard case .event(_, _, _, let ev) = f else { return XCTFail("not an event frame") }
                if case .unknown = ev.payload { XCTFail("\(name): unknown \(ev.type)") }
                if case .custom(let c) = ev.payload, case .unknown = try PisaServerEvent(c) { XCTFail("\(name): \(c.name)") }
            }
        }
    }

    func testClientFramesHaveTheSchemaShape() throws {
        let hello = try JSONValue.parse(Wire.string(ClientFrame.hello(.current(device: device))))
        XCTAssertEqual(hello["op"], .string("hello"))
        XCTAssertEqual(hello["a2ui"]?["1.0"]?["supportedCatalogIds"], .array([.string("urn:pisa:a2ui:catalog:core:1")]))
        XCTAssertEqual(hello["rings"], .array([.number(0), .number(1), .number(2)]))
        let presence = try JSONValue.parse(Wire.string(ClientFrame.custom(.presence(event: "earbuds_connected", state: .init(foreground: false, route: "earbuds")))))
        XCTAssertEqual(presence, try JSONValue.parse(#"{"op":"custom","name":"pisa.presence","value":{"event":"earbuds_connected","state":{"foreground":false,"route":"earbuds"}}}"#))
        let cancel = try JSONValue.parse(Wire.string(ClientFrame.cancel(runId: "run-1")))
        XCTAssertEqual(cancel, .object(["op": .string("cancel"), "runId": .string("run-1")]))
    }

    func testApprovalCanonicalIsByteIdenticalToTheServer() throws {
        let fx = try Fixtures.json("approval_canonical.json")
        let interrupt = try fx["interrupt"]!.decode(Interrupt.self)
        let a = try XCTUnwrap(ApprovalInterrupt(interrupt))
        let mine = a.canonical(device: fx["device"]!.string!, nonce: fx["nonce"]!.string!, expiresAt: fx["expiresAt"]!.string!)
        XCTAssertEqual(Array(mine.utf8), Array(fx["canonical"]!.string!.utf8))
        XCTAssertFalse(mine.contains("Dinner"))
        XCTAssertFalse(a.challengeMatches(mine + "\nsummary=x", device: fx["device"]!.string!, nonce: fx["nonce"]!.string!, expiresAt: fx["expiresAt"]!.string!))
        XCTAssertEqual(a.interrupt.message, "Send an email · notifies 1")
    }

    func testSafetyFeaturesAreAlwaysOnAndPairingsAreKeyedByServerIdAndHost() throws {
        let dir = Fixtures.root.appendingPathComponent("discovery")
        for name in ["minimal_profile", "full_example"] {
            let d = try Wire.decoder.decode(ServerDiscovery.self, from: Data(contentsOf: dir.appendingPathComponent("valid/\(name).json"))
                .jsonDocument())
            XCTAssertEqual(d.verdict(), .compatible, name)
            XCTAssertEqual(d.missingSafetyFeatures, [], name)
        }
        for name in ["no_stop", "no_signed_approvals", "stop_paywalled", "approvals_deprecated", "no_features"] {
            let d = try Wire.decoder.decode(ServerDiscovery.self, from: Data(contentsOf: dir.appendingPathComponent("invalid/\(name).json"))
                .jsonDocument())
            XCTAssertEqual(d.verdict(), .updateServer, name)
        }
        XCTAssertThrowsError(try Wire.decoder.decode(ServerDiscovery.self, from: Data(contentsOf: dir.appendingPathComponent("invalid/no_server_id.json")).jsonDocument()))
        let d = try Wire.decoder.decode(ServerDiscovery.self, from: Data(contentsOf: dir.appendingPathComponent("valid/minimal_profile.json")).jsonDocument())
        XCTAssertEqual(d.pairingKey(host: "Pisa.Example"), .init(serverId: "srv_minimal0001_Hc8Wq3Ld5Rv", host: "pisa.example"))
        XCTAssertNotEqual(d.pairingKey(host: "pisa.example"), d.pairingKey(host: "192.0.2.10"))
        let gate = FeatureGate(offered: [], entitlementLevels: ["pisa.pro"], active: nil)
        XCTAssertEqual(gate.visibility("stop"), .shown)
        XCTAssertEqual(gate.visibility("approvals.signed"), .shown)
    }

    func testDiscoveryVerdictAndFeatureGate() throws {
        let url = Fixtures.root.deletingLastPathComponent()
            .appendingPathComponent("examples/server.json")
        let d = try Wire.decoder.decode(ServerDiscovery.self, from: Data(contentsOf: url))
        XCTAssertEqual(d.verdict(), .compatible)
        XCTAssertEqual(d.verdict(appSpeaks: ["1.0"]), .updateApp)
        XCTAssertEqual(d.verdict(appSpeaks: ["3.0"]), .updateServer)
        let levels = ["pisa.plus", "pisa.pro"]
        XCTAssertEqual(FeatureGate(offered: d.features, entitlementLevels: levels, active: nil).visibility("push.relay"), .paywalled(level: "pisa.pro"))
        XCTAssertEqual(FeatureGate(offered: d.features, entitlementLevels: levels, active: "pisa.plus").visibility("push.relay"), .paywalled(level: "pisa.pro"))
        XCTAssertEqual(FeatureGate(offered: d.features, entitlementLevels: levels, active: "pisa.pro").visibility("push.relay"), .shown)
        XCTAssertEqual(FeatureGate(offered: d.features, entitlementLevels: levels, active: "pisa.pro").visibility("today"), .shown)
        XCTAssertEqual(FeatureGate(offered: d.features, entitlementLevels: levels, active: "pisa.pro").visibility("not.offered"), .hidden)
    }
}

private extension Data {
    /// A discovery fixture wraps the document: {"why", "document"}.
    func jsonDocument() throws -> Data {
        guard case .object(let o) = try JSONValue.parse(String(decoding: self, as: UTF8.self)), let doc = o["document"] else { return self }
        return try Wire.encoder.encode(doc)
    }
}
