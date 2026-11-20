# Changelog

All notable changes to PISA are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). From the first release on, release-please writes the entries from the
Conventional Commit titles.

## [0.1.0] - 2026-11-20

- The first public version of PISA: the server (`core/`), the push relay (`relay/`), and the device protocol
  (`protocol/`).
- Replies never contain tool-call syntax: a call the model writes as text is said as its words, or not at all.
- "What can you do" and every reply name only what this install and this client can do. A client that didn't
  pick the phone's extensions (any run over HTTP) is never offered messages, calls or directions, and an offer
  Pisa holds back takes its follow-up question with it.
- When a reply already says something isn't connected, Pisa doesn't repeat it; the notice names only what was
  claimed.
- A task title written in another language than the user's words is restated in theirs. A title they said word
  for word stays as it is.
- One message that names several tasks marks each one done ("I bought milk, bread and eggs").
- "Undo" right after Pisa added a task removes that task; it can be restored for 24 hours.
