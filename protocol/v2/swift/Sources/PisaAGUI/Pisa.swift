import Foundation

// MARK: - Frames on /v2/live (CONTRACT-2 §3)

/// Server -> app. An AG-UI event of one thread's stream, or a control frame.
public enum ServerFrame: Decodable, Equatable, Sendable {
    case event(seq: Int, threadId: String, runId: String?, event: AGUIEvent)
    case welcome(Welcome)
    case pong(seq: Int)
    case error(seq: Int, code: String, message: String, retryable: Bool)

    enum K: String, CodingKey { case seq, threadId, runId, event, control, code, message, retryable }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        if let control = try c.decodeIfPresent(String.self, forKey: .control) {
            switch control {
            case "welcome": self = .welcome(try Welcome(from: decoder))
            case "pong": self = .pong(seq: try c.decode(Int.self, forKey: .seq))
            case "error":
                self = .error(seq: try c.decode(Int.self, forKey: .seq), code: try c.decode(String.self, forKey: .code),
                              message: try c.decode(String.self, forKey: .message), retryable: try c.decode(Bool.self, forKey: .retryable))
            default: throw DecodingError.dataCorruptedError(forKey: .control, in: c, debugDescription: "unknown control \(control)")
            }
            return
        }
        self = .event(seq: try c.decode(Int.self, forKey: .seq), threadId: try c.decode(String.self, forKey: .threadId),
                      runId: try c.decodeIfPresent(String.self, forKey: .runId), event: try c.decode(AGUIEvent.self, forKey: .event))
    }

    public var seq: Int {
        switch self {
        case .event(let s, _, _, _), .pong(let s), .error(let s, _, _, _): return s
        case .welcome(let w): return w.seq
        }
    }
}

/// App -> server.
public enum ClientFrame: Encodable, Equatable, Sendable {
    case hello(Hello)
    case run(RunAgentInput)
    case cancel(runId: String)
    case a2ui(JSONValue)
    case custom(PisaClientEvent)
    case ping

    enum K: String, CodingKey { case op, input, runId, message, name, value }
    public func encode(to encoder: Encoder) throws {
        if case .hello(let h) = self { try h.encode(to: encoder); return }
        var c = encoder.container(keyedBy: K.self)
        switch self {
        case .hello: break
        case .run(let i): try c.encode("run", forKey: .op); try c.encode(i, forKey: .input)
        case .cancel(let r): try c.encode("cancel", forKey: .op); try c.encode(r, forKey: .runId)
        case .a2ui(let m): try c.encode("a2ui", forKey: .op); try c.encode(m, forKey: .message)
        case .custom(let e): try c.encode("custom", forKey: .op); try c.encode(e.name, forKey: .name); try c.encode(e.value, forKey: .value)
        case .ping: try c.encode("ping", forKey: .op)
        }
    }
}

// MARK: - Negotiation (CONTRACT-2 §4)

public struct Hello: Codable, Equatable, Sendable {
    public struct Versions: Codable, Equatable, Sendable { public var versions: [String] }
    public struct AGUIVersions: Codable, Equatable, Sendable { public var protocolVersions: [String] }
    public struct MCPApps: Codable, Equatable, Sendable { public var protocolVersions: [String]; public var mimeTypes: [String]; public var bundledLibraries: [String]? }
    public struct Device: Codable, Equatable, Sendable {
        public var deviceId: String; public var appVersion: String; public var platform: String
        public var surfaces: [String]?; public var audioFormats: [String]?; public var tz: String?; public var region: String?; public var hourCycle: String?
        public init(deviceId: String, appVersion: String, platform: String = "ios") { self.deviceId = deviceId; self.appVersion = appVersion; self.platform = platform }
    }
    public struct Resume: Codable, Equatable, Sendable { public var sessionId: String; public var lastSeq: Int }

    public var op = "hello"
    public var contract: Versions
    public var agui: AGUIVersions
    public var a2ui: JSONValue?
    public var mcpApps: MCPApps?
    public var rings: [Int]
    public var extensions: [String: Int]
    public var device: Device
    public var entitlements: [String]?
    public var resume: Resume?

    public static let catalogV1 = "urn:pisa:a2ui:catalog:core:1"

