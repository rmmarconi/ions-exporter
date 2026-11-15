#!/usr/bin/env python3
"""Prometheus exporter for Intelbras IONS-2K M1 microinverters (standard library only).

Reads each microinverter over Modbus TCP (port 502, unit id 1, no password) through
its built-in Wi-Fi. Register map, settings and metrics: see README.md.

Register map (function 4 = input registers, 0-based addresses, U32 = low word first):
  2504-2509 serial (ASCII) · 2515-2524 model (ASCII) · 5220 state (0 = producing)
  5227-5228 lifetime energy 0.1 kWh · 5231-5232 AC W · 5235-5236 DC W · 5237-5238 VA
  5240 power factor S16 x0.001 · 5241 Hz x0.01 · 5244 °C x0.1 · 5246 V x0.1
  5249 A x0.1 · 5332-5339 panels 1-4 (V x0.1 and A x0.1 interleaved)
  function 3, 29501-29502: auxiliary temperatures x0.1 °C

The inverter keeps neither daily energy nor per-panel energy, so the exporter computes
"today" as lifetime counter minus the first reading of the day, and per-panel energy by
integrating V x A. That state lives in STATE_FILE so it survives restarts.

License: GPL-3.0-or-later.
"""
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.0.0"

PORT = int(os.environ.get("PORT", "9596"))
SITE = os.environ.get("SITE", "ions")
TIMEOUT = float(os.environ.get("IONS_TIMEOUT", "5"))
POLL = float(os.environ.get("IONS_POLL", "30"))
MAX_AGE = float(os.environ.get("IONS_MAX_AGE", "300"))
STATE_FILE = os.environ.get("STATE_FILE", "ions-state.json")
RATED_W = int(os.environ.get("IONS_RATED_W", "2000"))
MAX_GAP = 300   # seconds; longer gaps (night, outage) are not integrated


def targets():
    out = []
    for item in os.environ.get("IONS_TARGETS", "").split(","):
        item = item.strip()
        if item:
            name, ip = item.split("=", 1) if "=" in item else (item, item)
            out.append((name.strip(), ip.strip()))
    return out


# ---------- Modbus TCP ----------

def read_regs(sock, fn, start, count, tid=1):
    sock.sendall(struct.pack(">HHHBBHH", tid, 0, 6, 1, fn, start, count))
    head = b""
    while len(head) < 9:
        chunk = sock.recv(9 - len(head))
        if not chunk:
            raise IOError("connection closed")
        head += chunk
    if head[7] & 0x80:
        raise IOError(f"Modbus exception {head[8]} (fn {fn}, {start})")
    n = head[8]
    body = b""
    while len(body) < n:
        chunk = sock.recv(n - len(body))
        if not chunk:
            raise IOError("short response")
        body += chunk
    return dict(zip(range(start, start + count), struct.unpack(f">{n // 2}H", body)))


def ascii_regs(regs, a, b):
    raw = "".join(chr(regs[r] >> 8) + chr(regs[r] & 0xFF) for r in range(a, b))
    return "".join(c for c in raw if 32 <= ord(c) < 127).strip()


def read_inverter(ip, need_info):
    regs4, regs3 = {}, {}
    with socket.create_connection((ip, 502), timeout=TIMEOUT) as s:
        s.settimeout(TIMEOUT)
        regs4.update(read_regs(s, 4, 5215, 45, 1))
        regs4.update(read_regs(s, 4, 5332, 8, 2))
        try:
            regs3.update(read_regs(s, 3, 29501, 2, 3))
        except IOError:
            pass
        if need_info:
            regs4.update(read_regs(s, 4, 2500, 37, 4))
    return regs4, regs3


def u32(r, lo):
    return r[lo] + (r[lo + 1] << 16)


def s16(v):
    return v - 65536 if v >= 32768 else v


