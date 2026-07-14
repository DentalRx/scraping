#!/usr/bin/env python3
"""
Website Scraper - Extracts JSON-LD, text content, and images from sitemap URLs.

Output layout (a clean, browsable migration reference):

    <output_dir>/
        pages/   <slug>.txt    one text file per page
        jsonld/  <slug>.json   JSON-LD for pages that have it
        images/  <hash>.<ext>  de-duplicated images (+ _manifest.json)
        urls_scraped.txt       the list of URLs that were crawled

Run it directly to read configuration from .env:

    python scrape_sitemap.py

Or drive it from scrape.py (which adds --client / --url / --upload-drive), or
import scrape_site() to reuse the crawl.
"""

import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
import hashlib

from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

# Load environment variables from .env file
load_dotenv()

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}


def sanitize_filename(url):
    """Convert URL to a safe folder name."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if not path:
        path = 'homepage'
    # Replace special characters with underscores
    safe_name = re.sub(r'[^\w\-]', '_', path)
    # Truncate if too long and add hash for uniqueness
    if len(safe_name) > 100:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        safe_name = safe_name[:90] + '_' + url_hash
    return safe_name


def _unique_slug(base, url, used):
    """Return a slug not already in `used`, disambiguating collisions by url hash."""
    slug = base
    if slug in used:
        suffix = hashlib.md5(url.encode()).hexdigest()[:6]
        slug = f"{base}_{suffix}"
        n = 2
        while slug in used:
            slug = f"{base}_{suffix}_{n}"
            n += 1
    used.add(slug)
    return slug


def fetch_sitemap(sitemap_url):
    """Fetch and parse sitemap XML, returns list of URLs."""
    print(f"Fetching sitemap: {sitemap_url}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    }

    response = requests.get(sitemap_url, headers=headers, timeout=30)
    response.raise_for_status()

    urls = []

    # Parse XML
    root = ET.fromstring(response.content)

    # Handle namespace
    namespace = ''
    if root.tag.startswith('{'):
        namespace = root.tag.split('}')[0] + '}'

    # Check if this is a sitemap index (contains other sitemaps)
    sitemap_tags = root.findall(f'.//{namespace}sitemap')
    if sitemap_tags:
        print("Found sitemap index, fetching nested sitemaps...")
        for sitemap in sitemap_tags:
            loc = sitemap.find(f'{namespace}loc')
            if loc is not None and loc.text:
                nested_urls = fetch_sitemap(loc.text.strip())
                urls.extend(nested_urls)
    else:
        # Regular sitemap with URLs
        for url_elem in root.findall(f'.//{namespace}url'):
            loc = url_elem.find(f'{namespace}loc')
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    return urls


def extract_json_ld(soup):
    """Extract all JSON-LD scripts from the page."""
    json_ld_data = []

    scripts = soup.find_all('script', type='application/ld+json')
    for script in scripts:
        try:
            if script.string:
                data = json.loads(script.string)
                json_ld_data.append(data)
        except json.JSONDecodeError as e:
            print(f"  Warning: Could not parse JSON-LD: {e}")

    return json_ld_data


def extract_text_content(soup):
    """Extract readable text content from the page."""
    content = []

    # Remove script, style, and other non-content elements
    for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript']):
        element.decompose()

    # Extract title
    title = soup.find('title')
    if title and title.string:
        content.append(f"TITLE: {title.string.strip()}\n")

    # Extract meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        content.append(f"META DESCRIPTION: {meta_desc['content'].strip()}\n")

    content.append("\n" + "=" * 50 + "\n")

    # Extract headings and paragraphs in order
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'span', 'div', 'article', 'section']):
        # Skip if this tag has children that are also content tags (avoid duplication)
        if tag.name in ['div', 'article', 'section']:
            if tag.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p']):
                continue

        text = tag.get_text(separator=' ', strip=True)
        if text and len(text) > 2:  # Skip very short text
            if tag.name.startswith('h'):
                level = tag.name[1]
                prefix = '#' * int(level)
                content.append(f"\n{prefix} {text}\n")
            elif tag.name == 'li':
                content.append(f"  - {text}")
            elif tag.name == 'p':
                content.append(f"\n{text}\n")
            elif len(text) > 20:  # Only include longer div/span text
                content.append(text)

    # Deduplicate while preserving order
    seen = set()
    unique_content = []
    for item in content:
        item_clean = item.strip()
        if item_clean and item_clean not in seen:
            seen.add(item_clean)
            unique_content.append(item)

    return '\n'.join(unique_content)


def extract_images(soup, base_url):
    """Extract all image URLs from the page."""
    images = []

    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
        if src:
            # Convert relative URLs to absolute
            absolute_url = urljoin(base_url, src)

            # Get alt text
            alt = img.get('alt', '')

            images.append({
                'url': absolute_url,
                'alt': alt
            })

    # Also check for background images in style attributes and picture sources
    for source in soup.find_all('source'):
        srcset = source.get('srcset')
        if srcset:
            # Parse srcset (may contain multiple URLs)
            for src_part in srcset.split(','):
                src = src_part.strip().split()[0]
                if src:
                    absolute_url = urljoin(base_url, src)
                    images.append({'url': absolute_url, 'alt': ''})

    # Deduplicate by URL
    seen_urls = set()
    unique_images = []
    for img in images:
        if img['url'] not in seen_urls:
            seen_urls.add(img['url'])
            unique_images.append(img)

    return unique_images


def download_image(url, save_path, headers):
    """Download an image and save it to disk."""
    try:
        response = requests.get(url, headers=headers, timeout=30, stream=True)
        response.raise_for_status()

        # Get file extension from URL or content type
        parsed_url = urlparse(url)
        ext = os.path.splitext(parsed_url.path)[1].lower()

        if not ext or ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico']:
            content_type = response.headers.get('content-type', '')
            if 'jpeg' in content_type or 'jpg' in content_type:
                ext = '.jpg'
            elif 'png' in content_type:
                ext = '.png'
            elif 'gif' in content_type:
                ext = '.gif'
            elif 'webp' in content_type:
                ext = '.webp'
            elif 'svg' in content_type:
                ext = '.svg'
            else:
                ext = '.jpg'  # Default

        # Create filename from URL hash
        filename = hashlib.md5(url.encode()).hexdigest()[:16] + ext
        filepath = os.path.join(save_path, filename)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return filename
    except Exception as e:
        print(f"    Failed to download {url}: {e}")
        return None


def scrape_page(url, pages_dir, jsonld_dir, images_dir, headers,
                download_images=True, used_slugs=None, image_manifest=None, log=print):
    """Scrape a single page into the pages/ jsonld/ images/ layout."""
    log(f"\nScraping: {url}")

    used_slugs = used_slugs if used_slugs is not None else set()
    slug = _unique_slug(sanitize_filename(url), url, used_slugs)

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract JSON-LD *before* extract_text_content(), which strips <script>.
        json_ld = extract_json_ld(soup)
        if json_ld:
            jsonld_path = os.path.join(jsonld_dir, slug + '.json')
            with open(jsonld_path, 'w', encoding='utf-8') as f:
                json.dump({'source_url': url, 'json_ld': json_ld}, f, indent=2, ensure_ascii=False)
            log(f"  Saved JSON-LD ({len(json_ld)} items)")
        else:
            log("  No JSON-LD found")

        # Extract text content (mutates soup: removes script/nav/footer/etc.)
        text_content = extract_text_content(soup)
        text_path = os.path.join(pages_dir, slug + '.txt')
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(f"Source URL: {url}\n")
            f.write("=" * 50 + "\n\n")
            f.write(text_content)
        log("  Saved page text")

        # Extract and download images
        images = extract_images(soup, url)
        log(f"  Found {len(images)} images")

        if download_images and images:
            downloaded = 0
            for i, img in enumerate(images):
                filename = download_image(img['url'], images_dir, headers)
                if filename:
                    downloaded += 1
                    if image_manifest is not None and filename not in image_manifest:
                        image_manifest[filename] = {
                            'original_url': img['url'],
                            'alt': img['alt'],
                            'source_page': url,
                        }
                # Rate limiting
                if i > 0 and i % 10 == 0:
                    time.sleep(0.5)

            log(f"  Downloaded {downloaded} images")

        return True

    except Exception as e:
        log(f"  Error scraping {url}: {e}")
        error_path = os.path.join(pages_dir, slug + '.error.txt')
        with open(error_path, 'w', encoding='utf-8') as f:
            f.write(f"URL: {url}\n")
            f.write(f"Error: {str(e)}\n")
        return False


def scrape_site(sitemap_url, output_dir, *, download_images=True, include_homepage=True,
                delay=1.0, limit=None, headers=None, log=print):
    """Crawl a site's sitemap into `output_dir` and return a summary dict.

    Raises on fatal errors (bad sitemap, no URLs) so a caller can abort before
    doing anything with a half-finished crawl. Per-page errors are non-fatal:
    they are logged, recorded as failures, and the crawl continues.
    """
    headers = headers or DEFAULT_HEADERS
    output_dir = os.path.abspath(output_dir)
    pages_dir = os.path.join(output_dir, 'pages')
    jsonld_dir = os.path.join(output_dir, 'jsonld')
    images_dir = os.path.join(output_dir, 'images')
    for d in (pages_dir, jsonld_dir, images_dir):
        os.makedirs(d, exist_ok=True)

    urls = fetch_sitemap(sitemap_url)
    if not urls:
        raise RuntimeError(f"No URLs found in sitemap: {sitemap_url}")

    # Add homepage if not already in sitemap
    if include_homepage:
        parsed = urlparse(sitemap_url)
        homepage_url = f"{parsed.scheme}://{parsed.netloc}/"
        if homepage_url not in urls and homepage_url.rstrip('/') not in urls:
            urls.insert(0, homepage_url)
            log("Added homepage to URL list")

    if limit:
        urls = urls[:limit]

    log(f"Scraping {len(urls)} URLs -> {output_dir}")

    # Save URL list
    urls_path = os.path.join(output_dir, 'urls_scraped.txt')
    with open(urls_path, 'w', encoding='utf-8') as f:
        f.write(f"Sitemap: {sitemap_url}\n")
        f.write(f"Total URLs: {len(urls)}\n")
        f.write("=" * 50 + "\n\n")
        for url in urls:
            f.write(url + "\n")

    used_slugs = set()
    image_manifest = {}
    success_count = 0
    fail_count = 0

    for i, url in enumerate(urls, 1):
        log(f"\n[{i}/{len(urls)}]")
        ok = scrape_page(url, pages_dir, jsonld_dir, images_dir, headers,
                         download_images=download_images, used_slugs=used_slugs,
                         image_manifest=image_manifest, log=log)
        if ok:
            success_count += 1
        else:
            fail_count += 1

        # Rate limiting
        if i < len(urls):
            time.sleep(delay)

    # Persist the image manifest so hashed filenames stay traceable.
    if download_images and image_manifest:
        manifest_path = os.path.join(images_dir, '_manifest.json')
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(image_manifest, f, indent=2, ensure_ascii=False)

    return {
        'total': len(urls),
        'success': success_count,
        'failed': fail_count,
        'output_dir': output_dir,
    }


def main():
    # Load configuration from .env file
    sitemap_url = os.getenv('SITEMAP_URL')
    output_dir = os.getenv('OUTPUT_DIR', './scraped_data')
    download_images = os.getenv('DOWNLOAD_IMAGES', 'true').lower() == 'true'
    include_homepage = os.getenv('INCLUDE_HOMEPAGE', 'true').lower() == 'true'
    delay = float(os.getenv('REQUEST_DELAY', '1.0'))
    limit = os.getenv('PAGE_LIMIT')
    limit = int(limit) if limit else None

    # Validate required config
    if not sitemap_url:
        print("Error: SITEMAP_URL is not set in .env file")
        print("Please edit .env and set SITEMAP_URL to your sitemap URL")
        sys.exit(1)

    print("=" * 60)
    print("Website Scraper")
    print("=" * 60)
    print(f"Download images: {download_images}")
    print(f"Delay between requests: {delay}s")
    print("=" * 60)

    try:
        summary = scrape_site(
            sitemap_url,
            output_dir,
            download_images=download_images,
            include_homepage=include_homepage,
            delay=delay,
            limit=limit,
        )
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Summary
    print("\n" + "=" * 60)
    print("SCRAPING COMPLETE")
    print("=" * 60)
    print(f"Total pages: {summary['total']}")
    print(f"Successful: {summary['success']}")
    print(f"Failed: {summary['failed']}")
    print(f"Output saved to: {summary['output_dir']}")


if __name__ == '__main__':
    main()
