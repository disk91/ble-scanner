#!/usr/bin/env python3

import argparse
import json
import sqlite3
import time
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table


console = Console()


def fmt_ts(ts):
    if ts is None:
        return "-"

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def connect(db):
    return sqlite3.connect(db)


def short_json(value, max_len=40):
    if not value:
        return "-"

    try:
        parsed = json.loads(value)
        text = json.dumps(parsed, ensure_ascii=False)
    except Exception:
        text = str(value)

    if len(text) > max_len:
        return text[: max_len - 3] + "..."

    return text


def build_live_table(conn, limit, details):
    if details:
        rows = conn.execute(
            """
            SELECT
                address,
                COALESCE(name, '-'),
                last_seen,
                last_rssi,
                session_max_rssi,
                manufacturer_data,
                service_uuids

            FROM devices

            WHERE is_present = 1

            ORDER BY last_seen DESC

            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                address,
                COALESCE(name, '-'),
                last_seen,
                last_rssi,
                session_max_rssi

            FROM devices

            WHERE is_present = 1

            ORDER BY last_seen DESC

            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    table = Table(title="BLE devices currently present")

    table.add_column("Address")
    table.add_column("Name")
    table.add_column("Last Seen")
    table.add_column("RSSI")
    table.add_column("Session Max RSSI")

    if details:
        table.add_column("Manufacturer Data")
        table.add_column("Service UUIDs")

    for row in rows:
        if details:
            address, name, last_seen, rssi, max_rssi, manufacturer_data, service_uuids = row

            table.add_row(
                address,
                name,
                fmt_ts(last_seen),
                str(rssi if rssi is not None else "-"),
                str(max_rssi if max_rssi is not None else "-"),
                short_json(manufacturer_data),
                short_json(service_uuids),
            )
        else:
            address, name, last_seen, rssi, max_rssi = row

            table.add_row(
                address,
                name,
                fmt_ts(last_seen),
                str(rssi if rssi is not None else "-"),
                str(max_rssi if max_rssi is not None else "-"),
            )

    return table


def cmd_live(args):
    conn = connect(args.db)

    with Live(
        build_live_table(conn, args.limit, args.details),
        refresh_per_second=1,
    ) as live:
        while True:
            live.update(build_live_table(conn, args.limit, args.details))
            time.sleep(args.refresh)


def cmd_devices(args):
    conn = connect(args.db)

    rows = conn.execute(
        """
        SELECT
            address,
            COALESCE(name, '-'),
            first_seen,
            last_seen,
            last_rssi,
            session_max_rssi,
            is_present,
            manufacturer_data,
            service_uuids

        FROM devices

        ORDER BY last_seen DESC

        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    table = Table(title="Known BLE devices")

    table.add_column("Address")
    table.add_column("Name")
    table.add_column("First Seen")
    table.add_column("Last Seen")
    table.add_column("RSSI")
    table.add_column("Session Max")
    table.add_column("Present")

    if args.details:
        table.add_column("Manufacturer Data")
        table.add_column("Service UUIDs")

    for (
        address,
        name,
        first_seen,
        last_seen,
        rssi,
        max_rssi,
        is_present,
        manufacturer_data,
        service_uuids,
    ) in rows:
        values = [
            address,
            name,
            fmt_ts(first_seen),
            fmt_ts(last_seen),
            str(rssi if rssi is not None else "-"),
            str(max_rssi if max_rssi is not None else "-"),
            "YES" if is_present else "NO",
        ]

        if args.details:
            values.extend(
                [
                    short_json(manufacturer_data),
                    short_json(service_uuids),
                ]
            )

        table.add_row(*values)

    console.print(table)


def cmd_events(args):
    conn = connect(args.db)

    rows = conn.execute(
        """
        SELECT
            ts,
            event_type,
            address,
            COALESCE(name, '-'),
            rssi,
            manufacturer_data,
            service_uuids

        FROM presence_events

        ORDER BY ts DESC

        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    table = Table(title="BLE events")

    table.add_column("Timestamp")
    table.add_column("Event")
    table.add_column("Address")
    table.add_column("Name")
    table.add_column("RSSI")

    if args.details:
        table.add_column("Manufacturer Data")
        table.add_column("Service UUIDs")

    for ts, event_type, address, name, rssi, manufacturer_data, service_uuids in rows:
        values = [
            fmt_ts(ts),
            event_type,
            address,
            name,
            str(rssi if rssi is not None else "-"),
        ]

        if args.details:
            values.extend(
                [
                    short_json(manufacturer_data),
                    short_json(service_uuids),
                ]
            )

        table.add_row(*values)

    console.print(table)


def cmd_history(args):
    conn = connect(args.db)

    rows = conn.execute(
        """
        SELECT
            ts,
            event_type,
            COALESCE(name, '-'),
            rssi,
            manufacturer_data,
            service_uuids

        FROM presence_events

        WHERE address = ?

        ORDER BY ts DESC

        LIMIT ?
        """,
        (args.address, args.limit),
    ).fetchall()

    table = Table(title=f"History for {args.address}")

    table.add_column("Timestamp")
    table.add_column("Event")
    table.add_column("Name")
    table.add_column("RSSI")

    if args.details:
        table.add_column("Manufacturer Data")
        table.add_column("Service UUIDs")

    for ts, event_type, name, rssi, manufacturer_data, service_uuids in rows:
        values = [
            fmt_ts(ts),
            event_type,
            name,
            str(rssi if rssi is not None else "-"),
        ]

        if args.details:
            values.extend(
                [
                    short_json(manufacturer_data),
                    short_json(service_uuids),
                ]
            )

        table.add_row(*values)

    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="BLE Presence CLI")

    parser.add_argument("--db", default="ble_presence.db")

    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("live")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--refresh", type=float, default=1.0)
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("devices")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("events")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("history")
    p.add_argument("address")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_history)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
