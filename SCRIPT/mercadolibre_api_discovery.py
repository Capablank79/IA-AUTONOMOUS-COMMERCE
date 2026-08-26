import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.mercadolibre.com"
SITE_ID = "MLC"
CATALOG_PRODUCT_IDS = [
    "MLC75765637", "MLC72234235", "MLC75765649",
    "MLC69360615", "MLC28601073"
]
SENSITIVE = {
    "access_token", "refresh_token", "client_secret", "client_id",
    "email", "phone", "address", "first_name", "last_name",
    "nickname", "buyer", "payer", "collector", "contact"
}

def find_oauth():
    found = []
    for base in [Path.cwd(), *Path.cwd().parents]:
        found += list((base / "data" / "oauth").glob("mercadolibre_*.json"))
    if not found:
        raise FileNotFoundError(
            "No se encontro data/oauth/mercadolibre_*.json. "
            "Ejecuta el script desde el proyecto."
        )
    return max(found, key=lambda p: p.stat().st_mtime)

def redact(v):
    if isinstance(v, dict):
        return {
            k: "***REDACTED***" if k.lower() in SENSITIVE
            or "token" in k.lower() or "secret" in k.lower()
            else redact(x)
            for k, x in v.items()
        }
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v

def keys(v, prefix=""):
    out = set()
    if isinstance(v, dict):
        for k, x in v.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.add(p)
            out |= keys(x, p)
    elif isinstance(v, list):
        for x in v[:20]:
            out |= keys(x, prefix + "[]")
    return out

def get(token, path):
    req = Request(
        API_BASE + path,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=20) as r:
            return r.status, True, json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
        return e.code, False, raw
    except (URLError, json.JSONDecodeError) as e:
        return None, False, str(e)

def probe(report, token, level, name, path, note=""):
    status, ok, data = get(token, path)
    entry = {
        "level": level, "name": name, "path": path,
        "http": status, "ok": ok, "note": note
    }
    if ok:
        entry["keys"] = sorted(keys(data))
        entry["sample"] = redact(data)
    else:
        entry["error"] = redact(data)
    report.append(entry)

