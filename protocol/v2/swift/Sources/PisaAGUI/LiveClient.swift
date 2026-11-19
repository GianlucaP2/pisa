import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// MARK: - Transport

public protocol LiveConnection: AnyObject, Sendable {
    func send(_ text: String) async throws
    /// The next text frame; throws when the socket closes.
    func receive() async throws -> String
    func close()
}

public protocol LiveTransport: Sendable {
    func connect() async throws -> LiveConnection
}

/// URLSessionWebSocketTask over wss://<server>/v2/live, subprotocol pisa.v2, device token as Bearer.
public final class URLSessionLiveTransport: LiveTransport, @unchecked Sendable {
    let url: URL; let token: String; let session: URLSession
    public init(url: URL, token: String, session: URLSession = .shared) { self.url = url; self.token = token; self.session = session }
    public func connect() async throws -> LiveConnection {
        var req = URLRequest(url: url)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("pisa.v2", forHTTPHeaderField: "Sec-WebSocket-Protocol")
        let task = session.webSocketTask(with: req)
        task.resume()
        return Conn(task)
    }
    final class Conn: LiveConnection, @unchecked Sendable {
        let task: URLSessionWebSocketTask
        init(_ t: URLSessionWebSocketTask) { task = t }
        func send(_ text: String) async throws { try await task.send(.string(text)) }
        func receive() async throws -> String {
            switch try await task.receive() {
            case .string(let s): return s
            case .data(let d): return String(decoding: d, as: UTF8.self)   // binary audio frames: not in this spike
            @unknown default: throw LiveClient.Failure.closed
            }
        }
        func close() { task.cancel(with: .normalClosure, reason: nil) }
    }
}

// MARK: - Client

