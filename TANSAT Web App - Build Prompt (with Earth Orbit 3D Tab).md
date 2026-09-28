# Build prompt: TANSAT-style CubeSat ground station web app (with 3D Earth-orbit view)

Paste everything below as one message to an AI coding assistant. It is self-contained:
mission context, exact wire protocol, exact formulas, design system, architecture, every
page including the new Earth/orbit 3D tab, and a build order. Don't skip sections —
each one is load-bearing for the next.

---

## 0. Role and ground rules

You are a senior full-stack engineer building a multi-operator satellite ground station
web application from scratch. Follow this spec exactly — constants, formulas and field
names are not suggestions. Where the spec is ambiguous, ask rather than guess. Build in
phases (see §11) and don't move to the next phase until the current one's acceptance
checks pass.

Non-negotiable constraints:
- The telemetry parser and all physics/math live in a package with **no web-framework
  import at all** (no Flask/Express import inside it), so it's unit-testable and
  reusable outside the web server.
- Authorization is enforced **server-side** on every protected route. Hiding a
  navigation link is not access control.
- The radio/telemetry source is a **single shared resource** — one acquisition process
  per server, arbitrated by a control lock, fanned out to every connected browser.
- Every number below (thresholds, colors, constants) is exact. Use them as given.

---

## 1. Mission context

This is a ground station for a CubeSat-class satellite in low Earth orbit (LEO,
~400–550 km altitude). The spacecraft's onboard computer streams telemetry over a radio
link to a ground modem, which presents it to a host PC as a serial (or network) stream.
The web app is the ground-station software: receiving, decoding, displaying, logging,
and — for authorized operators — commanding the spacecraft.

Glossary: **AOS/LOS** = acquisition/loss of signal (satellite rises/sets over the
horizon); **azimuth** = compass bearing to the satellite (0–360°, 0 = north);
**elevation** = look-angle above horizon (−90..+90°, negative = below horizon);
**slant range** = straight-line 3D distance to the satellite; **range rate** = rate of
change of slant range (km/s, negative = approaching); **Doppler shift** = frequency
offset from range rate; **pass** = one horizon-to-horizon transit, typically 5–12
minutes in LEO; **ground station position** = the surveyed lat/lon/altitude of your
antenna — all pointing geometry is computed relative to it.

---

## 2. Wire protocol — implement exactly

The modem emits newline-delimited text. Parse with this **strict dispatch order**:

1. Strip trailing whitespace/CRLF. Empty → discard.
2. Strip a trailing link-quality tag if present (e.g. `RSSI:-82dBm SNR:9.1dB` or
   `[RSSI=-82 SNR=9.1]`) before anything else, so it can never corrupt a field count.
   Attach the extracted numbers to whatever packet the remainder parses as. Purely
   additive — a line without the tag is unaffected.
3. If the remaining line starts with `{`, try JSON; dispatch on its `type` field.
4. If it's untagged (no HK/ORB/IMG/BEACON prefix) and, split on commas, has **exactly
   14 fields**, parse as live TLM (below).
5. If untagged and NOT exactly 14 numeric-looking fields → **discard silently**. Do not
   pad or truncate. A malformed line is worse than no line.
6. Otherwise parse as tagged CSV (HK/ORB/IMG/BEACON).

### 2.1 Live TLM — 14 bare comma-separated fields (the primary format)

| # | Field | Unit | Notes |
|---|---|---|---|
| 0 | temperature | °C | internal/onboard temperature |
| 1 | pressure_pa | Pa | absolute pressure |
| 2 | altitude_m | m | **relative** barometric offset from the device's own power-on baseline — NOT altitude above sea level |
| 3–5 | accel_x, accel_y, accel_z | g | body-frame acceleration |
| 6–8 | gyro_x, gyro_y, gyro_z | °/s | body-frame angular rates |
| 9 | latitude | deg | **0.0 means NO GPS FIX** |
| 10 | longitude | deg | **0.0 means NO GPS FIX** |
| 11 | humidity | % | |
| 12 | bus_voltage | V | battery/bus voltage |
| 13 | seq | int | monotonic counter — gaps = lost packets |