def main():
    oauth = find_oauth()
    connection = json.loads(oauth.read_text(encoding="utf-8"))
    token = connection.get("access_token")
    user_id = str(connection.get("user_id"))
    if not token or not user_id:
        raise RuntimeError("OAuth JSON no contiene access_token/user_id.")

    report = []

    # LEVEL 1: identity + catalog
    probe(report, token, "1", "Authenticated user",
          f"/users/{user_id}")
    probe(report, token, "1", "Catalog search",
          "/products/search?" + urlencode({
              "site_id": SITE_ID, "q": "aspiradora",
              "status": "active", "limit": 10
          }))
    for pid in CATALOG_PRODUCT_IDS:
        probe(report, token, "1", f"Catalog product {pid}",
              f"/products/{pid}")

    # LEVEL 2: demand / market signals
    probe(report, token, "2", "Trends MLC", f"/trends/{SITE_ID}",
          "Weekly search-trend capability.")
    for pid in CATALOG_PRODUCT_IDS:
        probe(report, token, "2", f"Best seller position {pid}",
              f"/highlights/{SITE_ID}/product/{pid}")
        probe(report, token, "2", f"Catalog competition {pid}",
              f"/products/{pid}/items")

    # LEVEL 3: seller/items/pricing/inventory
    seller_items_path = f"/users/{user_id}/items/search?limit=20"
    probe(report, token, "3", "Seller items search", seller_items_path)

    status, ok, data = get(token, seller_items_path)
    item_ids = []
    if ok and isinstance(data, dict):
        item_ids = [str(x) for x in data.get("results", []) if x]

    for item_id in item_ids[:5]:
        probe(report, token, "3", f"Item {item_id}", f"/items/{item_id}")
        probe(report, token, "3", f"Prices {item_id}",
              f"/items/{item_id}/prices")
        probe(report, token, "3", f"Sale price {item_id}",
              f"/items/{item_id}/sale_price?context=channel_marketplace")
        probe(report, token, "3", f"Visits {item_id}",
              f"/items/{item_id}/visits/time_window?last=7&unit=day")
        probe(report, token, "3", f"Pricing automation {item_id}",
              f"/pricing-automation/items/{item_id}/automation")
        probe(report, token, "3", f"Price history {item_id}",
              f"/pricing-automation/items/{item_id}/price/history?days=30&page=0&size=10")

    # LEVEL 4: business metrics
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=7)).isoformat()
    end = today.isoformat()
    probe(report, token, "4", "User visits time window",
          f"/users/{user_id}/items_visits/time_window?last=7&unit=day")
    probe(report, token, "4", "User visits date range",
          "/users/" + user_id + "/items_visits?" + urlencode({
              "date_from": start + "T00:00:00Z",
              "date_to": end + "T23:59:59Z"
          }))

    # LEVEL 5: sales
    probe(report, token, "5", "Seller orders",
          "/orders/search?" + urlencode({
              "seller": user_id,
              "order.status": "paid",
              "sort": "date_desc"
          }),
          "Read-only; sensitive fields are redacted.")

    # LEVEL 6: risk
    probe(report, token, "6", "Seller claims",
          "/post-purchase/v1/claims/search?" + urlencode({
              "players.user_id": user_id,
              "players.role": "respondent",
              "limit": 30, "offset": 0
          }),
          "Scoped to this seller.")

    # LEVEL 7: known restricted marketplace search
    probe(report, token, "7", "Marketplace public search",
          "/sites/MLC/search?" + urlencode({"q": "aspiradora", "limit": 5}),
          "Capability check; previously returned 403.")

    success = sum(x["ok"] for x in report)
    failed = len(report) - success
    all_keys = sorted({k for x in report for k in x.get("keys", [])})

    lines = [
        "MERCADO LIBRE API CAPABILITY DISCOVERY",
        "=" * 72,
        f"Generated UTC: {datetime.now(timezone.utc).isoformat()}",
        f"OAuth file: {oauth}",
        f"User ID: {user_id}",
        "",
        "IMPORTANT",
        "-" * 72,
        "Live GET-only capability probe. No write/update operation is executed.",
        "Sensitive fields are redacted before writing this report.",
        "This is not an exhaustive enumeration of every Mercado Libre endpoint.",
        "",
        f"TOTAL PROBES: {len(report)}",
        f"SUCCESS: {success}",
        f"FAILED/RESTRICTED: {failed}",
        ""
    ]

    for level in sorted(set(x["level"] for x in report),
                        key=lambda x: [int(p) for p in x.split(".") if p.isdigit()]):
        lines += ["", f"LEVEL {level}", "=" * 72]
        for x in [r for r in report if r["level"] == level]:
            lines += [
                "", f"NAME: {x['name']}", f"PATH: {x['path']}",
                f"HTTP: {x['http']}", f"OK: {x['ok']}"
            ]
            if x["note"]:
                lines.append(f"NOTE: {x['note']}")
            if x["ok"]:
                lines.append("RESPONSE KEYS:")
                lines += [f"  - {k}" for k in x["keys"]]
                lines.append("SAMPLE:")
                lines += json.dumps(x["sample"], ensure_ascii=False,
                                    indent=2).splitlines()
            else:
                lines.append("ERROR:")
                lines += json.dumps(x["error"], ensure_ascii=False,
                                    indent=2).splitlines()

    lines += ["", "=" * 72, "UNIQUE RESPONSE VARIABLES", "=" * 72]
    lines += [f"- {k}" for k in all_keys]

    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    output = desktop / (
        "mercadolibre_api_discovery_"
        + datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
    )
    output.write_text("\n".join(lines), encoding="utf-8")

    print(f"DISCOVERY COMPLETED: {output}")
    print(f"PROBES: {len(report)} | SUCCESS: {success} | FAILED/RESTRICTED: {failed}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
