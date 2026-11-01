"""Read public project model selection; never reads credentials or probes a provider."""
import json
from pathlib import Path


def model_route():
    path = Path(__file__).resolve().parents[2] / 'config/model_route.json'
    return json.loads(path.read_text(encoding='utf-8'))


def model_identity():
    route = model_route()
    return {'route': route['provider'], 'model': route['model']}
