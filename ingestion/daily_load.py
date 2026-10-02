"""
Carga diária (corre de madrugada, pelo GitHub Actions):
  1. Procura diária de cada linha (cada resposta traz o histórico todo)
  2. Lista de linhas (para o snapshot no dbt)
  3. Meteorologia dos últimos 7 dias (os dados mais recentes podem chegar
     com atraso, por isso repetimos uma semana; o staging fica com a versão
     mais recente de cada linha e dia)

Uso (a partir da pasta raiz do repositório):
    pip install requests pyyaml snowflake-connector-python
    python ingestion/daily_load.py
"""

import json
import os
import sys
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
WEATHER_LOOKBACK_DAYS = 7
PAUSE = 0.5


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


def load_config():
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))["lines"]


def ingest_demand(cursor, lines):
    for entry in lines:
        line_id = entry["line_id"]
        payload = fetch(f"{CARRIS_URL}/metrics/demand/by_line/{line_id}")
        cursor.execute(
            "INSERT INTO carris_raw.carris.demand (line_id, payload) SELECT %s, PARSE_JSON(%s)",
            (line_id, json.dumps(payload)),
        )
    print(f"    {len(lines)} linhas gravadas")


def ingest_lines(cursor):
    payload = fetch(f"{CARRIS_URL}/lines")
    cursor.execute(
        "INSERT INTO carris_raw.carris.lines (payload) SELECT PARSE_JSON(%s)",
        (json.dumps(payload),),
    )
    print(f"    {len(payload)} linhas da rede gravadas")


def ingest_weather(cursor, lines):
    stops = {stop["id"]: stop for stop in fetch(f"{CARRIS_URL}/stops")}
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=WEATHER_LOOKBACK_DAYS)
    for entry in lines:
        middle_stop = entry["stops"][len(entry["stops"]) // 2]
        latitude = float(stops[middle_stop]["lat"])
        longitude = float(stops[middle_stop]["lon"])
        payload = fetch(OPEN_METEO_URL, params={
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": DAILY_VARIABLES,
            "timezone": "Europe/Lisbon",
        })
        cursor.execute(
            "INSERT INTO carris_raw.openmeteo.weather_daily (line_id, latitude, longitude, payload) "
            "SELECT %s, %s, %s, PARSE_JSON(%s)",
            (entry["line_id"], latitude, longitude, json.dumps(payload)),
        )
    print(f"    {len(lines)} linhas, de {start} a {end}")


def main():
    lines = load_config()
    connection = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ["SNOWFLAKE_ROLE"],
    )
    cursor = connection.cursor()

    jobs = [
        ("Procura", lambda: ingest_demand(cursor, lines)),
        ("Linhas", lambda: ingest_lines(cursor)),
        ("Meteorologia (Open-Meteo)", lambda: ingest_weather(cursor, lines)),
    ]

    failures = []
    for name, job in jobs:
        print(f"> {name}")
        try:
            job()
        except Exception as error:  # se uma fonte falhar, as outras continuam
            print(f"    ERRO: {error}")
            failures.append(name)
        print()

    connection.commit()
    cursor.close()
    connection.close()

    if failures:
        print(f"Terminado com erros em: {', '.join(failures)}")
        sys.exit(1)  # faz o GitHub Actions marcar o run como falhado
    print("Terminado sem erros.")


if __name__ == "__main__":
    main()
