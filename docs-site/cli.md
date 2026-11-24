# The `pisa` command

Every command runs where Pisa runs; with docker compose, inside the `worker` container (it runs under every profile):

```bash
cd deploy
alias pisa='docker compose exec worker pisa'
```

`pisa <command> --help` shows each command's options.

| Command | What it does |
|---|---|
| `pisa setup` | The connection guide: your place (language, time zone, country), the AI models (every feature's, from your provider's catalog), voice talks (natural with Gemini Live when a Google key is set, else economy), push, places and web search (what Apple Maps, Google Maps and Tavily are for, where each key comes from, and a Google or Tavily key stored encrypted: `--places google\|tavily` with `--places-key-stdin`), pairing. `--yes` with flags runs it without questions; `--skip place,ai,talk,push,places,pair` skips steps. |
| `pisa doctor [--live]` | Checks the configuration for weak spots, each with what to do. Exit code 1 when it found something. `--live` also opens and closes a real session with natural mode's talker (it spends a little) and prints the provider's exact error, keys hidden. See [Troubleshooting](troubleshooting.md). |
| `pisa consent [--device <id>] [--health] [--yes]` | Shows the AI disclosure (each provider, model and what it receives) and records your consent for a paired device, as the app does. `--health` also allows health; `--yes` doesn't ask. See [Troubleshooting](troubleshooting.md#pisa-consent). |
| `pisa migrate` | Applies pending database migrations (needs `PISA_MIGRATE_DATABASE_URL`). Docker compose runs it in the `migrate` container. |
| `pisa serve-api` | Runs the HTTP API and the clients' live socket (`/v2/live`). |
| `pisa worker` | Runs the background worker (schedules: email intake, reminders, the night's work, the catalog refresh, pruning). |
| `pisa pair [name]` | Pairs a client (your own, the PISA app, or `pisa client`): a QR code (or typed code), confirmed by its 6 digits. See [Pairing](pairing.md). |
| `pisa pair --review [--devices N]` / `--review --revoke` | App Store review mode only (`PISA_REVIEW_MODE=1`): a code valid 30 days that pairs up to 3 devices (`--devices`) without the 6-digit check; `--revoke` ends it and unpairs the devices paired with it. See [For App Store reviewers](app-review.md). |
| `pisa devices list` / `revoke <id>` / `purge <id>` | Lists paired devices; cuts one off; removes a throwaway device and everything it made. |
| `pisa client pair` / `token` / `call` | A developer and testing tool: this machine as a paired device with its own key: pair with a code, print a 1-hour session token, make one REST request (`--sign OP TARGET` signs it with the device key). |
| `pisa client approve` / `reject <id>`, `pisa client erase <family\|all>` | Decide an approval, signed with the device key when it needs a signature; delete a family of data (or all), signed. |
| `pisa phone-keys reset --keep <device id>` | Removes every Face ID key (the PISA app's) and revokes every device but one. |
| `pisa secrets set <provider>` | Stores a provider's API key, read from a hidden prompt or stdin. |
| `pisa secrets set-apns`, `set-maps` | Stores the APNs or the Apple Maps `.p8` key, read from stdin ([Places and web search](places-and-web.md#apple-maps)). |
| `pisa catalog refresh` | Refreshes the models, their prices and the USD→EUR rate now. |
| `pisa models list` / `available` / `set <slot> <provider:model>` / `probe <server:model>` | The model of each feature slot. See [AI providers](ai-providers.md). |
| `pisa identity show` / `rotate` / `revoke` | Pisa's own signing key (Web Bot Auth, with `PISA_AGENT_URL`). |
| `pisa intake retriage --since <date>` | Dismisses the email tasks of the first sync that today's rules would not make: a dry run first, then `--apply`. |

Secrets are never passed on the command line: `pisa secrets` reads them from a hidden prompt or stdin.
