# ble-scanner
Scan the BLE device around and track in/out - 100% vibe coded
Work on a RPI 2W with the native BLE adapter and also tested with TP-LINK UB500 Plus for long range coverage.
RPI2W installed with ubuntu 26

## Install

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip bluetooth bluez rfkill
sudo systemctl enable --now bluetooth
rfkill list
sudo rfkill unblock bluetooth
git clone https://github.com/disk91/ble-scanner.git
cd ble-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install bleak rich
```

## Run deamon

`ble_deamon.py` run background and capture the in/out in a database it takes parameters
- adapter : select the BLE adapter
- lost-after : out event after xx second not seen

```bash
source .venv/bin/activate
python3 ble_daemon.py --adapter hci0 --lost-after 90
```

## Configure as a service

In `/etc/systemd/system/ble-daemon.service`
```
[Unit]
Description=BLE Presence Daemon
# Démarre après que le réseau et BlueZ soient disponibles
After=network.target bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/ubuntu/ble-presence

# Débloque l'adaptateur si rfkill le bloque (ignore l'échec si rfkill absent)
ExecStartPre=-/usr/sbin/rfkill unblock bluetooth
# Remet hci0 UP (ignore l'échec si l'adaptateur est temporairement absent)
ExecStartPre=-/usr/bin/hciconfig hci0 reset
ExecStartPre=-/usr/bin/hciconfig hci0 up
# Laisse 1s à BlueZ pour enregistrer l'adaptateur sur D-Bus après le up
ExecStartPre=/bin/sleep 1

ExecStart=/home/ubuntu/ble-presence/.venv/bin/python ble_daemon.py \
    --db /home/ubuntu/ble-presence/ble_presence.db \
    --adapter hci0 \
    --lost-after 30 \
    --flush-interval 5 \
    --watchdog-timeout 60

# Relancer automatiquement si le process crash (filet de sécurité)
Restart=always
RestartSec=5

# Donner les droits nécessaires pour accéder au hardware BLE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

# Logs visibles dans journalctl -u ble-daemon -f
StandardOutput=journal
StandardError=journal

[Install]
# Démarre automatiquement en mode multi-utilisateur (boot normal)
WantedBy=multi-user.target
```


## Get the results

`ble_cli.py` extract the information for the database with options
- `devices` list the seen devices
- `events` list the events (in / out) by date
- `history` list events for a particular device
- `live` display the live scan

#### examples

```bash
source .venv/bin/activate
python3 ble_cli.py devices --details
python3 ble_cli.py events --limit 30
python3 ble_cli.py history AA:BB:CC:DD:EE:FF
python3 ble_cli.py live --refresh 0.5
``` 


