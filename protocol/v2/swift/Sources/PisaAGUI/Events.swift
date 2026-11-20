import Foundation

/// AG-UI 1.0 (https://ag-ui.com/spec/1.0/schema.json, frozen 2026-09-17). All 31 event types, camelCase on the
/// wire. A type this build does not know decodes to `.unknown` and is tolerated (the spec's rule for newer minors).
public struct AGUIEvent: Codable, Equatable, Sendable {
    public var type: String
    public var timestamp: Int?
    public var rawEvent: JSONValue?
    public var metadata: JSONValue?
    public var subagentRunId: String?
    public var payload: Payload

    public init(_ payload: Payload, timestamp: Int? = nil, metadata: JSONValue? = nil) {
        self.type = payload.typeName
        self.timestamp = timestamp
        self.metadata = metadata
        self.payload = payload
    }

    public enum Payload: Equatable, Sendable {
        case runStarted(RunStarted), runFinished(RunFinished), runError(RunError)
        case stepStarted(Step), stepFinished(Step)
        case textMessageStart(TextMessageStart), textMessageContent(TextDelta), textMessageEnd(MessageRef), textMessageChunk(TextMessageChunk)
        case toolCallStart(ToolCallStart), toolCallArgs(ToolCallArgs), toolCallEnd(ToolCallRef), toolCallChunk(ToolCallChunk), toolCallResult(ToolCallResult)
        case stateSnapshot(StateSnapshot), stateDelta(StateDelta), messagesSnapshot(MessagesSnapshot)
        case activitySnapshot(ActivitySnapshot), activityDelta(ActivityDelta)
        case raw(Raw), custom(Custom)
        case reasoningStart(MessageRef), reasoningEnd(MessageRef)
        case reasoningMessageStart(ReasoningMessageStart), reasoningMessageContent(TextDelta), reasoningMessageEnd(MessageRef), reasoningMessageChunk(ReasoningMessageChunk)
        case reasoningEncryptedValue(ReasoningEncryptedValue)
        case subagentStarted(SubagentStarted), subagentFinished(SubagentFinished), subagentError(SubagentError)
        case unknown(JSONValue)

        public var typeName: String {
            switch self {
            case .runStarted: return "RUN_STARTED"
            case .runFinished: return "RUN_FINISHED"
            case .runError: return "RUN_ERROR"
            case .stepStarted: return "STEP_STARTED"
            case .stepFinished: return "STEP_FINISHED"
            case .textMessageStart: return "TEXT_MESSAGE_START"
            case .textMessageContent: return "TEXT_MESSAGE_CONTENT"
            case .textMessageEnd: return "TEXT_MESSAGE_END"
            case .textMessageChunk: return "TEXT_MESSAGE_CHUNK"
            case .toolCallStart: return "TOOL_CALL_START"
            case .toolCallArgs: return "TOOL_CALL_ARGS"
            case .toolCallEnd: return "TOOL_CALL_END"
            case .toolCallChunk: return "TOOL_CALL_CHUNK"
            case .toolCallResult: return "TOOL_CALL_RESULT"
            case .stateSnapshot: return "STATE_SNAPSHOT"
            case .stateDelta: return "STATE_DELTA"
            case .messagesSnapshot: return "MESSAGES_SNAPSHOT"
            case .activitySnapshot: return "ACTIVITY_SNAPSHOT"
            case .activityDelta: return "ACTIVITY_DELTA"
            case .raw: return "RAW"
            case .custom: return "CUSTOM"
            case .reasoningStart: return "REASONING_START"
            case .reasoningEnd: return "REASONING_END"
            case .reasoningMessageStart: return "REASONING_MESSAGE_START"
            case .reasoningMessageContent: return "REASONING_MESSAGE_CONTENT"
            case .reasoningMessageEnd: return "REASONING_MESSAGE_END"
            case .reasoningMessageChunk: return "REASONING_MESSAGE_CHUNK"
            case .reasoningEncryptedValue: return "REASONING_ENCRYPTED_VALUE"
            case .subagentStarted: return "SUBAGENT_STARTED"
            case .subagentFinished: return "SUBAGENT_FINISHED"
            case .subagentError: return "SUBAGENT_ERROR"
            case .unknown(let v): return v["type"]?.string ?? "UNKNOWN"
            }
        }
    }

