"""A turn followed step by step: Pydantic AI's OpenTelemetry spans, written to Pisa's own logs .

No third party and no content. Every agent run, model request and tool call ends as one `span` log line with
ids, names, the duration, tokens and cached tokens. RAIL: logs carry no content. The instrumentation is set
not to record messages, and this exporter keeps only the attributes listed here even if a library adds more.
`gateway_span` opens one root span per gateway call carrying Pisa's `trace_id`, so the spans of a turn share
the OpenTelemetry trace that Pisa's other log lines name.
"""

import contextlib
from collections.abc import Iterator, Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from pisa.logging import get_logger

_log = get_logger("pisa.trace")
_provider: TracerProvider | None = None

# Attribute → log field. Only names, ids and numbers: never a message, a tool's arguments or its result.
_KEEP = {
    "gen_ai.operation.name": "op",
    "gen_ai.tool.name": "tool",
    "gen_ai.request.model": "model",
    "gen_ai.provider.name": "provider",
    "pisa.trace_id": "trace_id",
    "pisa.slot": "slot",
}
_TOKENS = ("gen_ai.usage.", "gen_ai.aggregated_usage.")


class LogSpans(SpanExporter):
    """Each finished span → one structured log line."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            _log.info("span", **fields(span))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def fields(span: ReadableSpan) -> dict[str, Any]:
    attrs = dict(span.attributes or {})
    out: dict[str, Any] = {"name": span.name.split(" ")[0]}  # "chat <model>" → "chat"
    ctx = span.get_span_context()
    if ctx is not None:
        out["otel_trace"] = f"{ctx.trace_id:032x}"
        out["span"] = f"{ctx.span_id:016x}"
    if span.parent is not None:
        out["parent"] = f"{span.parent.span_id:016x}"
    if span.start_time is not None and span.end_time is not None:
        out["ms"] = round((span.end_time - span.start_time) / 1e6, 1)
    out["ok"] = span.status.is_ok
    for key, field in _KEEP.items():
        if isinstance(attrs.get(key), str):
            out[field] = attrs[key]
    for key, value in attrs.items():
        if key.startswith(_TOKENS) and isinstance(value, int | float) and not isinstance(value, bool):
            out[key.split(".", 2)[2].replace(".", "_")] = value  # input_tokens, cache_read_tokens, …
    return out


def instrument() -> TracerProvider:
    """Once per process: every Pydantic AI agent reports its spans here, without content."""
    global _provider
    if _provider is None:
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        _provider = TracerProvider()
        # exported synchronously at span end (a few lines a turn; no thread, nothing lost on exit).
        _provider.add_span_processor(SimpleSpanProcessor(LogSpans()))
        Agent.instrument_all(
            InstrumentationSettings(
                tracer_provider=_provider, include_content=False, include_binary_content=False
            )
        )
    return _provider


@contextlib.contextmanager
def gateway_span(slot: str, trace_id: str) -> Iterator[None]:
    """The root span of one gateway call: its model requests and tool calls nest under it."""
    tracer = trace.get_tracer("pisa", tracer_provider=instrument())
    with tracer.start_as_current_span("pisa.call", attributes={"pisa.trace_id": trace_id, "pisa.slot": slot}):
        yield