def decode(r4, r3):
    pf = s16(r4[5240]) / 1000
    d = {
        "state": r4[5220],
        "total": u32(r4, 5227) / 10,
        "ac_w": u32(r4, 5231),
        "dc_w": u32(r4, 5235),
        "va": u32(r4, 5237),
        "pf": pf if -1 <= pf <= 1 else None,
        "grid_hz": r4[5241] / 100,
        "temp_c": r4[5244] / 10,
        "grid_v": r4[5246] / 10,
        "grid_a": r4[5249] / 10,
        "pv": {},
        "aux": {},
    }
    for i in range(4):
        v, a = r4[5332 + 2 * i] / 10, r4[5333 + 2 * i] / 10
        d["pv"][str(i + 1)] = {"voltage": v, "current": a, "power": round(v * a, 1)}
    for i, reg in enumerate((29501, 29502), start=1):
        if reg in r3 and r3[reg] != 0xFFFF:
            d["aux"][str(i)] = r3[reg] / 10
    if 2504 in r4:
        d["serial"] = ascii_regs(r4, 2504, 2510)
        model = ascii_regs(r4, 2515, 2525)          # e.g. "IONS-2K M1 MICROV11"
        d["model"], _, d["firmware"] = model.rpartition(" ") if " MICRO" in model else (model, "", "")
    return d


# ---------- daily state (today's energy and per-panel energy) ----------

LOCK = threading.Lock()


def today():
    return time.strftime("%Y-%m-%d")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


STATE = load_state()   # name -> {"day", "base_total", "pv_wh": {pv: Wh}, "last_t"}


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(STATE, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"warning: cannot write {STATE_FILE}: {e}", file=sys.stderr)


def update_daily(name, d, now):
    with LOCK:
        st = STATE.get(name)
        if not st or st.get("day") != today():
            st = {"day": today(), "base_total": d["total"], "pv_wh": {k: 0.0 for k in d["pv"]}, "last_t": now}
            STATE[name] = st
        dt = now - st.get("last_t", now)
        if 0 < dt <= MAX_GAP:
            for k, p in d["pv"].items():
                st["pv_wh"][k] = st["pv_wh"].get(k, 0.0) + p["power"] * dt / 3600
        st["last_t"] = now
        if d["total"] < st["base_total"]:   # counter reset or inverter replaced
            st["base_total"] = d["total"]
        d["today"] = round(d["total"] - st["base_total"], 1)
        d["pv_today"] = {k: round(v / 1000, 3) for k, v in st["pv_wh"].items()}
        save_state()


# ---------- polling ----------

CACHE = {}   # name -> dict(ip, data, err, dt, ok_t)
INFO = {}    # name -> (serial, model, firmware)


def poller(name, ip):
    while True:
        t0 = time.time()
        try:
            r4, r3 = read_inverter(ip, name not in INFO)
            d = decode(r4, r3)
            if "serial" in d:
                INFO[name] = (d["serial"], d["model"], d["firmware"])
            update_daily(name, d, time.time())
            CACHE[name] = dict(ip=ip, data=d, err=None, dt=time.time() - t0, ok_t=time.time())
        except Exception as e:  # noqa: BLE001 - any failure just marks the inverter down
            prev = CACHE.get(name, {})
            keep = prev.get("data") if prev.get("ok_t") and time.time() - prev["ok_t"] <= MAX_AGE else None
            CACHE[name] = dict(ip=ip, data=keep, err=e, dt=time.time() - t0, ok_t=prev.get("ok_t"))
        time.sleep(POLL)


METRICS = [
    ("ions_up", "1 if the inverter answered the last Modbus read"),
    ("ions_scrape_seconds", "duration of the last Modbus read"),
    ("ions_last_success_timestamp_seconds", "time of the last successful read"),
    ("solar_info", "serial number, model, firmware and rated power"),
    ("solar_state", "raw state register (0 = producing)"),
    ("solar_ac_power_watts", "AC active power delivered"),
    ("solar_dc_power_watts", "DC power from the panels (sum of all inputs)"),
    ("solar_apparent_power_va", "AC apparent power"),
    ("solar_power_factor", "power factor"),
    ("solar_energy_today_kwh", "energy produced today (lifetime counter minus first reading of the day)"),
    ("solar_energy_total_kwh", "lifetime energy produced"),
    ("solar_grid_voltage_volts", "grid voltage"),
    ("solar_grid_current_amperes", "AC current delivered"),
    ("solar_grid_frequency_hertz", "grid frequency"),
    ("solar_temperature_celsius", "internal temperature"),
    ("solar_aux_temperature_celsius", "auxiliary temperatures"),
    ("solar_pv_voltage_volts", "panel DC voltage"),
    ("solar_pv_current_amperes", "panel DC current"),
    ("solar_pv_power_watts", "panel DC power (V x A)"),
    ("solar_pv_energy_today_kwh", "panel energy today (V x A integrated by the exporter)"),
]


