"""Network-free MCP stdio server used by the platform acceptance fixture."""

from __future__ import annotations

import json
import sys


def _reply(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":request_id,"result":result},separators=(",",":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    for raw in sys.stdin:
        request=json.loads(raw)
        if "id" not in request:
            continue
        method=request.get("method")
        if method=="initialize":
            _reply(request["id"],{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"odys-fake","version":"1"}})
        elif method=="tools/list":
            _reply(request["id"],{"tools":[{"name":"echo","description":"Return bounded offline evidence","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]})
        elif method=="tools/call":
            text=str(request.get("params",{}).get("arguments",{}).get("text",""))[:1000]
            _reply(request["id"],{"content":[{"type":"text","text":text}],"isError":False})
        else:
            sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32601,"message":"method not found"}}) + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
