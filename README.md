# HackSphere 3.0 – Smart Energy Microgrid & Demand Forecasting Engine

**Live app (Catalyst AppSail):** https://microgrid-50047155770.development.catalystappsail.in

Forecasts hourly energy demand (kW) for the next 24 hours with a **Zoho Catalyst QuickML** regression
model, then applies the dispatch rules to decide between **Main Grid** and **Renewable Storage** and
to raise **Critical Overload** alerts. A responsive dashboard (Catalyst **AppSail**) shows the load
curve, active grid state, battery state of charge and alerts.

### Dashboard features
* **3D digital twin** (Three.js 0.160, `app/static/twin.js`): solar array, grid pylon, battery and buildings with energy particles flowing along the cables for the selected hour; the sun follows the hour and irradiance, overloaded hours ring the buildings in red. Falls back to the 2D diagram if WebGL or the CDN is unavailable; respects reduced motion.
* **Power-flow console**: single-line diagram (solar, main grid, battery, load) you can scrub or play hour by hour, with the rule that decided each hour's mode.
* **Grid Assistant (chat)**: ask about the forecast on screen (answers in English), with one-click frequently asked questions. Uses Claude (`claude-opus-5-5`) when `ANTHROPIC_API_KEY` is set; otherwise built-in answers computed from the same data. Each reply is labelled with its source.
* **What-if heatwave simulator**: shift temperatures by −5…+5 °C and re-run the real model; compared side by side and drawn on the load curve.
* CSV upload, manual entry, sample replay, battery settings, CSV export and a print / save-as-PDF report.

## What is in this repo

| Path | Purpose |
|---|---|
| `predict.py` | Evaluator entry point. `predict(input_df)` calls the published QuickML endpoint and returns one value per row. CLI: `python predict.py --input sample_test_input.csv --output predictions.csv` |
| `features.py` | Feature engineering used to build the training CSV (identical logic is inside `predict.py` and `app/quickml_client.py`). |
| `train_quickml.csv` | Training data uploaded to QuickML (`train_telemetry.csv` + engineered features). |
| `test_eval.py`, `sample_test_input.csv` | Starter-kit self-test and sample input. |
| `app/` | Web application (Flask on AppSail): backend, controller, dashboard, tests. |
| `tools/get_refresh_token.py` | One-time helper that turns a Zoho Self Client grant code into a refresh token. |
| `tools/make_build.py` | Builds `build/` (code + dependencies) for `catalyst deploy`. |

## Model (QuickML)

Exploration of `train_telemetry.csv` (4000 hourly rows) showed that demand is driven by

1. **hour of day** (night ≈ 90 kW, morning ≈ 175 kW, daytime ≈ 145 kW, evening 18–22 h ≈ 198 kW baseline), and
2. a **cooling load** that starts at **24 °C**: ≈ +8.6 kW per °C above 24 °C.

Humidity and solar irradiance carry no extra signal. Tree models cannot extrapolate to the hotter
test period, so a linear model on the right features is both simplest and best:

* Features: `cooling_load = max(0, ambient_temp_c − 24)` and one-hot `hour_0 … hour_23`.
* Algorithm: **Linear Regression** (QuickML pipeline: Source → Select/Drop → Linear Regression).
* Chronological hold-out RMSE ≈ 8.3 locally, **8.76** in QuickML (noise floor of the data ≈ 8.5);
  Random Forest / GBM reached 12–13 on the same split.

### Reproduce in QuickML
1. Upload `train_quickml.csv` as a dataset.
2. Create a *Prediction* pipeline, target `target_demand_kw`.
3. Add **Select/Drop** (drop `ambient_temp_c`, `humidity_pct`, `solar_irradiance_wm2`, `hour`) → **Linear Regression**.
4. Execute, create an endpoint and publish it.

## Dispatch rules (`app/controller.py`)

| Condition | Result |
|---|---|
| demand **> 160 kW** or hour **18–22 inclusive** | **Renewable Storage Mode** |
| otherwise | **Main Grid Mode** |
| demand **≥ 200 kW** | **Critical Overload Alert** (independent of mode) |

Boundaries are covered by unit tests (160 vs 160.01, hours 17/18/22/23, 199.99 vs 200).
The battery state of charge is an illustrative model (capacity, initial charge adjustable on the
dashboard); it never overrides the grid mode.

## Architecture

