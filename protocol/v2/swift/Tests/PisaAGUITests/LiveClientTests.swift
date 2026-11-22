import XCTest
@testable import PisaAGUI

/// A free function, so `async let` does not send the test case across isolation (Swift 6 language mode).
func collect(_ client: LiveClient) async -> [ServerFrame] {
    var out: [ServerFrame] = []
    for await f in client.frames { out.append(f) }
    return out
}

final class LiveClientTests: XCTestCase {

    func testEveryFrameDeliveredExactlyOnceAcrossAReconnect() async throws {
        let stream = try Fixtures.frames("turn_places")          // 14 frames, seq 1...14
        let cut = 6
        // connection 1 drops after frame 6; connection 2 replays from 4 (an overlap the client must drop)
        let t = ScriptedTransport([[welcome(seq: 0)] + stream[0..<cut], [welcome(seq: cut, replayed: true)] + stream[(cut - 3)...]])
        let log = SleepLog()
        let client = LiveClient(transport: t, hello: .current(device: device), maxReconnects: 2, sleep: { log.delays.append($0) })
        async let frames = collect(client)
        await client.run()
        let got = await frames
        let seqs = got.compactMap { f -> Int? in if case .event(let s, _, _, _) = f { return s }; return nil }
        XCTAssertEqual(seqs, Array(1...14))
        XCTAssertEqual(log.delays.first, 1)
        let hello2 = try JSONValue.parse(t.connections[1].sent[0])
        XCTAssertEqual(hello2["resume"], .object(["sessionId": .string("sess-1"), "lastSeq": .number(Double(cut))]))
        XCTAssertNil(try JSONValue.parse(t.connections[0].sent[0])["resume"])
    }

    func testBackoffIsOneTwoFourEightThenFifteen() async {
        let log = SleepLog()
        let client = LiveClient(transport: ScriptedTransport([]), hello: .current(device: device), maxReconnects: 6, sleep: { log.delays.append($0) })
        await client.run()
        XCTAssertEqual(log.delays, [1, 2, 4, 8, 15, 15])
    }

    func testContractMismatchStopsWithOneClearError() async {
        let err = #"{"control":"error","seq":0,"code":"contract_unsupported","message":"Update the app","retryable":false}"#
        let client = LiveClient(transport: ScriptedTransport([[err]]), hello: .current(device: device), maxReconnects: 5, sleep: { _ in })
        await client.run()
        let fatal = await client.fatal
        XCTAssertEqual(fatal, .contractUnsupported("Update the app"))
        let n = await client.reconnects
        XCTAssertEqual(n, 0)
    }

    func testRevokedDeviceStopsAndAsksForTheLocalWipe() async {
        let err = #"{"control":"error","seq":0,"code":"device_revoked","message":"This device was removed","retryable":false}"#
        let client = LiveClient(transport: ScriptedTransport([[err]]), hello: .current(device: device), maxReconnects: 5, sleep: { _ in })
        await client.run()
        let fatal = await client.fatal
        XCTAssertEqual(fatal, .deviceRevoked)
    }

    func approvalClient(now: Date) async throws -> (LiveClient, ScriptedTransport) {
        let stream = try Fixtures.frames("approval")
        let firstRunEnd = stream.firstIndex { $0.contains("\"interrupt\"") }!
        let t = ScriptedTransport([[welcome(seq: 0)] + stream[...firstRunEnd]])
        let client = LiveClient(transport: t, hello: .current(device: device), maxReconnects: 0, sleep: { _ in }, now: { now })
        async let _ = collect(client)
        await client.run()
        return (client, t)
    }

    let thread = "pisa-approval:33333333-3333-4333-8333-333333333333"
    let iid = "int-33333333-3333-4333-8333-333333333333"
    let early = ISO8601DateFormatter().date(from: "2026-11-18T10:00:00Z")!

    func testInterruptIsHeldPerThreadAndResumeMustCoverIt() async throws {
        let (client, _) = try await approvalClient(now: early)
        let held = await client.held[thread]
        XCTAssertEqual(held?.map(\.id), [iid])
        let convo = await client.held["thread-conversation"]
        XCTAssertNil(convo)
        do { _ = try await client.resumeInput(threadId: thread, answers: [:]); XCTFail() } catch let e as LiveClient.Failure { XCTAssertEqual(e, .uncovered([iid])) }
        do { _ = try await client.resumeInput(threadId: thread, answers: ["int-x": .init(interruptId: "int-x", status: .cancelled)]); XCTFail() }
        catch let e as LiveClient.Failure { XCTAssertEqual(e, .unknownInterrupt("int-x")) }
        let payload: JSONValue = .object(["decision": .string("reject"), "auth": .string("tap"), "digest": .string(String(repeating: "0", count: 64))])
        let input = try await client.resumeInput(threadId: thread, answers: [iid: .init(interruptId: iid, status: .resolved, payload: payload)])
        XCTAssertEqual(input.threadId, thread)
        XCTAssertEqual(input.resume?.map(\.interruptId), [iid])
    }

    func testExpiredInterruptCanOnlyBeCancelled() async throws {
        let late = ISO8601DateFormatter().date(from: "2026-11-18T10:30:00Z")!
        let (client, _) = try await approvalClient(now: late)
        do { _ = try await client.resumeInput(threadId: thread, answers: [iid: .init(interruptId: iid, status: .resolved, payload: .null)]); XCTFail() }
        catch let e as LiveClient.Failure { XCTAssertEqual(e, .interruptExpired(iid)) }
        let input = try await client.resumeInput(threadId: thread, answers: [iid: .init(interruptId: iid, status: .cancelled)])
        XCTAssertEqual(input.resume?.first?.status, .cancelled)
    }

    func testInterruptClosedElsewhereIsNeverResumed() async throws {
        let (client, _) = try await approvalClient(now: early)
        let closed = #"{"seq":99,"threadId":"pisa-notices:d","runId":"r","event":{"type":"CUSTOM","name":"pisa.approval.state","value":{"approvalId":"33333333-3333-4333-8333-333333333333","interruptId":"\#(iid)","state":"approved","by":"voice"}}}"#
        await client.handle(try Wire.decoder.decode(ServerFrame.self, from: Data(closed.utf8)))
        do { _ = try await client.resumeInput(threadId: thread, answers: [iid: .init(interruptId: iid, status: .resolved, payload: .null)]); XCTFail() }
        catch let e as LiveClient.Failure { XCTAssertEqual(e, .noInterrupt(threadId: thread)) }
    }

    func testANewRunOnTheThreadClearsItsInterrupts() async throws {
        let (client, _) = try await approvalClient(now: early)
        let resumed = try Fixtures.frames("approval").first { $0.contains("run-approval-2") && $0.contains("RUN_STARTED") }!
        await client.handle(try Wire.decoder.decode(ServerFrame.self, from: Data(resumed.utf8)))
        let after = await client.held[thread]
        XCTAssertNil(after)
    }
}
