import csv
from collections import deque
import json
import logging
import mimetypes
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup


# =========================
# 路徑設定
# =========================
ROOT = Path(__file__).resolve().parent
URL_FILE = ROOT / "input" / "urls.csv"

OUTPUT_DIR = ROOT / "output" / "standard_crawl"
ARTICLE_DIR = OUTPUT_DIR / "articles"
IMAGE_DIR = OUTPUT_DIR / "images"
INDEX_FILE = OUTPUT_DIR / "index.jsonl"
REPORT_FILE = OUTPUT_DIR / "crawl_report.csv"
LOG_FILE = OUTPUT_DIR / "crawler.log"

TIMEOUT = 20
REQUEST_DELAY = 1
IMAGE_DELAY = 0.2
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 單張圖片最多 20 MB

# 延伸探索最低要求：只探索一層，最多新增 10 個觀音區相關連結
MAX_EXTENSION_DEPTH = 1
MAX_EXTENSION_URLS = 10
GUANYIN_KEYWORDS = ("觀音", "草漯", "白沙岬", "甘泉寺", "觀音山", "觀音區")


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe", "nav", "footer", "aside",
    ".advertisement", ".advert", ".ads", ".ad-banner", ".social-share",
    ".share-buttons", ".cookie-notice", ".related-articles",
    ".recommendations", ".popup", ".modal",
]

MAIN_SELECTORS = [
    "article", "main", '[role="main"]', ".article-content",
    ".article-body", ".post-content", ".entry-content",
    ".content-body", ".article", "#content", "#main",
]

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
}


def setup():
    ARTICLE_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


VIDEO_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def is_video_url(url):
    """判斷是否為 YouTube 影片網址；這些網址不列入爬取清單。"""
    host = (urlparse(url).hostname or "").lower()
    return host in VIDEO_HOSTS or host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")


def load_urls():
    if not URL_FILE.exists():
        raise FileNotFoundError(f"找不到網址清單：{URL_FILE}")

    urls = []
    seen = set()
    skipped_videos = 0

    with URL_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or "url" not in reader.fieldnames:
            raise ValueError("urls.csv 必須包含 url 欄位")

        for row in reader:
            url = (row.get("url") or "").strip()
            category = (row.get("category") or "").strip()

            if not url.startswith(("http://", "https://")):
                continue

            if is_video_url(url):
                skipped_videos += 1
                logging.info("略過 YouTube 影片網址：%s", url)
                continue

            if url not in seen:
                urls.append({"url": url, "category": category})
                seen.add(url)

    logging.info("已略過 YouTube 影片網址：%d 個", skipped_videos)
    return urls


def remove_noise(soup):
    for selector in NOISE_SELECTORS:
        for element in soup.select(selector):
            element.decompose()
    return soup


def select_main(soup):
    for selector in MAIN_SELECTORS:
        element = soup.select_one(selector)
        if element and len(element.get_text(" ", strip=True)) >= 80:
            return element, selector
    return soup.body or soup, "body_fallback"


def extract_text(container):
    parts = []
    for element in container.select("h1, h2, h3, h4, p, li, blockquote, td, th"):
        text = element.get_text(" ", strip=True)
        if text and (not parts or text != parts[-1]):
            parts.append(text)
    if parts:
        return "\n\n".join(parts)
    return container.get_text("\n", strip=True)


def extract_images(container, page_url):
    images = []
    seen = set()

    for img in container.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
            or img.get("data-original-src")
            or img.get("src")
        )

        if not src:
            srcset = img.get("data-srcset") or img.get("srcset") or ""
            if srcset:
                # 選擇 srcset 中最後一個候選項，通常是較高解析度版本
                src = srcset.split(",")[-1].strip().split()[0]

        if not src:
            continue

        image_url = urljoin(page_url, src.strip())
        if not image_url.startswith(("http://", "https://")):
            continue
        if image_url in seen:
            continue

        # 排除常見的追蹤像素／極小圖標
        if any(token in image_url.lower() for token in
               ("favicon", "tracking-pixel", "pixel.gif", "blank.gif")):
            continue

        seen.add(image_url)

        caption = ""
        figure = img.find_parent("figure")
        if figure:
            caption_element = figure.find("figcaption")
            if caption_element:
                caption = caption_element.get_text(" ", strip=True)

        images.append({
            "image_url": image_url,
            "alt": img.get("alt", "").strip(),
            "caption": caption,
            "local_path": "",
            "download_status": "pending",
            "download_reason": "",
        })

    return images