    /// What this build of the app speaks.
    public static func current(device: Device, entitlements: [String] = []) -> Hello {
        Hello(contract: .init(versions: ["2.0"]), agui: .init(protocolVersions: ["1.0"]),
              a2ui: .object(["1.0": .object(["supportedCatalogIds": .array([.string(catalogV1)])])]),
              mcpApps: .init(protocolVersions: ["2026-01-26"], mimeTypes: ["text/html;profile=mcp-app"],
                             bundledLibraries: ["d3@7.9.0", "mermaid@12.2.0", "vega@6.4.0", "vega-lite@6.5.0", "vega-embed@7.3.0", "vega-interpreter@2.3.2"]),
              rings: [0, 1, 2],
              extensions: ["pisa.voice": 1, "pisa.presence": 1, "pisa.location": 1, "pisa.notice": 1, "pisa.directions": 1,
                           "pisa.call": 1, "pisa.compose": 1, "pisa.approval": 1, "pisa.surface": 1],
              device: device, entitlements: entitlements.isEmpty ? nil : entitlements, resume: nil)
    }
}

public struct Feature: Codable, Equatable, Sendable {
    public var id: String; public var version: Int; public var entitlement: String?
    public struct Deprecated: Codable, Equatable, Sendable { public var sunset: String; public var replacement: String? }
    public var deprecated: Deprecated?
}

public struct Welcome: Codable, Equatable, Sendable {
    public struct A2UIPick: Codable, Equatable, Sendable { public var version: String; public var catalogIds: [String] }
    public struct MCPPick: Codable, Equatable, Sendable { public var protocolVersion: String }
    public var seq: Int
    public var sessionId: String
    public var contract: String
    public var agui: String
    public var a2ui: A2UIPick?
    public var mcpApps: MCPPick?
    public var rings: [Int]
    public var extensions: [String: Int]
    public var features: [Feature]?
    public var languages: [String]?
    public var replayed: Bool?
}

/// GET /v1/server (D-128): what the connection guide reads before pairing.
public struct ServerDiscovery: Codable, Equatable, Sendable {
    public struct Contract: Codable, Equatable, Sendable { public var versions: [String]; public var minApp: String? }
    public struct A2UI: Codable, Equatable, Sendable { public var versions: [String]; public var catalogIds: [String] }
    public struct Readiness: Codable, Equatable, Sendable {
        public struct Check: Codable, Equatable, Sendable { public var name: String; public var ok: Bool }
        public var ready: Bool; public var checks: [Check]?
    }
    public struct Reach: Codable, Equatable, Sendable { public var scheme: String; public var network: String }
    public struct Push: Codable, Equatable, Sendable { public var modes: [String]; public var relayConfigured: Bool? }
    /// An opaque random id the server picked at install; never derived from hardware, the hostname or the owner.
    public var serverId: String
    public var product: String; public var serverVersion: String; public var contract: Contract
    public var agui: [String]; public var a2ui: A2UI; public var mcpApps: [String]; public var features: [Feature]
    public var readiness: Readiness; public var reach: Reach; public var push: Push

    public enum Verdict: Equatable, Sendable { case compatible, updateApp, updateServer }

    /// Safety features every server offers, always on: never optional, paywalled or deprecated.
    public static let requiredFeatures = ["stop", "approvals.signed"]

    /// The safety features this document lacks, or offers behind an entitlement or a sunset (a non-conformant server).
    public var missingSafetyFeatures: [String] {
        Self.requiredFeatures.filter { id in
            guard let f = features.first(where: { $0.id == id }) else { return true }
            return f.entitlement != nil || f.deprecated != nil
        }
    }

    /// Pairings are kept per server install AND host: the same install reached at two hosts is two pairings, and a
    /// reinstalled server at the same host (a new serverId) is never mistaken for the old one.
    public struct PairingKey: Hashable, Codable, Sendable { public var serverId: String; public var host: String }
    public func pairingKey(host: String) -> PairingKey { PairingKey(serverId: serverId, host: host.lowercased()) }

