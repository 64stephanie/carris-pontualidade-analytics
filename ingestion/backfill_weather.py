"""
Carga inicial (corre-se uma única vez): meteorologia diária desde 1 de janeiro
de 2024 até ontem, para cada linha do config/lines.yml.

Cada linha usa as coordenadas da sua paragem do meio (a segunda paragem
do lines.yml), lidas do endpoint /stops da Carris.

Uso (a partir da pasta raiz do repositório):
    pip install requests pyyaml snowflake-connector-python
    python ingestion/backfill_weather.py
"""

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import snowflake.connector
import yaml

CARRIS_URL = "https://api.carrismetropolitana.pt/v2"
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARIABLES = "precipitation_sum,temperature_2m_max,temperature_2m_min"
CONFIG_FILE = Path("config/lines.yml")
START_DATE = date(2024, 1, 1)
PAUSE = 1.0   # o Open-Meteo é gratuito: não vale a pena ter pressa


def fetch(url, params=None, retries=3):
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            time.sleep(PAUSE)
            return response.json()
        except requests.RequestException as error:
            print(f"    tentativa {attempt}/{retries} falhou: {error}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"Não foi possível obter {url}")


def weather_points():
    """Devolve, para cada linha, as coordenadas da paragem do meio."""
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    stops = {stop["id"]: stop for stop in fetch(f"{CARRIS_URL}/stops")}
    points = []
    for entry in config["lines"]:
        middle_stop = entry["stops"][len(entry["stops"]) // 2]
        stop = stops[middle_stop]
        points.append({
            "line_id": entry["line_id"],
            "stop_id": middle_stop,
            "latitude": float(stop["lat"]),
            "longitude": float(stop["lon"]),
        })
    return points


def fetch_weather(latitude, longitude, start, end):
    return fetch(OPEN_METEO_URL, params={
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": DAILY_VARIABLES,
        "timezone": "Europe/Lisbon",
    })


def main():
    yesterday = date.today() - timedelta(days=1)
    points = weather_points()
    print(f"A carregar a meteorologia de {START_DATE} a {yesterday} para {len(points)} linhas\n")

    connection = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ["SNOWFLAKE_ROLE"],
    )
    cursor = connection.cursor()

    for point in points:
        payload = fetch_weather(point["latitude"], point["longitude"], START_DATE, yesterday)
        cursor.execute(
            "INSERT INTO carris_raw.openmeteo.weather_daily (line_id, latitude, longitude, payload) "
            "SELECT %s, %s, %s, PARSE_JSON(%s)",
            (point["line_id"], point["latitude"], point["longitude"], json.dumps(payload)),
        )
        days = len(payload.get("daily", {}).get("time", []))
        print(f"    linha {point['line_id']} (paragem {point['stop_id']}): {days} dias")

    connection.commit()
    cursor.close()
    connection.close()
    print("\nCarga inicial terminada.")


if __name__ == "__main__":
    main()
