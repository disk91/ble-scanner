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


