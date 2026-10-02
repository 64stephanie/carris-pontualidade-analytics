"""

"""

import json
import os
import sys
import time
from pathlib import Path

import requests
import snowflake.connector
import yaml

CARRIS_URL = "https://api.carrismetropolitana.pt/v2"
IPMA_OBSERVATIONS_URL = "https://api.ipma.pt/open-data/observation/meteorology/stations/observations.json"
CONFIG_FILE = Path("config/lines.yml")
DATABASE = "CARRIS_RAW"
PAUSE = 0.2       # segundos entre pedidos, para não sobrecarregar as APIs
RETRIES = 3       # tentativas por pedido antes de desistir


# ---------- APIs ----------

def fetch(url):
    """Faz um pedido GET com algumas tentativas. Devolve o JSON."""
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            time.sleep(PAUSE)
            return response.json()
        except requests.RequestException as error:
            print(f"    tentativa {attempt}/{RETRIES} falhou: {error}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"Não foi possível obter {url}")


def load_config():
    """Lê as linhas e as paragens do config/lines.yml."""
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    lines = [entry["line_id"] for entry in config["lines"]]
    stops = sorted({stop for entry in config["lines"] for stop in entry["stops"]})
    return lines, stops


# ---------- Snowflake ----------

def connect():
    """Liga ao Snowflake com as credenciais das variáveis de ambiente."""
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ["SNOWFLAKE_ROLE"],
        database=DATABASE,
    )


def insert(cursor, table, payload, key_column=None, key_value=None):
    """Grava uma resposta inteira numa tabela raw, com o JSON numa coluna VARIANT."""
    body = json.dumps(payload)
    if key_column:
        cursor.execute(
            f"INSERT INTO {table} ({key_column}, payload) SELECT %s, PARSE_JSON(%s)",
            (key_value, body),
        )
    else:
        cursor.execute(
            f"INSERT INTO {table} (payload) SELECT PARSE_JSON(%s)",
            (body,),
        )


# ---------- Recolhas ----------

def ingest_arrivals(cursor, stops):
    for stop_id in stops:
        payload = fetch(f"{CARRIS_URL}/arrivals/by_stop/{stop_id}")
        insert(cursor, "carris.arrivals", payload, "stop_id", stop_id)
        # Uma lista vazia não é um erro: guardamos na mesma, para saber que a recolha correu
        print(f"    paragem {stop_id}: {len(payload)} chegadas")


def ingest_demand(cursor, lines):
    for line_id in lines:
        payload = fetch(f"{CARRIS_URL}/metrics/demand/by_line/{line_id}")
        insert(cursor, "carris.demand", payload, "line_id", line_id)
        print(f"    linha {line_id}: gravada")


def ingest_vehicles(cursor):
    payload = fetch(f"{CARRIS_URL}/vehicles")
    insert(cursor, "carris.vehicles", payload)
    print(f"    {len(payload)} veículos")


def ingest_weather(cursor):
    payload = fetch(IPMA_OBSERVATIONS_URL)
    insert(cursor, "ipma.observations", payload)
    print(f"    {len(payload)} horas de observações")


def main():
    lines, stops = load_config()
    print(f"{len(lines)} linhas e {len(stops)} paragens no {CONFIG_FILE}\n")

    connection = connect()
    cursor = connection.cursor()

    jobs = [
        ("Chegadas", lambda: ingest_arrivals(cursor, stops)),
        ("Procura", lambda: ingest_demand(cursor, lines)),
        ("Veículos", lambda: ingest_vehicles(cursor)),
        ("Meteorologia (IPMA)", lambda: ingest_weather(cursor)),
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

    cursor.close()
    connection.close()

    if failures:
        print(f"Terminado com erros em: {', '.join(failures)}")
        sys.exit(1)  # faz o GitHub Actions marcar o run como falhado
    print("Terminado sem erros.")


if __name__ == "__main__":
    main()
