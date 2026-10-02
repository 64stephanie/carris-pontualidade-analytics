"""
Escolhe automaticamente 3 paragens (inicial, meio e final) para cada linha
da Carris Metropolitana e escreve o ficheiro config/lines.yml.

"""

import time
from datetime import date
from pathlib import Path

import requests

BASE_URL = "https://api.carrismetropolitana.pt/v2"
LINES = ["1715", "1721", "2790", "2711", "3508", "3022", "4600", "4701"]
OUTPUT = Path("config/lines.yml")
MAX_TRIES = 5   # quantas paragens vizinhas experimentar se uma não tiver chegadas
PAUSE = 0.2     # segundos entre pedidos, para não sobrecarregar a API


def get_json(endpoint):
    """Faz um pedido GET à API e devolve o JSON."""
    response = requests.get(f"{BASE_URL}/{endpoint}", timeout=30)
    response.raise_for_status()
    time.sleep(PAUSE)
    return response.json()


def get_pattern(pattern_id):
    """Devolve um percurso. A API responde com uma lista que tem um só objeto."""
    data = get_json(f"patterns/{pattern_id}")
    return data[0] if isinstance(data, list) else data


def find_valid_pattern(line, today):
    """Devolve o primeiro percurso da linha que está em vigor hoje."""
    for pattern_id in line.get("pattern_ids", []):
        pattern = get_pattern(pattern_id)
        if today in pattern.get("valid_on", []):
            return pattern
    return None


def count_arrivals(stop_id, line_id):
    """Conta as chegadas desta linha na paragem, hoje: o total e as já observadas."""
    arrivals = get_json(f"arrivals/by_stop/{stop_id}")
    of_line = [a for a in arrivals if a.get("line_id") == line_id]
    observed = [a for a in of_line if a.get("observed_arrival")]
    return len(of_line), len(observed)


def pick_stop(path, start_index, step, line_id, already_chosen):
    """
    A partir de uma posição do percurso, procura a primeira paragem
    que tenha chegadas da linha. Anda para a frente (step=1) ou para trás (step=-1).
    """
    index = start_index
    for _ in range(MAX_TRIES):
        if not 0 <= index < len(path):
            break
        stop_id = path[index]["stop_id"]
        if stop_id not in already_chosen:
            total, observed = count_arrivals(stop_id, line_id)
            if total > 0:
                return stop_id, total, observed
        index += step
    return None, 0, 0


def write_yaml(results, stop_names, today):
    """Escreve o config/lines.yml sem precisar de bibliotecas extra."""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    out = [f"# Gerado por select_stops.py a {today}", "lines:"]
    for r in results:
        headsign = r["headsign"].replace('"', "'")
        out += [
            f'  - line_id: "{r["line_id"]}"',
            f'    pattern_id: "{r["pattern_id"]}"',
            f'    headsign: "{headsign}"',
            "    stops:",
        ]
        for label, stop_id, _, _ in r["stops"]:
            name = stop_names.get(stop_id, "")
            out.append(f'      - "{stop_id}"   # {label}: {name}')
    OUTPUT.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    today = date.today().strftime("%Y%m%d")
    print(f"A procurar percursos em vigor a {today}...\n")

    all_lines = {line["id"]: line for line in get_json("lines")}
    stop_names = {stop["id"]: stop.get("long_name", "") for stop in get_json("stops")}

    results = []
    for line_id in LINES:
        line = all_lines.get(line_id)
        if line is None:
            print(f"[{line_id}] linha não encontrada na API, a saltar")
            continue

        pattern = find_valid_pattern(line, today)
        if pattern is None:
            print(f"[{line_id}] nenhum percurso em vigor hoje, a saltar")
            continue

        path = sorted(pattern["path"], key=lambda p: p["stop_sequence"])
        positions = [
            ("inicial", 0, 1),
            ("meio", len(path) // 2, 1),
            ("final", len(path) - 1, -1),
        ]

        stops = []
        chosen = set()
        for label, index, step in positions:
            stop_id, total, observed = pick_stop(path, index, step, line_id, chosen)
            if stop_id:
                stops.append((label, stop_id, total, observed))
                chosen.add(stop_id)

        print(f"[{line_id}] percurso {pattern['id']} -> {pattern.get('headsign', '')}")
        for label, stop_id, total, observed in stops:
            name = stop_names.get(stop_id, "")
            print(f"    {label:<8} {stop_id}  {name}  ({total} chegadas hoje, {observed} já observadas)")
        if len(stops) < 3:
            print("    ATENÇÃO: não foi possível encontrar 3 paragens com dados")
        print()

        results.append({
            "line_id": line_id,
            "pattern_id": pattern["id"],
            "headsign": pattern.get("headsign", ""),
            "stops": stops,
        })

    write_yaml(results, stop_names, today)
    print(f"Ficheiro escrito: {OUTPUT}")


if __name__ == "__main__":
    main()
