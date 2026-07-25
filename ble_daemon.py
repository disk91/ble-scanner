#!/usr/bin/env python3

import argparse
import asyncio
import json
import logging
import signal
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

from bleak import BleakScanner
from bleak.exc import BleakError


# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────────
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

# FIX 2 — Durée avant de supprimer un device absent des dicts en mémoire
MEMORY_PURGE_AFTER = 3600.0  # 1 heure

# Nombre max d'échecs consécutifs "adapter not found" avant de quitter
# proprement et laisser systemd relancer tout le stack BlueZ
MAX_ADAPTER_FAILURES = 5


class BLEPresenceDaemon:
    def __init__(
        self,
        db_path: Path,
        adapter: str,
        lost_after: float,
        flush_interval: float,
        passive: bool,
        debug: bool,
        watchdog_timeout: float,  # FIX 3
    ):
        self.db_path = db_path
        self.adapter = adapter
        self.lost_after = lost_after
        self.flush_interval = flush_interval
        self.passive = passive
        self.debug = debug
        self.watchdog_timeout = watchdog_timeout

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

        # ── État en mémoire ───────────────────────────────────────────────
        self.first_seen: Dict[str, float] = {}     # FIX 4 — heure réelle de première vue
        self.last_seen: Dict[str, float] = {}
        self.names: Dict[str, Optional[str]] = {}
        self.rssi: Dict[str, Optional[int]] = {}
        self.present: Dict[str, bool] = {}
        self.session_max_rssi: Dict[str, Optional[int]] = {}
        self.manufacturer_data: Dict[str, Optional[str]] = {}
        self.service_uuids: Dict[str, Optional[str]] = {}

        self.dirty = False
        self.stop_event = asyncio.Event()

        # FIX 3 — Watchdog : timestamp du dernier callback + signal de restart
        self.last_callback_ts: float = time.time()
        self._need_restart = asyncio.Event()

        # Compteur d'échecs consécutifs "adapter not found"
        self._adapter_fail_count: int = 0

    # ── Helpers ───────────────────────────────────────────────────────────────

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
        return json.dumps(
            {str(k): v.hex() for k, v in data.items()},
            separators=(",", ":"),
        )

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
                address, name, event_type, ts, rssi, manufacturer_data, service_uuids
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (address, name, event_type, ts, rssi, manufacturer_data, service_uuids),
        )
        self.dirty = True

    # ── Callback BLE ──────────────────────────────────────────────────────────

    def on_detected(self, device, adv_data):
        # FIX 5 — Protéger le callback : une exception non catchée ici peut
        #          faire taire silencieusement certaines versions de Bleak.
        try:
            ts = self.now()
            self.last_callback_ts = ts  # FIX 3 — nourrir le watchdog

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

            # FIX 4 — Enregistrer l'heure réelle de première détection
            if first_time_seen:
                self.first_seen[address] = ts

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
                log.debug("Detected: %s (%s) rssi=%s", address, name, rssi)

            self.dirty = True

            if first_time_seen or not was_present:
                self.insert_event(
                    address, name, "appeared", ts, rssi,
                    manufacturer_data, service_uuids,
                )

        except Exception:
            log.exception("Error in on_detected for %s", getattr(device, "address", "?"))

    # ── Flush SQLite ──────────────────────────────────────────────────────────

    def flush_devices(self):
        for address, last_seen in self.last_seen.items():
            self.conn.execute(
                """
                INSERT INTO devices(
                    address, name, first_seen, last_seen, last_rssi,
                    is_present, session_max_rssi, manufacturer_data, service_uuids
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    name             = COALESCE(excluded.name, devices.name),
                    last_seen        = excluded.last_seen,
                    last_rssi        = excluded.last_rssi,
                    is_present       = excluded.is_present,
                    session_max_rssi = excluded.session_max_rssi,
                    manufacturer_data = COALESCE(excluded.manufacturer_data, devices.manufacturer_data),
                    service_uuids    = COALESCE(excluded.service_uuids, devices.service_uuids)
                """,
                (
                    address,
                    self.names.get(address),
                    self.first_seen.get(address, last_seen),  # FIX 4
                    last_seen,
                    self.rssi.get(address),
                    1 if self.present.get(address, False) else 0,
                    self.session_max_rssi.get(address),
                    self.manufacturer_data.get(address),
                    self.service_uuids.get(address),
                ),
            )
        self.conn.commit()
        self.dirty = False

    # FIX 2 — Purge mémoire ───────────────────────────────────────────────────

    def purge_stale_memory(self):
        """Supprime des dicts en mémoire les devices absents depuis plus de
        MEMORY_PURGE_AFTER secondes, pour éviter la fuite mémoire."""
        cutoff = self.now() - MEMORY_PURGE_AFTER

        stale = [
            addr for addr, ts in self.last_seen.items()
            if ts < cutoff and not self.present.get(addr, False)
        ]

        for addr in stale:
            for d in (
                self.first_seen, self.last_seen, self.names, self.rssi,
                self.present, self.session_max_rssi,
                self.manufacturer_data, self.service_uuids,
            ):
                d.pop(addr, None)

        if stale:
            log.info("Purged %d stale device(s) from memory", len(stale))

    # ── Boucles asynchrones ───────────────────────────────────────────────────

    async def disappearance_loop(self):
        # FIX 1 — Le corps de la boucle est dans un try/except : une exception
        #          SQLite (disque plein, lock…) ne tue plus la tâche silencieusement.
        while not self.stop_event.is_set():
            try:
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

                self.purge_stale_memory()  # FIX 2

            except Exception:
                log.exception("Error in disappearance_loop — continuing")

            await asyncio.sleep(self.flush_interval)

    async def watchdog_loop(self):
        # FIX 3 — Détecte le gel du scanner (plus aucun callback reçu depuis
        #          watchdog_timeout secondes) et demande un restart.
        await asyncio.sleep(self.watchdog_timeout)  # grâce initiale au démarrage

        while not self.stop_event.is_set():
            silence = self.now() - self.last_callback_ts

            if silence >= self.watchdog_timeout:
                log.warning(
                    "No BLE callback for %.0fs — requesting scanner restart", silence
                )
                self._need_restart.set()
                # Grâce après restart : on laisse le temps au scanner de revenir
                await asyncio.sleep(self.watchdog_timeout)
            else:
                await asyncio.sleep(self.watchdog_timeout - silence)

    async def _try_recover_adapter(self) -> bool:
        """Tente de remonter l'adaptateur via hciconfig reset puis up.
        Retourne True si l'adaptateur répond à nouveau, False sinon."""
        log.warning("Adapter %s disappeared — attempting soft reset", self.adapter)

        # 'reset' fait down+up en une commande ; on ignore son code retour
        # car l'adaptateur peut déjà être DOWN au niveau kernel (ce qui est normal).
        await self._run_hci_cmd(["hciconfig", self.adapter, "reset"], ignore_failure=True)

        # 'up' est la commande critique : si elle échoue, l'adaptateur est mort
        ok = await self._run_hci_cmd(["hciconfig", self.adapter, "up"], ignore_failure=False)

        if not ok:
            log.error("hciconfig %s up failed — adapter unrecoverable this round", self.adapter)
            return False

        # Laisse BlueZ le temps de ré-enregistrer l'adaptateur sur D-Bus
        await asyncio.sleep(3)
        log.info("Soft reset done, resuming scan")
        return True

    async def _run_hci_cmd(self, cmd: list, ignore_failure: bool) -> bool:
        """Exécute une commande hciconfig. Retourne True si succès (ou si ignore_failure)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode != 0:
                msg = stderr.decode().strip()
                if ignore_failure:
                    log.debug("hciconfig %s returned %d (ignored): %s", " ".join(cmd[1:]), proc.returncode, msg)
                else:
                    log.error("hciconfig %s returned %d: %s", " ".join(cmd[1:]), proc.returncode, msg)
                return ignore_failure

            return True

        except Exception:
            log.exception("Failed to run %s", " ".join(cmd))
            return ignore_failure

    async def _run_scanner_once(self, scanning_mode: str):
        """Lance le scanner et attend soit l'arrêt, soit un signal de restart.
        Gère spécifiquement la disparition de l'adaptateur."""
        self._need_restart.clear()

        scanner = BleakScanner(
            detection_callback=self.on_detected,
            scanning_mode=scanning_mode,
            bluez={"adapter": self.adapter},
        )

        try:
            async with scanner:
                log.info("Scanner running on %s", self.adapter)
                self._adapter_fail_count = 0  # reset dès que le scan démarre

                stop_task = asyncio.create_task(self.stop_event.wait())
                restart_task = asyncio.create_task(self._need_restart.wait())

                _, pending = await asyncio.wait(
                    [stop_task, restart_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        except BleakError as e:
            if "not found" in str(e):
                # L'adaptateur a disparu de BlueZ — tenter une recovery
                self._adapter_fail_count += 1
                log.error(
                    "Adapter %s not found (attempt %d/%d)",
                    self.adapter, self._adapter_fail_count, MAX_ADAPTER_FAILURES,
                )
                if self._adapter_fail_count < MAX_ADAPTER_FAILURES:
                    await self._try_recover_adapter()
                # Si on atteint MAX_ADAPTER_FAILURES, run() va sortir de la boucle
            else:
                log.exception("BleakError on %s", self.adapter)

        except Exception:
            log.exception("Scanner error on %s", self.adapter)

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def run(self):
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop_event.set)

        scanning_mode = "passive" if self.passive else "active"

        log.info("BLE daemon starting — adapter=%s mode=%s", self.adapter, scanning_mode)
        log.info("Database: %s", self.db_path)
        log.info(
            "lost_after=%.0fs  flush_interval=%.0fs  watchdog_timeout=%.0fs",
            self.lost_after, self.flush_interval, self.watchdog_timeout,
        )

        # FIX 1 — Les tâches background sont créées une seule fois et supervisées
        disappearance_task = asyncio.create_task(self.disappearance_loop())
        watchdog_task = asyncio.create_task(self.watchdog_loop())

        # Boucle de restart du scanner
        while not self.stop_event.is_set():
            await self._run_scanner_once(scanning_mode)

            if self.stop_event.is_set():
                break

            # Trop d'échecs consécutifs "adapter not found" : hciconfig n'a pas
            # suffi. On quitte proprement et on laisse systemd relancer tout le
            # process (ce qui rejoue ExecStartPre=hciconfig hci0 up et recharge
            # entièrement le stack BlueZ).
            if self._adapter_fail_count >= MAX_ADAPTER_FAILURES:
                log.error(
                    "Adapter unrecoverable after %d attempts — exiting for systemd restart",
                    self._adapter_fail_count,
                )
                break

            log.info("Restarting scanner in 3s...")
            await asyncio.sleep(3)

        # ── Nettoyage ─────────────────────────────────────────────────────
        for task in (disappearance_task, watchdog_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self.dirty:
            self.flush_devices()

        self.conn.close()
        log.info("Stopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BLE Presence Scanner")

    parser.add_argument("--db", default="ble_presence.db")
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--lost-after", type=float, default=30.0)
    parser.add_argument("--flush-interval", type=float, default=5.0)
    parser.add_argument("--passive", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--watchdog-timeout",
        type=float,
        default=60.0,
        help="Restart scanner if no BLE packet received for this many seconds (default: 60)",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    daemon = BLEPresenceDaemon(
        db_path=Path(args.db),
        adapter=args.adapter,
        lost_after=args.lost_after,
        flush_interval=args.flush_interval,
        passive=args.passive,
        debug=args.debug,
        watchdog_timeout=args.watchdog_timeout,
    )

    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