    /// D-128 §3: one clear screen when there is no common contract major. A server without the always-on safety
    /// features does not conform to contract 2, so it also gets "update the server".
    public func verdict(appSpeaks: [String] = ["2.0"]) -> Verdict {
        let compatible = majorVerdict(appSpeaks: appSpeaks)
        if compatible == .compatible, !missingSafetyFeatures.isEmpty { return .updateServer }
        return compatible
    }

    private func majorVerdict(appSpeaks: [String]) -> Verdict {
        let majors = { (v: [String]) in Set(v.compactMap { $0.split(separator: ".").first.map(String.init) }) }
        if !majors(appSpeaks).isDisjoint(with: majors(contract.versions)) { return .compatible }
        let appMax = appSpeaks.compactMap { Double($0) }.max() ?? 0
        let srvMax = contract.versions.compactMap { Double($0) }.max() ?? 0
        return srvMax > appMax ? .updateApp : .updateServer
    }
}

/// D-129 §4: a feature shows only if the server offers it AND (it needs no entitlement OR StoreKit grants it).
public struct FeatureGate: Sendable {
    public let offered: [Feature]
    public let entitlementLevels: [String]   // ordered, lowest first, e.g. ["pisa.plus", "pisa.pro"]
    public let active: String?               // the StoreKit level currently active, if any
    public init(offered: [Feature], entitlementLevels: [String], active: String?) { self.offered = offered; self.entitlementLevels = entitlementLevels; self.active = active }

    public enum Visibility: Equatable, Sendable { case hidden, shown, paywalled(level: String) }
    public func visibility(_ id: String) -> Visibility {
        if ServerDiscovery.requiredFeatures.contains(id) { return .shown }   // safety features are never flags
        guard let f = offered.first(where: { $0.id == id }) else { return .hidden }
        guard let need = f.entitlement else { return .shown }
        guard let needIdx = entitlementLevels.firstIndex(of: need) else { return .hidden }   // unknown level: never offer to buy it
        if let a = active, let have = entitlementLevels.firstIndex(of: a), have >= needIdx { return .shown }
        return .paywalled(level: need)
    }
}

// MARK: - pisa.* events (CONTRACT-2 §5)

public enum PisaServerEvent: Equatable, Sendable {
    public struct VoiceAudio: Codable, Equatable, Sendable {
        public var messageId: String?; public var noticeId: String?; public var seq: Int; public var format: String
        public var sampleRate: Int; public var data: String; public var final: Bool
        public var sentenceEnd: Bool?; public var complete: Bool?; public var spokenChars: Int?
    }
    public struct VoiceState: Codable, Equatable, Sendable { public var state: String }
    /// G5: whose voice the gate hears during a gated talk (owner, other, none).
    public struct VoiceGate: Codable, Equatable, Sendable { public var heard: String }
    public struct Transcript: Codable, Equatable, Sendable { public var text: String; public var final: Bool; public var ignored: String? }
    public struct TurnHint: Codable, Equatable, Sendable { public var replyTo: String; public var pEnd: Double; public var waitMs: Int }
    public struct Understood: Codable, Equatable, Sendable {
        public struct Correction: Codable, Equatable, Sendable { public var from: String; public var to: String }
        public var replyTo: String; public var meant: String; public var raw: String; public var corrections: [Correction]; public var ask: String?
        enum CodingKeys: String, CodingKey { case replyTo, meant, raw, corrections, ask }
        public func encode(to encoder: Encoder) throws {   // `ask` is required on the wire, null when J has nothing to ask
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(replyTo, forKey: .replyTo); try c.encode(meant, forKey: .meant); try c.encode(raw, forKey: .raw)
            try c.encode(corrections, forKey: .corrections); try c.encode(ask, forKey: .ask)
        }
    }
    public struct Notice: Codable, Equatable, Sendable {
        public struct CheckIn: Codable, Equatable, Sendable {
            public struct Choice: Codable, Equatable, Sendable { public var id: String; public var label: String }
            public var checkInId: String; public var choices: [Choice]; public var expectsReply: Bool?
        }
        public struct Undo: Codable, Equatable, Sendable { public var undoId: String; public var until: String }
        public var noticeId: String; public var kind: String; public var priority: String; public var urgency: String?
        public var lang: String; public var text: String; public var title: String?; public var wrist: String?
        public var voice: Bool?; public var sensitive: Bool?; public var checkIn: CheckIn?; public var undo: Undo?
        public var place: String?; public var refs: [String]?; public var surfaceId: String?
        public var privacy: String?; public var genericText: String?

