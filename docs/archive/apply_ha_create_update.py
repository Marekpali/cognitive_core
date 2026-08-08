from pathlib import Path
import hashlib
import shutil
import sys

target = Path(r"C:\Users\marek\Documents\cognitive_core\src\adapters\ha.py")
backup = target.with_name("ha_pre_create_update_backup.py")

old = 'if action == "create":'
new = 'if action in {"create", "update"}:'

if not target.exists():
    print(f"ERROR: target not found: {target}")
    sys.exit(1)

data = target.read_text(encoding="utf-8")
count = data.count(old)

if count != 1:
    print(f"ERROR: expected exactly 1 occurrence of {old!r}, found {count}.")
    print("No changes made.")
    sys.exit(2)

before_hash = hashlib.sha256(target.read_bytes()).hexdigest()

shutil.copy2(target, backup)

updated = data.replace(old, new, 1)
target.write_text(updated, encoding="utf-8", newline="")

after = target.read_text(encoding="utf-8")

if new not in after:
    print("ERROR: replacement verification failed.")
    shutil.copy2(backup, target)
    print("Original restored from backup.")
    sys.exit(3)

if old in after:
    print("ERROR: old filter still present after replacement.")
    shutil.copy2(backup, target)
    print("Original restored from backup.")
    sys.exit(4)

after_hash = hashlib.sha256(target.read_bytes()).hexdigest()

print("OK: filter updated.")
print(f"Backup: {backup}")
print(f"Before SHA256: {before_hash}")
print(f"After  SHA256: {after_hash}")
print(f'Confirmed: {new}')