    public static let knownTypes: Set<String> = [
        "RUN_STARTED", "RUN_FINISHED", "RUN_ERROR", "STEP_STARTED", "STEP_FINISHED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END", "TEXT_MESSAGE_CHUNK", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_CHUNK",
        "TOOL_CALL_RESULT", "STATE_SNAPSHOT", "STATE_DELTA", "MESSAGES_SNAPSHOT", "ACTIVITY_SNAPSHOT", "ACTIVITY_DELTA", "RAW",
        "CUSTOM", "REASONING_START", "REASONING_END", "REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_END", "REASONING_MESSAGE_CHUNK", "REASONING_ENCRYPTED_VALUE", "SUBAGENT_STARTED",
        "SUBAGENT_FINISHED", "SUBAGENT_ERROR",
    ]

    enum BaseKeys: String, CodingKey { case type, timestamp, rawEvent, metadata, subagentRunId }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: BaseKeys.self)
        type = try c.decode(String.self, forKey: .type)
        timestamp = try c.decodeIfPresent(Int.self, forKey: .timestamp)
        rawEvent = try c.decodeIfPresent(JSONValue.self, forKey: .rawEvent)
        metadata = try c.decodeIfPresent(JSONValue.self, forKey: .metadata)
        subagentRunId = try c.decodeIfPresent(String.self, forKey: .subagentRunId)
        func p<T: Decodable>(_ t: T.Type) throws -> T { try T(from: decoder) }
        switch type {
        case "RUN_STARTED": payload = .runStarted(try p(RunStarted.self))
        case "RUN_FINISHED": payload = .runFinished(try p(RunFinished.self))
        case "RUN_ERROR": payload = .runError(try p(RunError.self))
        case "STEP_STARTED": payload = .stepStarted(try p(Step.self))
        case "STEP_FINISHED": payload = .stepFinished(try p(Step.self))
        case "TEXT_MESSAGE_START": payload = .textMessageStart(try p(TextMessageStart.self))
        case "TEXT_MESSAGE_CONTENT": payload = .textMessageContent(try p(TextDelta.self))
        case "TEXT_MESSAGE_END": payload = .textMessageEnd(try p(MessageRef.self))
        case "TEXT_MESSAGE_CHUNK": payload = .textMessageChunk(try p(TextMessageChunk.self))
        case "TOOL_CALL_START": payload = .toolCallStart(try p(ToolCallStart.self))
        case "TOOL_CALL_ARGS": payload = .toolCallArgs(try p(ToolCallArgs.self))
        case "TOOL_CALL_END": payload = .toolCallEnd(try p(ToolCallRef.self))
        case "TOOL_CALL_CHUNK": payload = .toolCallChunk(try p(ToolCallChunk.self))
        case "TOOL_CALL_RESULT": payload = .toolCallResult(try p(ToolCallResult.self))
        case "STATE_SNAPSHOT": payload = .stateSnapshot(try p(StateSnapshot.self))
        case "STATE_DELTA": payload = .stateDelta(try p(StateDelta.self))
        case "MESSAGES_SNAPSHOT": payload = .messagesSnapshot(try p(MessagesSnapshot.self))
        case "ACTIVITY_SNAPSHOT": payload = .activitySnapshot(try p(ActivitySnapshot.self))
        case "ACTIVITY_DELTA": payload = .activityDelta(try p(ActivityDelta.self))
        case "RAW": payload = .raw(try p(Raw.self))
        case "CUSTOM": payload = .custom(try p(Custom.self))
        case "REASONING_START": payload = .reasoningStart(try p(MessageRef.self))
        case "REASONING_END": payload = .reasoningEnd(try p(MessageRef.self))
        case "REASONING_MESSAGE_START": payload = .reasoningMessageStart(try p(ReasoningMessageStart.self))
        case "REASONING_MESSAGE_CONTENT": payload = .reasoningMessageContent(try p(TextDelta.self))
        case "REASONING_MESSAGE_END": payload = .reasoningMessageEnd(try p(MessageRef.self))
        case "REASONING_MESSAGE_CHUNK": payload = .reasoningMessageChunk(try p(ReasoningMessageChunk.self))
        case "REASONING_ENCRYPTED_VALUE": payload = .reasoningEncryptedValue(try p(ReasoningEncryptedValue.self))
        case "SUBAGENT_STARTED": payload = .subagentStarted(try p(SubagentStarted.self))
        case "SUBAGENT_FINISHED": payload = .subagentFinished(try p(SubagentFinished.self))
        case "SUBAGENT_ERROR": payload = .subagentError(try p(SubagentError.self))
        default: payload = .unknown(try JSONValue(from: decoder))
        }
    }

    public func encode(to encoder: Encoder) throws {
        if case .unknown(let raw) = payload { try raw.encode(to: encoder); return }
        var c = encoder.container(keyedBy: BaseKeys.self)
        try c.encode(type, forKey: .type)
        try c.encodeIfPresent(timestamp, forKey: .timestamp)
        try c.encodeIfPresent(rawEvent, forKey: .rawEvent)
        try c.encodeIfPresent(metadata, forKey: .metadata)
        try c.encodeIfPresent(subagentRunId, forKey: .subagentRunId)
        switch payload {
        case .runStarted(let v): try v.encode(to: encoder)
        case .runFinished(let v): try v.encode(to: encoder)
        case .runError(let v): try v.encode(to: encoder)
        case .stepStarted(let v), .stepFinished(let v): try v.encode(to: encoder)
        case .textMessageStart(let v): try v.encode(to: encoder)
        case .textMessageContent(let v), .reasoningMessageContent(let v): try v.encode(to: encoder)
        case .textMessageEnd(let v), .reasoningStart(let v), .reasoningEnd(let v), .reasoningMessageEnd(let v): try v.encode(to: encoder)
        case .textMessageChunk(let v): try v.encode(to: encoder)
        case .toolCallStart(let v): try v.encode(to: encoder)
        case .toolCallArgs(let v): try v.encode(to: encoder)
        case .toolCallEnd(let v): try v.encode(to: encoder)
        case .toolCallChunk(let v): try v.encode(to: encoder)
        case .toolCallResult(let v): try v.encode(to: encoder)
        case .stateSnapshot(let v): try v.encode(to: encoder)
        case .stateDelta(let v): try v.encode(to: encoder)
        case .messagesSnapshot(let v): try v.encode(to: encoder)
        case .activitySnapshot(let v): try v.encode(to: encoder)
        case .activityDelta(let v): try v.encode(to: encoder)
        case .raw(let v): try v.encode(to: encoder)
        case .custom(let v): try v.encode(to: encoder)
        case .reasoningMessageStart(let v): try v.encode(to: encoder)
        case .reasoningMessageChunk(let v): try v.encode(to: encoder)
        case .reasoningEncryptedValue(let v): try v.encode(to: encoder)
        case .subagentStarted(let v): try v.encode(to: encoder)
        case .subagentFinished(let v): try v.encode(to: encoder)
        case .subagentError(let v): try v.encode(to: encoder)
        case .unknown: break
        }
    }
}

