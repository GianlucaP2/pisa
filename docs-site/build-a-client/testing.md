# Testing your client

`pisa-conformance`, the conformance kit in `protocol/v2/conformance/`, is the developer's and tester's tool for
contract 2. It holds a headless reference client (it connects, says hello, sends frames, records what comes back)
and the validators behind it: every frame against the official AG-UI 1.0 and A2UI 1.0 schemas, Pisa's catalog and
contract schema, the AG-UI lifecycle per thread, `seq`, and the negotiation. It is a tool for building and
checking clients and servers, not a client for people to use.

```bash
pip install './protocol/v2/conformance[live]'      # from a checkout; Python 3.11+
```

## Against the contract's fixtures

```bash
pisa-conformance selftest              # every recorded stream, event, gap and enrolment fixture
python vectors.py                      # this guide's byte formats against the contract's vectors
python render.py protocol/v2/fixtures/streams/*.jsonl protocol/v2/fixtures/events/*.jsonl
python screens.py protocol/v2/fixtures/streams/*.jsonl
```

The fixtures are the contract's own examples of every event and stream: a renderer that draws all of them, and
skips what it doesn't know, draws what a server sends. Port [`vectors.py`](examples/vectors.py) first when you
write a client in another language: pairing, session, the approval digest and canonical text, and the sealed
push are byte-exact.

## Against your own traffic

Record what your client sends and receives on `/v2/live`, one JSON frame per line, and check both sides:

```bash
python chat_live.py "What's on today?" --record mine        # writes mine-1.server.jsonl, mine-1.client.jsonl
pisa-conformance stream mine-1.server.jsonl                 # the server's frames: schemas, seq, lifecycle
pisa-conformance stream --client mine-1.client.jsonl        # your frames: hello first, each one valid
```

Record one file per connection: after a reconnect the `welcome` takes the `lastSeq` you resumed from, so two
connections in one file look like a repeated `seq`.

A talk's data channel takes no `hello`, so `stream --client` (which wants one first) does not fit it: check its
frames one by one with the kit's library, `pisa_conformance.validate.client_frame_errors(frame)` (an empty list
is valid). `server_frame_errors(frame)` does the same for a server frame, and `check_seq` and `check_lifecycle`
take a list of frames.

## Against a server, live

```bash
pisa-conformance discovery https://pisa.example.com/v1/server
PISA_TOKEN="$(python session.py --print-token)" pisa-conformance live wss://pisa.example.com/v2/live \
    --token-env PISA_TOKEN --hello my-hello.json --send my-frames.jsonl --seconds 10 --record got.jsonl
```

`live` sends your `hello` (or a conservative one), then your frames after `welcome`, records what arrives for
`--seconds`, and checks it, including that the server sent nothing your `hello` did not negotiate. The token is
read from the named variable, never from the command line. Exit status: 0 clean, 1 a check failed, 2 a usage or
connection error.

## How this guide is tested

Every example here runs in Pisa's CI against a real server built from the same commit (no model chosen, nothing
leaving the machine): pairing from the server's QR, sessions, consent, both chat paths and curl, the live socket
with cancel and resume (its recorded frames checked by the kit), voice in economy, a WebRTC offer and its
fallback, approvals, push, erase, every provokable error, and a second and third device. A check also fails the
build when a code block on these pages differs from its file.
