# Run on a Windows home PC connected to JioFiber

Use **Windows 11 22H2 or newer**, Ubuntu in WSL2 with **mirrored networking**, and **Docker Engine installed inside that Ubuntu distribution**. Keep the PC connected to the same Fiber router, with working IPv6, and awake during calls.

This gateway opens IPv6 SIP/TLS connections and advertises its local IPv6 address for RTP audio. The Compose file uses Linux host networking. Docker Desktop's host networking has different interface/binding behavior, so it is not the deployment path described here. Windows 10's default WSL NAT configuration is not suitable for this setup. See [Microsoft's mirrored networking documentation](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking) and [Docker's host networking limitations](https://docs.docker.com/engine/network/drivers/host/#limitations).

The image and local synthetic tests can be checked elsewhere, but actual registration and audio must be verified on your Windows/Fiber network.

## 1. Set up Ubuntu and mirrored networking

In **Administrator PowerShell**:

```powershell
wsl --install -d Ubuntu-24.04
wsl --update
```

Restart Windows if prompted. Launch Ubuntu once and create its Linux user account.

Open the WSL configuration from PowerShell:

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

Add these settings, merging them into an existing `[wsl2]` section if present:

```ini
[wsl2]
networkingMode=mirrored
dnsTunneling=true
```

Then restart WSL from PowerShell:

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04
```

Inside **Ubuntu**, check that systemd is running:

```bash
ps -p 1 -o comm=
```

If it does not say `systemd`, edit `/etc/wsl.conf` with `sudo nano /etc/wsl.conf` and merge in:

```ini
[boot]
systemd=true
```

Restart WSL again. See [Microsoft's systemd instructions](https://learn.microsoft.com/en-us/windows/wsl/systemd).

## 2. Install Docker Engine inside Ubuntu

Follow [Docker's Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository), including the Compose plugin. Use the Docker apt repository method, inside Ubuntu.

If Docker Desktop is also installed, disable its **WSL integration for this Ubuntu distribution** so its daemon does not replace the one running inside Ubuntu. This guide uses `sudo docker` and the local Linux daemon:

```bash
sudo systemctl enable --now docker
sudo docker -H unix:///var/run/docker.sock info --format '{{.OSType}}'
```

Expected: `linux`. Run the remaining commands from Ubuntu with `DOCKER_HOST` and `DOCKER_CONTEXT` overrides unset:

```bash
unset DOCKER_HOST DOCKER_CONTEXT
sudo docker context use default
```

## 3. Get the gateway

Clone the project into the Linux filesystem under `~`. Don't run it directly from `/mnt/c`.

```bash
sudo apt install -y git
git clone https://github.com/Ibnealamkhan/fiber-voice.git ~/fiber-voice
mkdir -p ~/fiber-voice/gateway/.runtime
```

**New setup (most people):** leave `.runtime` empty. After the gateway starts, the page at `http://127.0.0.1:8765/` walks you through trusting the router and entering the OTP sent to your registered mobile.

**Moving an existing setup from a Mac** (optional, so you skip the OTP): copy these three hidden private files from the Mac's `gateway/.runtime/` (or `~/Library/Application Support/Fiber Voice/`) into `~/fiber-voice/gateway/.runtime/`:

- `provisioning.json`
- `client-identity.json`
- `router-trust.json`

They contain the registration and SIP credentials. Use a private transfer method; Git and the Docker build deliberately exclude them. Don't paste them into chat or publish them, and don't edit them. Then **quit the Mac copy**, so two copies of the same identity never run at once.

In Ubuntu:

```bash
cd ~/fiber-voice/gateway
chmod 700 .runtime
chmod 600 .runtime/*.json 2>/dev/null || true
printf 'GATEWAY_UID=%s\nGATEWAY_GID=%s\n' "$(id -u)" "$(id -g)" > .env
chmod 600 .env
```

Any copied runtime files must belong to your Ubuntu user. If they were copied as root, fix that ownership before continuing. Compose uses the UID/GID in `.env` so the container can read the private files without running as root.

## 4. Build and check the network

```bash
sudo docker compose build
sudo docker compose run --rm --no-deps gateway python docker_check.py
```

The check verifies both native codecs, IPv6 router DNS/connectivity, binding a UDP socket to the selected local IPv6 address and, if you copied an existing setup, the saved files and TLS certificate pin. It does **not** send an OTP, REGISTER, or place a call.

If it fails:

- Incomplete setup or permission denied: copy all three private files or none, and check ownership and the `.env` UID/GID.
- No IPv6 router address or network unreachable: check Windows/WSL IPv6, mirrored mode, and that the PC is on the Fiber router. The router hostname is `jiofiber.local.html`; use the Fiber router's DNS for that local name.
- Router certificate mismatch: stop and verify that this is the original router. Do not bypass the saved pin.

## 5. Start and open the dialer

```bash
sudo docker compose up -d
sudo docker compose logs --tail=50 gateway
sudo docker compose ps
```

Open **http://127.0.0.1:8765/** in Chrome or Edge **on the Windows PC**. WSL mirrored mode supports IPv4 localhost access between Windows and Linux. The HTTP server stays bound to loopback.

Wait for **SIP REGISTERED**, then use **Start audio test** to check microphone/playback without a carrier call. After that, place a call yourself and verify audio in both directions. Container health means the web API is responding; it does not mean SIP registration or audio has succeeded.

If the router has revoked the saved provisioning, stop the server and explicitly enroll again:

```bash
sudo docker compose stop gateway
sudo docker compose run --rm --no-deps gateway python enroll.py --replace
sudo docker compose up -d
```

The enrollment helper prompts for the new OTP interactively. A fresh OTP is not needed on normal restarts.

## Audio and Windows firewall

If the page works but audio does not, check the Windows/Hyper-V firewall. The router audio sockets use UDP **52000–52200**; aiortc's WebRTC sockets use the Linux ephemeral UDP range. Read that range inside Ubuntu:

```bash
cat /proc/sys/net/ipv4/ip_local_port_range
```

If it reports `32768 60999`, the following **Administrator PowerShell** rules allow those UDP ranges for WSL. Replace the WebRTC range if yours differs. These rules apply to the WSL VM creator; do not disable the firewall globally.

```powershell
New-NetFirewallHyperVRule -Name JioFiberRTP -DisplayName "Jio Fiber RTP" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol UDP -LocalPorts '52000-52200' -Action Allow
New-NetFirewallHyperVRule -Name JioFiberWebRTC -DisplayName "Jio Fiber WebRTC" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol UDP -LocalPorts '32768-60999' -Action Allow
```

See [Microsoft's WSL firewall guidance](https://learn.microsoft.com/en-us/windows/wsl/networking#wsl-and-firewall) and [Hyper-V firewall rules](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallhypervrule). A separately configured Windows or third-party firewall may also need equivalent UDP allowances. Incoming packet delivery and browser ICE connectivity still need an actual audio test on the PC.

Remove the added rules later with `Remove-NetFirewallHyperVRule -Name JioFiberRTP` and `Remove-NetFirewallHyperVRule -Name JioFiberWebRTC`.

## Stop, restart and test

From Ubuntu in `~/fiber-voice/gateway`:

```bash
sudo docker compose stop
sudo docker compose up -d
sudo docker compose run --rm --no-deps gateway python -m unittest discover -s tests -v
```

Tests use synthetic local peers and do not place carrier calls. Do not run them alongside a live call because they share the host's test/media port range.

`restart: unless-stopped` restarts the container while Docker is running. It does not keep WSL alive or prevent Windows sleep, and it does not set up Windows startup/login automation. Keep Ubuntu/WSL running and the PC awake; start Docker/Compose after a reboot as needed.

## Phone access and Tailscale

Docker does not add phone access. The current app accepts localhost requests only. A phone needs a separately configured private HTTPS origin and a working WebRTC media path. Tailscale Serve can proxy the page/API, but that alone does not tunnel WebRTC UDP audio. Do not publish the gateway through Funnel or change its bind address expecting phone calls to work automatically.