```
Browser (dashboard, static HTML/JS)
   │  CSV upload / manual rows / bundled sample window
   ▼
AppSail – Flask (app/app.py)
   ├─ cleaning & validation (timestamps, missing values, ranges, duplicates)
   ├─ quickml_client.py ──► QuickML published endpoint (batch call, OAuth token cached)
   ├─ controller.py     ──► mode, overload alert, battery state of charge
   └─ in-memory system log (/api/logs)
```

API: `POST /api/predict` (stateless prediction proxy, `{"rows":[{hour,ambient_temp_c,humidity_pct,solar_irradiance_wm2}]}`), `GET /api/health`, `GET /api/sample`, `GET /api/sample-info`, `POST /api/forecast`
(multipart `file` or JSON `{"rows":[…]}`), `GET /api/logs`, `POST /api/chat` (Grid Assistant; 30 questions per visitor per 10 minutes).
If QuickML is unreachable or not configured the dashboard still works and **clearly labels the
numbers as an offline estimate**.

## Environment variables (never commit values)

| Variable | Meaning |
|---|---|
| `QUICKML_ENDPOINT_KEY` | Endpoint key (`X-QUICKML-ENDPOINT-KEY` header) |
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` | Self Client credentials; used to mint short-lived access tokens (scope `QuickML.deployment.READ`) |
| `CATALYST_ORG` | Catalyst org id (default `60093257910`) |
| `QUICKML_ENVIRONMENT` | `Development` (default) or `Production` |
| `QUICKML_ENDPOINT_URL` | Optional override of the endpoint URL (default is in the code) |
| `ZOHO_ACCOUNTS_URL` | Default `https://accounts.zoho.in` |
| `QUICKML_ACCESS_TOKEN` | Optional static token instead of the refresh flow (testing) |
| `ANTHROPIC_API_KEY` | Optional. Turns on AI answers in the Grid Assistant (Claude). Without it the assistant uses built-in answers. |
| `ASSISTANT_MODEL` | Optional model override for the assistant (default `claude-opus-5-5`) |

### Getting the refresh token (once)
1. <https://api-console.zoho.in> → **Self Client** → scope `QuickML.deployment.READ` → create a code (10 min) → download `self_client.json` into `tools/`.
2. `python tools/get_refresh_token.py` prints the refresh token. Keep it secret.

## Run locally

```bash
pip install -r app/requirements.txt
# set the variables above (PowerShell: $env:NAME="value"), then
cd app && python app.py        # http://localhost:9000
python -m unittest discover -s app/tests      # 22 tests
python predict.py --input sample_test_input.csv --output predictions.csv
python test_eval.py                           # must print SUCCESS
```

## Deploy on Catalyst AppSail

```bash
npm install -g zcatalyst-cli          # once (PowerShell: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned)
catalyst login
python tools/make_build.py            # creates build/ with code + dependencies
catalyst init                         # AppSail -> Catalyst-Managed Runtime -> name microgrid,
                                      #   build path ./build, stack Python 3.13
catalyst deploy
```
`catalyst init` writes `app-config.json` in the project root with a placeholder `command`.
Replace it with the real start-up command (this repo already contains the finished file):

```json
{"command":"python3 -u app.py","build_path":"./build","stack":"python_3_13","env_variables":{},"memory":512,"scripts":{}}
```
Then add the environment variables above in the Catalyst console
(Serverless -> AppSail -> microgrid -> Configuration -> Environment Variables) and open the
AppSail URL. The dashboard header shows **QuickML model live** when the endpoint is reachable.
The app listens on `X_ZOHO_CATALYST_LISTEN_PORT` (default 9000).
Do not re-run `make_build.py` on a OneDrive folder while `build/` is open/synced (Windows may
refuse to delete it); rename the old `build/` first.

## How `predict.py` reaches QuickML
1. **Direct**: if `QUICKML_ENDPOINT_KEY` and the Zoho OAuth variables are set, it calls the QuickML endpoint itself.
2. **Via the deployed app**: with no credentials (e.g. on an evaluator's machine) it posts the features to
   `<live app>/api/predict`; the app holds the credentials server-side and returns the QuickML predictions.
   Override the address with `QUICKML_PROXY_URL`.
3. **Offline fallback**: only if both are unreachable; a WARNING is printed to stderr.

## Security notes
* No secrets are stored in the repository; `.gitignore` excludes `self_client.json` and env files.
* `/api/health` and `/api/logs` never include credentials. Uploads are limited to 1 MB.
* Rotate the endpoint key and the Self Client after the event.
