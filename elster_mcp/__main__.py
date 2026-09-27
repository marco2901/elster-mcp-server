"""Kommandozeile.

    python -m elster_mcp                        # MCP-Server über stdio (Claude Desktop/Code)
    python -m elster_mcp serve --transport http # Streamable HTTP mit Bearer-Token
    python -m elster_mcp check                  # Sicherheitsmerkmale lokal prüfen
    python -m elster_mcp store-secret cert-password   # PIN/Passwort in den OS-Keyring
    python -m elster_mcp gen-token              # zufälliges HTTP-Token erzeugen
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys

from .secrets import SecretError, keyring_set, setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="elster-mcp", description="ELSTER MCP Server")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="MCP-Server starten (Standard)")
    serve.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("ELSTER_MCP_TRANSPORT", "stdio"))

    sub.add_parser("check", help="Konfiguration und Sicherheitsmerkmale prüfen (ohne Login)")

    store = sub.add_parser("store-secret", help="Geheimnis im OS-Keyring ablegen")
    store.add_argument("key", choices=["cert-password", "http-token"])

    sub.add_parser("gen-token", help="Zufälliges Token für den HTTP-Transport erzeugen")

    args = parser.parse_args(argv)
    setup_logging(os.environ.get("ELSTER_LOG_LEVEL", "INFO"))

    if args.cmd == "gen-token":
        print(secrets.token_urlsafe(48))
        return 0

    if args.cmd == "store-secret":
        value = getpass.getpass(f"{args.key}: ")
        if not value:
            print("Abgebrochen – leerer Wert.", file=sys.stderr)
            return 1
        try:
            keyring_set(args.key, value)
        except SecretError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"'{args.key}' im OS-Keyring gespeichert (Service 'elster-mcp').", file=sys.stderr)
        return 0

    if args.cmd == "check":
        from .server import elster_security_check

        result = elster_security_check()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 2

    from .server import run

    run("http" if getattr(args, "transport", "stdio") == "http" else "stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
