# Relay server

The relay lets friends play through an invite link, like
`https://relay.example.com/?invite=...`. The host at home connects *out* to the
relay, so the home connection needs no port forwarding, and its upload carries
one stream however many people watch.

```
friends' browsers ──https──▶ nginx (your subdomain) ──▶ relay on 127.0.0.1:8650
                                                             ▲
host: bluenetplay.py --relay wss://relay.example.com/console ┘ (outgoing)
```

What it needs on the server: Python 3.10+, nginx with a certificate for a
(sub)domain, and these paths: `/opt/bluenetplay-relay` (code and venv),
`/var/lib/bluenetplay-relay` (invites), `/etc/bluenetplay-relay/console_token`
(the host's secret) and a systemd service. It listens on `127.0.0.1:8650`
only, so it opens no new ports, and runs as its own user without a login.

Replace `relay.example.com` with your own domain everywhere below.

## 1. Copy the files

From the BlueNetplay folder on your computer:

```sh
scp -r relay web youruser@yourserver:/tmp/bluenetplay-relay/
```

On the server:

```sh
ss -ltn | grep -q ':8650 ' && echo "8650 is in use, pick another port" || echo "8650 is free"
sudo mkdir -p /opt/bluenetplay-relay
sudo cp -r /tmp/bluenetplay-relay/relay /tmp/bluenetplay-relay/web /opt/bluenetplay-relay/
sudo chown -R root:root /opt/bluenetplay-relay
```

If 8650 is taken, use another port here and in the service file.

## 2. User, Python and secret

```sh
sudo useradd --system --home-dir /var/lib/bluenetplay-relay --shell /usr/sbin/nologin bluenetplay
sudo mkdir -p /var/lib/bluenetplay-relay /etc/bluenetplay-relay
sudo chown bluenetplay:bluenetplay /var/lib/bluenetplay-relay
sudo chmod 750 /var/lib/bluenetplay-relay

sudo apt install -y python3-venv
sudo python3 -m venv /opt/bluenetplay-relay/venv
sudo /opt/bluenetplay-relay/venv/bin/pip install aiohttp

openssl rand -hex 32 | sudo tee /etc/bluenetplay-relay/console_token > /dev/null
sudo chown root:bluenetplay /etc/bluenetplay-relay/console_token
sudo chmod 640 /etc/bluenetplay-relay/console_token
```

`tee` writes the secret into the file without showing it. Keep it private:
anyone with it can act as your host.

## 3. Service

Set your domain in the service file, then start it:

```sh
sudo sed -i 's|https://relay.example.com|https://YOUR-DOMAIN|' /opt/bluenetplay-relay/relay/bluenetplay-relay.service
sudo cp /opt/bluenetplay-relay/relay/bluenetplay-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bluenetplay-relay
curl -s http://127.0.0.1:8650/healthz
```

`healthz` answers `{"ok": true, "console": false, ...}`. `"console": true`
means a host is connected. `daemon-reload` and restarting this service don't
affect other services or sites.

## 4. nginx and https

The relay needs a (sub)domain with a certificate (for example Let's Encrypt),
and nginx passing everything, WebSockets included, to the relay:

```nginx
location / {
    proxy_pass http://127.0.0.1:8650;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}
```

**Plesk example:** add the subdomain, install a Let's Encrypt certificate and turn on
the permanent http → https redirect. Under **Apache & nginx Settings**, untick
**Proxy mode**, and paste the block into **Additional nginx directives** with
`location ~ ^/ {` instead of `location / {`, so it doesn't clash with the
`location /` Plesk writes itself. It only applies to that subdomain.

Check from anywhere: `https://relay.example.com/healthz`. Without an invite,
the page itself answers 403.

## 5. Invite links

```sh
R="sudo -u bluenetplay /opt/bluenetplay-relay/venv/bin/python /opt/bluenetplay-relay/relay/relay.py --data /var/lib/bluenetplay-relay --public-url https://relay.example.com"

$R invite --name Host --host      # you: any player, can pair new pads
$R invite --name Bob --player 2   # Bob always plays player 2
$R invite --name Mum --watch      # video and sound only
$R invites                        # list all links
$R revoke Bob                     # stops working immediately
```

New and revoked invites work without a restart.

## 6. Connect the host

Show the secret once on the server and paste it into `relay_token.txt` next to
`bluenetplay.py` on the host:

```sh
sudo cat /etc/bluenetplay-relay/console_token
```

Then start the host with `--relay wss://relay.example.com/console`. Its log
shows `Connected to the relay` and `Relay video: avc1...`. Connect one host at
a time.

## Updating

Copy the changed files to the server as in step 1, then:

```sh
sudo cp /tmp/bluenetplay-relay/web/* /opt/bluenetplay-relay/web/
sudo cp /tmp/bluenetplay-relay/relay/relay.py /opt/bluenetplay-relay/relay/
sudo systemctl restart bluenetplay-relay
```

Web files are read on every page load, so the restart is only needed when
`relay.py` changed. Logs: `journalctl -u bluenetplay-relay -f`.

## Removing

```sh
sudo systemctl disable --now bluenetplay-relay
sudo rm /etc/systemd/system/bluenetplay-relay.service && sudo systemctl daemon-reload
sudo rm -rf /opt/bluenetplay-relay /var/lib/bluenetplay-relay /etc/bluenetplay-relay
sudo userdel bluenetplay
```

Then remove the nginx block or the subdomain.