**GPS-fix rule:** if `latitude == 0.0` OR `longitude == 0.0` → no fix. Set
`gps_lock=False`, and set azimuth/elevation/range/slant/horizontal all to `0.0`
(don't compute geometry against a null-island fix). The **frontend**, not the wire
value, is responsible for showing "NO FIX" — never trust a bare `0.0` for these fields
without checking `gps_lock` first, or you'll render a false "the satellite is right
here" reading.

**Packet loss:** track previous `seq` for TLM packets. `gap = seq − prev_seq − 1`. If
`0 < gap < 10000`, add to a lost-packet counter (upper bound guards against a firmware
counter reset producing millions of false losses).

**True device altitude (MSL)** = `GS_ALT + altitude_m` — computed in the display layer,
not the parser (keep domain logic free of ground-station-specific config).

### 2.2 Tagged families (CSV or JSON, both must be accepted)

- `HK` (housekeeping): satellite_id, its own timestamp, battery_voltage (V),
  internal_temperature (°C)
- `ORB` (orbit/track, from a separate propagator feed if you have one):
  next_aos, azimuth_at_aos, next_los, max_elevation, latitude, longitude,
  altitude(km), range(km), range_rate(km/s), doppler_100mhz(Hz), azimuth, elevation
- `IMG` (camera payload, chunked): satellite_id, chunk_seq, total_chunks, base64 JPEG
- `BEACON`: treat identically to HK

Accept common key aliases case-insensitively (`range`→`range_km`, `b64`→`image_b64`,
`sat`/`id`→`satellite_id`).

**Image reassembly:** buffer chunks by sequence; when 1..total are all present,
concatenate in order, base64-decode, write file, reset buffer. Cap assembled size at
2,000,000 bytes and abandon if `total` implies more — a corrupt field must not exhaust
memory.

**Uplink command framing:** plain text + CRLF. Optional NMEA-style checksum: XOR every
character of the command body, append `*` + 2-digit uppercase hex.
`"PING"` → `PING*4D\r\n`. Lines starting with `ACK/OK/ERR/CMD_ACK/TX_OK/UPLINK_ACK` (or
containing "ACK" and under 120 chars) are command acknowledgements — route to a command
console, never parse as telemetry.

---

## 3. Physics/algorithms — exact formulas

Put these in a standalone, framework-free module.

### 3.1 Pointing geometry (azimuth, elevation, slant range)

```python
EARTH_RADIUS_M = 6_371_000.0

def calculate_azimuth_and_range(gs_lat, gs_lon, gs_alt, dev_lat, dev_lon, dev_alt):
    lat1, lat2 = radians(gs_lat), radians(dev_lat)
    dlat, dlon = radians(dev_lat - gs_lat), radians(dev_lon - gs_lon)

    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    az = (degrees(atan2(y, x)) + 360.0) % 360.0

    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    a = max(0.0, min(1.0, a))                       # clamp FP overshoot
    horizontal = 2 * EARTH_RADIUS_M * atan2(sqrt(a), sqrt(1 - a))

    d_alt = dev_alt - gs_alt
    slant = sqrt(horizontal**2 + d_alt**2)
    elevation = (degrees(atan2(d_alt, horizontal)) if horizontal > 1e-6
                 else (90.0 if d_alt > 0 else (-90.0 if d_alt < 0 else 0.0)))
    return az, horizontal, slant, elevation
```

**Important, non-obvious fact:** `gs_alt` cancels out of `d_alt` if you compute
`dev_alt` as `gs_alt + altitude_offset` — meaning ground-station altitude has **zero
effect** on range/azimuth/elevation. It only affects a displayed absolute-altitude
value. Don't let ground-station altitude changes appear to "fix" a range reading —
they mathematically cannot.

### 3.2 Attitude from accelerometer

```
pitch = degrees(atan2(ax, sqrt(ay**2 + az**2)))
roll  = degrees(atan2(ay, sqrt(ax**2 + az**2)))
```

### 3.3 Gyro dead-band

Clamp any gyro axis with `|rate| < 0.05 °/s` to exactly zero (treat missing as zero).
Without this, a stationary device's attitude visibly drifts from integrated noise.

### 3.4 Complementary attitude filter (α = 0.98)

```
pitch_acc, roll_acc = accel_orientation(ax, ay, az)
if first_update:
    pitch, roll = pitch_acc, roll_acc     # SEED from accel, don't start at 0
    return pitch, roll, yaw
pitch = α * (pitch + gx * dt) + (1-α) * pitch_acc
roll  = α * (roll  + gy * dt) + (1-α) * roll_acc
yaw   = (yaw + gz * dt) % 360.0; if yaw > 180: yaw -= 360   # no accel anchor for yaw
```

Seeding matters: without it, the display visibly "crawls" from 0° to the true angle at
every connection. Clamp `dt` to `[0.005, 0.5]` seconds (bursts/dropouts otherwise cause
huge bogus rotations).

### 3.5 GPS jitter filter (dead-band 2.0 m + EMA α = 0.2)

- No fix (lat or lon is 0, or missing) → return previous fix, mark invalid, don't
  update state.
- First valid fix → pass through, seed the filter.
- New fix within 2.0 m (haversine) of previous filtered fix → hold previous (stationary
  suppression).
- Otherwise → `filtered = 0.2*new + 0.8*previous`.

### 3.6 Range-rate and Doppler (derive if not directly provided)

Most raw hardware only ever sends bare TLM — range_rate/doppler need to be derived
server-side from consecutive slant-range samples, not left blank:

```
dt = now - prev_sample_time
if 0 < dt <= 5.0:
    raw_rate = (slant_range_m - prev_slant_range_m) / dt
    rate_ema = 0.3*raw_rate + 0.7*rate_ema   # smooth serial-timing jitter
    range_rate_km_s = rate_ema / 1000
    doppler_100mhz  = -(rate_ema * 100e6 / 299_792_458.0)   # receding => negative shift
```
Only fill these if not already supplied by a genuine ORB packet. Reset the derivative
state whenever GPS fix is lost, so a re-acquired fix doesn't compute a rate across the
outage.

### 3.7 Battery percentage (non-linear Li-ion curve)

Piecewise-interpolate through: `3.30V→0%, 3.65V→15%, 3.80V→40%, 3.90V→62%, 4.05V→82%,
4.20V→100%`. If hardware sends an explicit percentage, prefer it. If "voltage" > 5.0,
assume it's actually a percentage. Clamp 0–100.

---

## 4. Design system

Aerospace operations-console aesthetic — dense, dark, functional, read at a glance,
not decorative.

**Dark palette (default):** bg `#050508`, card `#0f1015`, card-hover `#161822`,
border `#2a2d3d`, border-light `#363a52`, text `#e8e9ef`, text-secondary `#8b8fa8`,
text-muted `#5a5e7a`, accent `#4f7cff`, accent-hover `#6b92ff`, success `#00e6a0`,
warning `#ffb020`, danger `#ff4d6a`.

**Light palette (toggle, chrome only):** bg `#f3f5fb`, card `#ffffff`,
border `#d7dbea`, text `#171923`, text-secondary `#4a4f66`, accent `#3862e0`,
success `#0a9e6e`, warning `#b5750a`, danger `#d6304a`. **Instrument surfaces (plots,
3D views, map, the new Earth-orbit view) stay dark in both themes** — series colors are
tuned for a dark backdrop; this is the Grafana/ops-console convention, not an oversight.

**Series colors** (one metric = one color everywhere it appears):
battery `#00e6a0`, temperature `#ff4d6a`, altitude `#4f7cff`, range `#a855f7`,
range_rate `#ffb020`, doppler `#00d4ff`, azimuth `#ec4899`, elevation `#ffd23f`.

**Typography:** UI text Inter/Segoe UI, 13px body, 10–11px labels. **All numeric
telemetry in monospace** (JetBrains Mono / Cascadia Mono) — proportional digits jitter
horizontally as values update, which is unreadable at high packet rates.

**Density:** 10px card radius, 7px control radius, 10px gaps, 12–14px card padding.
Tight — an operator wants many values visible simultaneously.

**Never show a bare zero for missing data** — use an em dash in muted color. A zero is
a measurement; a dash is an absence. Confusing them is dangerous during a live pass.

**Motion:** 120ms transitions; a live-link indicator pulses ~1.6s; nothing else
animates decoratively; honor `prefers-reduced-motion`.

---

## 5. Why this can't just be "one user's app with a login screen"

Four properties a single-operator tool can assume, that a shared ground station cannot:

1. **Identity** — a login MUST verify credentials server-side (hashed passwords,
   sessions), not just ask "who are you" and believe the answer.
2. **Concurrency of intent** — the radio is one physical resource. Two operators both
   pressing "Connect" must not both succeed. Solve with a **control lock**: everyone
   watches, exactly one person commands at a time. The lock auto-expires (~90s) without
   a heartbeat so a crashed browser can't hold the station hostage; an admin can force-
   seize it.
3. **Fan-out delivery** — one incoming telemetry stream must reach N browsers, not one
   window. Use Server-Sent Events (one-way, no extra dependency, browser
   auto-reconnects) with a bounded per-client queue (drop oldest on backpressure —
   never let a slow browser stall the radio) and a rolling history buffer so a late
   joiner can back-fill their charts instantly.
4. **Shared storage** — session logs, payload images, exports live on the server, not
   one operator's laptop, so the whole team can access them.

---

## 6. Roles and accounts

Three roles:

| Capability | Admin | Operator | Viewer |
|---|---|---|---|
| View all pages, download logs/exports | ✓ | ✓ | ✓ |
| Take control, connect, transmit uplink | ✓ | ✓ | ✗ |
| Force-seize control from another user | ✓ | ✗ | ✗ |
| Manage accounts, settings, OTA, audit log | ✓ | ✗ | ✗ |

Build a full **account-management screen** (admin only): create/edit/suspend/
reactivate/reset-password/delete operator accounts, with generated one-time temp
passwords, forced password change at first login, and these 8 guard rails enforced
server-side (not just UI-hidden):

1–3. The last active administrator can't be demoted / suspended / deleted.
4–5. You can't suspend or delete your own account.
6–7. Usernames unique (case-insensitive), 3–64 chars, letters/digits/`.`/`-`/`_` only.
8. Email addresses unique when present.

Keep an append-only **audit log** (who, what, when, source IP) for every login,
account change, control hand-over, acquisition start/stop, and uplink command —
retained even if the account is later deleted.

---

## 7. Pages (the navigation)

Standard pages, each a route rendering a shell that fills live via the SSE stream:

1. **Dashboard** — metric strip (temp/altitude/pressure/humidity), acceleration + gyro
   line charts, ground-track map, battery gauge, flight-status grid.
2. **Full Telemetry** — six plots (battery %, temperature, altitude, range, Doppler,
   azimuth+elevation), downlink console, RSSI/SNR/packet-count tiles.
3. **Analytics Logs** — browse/preview/download recorded CSV sessions.
4. **GIS Map View** — 2D ground track, ground-station marker, line-of-sight.
5. **Satellite Tracking** — polar az/el plot, pass predictions, live table, Excel export.
6. **3D Attitude View** — the spacecraft's own body orientation (roll/pitch/yaw), a
   close-up CubeSat model. **This is NOT the new tab below — keep them separate.**
7. **Earth Orbit View** — **THE NEW TAB. Full spec in §8.**
8. **Payload Feed** — downlinked camera images.
9. *(admin)* **System Settings** — ground-station lat/lon/alt, baud rate, and an
   altitude-calibration offset (see note below).
10. *(admin)* **Node Manager** — uplink console + auto-discovered node table.
11. *(admin)* **Firmware OTA** — firmware staging/upload.
12. *(admin)* **Operator Accounts**, **Audit Trail** — from §6.

**Altitude calibration note:** the raw barometric `altitude_m` has a sensor bias that
drifts with local air pressure and can easily be off by tens of meters until
calibrated — this is normal sensor behavior, not a bug. Build a "Zero altitude now"
control: while the device sits at a *known* height above the ground station, capture
the current raw reading as `offset`; thereafter
`displayed_altitude = GS_ALT + (altitude_m - offset)`. Keep this offset **separate**
from `GS_ALT` itself (which is a real geographic survey value shown elsewhere as a
label) — don't conflate a sensor calibration hack with the station's actual surveyed
position.

---

## 8. NEW TAB — full detailed spec: "Earth Orbit View"

This is the feature this prompt exists to add. Build a full navigation destination
(own route, own nav icon, e.g. a globe icon) showing a **planetary-scale 3D view**: a
rotating Earth with the satellite's real position plotted around it. This is distinct
from the CubeSat body-attitude view — here the camera is far out in space, Earth is the
subject, and the satellite is a small marker at its correct orbital position.

### 8.1 Rendering stack

Use Three.js (or an equivalent WebGL library already in your stack). Load it as a
pinned-version script if your environment restricts external module imports.

### 8.2 The Earth

- A sphere (radius arbitrary in scene units, e.g. 50) textured with a day-side Earth
  map (NASA "Blue Marble" or similar public-domain/CC imagery — do not use a
  copyrighted stock photo).
- Light the scene with a single directional light representing the sun, positioned so
  day/night naturally falls out of standard shading — you do not need a literal
  day/night texture blend unless you want the extra polish; a diffuse material + one
  directional light already produces a correct terminator.
- Optional polish: a faint atmosphere glow (a slightly larger, additive-blended,
  translucent sphere) and a starfield skybox for context — don't spend disproportionate
  effort here; the physically-correct satellite position matters far more than visual
  flourish.
- **Earth's rotation**: real sidereal rotation is ~23h56m per revolution — at real-time
  speed it will look nearly static during a normal viewing session. Provide a
  **rotation-speed control** (e.g. "Real-time" vs a visibly fast demo speed like
  "1 rev / 30s") so the rotation is actually visible when wanted, but real-time is the
  scientifically honest default. Label whichever speed is active clearly — never let
  it silently run at a fake speed without saying so.

### 8.3 The coordinate transform — implement exactly

You must convert the satellite's `(latitude, longitude, true_altitude_msl)` — using
the **already GPS-filtered** lat/lon and the corrected MSL altitude (§2.1/§7 note), not
raw jittery values — into Earth-Centered-Earth-Fixed (ECEF) Cartesian coordinates, then
into your Three.js scene:

```
lat_rad = radians(latitude)
lon_rad = radians(longitude)
r = EARTH_RADIUS_SCENE_UNITS + (true_altitude_km / EARTH_RADIUS_KM) * EARTH_RADIUS_SCENE_UNITS

x = r * cos(lat_rad) * cos(lon_rad)
y = r * sin(lat_rad)                      # Three.js Y-up convention
z = r * cos(lat_rad) * sin(lon_rad)
```

Apply this position **relative to the Earth mesh's own current rotation** (either
counter-rotate the satellite's longitude by the Earth's current rotation angle, or —
simpler and less error-prone — make the satellite a **child object of the Earth mesh**
in the scene graph, so it rotates rigidly with the surface automatically and you never
have to manually synchronize the two).