        /// What the lock screen, the watch and a relay push may show (CONTRACT-2 §9.1). Health and finance are generic
        /// unless the user allowed full previews for that class; a sensitive place is always generic.
        public func preview(fullPreviewsAllowedFor allowed: Set<String>) -> String {
            let cls = privacy ?? "none"
            let generic = genericText ?? "A notice from Pisa"
            if sensitive == true { return generic }
            if (cls == "health" || cls == "finance") && !allowed.contains(cls) { return generic }
            return text
        }
    }
    public struct AIDisclosure: Codable, Equatable, Sendable {
        public struct Processor: Codable, Equatable, Sendable {
            public struct Provider: Codable, Equatable, Sendable { public var id: String; public var name: String; public var kind: String; public var region: String? }
            public var slot: String; public var provider: Provider; public var model: String; public var dataCategories: [String]; public var features: [String]?
        }
        public var revision: String; public var processors: [Processor]; public var healthRequiresSeparateConsent: Bool
    }
    public struct Endpoints: Codable, Equatable, Sendable {
        public struct Endpoint: Codable, Equatable, Sendable { public var kind: String; public var address: String; public var port: Int?; public var relayHandle: String? }
        public var revision: String; public var endpoints: [Endpoint]; public var spki: [String]
    }
    public struct AskToOpen: Codable, Equatable, Sendable { public var why: String; public var line: String; public var lang: String; public var noticeId: String? }
    public struct Directions: Codable, Equatable, Sendable { public var directionsId: String; public var name: String; public var lat: Double; public var lon: Double; public var mode: String; public var placeId: String? }
    public struct Call: Codable, Equatable, Sendable { public var callId: String; public var name: String; public var phone: String }
    public struct Compose: Codable, Equatable, Sendable {
        public struct Recipient: Codable, Equatable, Sendable { public var name: String; public var phone: String? }
        public var composeId: String; public var app: String; public var recipient: Recipient; public var text: String
    }
    public struct ActionDone: Codable, Equatable, Sendable { public var approvalId: String; public var kind: String; public var summary: String; public var result: String; public var undoUntil: String? }
    public struct ApprovalState: Codable, Equatable, Sendable { public var approvalId: String; public var interruptId: String; public var state: String; public var by: String? }
    public struct SurfaceFocus: Codable, Equatable, Sendable { public var surfaceId: String; public var mode: String; public var highlight: String? }
    public struct EntitlementRequired: Codable, Equatable, Sendable { public var feature: String; public var level: String }
    /// Contract 2.2, the feature `app.actions` (`pisa.app`): open one of the app's own screens (`target`: tab, riepilogo,
    /// task, ai_privacy, riepilogo_refresh; `id`: the tab's or the task's), then answer with `PisaClientEvent.appResult`.
    public struct AppOpen: Codable, Equatable, Sendable { public var actionId: String; public var target: String; public var id: String? }
    /// Contract 2.2, the feature `briefing.view` (`pisa.app`): these views are out of date; read them again.
    public struct AppChanged: Codable, Equatable, Sendable { public var views: [String]; public var at: String }

    case voiceAudio(VoiceAudio), voiceState(VoiceState), voiceGate(VoiceGate), transcript(Transcript), turnHint(TurnHint), understood(Understood)
    case notice(Notice), askToOpen(AskToOpen), directions(Directions), call(Call), compose(Compose)
    case actionDone(ActionDone), approvalState(ApprovalState), surfaceFocus(SurfaceFocus), entitlementRequired(EntitlementRequired)
    case aiDisclosure(AIDisclosure), endpoints(Endpoints)
    case appOpen(AppOpen), appChanged(AppChanged)
    /// A pisa.* name from a newer server, or a non-pisa CUSTOM event: tolerated, ignored by the app.
    case unknown(name: String, value: JSONValue)

