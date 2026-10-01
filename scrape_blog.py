#!/usr/bin/env python3
"""
Blog Scraper - Extracts blog posts and converts them to Markdown.

Configuration is loaded from .env file. Just edit .env and run:
    python scrape_blog.py
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


def sanitize_filename(url):
    """Convert URL to a safe filename."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    # Get the last part of the path (the blog slug)
    slug = path.split('/')[-1] if '/' in path else path
    if not slug or slug == 'blog':
        slug = 'blog-index'
    # Replace special characters
    safe_name = re.sub(r'[^\w\-]', '_', slug)
    # Truncate if too long
    if len(safe_name) > 100:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        safe_name = safe_name[:90] + '_' + url_hash
    return safe_name


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

    # Check if this is a sitemap index
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


def filter_blog_urls(urls, blog_path='/blog/'):
    """Filter URLs to only include blog posts."""
    blog_urls = []
    # Pattern for date-based blog URLs like /2024/01/30/post-slug/
    date_pattern = re.compile(r'/\d{4}/\d{2}/\d{2}/')
    for url in urls:
        parsed = urlparse(url)
        path = parsed.path
        # Match date-based blog URLs or URLs containing the blog path
        if date_pattern.search(path):
            blog_urls.append(url)
        elif blog_path in path and path.rstrip('/') != blog_path.rstrip('/'):
            blog_urls.append(url)
    return blog_urls


def html_to_markdown(soup, base_url):
    """Convert HTML content to Markdown."""
    markdown_lines = []

    def process_element(element, list_prefix=''):
        """Recursively process HTML elements to markdown."""
        if element.name is None:
            # Text node
            text = str(element).strip()
            if text:
                return text
            return ''

        if element.name in ['script', 'style', 'nav', 'footer', 'aside', 'noscript', 'form']:
            return ''

        if element.name == 'h1':
            text = element.get_text(strip=True)
            return f"\n# {text}\n" if text else ''

        if element.name == 'h2':
            text = element.get_text(strip=True)
            return f"\n## {text}\n" if text else ''

        if element.name == 'h3':
            text = element.get_text(strip=True)
            return f"\n### {text}\n" if text else ''

        if element.name == 'h4':
            text = element.get_text(strip=True)
            return f"\n#### {text}\n" if text else ''

        if element.name == 'h5':
            text = element.get_text(strip=True)
            return f"\n##### {text}\n" if text else ''

        if element.name == 'h6':
            text = element.get_text(strip=True)
            return f"\n###### {text}\n" if text else ''

        if element.name == 'p':
            text = element.get_text(strip=True)
            return f"\n{text}\n" if text else ''

        if element.name == 'br':
            return '\n'

        if element.name == 'hr':
            return '\n---\n'

        if element.name == 'strong' or element.name == 'b':
            text = element.get_text(strip=True)
            return f"**{text}**" if text else ''

        if element.name == 'em' or element.name == 'i':
            text = element.get_text(strip=True)
            return f"*{text}*" if text else ''

        if element.name == 'a':
            text = element.get_text(strip=True)
            href = element.get('href', '')
            if href and text:
                # Make relative URLs absolute
                if href.startswith('/'):
                    href = urljoin(base_url, href)
                return f"[{text}]({href})"
            return text

        if element.name == 'img':
            src = element.get('src') or element.get('data-src', '')
            alt = element.get('alt', 'image')
            if src:
                if src.startswith('/'):
                    src = urljoin(base_url, src)
                return f"\n![{alt}]({src})\n"
            return ''

        if element.name == 'ul':
            items = []
            for li in element.find_all('li', recursive=False):
                text = li.get_text(strip=True)
                if text:
                    items.append(f"- {text}")
            return '\n' + '\n'.join(items) + '\n' if items else ''

        if element.name == 'ol':
            items = []
            for i, li in enumerate(element.find_all('li', recursive=False), 1):
                text = li.get_text(strip=True)
                if text:
                    items.append(f"{i}. {text}")
            return '\n' + '\n'.join(items) + '\n' if items else ''

        if element.name == 'blockquote':
            text = element.get_text(strip=True)
            if text:
                lines = text.split('\n')
                return '\n' + '\n'.join(f"> {line}" for line in lines) + '\n'
            return ''

        if element.name == 'code':
            text = element.get_text()
            return f"`{text}`" if text else ''

        if element.name == 'pre':
            text = element.get_text()
            return f"\n```\n{text}\n```\n" if text else ''

        # For container elements, process children
        if element.name in ['div', 'article', 'section', 'span', 'main']:
            result = []
            for child in element.children:
                child_md = process_element(child)
                if child_md:
                    result.append(child_md)
            return ''.join(result)

        # Default: just get text
        return element.get_text(strip=True)

    return process_element(soup)


def extract_blog_content(soup, base_url):
    """Extract blog content and convert to markdown."""
    # Try to find the main content area
    content_div = None

    # Try common content selectors
    selectors = [
        ('div', {'class_': 'col-12 col-lg-9'}),  # This site's structure
        ('article', {}),
        ('div', {'class_': 'post-content'}),
        ('div', {'class_': 'entry-content'}),
        ('div', {'class_': 'blog-content'}),
        ('main', {}),
    ]

    for tag, attrs in selectors:
        content_div = soup.find(tag, **attrs)
        if content_div:
            break

    if not content_div:
        # Fallback to body
        content_div = soup.find('body')

    if not content_div:
        return "No content found"

    # Extract title
    title = ""
    h1 = content_div.find('h1')
    if h1:
        title = h1.get_text(strip=True)

    # Extract date if available
    date = ""
    date_elem = soup.find(['time', 'span', 'div'], class_=re.compile(r'date|time|published', re.I))
    if date_elem:
        date = date_elem.get_text(strip=True)

    # Convert content to markdown
    markdown = html_to_markdown(content_div, base_url)

    # Clean up the markdown
    # Remove excessive newlines
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    # Remove any remaining HTML-like artifacts
    markdown = re.sub(r'<[^>]+>', '', markdown)

    return markdown.strip()