**Scale honesty, deliberately broken in one specific way:** compute the satellite's
position at the physically correct radius (Earth radius + real altitude — for a
~450km-altitude CubeSat this is only about 7% of Earth's radius above the surface, so
it sits visibly close to the surface, not far off in space like a geostationary
satellite). But render the satellite **marker/icon itself** a few scene-units in size
regardless of true physical scale — a literal 10cm CubeSat at accurate scale would be
sub-pixel and invisible. State this exaggeration explicitly in code comments: position
must be physically accurate, marker *size* is a visibility exaggeration only.

### 8.4 What to draw

- **Satellite marker** at the transformed position — a small glowing sphere or sprite,
  in the `azimuth` series color (`#ec4899`) or a distinct bright marker color.
- **Orbital trail**: keep the last N (e.g. 200) filtered positions and draw them as a
  fading line/tube trailing the satellite — like a comet tail — so orbital motion
  direction is visible at a glance, not just the current point.
- **Ground track** (optional but recommended): project the same trail onto the Earth's
  surface (radius = Earth radius, ignoring altitude) as a second, dimmer line — this is
  the classic "ground track" satellite-tracking visualization.
- **Ground-station marker**: a fixed point at your surveyed `GS_LAT`/`GS_LON` on the
  Earth's surface (in the warning color `#ffb020`, matching its use elsewhere for the
  ground-station marker on the 2D map, for visual consistency across pages).
