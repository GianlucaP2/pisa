# For App Store reviewers

The PISA app is a client for **a Pisa server the user runs themselves** (on a Mac, a Linux machine or a rented
server). There is no PISA cloud service and no account with us: the app works only once it is paired with a
server. To make a review possible, the server has a **review mode**: a demo server with invented data, nothing
connected, and a pairing code the reviewer can use without anyone confirming it on the server.

Whether a review server runs, where, and for how long is the publisher's decision. This page says what one is
and how to set it up; the Review Notes of each submission say whether there is one and how to reach it.

## The short version

- The publisher runs a Pisa server with `PISA_REVIEW_MODE=1`, reachable from the internet with a public HTTPS
  certificate.
- `pisa pair --review` prints a code, as a QR and as 26 characters to type. It works for 30 days and for up to
  3 devices, and the app pairs with it without the usual 6-digit check on the server.
- The publisher puts the server's address and the 26 characters in the Review Notes.
- On that server, everything is invented: a few demo tasks, no real mail, calendar, health or bank data.
  Nothing is sent anywhere except to the AI provider the publisher set up and to the weather service (and
  Apple's push service, if the publisher set up direct push).

## What the publisher runs

1. **A server reachable from the internet**, with a public HTTPS certificate. Any host will do; the
   [VPS install](install/vps.md) with the `caddy` profile and a domain is the simplest. The `tailscale` profile,
   the usual home setup, reaches only the owner's own devices, so a reviewer can't use it.
2. **A fresh database, used only for the review.** Never turn review mode on for a server with anyone's real
   data: a database that already holds tasks, conversations or paired devices, and was never a review
   server's, makes the server refuse to start with `PISA_REVIEW_MODE=1` (point `PISA_DATABASE_URL` at a new,
   empty database instead).
3. **Review mode on**: `PISA_REVIEW_MODE=1` in the server's `.env` (`~/pisa/deploy/.env` with the VPS install),
   then restart (`pisa-compose up -d`). At its first start, Pisa adds a few demo tasks to the empty database:
   two for today, two for tomorrow, one without a day, and one already done ("Water the plants", "Call the
   dentist (demo)", ...). It does this once: a later start, or an emptied task list, adds nothing again.
4. **An AI provider**, with a key made only for the review and a low spending limit at the provider
   ([Getting a key](ai-providers.md#getting-a-key)), set with `pisa setup` or `pisa secrets set`, and low limits
   on the server too (`PISA_HARD_DAILY_EUR`, `PISA_HARD_MONTHLY_EUR`: [Spending](ai-providers.md#spending)).
5. **The review code**: `pisa-compose exec worker pisa pair --review`. It prints the QR, the 26-character code
   and until when it works. `--devices 1` makes it work for one device only (the default is 3, so the reviewer
   can pair again, or pair a second phone). A new `pisa pair --review` replaces the old code (devices already
   paired with it stay paired).
6. **The Review Notes**: the server's address (`https://pisa.example.com`), the 26-character code, how to reach
   the publisher, and the permissions the app asks for and why.
7. **After the review, reset the review server**: run `pisa pair --review --revoke`, which ends the code and
   unpairs every device paired with it (and deletes their Face ID keys); then, before any other use, point the
   server at a fresh database (`PISA_DATABASE_URL` at a new, empty one, `pisa migrate`) or take it down. The
   review's database holds what the reviewer typed and said; don't keep using it as anyone's server. One device
   can also be unpaired with `pisa devices revoke <id>` (`pisa devices list` shows them). A server started
   without `PISA_REVIEW_MODE=1` while review devices are still paired logs a warning naming how many and
   `pisa pair --review --revoke`; it unpairs nothing by itself. A review database used for real since (a
   device paired the usual way, or tasks added once it ran without review mode) refuses review mode again:
   the next review starts from a fresh database too.

`pisa pair --review` refuses to make a code unless `PISA_REVIEW_MODE=1`. With review mode off, an old review
code no longer pairs anything, and ordinary pairing is exactly as usual (a 10-minute code and the 6-digit check).

Anyone who has the code can pair with the server while it works, up to its number of devices: that is why the
server holds invented data only, and why the code ends with `--revoke` once the review is over. A device paired
with the review code can't pair further devices from the app, register a Face ID key or vouch for another
device's key.

## What the reviewer can try

1. **Pair**: in the app, enter the server's address and the 26-character code (or scan the QR). No one needs to
   confirm it on the server: the app is paired within a few seconds.
2. **Consent first.** The app shows the AI disclosure (which provider and model get which kinds of data) and
   asks for consent before anything goes to an AI. Health has its own, separate consent.
3. **Today and tasks**: see today's demo tasks and the plan of the day; add a task by typing ("remind me to buy
   milk tomorrow"), complete one, delete one.
4. **Typed chat and voice**: ask about today, or the weather; the economy voice (the phone transcribes, the
   server answers).
5. **Stop**: say or type "stop everything", then resume.
6. **Settings**: the AI providers and models (shown, not changed: see below), and deleting your data.

## What is off in review mode

Review mode turns these off, whatever else the server's settings say:

- **Every connector**: Google (Gmail and Calendar), Apple Health, Garmin, the banks and the rest. The app's
  Connections screen is empty and their pages answer "not found".
- **Everything that writes outside the server**: no email is sent, no calendar event is written, no push relay
  registration.
- **Web research and maps providers**: no web search, no page reading, no Google Maps, Apple Maps or Tavily
  call.
- **Every experimental feature** (`PISA_EXPERIMENTAL` is ignored): the ones listed in
  [Experimental features](experimental.md); and, though supported, places and the web, messages and calls,
  the server's voices (the phone speaks) and the public data's downloads (transit, ZTL, pharmacies).
- **Pisa Learn**: no paper, page or URL is fetched (arXiv, Unpaywall, OpenAlex, Semantic Scholar or any
  address), Learn's pages answer "not found" and its tools aren't offered; the night's learning doesn't run.
- **Handoffs and store carts**: Pisa prepares no handoff and calls no merchant (no UCP catalog or cart); their
  tools aren't offered.
- **Changing the AI setup from the app**: the app can't set an AI key, add a model server, pick a feature's
  model, override a model's capabilities or change the spending caps; all of it is what the publisher set up
  on the server.

What still goes out: the requests to the AI provider the publisher set up (with their own key), the weather
(Open-Meteo), and, only if the publisher set up direct push with their own Apple key, notifications to the
paired phone through Apple.

## Permissions

The app asks for each permission when you first use a feature that needs it, with the reason in the prompt.
Health data (HealthKit) goes to the user's own server only, and to an AI only with the separate health consent
([Privacy](privacy.md)).

## No account

The app creates no account with us, so there is no account to delete. Pairing is with a server, and all data
stays there. The app sends nothing to the publisher: what it sends where is in the [privacy policy](privacy.md).
