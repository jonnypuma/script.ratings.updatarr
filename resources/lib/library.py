"""Video library reads and writes over an injected JSON-RPC function."""

from __future__ import annotations

import json

from resources.lib.media import SPECS, details_params, item_from_json


PAGE_SIZE = 200


class JsonRpcError(Exception):
    """Kodi rejected a JSON-RPC call."""


def parse_rpc_payload(raw: str) -> dict:
    if not raw:
        raise JsonRpcError("Empty response from Kodi")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonRpcError("Kodi returned invalid JSON") from exc
    error = parsed.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise JsonRpcError(message or "JSON-RPC error")
    result = parsed.get("result")
    return result if isinstance(result, dict) else {}


class LibraryClient:
    def __init__(self, rpc) -> None:
        self.rpc = rpc

    def list_items(self, mediatype: str):
        spec = SPECS[mediatype]
        start = 0
        items = []
        while True:
            result = self.rpc(
                spec.list_method,
                {
                    "properties": list(spec.properties),
                    "limits": {"start": start, "end": start + PAGE_SIZE},
                },
            )
            batch = result.get(spec.list_key) or []
            items.extend(item_from_json(mediatype, row) for row in batch if isinstance(row, dict))
            total = (result.get("limits") or {}).get("total")
            start += len(batch)
            if not batch:
                break
            if total is None:
                if len(batch) < PAGE_SIZE:
                    break
                continue
            if start >= int(total):
                break
        return items

    def get_item(self, mediatype: str, dbid: int):
        spec = SPECS[mediatype]
        result = self.rpc(
            spec.details_method,
            {spec.id_field: int(dbid), "properties": list(spec.properties)},
        )
        payload = result.get(spec.details_key)
        if not isinstance(payload, dict):
            return None
        return item_from_json(mediatype, payload)

    def apply(self, item, ratings: dict, uniqueids: dict) -> None:
        self.rpc(SPECS[item.mediatype].set_method, details_params(item, ratings, uniqueids))
