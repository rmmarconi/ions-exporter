# ions-exporter

Prometheus exporter for **Intelbras IONS-2K M1** microinverters, read directly over
**Modbus TCP** through the inverter's built-in Wi-Fi. No cloud, no app, no extra
hardware. A single Python file using only the standard library.

[Leia em português](README.pt-BR.md)

> Unofficial project, not affiliated with Intelbras. The register map below was
> reverse-engineered and has only been tested on the IONS-2K M1 with firmware `MICROV11`.

## What you get

- AC and DC power, lifetime energy, energy today
- Voltage, current and power **per panel** (4 inputs), plus energy per panel today
- Grid voltage, current, frequency, power factor, apparent power
- Internal and auxiliary temperatures, state, serial number, model and firmware
- A ready-made Grafana dashboard ([grafana/ions-solar.json](grafana/ions-solar.json))

## Requirements

- The inverter reachable from the exporter host on **TCP 502** (Modbus TCP, unit id 1, no password).
  Give each inverter a fixed IP (DHCP reservation) on your router.
- Python 3.9+ or Docker.

## Quick start

```bash
# print one decoded reading and exit
ONCE=1 IONS_TARGETS=micro1=192.168.1.50 python3 exporter.py

# run the exporter (metrics on http://localhost:9596/metrics)
IONS_TARGETS=micro1=192.168.1.50,micro2=192.168.1.51 python3 exporter.py
```

### Docker Compose

```yaml
services:
  ions-exporter:
    build: .
    restart: unless-stopped
    environment:
      IONS_TARGETS: "micro1=192.168.1.50,micro2=192.168.1.51"
      SITE: "home"
      TZ: "America/Sao_Paulo"
    volumes:
      - ./state:/state
    ports:
      - "9596:9596"
```

See [docker-compose.yml](docker-compose.yml) and [prometheus.yml](prometheus.yml).

### Prometheus

```yaml
scrape_configs:
  - job_name: ions
    scrape_interval: 60s
    static_configs:
      - targets: ["ions-exporter:9596"]
```

### Grafana

Import [grafana/ions-solar.json](grafana/ions-solar.json) (Dashboards → New → Import) and pick
your Prometheus data source. Set the **Tariff per kWh** variable at the top to see savings.
The yearly chart needs Prometheus retention of at least one year
(`--storage.tsdb.retention.time=400d`).

## Configuration

| Variable       | Default            | Description |
|----------------|--------------------|-------------|
| `IONS_TARGETS` | (required)         | `name=ip` pairs separated by commas, e.g. `micro1=192.168.1.50,micro2=192.168.1.51` |
| `SITE`         | `ions`             | value of the `site` label on every series |
| `PORT`         | `9596`             | HTTP port for `/metrics` |
| `IONS_POLL`    | `30`               | seconds between reads of each inverter |
| `IONS_TIMEOUT` | `5`                | connect/read timeout (s) |
| `IONS_MAX_AGE` | `300`              | how long to keep serving the last good reading after the inverter stops answering (s) |
| `IONS_RATED_W` | `2000`             | rated power reported in `solar_info` |
| `STATE_FILE`   | `ions-state.json`  | where daily energy state is kept (`/state/ions-state.json` in the Docker image) |
| `TZ`           | system             | time zone used to roll over the day |

## Metrics

Labels: `site`, `inverter`, plus `pv` (1-4) on panel metrics and `sensor` on auxiliary temperatures.

| Metric | Unit |
|--------|------|
| `ions_up` (with an `error` label when 0) | 1/0 |
| `ions_scrape_seconds`, `ions_last_success_timestamp_seconds` | s |
| `solar_info{ip,serial,model,firmware,rated_w}` | 1 |
| `solar_state` | raw code (0 = producing) |
| `solar_ac_power_watts`, `solar_dc_power_watts` | W |
| `solar_apparent_power_va` | VA |
| `solar_power_factor` | 0-1 |
| `solar_energy_today_kwh`, `solar_energy_total_kwh` | kWh |
| `solar_grid_voltage_volts`, `solar_grid_current_amperes`, `solar_grid_frequency_hertz` | V, A, Hz |
| `solar_temperature_celsius`, `solar_aux_temperature_celsius{sensor}` | °C |
| `solar_pv_voltage_volts`, `solar_pv_current_amperes`, `solar_pv_power_watts` | V, A, W |
| `solar_pv_energy_today_kwh` | kWh |

The inverter stores neither daily energy nor per-panel energy, so the exporter computes them:

- **today** = lifetime counter − first reading of the day;
- **per panel** = voltage × current integrated at every read (gaps longer than 5 minutes are not counted).

That state is saved to `STATE_FILE` and survives restarts. On the first day both values start
when the exporter starts.

At night the inverter shuts down together with its Wi-Fi: `ions_up` goes to 0 and the
measurement series disappear until sunrise. In dashboards, use `last_over_time(...)` for
"today" values (the bundled dashboard already does).

## Register map

There is no public documentation for the IONS-2K M1. The map below was reverse-engineered and
validated against an independent energy meter. The Sungrow string-inverter map (5001+, 5011+…)
does **not** apply: those addresses return exception 2.

0-based addresses (the value sent in the Modbus frame). U32 = low word first.

| Function | Address   | Type / scale | Meaning |
|----------|-----------|--------------|---------|
| 4 | 2504-2509 | ASCII         | serial number |
| 4 | 2515-2524 | ASCII         | model and firmware (`IONS-2K M1 MICROV11`) |
| 4 | 5220      | U16           | state (0 = producing; 5120/5632 while shutting down) |
| 4 | 5227-5228 | U32, 0.1 kWh  | lifetime energy |
| 4 | 5231-5232 | U32, W        | AC active power (clips at the 2000 W rating) |
| 4 | 5235-5236 | U32, W        | total DC power (sum of the panels) |
| 4 | 5237-5238 | U32, VA       | apparent power |
| 4 | 5240      | S16, 0.001    | power factor |
| 4 | 5241      | U16, 0.01 Hz  | grid frequency |
| 4 | 5244      | U16, 0.1 °C   | internal temperature |
| 4 | 5246      | U16, 0.1 V    | grid voltage |
| 4 | 5249      | U16, 0.1 A    | AC current |
| 4 | 5332-5339 | U16           | panels 1-4: voltage (0.1 V) and current (0.1 A), interleaved |
| 3 | 29501-29502 | U16, 0.1 °C | two auxiliary temperatures (exact meaning unknown) |

`65535` (0xFFFF) = register not supported. Other readable ranges (function 4 2500-2559 and
5360-5419, function 3 29498-29519) hold only constant identification/configuration data.
Function 3 starting at 0 returns an exception.

## Limitations

- Tested only on the IONS-2K M1, firmware `MICROV11`. Other IONS models may use a different map;
  reports and pull requests are welcome.
- Read-only: the exporter never writes a register.
- Per-panel energy is an estimate from integrating V × A; accuracy depends on the poll interval.

## License

[GPL-3.0](LICENSE)
