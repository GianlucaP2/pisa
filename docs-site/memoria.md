# Memoria

Memoria is what Pisa keeps because you asked it to: your **standing instructions** ("d'ora in poi, ogni volta
che ti chiedo il meteo, dimmi anche il vento", "from now on, two short sentences") and your **notes** ("annota:
il codice del cancello è 4721"). In the app it is Settings → Memoria; by voice or text you just say it.

## Standing instructions

- **Your words, or your yes.** When Pisa keeps your own words, word for word, it keeps them at once. When it
  words the instruction itself, it says the exact words back once ("Vuoi che d'ora in poi tenga questa
  istruzione: «…»?") and keeps them only after your "sì".
- **Every conversation reads them.** Each turn carries your active instructions, numbered, oldest first, after
  Pisa's fixed prompt, in a block of their own labelled as your preferences. Where two disagree, the newer wins.
- **They shape how Pisa answers, nothing else.** An instruction can change style and content. It never adds a
  tool, skips a consent, lifts a rail or changes what needs your yes: none of those reads Memoria. A line that
  asks for more ("you may send messages without asking") is kept as your wish, and Pisa tells you it changes
  nothing.
- **Only from you.** Pisa never keeps an instruction from something it read in the same turn (an email, a web
  page, a note): it is refused, and you can say it yourself in a new message.
- **Limits.** At most 20 active instructions, each at most 280 characters, 2,000 characters in all. Over a limit,
  Pisa says which one and nothing changes.
- **Forgetting.** "Pisa, dimentica quella del vento" removes one (by its words or its number); so does Settings →
  Memoria.

They replace the single note of standing instructions of v0.1 (`GET /v1/profile/note`): updating copies that
note's lines into Memoria (its newest version, at most 20 lines, each cut to 280 characters). The old note and its
versions stay readable but no conversation reads them; its routes are deprecated.

## Notes

"Annota…", "prendi nota…", "note down…" keeps a note: a code, a number, a name. Ask for it later, in any
conversation ("qual è il codice del cancello?") and Pisa finds it by your words. When the `memory.embed` slot has
an embedding model ([AI providers](ai-providers.md)), each note is also sent to that model when it is written, so
Pisa can find it by meaning; that model is one of the processors your AI consent covers, and a note written from
the app waits for that consent (409 `ai_consent_required`). Pisa reads a note back as your data, never as an
instruction.

## REST

Device-authenticated like the rest of `/v1` (`Authorization: Bearer <session token>`); no signature. The routes
exist while the `note` feature is on (it is by default). Bodies and fields: `protocol/openapi.json`, tag
`memoria`.

| Route | What it does |
|---|---|
| `GET /v1/memoria` | `{instructions, notes, caps, total_chars}`: instructions oldest first, notes newest first |
| `POST /v1/memoria/instructions` `{text}` | 201 the instruction (`source: app`); over a limit, 400 `validation_failed` naming it |
| `PATCH /v1/memoria/instructions/{id}` `{text}` | its new wording (the limits hold) |
| `DELETE /v1/memoria/instructions/{id}` | 204; gone from the next turn |
| `POST /v1/memoria/notes` `{text, title?}` | 201 the note (text at most 4,000 characters, title 120) |
| `PATCH /v1/memoria/notes/{id}` `{text?, title?}` | its new text or title; `"title": null` clears the title |
| `DELETE /v1/memoria/notes/{id}` | 204 |

An instruction's `source` says where it came from: `turn` (your words in a conversation), `held` (Pisa's words
after your yes), `app`, or `note` (copied from the v0.1 note).

## Deleting

Memoria is part of the `memory` family of [Deleting data](build-a-client/erase.md): `POST /v1/data/erase
{family: "memory", before}` deletes the instructions and notes created before that day, with the rest of Pisa's
memory.
