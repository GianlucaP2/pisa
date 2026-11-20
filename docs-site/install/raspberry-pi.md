# Install Pisa on a Raspberry Pi

A Raspberry Pi makes a quiet, low-power Pisa server. The steps are the [Linux](linux.md) ones; this page says
what is particular to the Pi. What the self-hosted setup does not do yet: [Limits](../limits.md).

## What you need

- A **Raspberry Pi 4 or 5**, ideally with 4 GB of memory or more (Pisa uses about 2 GB). Other models are not
  supported.
- **Raspberry Pi OS (64-bit) only.** Pisa's images are for arm64; the 32-bit Raspberry Pi OS cannot run them,
  and the install script stops on it.
- **An SSD, recommended** instead of a microSD card: a database writes all the time, and microSD cards are slow
  and wear out. Raspberry Pi's documentation explains which boot media your model can start from (USB drives,
  and NVMe drives through an adapter on models that support it).
- The official power supply for your model and, ideally, a wired network connection.

## 1. The operating system

Use **Raspberry Pi Imager** (download it from <https://www.raspberrypi.com/software/>): choose your Pi model,
then a 64-bit Raspberry Pi OS (**Raspberry Pi OS (64-bit)**; its Lite version, with no desktop, is enough), then
your SSD or card. In the customisation screens, set a hostname, a user and password, your Wi-Fi if needed, and
turn on **Enable SSH** so that you can work from another computer. Start the Pi from it, then update it:

```bash
sudo apt update && sudo apt full-upgrade -y
uname -m          # must print aarch64
```

## 2. Docker

Raspberry Pi OS (64-bit) is Debian for arm64: install Docker Engine with Docker's **Debian** instructions,
<https://docs.docker.com/engine/install/debian/>, using the `apt` repository, then the post-installation steps
(start at boot, the `docker` group), as on the [Linux page](linux.md#1-docker-engine).

## 3. Pisa

Follow the [Linux page](linux.md) from [section 2](linux.md#2-the-tailnet): the tailnet, the install script,
then start, set up and pair. The first start downloads the arm64 images, which takes a while on a Pi.

Keep the Pi powered and connected: it has no sleep setting to change.
