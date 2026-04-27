# Native Zigbee2MQTT on Proxmox LXC

This layout is for running Zigbee2MQTT directly inside the LXC without Docker.

## 1. Proxmox host

Bind the USB serial device into the LXC by editing `/etc/pve/lxc/<CTID>.conf` on the Proxmox host:

```ini
lxc.cgroup2.devices.allow: c 188:* rwm
lxc.mount.entry: /dev/ttyUSB0 dev/ttyUSB0 none bind,optional,create=file
```

Restart the container:

```bash
pct restart <CTID>
```

## 2. Inside the LXC

Install packages:

```bash
apt update
apt install -y curl git make g++ gcc mosquitto mosquitto-clients
curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
apt install -y nodejs
corepack enable
```

Create the Zigbee2MQTT user and install location:

```bash
useradd --system --create-home --home-dir /var/lib/zigbee2mqtt --shell /usr/sbin/nologin zigbee2mqtt
git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git /opt/zigbee2mqtt
cd /opt/zigbee2mqtt
pnpm install --frozen-lockfile
mkdir -p /opt/zigbee2mqtt/data
cp /path/to/this/repo/zigbee-stack-native/zigbee2mqtt/configuration.yaml /opt/zigbee2mqtt/data/configuration.yaml
chown -R zigbee2mqtt:zigbee2mqtt /opt/zigbee2mqtt /var/lib/zigbee2mqtt
```

Install Mosquitto config:

```bash
cp /path/to/this/repo/zigbee-stack-native/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/zigbee2mqtt.conf
mosquitto_passwd -c /etc/mosquitto/password_file zigbee2mqtt
systemctl enable --now mosquitto
```

Install the systemd unit:

```bash
cp /path/to/this/repo/zigbee-stack-native/systemd/zigbee2mqtt.service /etc/systemd/system/zigbee2mqtt.service
systemctl daemon-reload
systemctl enable --now zigbee2mqtt
```

Watch logs:

```bash
journalctl -u zigbee2mqtt -f
```

## Notes

- If your adapter is not TI-based, change `adapter: zstack` in `configuration.yaml`.
- If the device appears as `/dev/ttyACM0`, update `serial.port`.
- Keep the dongle on a short USB extension cable for better Zigbee stability.