- **Line-of-sight indicator**: when `gps_lock` is true and `elevation > 0` (satellite
  above the local horizon), draw a line from the ground-station marker to the satellite
  marker in the success color; when below the horizon or no fix, don't draw it (or draw
  it dimmed) — reuse the already-computed `elevation` value, don't recompute visibility
  independently.
- **No-fix handling**: if `gps_lock` is false, do not plot a marker at a fabricated
  position (no null-island placement) — show the Earth alone with a clear "no position
  fix" label, exactly as the 2D map and other geometry-dependent views must already do.

### 8.5 Camera and interaction

- Orbit controls: click-drag to rotate the view around Earth, scroll to zoom, matching
  standard 3D-viewer conventions.
- A "Follow satellite" toggle that smoothly moves the camera to track the satellite's
  position instead of a fixed wide view.
- A "Reset view" button returning to a default wide framing of the whole Earth.

### 8.6 Data + performance

- Subscribe to the same live packet stream every other page uses; update the
  satellite's target position on each packet with GPS lock, easing/interpolating
  toward it (don't snap) so motion between packets looks smooth rather than jumpy.
- Cap the render loop via `requestAnimationFrame`; dispose of Three.js geometries/
  materials/textures when the user navigates away from this page (this scene is
  heavier than the CubeSat attitude view — leaking it across page navigations will
  visibly degrade performance over a session).
- Show small readouts alongside the 3D view: current lat/lon, true altitude,
  azimuth, elevation, range — reusing existing formatted/gated (gps_lock-aware) values,
  not recomputing them independently in the 3D page.

### 8.7 Acceptance criteria for this tab specifically

- With the simulator/demo data source running, the satellite marker visibly moves
  along a smooth, continuous arc around the Earth as packets arrive (not jumping
  discontinuously).
- Losing GPS fix removes/freezes the marker with a clear "no fix" indication rather
  than snapping it to (0,0) on the equator/prime-meridian.
- Earth's rotation speed control works and is honestly labeled.
- Ground-station marker appears at the correct surveyed location and stays fixed
  relative to the rotating Earth (i.e., it's also a child of the Earth mesh, not a
  separately-computed static scene position).
- Frame rate stays smooth (no visible stutter) with the trail at its full length.
- Navigating away and back doesn't leak memory or duplicate the WebGL context.

---

## 9. API shape

Server-rendered HTML pages for navigation; JSON/SSE for live data — this split lets the
same backend later serve a mobile client with no server changes.

- `GET /api/stream` — Server-Sent Events: `status`, `packet`, `raw`, `uplink`, `image`,
  `control`, `error` event types.
- `GET /api/status`, `GET /api/history?limit=` — snapshot + chart back-fill.
- `GET /api/ports` — serial ports visible to the **server** (not the operator's laptop
  — say this explicitly in the Connect dialog, it surprises everyone once).
- `POST /api/control/take|release|heartbeat` — the control lock.
- `POST /api/acquisition/start|stop`, `POST /api/uplink` — all require holding the lock.
- `GET /api/logs/<name>[.preview]`, `GET /api/export/orb.xlsx` — session data.
- Every JSON response: `{ok: true|false, message|error: "...", ...}`.

Three DB tables only: `users`, `audit_log`, `station_settings` (key/value — ground
station position, calibration offset, baud, control-lock TTL). **Telemetry itself does
NOT go in the database** — keep writing it to CSV session logs, exactly as a real
ground station's downlink recorder would.

---

## 10. Deployment notes

- Every connected browser holds one long-lived SSE connection open → the server **must**
  be multi-threaded, and must run as **one process** (the acquisition service and
  control lock are per-process singletons — multiple worker processes would each think
  they own the radio independently).
- Reverse-proxy HTTPS in front of it; disable proxy response buffering or SSE arrives
  in laggy bursts instead of smoothly.
- Real secret key from environment, never a hard-coded default; bootstrap admin account
  forced to change its password on first login.

---

## 11. Build order

1. **Domain core** (§2, §3) — parser + physics, zero framework dependency, unit tested
   against hand-built sample lines.
2. **Auth + roles + account management** (§6) — including all 8 guard rails.
3. **Acquisition service + control lock + SSE fan-out** (§5) — prove it with two
   simultaneous browser sessions before writing a single page.
4. **Core pages** (§7 items 1–6, 8–12).
5. **Earth Orbit View** (§8) — build last, since it depends on the filtered
   lat/lon/altitude pipeline from phases 1–3 already being correct.
6. **Hardening**: HTTPS, rate limiting, a real test suite, deployment config (§10).

At each phase, stop and verify against that phase's acceptance criteria before
continuing — do not let bugs from an early phase get masked by a later one (the classic
failure mode: a demo/simulator data source that "just works" while the real hardware
path silently never gets exercised at all).
