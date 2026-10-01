#!/usr/bin/env python3
"""
scrape.py — scrape a site and (optionally) upload the result to the client's
Google Drive.

    # scrape only (writes ./scraped_data, same as scrape_sitemap.py)
    python scrape.py --url https://www.example.com/sitemap.xml

    # scrape and upload to the client's Drive, resolved from Notion by slug
    python scrape.py --client sovrle --url https://www.sovrledental.com/sitemap.xml --upload-drive

With --upload-drive the crawl runs into a local temp directory and the completed
tree is uploaded at the end, into:

    <client's Google Drive>/Site Scrape YYYY-MM/
        pages/  jsonld/  images/

Uploading only happens after a clean crawl, so a mid-crawl crash fails without
leaving a half-populated Drive folder. Configuration defaults come from .env;
CLI flags override them. See README.md for setup.
"""

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime

from dotenv import load_dotenv


def _default_folder_name():
    return "Site Scrape " + datetime.now().strftime("%Y-%m")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape a site's sitemap and optionally upload to the client's Google Drive.",
    )
    p.add_argument("-c", "--client", help="Client slug (see clients.yaml). Required with --upload-drive.")
    p.add_argument("-u", "--url", help="Sitemap URL (default: SITEMAP_URL from .env)")
    p.add_argument("--upload-drive", action="store_true", help="Upload the completed scrape to the client's Google Drive")
    p.add_argument("-o", "--output-dir", help="Local output dir when not uploading (default: OUTPUT_DIR or ./scraped_data)")
    p.add_argument("--limit", type=int, help="Max number of pages to scrape")
    p.add_argument("--delay", type=float, help="Delay between requests in seconds (default: 1.0)")
    p.add_argument("--no-images", action="store_true", help="Do not download images")
    p.add_argument("--no-homepage", action="store_true", help="Do not force-add the homepage")
    p.add_argument("--drive-folder-name", help=f'Drive folder name for this upload (default: "{_default_folder_name()}")')
    p.add_argument("--keep-local", action="store_true", help="With --upload-drive, keep the local temp copy instead of deleting it")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    load_dotenv()

    sitemap_url = args.url or os.getenv("SITEMAP_URL")
    if not sitemap_url:
        print("Error: no sitemap URL. Pass --url or set SITEMAP_URL in .env.", file=sys.stderr)
        return 1

    # Env-backed defaults for the crawl.
    download_images = not args.no_images and os.getenv("DOWNLOAD_IMAGES", "true").lower() == "true"
    include_homepage = not args.no_homepage and os.getenv("INCLUDE_HOMEPAGE", "true").lower() == "true"
    delay = args.delay if args.delay is not None else float(os.getenv("REQUEST_DELAY", "1.0"))
    page_limit = os.getenv("PAGE_LIMIT")
    limit = args.limit if args.limit is not None else (int(page_limit) if page_limit else None)

    # Import the crawl here so plain scraping doesn't require the Drive/Notion deps.
    from scrape_sitemap import scrape_site

    if not args.upload_drive:
        output_dir = args.output_dir or os.getenv("OUTPUT_DIR", "./scraped_data")
        print("=" * 60)
        print("Website Scraper")
        print("=" * 60)
        try:
            summary = scrape_site(
                sitemap_url, output_dir,
                download_images=download_images, include_homepage=include_homepage,
                delay=delay, limit=limit,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        _print_summary(summary)
        return 0

    # ---- Upload path -------------------------------------------------------
    if not args.client:
        print("Error: --client is required with --upload-drive.", file=sys.stderr)
        return 1

    # Resolve everything that can fail *before* spending minutes crawling.
    import notion
    import drive

    key_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    try:
        service = drive.build_service(key_path)  # validates key exists & is loadable
    except drive.DriveError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        drive_id = notion.resolve_drive_folder_id(args.client)
    except notion.NotionLookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    folder_name = args.drive_folder_name or _default_folder_name()
    print("=" * 60)
    print(f"Website Scraper  |  client={args.client}  drive={drive_id}")
    print("=" * 60)

    tmp_dir = tempfile.mkdtemp(prefix=f"scrape-{args.client}-")
    try:
        try:
            summary = scrape_site(
                sitemap_url, tmp_dir,
                download_images=download_images, include_homepage=include_homepage,
                delay=delay, limit=limit,
            )
        except Exception as e:
            print(f"Error during crawl (nothing uploaded): {e}", file=sys.stderr)
            return 1

        _print_summary(summary)

        print(f"\nUploading to Drive folder '{folder_name}' ...")
        try:
            result = drive.upload_scrape(service, tmp_dir, drive_id, folder_name)
        except Exception as e:
            print(f"Error during upload: {e}", file=sys.stderr)
            if not args.keep_local:
                print(f"Local copy kept for retry: {tmp_dir}", file=sys.stderr)
                args.keep_local = True  # don't delete on failure
            return 1

        s = result["stats"]
        print("\n" + "=" * 60)
        print("UPLOAD COMPLETE")
        print("=" * 60)
        print(f"Uploaded {s['files']} files in {s['folders']} folders "
              f"({drive._human_size(s['bytes'])}).")
        print(f"Drive folder: {result['link']}")
        return 0
    finally:
        if args.keep_local:
            print(f"Local copy: {tmp_dir}")
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _print_summary(summary):
    print("\n" + "=" * 60)
    print("SCRAPING COMPLETE")
    print("=" * 60)
    print(f"Total pages: {summary['total']}")
    print(f"Successful: {summary['success']}")
    print(f"Failed: {summary['failed']}")
    print(f"Output: {summary['output_dir']}")


if __name__ == "__main__":
    sys.exit(main())