    public static let names = ["pisa.voice.audio", "pisa.voice.state", "pisa.voice.gate", "pisa.voice.transcript", "pisa.voice.turn_hint", "pisa.voice.understood",
                               "pisa.notice", "pisa.ask_to_open", "pisa.directions", "pisa.call", "pisa.compose", "pisa.action_done",
                               "pisa.approval.state", "pisa.surface.focus", "pisa.entitlement.required",
                               "pisa.ai.disclosure", "pisa.endpoints", "pisa.app.open", "pisa.app.changed"]

    public init(_ custom: Custom) throws {
        let v = custom.value
        switch custom.name {
        case "pisa.voice.audio": self = .voiceAudio(try v.decode(VoiceAudio.self))
        case "pisa.voice.state": self = .voiceState(try v.decode(VoiceState.self))
        case "pisa.voice.gate": self = .voiceGate(try v.decode(VoiceGate.self))
        case "pisa.voice.transcript": self = .transcript(try v.decode(Transcript.self))
        case "pisa.voice.turn_hint": self = .turnHint(try v.decode(TurnHint.self))
        case "pisa.voice.understood": self = .understood(try v.decode(Understood.self))
        case "pisa.notice": self = .notice(try v.decode(Notice.self))
        case "pisa.ask_to_open": self = .askToOpen(try v.decode(AskToOpen.self))
        case "pisa.directions": self = .directions(try v.decode(Directions.self))
        case "pisa.call": self = .call(try v.decode(Call.self))
        case "pisa.compose": self = .compose(try v.decode(Compose.self))
        case "pisa.action_done": self = .actionDone(try v.decode(ActionDone.self))
        case "pisa.approval.state": self = .approvalState(try v.decode(ApprovalState.self))
        case "pisa.surface.focus": self = .surfaceFocus(try v.decode(SurfaceFocus.self))
        case "pisa.entitlement.required": self = .entitlementRequired(try v.decode(EntitlementRequired.self))
        case "pisa.ai.disclosure": self = .aiDisclosure(try v.decode(AIDisclosure.self))
        case "pisa.endpoints": self = .endpoints(try v.decode(Endpoints.self))
        case "pisa.app.open": self = .appOpen(try v.decode(AppOpen.self))
        case "pisa.app.changed": self = .appChanged(try v.decode(AppChanged.self))
        default: self = .unknown(name: custom.name, value: v)
        }
    }

    public var custom: Custom {
        func c<T: Encodable>(_ n: String, _ v: T) -> Custom { Custom(name: n, value: (try? JSONValue.from(v)) ?? .null) }
        switch self {
        case .voiceAudio(let v): return c("pisa.voice.audio", v)
        case .voiceState(let v): return c("pisa.voice.state", v)
        case .voiceGate(let v): return c("pisa.voice.gate", v)
        case .transcript(let v): return c("pisa.voice.transcript", v)
        case .turnHint(let v): return c("pisa.voice.turn_hint", v)
        case .understood(let v): return c("pisa.voice.understood", v)
        case .notice(let v): return c("pisa.notice", v)
        case .askToOpen(let v): return c("pisa.ask_to_open", v)
        case .directions(let v): return c("pisa.directions", v)
        case .call(let v): return c("pisa.call", v)
        case .compose(let v): return c("pisa.compose", v)
        case .actionDone(let v): return c("pisa.action_done", v)
        case .approvalState(let v): return c("pisa.approval.state", v)
        case .surfaceFocus(let v): return c("pisa.surface.focus", v)
        case .entitlementRequired(let v): return c("pisa.entitlement.required", v)
        case .aiDisclosure(let v): return c("pisa.ai.disclosure", v)
        case .endpoints(let v): return c("pisa.endpoints", v)
        case .appOpen(let v): return c("pisa.app.open", v)
        case .appChanged(let v): return c("pisa.app.changed", v)
        case .unknown(let n, let v): return Custom(name: n, value: v)
        }
    }
}

