import Foundation

// The talk data channel's gaps closed in contract 2: G4 and G5.

/// G4: how a talk ended, carried in the talk's last `RUN_FINISHED.result` (replaces v1 `talk.ended`).
public struct TalkEndedResult: Codable, Equatable, Sendable {
    public enum Reason: String, Codable, Sendable {
        case idle, owner, budget, error, fallback
        /// The gated talk's route changed: reopen it at once on the new route, with no error shown.
        case route
    }
    public var talkEnded: Reason
    public init(talkEnded: Reason) { self.talkEnded = talkEnded }

    /// The reason in a run's result, or nil when the result is not a talk's end (or a reason this app does not know).
    public init?(result: JSONValue?) {
        guard let r = result, case .object(let o) = r, o.count == 1, let s = o["talkEnded"]?.string, let reason = Reason(rawValue: s) else { return nil }
        talkEnded = reason
    }
}

/// G5: the voice enrolment exchange (VOICE-ID-1). It runs on the data channel of a talk session opened with
/// purpose "enrol", outside any AG-UI run: server frames are `{"seq", "event": CUSTOM}` with no threadId.
public enum Enrol {
    public struct BySnr: Codable, Equatable, Sendable {
        public var clean: Double; public var plus10: Double; public var plus5: Double; public var zero: Double
        enum CodingKeys: String, CodingKey { case clean, plus10 = "10", plus5 = "5", zero = "0" }
    }
    public struct Quality: Codable, Equatable, Sendable {
        public var block: String; public var voicedS: Double; public var snrDb: Double; public var clipped: Bool; public var ok: Bool; public var hint: String?
        enum CodingKeys: String, CodingKey { case block, voicedS, snrDb, clipped, ok, hint }
        public func encode(to encoder: Encoder) throws {   // hint is always present (null when there is none)
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(block, forKey: .block); try c.encode(voicedS, forKey: .voicedS); try c.encode(snrDb, forKey: .snrDb)
            try c.encode(clipped, forKey: .clipped); try c.encode(ok, forKey: .ok); try c.encode(hint, forKey: .hint)
        }
    }
    public struct TestResult: Codable, Equatable, Sendable {
        public var acceptedOwner: Int; public var ownerTotal: Int; public var acceptedStrangers: Int; public var strangersTotal: Int
        public var ok: Bool; public var bySnr: BySnr
    }
    public struct Done: Codable, Equatable, Sendable {
        public var route: String; public var ok: Bool
        public var ownerFalseReject: Double?; public var strangersPerMin: Double?; public var bySnr: BySnr?
        public var reason: String?; public var missing: [String]?
        /// The reason as the closed set of contract 2 (nil when ok, or a reason this app does not know).
        public var reasonCode: Reason? { reason.flatMap(Reason.init(rawValue:)) }
    }

    /// Why an enrolment ended with ok false (contract 2 `EnrolReason`).
    public enum Reason: String, Codable, Sendable, CaseIterable {
        /// Blocks still needed: see `Done.missing`.
        case missing
        /// Voice consent withdrawn meanwhile: nothing stored.
        case consent
        /// No usable speech in the blocks.
        case noSpeech = "no_speech"
        /// The 15-minute limit.
        case timeout
        /// Prints were made and stored, but the route's gate cannot rely on them: the report is sent.
        case notReliable = "not_reliable"
        /// The server failed handling the session.
        case error
    }

    public enum ServerEvent: Equatable, Sendable {
        case quality(Quality), testResult(TestResult), done(Done)
        case unknown(name: String, value: JSONValue)

        public static let names = ["pisa.voice.enrol.quality", "pisa.voice.enrol.test_result", "pisa.voice.enrol.done"]

        public init(_ custom: Custom) throws {
            let v = custom.value
            switch custom.name {
            case "pisa.voice.enrol.quality": self = .quality(try v.decode(Quality.self))
            case "pisa.voice.enrol.test_result": self = .testResult(try v.decode(TestResult.self))
            case "pisa.voice.enrol.done": self = .done(try v.decode(Done.self))
            default: self = .unknown(name: custom.name, value: v)
            }
        }

        public var custom: Custom {
            func c<T: Encodable>(_ n: String, _ v: T) -> Custom { Custom(name: n, value: (try? JSONValue.from(v)) ?? .null) }
            switch self {
            case .quality(let v): return c("pisa.voice.enrol.quality", v)
            case .testResult(let v): return c("pisa.voice.enrol.test_result", v)
            case .done(let v): return c("pisa.voice.enrol.done", v)
            case .unknown(let n, let v): return Custom(name: n, value: v)
            }
        }
    }

    /// A server frame on the enrolment channel. A frame with a threadId belongs to /v2/live, not here.
    public struct ServerFrame: Decodable, Equatable, Sendable {
        public var seq: Int
        public var event: ServerEvent
        enum K: String, CodingKey { case seq, event, threadId, runId }
        public struct NotAnEnrolFrame: Error {}
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: K.self)
            guard !c.contains(.threadId), !c.contains(.runId) else { throw NotAnEnrolFrame() }
            seq = try c.decode(Int.self, forKey: .seq)
            let ev = try c.decode(AGUIEvent.self, forKey: .event)
            guard case .custom(let custom) = ev.payload else { throw NotAnEnrolFrame() }
            event = try ServerEvent(custom)
        }
    }

    /// App -> server on the enrolment channel: `{"op": "custom", name, value}`.
    public enum ClientFrame: Encodable, Equatable, Sendable {
        case block(String, start: Bool)
        case test(start: Bool)
        case finish, cancel

        enum K: String, CodingKey { case op, name, value }
        public var name: String {
            switch self {
            case .block: return "pisa.voice.enrol.block"
            case .test: return "pisa.voice.enrol.test"
            case .finish: return "pisa.voice.enrol.finish"
            case .cancel: return "pisa.voice.enrol.cancel"
            }
        }
        public var value: JSONValue {
            switch self {
            case .block(let b, let start): return .object(["block": .string(b), "state": .string(start ? "start" : "end")])
            case .test(let start): return .object(["state": .string(start ? "start" : "end")])
            case .finish, .cancel: return .object([:])
            }
        }
        public func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: K.self)
            try c.encode("custom", forKey: .op); try c.encode(name, forKey: .name); try c.encode(value, forKey: .value)
        }
    }
}
