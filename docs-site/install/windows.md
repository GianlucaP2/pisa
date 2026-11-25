# Install Pisa on Windows

> **Coming soon / community testing.** Windows is [experimental](../experimental.md): nobody has run this page
> end to end yet, and it is not a supported way to install Pisa. For a supported install, use a
> [Mac](mac.md). If you try it, please report what happens (the step's number and what you saw), with an issue
> labelled `experimental`.

This page takes you from a Windows PC to the PISA app on your iPhone talking to your own Pisa server, reached
through **Tailscale** (a private network between your own devices: no domain, no router settings). It has the
same shape as the [Mac page](mac.md), which has more detail on the Tailscale and Pisa steps. What the
self-hosted setup does not do yet: [Limits](../limits.md).

## What you need

- 64-bit Windows 11 (version 23H2 or later) or Windows 10 (version 22H2), in an edition Docker Desktop
  supports, with at least 8 GB of memory and hardware virtualization turned on in the PC's firmware
  (BIOS/UEFI). Docker Desktop's install page lists the exact requirements: check them first.
- 20 GB of free disk space.
- An iPhone with iOS 16 or later, an AI provider's API key, and the release's install commands: copy them from
  the latest release on the repository's **Releases** page: its notes have an **Install** section with the exact commands for that release, the repository and the version already filled in. This page shows their form, with `<owner>/<repo>` and `vX.Y.Z` where the release's values go.

## Part 1: Docker Desktop with WSL 2

**Step 1.** Open <https://docs.docker.com/desktop/setup/install/windows-install/> and download Docker Desktop
for Windows (pick the Arm version only if your PC has an Arm processor).

You should see `Docker Desktop Installer.exe` in your Downloads folder.

**Step 2.** Double-click it. On the **Configuration** page, make sure **Use WSL 2 instead of Hyper-V** is
selected. Follow the installer, then select **Close**.

If the installer says WSL is missing or too old, follow what it offers (it can install or update WSL), or see
Microsoft's "Install WSL" guide, then run the installer again. Restart the PC if asked.

**Step 3.** Docker Desktop does not start by itself after installing: open the Start menu, search for
**Docker Desktop**, and open it. Read the Docker Subscription Service Agreement and select **Accept**. You can
skip signing in to Docker.

You should see Docker Desktop's window, and its whale icon in the notification area of the taskbar. Wait until
it says its engine is running.

**Step 4.** In Docker Desktop, open **Settings**, then **General**, and turn on **Start Docker Desktop when you
sign in to your computer**. Apply.

## Part 2: Tailscale

**Step 5.** Download Tailscale for Windows from <https://tailscale.com/download/windows> and run the installer.

You should see Tailscale's icon in the notification area (it may be hidden behind the up arrow).

**Step 6.** Right-click the icon, select **Log in**, and sign in in the browser with an account you already have
(Google, Microsoft, Apple...). This creates your tailnet if you have none.

**Step 7.** Prepare the tailnet, exactly as on the Mac: on the DNS page
(<https://console.tailscale.com/admin/dns>) enable **MagicDNS** if it is off and **Enable HTTPS** under **HTTPS
Certificates**; then on the Keys page (<https://console.tailscale.com/admin/settings/keys>) select **Generate
auth key**, turn **Reusable** on, leave **Ephemeral** off, and select **Generate key**. Copy the key (it starts
with `tskey-`). Why each setting: [Mac page, part 4](mac.md#part-4-prepare-the-tailnet-for-pisa).

## Part 3: Install Pisa

**Step 8.** Open **PowerShell**: open the Start menu, type `PowerShell`, and open **Windows PowerShell** (not as
administrator).

**Step 9.** Check that Docker answers:

```powershell
docker compose version
```

You should see `Docker Compose version v2...`. If `docker` is not recognized, close PowerShell, make sure
Docker Desktop is running, and open PowerShell again.

**Step 10.** Download the install script. Open the latest release on the repository's **Releases** page;
under **Install**, in the **Windows** box, copy the first line (it starts with `Invoke-WebRequest`), paste it in
PowerShell and press Enter. It looks like this:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/<owner>/<repo>/vX.Y.Z/deploy/install.ps1 -OutFile install.ps1
```

You should see no message. (To read it first: `notepad install.ps1`.)

**Step 11.** Allow scripts for this PowerShell window only. Windows refuses to run scripts by default; this
lifts that for the current window and nothing else (it is gone when you close it):

```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
```

Answer `Y` if asked.

**Step 12.** Run the install script: copy the second line of the same box (it starts with `$env:PISA_REPO`),
paste it and press Enter. It looks like this:

```powershell
$env:PISA_REPO = '<owner>/<repo>'; $env:PISA_VERSION = 'vX.Y.Z'; .\install.ps1
```

You should see `Installed the compose bundle in C:\Users\<you>\pisa`, Docker downloading Pisa, then
`pisa init`'s questions. Answer them as on the Mac
([step 19 there](mac.md#part-5-install-pisa)): press Enter for `tailscale`, type your time zone (for example
`Europe/London`), press Enter for English and for the country your time zone shows, paste the Tailscale key (nothing appears while you paste), press
Enter for the name `pisa`.

You should see `Wrote C:\Users\<you>\pisa\deploy/.env (readable by you only).`, then the installer starts
Pisa (Docker downloads it the first time: a few minutes) and waits until it runs: `Pisa is running.` and the
**Next:** steps, `pisa setup` and `pisa pair`. (To only write the settings, add `--no-start` at the end.)

If pulling the images fails with `input/output error`, quit Docker Desktop (whale menu → Quit Docker Desktop),
open it again, wait for **Engine running**, and run the install command again.

If it ends with `error: Pisa did not become healthy within 180 seconds`, it names each part that did not come
up and the `docker compose ... logs` command that shows what it says.

If it says `running scripts is disabled on this system`, do step 11 again in the same window.

**Step 13.** Keep a copy of `PISA_SECRETS_KEY` somewhere safe (a password manager):

```powershell
Select-String PISA_SECRETS_KEY $HOME\pisa\deploy\.env
```

## Part 4: Start, set up, pair

**Step 14.** The installer started Pisa at the end of step 12. If it stopped with an error, or you added
`--no-start`, start it yourself:

```powershell
docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env up -d
```

**Step 15.** Check it (all `Up`, then `healthy`: `postgres`, `api`, `worker`, `tailscale`):

```powershell
docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env ps
```

Then check that a machine called `pisa` is on <https://console.tailscale.com/admin/machines>, and disable its key
expiry there (the `...` menu, **Disable Key Expiry**).

**Step 16.** Set up Pisa: your country, your AI provider and its key (stored encrypted in Pisa's database, never
in a file), push. Answers: [Mac page, part 7](mac.md#part-7-set-up-pisa).

```powershell
docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env exec worker pisa setup
```

**Step 17.** On the iPhone, install Tailscale from the App Store, tap **Get Started**, allow the VPN
configuration, and log in with the **same account** as on the PC. You should see `pisa` in its device list.

**Step 18.** Install and open the PISA app. On the PC, show the pairing code:

```powershell
docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env exec worker pisa pair
```

Scan the QR code with the PISA app. When PowerShell asks `Does it show ...? Confirm only if the 6 digits are the
same [y/N]`, compare the six digits with the phone's: type `y` and press Enter only if they are the same. You
should see `Paired: ...`. If the QR code looks broken, make the window wider and run it again.

Voice talks need nothing more: the Tailscale container writes the server's tailnet address and Pisa offers it to
the phone at each talk. To check, run
`docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env exec api cat /etc/pisa/ts-cert/ip`:
it prints an address starting with `100.` ([Limits](../limits.md)).

**Step 19.** Send "Hello" in the PISA app: you should get an answer. If not, see the checks in
[Mac page, part 10](mac.md#part-10-check-that-it-works) (the commands are the same, with the paths above).

## Every day

- **Keep the PC awake:** in Start, Settings, System, **Power & battery** (on a desktop, **Power**), under the
  screen and sleep settings, set the PC never to sleep when plugged in. The screen may turn off.
- **Stop:** `docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env stop`;
  **start:** the same with `up -d`.
- **Update:** back up ([Backups](../limits.md#backups)), then run the install command from the new release
  (its notes on the **Releases** page), as in step 10. It keeps your settings and keys, switches Pisa to the new
  version and starts it.

## Uninstall

This deletes Pisa and everything it knows; back up first if you may want it again.

```powershell
docker compose -f $HOME\pisa\deploy\docker-compose.yml --env-file $HOME\pisa\deploy\.env down --volumes
Remove-Item -Recurse -Force $HOME\pisa
```

Then remove the `pisa` machine and revoke the key in the Tailscale admin console, remove the server in the PISA
app, and, if you like, uninstall Docker Desktop and Tailscale from Windows' installed apps.
