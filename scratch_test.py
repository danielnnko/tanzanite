import os
import sys

# Load .env
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'\"")

from app import create_app
from tansat import db, auth

app = create_app()
client = app.test_client()

admin = db.get_user_by_username("admin")
print("Admin in DB:", admin)

# Test GET /login
r_login = client.get("/login")
print("GET /login status:", r_login.status_code)

# Test POST login with correct password or test login flow
if admin:
    # Set known password
    db.update_user(admin["id"], password_hash=auth.hash_password("Pass1234!"), must_change_password=True)
    r_post = client.post("/login", data={"username": "admin", "password": "Pass1234!"}, follow_redirects=False)
    print("POST /login redirect location:", r_post.headers.get("Location"), "Status:", r_post.status_code)
    
    # Follow the redirect
    target = r_post.headers.get("Location")
    if target:
        r_target = client.get(target)
        print(f"GET {target} status:", r_target.status_code)
        if r_target.status_code == 500:
            print("ERROR on target page! Data preview:")
            print(r_target.data[:1000].decode("utf-8", errors="replace"))
