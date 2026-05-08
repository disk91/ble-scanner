#!/usr/bin/env python3

import argparse
import asyncio
import json
import signal
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

from bleak import BleakScanner


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS devices (
    address TEXT PRIMARY KEY,
    name TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    last_rssi INTEGER,
    is_present INTEGER NOT NULL DEFAULT 0,
    session_max_rssi INTEGER,
    manufacturer_data TEXT,
    service_uuids TEXT
);

CREATE TABLE IF NOT EXISTS presence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    name TEXT,
    event_type TEXT NOT NULL CHECK(event_type IN ('appeared', 'disappeared')),
    ts REAL NOT NULL,
    rssi INTEGER,
    manufacturer_data TEXT,
    service_uuids TEXT,
    FOREIGN KEY(address) REFERENCES devices(address)
);

CREATE INDEX IF NOT EXISTS idx_presence_address_ts
ON presence_events(address, ts);

CREATE INDEX IF NOT EXISTS idx_presence_ts
ON presence_events(ts);
"""


class BLEPresenceDaemon:
    def __init__(
        self,
        db_path: Path,
        adapter: str,
        lost_after: float,
        flush_interval: float,
        passive: bool,
        debug: bool,
    ):
        self.db_path = db_path
        self.adapter = adapter
        self.lost_after = lost_after
        self.flush_interval = flush_interval
        self.passive = passive
        self.debug = debug

        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(SCHEMA)

        self.safe_migration("devices", "is_present", "INTEGER NOT NULL DEFAULT 0")
        self.safe_migration("devices", "session_max_rssi", "INTEGER")
        self.safe_migration("devices", "manufacturer_data", "TEXT")
        self.safe_migration("devices", "service_uuids", "TEXT")
        self.safe_migration("presence_events", "manufacturer_data", "TEXT")
        self.safe_migration("presence_events", "service_uuids", "TEXT")

        self.conn.execute("UPDATE devices SET is_present = 0, session_max_rssi = NULL")
        self.conn.commit()

        self.last_seen: Dict[str, float] = {}
        self.names: Dict[str, Optional[str]] = {}
        self.rssi: Dict[str, Optional[int]] = {}
        self.present: Dict[str, bool] = {}
        self.session_max_rssi: Dict[str, Optional[int]] = {}
        self.manufacturer_data: Dict[str, Optional[str]] = {}
        self.service_uuids: Dict[str, Optional[str]] = {}

        self.dirty = False
        self.stop_event = asyncio.Event()

    def safe_migration(self, table: str, column: str, column_type: str):
        try:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        except sqlite3.OperationalError:
            pass

    def now(self) -> float:
        return time.time()

    def encode_manufacturer_data(self, data) -> Optional[str]:
        if not data:
            return None

        encoded = {}

        for company_id, raw in data.items():
            encoded[str(company_id)] = raw.hex()

        return json.dumps(encoded, separators=(",", ":"))

    def encode_service_uuids(self, service_uuids) -> Optional[str]:
        if not service_uuids:
            return None

        return json.dumps(list(service_uuids), separators=(",", ":"))

    def insert_event(
        self,
        address: str,
        name: Optional[str],
        event_type: str,
        ts: float,
        rssi: Optional[int],
        manufacturer_data: Optional[str],
        service_uuids: Optional[str],
    ):
        self.conn.execute(
            """
            INSERT INTO presence_events(
                address,
                name,
                event_type,
                ts,
                rssi,
                manufacturer_data,
                service_uuids
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                address,
                name,
                event_type,
                ts,
                rssi,
                manufacturer_data,
                service_uuids,
            ),
        )

        self.dirty = True

    def on_detected(self, device, adv_data):
        ts = self.now()

        address = device.address
        name = adv_data.local_name or device.name or self.names.get(address)
        rssi = getattr(adv_data, "rssi", None)

        manufacturer_data = self.encode_manufacturer_data(
            getattr(adv_data, "manufacturer_data", None)
        )

        service_uuids = self.encode_service_uuids(
            getattr(adv_data, "service_uuids", None)
        )

        first_time_seen = address not in self.last_seen
        was_present = self.present.get(address, False)

        self.last_seen[address] = ts
        self.names[address] = name
        self.rssi[address] = rssi
        self.present[address] = True

        if manufacturer_data:
            self.manufacturer_data[address] = manufacturer_data

        if service_uuids:
            self.service_uuids[address] = service_uuids

        if rssi is not None:
            current_max = self.session_max_rssi.get(address)

            if current_max is None or rssi > current_max:
                self.session_max_rssi[address] = rssi

        if self.debug:
            print(
                {
                    "address": address,
                    "name": name,
                    "rssi": rssi,
                    "manufacturer_data": manufacturer_data,
                    "service_uuids": service_uuids,
                }
            )

        self.dirty = True

        if first_time_seen or not was_present:
            self.insert_event(
                address,
                name,
                "appeared",
                ts,
                rssi,
                manufacturer_data,
                service_uuids,
            )

    def flush_devices(self):
        ts = self.now()

        for address, last_seen in self.last_seen.items():
            name = self.names.get(address)
            rssi = self.rssi.get(address)

            self.conn.execute(
                """
                INSERT INTO devices(
                    address,
                    name,
                    first_seen,
                    last_seen,
                    last_rssi,
                    is_present,
                    session_max_rssi,
                    manufacturer_data,
                    service_uuids
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(address) DO UPDATE SET
                    name = COALESCE(excluded.name, devices.name),
                    last_seen = excluded.last_seen,
                    last_rssi = excluded.last_rssi,
                    is_present = excluded.is_present,
                    session_max_rssi = excluded.session_max_rssi,
                    manufacturer_data = COALESCE(excluded.manufacturer_data, devices.manufacturer_data),
                    service_uuids = COALESCE(excluded.service_uuids, devices.service_uuids)
                """,
                (
                    address,
                    name,
                    ts,
                    last_seen,
                    rssi,
                    1 if self.present.get(address, False) else 0,
                    self.session_max_rssi.get(address),
                    self.manufacturer_data.get(address),
                    self.service_uuids.get(address),
                ),
            )

        self.conn.commit()
        self.dirty = False

    async def disappearance_loop(self):
        while not self.stop_event.is_set():
            ts = self.now()

            for address, is_present in list(self.present.items()):
                if not is_present:
                    continue

                last = self.last_seen.get(address, 0)

                if ts - last >= self.lost_after:
                    self.present[address] = False

                    self.insert_event(
                        address,
                        self.names.get(address),
                        "disappeared",
                        last + self.lost_after,
                        self.rssi.get(address),
                        self.manufacturer_data.get(address),
                        self.service_uuids.get(address),
                    )

            if self.dirty:
                self.flush_devices()

            await asyncio.sleep(self.flush_interval)

    async def run(self):
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_event.set)

        scanning_mode = "passive" if self.passive else "active"

        scanner = BleakScanner(
            detection_callback=self.on_detected,
            scanning_mode=scanning_mode,
            bluez={"adapter": self.adapter},
        )

        print(f"BLE scan started on {self.adapter}")
        print(f"Database: {self.db_path}")
        print(f"Scanning mode: {scanning_mode}")

        async with scanner:
            task = asyncio.create_task(self.disappearance_loop())
            await self.stop_event.wait()
            task.cancel()

        if self.dirty:
            self.flush_devices()

        self.conn.close()
        print("Stopped.")


def main():
    parser = argparse.ArgumentParser(description="BLE Presence Scanner")

    parser.add_argument("--db", default="ble_presence.db")
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--lost-after", type=float, default=30.0)
    parser.add_argument("--flush-interval", type=float, default=5.0)
    parser.add_argument("--passive", action="store_true")
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    daemon = BLEPresenceDaemon(
        db_path=Path(args.db),
        adapter=args.adapter,
        lost_after=args.lost_after,
        flush_interval=args.flush_interval,
        passive=args.passive,
        debug=args.debug,
    )

    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
