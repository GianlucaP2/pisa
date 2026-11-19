# Security policy

PISA holds a person's mail, calendar, money and messages. A security problem in it matters: thank you for reporting
it carefully.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a vulnerability.**

Report it privately through GitHub's private vulnerability reporting: the repository's **Security** tab, then
**Report a vulnerability**. Only the maintainers see the report.

Please include:
- what an attacker can do, and what they need first (network position, an account, a paired device…);
- the version or commit, and how PISA was deployed (docker compose, behind which proxy);
- steps to reproduce, or a proof of concept;
- whether the problem is in the server (`core/`), the push relay (`relay/`) or the protocol (`protocol/`).

Never include real personal data, real credentials or real tokens in a report: invent them.

## What happens next

- We acknowledge the report within **5 working days**, and keep you told of progress at least every two weeks.
- We confirm the problem, decide its severity, and agree a disclosure date with you, normally within **90 days** of
  the report, sooner when a fix is out.
- The fix is released with a GitHub security advisory (and a CVE when it qualifies). We credit you there unless you
  ask us not to.

## Supported versions

Security fixes go to the latest release. Self-hosters should run it: PISA has no long-term-support branches yet.

## Scope

In scope: the code in this repository, its container images, and its default configuration
(`deploy/docker-compose.yml`, `.env.example`).

Out of scope: a deployment's own host, proxy or DNS; third-party services PISA connects to (report those to their
owners); findings that need a compromised server or a stolen unlocked device; denial of service by volume.

Calendar events are foreign (their text is a stranger's, never instructions) unless proven the user's own: made
by PISA, or created by the user in Google Calendar on a calendar they own with no guest list and no invitation in
their mail naming the event's UID; an event that arrived only by email is always foreign. While Gmail is connected,
an event changed after the last mail sync is not the user's own until that sync has read the mail. Erasing the mail
keeps its invitations' events foreign. Known limit: an invitation that reached the mailbox outside the mail sync's
window, or was deleted before PISA first read it, leaves no trace, and its event can pass as the user's own.

## How a client trusts the server

It depends on how PISA is reached (the compose profile, `COMPOSE_PROFILES`):

- `tailscale` and `caddy`: the server's certificate is a public WebPKI one (issued by Tailscale, or by Let's
  Encrypt through Caddy). After pairing, clients rely on WebPKI for the server's identity.
- `local`: PISA's own self-signed key and certificate, made by `pisa init` in `deploy/tls/` (the key readable by
  its owner only). No certificate authority vouches for it: the SPKI pin the pairing QR carries is the client's
  only trust anchor. Keep the key private; a new key (`pisa init --rotate-tls`) means every client pairs again.

A proof of the server's identity that does not rest on TLS is a later 2.x addition.

## Safe harbour

We will not take legal action against research done in good faith under this policy: testing only systems you own
or are allowed to test, never accessing other people's data, and giving us reasonable time to fix before
disclosure.