def extract_metadata(soup, url):
    """Extract metadata from the page."""
    metadata = {
        'url': url,
        'title': '',
        'description': '',
        'date': '',
        'author': '',
    }

    # Title
    title_tag = soup.find('title')
    if title_tag:
        metadata['title'] = title_tag.get_text(strip=True)

    # OG title (often cleaner)
    og_title = soup.find('meta', property='og:title')
    if og_title and og_title.get('content'):
        metadata['title'] = og_title['content']

    # Description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        metadata['description'] = meta_desc['content']

    # OG description
    og_desc = soup.find('meta', property='og:description')
    if og_desc and og_desc.get('content'):
        metadata['description'] = og_desc['content']

    # Date from URL or meta
    og_url = soup.find('meta', property='og:url')
    if og_url and og_url.get('content'):
        # Try to extract date from URL like /blog/2025/12/14/post-slug
        date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', og_url['content'])
        if date_match:
            metadata['date'] = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"

    # Author
    author_meta = soup.find('meta', attrs={'name': 'author'})
    if author_meta and author_meta.get('content'):
        metadata['author'] = author_meta['content']

    return metadata


def extract_images(soup, base_url):
    """Extract all image URLs from the page."""
    images = []

    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
        if src:
            absolute_url = urljoin(base_url, src)
            alt = img.get('alt', '')
            images.append({'url': absolute_url, 'alt': alt})

    for source in soup.find_all('source'):
        srcset = source.get('srcset')
        if srcset:
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
                ext = '.jpg'

        filename = hashlib.md5(url.encode()).hexdigest()[:16] + ext
        filepath = os.path.join(save_path, filename)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return filename
    except Exception as e:
        print(f"    Failed to download {url}: {e}")
        return None


def scrape_blog_post(url, output_dir, images_dir, headers):
    """Scrape a single blog post and save as markdown."""
    print(f"\nScraping: {url}")

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Extract metadata
        metadata = extract_metadata(soup, url)

        # Extract and convert content
        markdown_content = extract_blog_content(soup, url)

        # Build the final markdown file
        final_markdown = []

        # Add frontmatter
        final_markdown.append('---')
        final_markdown.append(f'title: "{metadata["title"]}"')
        final_markdown.append(f'url: {metadata["url"]}')
        if metadata['date']:
            final_markdown.append(f'date: {metadata["date"]}')
        if metadata['description']:
            final_markdown.append(f'description: "{metadata["description"]}"')
        if metadata['author']:
            final_markdown.append(f'author: "{metadata["author"]}"')
        final_markdown.append('---\n')

        # Add content
        final_markdown.append(markdown_content)

        # Save to file
        filename = sanitize_filename(url) + '.md'
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(final_markdown))

        print(f"  Saved: {filename}")

        # Download images to shared images directory
        images = extract_images(soup, url)
        if images:
            downloaded_count = 0
            for i, img in enumerate(images):
                if download_image(img['url'], images_dir, headers):
                    downloaded_count += 1
                if i > 0 and i % 10 == 0:
                    time.sleep(0.5)
            print(f"  Downloaded {downloaded_count} images")

        return True

    except Exception as e:
        print(f"  Error: {e}")
        return False


def main():
    # Load configuration from .env file
    sitemap_url = os.getenv('SITEMAP_URL')
    output_dir = os.getenv('BLOG_OUTPUT_DIR', './scraped_blog')
    blog_path = os.getenv('BLOG_PATH', '/blog/')
    delay = float(os.getenv('REQUEST_DELAY', '1.0'))
    limit = os.getenv('BLOG_LIMIT')
    limit = int(limit) if limit else None

    # Validate required config
    if not sitemap_url:
        print("Error: SITEMAP_URL is not set in .env file")
        sys.exit(1)

    # Setup
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Images directory under blog output
    images_dir = os.path.join(output_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }

    print("=" * 60)
    print("Blog Scraper - Markdown Export")
    print("=" * 60)

    # Fetch all URLs from sitemap
    try:
        all_urls = fetch_sitemap(sitemap_url)
    except Exception as e:
        print(f"Error fetching sitemap: {e}")
        sys.exit(1)

    # Filter to only blog URLs
    blog_urls = filter_blog_urls(all_urls, blog_path)

    if not blog_urls:
        print(f"No blog URLs found matching path: {blog_path}")
        sys.exit(1)

    print(f"\nFound {len(blog_urls)} blog posts")

    if limit:
        blog_urls = blog_urls[:limit]
        print(f"Limiting to {limit} posts")

    print(f"Output directory: {output_dir}")
    print(f"Delay between requests: {delay}s")
    print("=" * 60)

    # Scrape each blog post
    success_count = 0
    fail_count = 0

    for i, url in enumerate(blog_urls, 1):
        print(f"\n[{i}/{len(blog_urls)}]", end="")

        if scrape_blog_post(url, output_dir, images_dir, headers):
            success_count += 1
        else:
            fail_count += 1

        # Rate limiting
        if i < len(blog_urls):
            time.sleep(delay)

    # Summary
    print("\n" + "=" * 60)
    print("SCRAPING COMPLETE")
    print("=" * 60)
    print(f"Total blog posts: {len(blog_urls)}")
    print(f"Successful: {success_count}")
    print(f"Failed: {fail_count}")
    print(f"Output saved to: {output_dir}")


if __name__ == '__main__':
    main()