def extract_links(soup, page_url):
    links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].strip())
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        links.append({
            "text": a.get_text(" ", strip=True),
            "url": href,
        })
    return links


def safe_extension(image_url, content_type):
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[content_type]

    path = unquote(urlparse(image_url).path)
    suffix = Path(path).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
               ".bmp", ".tif", ".tiff", ".avif"}
    if suffix in allowed:
        return ".jpg" if suffix == ".jpeg" else suffix

    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed in allowed:
        return guessed
    return ".jpg"


def download_image(image, page_id, image_number, session, page_url):
    """下載單張圖片；失敗時保留原因，不會假裝下載成功。"""
    image_url = image["image_url"]
    response = None
    temp_path = None

    try:
        response = session.get(
            image_url,
            timeout=TIMEOUT,
            stream=True,
            headers={"Referer": page_url},
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        content_type_clean = content_type.split(";")[0].strip().lower()
        if not content_type_clean.startswith("image/"):
            image["download_status"] = "failed"
            image["download_reason"] = f"回應不是圖片：Content-Type={content_type_clean or '未知'}"
            return image

        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_IMAGE_BYTES:
            image["download_status"] = "failed"
            image["download_reason"] = f"圖片超過大小限制（{MAX_IMAGE_BYTES // (1024 * 1024)} MB）"
            return image

        ext = safe_extension(image_url, content_type)
        # 每個網頁使用獨立資料夾，避免不同網頁的圖片混在一起
        page_image_dir = IMAGE_DIR / page_id
        page_image_dir.mkdir(parents=True, exist_ok=True)
        filename = f"image_{image_number:03d}{ext}"
        target_path = page_image_dir / filename
        temp_path = target_path.with_suffix(target_path.suffix + ".part")

        total_bytes = 0
        with temp_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    raise ValueError(
                        f"圖片超過大小限制（{MAX_IMAGE_BYTES // (1024 * 1024)} MB）"
                    )
                output.write(chunk)

        if total_bytes == 0:
            raise ValueError("下載到的檔案是空的")

        temp_path.replace(target_path)
        image["local_path"] = str(Path("images") / page_id / filename).replace("\\", "/")
        image["download_status"] = "success"
        image["download_reason"] = ""
        return image

    except Exception as exc:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        image["download_status"] = "failed"
        image["download_reason"] = f"{type(exc).__name__}: {exc}"
        logging.warning("圖片下載失敗：%s | %s", image_url, image["download_reason"])
        return image
    finally:
        if response is not None:
            response.close()


def crawl_one(item, session, index):
    url = item["url"]
    response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    soup = BeautifulSoup(response.text, "html.parser")

    title_element = soup.find("meta", property="og:title")
    if title_element and title_element.get("content"):
        title = title_element["content"].strip()
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)
    else:
        title = ""

    site_element = soup.find("meta", property="og:site_name")
    source_name = (
        site_element["content"].strip()
        if site_element and site_element.get("content")
        else urlparse(response.url).netloc
    )

    original_soup = BeautifulSoup(response.text, "html.parser")
    content_soup = BeautifulSoup(response.text, "html.parser")
    remove_noise(content_soup)
    main_content, selector_used = select_main(content_soup)

    content = extract_text(main_content)
    images = extract_images(main_content, response.url)
    links = extract_links(original_soup, response.url)

    # 逐張下載圖片，並將下載結果寫入圖片資料欄位
    page_id = f"page_{index:03d}"
    for image_number, image in enumerate(images, start=1):
        download_image(image, page_id, image_number, session, response.url)
        time.sleep(IMAGE_DELAY)

    successful_images = sum(
        image["download_status"] == "success" for image in images
    )
    failed_images = len(images) - successful_images

    return {
        "id": page_id,
        "type": "webpage",
        "category": item.get("category", ""),
        "title": title,
        "source_url": url,
        "final_url": response.url,
        "source_name": source_name,
        "content": content,
        "images": images,
        "links": links,
        "crawl_status": "success",
        "status_reason": "",
        "http_status": response.status_code,
        "main_selector": selector_used,
        "image_count": len(images),
        "images_downloaded": successful_images,
        "images_failed": failed_images,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def save_article(record):
    image_lines = []
    for img in record.get("images", []):
        image_lines.append(
            f"- 圖片網址：{img['image_url']}\n"
            f"  - 本機檔案：{img.get('local_path') or '未下載'}\n"
            f"  - 下載狀態：{img.get('download_status', 'unknown')}\n"
            f"  - 下載原因：{img.get('download_reason', '') or '無'}\n"
            f"  - alt：{img.get('alt', '')}\n"
            f"  - 說明：{img.get('caption', '')}"
        )
    images_text = "\n".join(image_lines) or "未找到圖片。"

    links_text = "\n".join(
        f"- [{link['text'] or link['url']}]({link['url']})"
        for link in record.get("links", [])
    ) or "未找到連結。"

    content = record.get("content") or "（未擷取到正文，請檢查網頁結構。）"
    markdown = (
        f"# {record.get('title') or '（無標題）'}\n\n"
        f"- 分類：{record.get('category') or '未分類'}\n"
        f"- 原始網址：{record['source_url']}\n"
        f"- 最終網址：{record.get('final_url', '')}\n"
        f"- 網站名稱：{record.get('source_name', '')}\n"
        f"- HTTP 狀態碼：{record.get('http_status', '')}\n"
        f"- 正文選擇器：`{record.get('main_selector', '')}`\n"
        f"- 圖片資訊數：{record.get('image_count', 0)}\n"
        f"- 圖片下載成功：{record.get('images_downloaded', 0)}\n"
        f"- 圖片下載失敗：{record.get('images_failed', 0)}\n\n"
        f"## 清理後正文\n\n{content}\n\n"
        f"## 圖片資訊與下載結果\n\n{images_text}\n\n"
        f"## 頁面連結\n\n{links_text}\n"
    )
    (ARTICLE_DIR / f"{record['id']}.md").write_text(markdown, encoding="utf-8")


def normalize_url(url):
    """移除網址片段，避免同一網址因 #fragment 被重複爬取。"""
    from urllib.parse import urldefrag
    return urldefrag((url or "").strip())[0]


def is_guanyin_related_link(link):
    """以連結文字或網址中的關鍵字，初步判斷是否與觀音區主題相關。"""
    text = f"{link.get('text', '')} {link.get('url', '')}".lower()
    return any(keyword.lower() in text for keyword in GUANYIN_KEYWORDS)


def main():
    setup()
    urls = load_urls()
    if not urls:
        logging.warning("網址清單沒有有效網址。")
        return

    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    visited = set()
    queue = deque()
    extension_count = 0

    # 第一階段：教師提供的網址，深度 0
    for item in urls:
        normalized = normalize_url(item["url"])
        if normalized and normalized not in visited:
            visited.add(normalized)
            queue.append({
                "url": normalized,
                "category": item.get("category", ""),
                "depth": 0,
                "is_extension": False,
                "discovered_from": "",
            })

    next_index = 1

    try:
        while queue:
            task = queue.popleft()
            url = task["url"]
            depth = task["depth"]
            page_id = f"page_{next_index:03d}"
            logging.info(
                "正在處理 [%s] depth=%d extension=%s：%s",
                page_id, depth, task["is_extension"], url
            )

            try:
                record = crawl_one(
                    {"url": url, "category": task["category"]},
                    session,
                    next_index,
                )
                record["crawl_depth"] = depth
                record["is_extension"] = task["is_extension"]
                record["discovered_from"] = task["discovered_from"]
                save_article(record)

                logging.info(
                    "成功：%s | depth=%d | 延伸探索=%s",
                    record.get("title", ""),
                    depth,
                    task["is_extension"],
                )

                # 僅從原始指定網址（depth 0）尋找延伸連結；depth 1 不再擴展
                if depth == 0 and extension_count < MAX_EXTENSION_URLS:
                    for link in record.get("links", []):
                        if extension_count >= MAX_EXTENSION_URLS:
                            break
                        candidate = normalize_url(link.get("url", ""))
                        if not candidate or candidate in visited:
                            continue
                        if urlparse(candidate).scheme not in ("http", "https"):
                            continue
                        if not is_guanyin_related_link(link):
                            continue

                        visited.add(candidate)
                        queue.append({
                            "url": candidate,
                            "category": "觀音區延伸探索",
                            "depth": MAX_EXTENSION_DEPTH,
                            "is_extension": True,
                            "discovered_from": url,
                        })
                        extension_count += 1
                        logging.info(
                            "加入延伸探索佇列（depth=1）：%s（來源：%s）",
                            candidate, url
                        )

            except Exception as exc:
                record = {
                    "id": page_id,
                    "type": "webpage",
                    "category": task["category"],
                    "title": "",
                    "source_url": url,
                    "final_url": "",
                    "source_name": urlparse(url).netloc,
                    "content": "",
                    "images": [],
                    "links": [],
                    "crawl_status": "failed_or_unavailable",
                    "status_reason": f"{type(exc).__name__}: {exc}",
                    "http_status": "",
                    "main_selector": "",
                    "image_count": 0,
                    "images_downloaded": 0,
                    "images_failed": 0,
                    "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "crawl_depth": depth,
                    "is_extension": task["is_extension"],
                    "discovered_from": task["discovered_from"],
                }
                logging.warning("失敗：%s | %s", url, record["status_reason"])

            records.append(record)
            next_index += 1
            time.sleep(REQUEST_DELAY)

    finally:
        session.close()

    # 一次寫出完整 JSONL，確保原始網址與延伸網址都有紀錄
    with INDEX_FILE.open("w", encoding="utf-8") as index_file:
        for record in records:
            index_file.write(json.dumps(record, ensure_ascii=False) + "\n")

    report_fields = [
        "id", "category", "title", "source_url", "final_url",
        "crawl_status", "status_reason", "http_status", "main_selector",
        "image_count", "images_downloaded", "images_failed",
        "crawl_depth", "is_extension", "discovered_from", "crawled_at",
    ]
    with REPORT_FILE.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=report_fields)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key, "") for key in report_fields})

    success_count = sum(r["crawl_status"] == "success" for r in records)
    failed_count = len(records) - success_count
    total_found = sum(r.get("image_count", 0) for r in records)
    total_downloaded = sum(r.get("images_downloaded", 0) for r in records)
    total_image_failed = sum(r.get("images_failed", 0) for r in records)
    extension_success = sum(
        r.get("is_extension") and r.get("crawl_status") == "success"
        for r in records
    )
    extension_failed = sum(
        r.get("is_extension") and r.get("crawl_status") != "success"
        for r in records
    )

    print("\n========== 爬取完成 ==========")
    print(f"原始指定網址（去重後）：{len(urls)}")
    print(f"總處理紀錄：{len(records)}")
    print(f"網頁成功：{success_count}")
    print(f"網頁失敗：{failed_count}")
    print(f"延伸探索網址：{extension_count}")
    print(f"延伸探索成功：{extension_success}")
    print(f"延伸探索失敗：{extension_failed}")
    print(f"找到圖片資訊：{total_found}")
    print(f"圖片下載成功：{total_downloaded}")
    print(f"圖片下載失敗：{total_image_failed}")
    print(f"文章資料夾：{ARTICLE_DIR}")
    print(f"圖片資料夾：{IMAGE_DIR}")
    print(f"JSONL：{INDEX_FILE}")
    print(f"爬取報告：{REPORT_FILE}")
    print(f"執行紀錄：{LOG_FILE}")


if __name__ == "__main__":
    main()