/// The app's side of /v2/live: hello/welcome, frames in seq order exactly once across reconnects (resume by
/// lastSeq), interrupts held per thread, and AG-UI's resume rules enforced before anything is sent.
public actor LiveClient {
    public enum Failure: Error, Equatable {
        case closed
        case contractUnsupported(String)
        case authFailed
        /// The server revoked this device: the app runs "Unpair and delete" locally (CONTRACT-2 §4.5).
        case deviceRevoked
        case noInterrupt(threadId: String)
        case uncovered([String])
        case unknownInterrupt(String)
        case interruptClosed(String)
        case interruptExpired(String)
    }

    public nonisolated let frames: AsyncStream<ServerFrame>
    private let continuation: AsyncStream<ServerFrame>.Continuation
    private let transport: LiveTransport
    private var hello: Hello
    private let backoff: [Double]
    private let sleep: @Sendable (Double) async -> Void
    private let now: @Sendable () -> Date
    private let maxReconnects: Int

    public private(set) var sessionId: String?
    public private(set) var lastSeq = 0
    public private(set) var welcome: Welcome?
    public private(set) var reconnects = 0
    public private(set) var invalidFrames = 0
    public private(set) var sentFrames: [String] = []
    public private(set) var held: [String: [Interrupt]] = [:]
    private var closedInterrupts: Set<String> = []
    private var connection: LiveConnection?
    private var stopped = false
    public private(set) var fatal: Failure?

    public init(transport: LiveTransport, hello: Hello, backoff: [Double] = [1, 2, 4, 8, 15], maxReconnects: Int = .max,
                sleep: @escaping @Sendable (Double) async -> Void = { s in try? await Task.sleep(nanoseconds: UInt64(s * 1e9)) },
                now: @escaping @Sendable () -> Date = { Date() }) {
        var c: AsyncStream<ServerFrame>.Continuation!
        frames = AsyncStream { c = $0 }
        continuation = c
        self.transport = transport; self.hello = hello; self.backoff = backoff
        self.sleep = sleep; self.now = now; self.maxReconnects = maxReconnects
    }

    /// Runs until stop(), a fatal error, or maxReconnects. Reconnects with backoff 1, 2, 4, 8, then 15 s.
    public func run() async {
        var attempt = 0
        while !stopped {
            do {
                let conn = try await transport.connect()
                connection = conn
                var h = hello
                if let s = sessionId { h.resume = .init(sessionId: s, lastSeq: lastSeq) }
                try await rawSend(Wire.string(ClientFrame.hello(h)), on: conn)
                let first = try Wire.decoder.decode(ServerFrame.self, from: Data(try await conn.receive().utf8))
                switch first {
                case .welcome(let w):
                    welcome = w; sessionId = w.sessionId; attempt = 0
                    continuation.yield(first)
                case .error(_, let code, let message, _) where code == "contract_unsupported":
                    fatal = .contractUnsupported(message); stopped = true; continue
                case .error(_, let code, _, _) where code == "auth_failed":
                    fatal = .authFailed; stopped = true; continue
                case .error(_, let code, _, _) where code == "device_revoked":
                    fatal = .deviceRevoked; stopped = true; continue
                default:
                    throw Failure.closed
                }
                while !stopped {
                    let text = try await conn.receive()
                    guard let frame = try? Wire.decoder.decode(ServerFrame.self, from: Data(text.utf8)) else { invalidFrames += 1; continue }
                    handle(frame)
                }
            } catch {
                connection = nil
                if stopped { break }
                reconnects += 1
                if reconnects > maxReconnects { break }
                await sleep(backoff[min(attempt, backoff.count - 1)])
                attempt += 1
            }
        }
        continuation.finish()
    }

    public func stop() {
        stopped = true
        connection?.close()
    }

    func handle(_ frame: ServerFrame) {
        if case .event(let seq, let thread, _, let event) = frame {
            if seq <= lastSeq { return }          // already delivered before the reconnect
            lastSeq = seq
            switch event.payload {
            case .runStarted:
                held[thread] = nil                 // a new run on the thread continues past its interrupts
            case .runFinished(let f):
                if case .interrupt(let list) = f.outcome { held[thread] = list }
            case .custom(let c) where c.name == "pisa.approval.state":
                if let id = c.value["interruptId"]?.string {
                    closedInterrupts.insert(id)
                    for (t, list) in held { held[t] = list.filter { $0.id != id }; if held[t]?.isEmpty == true { held[t] = nil } }
                }
            default: break
            }
        }
        continuation.yield(frame)
    }

    // MARK: sending

    public func send(_ frame: ClientFrame) async throws {
        guard let c = connection else { throw Failure.closed }
        try await rawSend(Wire.string(frame), on: c)
    }

    private func rawSend(_ text: String, on c: LiveConnection) async throws {
        sentFrames.append(text)
        try await c.send(text)
    }

    /// A user's turn.
    public func say(_ text: String, threadId: String, lang: String, source: String = "typed", wake: String = "tap") async throws -> String {
        let id = UUID().uuidString.lowercased()
        let input = RunAgentInput(threadId: threadId, runId: "run-\(id)",
                                  messages: [.object(["id": .string(id), "role": .string("user"), "content": .string(text)])],
                                  forwardedProps: .object(["pisa": .object(["turn": .object(["source": .string(source), "wake": .string(wake), "lang": .string(lang), "origin": .string("phone")])])]))
        try await send(.run(input))
        return input.runId
    }

    /// Builds the resuming input for `threadId`. AG-UI 1.0: entries must cover every interrupt of the run being
    /// continued; an expired one can only be cancelled; and an interrupt closed elsewhere (pisa.approval.state)
    /// is never resumed. Throws before anything is sent.
    public func resumeInput(threadId: String, answers: [String: ResumeEntry]) throws -> RunAgentInput {
        guard let list = held[threadId], !list.isEmpty else { throw Failure.noInterrupt(threadId: threadId) }
        let ids = Set(list.map(\.id))
        if let extra = answers.keys.first(where: { !ids.contains($0) }) { throw Failure.unknownInterrupt(extra) }
        let missing = list.map(\.id).filter { answers[$0] == nil }
        if !missing.isEmpty { throw Failure.uncovered(missing) }
        let iso = ISO8601DateFormatter()
        for i in list {
            let entry = answers[i.id]!
            guard entry.status == .resolved else { continue }
            if closedInterrupts.contains(i.id) { throw Failure.interruptClosed(i.id) }
            if let e = i.expiresAt, let d = iso.date(from: e), d <= now() { throw Failure.interruptExpired(i.id) }
        }
        return RunAgentInput(threadId: threadId, runId: "run-\(UUID().uuidString.lowercased())", resume: list.map { answers[$0.id]! })
    }

    public func resume(threadId: String, answers: [String: ResumeEntry]) async throws {
        let input = try resumeInput(threadId: threadId, answers: answers)
        held[threadId] = nil
        try await send(.run(input))
    }

    /// Barge-in: the server ends the run with the cancelled outcome and sends nothing more for it.
    public func cancel(runId: String) async throws { try await send(.cancel(runId: runId)) }
}
