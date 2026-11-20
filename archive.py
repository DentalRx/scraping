#!/usr/bin/env python3
"""
archive.py — make a complete "before" archive of a client's old website ahead of
a rebuild, and (optionally) upload it to the client's Google Drive.

Where scrape.py is a quick static crawl, archive.py renders every page in a real
browser (Playwright / Chromium), so it also captures what JavaScript draws:

    Old Website Archive - YYYY-MM-DD (domain)/
        README.md                        index of every page, counts, issues found
        pages.csv                        the same index as a spreadsheet
        01 Screenshots/
            Desktop (1440px)/  desktop-01-home.png, desktop-02-services.png, ...
            Mobile (360px)/    mobile-01-home.png,  mobile-02-services.png,  ...
        02 Sitemaps/           robots.txt, every sitemap XML as served, urls.txt
        03 Content (Markdown)/ 01-home.md ... (front matter: url, title, meta, h1)
                               _site-navigation.md (header / nav / footer, once)
        04 Structured Data (JSON-LD)/
                               01-home.json ... , _all-jsonld.json
        05 Images/             every image used on the site (img, srcset,
                               <picture>, CSS backgrounds, og:image, icons)
                               _manifest.json (filename -> url, alt, pages)

The same NN number is used for a page in every folder, so desktop-04-*.png,
mobile-04-*.png, 04-*.md and 04-*.json are all the same page.

Pages come from the sitemap(s) (found via robots.txt or the usual paths) plus
any same-site links found while crawling (menus, footer, legal pages), which are
flagged in README.md as "not in sitemap".

Usage:

    pip install -r requirements.txt && python -m playwright install chromium

    # archive to ./site_archive/<folder>
    python archive.py --site https://www.example.com

    # archive and upload to the client's shared drive (slug -> Notion -> Drive,
    # exactly as scrape.py does), or straight to a Drive folder / shared drive id
    python archive.py --site https://www.example.com --client sovrle --upload-drive
    python archive.py --site https://www.example.com --drive-parent <id> --upload-drive

    # keep every file under ~3 MB (for uploading with a tool that has a size cap):
    # oversized full-page PNGs become JPEGs, or are split into parts
    python archive.py --site https://www.example.com --max-file-mb 3
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from scrape_sitemap import DEFAULT_HEADERS

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 360, "height": 800}
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

DIR_SHOTS = "01 Screenshots"
DIR_DESKTOP = os.path.join(DIR_SHOTS, "Desktop (1440px)")
DIR_MOBILE = os.path.join(DIR_SHOTS, "Mobile (360px)")
DIR_SITEMAPS = "02 Sitemaps"
DIR_MD = "03 Content (Markdown)"
DIR_JSONLD = "04 Structured Data (JSON-LD)"
DIR_IMAGES = "05 Images"

SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|ico|avif|bmp|tiff?|mp4|mov|webm|mp3|wav|zip|docx?|xlsx?|pptx?|csv|xml|txt|json|css|js|woff2?|ttf|eot)$", re.I)
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
             "image/svg+xml": ".svg", "image/avif": ".avif", "image/x-icon": ".ico",
             "image/vnd.microsoft.icon": ".ico", "image/bmp": ".bmp", "image/tiff": ".tif"}

# Runs in the page: everything we need from the rendered DOM in one round trip.
EXTRACT_JS = r"""
() => {
  const abs = u => { try { return new URL(u, document.baseURI).href } catch (e) { return null } };
  const imgs = [];
  const add = (u, alt) => { const a = u && abs(u.trim()); if (a && !a.startsWith('data:')) imgs.push([a, alt || '']) };
  const srcset = (s, alt) => (s || '').split(',').forEach(p => add(p.trim().split(/\s+/)[0], alt));
  document.querySelectorAll('img').forEach(i => {
    const alt = i.getAttribute('alt') || '';
    add(i.currentSrc, alt); add(i.getAttribute('src'), alt);
    ['data-src', 'data-lazy-src', 'data-original', 'data-bg'].forEach(k => add(i.getAttribute(k), alt));
    srcset(i.getAttribute('srcset'), alt); srcset(i.getAttribute('data-srcset'), alt);
  });
  document.querySelectorAll('source[srcset], source[data-srcset]').forEach(s => {
    srcset(s.getAttribute('srcset')); srcset(s.getAttribute('data-srcset')) });
  document.querySelectorAll('*').forEach(el => {
    for (const pseudo of [null, '::before', '::after']) {
      const bg = getComputedStyle(el, pseudo).backgroundImage;
      if (bg && bg !== 'none') for (const m of bg.matchAll(/url\(["']?(.*?)["']?\)/g)) add(m[1]);
    }
  });
  document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]').forEach(m => add(m.content));
  document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"]').forEach(l => add(l.href));
  const jsonld = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent);
  const links = [...document.querySelectorAll('a[href]')].map(a => a.href);
  return { imgs, jsonld, links };
}
"""


# --------------------------------------------------------------------------- helpers

def norm_url(u):
    """Normalise a page URL for de-duplication (no fragment/query, no trailing slash)."""
    p = urlparse(u)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))


def same_site(u, host):
    h = urlparse(u).netloc.lower()
    return h == host or h == "www." + host or "www." + h == host


def page_slug(url):
    path = urlparse(url).path.strip("/")
    if not path:
        return "home"
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    if len(slug) > 80:
        slug = slug[:72].rstrip("-") + "-" + hashlib.md5(url.encode()).hexdigest()[:6]
    return slug or "page"


def get(url, **kw):
    return requests.get(url, headers=DEFAULT_HEADERS, timeout=30, **kw)


def ld_types(node, out):
    """Collect every @type in a JSON-LD structure (including @graph)."""
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.add(t)
        elif isinstance(t, list):
            out.update(x for x in t if isinstance(x, str))
        for v in node.values():
            ld_types(v, out)
    elif isinstance(node, list):
        for v in node:
            ld_types(v, out)
    return out


# --------------------------------------------------------------------------- sitemaps

def discover_sitemaps(site, sitemap_dir, explicit=None, log=print):
    """Save robots.txt and every sitemap (recursing into indexes) as served.

    Returns (page_urls_in_sitemap_order, saved_sitemap_urls).
    """
    os.makedirs(sitemap_dir, exist_ok=True)
    candidates = [explicit] if explicit else []
    try:
        r = get(urljoin(site, "/robots.txt"))
        if r.ok and "text" in r.headers.get("content-type", "text"):
            with open(os.path.join(sitemap_dir, "robots.txt"), "w", encoding="utf-8") as f:
                f.write(r.text)
            candidates += [l.split(":", 1)[1].strip() for l in r.text.splitlines()
                           if l.lower().startswith("sitemap:")]
    except requests.RequestException as e:
        log(f"  robots.txt: {e}")
    pages, saved, seen, used_names = [], [], set(), set()

    def fetch(sm_url, depth=0):
        if sm_url in seen or depth > 4:
            return
        seen.add(sm_url)
        try:
            r = get(sm_url)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            log(f"  sitemap {sm_url}: {e}")
            return
        name = re.sub(r"[^\w.-]+", "_", urlparse(sm_url).path.strip("/")) or "sitemap.xml"
        if not name.endswith(".xml"):
            name += ".xml"
        while name in used_names:
            name = name[:-4] + "_" + hashlib.md5(sm_url.encode()).hexdigest()[:4] + ".xml"
        used_names.add(name)
        with open(os.path.join(sitemap_dir, name), "wb") as f:
            f.write(r.content)
        saved.append(sm_url)
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        children = root.findall(f".//{ns}sitemap/{ns}loc")
        if children:
            for loc in children:
                if loc.text:
                    fetch(loc.text.strip(), depth + 1)
        else:
            pages.extend(loc.text.strip() for loc in root.findall(f".//{ns}url/{ns}loc") if loc.text)

    defaults = [urljoin(site, p) for p in
                ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml", "/sitemap-index.xml")]
    for c in candidates:
        fetch(c)
    if not saved and not explicit:
        for c in defaults:
            fetch(c)
            if saved:
                break
    log(f"Sitemaps saved: {len(saved)}  |  URLs listed: {len(pages)}")
    return pages, saved


# --------------------------------------------------------------------------- markdown

def to_markdown(rendered_html, url, status, nav_out=None):
    from markdownify import markdownify

    soup = BeautifulSoup(rendered_html, "html.parser")
    meta = lambda **a: (soup.find("meta", attrs=a) or {}).get("content", "") if soup.find("meta", attrs=a) else ""
    canonical = soup.find("link", rel="canonical")
    front = {
        "url": url,
        "status": status,
        "title": (soup.title.get_text(strip=True) if soup.title else ""),
        "meta_description": meta(name="description"),
        "canonical": canonical.get("href", "") if canonical else "",
        "robots": meta(name="robots"),
        "og_title": meta(property="og:title"),
        "og_description": meta(property="og:description"),
        "og_image": meta(property="og:image"),
        "h1": [h.get_text(" ", strip=True) for h in soup.find_all("h1")],
    }
    for t in soup(["script", "style", "noscript", "template", "svg", "iframe", "link", "meta"]):
        t.decompose()
    # Make links and images absolute (and un-lazy) so the markdown stands alone.
    for a in soup.find_all("a", href=True):
        a["href"] = urljoin(url, a["href"])
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-lazy-src") or ""
        if src and not src.startswith("data:"):
            im["src"] = urljoin(url, src)
    chrome = [c for c in soup.find_all(["header", "nav", "footer"])
              if not c.find_parent(["header", "nav", "footer"])]
    if nav_out is not None and chrome:
        nav_out.append("\n\n---\n\n".join(
            markdownify(str(c), heading_style="ATX").strip() for c in chrome))
    for c in chrome:
        c.decompose()
    body = soup.find("main") or soup.body or soup
    md = markdownify(str(body), heading_style="ATX", bullets="-")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    fm = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items())
    return f"---\n{fm}\n---\n\n{md}\n", front


# --------------------------------------------------------------------------- browser pass

def autoscroll(page):
    """Scroll to the bottom in steps so lazy-loaded images and sections render."""
    try:
        page.evaluate("""async () => {
            const step = Math.max(400, innerHeight * 0.8);
            for (let y = 0; y < document.body.scrollHeight && y < 60000; y += step) {
                scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
            scrollTo(0, 0); }""")
        page.wait_for_timeout(600)
    except Exception:
        pass


def open_page(page, url):
    resp = page.goto(url, wait_until="load", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    autoscroll(page)
    return resp


SOFT_404 = re.compile(r"page not found|\b404\b|not be found", re.I)


def full_shot(page, path, width):
    """Full-page screenshot clipped to the viewport width.

    An element that overflows to the right makes Playwright capture a wider image with a blank
    band; visitors don't see that band, so it's cut. Returns the page's real scroll width.
    """
    w, h = page.evaluate("[document.documentElement.scrollWidth,"
                         " Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)]")
    page.screenshot(path=path, full_page=True, clip={"x": 0, "y": 0, "width": width, "height": max(h, 1)})
    return w


def launch(pw):
    kw = {}
    for exe in (os.getenv("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"):
        if exe and os.path.exists(exe):
            kw["executable_path"] = exe
            break
    try:
        return pw.chromium.launch(**kw)
    except Exception:
        return pw.chromium.launch()


def crawl(site, seed_urls, out, *, screenshots=True, discover=True, max_pages=500, delay=0.5, log=print):
    from playwright.sync_api import sync_playwright

    host = urlparse(site).netloc.lower().removeprefix("www.")
    for d in (DIR_DESKTOP, DIR_MOBILE, DIR_MD, DIR_JSONLD):
        os.makedirs(os.path.join(out, d), exist_ok=True)

    sitemap_set = {norm_url(u) for u in seed_urls}
    queue, queued = [], set()

    def enqueue(u, source):
        n = norm_url(u)
        if n in queued or not same_site(n, host) or SKIP_EXT.search(urlparse(n).path):
            return
        if not n.startswith(("http://", "https://")):
            return
        queued.add(n)
        queue.append((n, source))

    enqueue(norm_url(site), "homepage")
    for u in seed_urls:
        enqueue(u, "sitemap")

    pages, images, all_ld, nav_md, used = [], {}, [], [], set()
    redirects, done = [], {}  # done: normalized url -> page number
    with sync_playwright() as pw:
        browser = launch(pw)
        desk = browser.new_context(viewport=DESKTOP, device_scale_factor=1, user_agent=DEFAULT_HEADERS["User-Agent"])
        mob = browser.new_context(viewport=MOBILE, device_scale_factor=1, is_mobile=True, has_touch=True, user_agent=MOBILE_UA)
        dp, mp = desk.new_page(), mob.new_page()

        i = 0
        while i < len(queue) and len(pages) < max_pages:
            url, source = queue[i]
            i += 1
            if url in done:  # already archived as the target of an earlier redirect
                continue
            num = f"{len(pages) + 1:02d}"
            rec = {"num": num, "name": "", "url": url, "source": source,
                   "in_sitemap": url in sitemap_set, "status": None, "final_url": "",
                   "title": "", "jsonld_types": "", "notes": "", "error": ""}
            log(f"[{num}] {url}")
            try:
                resp = open_page(dp, url)
                rec["status"] = resp.status if resp else None
                rec["final_url"] = dp.url
                final = norm_url(dp.url)
                if final != url and final in done:
                    # Redirect to a page already archived: list it, don't archive it twice.
                    redirects.append({"url": url, "to": final, "num": done[final], "source": source,
                                      "in_sitemap": rec["in_sitemap"]})
                    log(f"     redirects to [{done[final]}] {final}, skipped")
                    time.sleep(delay)
                    continue
                queued.add(final)  # don't crawl the redirect target again later
                slug = page_slug(url)
                while slug in used:
                    slug += "-" + hashlib.md5(url.encode()).hexdigest()[:4]
                used.add(slug)
                name = rec["name"] = f"{num}-{slug}"
                done[url] = done[final] = num
                data = dp.evaluate(EXTRACT_JS)
                raw_html = resp.text() if resp else ""
                rendered = dp.content()

                if screenshots:
                    notes = []
                    sw = full_shot(dp, os.path.join(out, DIR_DESKTOP, f"desktop-{name}.png"), DESKTOP["width"])
                    if sw > DESKTOP["width"] + 2:
                        notes.append(f"horizontal overflow on desktop ({sw}px wide)")
                    open_page(mp, url)
                    sw = full_shot(mp, os.path.join(out, DIR_MOBILE, f"mobile-{name}.png"), MOBILE["width"])
                    if sw > MOBILE["width"] + 2:
                        notes.append(f"horizontal overflow on mobile ({sw}px wide)")
                    rec["notes"] = "; ".join(notes)

                md, front = to_markdown(rendered, url, rec["status"], nav_md if not nav_md else None)
                rec["title"] = front["title"]
                if rec["status"] and rec["status"] < 400 and SOFT_404.search(rec["title"] or ""):
                    rec["notes"] = "; ".join(filter(None, ["soft 404 (error page returned with status "
                                                           f"{rec['status']})", rec["notes"]]))
                with open(os.path.join(out, DIR_MD, f"{name}.md"), "w", encoding="utf-8") as f:
                    f.write(md)

                # JSON-LD: as served (raw HTML) and as rendered (JS-injected), de-duplicated.
                blocks = [s.string or "" for s in BeautifulSoup(raw_html, "html.parser")
                          .find_all("script", type="application/ld+json")] + data["jsonld"]
                items, errors, seen = [], [], set()
                for b in blocks:
                    b = b.strip()
                    if not b:
                        continue
                    try:
                        obj = json.loads(b)
                    except json.JSONDecodeError as e:
                        if all(x["raw"] != b[:2000] for x in errors):
                            errors.append({"error": str(e), "raw": b[:2000]})
                        continue
                    key = json.dumps(obj, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        items.append(obj)
                types = sorted(ld_types(items, set()))
                rec["jsonld_types"] = ", ".join(types)
                if items or errors:
                    doc = {"source_url": url, "types": types, "json_ld": items, "parse_errors": errors}
                    with open(os.path.join(out, DIR_JSONLD, f"{name}.json"), "w", encoding="utf-8") as f:
                        json.dump(doc, f, indent=2, ensure_ascii=False)
                    all_ld.append(doc)

                for img_url, alt in data["imgs"]:
                    e = images.setdefault(img_url, {"alt": set(), "pages": set()})
                    if alt:
                        e["alt"].add(alt)
                    e["pages"].add(url)
                if discover:
                    for link in data["links"]:
                        enqueue(link, "link")
            except Exception as e:
                rec["error"] = str(e).splitlines()[0][:300]
                if not rec["name"]:
                    rec["name"] = f"{num}-{page_slug(url)}"
                log(f"     error: {rec['error']}")
            pages.append(rec)
            time.sleep(delay)
        browser.close()

    if nav_md:
        with open(os.path.join(out, DIR_MD, "_site-navigation.md"), "w", encoding="utf-8") as f:
            f.write("# Site header, navigation and footer (from the home page)\n\n" + nav_md[0] + "\n")
    with open(os.path.join(out, DIR_JSONLD, "_all-jsonld.json"), "w", encoding="utf-8") as f:
        json.dump(all_ld, f, indent=2, ensure_ascii=False)
    return pages, images, redirects


# --------------------------------------------------------------------------- images

def download_images(images, img_dir, log=print):
    os.makedirs(img_dir, exist_ok=True)
    manifest, used, failed = {}, set(), []
    for n, (url, info) in enumerate(sorted(images.items()), 1):
        try:
            r = get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith("image/"):
                raise ValueError(f"not an image ({ctype})")
        except Exception as e:
            failed.append({"url": url, "error": str(e)[:200]})
            continue
        base = os.path.basename(urlparse(url).path) or "image"
        stem, ext = os.path.splitext(base)
        stem = re.sub(r"[^\w.-]+", "-", stem)[:80].strip("-.") or "image"
        if ext.lower() not in IMAGE_EXT.values() and ext.lower() not in (".jpeg", ".tif", ".tiff"):
            ext = IMAGE_EXT.get(ctype, ext or ".img")
        fname = stem + ext.lower()
        if fname in used:
            fname = f"{stem}-{hashlib.md5(url.encode()).hexdigest()[:6]}{ext.lower()}"
        used.add(fname)
        with open(os.path.join(img_dir, fname), "wb") as f:
            f.write(r.content)
        manifest[fname] = {"url": url, "alt": sorted(info["alt"]), "pages": sorted(info["pages"]),
                           "bytes": len(r.content), "content_type": ctype}
        if n % 25 == 0:
            log(f"  images: {n}/{len(images)}")
    with open(os.path.join(img_dir, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"images": manifest, "failed": failed}, f, indent=2, ensure_ascii=False)
    log(f"Images downloaded: {len(manifest)}  |  failed: {len(failed)}")
    return manifest, failed


# --------------------------------------------------------------------------- size cap

def cap_file_sizes(out, max_bytes, log=print):
    """Make every screenshot fit under max_bytes: PNG -> JPEG, then split if needed.

    Returns a list of other files (e.g. huge images) that are still over the cap.
    """
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    for d in (DIR_DESKTOP, DIR_MOBILE):
        folder = os.path.join(out, d)
        for fn in sorted(os.listdir(folder)):
            path = os.path.join(folder, fn)
            if os.path.getsize(path) <= max_bytes:
                continue
            stem = os.path.splitext(fn)[0]
            im = Image.open(path).convert("RGB")
            jpg = os.path.join(folder, stem + ".jpg")
            im.save(jpg, "JPEG", quality=85, optimize=True)
            if os.path.getsize(jpg) > max_bytes:
                parts = math.ceil(os.path.getsize(jpg) / (max_bytes * 0.85))
                os.remove(jpg)
                h = math.ceil(im.height / parts)
                for p in range(parts):
                    im.crop((0, p * h, im.width, min(im.height, (p + 1) * h))).save(
                        os.path.join(folder, f"{stem}-part{p + 1}of{parts}.jpg"), "JPEG", quality=85, optimize=True)
                log(f"  {fn}: split into {parts} JPEG parts")
            else:
                log(f"  {fn}: saved as JPEG ({os.path.getsize(jpg) // 1024} KB)")
            os.remove(path)
    still_big = []
    for root, _, files in os.walk(out):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.getsize(p) > max_bytes:
                still_big.append(os.path.relpath(p, out))
    return still_big


# --------------------------------------------------------------------------- index

def write_index(out, site, folder_name, pages, sitemaps, manifest, failed_imgs, too_big, redirects=()):
    with open(os.path.join(out, "pages.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(pages[0].keys()) if pages else ["num"])
        w.writeheader()
        w.writerows(pages)
    ok = [p for p in pages if p["status"] and p["status"] < 400 and not p["error"]
          and "soft 404" not in p["notes"]]
    bad = [p for p in pages if p not in ok]
    extra = [p for p in pages if not p["in_sitemap"] and p["source"] == "link"]
    with_ld = [p for p in pages if p["jsonld_types"]]
    overflow = [p for p in pages if "overflow" in p["notes"]]
    home_missing = sitemaps and pages and not pages[0]["in_sitemap"]
    L = [f"# {folder_name}", "",
         f"Archive of **{site}** captured on {date.today().isoformat()} before the rebuild.", "",
         f"- Pages captured: **{len(pages)}** ({len(ok)} OK, {len(bad)} with errors or 4xx/5xx)",
         f"- Sitemaps saved: {len(sitemaps)}",
         f"- Pages found through links but missing from the sitemap: {len(extra)}",
         f"- Redirects (listed, not archived twice): {len(redirects)}",
         f"- Pages with JSON-LD: {len(with_ld)}",
         f"- Images downloaded: {len(manifest)} ({len(failed_imgs)} could not be downloaded)", "",
         "Each page has the same number in every folder: `desktop-NN-*.png`, `mobile-NN-*.png`, "
         "`NN-*.md`, `NN-*.json`.", "",
         "| # | Page | Status | In sitemap | JSON-LD types |", "|---|---|---|---|---|"]
    for p in pages:
        st = p["error"] and f"error: {p['error'][:60]}" or str(p["status"])
        L.append(f"| {p['num']} | [{p['title'] or p['name']}]({p['url']}) | {st} | "
                 f"{'yes' if p['in_sitemap'] else 'no'} | {p['jsonld_types'] or '—'} |")
    if bad:
        L += ["", "## Pages with problems", ""] + [
            f"- {p['num']} {p['url']} — {p['error'] or ('soft 404' if 'soft 404' in p['notes'] else p['status'])}"
            for p in bad]
    if home_missing:
        L += ["", "The home page is not listed in the sitemap."]
    if redirects:
        L += ["", "## Redirects (keep these working on the new site)", ""] + [
            f"- {r['url']} → {r['to']} (page {r['num']})" for r in redirects]
    if overflow:
        L += ["", "## Layout problems", "",
              "Screenshots are cropped to the viewport width; these pages scroll sideways:", ""] + [
            f"- {p['num']} {p['url']} — {p['notes']}" for p in overflow]
    if extra:
        L += ["", "## Pages not in the sitemap (found through menus or links)", ""] + [f"- {p['num']} {p['url']}" for p in extra]
    if too_big:
        L += ["", "## Files over the upload size limit (not uploaded by the connector)", ""] + [f"- {f}" for f in too_big]
    if failed_imgs:
        L += ["", f"## Images that could not be downloaded ({len(failed_imgs)})", "",
              "See `05 Images/_manifest.json` → `failed`."]
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# --------------------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Archive a client's old website (screenshots, sitemaps, markdown, JSON-LD, images).")
    p.add_argument("--site", required=True, help="Site home page, e.g. https://www.example.com")
    p.add_argument("--sitemap", help="Sitemap URL (default: from robots.txt or the usual paths)")
    p.add_argument("-o", "--output-dir", default="./site_archive", help="Parent folder for the archive (default ./site_archive)")
    p.add_argument("--folder-name", help='Archive folder name (default "Old Website Archive - YYYY-MM-DD (domain)")')
    p.add_argument("--max-pages", type=int, default=500)
    p.add_argument("--delay", type=float, default=0.5, help="Pause between pages, seconds")
    p.add_argument("--no-screenshots", action="store_true")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--no-discover", action="store_true", help="Only crawl sitemap URLs (+ home page)")
    p.add_argument("--max-file-mb", type=float, default=0, help="Shrink/split screenshots to fit this size (0 = off)")
    p.add_argument("--upload-drive", action="store_true", help="Upload the archive with the service account (drive.py)")
    p.add_argument("-c", "--client", help="Client slug (clients.yaml) to find their Drive via Notion")
    p.add_argument("--drive-parent", help="Drive folder / shared drive id to upload into (instead of --client)")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    site = a.site if a.site.startswith("http") else "https://" + a.site
    site = norm_url(site).rstrip("/") + "/"
    domain = urlparse(site).netloc.removeprefix("www.")
    folder_name = a.folder_name or f"Old Website Archive - {date.today().isoformat()} ({domain})"

    service = parent = None
    if a.upload_drive:  # resolve everything that can fail before crawling
        import drive
        try:
            service = drive.build_service(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"))
            if a.drive_parent:
                parent = a.drive_parent
            elif a.client:
                import notion
                parent = notion.resolve_drive_folder_id(a.client)
            else:
                print("Error: --upload-drive needs --client or --drive-parent.", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    out = os.path.join(os.path.abspath(a.output_dir), folder_name)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    print(f"Archiving {site} -> {out}")

    seeds, sitemaps = discover_sitemaps(site, os.path.join(out, DIR_SITEMAPS), a.sitemap)
    pages, images, redirects = crawl(site, seeds, out, screenshots=not a.no_screenshots,
                          discover=not a.no_discover, max_pages=a.max_pages, delay=a.delay)
    with open(os.path.join(out, DIR_SITEMAPS, "urls.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(p["url"] for p in pages) + "\n")
    if redirects:
        with open(os.path.join(out, DIR_SITEMAPS, "redirects.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["url", "to", "num", "source", "in_sitemap"])
            w.writeheader()
            w.writerows(redirects)

    if not any(p["status"] and p["status"] < 400 for p in pages):
        print("Error: no page loaded successfully; nothing uploaded. Is the site reachable from here?", file=sys.stderr)
        return 2

    manifest, failed = ({}, [])
    if not a.no_images and images:
        manifest, failed = download_images(images, os.path.join(out, DIR_IMAGES))

    too_big = cap_file_sizes(out, int(a.max_file_mb * 1024 * 1024)) if a.max_file_mb else []
    write_index(out, site, folder_name, pages, sitemaps, manifest, failed, too_big, redirects)

    print("\n" + "=" * 60)
    print(f"ARCHIVE COMPLETE: {len(pages)} pages, {len(manifest)} images")
    print(f"Folder: {out}")
    if too_big:
        print(f"Over {a.max_file_mb} MB: {len(too_big)} files (listed in README.md)")

    if a.upload_drive:
        import drive
        print(f"\nUploading to Drive ({parent}) ...")
        result = drive.upload_scrape(service, out, parent, folder_name)
        s = result["stats"]
        print(f"Uploaded {s['files']} files in {s['folders']} folders. {result['link']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