def lbl(**kw):
    def esc(v):
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in kw.items()) + "}"


def collect():
    rows = {m: [] for m, _ in METRICS}
    for name, ip in targets():
        c = CACHE.get(name)
        if c is None:
            continue
        base = dict(site=SITE, inverter=name)
        up = dict(base)
        if c["err"] is not None:
            up["error"] = str(c["err"])[:80]
        rows["ions_up"].append((up, 0 if c["err"] else 1))
        rows["ions_scrape_seconds"].append((base, round(c["dt"], 3)))
        if c["ok_t"]:
            rows["ions_last_success_timestamp_seconds"].append((base, round(c["ok_t"])))
        if name in INFO:
            serial, model, fw = INFO[name]
            rows["solar_info"].append((dict(base, ip=ip, serial=serial, model=model, firmware=fw, rated_w=RATED_W), 1))
        d = c["data"]
        if d is None:
            continue
        for m, k in (("solar_state", "state"), ("solar_ac_power_watts", "ac_w"), ("solar_dc_power_watts", "dc_w"),
                     ("solar_apparent_power_va", "va"), ("solar_power_factor", "pf"),
                     ("solar_energy_today_kwh", "today"), ("solar_energy_total_kwh", "total"),
                     ("solar_grid_voltage_volts", "grid_v"), ("solar_grid_current_amperes", "grid_a"),
                     ("solar_grid_frequency_hertz", "grid_hz"), ("solar_temperature_celsius", "temp_c")):
            if d.get(k) is not None:
                rows[m].append((base, d[k]))
        for s, v in d["aux"].items():
            rows["solar_aux_temperature_celsius"].append((dict(base, sensor=s), v))
        for i, p in d["pv"].items():
            lp = dict(base, pv=i)
            rows["solar_pv_voltage_volts"].append((lp, p["voltage"]))
            rows["solar_pv_current_amperes"].append((lp, p["current"]))
            rows["solar_pv_power_watts"].append((lp, p["power"]))
            rows["solar_pv_energy_today_kwh"].append((lp, d["pv_today"].get(i, 0)))
    out = []
    for m, h in METRICS:
        if not rows[m]:
            continue
        out.append(f"# HELP {m} {h}")
        out.append(f"# TYPE {m} gauge")
        out.extend(f"{m}{lbl(**l)} {v}" for l, v in rows[m])
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/metrics":
            body, ctype = collect().encode(), "text/plain; version=0.0.4"
        elif path == "/":
            body, ctype = (f"<h1>ions-exporter {__version__}</h1><p><a href='/metrics'>/metrics</a></p>").encode(), "text/html"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # PID 1 in Docker ignores SIGTERM otherwise
    ts = targets()
    if not ts:
        sys.exit("IONS_TARGETS is empty, e.g. IONS_TARGETS=micro1=192.168.1.50,micro2=192.168.1.51")
    if os.environ.get("ONCE"):
        for n, ip in ts:
            r4, r3 = read_inverter(ip, True)
            print(n, json.dumps(decode(r4, r3), ensure_ascii=False))
        return
    for n, ip in ts:
        threading.Thread(target=poller, args=(n, ip), daemon=True).start()
        time.sleep(1)
    print(f"ions-exporter {__version__} on :{PORT}, {len(ts)} inverter(s), state in {STATE_FILE}", file=sys.stderr)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
