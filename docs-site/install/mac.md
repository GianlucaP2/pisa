# Install Pisa on a Mac

This page takes you from a Mac with nothing installed to the PISA app on your iPhone talking to your own Pisa
server. You need no programming knowledge. Follow the steps in order, do one thing at a time, and after each
step check that you see what the step says you should see. If you don't, the step says what to do.

The Mac becomes the server: it must stay on (and awake) for Pisa to answer your phone. Your phone reaches it
through **Tailscale**, a private network between your own devices: no domain name, no router settings, nothing
opened to the internet.

Other ways to install: [Linux](linux.md), [Raspberry Pi](raspberry-pi.md), [a rented server (VPS)](vps.md),
[Windows](windows.md) (coming soon, experimental). What the self-hosted setup does not do yet:
[Limits](../limits.md).

## What you need

- A Mac with a recent version of macOS. Docker Desktop supports the current macOS and the two versions before
  it; Tailscale needs macOS Ventura 13.0 or later.
- At least 8 GB of memory and 20 GB of free disk space (Pisa uses about 0.6 GB of memory idle and up to about
  2 GB when busy; its image is about 650 MB to download and 2.5 GB on disk: [Limits](../limits.md#size-and-memory)).
- An iPhone with iOS 16 or later, for the Tailscale app and the PISA app.
- An API key from an AI provider (for example OpenAI, Anthropic or Mistral), from that provider's website.
- The Pisa release you are installing: open the repository's **Releases** page and click the latest release.
  Its notes have an **Install** section with the exact commands for that release (the repository and the
  version already filled in). You copy two of them in steps 18 and 19. Below, the same commands are shown with
  `<owner>/<repo>` and `vX.Y.Z` where the release's own values go: never type those by hand, copy the lines
  from the release.
- About an hour.

A word on typing commands: a command is shown in a grey box. Copy it exactly (select it, then press
Command-C), paste it in Terminal (Command-V), and press Return. Change only the parts written between `<` and
`>`; the two install commands come from the release page, already complete.

## Part 1: Find out which chip your Mac has

**Step 1.** Click the Apple menu (the apple in the top-left corner of the screen), then choose
**About This Mac**.

You should see a small window. Look at the line under the Mac's name:

- If it says **Chip** (for example "Chip Apple M2"), your Mac has **Apple silicon**.
- If it says **Processor** followed by an Intel name, your Mac has an **Intel** chip.

Write down which one it is: you need it in step 2. Close the window.

## Part 2: Install Docker Desktop

Docker runs Pisa and its database in sealed boxes ("containers"), so they don't mix with the rest of your Mac.

**Step 2.** Open <https://docs.docker.com/desktop/setup/install/mac-install/> in Safari. Click the download
button that matches your chip from step 1: **Docker Desktop for Mac with Apple silicon**, or **Docker Desktop
for Mac with Intel chip**.

You should see `Docker.dmg` in your Downloads folder (the downloads arrow in Safari's toolbar shows it). If the
page looks different, look for the same two choices: the important part is to pick the one for your chip.

**Step 3.** Double-click `Docker.dmg` in your Downloads folder.

You should see a window with the Docker whale icon and an Applications folder icon.

**Step 4.** Drag the Docker icon onto the Applications folder icon. Wait until the copy finishes, then close the
window.

**Step 5.** Open your Applications folder (in Finder, choose Go > Applications) and double-click **Docker**.
If macOS asks whether you are sure you want to open an app downloaded from the internet, click **Open**.

You should see the Docker Subscription Service Agreement.

**Step 6.** Read the agreement and click **Accept**. Docker Desktop does not run until you accept it.

- If Docker shows a choice between **Use recommended settings** and **Use advanced settings** (older versions
  do), choose **Use recommended settings**, click **Finish**, and type your Mac password when asked.
- If it asks you to sign in or create a Docker account, you can skip it: Pisa does not need one.

You should see the Docker whale icon in the menu bar at the top of the screen, and the Docker Desktop window.

**Step 7.** Wait until Docker Desktop says its engine is running (the exact words depend on the version; while
it starts, the whale in the menu bar is animated). The first start can take a few minutes.

If it is still starting after 10 minutes, click the whale in the menu bar, choose **Quit Docker Desktop**, and
open Docker again from Applications.

**Step 8.** Make Docker start by itself when you sign in to the Mac. In Docker Desktop, open **Settings** (the
gear icon at the top right), then **General**, and turn on **Start Docker Desktop when you sign in to your
computer**. Click **Apply** if the button is there.

**Step 9.** Check how much memory Docker may use. Still in **Settings**, open **Resources**, then **Advanced**
(in some versions the section is called **VM resources**). Look at **Memory limit**: by default Docker may use
half of the Mac's memory, so 4 GB on an 8 GB Mac, which is enough for Pisa (about 2 GB at most). Leave it as
it is if it shows 4 GB or more; if it shows less, raise it to 4 GB and click **Apply & restart** (or
**Apply**). Close the Settings window.

## Part 3: Install Tailscale on the Mac and create your private network

Tailscale makes the private network (Tailscale calls it a **tailnet**) that your phone uses to reach Pisa. The
Pisa server joins it on its own, with a key you make in part 4; installing Tailscale on the Mac too gives you
the account and lets you check the network from the Mac.

**Step 10.** Open <https://tailscale.com/download/mac> in Safari and click **Download Tailscale for macOS**.
This is the version Tailscale recommends (the one in the Mac App Store also works, but may need an Apple
Account with a payment card on file).

You should see a file ending in `.pkg` in your Downloads folder.

**Step 11.** Double-click the `.pkg` file and follow the installer: click **Continue** and **Install**, and type
your Mac password when asked.

**Step 12.** Open Tailscale (from Applications, or from the icon that appears in the menu bar) and follow its
welcome screens. When macOS asks to allow a **VPN configuration** (or a system extension), allow it: this is
how Tailscale works. If macOS sends you to **System Settings** to allow it, do so there, then come back.

**Step 13.** When Tailscale asks you to sign in, choose an account you already have (for example Apple,
Google or Microsoft) and sign in in the browser window that opens. If this is your first time, this creates
your tailnet.

You should see Tailscale's icon in the menu bar; clicking it shows your account and this Mac as connected. If
it says it is disconnected, click it and choose to connect.

## Part 4: Prepare the tailnet for Pisa

**Step 14.** Open the Tailscale admin console's DNS page: <https://console.tailscale.com/admin/dns>. Sign in
with the same account as in step 13 if asked.

- If you see a button **Enable MagicDNS**, click it. (Tailnets created since October 2022 have MagicDNS on
  already, and then there is no such button.)
- Under **HTTPS Certificates**, click **Enable HTTPS** and confirm. If it already says HTTPS is enabled, there
  is nothing to do.

Why: your phone talks to Pisa over HTTPS at an address like `https://pisa.<your-tailnet>.ts.net`. Pisa's
Tailscale part uses Tailscale's `serve` feature for that, which works only with HTTPS certificates enabled,
and HTTPS certificates need MagicDNS. Good to know before you click: with
HTTPS on, the names of the machines that get a certificate (here, `pisa`) and your tailnet's name appear in
the public log every certificate is recorded in. Nothing else about you or your data does.

**Step 15.** Open the admin console's Keys page: <https://console.tailscale.com/admin/settings/keys>. Click
**Generate auth key**. In the form:

- Description: type `pisa`.
- **Reusable**: turn it **on** (Pisa may need to join again, for example after an update).
- Expiration: leave it as it is, or choose up to 90 days. The key is used only when Pisa joins; once it has
  joined, the key expiring does not disconnect it.
- **Ephemeral**: leave it **off** (an ephemeral device is removed when it goes offline, for example when the
  Mac restarts).
- Leave the other options as they are.

Click **Generate key**.

You should see a long key starting with `tskey-`. Click the copy button next to it and keep this browser tab
open: you paste the key in step 19. Treat it like a password: anyone with it can add a machine to your
tailnet. Tailscale shows it only once; if you lose it, generate a new one.

## Part 5: Install Pisa

**Step 16.** Open **Terminal**: press Command-Space, type `Terminal`, and press Return.

You should see a window with a line ending in `%` (or `$`). This is where you type commands.

**Step 17.** Check that Terminal finds Docker. Type this and press Return:

```bash
docker compose version
```

You should see a line like `Docker Compose version v2...`.

- If you see `command not found: docker`: quit Terminal (Command-Q), open it again, and try once more. If it
  still says so, make sure Docker Desktop is running (step 7), then type
  `export PATH="$HOME/.docker/bin:$PATH"` and press Return, and try again.
- If you see that it cannot connect to the Docker daemon: Docker Desktop is not running. Open it (step 7) and
  wait for the engine to run.

**Step 18.** Download the install script. (If the repository is still private, follow
[Testing a private release](#testing-a-private-release) instead of steps 18 and 19.) Open the release page (the repository's **Releases** page, then the
latest release). Under **Install**, in the **macOS or Linux** box, copy the first line, the one that starts with
`curl`, paste it in Terminal and press Return. It looks like this, with the release's own values:

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/<owner>/<repo>/vX.Y.Z/deploy/install.sh
```

You should see no message at all: that means it worked. If you see `404` or `could not resolve`, check that you
copied the whole line, and your internet connection. (Curious what it does? Type `open -e install.sh` to read it in TextEdit.
It installs nothing but Pisa's folder.)

**Step 19.** Run the install script. From the same box on the release page, copy the second line, the one that
starts with `PISA_REPO=`, paste it in Terminal and press Return. It looks like this:

```bash
PISA_REPO=<owner>/<repo> PISA_VERSION=vX.Y.Z bash install.sh
```

You should see `Installed the compose bundle in /Users/<you>/pisa` and then, the first time, Docker
downloading Pisa (lines with `Pulling` and progress), which can take a few minutes. Then `pisa init` asks its
questions, one at a time. For each, type the answer and press Return; to keep the answer shown in
`[brackets]`, just press Return.

1. `How will your phone reach Pisa? tailscale (no domain, no open port), caddy (your own domain), or local (this network only) [tailscale]:`
   Press Return (this keeps `tailscale`).
2. `Your home time zone (a city such as Rome, or a region and city such as Europe/Rome) [Europe/Rome]:`
   The installer reads your Mac's time zone and shows it in brackets: press Return to keep it. Otherwise type
   a region and city (`Europe/Rome`, `America/New_York`) or just the city (`Rome`, `New York`); init says which
   zone it used. If the city is in more than one zone, it lists them: type the one you mean.
3. `The language Pisa speaks (en or it) [en]:`
   Press Return for English, or type `it` for Italian.
   Then `Your country: its two-letter code or its name ... [IT]:` shows the country of your time zone (when
   the zone is in one country only): press Return to keep it, or type a code (`IT`, `US`) or a name in English
   or Italian (`Italy`, `Italia`). `-` means none.
4. A paragraph about the Tailscale auth key appears, then
   `Tailscale auth key (hidden; empty: add TS_AUTHKEY to the .env later):`
   Paste the key from step 15 (Command-V) and press Return. **Nothing appears while you paste: that is on
   purpose**, the key stays hidden.
5. `This server's name on the tailnet [pisa]:`
   Press Return (this keeps `pisa`).

You should see `Wrote /Users/<you>/pisa/deploy/.env (readable by you only).`, a list of what was generated,
and the sentence `Back up PISA_SECRETS_KEY somewhere safe: without it the stored provider keys can't be read.`
Then the installer starts Pisa: `Starting Pisa`, Docker downloading its parts the first time (a few minutes),
lines `waiting for ...` every five seconds, and finally `Pisa is running.` with the **Next:** steps, two
`docker compose` commands: `pisa setup` (part 7) and `pisa pair` (part 9). (To only write the settings and
start Pisa yourself later, add `--no-start` at the end of the install command.)

What went wrong, and what to do:

- `error: Docker is installed but not running`: Docker Desktop isn't open. Open the Docker Desktop app
  (Applications → Docker), wait until it shows **Engine running** (bottom left of its window), then run step 19
  again. To have it start by itself, turn on Docker Desktop's setting to start when you sign in (part 2).
- `error: Docker is not installed`: go back to part 2 (step 7).
- `No free address range for Pisa's Docker network`: every address range the installer tries is already used by
  another Docker network on this Mac. Run `docker network prune` (it removes only networks no container uses),
  then run step 19 again.
- `error: set PISA_REPO ...` or `PISA_VERSION ... is not pinned`: one of the two values is missing or
  mistyped. Run step 19 again with the right values.
- A mistyped answer is not fatal: init says what was wrong and asks again, up to three times. After a
  third bad answer it stops (`error: ... No valid answer after 3 tries`), having written nothing: run step 19
  again, or add the answer as a flag after `install.sh`, for example `--timezone Europe/Rome`.
- `Pisa is already set up in ...`: Pisa was already set up in this folder. Nothing was lost: the installer
  keeps the settings and starts Pisa; go on to step 20.
- If pulling the images fails with `input/output error`, quit Docker Desktop (whale menu → Quit Docker
  Desktop), open it again, wait for **Engine running**, and run the install command again.
- `error: Pisa did not become healthy within 180 seconds. Not ready: ...`: the installer names each part of
  Pisa that did not come up and the `docker compose ... logs` command that shows what it says. See step 21.

**Step 20.** Keep a copy of the key that protects your provider keys. Type:

```bash
grep PISA_SECRETS_KEY ~/pisa/deploy/.env
```

Copy the line it shows into your password manager (or print it and keep it somewhere safe). Never share it.
Without it, a restored backup cannot read the AI provider keys you store in part 7.

## Part 6: Check that Pisa started

**Step 21.** The installer started Pisa at the end of step 19 and said `Pisa is running.` If it did, go on to
step 22. If it stopped with an error, or you added `--no-start`, start it yourself:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env up -d
```

You should see lines for each part of Pisa ending in `Started`, `Healthy` or `Running`. The first start takes
a minute or two. (Running step 19 again does the same: on a set-up install it keeps the settings and starts
Pisa.)

If it says `failed to create network ... overlapping`, another Docker network on this Mac uses the same addresses.
Run step 19 again: the installer then picks a free range and writes it into `~/pisa/deploy/.env` (your settings,
passwords and keys stay as they are), and starts Pisa.

**Step 22.** Check that everything runs. Type:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env ps
```

You should see `postgres`, `api`, `worker` and `tailscale` listed as running (`Up`, and after a minute
`healthy`). `migrate` may not be in the list, or show that it exited: that is normal, it runs once at each
start.

If something is missing or keeps restarting, show its messages with:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env logs --tail 50
```

and see [Troubleshooting](../troubleshooting.md). To see Tailscale's own view, type:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec tailscale tailscale status
```

A wrong or expired Tailscale key shows up in the messages as Tailscale failing to log in: make a new key
(step 15), open the settings file with `open -e ~/pisa/deploy/.env`, replace the text after `TS_AUTHKEY=` with
the new key, save, close, and run step 21 again.

**Step 23.** Check that Pisa joined your tailnet. Open <https://console.tailscale.com/admin/machines>.

You should see a machine called `pisa` (as well as your Mac). Tailscale makes a machine sign in again after a
while (by default after 180 days), which Pisa can't do by itself: open the `...` menu at the right of the
`pisa` row and choose **Disable Key Expiry**, so that Pisa doesn't drop off the tailnet in six months.

## Part 7: Set up Pisa

**Step 24.** Type:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec worker pisa setup
```

Pisa's setup guide starts. Each step explains what it asks and why. Press Return to keep what is shown in
`[brackets]`.

1. **Your place in the world.** `Language (en, it)` and `Time zone` show what you chose in step 19: press
   Return for each. Then it shows the country you chose in step 19 (`Country: IT, ...`); there is nothing to
   type. If it says `No country is set`, it gives the command that sets it: the install command of step 19
   again, with `--force --country IT` (your country's code) at the end.
2. **The AI.** `Choose: cloud, own (your model server), or skip`: type `cloud` and press Return.
   `Provider id (e.g. openai, anthropic, mistral)`: type your provider's id and press Return.
   `API key (not shown)`: paste your provider's API key and press Return; again, nothing appears while you
   paste. You should see `Key stored (encrypted).` and a test result. The key is kept encrypted in Pisa's
   database, never in a file. `Model for the conversation (<provider>:<model>)`: type the provider id, a colon,
   and a model name from your provider's list of models (for example `openai:` followed by the model's name),
   then press Return. Pisa tests the model, which can take a minute.
3. **Push** (notifications on the phone). `Which will your phone use: relay, direct, or none`: press Return to
   keep what is shown. Pisa works without push; see [Notifications](../notifications.md) to change it later.
4. **Pairing.** `Pair a device now? Give its name, or press Enter to skip`: press Return. You pair the phone in
   part 9, once Tailscale is on it.

You should see ``Setup done. Run `pisa setup` again at any time.`` You can run step 24 again whenever you like.

If you see `error: the database is unreachable`: Pisa is not running. Go back to step 21.

## Part 8: Put Tailscale on the iPhone

**Step 25.** On the iPhone, open the App Store, search for **Tailscale**, and install it.

**Step 26.** Open Tailscale and tap **Get Started**. When iOS asks to add a VPN configuration, tap **Allow**
(and enter your passcode or use Face ID if asked). Allow notifications too: Tailscale uses them to tell you when
you need to sign in again.

**Step 27.** Tap **Log in** and sign in with **the same account as on the Mac** (step 13).

You should see your Mac and `pisa` in Tailscale's list of devices on the phone. If they are not there, you
signed in with a different account: log out in the Tailscale app and sign in with the right one.

## Part 9: Pair the PISA app

**Step 28.** Install the PISA app on the iPhone and open it, up to the point where it offers to scan a pairing
code.

**Step 29.** On the Mac, in Terminal, type:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec worker pisa pair
```

You should see `Scan this code with the Pisa app (valid until ...)`, a large QR code made of blocks, and a line
`No camera? Type this code in the app: ...`, then `Waiting for the phone...`. If the QR code looks broken,
make the Terminal window bigger (or press Command-minus a couple of times) and run step 29 again.

**Step 30.** Scan the QR code with the PISA app (not with the Camera app).

You should see, on the Mac, `Pairing request from ...` and `Does it show ...? Confirm only if the 6 digits are
the same [y/N]`, and six digits on the phone.

**Step 31.** Compare the six digits on the phone with the six digits in Terminal. If they are the same, type `y`
and press Return. If they are different, press Return: someone else may have tried to pair, and nothing is
paired; run step 29 again.

You should see `Paired: ...`. The PISA app is now connected to your own Pisa.

If the code expired (`The code expired without a request`), run step 29 again: a code is valid for 10 minutes.
If the app can't reach the server, check that Tailscale is on (connected) on the phone, and that `pisa` is in
its device list (step 27).

## Part 10: Check that it works

**Step 32.** In the PISA app, send a short message such as "Hello". You should get an answer within a few
seconds.

If you don't:

- On the phone, open Tailscale and check that it is connected.
- On the Mac, run step 22 and check that everything is `Up`.
- Run Pisa's own check:

  ```bash
  docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec worker pisa doctor
  ```

  It prints what it found, each with what to do. See also [Troubleshooting](../troubleshooting.md).
- An answer about the AI provider (a refused key, no credit) means the key from step 24 needs fixing: run step
  24 again.

Voice talks need more than text: see [Limits](../limits.md). When voice isn't available, text still works.

**Voice, once.** This now happens by itself: within a minute of the server's first login to your tailnet, Pisa
finds its tailnet address and offers it to the app at each voice talk. `PISA_ICE_HOST` in `~/pisa/deploy/.env`
stays empty. To see the address it found, run
`docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec api cat /etc/pisa/ts-cert/ip`:
it prints one address that starts with `100.` (like `100.64.0.7`). If it says there is no such file, the server
hasn't logged in to your tailnet yet: wait a minute and run it again. Until then, text works and voice talks
don't connect.

## Part 11: Verify text and voice end to end

Do these three checks once, in this order, the first time you install Pisa or after an update. They take about
five minutes.

**Step 33. Pairing.** You did this in Part 9. Success: Terminal said `Paired: ...` and the app shows it is
connected to your Pisa.

**Step 34. Text.** In the app, type: "Add a task: buy stamps", then "What's on my list?". Success: Pisa
confirms the task, and the list it reads back includes "buy stamps".

**Step 35. Voice.** Pisa finds its tailnet address by itself. To check that it has, run:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec api cat /etc/pisa/ts-cert/ip
```

Success: an address that starts with `100.`. If you see an error instead, Tailscale hasn't joined yet: check
step 27, wait a minute, and run it again. Then start a voice talk in the app and say "What's on my list?".

Success: you hear Pisa answer within a few seconds, and it mentions "buy stamps". If the app says voice isn't
available and offers text instead, voice did not connect: text still works, and the next paragraph says what
to send.

**If voice falls back to text, or the app asks you to allow AI.** Your AI consent covers voice talks too:
allow it in the app, or run this and answer `y`:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec worker pisa consent
```

To see why a natural voice talk can't open, run Pisa's live check. It opens a real voice session for a few
seconds (it spends a little) and prints the provider's exact error, with your keys hidden:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec worker pisa doctor --live
```

**If a check fails, send us these four things.** Read them first: none of them should hold anything private,
but remove anything that looks personal before sending.

1. Which step failed, and what you saw (a screenshot of the app is fine).
2. The output of:

   ```bash
   docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env ps
   ```

3. The output of Pisa's own check (for a voice problem, with `--live` added at the end):

   ```bash
   docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env exec worker pisa doctor
   ```

4. The last lines of the logs:

   ```bash
   docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env logs --tail 200 api worker tailscale voice-relay > ~/Desktop/pisa-logs.txt
   ```

   This saves the logs to `pisa-logs.txt` on your Desktop. At the default log level they hold no messages,
   tasks or keys.

Never send the `.env` file: it holds your passwords and keys.

## Every day

### Keep the Mac awake

Pisa answers only while the Mac is awake. Choose Apple menu > **System Settings**, then:

- On a desktop Mac (iMac, Mac mini, Mac Studio, Mac Pro): click **Energy** in the sidebar (scroll down if you
  don't see it) and turn on **Prevent automatic sleeping when the display is off**.
- On a MacBook: click **Battery** in the sidebar, click **Options**, and turn on **Prevent automatic sleeping on
  power adapter when the display is off**. Keep the MacBook plugged in, and its lid open.

The screen can still turn off; only the Mac must not sleep. Names can differ slightly between macOS versions:
Apple's guide is "Set sleep and wake settings for your Mac" in the Mac User Guide.

After a restart, sign in to the Mac: Docker starts (step 8), and Pisa starts with it on its own.

### Stop and start Pisa

To stop Pisa (your data stays):

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env stop
```

To start it again:

```bash
docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env up -d
```

### Update Pisa

Back up first (below), then run the install command from the new release (its notes on the **Releases**
page), as in step 18. It keeps every answer, password and key you already have, switches Pisa to the new
version and starts it: you should see `Pisa is running.`

The database is upgraded on its own as Pisa starts. Updates go only forward: going back to an older version
means restoring the backup taken before the update. See [Backups](../limits.md#backups).

### Back up

Backups are your job: see [Backups](../limits.md#backups) for the command that saves Pisa's database to a file,
and keep that file somewhere other than this Mac.

## Testing a private release

Before a repository is public, its releases (a test release such as `v0.1.0-rc.1` included) can only be
downloaded with a GitHub token, and its images only pulled after `docker login`. Do this instead of steps 18
and 19, then carry on with step 20.

1. Make a token. On github.com, click your profile picture (top right) → **Settings** → **Developer settings**
   (bottom of the left sidebar) → **Personal access tokens** → **Tokens (classic)** → **Generate new token** →
   **Generate new token (classic)**. **Note:** `pisa test install`. **Expiration:** 7 days. **Scopes:** tick
   `repo` and `read:packages`, nothing else. Click **Generate token**, and copy the token now (GitHub shows it
   once). Don't save it in a file.
2. Let Docker pull the private images. In Terminal, type this (with your GitHub username) and press Return:

   ```bash
   docker login ghcr.io -u <your GitHub username>
   ```

   At `Password:`, paste the token (Command-V; nothing shows) and press Return. You should see
   `Login Succeeded`.
3. Give the token to this Terminal window without it staying in the shell's history. Type this and press
   Return; then paste the token (nothing shows) and press Return again:

   ```bash
   read -rs PISA_GITHUB_TOKEN; export PISA_GITHUB_TOKEN
   ```

4. Open the release page (the repository's **Releases** page, then the release you are testing). Its
   **Install** section has a box headed **While this repository is private**: copy its second line (it starts
   with `curl` and ends with `Bearer $PISA_GITHUB_TOKEN"`), paste it and press Return, then its third line (it
   starts with `PISA_REPO=`), and press Return. They look like this, with the release's own values:

   ```bash
   curl -fsSLo install.sh -H @- https://raw.githubusercontent.com/<owner>/<repo>/vX.Y.Z-rc.1/deploy/install.sh <<< "Authorization: Bearer $PISA_GITHUB_TOKEN"
   PISA_REPO=<owner>/<repo> PISA_VERSION=vX.Y.Z-rc.1 bash install.sh
   ```

   The install script sees `PISA_GITHUB_TOKEN` and fetches the files with it; it says `Using PISA_GITHUB_TOKEN
   for the downloads`. It never prints the token or writes it into Pisa's settings. The rest is as in step 19.
5. Afterwards, once Pisa has started (the installer's `up -d` pulls the last images):

   ```bash
   unset PISA_GITHUB_TOKEN
   docker logout ghcr.io
   ```

   Then delete the token on GitHub (the same **Tokens (classic)** page → **Delete**).

While the repository stays private, updating (see [Update Pisa](#update-pisa)) needs a token and the
`docker login` again, and so does any later `pull` of the images. A test release is tagged and pushed by a
maintainer by hand, so it needs no release token on the repository's side.

## Uninstall

This deletes Pisa and **everything it knows**. Back up first if you may want it again.

1. Stop Pisa and delete its containers and its database:

   ```bash
   docker compose -f ~/pisa/deploy/docker-compose.yml --env-file ~/pisa/deploy/.env down --volumes
   ```

2. Delete Pisa's folder:

   ```bash
   rm -rf ~/pisa
   ```

3. Remove the `pisa` machine from your tailnet: open <https://console.tailscale.com/admin/machines>, open the
   `...` menu of the `pisa` row and remove the machine. On the Keys page
   (<https://console.tailscale.com/admin/settings/keys>), revoke the key from step 15.
4. In the PISA app, remove the server.
5. Optionally, remove Docker Desktop and Tailscale as their makers describe in their own documentation
   (Docker: "Uninstall Docker Desktop"; Tailscale: "Uninstall Tailscale").