// MARK: - Payloads (field names exactly as the 1.0 schema)

public struct RunStarted: Codable, Equatable, Sendable {
    public var threadId: String; public var runId: String
    public var protocolVersion: String?; public var parentRunId: String?; public var input: JSONValue?
    public init(threadId: String, runId: String, protocolVersion: String? = "1.0", parentRunId: String? = nil, input: JSONValue? = nil) {
        self.threadId = threadId; self.runId = runId; self.protocolVersion = protocolVersion; self.parentRunId = parentRunId; self.input = input
    }
}

public struct RunFinished: Codable, Equatable, Sendable {
    public var threadId: String; public var runId: String
    public var result: JSONValue?; public var outcome: RunOutcome?; public var usage: JSONValue?
    public init(threadId: String, runId: String, outcome: RunOutcome? = nil) { self.threadId = threadId; self.runId = runId; self.outcome = outcome }
}

public enum RunOutcome: Codable, Equatable, Sendable {
    case success(pendingToolCallIds: [String]?)
    case interrupt([Interrupt])
    case cancelled
    case unknown(JSONValue)

    enum K: String, CodingKey { case type, pendingToolCallIds, interrupts }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: K.self)
        switch try c.decode(String.self, forKey: .type) {
        case "success": self = .success(pendingToolCallIds: try c.decodeIfPresent([String].self, forKey: .pendingToolCallIds))
        case "interrupt": self = .interrupt(try c.decode([Interrupt].self, forKey: .interrupts))
        case "cancelled": self = .cancelled
        default: self = .unknown(try JSONValue(from: decoder))
        }
    }
    public func encode(to encoder: Encoder) throws {
        if case .unknown(let v) = self { try v.encode(to: encoder); return }
        var c = encoder.container(keyedBy: K.self)
        switch self {
        case .success(let p): try c.encode("success", forKey: .type); try c.encodeIfPresent(p, forKey: .pendingToolCallIds)
        case .interrupt(let i): try c.encode("interrupt", forKey: .type); try c.encode(i, forKey: .interrupts)
        case .cancelled: try c.encode("cancelled", forKey: .type)
        case .unknown: break
        }
    }
}

