#!/usr/bin/env python3
"""
grab_images.py - pull images from websites / gallery pages into a named folder.

Setup (once):
    pip install requests beautifulsoup4

Usage:
    python grab_images.py "plumber-photos" https://site1.com https://site2.com/gallery
    python grab_images.py "plumber-photos" -f urls.txt
    python grab_images.py "plumber-photos" https://site1.com --follow-gallery
    python grab_images.py "plumber-photos" https://site1.com --out ~/Desktop --min-kb 30

Images land in:  <out>/<folder-name>/   (default out = ./downloads)
"""
import argparse
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
GALLERY_HINT = re.compile(r"gallery|photos?|portfolio|our-work|projects?|before-after|results", re.I)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
SKIP_HINT = re.compile(r"logo|icon|sprite|favicon|avatar|placeholder|pixel|spacer|badge", re.I)


def best_from_srcset(srcset):
    """Return the largest URL in a srcset string."""
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        w = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)", bits[1])
            w = int(m.group(1)) if m else 0
        if w >= best_w:
            best, best_w = url, w
    return best


def full_size(url):
    """WordPress-style thumbnails (-300x200.jpg) -> original."""
    return re.sub(r"-\d{2,4}x\d{2,4}(?=\.\w{3,4}(\?|$))", "", url)


def extract_images(soup, base):
    found = []

    def add(u):
        if not u or u.startswith("data:"):
            return
        found.append(urljoin(base, u.strip()))

    for img in soup.find_all("img"):
        for attr in ("data-src", "data-lazy-src", "data-original", "data-full", "src"):
            if img.get(attr):
                add(img[attr])
        for attr in ("srcset", "data-srcset"):
            if img.get(attr):
                add(best_from_srcset(img[attr]))
    for src in soup.find_all("source"):
        for attr in ("srcset", "data-srcset"):
            if src.get(attr):
                add(best_from_srcset(src[attr]))
    for a in soup.find_all("a", href=True):
        if urlparse(a["href"]).path.lower().endswith(IMG_EXT):
            add(a["href"])  # lightbox / gallery full-size links
    for meta in soup.find_all("meta", attrs={"property": "og:image"}):
        add(meta.get("content"))
    for tag in soup.find_all(style=True):
        for m in re.findall(r"url\(['\"]?([^'\")]+)['\"]?\)", tag["style"]):
            add(m)
    return found


def get(session, url, **kw):
    try:
        r = session.get(url, headers=HEADERS, timeout=20, **kw)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def gallery_links(soup, base):
    host = urlparse(base).netloc
    links = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"]).split("#")[0]
        if urlparse(full).netloc == host and full != base:
            if GALLERY_HINT.search(full) or GALLERY_HINT.search(a.get_text(" ")):
                links.add(full)
    return list(links)[:10]


def main():
    ap = argparse.ArgumentParser(description="Download images from websites into a named folder.")
    ap.add_argument("folder", help="folder name to save into")
    ap.add_argument("urls", nargs="*", help="website / gallery URLs")
    ap.add_argument("-f", "--file", help="text file with one URL per line")
    ap.add_argument("--out", default="downloads", help="parent directory (default: ./downloads)")
    ap.add_argument("--min-kb", type=int, default=20, help="skip files smaller than this (default 20)")
    ap.add_argument("--follow-gallery", action="store_true",
                    help="also scan gallery/photos/portfolio links found on each site")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        urls += [l.strip() for l in Path(args.file).read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
    if not urls:
        sys.exit("No URLs given. Pass URLs or use -f urls.txt")

    dest = Path(args.out).expanduser() / args.folder
    dest.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    seen_urls, seen_hashes = set(), set()
    # preload hashes so re-runs don't duplicate
    for p in dest.iterdir():
        if p.is_file():
            seen_hashes.add(hashlib.sha1(p.read_bytes()).hexdigest())
    saved = 0

    for site in urls:
        if not site.startswith("http"):
            site = "https://" + site
        domain = urlparse(site).netloc.replace("www.", "").split(":")[0]
        print(f"\n== {site}")

        pages = [site]
        r = get(session, site)
        if not r:
            print("   couldn't load page, skipping")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        if args.follow_gallery:
            extra = gallery_links(soup, site)
            print(f"   +{len(extra)} gallery pages")
            pages += extra

        site_count = 0
        for page in pages:
            if page != site:
                r = get(session, page)
                if not r:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
            for img_url in extract_images(soup, page):
                if img_url in seen_urls or SKIP_HINT.search(img_url):
                    continue
                seen_urls.add(img_url)
                path = urlparse(img_url).path.lower()
                if path.endswith(".svg") or (path.rsplit(".", 1)[-1] not in
                                             [e[1:] for e in IMG_EXT] and "." in path.rsplit("/", 1)[-1]):
                    continue

                # try the full-size version first, fall back to what the page gave us
                resp = None
                for candidate in dict.fromkeys([full_size(img_url), img_url]):
                    resp = get(session, candidate)
                    if resp and resp.headers.get("content-type", "").startswith("image"):
                        break
                    resp = None
                if not resp or len(resp.content) < args.min_kb * 1024:
                    continue

                digest = hashlib.sha1(resp.content).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)

                ext = Path(urlparse(img_url).path).suffix.lower()
                if ext not in IMG_EXT:
                    ext = ".jpg"
                site_count += 1
                saved += 1
                name = f"{domain}_{site_count:03d}{ext}"
                (dest / name).write_bytes(resp.content)
                print(f"   saved {name}")
                time.sleep(0.2)  # be polite
        print(f"   {site_count} images from {domain}")

    print(f"\nDone. {saved} new images in {dest.resolve()}")


if __name__ == "__main__":
    main()
