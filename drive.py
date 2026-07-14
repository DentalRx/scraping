#!/usr/bin/env python3
"""
drive.py — upload a local directory tree to Google Drive using a service account.

The client "Google Drive ID" values in the Notion Clients DB are Google *shared
drive* (Team Drive) roots, not ordinary My-Drive folders. Every Drive API call
here therefore passes supportsAllDrives=True, and the service account must be a
member (Content manager or higher) of each client's shared drive. See README.md
→ "Google Drive upload setup".

Typical use is via scrape.py --upload-drive, which calls upload_scrape(). The
module is also runnable for manual re-uploads / debugging:

    python drive.py --key service-account.json --parent <drive_id> \
        --name "Site Scrape 2026-07" ./some_local_dir
"""

import argparse
import mimetypes
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

# macOS / editor junk we never want to push into a client's Drive.
IGNORE_NAMES = {".DS_Store", "Thumbs.db", ".localized"}


class DriveError(Exception):
    """Raised for auth/config problems with an actionable message."""


def _should_skip(name):
    if name in IGNORE_NAMES:
        return True
    if name.startswith("._"):  # AppleDouble resource forks
        return True
    if name.startswith("Icon"):  # macOS custom-icon marker ("Icon\r")
        return True
    return False


def build_service(key_path):
    """Build an authenticated Drive v3 service from a service-account key file."""
    if not key_path or not os.path.exists(key_path):
        raise DriveError(
            f"Service-account key not found: {key_path!r}. Set "
            "GOOGLE_SERVICE_ACCOUNT_FILE in .env or pass --key."
        )
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on install
        raise DriveError(
            "Google client libraries are missing. Install requirements: "
            "pip install -r requirements.txt"
        ) from exc

    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    # cache_discovery=False avoids a noisy warning and a filesystem cache write.
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def create_folder(service, name, parent_id):
    """Create a folder named `name` under `parent_id`; return the new folder id."""
    metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    folder = (
        service.files()
        .create(body=metadata, fields="id, webViewLink", supportsAllDrives=True)
        .execute()
    )
    return folder["id"]


def upload_file(service, path, parent_id):
    """Upload a single file into `parent_id`; return the new file id."""
    from googleapiclient.http import MediaFileUpload

    mime, _ = mimetypes.guess_type(path)
    media = MediaFileUpload(path, mimetype=mime, resumable=True)
    metadata = {"name": os.path.basename(path), "parents": [parent_id]}
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id", supportsAllDrives=True)
        .execute()
    )
    return created["id"]


def upload_tree(service, local_dir, parent_id, log=print, _stats=None):
    """Recursively mirror the contents of `local_dir` into Drive folder `parent_id`.

    Returns a stats dict: {"folders": int, "files": int, "bytes": int}.
    Directories are created before their contents; entries are processed in
    sorted order for stable, predictable output.
    """
    stats = _stats if _stats is not None else {"folders": 0, "files": 0, "bytes": 0}

    for name in sorted(os.listdir(local_dir)):
        if _should_skip(name):
            continue
        full = os.path.join(local_dir, name)
        if os.path.isdir(full):
            child_id = create_folder(service, name, parent_id)
            stats["folders"] += 1
            log(f"  + {name}/")
            upload_tree(service, full, child_id, log=log, _stats=stats)
        elif os.path.isfile(full):
            size = os.path.getsize(full)
            upload_file(service, full, parent_id)
            stats["files"] += 1
            stats["bytes"] += size
            log(f"    - {name} ({_human_size(size)})")

    return stats


def upload_scrape(service, local_dir, drive_parent_id, folder_name, log=print):
    """Create `folder_name` under `drive_parent_id` and upload `local_dir` into it.

    `service` may be a built Drive service or a path to a service-account key.
    Returns {"folder_id", "link", "stats"}.
    """
    if isinstance(service, str):
        service = build_service(service)

    if not os.path.isdir(local_dir):
        raise DriveError(f"Local directory to upload does not exist: {local_dir}")

    log(f"Creating Drive folder: {folder_name}")
    root_id = create_folder(service, folder_name, drive_parent_id)
    link = f"https://drive.google.com/drive/folders/{root_id}"

    stats = upload_tree(service, local_dir, root_id, log=log)
    stats["folders"] += 1  # count the top-level folder we just created
    return {"folder_id": root_id, "link": link, "stats": stats}


def _human_size(num):
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Upload a local tree to a client's Google (shared) Drive.")
    parser.add_argument("local_dir", help="Local directory to upload")
    parser.add_argument("--key", help="Service-account JSON key (or GOOGLE_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--parent", required=True, help="Destination Drive id (shared-drive root or folder)")
    parser.add_argument("--name", required=True, help="Name of the folder to create for this upload")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    key = args.key or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    try:
        result = upload_scrape(key, args.local_dir, args.parent, args.name)
    except DriveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    s = result["stats"]
    print(f"\nUploaded {s['files']} files in {s['folders']} folders ({_human_size(s['bytes'])}).")
    print(f"Drive folder: {result['link']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
