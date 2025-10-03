import os
import re
import time
import random
import hashlib
import requests
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, urljoin, urlencode, urlunparse

from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ====== 配置 ======
BASE_LIST_URL = "https://www.mideahk.com/%E7%A9%BA%E6%B0%A3%E6%B5%81%E9%80%9A/%E5%86%B7%E6%B0%A3%E6%A9%9F"
SAVE_ROOT = "midea_ac_images"
PAGE_LOAD_TIMEOUT = 30
WAIT = 2.0  # 页面渲染额外等待（秒）
HEADLESS = True  # 若要看浏览器过程，改为 False
MAX_PAGES = 80   # 非常规循环保护
PAGE_PARAM = "page"  # 站点使用的分页参数名
MAX_ALLOWED_PAGE = 3  # —— 关键限制：最多到第 3 页 —— #

# ====== 通用 ======
def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")
    return s or f"item_{int(time.time()*1000)}"

def build_driver():
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=zh-CN,zh,en-US,en")
    # 降噪与稳定
    options.add_argument("--log-level=3")
    options.add_argument("--disable-notifications")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--remote-allow-origins=*")

    driver_path = ChromeDriverManager().install()
    service = Service(driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver

def scroll_to_bottom(driver, pause=0.6, max_rounds=20):
    last_height = driver.execute_script("return document.body.scrollHeight || document.documentElement.scrollHeight;")
    rounds = 0
    while rounds < max_rounds:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        new_height = driver.execute_script("return document.body.scrollHeight || document.documentElement.scrollHeight;")
        if new_height == last_height:
            break
        last_height = new_height
        rounds += 1

def pick_from_srcset(srcset: str) -> str:
    if not srcset:
        return ""
    # 选第一个 URL
    return srcset.split(",")[0].strip().split(" ")[0].strip('"').strip("'")

def extract_img_urls_from_img_elements(driver, base_url: str):
    """从页面上 class=img-center 的 <img> 元素提取图片 URL，兼容 src/srcset/data-*，
    也兼容 <picture><source srcset> 以及 background-image。"""
    urls = set()

    # 1) <img class="img-center"> 与 2) 父容器 .img-center 下的 img
    imgs = driver.find_elements(By.CSS_SELECTOR, 'img.img-center, .img-center img')

    # 3) <picture> 里 source[srcset]
    sources = driver.find_elements(By.CSS_SELECTOR, 'picture source[srcset]')

    # 4) 背景图写在 style 上
    bg_nodes = driver.find_elements(By.CSS_SELECTOR, '.img-center, .img-center *')

    for img in imgs:
        src = (
            img.get_attribute("src")
            or img.get_attribute("data-src")
            or img.get_attribute("data-original")
        )
        if not src:
            srcset = img.get_attribute("srcset")
            if srcset:
                src = pick_from_srcset(srcset)
        if not src:
            outer = img.get_attribute("outerHTML") or ""
            m = re.search(r'(?:src|data-src|data-original)\s*=\s*["\']([^"\']+)', outer, re.I)
            if m:
                src = m.group(1)
        if not src:
            continue
        src = src.split("?")[0]
        full = urljoin(base_url, src)
        if full.startswith("http"):
            urls.add(full)

    for s in sources:
        srcset = s.get_attribute("srcset")
        u = pick_from_srcset(srcset)
        if u:
            urls.add(urljoin(base_url, u.split("?")[0]))

    for n in bg_nodes:
        style = (n.get_attribute("style") or "")
        m = re.search(r'background(?:-image)?\s*:\s*url\(([^)]+)\)', style, re.I)
        if m:
            u = m.group(1).strip('"').strip("'")
            if u and not u.startswith('data:'):
                urls.add(urljoin(base_url, u.split("?")[0]))

    return urls

def product_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    pid = qs.get("product_id", [None])[0]
    if pid:
        return f"product_{pid}"
    tail = safe_name(os.path.basename(parsed.path) or "unknown")
    return f"product_{tail}"

def download(url: str, save_path: str, referer: str = None):
    for attempt in range(3):
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            if referer:
                headers["Referer"] = referer
            with requests.get(url, headers=headers, stream=True, timeout=25) as r:
                r.raise_for_status()
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
            print(f"  ✅ saved: {save_path}")
            return
        except Exception as e:
            print(f"  ⚠️ retry({attempt+1}) : {url} -> {e}")
            time.sleep(1.2 + random.random())
    print(f"  ❌ fail : {url}")

def unique_save_path(folder: str, img_url: str) -> str:
    fname = os.path.basename(img_url.split("?")[0]) or f"{int(time.time()*1000)}.jpg"
    fname = safe_name(fname)
    p = os.path.join(folder, fname)
    if os.path.exists(p):
        base, ext = os.path.splitext(fname)
        p = os.path.join(folder, f"{base}_{int(time.time()*1000)}{ext or '.jpg'}")
    return p

# ====== URL/页码工具 ======
def normalize_http_url(candidate: str, base_url: str):
    """把 candidate 标准化为绝对 URL，且必须是 http/https 协议；否则返回 None。"""
    if not candidate:
        return None
    c = candidate.strip()
    # 过滤无效协议
    if c in ("#", "javascript:void(0)", "void(0)"):
        return None
    lc = c.lower()
    if lc.startswith("javascript:") or lc.startswith("data:") or lc.startswith("mailto:"):
        return None
    # 合并相对/绝对
    c = urljoin(base_url, c)
    parsed = urlparse(c)
    if parsed.scheme not in ("http", "https"):
        return None
    return c

def get_page_index(url: str) -> int:
    """返回 URL 中 page 参数（默认 1），非法则按 1 处理"""
    try:
        qs = parse_qs(urlparse(url).query)
        return int(qs.get(PAGE_PARAM, ["1"])[0] or "1")
    except Exception:
        return 1

def _increment_page_url(url: str, step: int = 1) -> str:
    """兜底：基于 ?page= 递增生成下一页 URL（保持其他 query 不变）"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    cur = int(qs.get(PAGE_PARAM, [1])[0] or 1)
    nxt = cur + step
    qs[PAGE_PARAM] = [str(nxt)]
    flat = {}
    for k, v in qs.items():
        flat[k] = v if isinstance(v, list) and len(v) != 1 else (v[0] if isinstance(v, list) else v)
    query = urlencode(flat, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))

# ====== 文件夹去重 ======
def remove_empty_dirs(root_dir: str, remove_root: bool = False):
    """
    递归删除 root_dir 下的所有空文件夹。
    - remove_root=False 时，保留根目录本身（即使它是空的）。
    """
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
        # 若目录为空（不包含任何文件/子目录）
        try:
            if not os.listdir(dirpath):
                # 根目录是否保留
                if (dirpath == root_dir) and (not remove_root):
                    continue
                os.rmdir(dirpath)
                removed += 1
                print(f"🗑️ removed empty dir: {dirpath}")
        except Exception as e:
            print(f"⚠️ remove dir failed: {dirpath} -> {e}")
    print(f"Empty directories removed: {removed}")

# ====== 列表页抓取 + 分页 ======
def collect_product_links_from_current_page(driver):
    links = set()
    # 1) 直连: 带 product_id 参数
    for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="product_id="]'):
        href = normalize_http_url(a.get_attribute("href"), driver.current_url)
        if href:
            links.add(href)
    # 2) slug 形式，如 /product/xxx 或 /products/xxx
    for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/product/"] , a[href*="/products/"]'):
        href = normalize_http_url(a.get_attribute("href"), driver.current_url)
        if href:
            links.add(href)
    # 3) 卡片常见结构：包含产品卡 class 的链接
    for a in driver.find_elements(By.CSS_SELECTOR, 'a.product, a.product-card, .product a, .product-item a, .products a'):
        href = normalize_http_url(a.get_attribute("href"), driver.current_url)
        if href:
            links.add(href)
    return links

def find_next_page_url(driver, base_url):
    """增强版：支持 <i class="next">，并把页码严格限制在 1~3 之内"""
    def _limit_and_return(href: str):
        h = normalize_http_url(href, base_url)
        if not h:
            return None
        if get_page_index(h) > MAX_ALLOWED_PAGE:
            return None
        return h

    # 1) rel="next"
    el = driver.find_elements(By.CSS_SELECTOR, 'a[rel="next"]')
    if el:
        out = _limit_and_return(el[0].get_attribute('href'))
        if out:
            return out

    # 2) 常见 .next 链接
    el = driver.find_elements(By.CSS_SELECTOR, 'a.next, li.next a, .pagination-next a')
    if el:
        out = _limit_and_return(el[0].get_attribute('href'))
        if out:
            return out

    # 3) 特殊：<i class="next">，找最近祖先 <a> 的 href；没有就尝试点击后读取 current_url
    try:
        i_next = driver.find_element(By.CSS_SELECTOR, 'i.next')
        try:
            ancestor_a = i_next.find_element(By.XPATH, 'ancestor::a[1]')
        except Exception:
            ancestor_a = None

        if ancestor_a:
            out = _limit_and_return(ancestor_a.get_attribute('href'))
            if out:
                return out

        # 没有可用 href，尝试点击以触发跳转（若是前端路由）
        try:
            driver.execute_script("arguments[0].click();", ancestor_a or i_next)
            time.sleep(WAIT)
            cur_after_click = driver.current_url
            out = _limit_and_return(cur_after_click)
            if out and out != base_url:
                return out
        except Exception:
            pass
    except Exception:
        pass

    # 4) 数字分页：当前项的下一个兄弟
    current = None
    for li in driver.find_elements(By.CSS_SELECTOR, 'li.page-numbers, .pagination li, .pager li'):
        cls = (li.get_attribute('class') or '')
        if 'current' in cls or 'active' in cls:
            current = li
            break
    if current:
        try:
            nxt = current.find_element(By.XPATH, 'following-sibling::li[1]//a')
            out = _limit_and_return(nxt.get_attribute('href'))
            if out:
                return out
        except Exception:
            pass

    # 5) 兜底：按 ?page= 递增（仍然受 1~3 限制）
    try:
        guess = _increment_page_url(driver.current_url or base_url, step=1)
        out = _limit_and_return(guess)
        if out and out != base_url:
            return out
    except Exception:
        pass

    return None

def collect_product_links(driver, start_url: str):
    seen_pages = set()
    product_links = set()
    cur = normalize_http_url(start_url, start_url)
    pages = 0

    while cur and cur not in seen_pages and pages < MAX_PAGES:
        # —— 访问前保险：只抓 1~3 页 —— #
        if get_page_index(cur) > MAX_ALLOWED_PAGE:
            print(f"⚠️ 超过第 {MAX_ALLOWED_PAGE} 页，停止：{cur}")
            break

        print(f"📄 列表页：{cur}")
        seen_pages.add(cur)
        driver.get(cur)
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(WAIT)
        scroll_to_bottom(driver)
        # 抓本页产品链接
        product_links |= collect_product_links_from_current_page(driver)

        # 找下一页（兼容 <i class="next">）
        nxt = find_next_page_url(driver, cur)
        # —— 下一页保险：只抓 1~3 页 —— #
        if nxt and (nxt not in seen_pages) and get_page_index(nxt) <= MAX_ALLOWED_PAGE:
            cur = nxt
            pages += 1
            time.sleep(0.8 + random.random())
            continue
        break

    # —— 强化补抓：固定补 1/2/3 页，避免漏抓（不会越界） —— #
    try:
        parsed = urlparse(start_url)
        base_no_query = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment))
        candidates = [
            base_no_query,  # 第 1 页
            f"{base_no_query}?{PAGE_PARAM}=2",
            f"{base_no_query}?{PAGE_PARAM}=3",
        ]
        for c in candidates:
            c_norm = normalize_http_url(c, start_url)
            if c_norm and c_norm not in seen_pages and get_page_index(c_norm) <= MAX_ALLOWED_PAGE:
                print(f"📄（补抓）列表页：{c_norm}")
                seen_pages.add(c_norm)
                driver.get(c_norm)
                WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(WAIT)
                scroll_to_bottom(driver)
                product_links |= collect_product_links_from_current_page(driver)
                time.sleep(0.5 + random.random())
    except Exception:
        pass

    print(f"共发现产品链接：{len(product_links)}，跨越列表页：{len(seen_pages)}")
    return sorted(product_links)

# ====== 详情页抓图 ======
def crawl_one_product(driver, url: str, root: str):
    print(f"\n🧭 打开产品：{url}")
    try:
        driver.get(url)
    except Exception as e:
        print(f"❌ 无法打开：{e}")
        return 0

    WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(WAIT)

    # 懒加载滚动
    scroll_to_bottom(driver, pause=0.4, max_rounds=8)

    img_urls = extract_img_urls_from_img_elements(driver, url)
    if not img_urls:
        print("⚠️ 未找到 img-center 的图片")
        return 0

    pid = product_id_from_url(url)
    folder = os.path.join(root, pid)
    ensure_dir(folder)

    saved = 0
    for u in sorted(img_urls):
        sp = unique_save_path(folder, u)
        download(u, sp, referer=url)
        saved += 1
    return saved

# ====== 去重工具（精确 + 可选感知） ======
def _md5_of_file(path, chunk=1024 * 1024):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def _ahash(img: Image.Image, hash_size=8) -> int:
    """平均哈希（aHash），返回 64bit 整数。"""
    small = img.convert("L").resize((hash_size, hash_size), Image.BILINEAR)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for p in pixels:
        bits = (bits << 1) | (1 if p >= avg else 0)
    return bits

def _hamming_distance(x: int, y: int) -> int:
    return (x ^ y).bit_count()

def deduplicate_images(root_dir: str,
                       do_perceptual: bool = False,
                       ahash_threshold: int = 5,
                       dry_run: bool = False):
    """
    对 root_dir 下所有图片去重。
    - 先按 MD5 做“完全重复”去重；
    - do_perceptual=True 时，再用 aHash 做“近似重复”去重（汉明距离 <= ahash_threshold）。
      建议阈值 4~8，默认 5（偏保守）。
    - dry_run=True 时只打印不删除。
    """
    print("\n🧹 Start deduplication...")

    # 1) 收集所有图片路径
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
    all_imgs = []
    for base, _, files in os.walk(root_dir):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in exts:
                all_imgs.append(os.path.join(base, fn))

    print(f"Found {len(all_imgs)} images to check.")

    # 2) 精确去重（MD5）
    md5_map = {}
    exact_dups = []
    for p in all_imgs:
        try:
            d = _md5_of_file(p)
        except Exception as e:
            print(f"  ⚠️ hash fail: {p} -> {e}")
            continue
        if d in md5_map:
            exact_dups.append(p)
        else:
            md5_map[d] = p

    removed_exact = 0
    for p in exact_dups:
        print(f"  🗑️ exact dup: {p}")
        if not dry_run:
            try:
                os.remove(p)
                removed_exact += 1
            except Exception as e:
                print(f"    ⚠️ remove fail: {p} -> {e}")

    print(f"Exact duplicates removed: {removed_exact}")

    if not do_perceptual:
        print("Perceptual (aHash) dedup skipped.")
        print("✅ Deduplication done.\n")
        return

    # 3) 近似去重（aHash）
    # 重新列出剩余图片
    remain_imgs = []
    for base, _, files in os.walk(root_dir):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in exts:
                remain_imgs.append(os.path.join(base, fn))

    # 建立粗分桶：按宽高比与大小分段，减少比较次数
    size_buckets = defaultdict(list)
    for p in remain_imgs:
        try:
            sz = os.path.getsize(p)
            with Image.open(p) as im:
                w, h = im.size
            ratio_key = round(w / h, 2) if h else 0
            bin_key = (int(sz / 50_000), ratio_key)  # 50KB 一段
            size_buckets[bin_key].append(p)
        except Exception:
            size_buckets[("misc", 0)].append(p)

    removed_perc = 0
    seen_hashes = []  # [(hash_int, path)]
    for _, paths in size_buckets.items():
        for p in paths:
            try:
                with Image.open(p) as im:
                    h_int = _ahash(im, hash_size=8)
            except Exception as e:
                print(f"  ⚠️ aHash fail: {p} -> {e}")
                continue

            # 与已保留的图比对
            is_dup = False
            for h_keep, p_keep in seen_hashes:
                dist = _hamming_distance(h_int, h_keep)
                if dist <= ahash_threshold:
                    print(f"  🗑️ near-dup ({dist}): {p}  ~  {p_keep}")
                    if not dry_run:
                        try:
                            os.remove(p)
                            removed_perc += 1
                        except Exception as e:
                            print(f"    ⚠️ remove fail: {p} -> {e}")
                    is_dup = True
                    break
            if not is_dup:
                seen_hashes.append((h_int, p))

    print(f"Perceptual near-duplicates removed: {removed_perc}")
    print("✅ Deduplication done.\n")

# ====== 主流程 ======
def main():
    ensure_dir(SAVE_ROOT)
    driver = build_driver()

    try:
        product_links = collect_product_links(driver, BASE_LIST_URL)
        total_imgs = 0
        for link in product_links:
            total_imgs += crawl_one_product(driver, link, SAVE_ROOT)
            time.sleep(0.2 + random.random())
        print(f"\n🎉 完成。共下载图片：{total_imgs} 张。保存目录：{SAVE_ROOT}")
    finally:
        driver.quit()

    # —— 下载完成后做去重 —— #
    # 1) 只做精确去重（安全、零误删）
    deduplicate_images(SAVE_ROOT, do_perceptual=False)

    # —— 去重后清理空文件夹 —— #
    remove_empty_dirs(SAVE_ROOT, remove_root=False)  # 如需连根目录也删，设为 True


    # 2) 如需进一步去相似图，打开下面这一行（默认阈值 5，可在 4~8 之间调整）
    # deduplicate_images(SAVE_ROOT, do_perceptual=True, ahash_threshold=5)

if __name__ == "__main__":
    main()
