"""CLI tool to download the latest Plesk backup with progress output."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from plesk_backup_scheduler import PleskClient, PleskError

ESTIMATED_TOTAL = 5_740_131_779


def fmt_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:6.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def fmt_eta(sec: float) -> str:
    if sec < 0 or sec > 86400 * 7:
        return "--:--:--"
    sec = int(sec)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def draw_bar(done: int, total: int, t0: float, bar_w: int = 40) -> None:
    pct = (done / total * 100) if total else 0.0
    filled = int(bar_w * done / total) if total else 0
    bar = "#" * filled + "-" * (bar_w - filled)
    elapsed = time.time() - t0
    speed = done / elapsed if elapsed > 0 else 0
    eta = ((total - done) / speed) if speed > 0 and total else -1
    line = (
        f"\r[{bar}] {pct:6.2f}%  "
        f"{fmt_bytes(done)}/{fmt_bytes(total)}  "
        f"speed={fmt_bytes(speed)}/s  ETA={fmt_eta(eta)}"
    )
    sys.stdout.write(line.ljust(110))
    sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the latest backup available in a Plesk subscription."
    )
    parser.add_argument("--plesk-url", default=os.getenv("PLESK_URL", ""))
    parser.add_argument("--username", default=os.getenv("PLESK_USER", ""))
    parser.add_argument("--password", default=os.getenv("PLESK_PASSWORD", ""))
    parser.add_argument("--domain-id", type=int, default=int(os.getenv("PLESK_DOMAIN_ID", "1")))
    parser.add_argument("--domain-name", default=os.getenv("PLESK_DOMAIN_NAME", ""))
    parser.add_argument("--out-dir", default=os.getenv("PLESK_OUT_DIR", "./downloads"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = []
    if not args.plesk_url:
        missing.append("--plesk-url or PLESK_URL")
    if not args.username:
        missing.append("--username or PLESK_USER")
    if not args.password:
        missing.append("--password or PLESK_PASSWORD")
    if missing:
        print("[!] Missing required settings: " + ", ".join(missing))
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Connecting to Plesk: {args.plesk_url}")
    c = PleskClient(args.plesk_url, args.username, args.password, args.domain_id, args.domain_name)
    c.login()
    print("[+] Login successful.")

    print("[*] Loading backup list...")
    backups = c.list_backups()
    if not backups:
        print("[!] No backups found in this subscription.")
        return 2
    print(f"[+] {len(backups)} backup(s) found.")
    for b in backups:
        ts = b["timestamp"]
        when = f"20{ts[0:2]}-{ts[2:4]}-{ts[4:6]} {ts[6:8]}:{ts[8:10]}"
        print(f"     - ts={ts}  ({when})  dump_id={b['dump_id']}")

    latest = backups[0]
    ts = latest["timestamp"]
    when = f"20{ts[0:2]}-{ts[2:4]}-{ts[4:6]} {ts[6:8]}:{ts[8:10]}"
    dest = out_dir / f"plesk_backup_{ts}.zip"
    print(f"\n[*] Latest backup: {when}")
    print(f"[*] Output file  : {dest}")

    if dest.exists():
        size = dest.stat().st_size
        print(f"[i] Backup already exists ({fmt_bytes(size)}), skipping download.")
        return 0

    print("[*] Download started...\n")
    t0 = time.time()
    last_draw = [0.0]

    def cb(done, total):
        if total <= 0:
            total = max(ESTIMATED_TOTAL, done)
        now = time.time()
        if now - last_draw[0] >= 0.25 or done == total:
            draw_bar(done, total, t0)
            last_draw[0] = now

    try:
        c.download(latest["dump_id"], dest, progress_cb=cb)
    except PleskError as e:
        print(f"\n[!] Plesk error: {e}")
        return 3
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
        return 130

    elapsed = time.time() - t0
    size = dest.stat().st_size
    print("\n\n[+] COMPLETED")
    print(f"     File      : {dest}")
    print(f"     Size      : {fmt_bytes(size)} ({size:,} bytes)")
    print(f"     Duration  : {fmt_eta(elapsed)}")
    print(f"     Avg speed : {fmt_bytes(size / elapsed)}/s")

    # Verify ZIP signature.
    with open(dest, "rb") as f:
        head = f.read(4)
    print(f"     Signature : {head!r} -> {'OK (ZIP)' if head[:2] == b'PK' else 'INVALID'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
