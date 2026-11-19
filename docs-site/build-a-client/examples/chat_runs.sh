#!/usr/bin/env bash
# One turn with curl: POST /v2/runs as a server-sent event stream, then as one JSON document.
#   PISA_SERVER=https://pisa.example.com PISA_TOKEN="$(python session.py --print-token)" ./chat_runs.sh
# Over https, pin the server's key as the client does: -k --pinnedpubkey "sha256//<the QR's spki>" (-k skips the
# CA check, which a self-signed certificate fails; the pin is still enforced).
set -euo pipefail
: "${PISA_SERVER:?the server, e.g. https://pisa.example.com}" "${PISA_TOKEN:?a session token}"

# [guide:curl]
curl -sS -N "$PISA_SERVER/v2/runs" \
  -H "Authorization: Bearer $PISA_TOKEN" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"threadId": "curl-chat", "runId": "run-curl-1",
       "messages": [{"id": "m1", "role": "user", "content": "What is on today?"}],
       "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "en-US"}}}}'
# data: {"type":"RUN_STARTED","timestamp":...,"threadId":"curl-chat","runId":"run-curl-1","protocolVersion":"1.0"}
# data: {"type":"TEXT_MESSAGE_START",...}    ... and so on, to RUN_FINISHED or RUN_ERROR

curl -sS "$PISA_SERVER/v2/runs" \
  -H "Authorization: Bearer $PISA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"threadId": "curl-chat", "runId": "run-curl-2",
       "messages": [{"id": "m2", "role": "user", "content": "And tomorrow?"}]}'
# {"events": [{"type": "RUN_STARTED", ...}, ..., {"type": "RUN_FINISHED", ...}]}
# [/guide:curl]
echo
