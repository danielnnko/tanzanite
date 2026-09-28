"""
reset_admin.py — Reset or create the admin password directly from the command line.

Usage:
    python reset_admin.py [new_password]

If new_password is not supplied, it will generate a random one and display it.
"""

import os
import sys

# Load .env if present
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ:
                    os.environ[k] = v

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    print("ERROR: DATABASE_URL not set. Please set it in .env or your environment.", file=sys.stderr)
    sys.exit(1)

from tansat import db, auth

db.init_db(db_url)

# Get or generate password
new_password = sys.argv[1] if len(sys.argv) > 1 else auth.generate_temp_password(12)
pw_hash = auth.hash_password(new_password)

admin_user = db.get_user_by_username("admin")
if admin_user:
    db.update_user(admin_user["id"], password_hash=pw_hash, is_active=True, must_change_password=False)
    print(f"\n[OK] Admin password successfully updated!")
else:
    db.create_user("admin", None, pw_hash, "admin", must_change_password=False)
    print(f"\n[OK] Admin account created!")

print("═" * 50)
print(f"  Username : admin")
print(f"  Password : {new_password}")
print(f"  URL      : http://localhost:5000/login")
print("═" * 50 + "\n")
