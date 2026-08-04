#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily backup of Quant runtime data to /mnt/rclone/quant and /root/quant/backup.
Invoked by quant.sh or at log-rotate time.
Usage: python3 backup.py [backup|restore]
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BACKUP_DSTS = [Path("/root/quant/backup"), Path("/mnt/rclone/quant")]

BACKUP_FILES = {
    "quant.yaml": "quant.yaml",
    "quant_monitor_state.json": "quant_monitor_state.json",
}

BACKUP_DIRS = {
    "data/state_backups": "state_backups",
}


def backup_data():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_ok = True

    for dst_root in BACKUP_DSTS:
        dst_root.mkdir(parents=True, exist_ok=True)
        results = []

        for src_rel, dst_name in BACKUP_FILES.items():
            src = BASE_DIR / src_rel
            if not src.exists():
                results.append(f"skip: {dst_name} (not found)")
                continue
            try:
                shutil.copy2(src, dst_root / dst_name)
                results.append(f"ok: {dst_name}")
            except Exception as e:
                results.append(f"fail: {dst_name} ({e})")
                all_ok = False

        for src_rel, dst_name in BACKUP_DIRS.items():
            src = BASE_DIR / src_rel
            if not src.exists():
                results.append(f"skip: {dst_name}/ (not found)")
                continue
            dst = dst_root / dst_name
            try:
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                results.append(f"ok: {dst_name}/")
            except Exception as e:
                results.append(f"fail: {dst_name}/ ({e})")
                all_ok = False

        meta = {
            "backup_time": now,
            "source": str(BASE_DIR),
            "results": results,
        }
        try:
            (dst_root / "backup_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        print(f"[{now}] backup -> {dst_root}: " + ", ".join(results))

    return all_ok


def restore_data():
    src = Path("/root/quant/backup")
    if not src.exists():
        print(f"error: backup source {src} not found")
        return False
    meta_file = src / "backup_meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            print(f"restore from: {meta.get('backup_time', 'unknown')}")
        except Exception:
            pass

    results = []

    for src_rel, dst_name in BACKUP_FILES.items():
        src_file = src / dst_name
        if not src_file.exists():
            results.append(f"skip: {dst_name} (not found)")
            continue
        try:
            shutil.copy2(src_file, BASE_DIR / src_rel)
            results.append(f"ok: {dst_name}")
        except Exception as e:
            results.append(f"fail: {dst_name} ({e})")

    for src_rel, dst_name in BACKUP_DIRS.items():
        src_dir = src / dst_name
        if not src_dir.exists():
            results.append(f"skip: {dst_name}/ (not found)")
            continue
        dst_dir = BASE_DIR / src_rel
        try:
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir)
            results.append(f"ok: {dst_name}/")
        except Exception as e:
            results.append(f"fail: {dst_name}/ ({e})")

    print("restore done: " + ", ".join(results))
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backup"
    if cmd == "restore":
        restore_data()
    else:
        backup_data()