import os
import secrets
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

# ── Load .env ────────────────────────────────────────────────────────
env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
db_url = env["DATABASE_URL"]

from tansat import auth, db

db.init_db(db_url)

U = "tmp_gs_symbol_check"
P = "TmpGs-" + secrets.token_urlsafe(8)
old = db.get_user_by_username(U)
if old:
    db.delete_user(old["id"])
db.create_user(U, None, auth.hash_password(P), "admin", must_change_password=False)

# ── Selenium (headless Chrome) ───────────────────────────────────────
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

opts = Options()
opts.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
opts.add_argument("--headless=new")
opts.add_argument("--window-size=1600,1000")
opts.add_argument("--disable-gpu")
opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
driver = webdriver.Chrome(options=opts)

BASE = "http://localhost:5000"
SHOT = "temp_shots"
os.makedirs(SHOT, exist_ok=True)
DISH = "M3 15a9 9 0 0118 0"  # brand dish bowl path


def console_log():
    return [(e["level"], e["message"][:170]) for e in driver.get_log("browser")]


def login():
    driver.get(BASE + "/login")
    driver.find_element(By.ID, "username").send_keys(U)
    driver.find_element(By.ID, "password").send_keys(P)
    driver.find_element(
        By.CSS_SELECTOR, "form[action='/login'] button[type='submit']"
    ).click()
    time.sleep(1.2)


def js(script):
    return driver.execute_script(script)


HAS_DISH = "return el ? el.innerHTML.includes('%s') : false;" % DISH

try:
    login()
    print("LOGIN OK ->", driver.current_url)

    # ── 1. Satellite Control dashboard ───────────────────────────────
    driver.get(BASE + "/")
    time.sleep(2.5)
    print("[dashboard] map marker has dish svg:",
          js("const el = document.querySelector('#ground-track-map .gs-marker');" + HAS_DISH))
    print("[dashboard] legend has dish svg:",
          js("const l = document.querySelector('.panel-title-row');"
             "return l ? l.innerHTML.includes('%s') : false;" % DISH))
    driver.save_screenshot(SHOT + "/dashboard.png")
    print("[dashboard] console:", console_log())

    # ── 2. GIS Map View ──────────────────────────────────────────────
    driver.get(BASE + "/map")
    time.sleep(3.0)
    print("[map] marker has dish svg:",
          js("const el = document.querySelector('#map .gs-marker');" + HAS_DISH))
    print("[map] marker is divIcon marker (not circle):",
          js("return document.querySelectorAll('#map path.leaflet-interactive').length;"))
    print("[map] legend has dish svg:",
          js("const l = document.querySelector('.map-legend');"
             "return l ? l.innerHTML.includes('%s') : false;" % DISH))
    driver.save_screenshot(SHOT + "/map.png")
    print("[map] console:", console_log())

    # ── 3. Earth Orbit 3D view ───────────────────────────────────────
    driver.get(BASE + "/earth-orbit")
    time.sleep(6.0)
    print("[earth] helper + THREE sanity:",
          js("if (typeof TansatGS === 'undefined') return 'TansatGS MISSING';"
             "if (typeof THREE === 'undefined') return 'THREE MISSING';"
             "const s = TansatGS.sprite({});"
             "return s.isSprite ? 'sprite-ok' : 'sprite-FAIL';"))
    driver.save_screenshot(SHOT + "/earth.png")
    print("[earth] console:", console_log())

finally:
    driver.quit()
    old = db.get_user_by_username(U)
    if old:
        db.delete_user(old["id"])
    print("temp user deleted")
