"""The PDF reader's child process: `python -I -m pisa.connectors.pdf_worker`.

Reads one PDF on stdin, writes `{"outcome", "text"}` as JSON on stdout. It caps itself before pypdf is even
imported: address space (`MEMORY_BYTES`) and CPU time (`CPU_SECONDS`) by `setrlimit`, and pypdf's own
decompression limits sized for a 3 MB bill (`STREAM_BYTES`). Any failure inside is `unreadable`; the parent
kills it on its wall-clock timeout. Nothing here touches the network, the database or the logs.
"""

import io
import json
import resource
import sys

MEMORY_BYTES = 512 * 1024 * 1024
CPU_SECONDS = 8
STREAM_BYTES = 10 * 1024 * 1024  # a 3 MB bill's streams, decompressed
ELEMENTS = 20_000


def _limit(cpu: int = CPU_SECONDS, memory: int = MEMORY_BYTES) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))


def read(data: bytes, max_pages: int, max_chars: int) -> tuple[str, str]:
    outcome, parts = read_pages(data, max_pages, max_chars)
    return outcome, "\n".join(parts)[:max_chars]


def read_pages(
    data: bytes, max_pages: int, max_chars: int, streams: int = STREAM_BYTES
) -> tuple[str, list[str]]:
    import logging

    from pypdf import PdfReader, overwrite_configuration

    logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # its warnings would carry the PDF's own words
    overwrite_configuration(
        maximum_declared_stream_length=streams,
        array_based_stream_maximum_output_length=streams,
        array_maximum_element_count=ELEMENTS,
        dictionary_maximum_element_count=ELEMENTS,
        jbig2_maximum_output_length=streams,
        lzw_maximum_output_length=streams,
        run_length_maximum_output_length=streams,
        zlib_maximum_output_length=streams,
        zlib_maximum_recovery_input_length=streams // 2,
        brotli_maximum_output_length=streams,
    )
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return "encrypted", []
        if len(reader.pages) > max_pages:
            return "too_many_pages", []
        parts: list[str] = []
        size = 0
        for page in reader.pages:
            chunk = page.extract_text() or ""
            parts.append(chunk)
            size += len(chunk)
            if size >= max_chars:
                break
        return "read", parts
    except Exception:  # pypdf's LimitReachedError, ParseError, MemoryError, struct.error…
        return "unreadable", []


def main() -> int:
    """Argv: max pages, max chars, then for Pisa Learn's papers "pages", the CPU seconds, the memory
    and the stream bytes: each page's text on its own (the page-number oracle)."""
    pages_mode = len(sys.argv) > 3 and sys.argv[3] == "pages"
    if pages_mode:
        _limit(int(sys.argv[4]), int(sys.argv[5]))
    else:
        _limit()
    max_pages, max_chars = int(sys.argv[1]), int(sys.argv[2])
    data = sys.stdin.buffer.read()
    try:
        outcome, parts = read_pages(
            data, max_pages, max_chars, int(sys.argv[6]) if pages_mode else STREAM_BYTES
        )
    except BaseException:
        outcome, parts = "unreadable", []
    if pages_mode:
        sys.stdout.write(json.dumps({"outcome": outcome, "pages": parts}))
    else:
        sys.stdout.write(json.dumps({"outcome": outcome, "text": "\n".join(parts)[:max_chars]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