public enum PisaClientEvent: Equatable, Sendable {
    public struct PresenceState: Codable, Equatable, Sendable {
        public var foreground: Bool?; public var locked: Bool?; public var route: String?; public var glassesWorn: Bool?; public var companion: Bool?
        public init(foreground: Bool? = nil, locked: Bool? = nil, route: String? = nil) { self.foreground = foreground; self.locked = locked; self.route = route }
    }
    case presence(event: String, state: PresenceState?)
    case location(lat: Double, lon: Double, accuracyM: Double, measuredAt: String)
    case placeEnter(noteIds: [String])
    case deviceContext(tz: String?, region: String?, hourCycle: String?)
    case surfaceAck(surfaceId: String, status: String, surface: String, reason: String?)
    case surfaceEvent(surfaceId: String, event: String)
    case voicePartial(text: String, lang: String, turnId: String)
    /// Contract 2.1, the feature `talk.text`: words typed into the open talk (`id`: its session_id).
    case talkText(id: String, text: String)
    /// Contract 2.2, the feature `app.actions`: the app's report on `pisa.app.open` (`reason`: unsupported, not_found,
    /// not_now, error; only when `ok` is false).
    case appResult(actionId: String, ok: Bool, reason: String?)

    public var name: String {
        switch self {
        case .presence: return "pisa.presence"
        case .location: return "pisa.location"
        case .placeEnter: return "pisa.place_enter"
        case .deviceContext: return "pisa.device_context"
        case .surfaceAck: return "pisa.surface.ack"
        case .surfaceEvent: return "pisa.surface.event"
        case .voicePartial: return "pisa.voice.partial"
        case .talkText: return "pisa.voice.talk.text"
        case .appResult: return "pisa.app.result"
        }
    }

    public var value: JSONValue {
        var o: [String: JSONValue] = [:]
        func put(_ k: String, _ v: String?) { if let v { o[k] = .string(v) } }
        switch self {
        case .presence(let e, let s):
            o["event"] = .string(e)
            if let s, let j = try? JSONValue.from(s) { o["state"] = j }
        case .location(let lat, let lon, let acc, let at):
            o = ["lat": .number(lat), "lon": .number(lon), "accuracyM": .number(acc), "measuredAt": .string(at)]
        case .placeEnter(let ids): o["noteIds"] = .array(ids.map { .string($0) })
        case .deviceContext(let tz, let region, let hc): put("tz", tz); put("region", region); put("hourCycle", hc)
        case .surfaceAck(let id, let status, let surface, let reason):
            put("surfaceId", id); put("status", status); put("surface", surface); put("reason", reason)
        case .surfaceEvent(let id, let e): put("surfaceId", id); put("event", e)
        case .voicePartial(let t, let l, let id): put("text", t); put("lang", l); put("turnId", id)
        case .talkText(let id, let t): put("id", id); put("text", t)
        case .appResult(let id, let ok, let reason): put("actionId", id); o["ok"] = .bool(ok); put("reason", reason)
        }
        return .object(o)
    }
}

// MARK: - Approvals as interrupts (CONTRACT-2 §6)

public struct ApprovalInterrupt: Equatable, Sendable {
    public let interrupt: Interrupt
    public let approvalId: String
    public let kind: String
    public let requiresBiometric: Bool
    public let digest: String
    public let recipientsCount: Int
    public let server: String

    public init?(_ i: Interrupt) {
        guard i.reason == "pisa.approval", let p = i.metadata?["pisa"],
              let id = p["approvalId"]?.string, let kind = p["kind"]?.string, let dg = p["digest"]?.string,
              let srv = p["server"]?.string, let bio = p["requiresBiometric"]?.bool else { return nil }
        interrupt = i; approvalId = id; self.kind = kind; requiresBiometric = bio; digest = dg
        recipientsCount = Int(p["recipientsCount"]?.number ?? 0); server = srv
    }

    /// The exact bytes the Secure Enclave key signs (`pisa-approval/2`). No outside text, by construction.
    public func canonical(device: String, nonce: String, expiresAt: String) -> String {
        ["pisa-approval/2", "server=\(server)", "device=\(device)", "approval_id=\(approvalId)", "interrupt_id=\(interrupt.id)",
         "decision=approve", "kind=\(kind)", "digest=\(digest)", "recipients_count=\(recipientsCount)", "nonce=\(nonce)",
         "expires_at=\(expiresAt)"].joined(separator: "\n")
    }

    /// The phone checks the server's challenge text before Face ID: exactly our keys, our values, in order.
    public func challengeMatches(_ text: String, device: String, nonce: String, expiresAt: String) -> Bool {
        text == canonical(device: device, nonce: nonce, expiresAt: expiresAt)
    }
}
