# Public-data fixtures (synthetic)

Small synthetic files in each public source's own format, so that the readers are tested on real formats without
any recorded data. Every trip, alert text, pharmacy, person and number is invented; only public geography is
real (hub stations' names and rough coordinates, the alert zones' codes and names, the route ids Pisa's tables
know). No file here holds data from the sources themselves, so none of their licences applies to it.

They are built by `tests/support/synth/public.py` from a fixed seed (`uv run python -m
tests.support.synth.public` rewrites them byte for byte). Its docstring lists what the tests rely on.

| File | Format of | What it holds |
|---|---|---|
| `rome_trips.pb` | GTFS-RT trip updates (RSM) | 32 invented trips on 14 lines at 12:56 on 3 Nov 2026: several through Termini, two that end there, some outside the 90-minute look-ahead, and the 360 past an invented home stop |
| `rome_alerts.pb` | GTFS-RT service alerts (RSM) | six invented bus alerts and one Metro A alert active now |
| `rome_gtfs.zip` | static GTFS `stops.txt` and `agency.txt` | the stops those trips use (invented street names; Termini's platforms) |
| `nyc_trips_ace.pb` | MTA GTFS-RT, the A/C/E feed | invented A, C and E trips through 34 St-Penn Station, both directions |
| `nyc_gtfs.zip` | MTA static GTFS `stops.txt` | those trips' stations and platforms, and the 1/2/3 at Penn and Times Sq |
| `nyc_alerts.json` | MTA subway alerts JSON (Mercury) | 2 "Delays" active at the feed's time, 4 planned works (the A's reroute active at 1795870000) |
| `nws_alerts.json` | NWS `alerts/active` GeoJSON | one Moderate and one Minor alert for invented areas |
| `dpc_latest_all.zip` | DPC bulletin zip (today/tomorrow DBF zone tables) | Lazio's zones, Lomb-09 and Pugl-D, bulletin of 2 Nov 2026 14:57: green in Rome |
| `dpc_latest_all_yellow.zip` | the same | Lazi-D (Bacini di Roma) at ALLERTA GIALLA for temporali today |
| `lazio_pharmacies.csv` | Regione Lazio pharmacy registry (`;`, Latin-1) | 11 invented pharmacies near Termini: 4 usable, 5 closed, one 50 km off, one outside Rome |
