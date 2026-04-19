# solar-GW

FastAPI energy dashboard for combining Growatt battery data, GoodWe solar data, Tesla Wall Connector 3 charging behavior, and two Tesla vehicles into one hostable service.

## What it includes

- Server-rendered FastAPI dashboard plus JSON endpoint at `/api/dashboard`
- Tesla Fleet API adapters for Tesla vehicle charging data and Wall Connector behavior inference
- Flexible Growatt and GoodWe adapters that normalize vendor JSON from account-specific endpoints
- Demo mode so the dashboard still renders before live credentials are configured
- A `systemd` unit template for Linux hosting

## Quick start

1. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy the example environment file and add your credentials:

   ```bash
   cp .env.example .env
   ```

3. Run the app locally:

   ```bash
   uvicorn app.main:app --reload --port 8003
   ```

4. Open [http://localhost:8003](http://localhost:8003).

## Provider setup

### Tesla Wall Connector and Tesla vehicles

This app supports two Tesla auth modes:

- OAuth client flow with `TESLA_CLIENT_ID` and `TESLA_CLIENT_SECRET`
- Direct bearer token with `TESLA_ACCESS_TOKEN`

For the OAuth flow, set:

- `TESLA_CLIENT_ID`
- `TESLA_CLIENT_SECRET`
- `TESLA_REDIRECT_URI`
- `TESLA_SCOPE`
- `TESLA_TOKEN_STORE_PATH`
- `TESLA_VEHICLE_VINS`
- `WALL_CONNECTOR_NAME`
- `WALL_CONNECTOR_LOCATION`
- `WALL_CONNECTOR_MAX_KW`
- `WALL_CONNECTOR_CIRCUIT_AMPS`

Then open `/auth/tesla/login` or click the dashboard connect button.

For the manual token flow, set:

- `TESLA_ACCESS_TOKEN`
- `TESLA_VEHICLE_VINS`
- `WALL_CONNECTOR_NAME`
- `WALL_CONNECTOR_LOCATION`
- `WALL_CONNECTOR_MAX_KW`
- `WALL_CONNECTOR_CIRCUIT_AMPS`

The implementation uses Tesla Fleet API OAuth endpoints and vehicle data:

- `GET https://auth.tesla.com/oauth2/v3/authorize`
- `POST https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token`
- `GET /api/1/vehicles/{vin}/vehicle_data`

Tesla’s official Fleet API docs expose vehicle charging state and recommend Fleet Telemetry for efficient realtime monitoring. I did not find an official standalone Wall Connector device endpoint in the Fleet API docs, so the dashboard models charger sessions from vehicle charge state instead.

### Growatt

Growatt API access is commonly tied to ShineServer/OSS account permissions and API tokens. The app accepts:

- `GROWATT_OVERVIEW_URL`
- `GROWATT_BATTERY_URL`
- `GROWATT_TOKEN`
- `GROWATT_SERVER_URL`
- `GROWATT_PLATFORM`

If you have vendor-issued JSON endpoints, point `GROWATT_OVERVIEW_URL` and `GROWATT_BATTERY_URL` at those. If you only have a Growatt API token, the app can also query Growatt's token API directly via `GROWATT_SERVER_URL`.
This dashboard is currently tuned for the Growatt hybrid inverter platform and prefers SPH/storage-style battery detail endpoints when devices are visible.

### GoodWe

GoodWe can be connected in two ways.

Preferred sign-in method:

- `GOODWE_USERNAME`
- `GOODWE_PASSWORD`
- `GOODWE_PLANT_ID`
- `GOODWE_API_URL`

The Plant ID is the alphanumeric string at the end of the plant URL in SEMS Portal. This follows the SEMS-based configuration described by GoodWe ecosystem docs.

Optional direct API method:

- `GOODWE_OVERVIEW_URL`
- `GOODWE_BATTERY_URL`
- `GOODWE_TOKEN`

These should point at the authorized SEMS/Open API endpoints for your install if GoodWe has granted explicit API access.

## systemd deployment

Update the paths and user in [systemd/solar-gw.service](/Users/sekkevin/LocalR/solar_GW/systemd/solar-gw.service), then install it:

```bash
sudo cp systemd/solar-gw.service /etc/systemd/system/solar-gw.service
sudo systemctl daemon-reload
sudo systemctl enable --now solar-gw.service
sudo systemctl status solar-gw.service
```
