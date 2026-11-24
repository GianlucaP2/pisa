# Experimental features

Everything in Pisa that is built is supported and on by default: Google, Apple Health, reminders, places and
the web, notes, memory, the profile, the worker's steps and the rest. Only a few parts are **experimental**:
stubs, and integrations that were built but never run against the real service. They are off by default, and
you switch each one on yourself.

## What experimental means

- **Off unless you turn it on.** A fresh install leaves every experimental feature off; Pisa works without them.
- **It may not work, and it may change.** Its settings, its behaviour or its API may change in any release, or
  the feature may be removed. After an upgrade you may need to set it up again.
- **Best effort.** Issues about it are welcome, and help the most, but they are fixed when someone can.
- **Your data is still yours.** An experimental feature follows the same rules as the rest of Pisa: nothing goes
  to a provider you didn't set up, a connector shows where its data goes before it is switched on, and AI
  consent and the separate health consent still apply ([Privacy](privacy.md)).

Supported doesn't mean tested end to end: what has been tried with a real account, device or provider, and what
hasn't, is in the repository's `docs/pisa/SUPPORT-MATRIX.md`.

## The experimental features

| Id | What it switches on | Why it is experimental |
|---|---|---|
| `garmin` | The Garmin connector: activities, sleep and daily summaries, with a Garmin Personal API key. | Built, but never used. |
| `polar` | The Polar connector. | A stub: no provider yet. |
| `withings` | The Withings connector. | A stub: no provider yet. |
| `bank` | Read-only banking through Enable Banking (PSD2): accounts and transactions, never a payment. | Never run against a bank. |
| `ade` | Agenzia delle Entrate's tax drawer (cassetto fiscale). | Never run. |
| `radar` | The admin radar: PEC, the Italian administration's mail, documents and deadlines. | Never run against PEC or the Italian admin sources. |
| `talker_xai` | Natural voice with the xAI talker. | It failed as the default, so natural voice uses Gemini. |

Every id, with what it gates (the connectors, their routes, the tools), is in `core/pisa/domain/experimental.py`;
`PISA_EXPERIMENTAL=all` turns every one on.

## Turning one on

1. Open your server's `.env` file (with docker compose, `deploy/.env`).
2. Add one line with the ids you want, separated by commas:

    ```bash
    PISA_EXPERIMENTAL=garmin
    ```

3. Restart Pisa so it reads the change:

    ```bash
    cd deploy
    docker compose up -d
    ```

4. Set the feature up as its page says (a key, a connector's credential, a consent).

To turn a feature off again, remove its id from the line (or the whole line) and restart. Without the variable,
or with it empty, every experimental feature stays off.

## Reporting what you find

Your report is how a feature becomes supported. Open an issue in the repository's **Issues** tab, with the bug
form, and add the `experimental` label. Please say:

- the feature's id, and the Pisa release you installed;
- how you installed it (Mac, Linux, Raspberry Pi, VPS; the `tailscale`, `caddy` or `local` profile);
- what you did, step by step, what you expected and what happened;
- the relevant lines of `docker compose logs api worker`.

Remove anything personal first: names, addresses, mail, keys, tokens, health or bank data. A security problem
never goes in an issue: see the repository's SECURITY.md.

Did it work for you? Say so too: a report that a feature worked end to end, with what you tried, is just as
useful. How a feature moves to supported is in the repository's CONTRIBUTING.md, under "Experimental
features".
