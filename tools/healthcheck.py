"""Container-local liveness/engine-failure check. No credentials or DB access."""
import json
import sys
from urllib.request import urlopen
try:
    with urlopen('http://127.0.0.1:9080/healthz', timeout=3) as r:
        good = r.status == 200 and json.load(r).get('ok') is True
except Exception:
    good = False
sys.exit(0 if good else 1)
