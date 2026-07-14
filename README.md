# scraping

Small CLI tools for pulling a site's content (JSON-LD, page text, images) into a
clean, browsable tree — used as a **migration reference** during client
onboarding. Run manually as a step on the onboarding checklist.

```
pages/   <slug>.txt    one text file per page (title, meta, headings, copy)
jsonld/  <slug>.json   JSON-LD for the pages that have it
images/  <hash>.<ext>  de-duplicated images, plus _manifest.json
urls_scraped.txt       the list of URLs crawled
```

## Install

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env
```

`.env` is gitignored — it holds the Notion token and the path to the Google
service-account key. Never commit it (or the key JSON).

## Scrape only

```bash
# uses SITEMAP_URL from .env, writes ./scraped_data
python scrape.py

# or pass a sitemap explicitly
python scrape.py --url https://www.example.com/sitemap.xml --output-dir ./scraped_data
```

`scrape_sitemap.py` (whole-site) and `scrape_blog.py` (blog → Markdown) still
work standalone from `.env` as before.

## Scrape and upload to the client's Google Drive

```bash
python scrape.py --client sovrle --url https://www.sovrledental.com/sitemap.xml --upload-drive
```

This resolves the client slug to their Google Drive via Notion, crawls into a
**local temp directory**, and uploads the completed tree only after a clean
crawl — a mid-crawl crash fails without leaving a half-populated Drive folder.
Result in Drive:

```
<client's Google Drive>/
  Site Scrape 2026-07/       # date-stamped so a re-scrape won't clobber the last one
    pages/  jsonld/  images/
```

Useful flags: `--drive-folder-name "..."` (override the folder name),
`--keep-local` (keep the temp copy), `--limit N`, `--no-images`.

### How the slug → Drive lookup works

1. `clients.yaml` maps a slug (e.g. `sovrle`) to that client's **Notion page id**.
2. `notion.py` reads the **`Google Drive ID`** property off that Notion page.
3. `drive.py` uploads into that Drive.

The Drive ID lives on the Notion Clients row (single source of truth) — all 8
current clients already have it filled in. List known slugs with
`python notion.py --list`; debug one with `python notion.py sovrle`.

**Onboarding a new client:** create their Clients row in Notion with the
`Google Drive ID` filled in, then add a `slug: {name, page_id}` entry to
`clients.yaml`.

## Google Drive upload setup (one-time)

> **Important:** each client's `Google Drive ID` in Notion is a Google **shared
> drive** (Team Drive) root, *not* a normal folder. The service account must be a
> member of each shared drive, and the tooling passes `supportsAllDrives=True`
> throughout.

1. **Service account + key.** In Google Cloud, create a service account, enable
   the **Google Drive API**, create a JSON key, and point
   `GOOGLE_SERVICE_ACCOUNT_FILE` at it in `.env`. (No OAuth consent screen — this
   is a headless CLI.)
2. **Grant the service account access to each client's shared drive.** Open the
   shared drive → Manage members → add the service account's email
   (`...@...iam.gserviceaccount.com`) as **Content manager** (or higher). Do this
   for every client you intend to upload for.
3. **Notion integration.** Create an internal integration at
   <https://www.notion.so/my-integrations>, copy its token into `NOTION_TOKEN`,
   and **share the Clients database** with the integration (database → ••• →
   Connections → your integration).

## Configuration reference

See `.env.example` for all keys. The upload path uses:

| Key | Purpose |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the service-account JSON key |
| `NOTION_TOKEN` | Notion internal-integration token (secret) |
| `CLIENTS_YAML` | slug → Notion page-id map (default `./clients.yaml`) |
| `NOTION_DRIVE_ID_PROPERTY` | Drive-id property name (default `Google Drive ID`) |

CLI flags override `.env` values.
