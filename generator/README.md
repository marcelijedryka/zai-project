# zai-sensor – sensor emulator and sample-data seeder

`zai-sensor` plays the part of the IoT sensors of the ZAI 26Z measurement API. It sends
measurements with a sensor's API key (`POST /api/measurements`), backfills past data, streams live
values, and seeds the sample data (F11) through the public API only. Values are synthetic
(`uniform | random-walk | sine | constant`) or real Warsaw temperature and air quality from
[Open-Meteo](https://open-meteo.com/) (no key needed).

## Install and run

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install ./generator                 # installs the `zai-sensor` command
zai-sensor --help

# or without installing:
uv run --project generator zai-sensor --help
```

## Secrets

- The **sensor key** comes from `--api-key`, the `ZAI_API_KEY` variable, or a hidden prompt (in that
  order). Prefer the variable or the prompt: a value on the command line shows up in the process list.
- The **admin password** (only `seed`) comes from `ZAI_ADMIN_PASSWORD` or a hidden prompt, never from
  a flag.
- **Plain `http://` is refused** unless the host is loopback (`localhost`, `127.0.0.0/8`, `::1`), because the
  key and the admin password would travel unencrypted. Use `https://`; `--allow-insecure` overrides
  this (e.g. a compose-internal hostname). The check runs before anything is sent or asked for.
- Nothing is hard-coded and keys are never logged, not even with `--verbose`. `seed` keeps the new
  sensor keys in memory; `--save-keys PATH` additionally writes them (mode 600).

## Global options (before the command)

| Option | Default | Meaning |
|---|---|---|
| `--api-url URL` / `ZAI_API_URL` | – (required) | API base URL, e.g. `http://localhost:8000` |
| `--api-key KEY` / `ZAI_API_KEY` | prompt | Sensor key (see above) |
| `--timeout 10s` | `10s` | Per-request timeout |
| `--retries N` | `3` | Retries on 429, 503 and connection errors (see below) |
| `--verbose` | off | Log every request (never the key) |
| `--dry-run` | off | Print what would be sent; sends nothing, needs no key |
| `--allow-insecure` | off | Allow plain `http://` to a non-local host (see below) |

## Commands

### `send`: one value

```bash
zai-sensor --api-url http://localhost:8000 send --value 21.5
zai-sensor ... send --value 21.5 --timestamp 2026-10-01T12:00:00Z    # a past point
```

### `backfill`: past values, oldest first

Either `--count N` (the last N points, ending at `--end`) or `--start` (up to `--end`). `--end`
defaults to now and must not be in the future. Timestamps need a UTC offset.

| Option | Default | Meaning |
|---|---|---|
| `--count N` / `--start ISO` | one required | How many points / where to begin |
| `--end ISO` | now | Last timestamp |
| `--interval 1h` | `1h` | Spacing of synthetic points |
| `--source synthetic\|open-meteo` | `synthetic` | Where the values come from |
| `--show-each` | off | Print every accepted point (otherwise only runs of up to 20 points do) |

### `live`: one value every interval, with the server's clock

```bash
zai-sensor ... live --interval 3s --pattern sine --min 10 --max 35
zai-sensor ... live --interval 15m --source open-meteo --variable pm2_5    # latest observation
```

`--interval` (default `10s`), `--count N` stops after N values; Ctrl+C prints the summary.

### `seed`: the sample data (F11)

Logs in as admin, creates the series and one sensor per series through the API, then backfills
about 14 days of hourly values with each sensor's own key, and logs out. Series that already hold
data are skipped, so running it twice duplicates nothing.

```bash
ZAI_ADMIN_PASSWORD=... zai-sensor --api-url http://localhost:8000 seed --config generator/seed.example.toml
```

| Option | Default | Meaning |
|---|---|---|
| `--config FILE` | required | Seed file; see `seed.example.toml` (5 series, none prefixed `ZAI-TEST-`) |
| `--admin-user U` | `admin` | Admin username |
| `--days N` | from the file (14) | History length |
| `--source synthetic` | file | Use each series' own synthetic pattern instead of Open-Meteo (offline) |
| `--save-keys PATH` | off | Also write `{series: key}` here, mode 600 (gitignored: `*.keys.json`) |

### Synthetic pattern options (`backfill`, `live`)

| Option | Default | Meaning |
|---|---|---|
| `--pattern` | `sine` | `uniform`, `random-walk`, `sine` or `constant` |
| `--min`, `--max` | `0`, `100` | Series range; values are kept inside it |
| `--offset` | mid-range | Sine centre / constant level |
| `--amplitude` | quarter of the range | Sine amplitude |
| `--period 24h` | `24h` | Sine period |
| `--peak-hour H` | `15` | UTC hour of day of the sine maximum |
| `--noise S` | `0` | Gaussian noise (standard deviation) |
| `--step S` | 2 % of the range | Random-walk step |
| `--seed N` | random | Same seed and timestamps, same values |
| `--out-of-range-every N` | off | Every Nth value is deliberately invalid (the F4 demo) |

### Open-Meteo options (`backfill`, `live`)

| Option | Default | Meaning |
|---|---|---|
| `--variable` | `temperature_2m` | `temperature_2m`, `pm2_5`, `pm10`, `nitrogen_dioxide` |
| `--lat`, `--lon` | Warsaw (52.2297, 21.0122) | Location |

Temperature uses the forecast API, particulate matter and NO₂ the air-quality API. Open-Meteo's
times are naive; they are converted to UTC. `null` values are skipped, hours in the future are
dropped, and at most 92 past days are available. Recent air-quality hours are often `null`.

## Output and exit codes

```
✔ #1234 23.4 °C @ 2026-10-11T12:00:00Z                      accepted (201)
✘ REJECTED (422): value 612 outside [0, 500] for ...        rejected, the run continues (red)
✘ Sensor key rejected – sensor unregistered or key invalid  401, the run stops
sent=336 accepted=336 rejected=0 failed=0                   final summary
```

| Exit code | Meaning |
|---|---|
| 0 | everything accepted |
| 1 | some values rejected (422) or failed after the retries |
| 2 | the sensor key was refused (401) |
| 3 | fatal: API or data source unreachable, bad admin login, invalid seed file |
| 64 | invalid command line |

**Retries.** The API has no idempotency key, so a request that may have been stored is never
repeated. Retried: connection errors (the request never left), 429 and 503, honouring
`Retry-After` (otherwise 0.5 s, 1 s, 2 s ...). Not retried: timeouts after sending and every other
status; these are reported as failed.

## Examples

```bash
export ZAI_API_URL=http://localhost:8000 ZAI_API_KEY=zai_...      # the key shown once at registration

zai-sensor backfill --count 48 --pattern sine --min 10 --max 35 --seed 42    # two days of history
zai-sensor backfill --start 2026-09-01T00:00:00Z --end 2026-09-03T00:00:00Z --interval 30m
zai-sensor live --interval 3s --pattern random-walk --min 10 --max 35         # watch the dashboard
zai-sensor send --value 999                                                   # F4: red 422 rejection
zai-sensor backfill --count 12 --out-of-range-every 4 --min 0 --max 100       # rejections mid-run
zai-sensor backfill --source open-meteo --variable pm10 --count 48            # real data
zai-sensor --dry-run backfill --count 5                                       # preview only
```

## Development

```bash
cd generator
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest --cov
```

Tests mock the API and Open-Meteo (`respx`); the real Open-Meteo API is never called. A separate
integration test in `backend/tests/integration/test_generator.py` runs the CLI against the real
application and database.