public struct Interrupt: Codable, Equatable, Sendable {
    public var id: String; public var reason: String
    public var message: String?; public var toolCallId: String?; public var responseSchema: JSONValue?
    public var expiresAt: String?; public var metadata: JSONValue?; public var subagentRunId: String?
    public init(id: String, reason: String, message: String? = nil, expiresAt: String? = nil, metadata: JSONValue? = nil) {
        self.id = id; self.reason = reason; self.message = message; self.expiresAt = expiresAt; self.metadata = metadata
    }
}

public struct ResumeEntry: Codable, Equatable, Sendable {
    public enum Status: String, Codable, Sendable { case resolved, cancelled }
    public var interruptId: String; public var status: Status; public var payload: JSONValue?; public var metadata: JSONValue?
    public init(interruptId: String, status: Status, payload: JSONValue? = nil) { self.interruptId = interruptId; self.status = status; self.payload = payload }
}

public struct RunAgentInput: Codable, Equatable, Sendable {
    public var threadId: String; public var runId: String
    public var protocolVersion: String?; public var parentRunId: String?; public var state: JSONValue?
    public var messages: [JSONValue]; public var tools: [JSONValue]?; public var context: [JSONValue]?
    public var forwardedProps: JSONValue?; public var resume: [ResumeEntry]?
    public init(threadId: String, runId: String, messages: [JSONValue] = [], forwardedProps: JSONValue? = nil, resume: [ResumeEntry]? = nil) {
        self.threadId = threadId; self.runId = runId; self.protocolVersion = "1.0"; self.messages = messages
        self.forwardedProps = forwardedProps; self.resume = resume
    }
}

public struct RunError: Codable, Equatable, Sendable { public var message: String; public var code: String?; public var usage: JSONValue? }
public struct Step: Codable, Equatable, Sendable { public var stepName: String }
public struct MessageRef: Codable, Equatable, Sendable { public var messageId: String }
public struct TextDelta: Codable, Equatable, Sendable { public var messageId: String; public var delta: String }
public struct TextMessageStart: Codable, Equatable, Sendable { public var messageId: String; public var role: String?; public var name: String? }
public struct TextMessageChunk: Codable, Equatable, Sendable { public var messageId: String?; public var role: String?; public var delta: String?; public var name: String? }
public struct ToolCallStart: Codable, Equatable, Sendable { public var toolCallId: String; public var toolCallName: String; public var parentMessageId: String? }
public struct ToolCallArgs: Codable, Equatable, Sendable { public var toolCallId: String; public var delta: String }
public struct ToolCallRef: Codable, Equatable, Sendable { public var toolCallId: String }
public struct ToolCallChunk: Codable, Equatable, Sendable { public var toolCallId: String?; public var toolCallName: String?; public var parentMessageId: String?; public var delta: String? }
public struct ToolCallResult: Codable, Equatable, Sendable { public var messageId: String; public var toolCallId: String; public var content: JSONValue; public var role: String? }
public struct StateSnapshot: Codable, Equatable, Sendable { public var snapshot: JSONValue }
public struct StateDelta: Codable, Equatable, Sendable { public var delta: [JSONValue] }
public struct MessagesSnapshot: Codable, Equatable, Sendable { public var messages: [JSONValue] }
public struct ActivitySnapshot: Codable, Equatable, Sendable {
    public var messageId: String; public var activityType: String; public var content: JSONValue; public var replace: Bool?
}
public struct ActivityDelta: Codable, Equatable, Sendable { public var messageId: String; public var activityType: String; public var patch: [JSONValue] }
public struct Raw: Codable, Equatable, Sendable { public var event: JSONValue; public var source: String? }
public struct Custom: Codable, Equatable, Sendable {
    public var name: String; public var value: JSONValue
    public init(name: String, value: JSONValue) { self.name = name; self.value = value }
}
public struct ReasoningMessageStart: Codable, Equatable, Sendable { public var messageId: String; public var role: String }
public struct ReasoningMessageChunk: Codable, Equatable, Sendable { public var messageId: String?; public var delta: String? }
public struct ReasoningEncryptedValue: Codable, Equatable, Sendable { public var subtype: String; public var entityId: String; public var encryptedValue: String }
public struct SubagentStarted: Codable, Equatable, Sendable {
    public var subagentRunId: String; public var name: String; public var description: String?
    public var parentSubagentRunId: String?; public var parentToolCallId: String?; public var parentMessageId: String?
}
public struct SubagentFinished: Codable, Equatable, Sendable { public var subagentRunId: String; public var result: JSONValue?; public var outcome: JSONValue? }
public struct SubagentError: Codable, Equatable, Sendable { public var subagentRunId: String; public var message: String; public var code: String? }
