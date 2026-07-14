#!/usr/bin/env python3
"""
notion.py — resolve a human-readable client slug to that client's Google Drive
folder ID, using the Notion "Clients" database as the source of truth.

Resolution has two steps:

  1. slug  -> Notion page id   (via clients.yaml, a small slug->page-id map)
  2. page  -> Google Drive id  (read the "Google Drive ID" property off the page)

Keeping the Drive ID on the Notion page (rather than in the repo) means it stays
a single source of truth: onboarding fills it in once on the client row and this
script always reads the current value.

Standalone usage (handy for debugging the lookup):

    python notion.py sovrle
    python notion.py --list

Requires NOTION_TOKEN in the environment (or --token), and a Notion internal
integration that has been shared with the Clients database.
"""

import argparse
import os
import re
import sys

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Name of the Notion page property that stores the Drive ID. Overridable via env
# in case the database property is ever renamed.
DEFAULT_DRIVE_ID_PROPERTY = "Google Drive ID"
DEFAULT_CLIENTS_YAML = "clients.yaml"


class NotionLookupError(Exception):
    """Base class for slug-resolution failures (raised with a human message)."""


class ClientNotFoundError(NotionLookupError):
    pass


class DriveIdNotSetError(NotionLookupError):
    pass


def load_client_map(path=None):
    """Load the slug -> {page_id, name} map from clients.yaml.

    Returns a dict keyed by slug. Raises NotionLookupError with an actionable
    message if the file or PyYAML is missing, or the file is malformed.
    """
    path = path or os.getenv("CLIENTS_YAML") or DEFAULT_CLIENTS_YAML

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on install
        raise NotionLookupError(
            "PyYAML is required to read clients.yaml. Install requirements: "
            "pip install -r requirements.txt"
        ) from exc

    if not os.path.exists(path):
        raise NotionLookupError(
            f"Client map not found: {path}\n"
            "Create it (slug -> Notion page id) or set CLIENTS_YAML. "
            "See clients.yaml in the repo for the format."
        )

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    clients = raw.get("clients", raw)
    if not isinstance(clients, dict):
        raise NotionLookupError(
            f"{path} must map slugs to entries (a 'clients:' mapping)."
        )

    normalized = {}
    for slug, entry in clients.items():
        if isinstance(entry, str):
            entry = {"page_id": entry}
        elif not isinstance(entry, dict):
            raise NotionLookupError(f"{path}: entry for '{slug}' must be a page id or a mapping.")
        normalized[str(slug).strip().lower()] = entry
    return normalized


def parse_drive_id(value):
    """Extract a Drive id from a raw id or a Drive URL.

    Accepts the bare id already stored in Notion (e.g. '0AMO7AupC4iwlUk9PVA')
    or any of the common Drive URL shapes, and returns just the id.
    """
    if not value:
        return ""
    value = value.strip()

    # https://drive.google.com/drive/folders/<id>[?...]   (also /u/0/folders/<id>)
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    # https://drive.google.com/open?id=<id>  or  ...?id=<id>&...
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    # Otherwise assume it is already a bare id.
    return value


def _property_to_text(prop):
    """Flatten a Notion property value to a plain string.

    Handles the property types the Drive id might reasonably live in:
    rich_text (the current schema), url, and title.
    """
    if not isinstance(prop, dict):
        return ""
    ptype = prop.get("type")
    if ptype == "rich_text":
        return "".join(part.get("plain_text", "") for part in prop.get("rich_text", []))
    if ptype == "title":
        return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    if ptype == "url":
        return prop.get("url") or ""
    return ""


def fetch_page(page_id, token):
    """GET a Notion page by id. Raises NotionLookupError on HTTP/auth errors."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }
    resp = requests.get(f"{NOTION_API}/pages/{page_id}", headers=headers, timeout=30)
    if resp.status_code == 401:
        raise NotionLookupError(
            "Notion returned 401 Unauthorized. Check NOTION_TOKEN."
        )
    if resp.status_code == 404:
        raise NotionLookupError(
            f"Notion page {page_id} not found (404). Confirm the page id in "
            "clients.yaml and that the integration is shared with the Clients database."
        )
    resp.raise_for_status()
    return resp.json()


def get_page_drive_id(page_id, token, property_name=None):
    """Read and normalize the Drive id property off a Notion client page."""
    property_name = property_name or os.getenv("NOTION_DRIVE_ID_PROPERTY") or DEFAULT_DRIVE_ID_PROPERTY
    page = fetch_page(page_id, token)
    props = page.get("properties", {})
    if property_name not in props:
        raise NotionLookupError(
            f"Property '{property_name}' not found on Notion page {page_id}. "
            f"Available properties: {', '.join(sorted(props)) or '(none)'}"
        )
    raw = _property_to_text(props[property_name]).strip()
    drive_id = parse_drive_id(raw)
    return drive_id


def resolve_drive_folder_id(slug, token=None, clients_yaml=None, property_name=None):
    """slug -> Google Drive folder/shared-drive id, via the Notion Clients DB.

    Raises a NotionLookupError subclass with an actionable message on any failure.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        raise NotionLookupError("No client slug provided.")

    token = token or os.getenv("NOTION_TOKEN")
    if not token:
        raise NotionLookupError(
            "NOTION_TOKEN is not set. Add it to .env or pass --token. "
            "Create an internal integration at https://www.notion.so/my-integrations "
            "and share the Clients database with it."
        )

    clients = load_client_map(clients_yaml)
    if slug not in clients:
        known = ", ".join(sorted(clients)) or "(none)"
        raise ClientNotFoundError(
            f"Unknown client slug '{slug}'. Known slugs: {known}"
        )

    entry = clients[slug]
    page_id = str(entry.get("page_id", "")).strip()
    if not page_id:
        raise NotionLookupError(f"clients.yaml entry for '{slug}' has no page_id.")

    drive_id = get_page_drive_id(page_id, token, property_name)
    if not drive_id:
        name = entry.get("name", slug)
        raise DriveIdNotSetError(
            f"Client '{name}' ({slug}) has no Drive ID set on its Notion page. "
            f"Fill in the '{property_name or DEFAULT_DRIVE_ID_PROPERTY}' property on that row."
        )
    return drive_id


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Resolve a client slug to a Google Drive ID via Notion.")
    parser.add_argument("slug", nargs="?", help="Client slug (see clients.yaml)")
    parser.add_argument("--list", action="store_true", help="List known client slugs and exit")
    parser.add_argument("--token", help="Notion token (overrides NOTION_TOKEN)")
    parser.add_argument("--clients-yaml", help="Path to clients.yaml")
    args = parser.parse_args(argv)

    # Load .env for standalone use so NOTION_TOKEN etc. are available.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        if args.list:
            clients = load_client_map(args.clients_yaml)
            for slug in sorted(clients):
                name = clients[slug].get("name", "")
                print(f"{slug:24} {name}")
            return 0

        if not args.slug:
            parser.error("provide a client slug, or use --list")

        drive_id = resolve_drive_folder_id(
            args.slug, token=args.token, clients_yaml=args.clients_yaml
        )
        print(drive_id)
        return 0
    except NotionLookupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
