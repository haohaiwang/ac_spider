# midea_products_scraper.py
# 目标：抓取 https://www.mideahk.com/空氣流通/冷氣機 列表第 1~3 页的所有产品链接，
#       打开详情页后下载 class="img-center" 的图片（支持 src/srcset/data-*），
#       并按产品 slug 分目录保存。

import os
import re
import time
import requests
from urllib.parse import urlparse
from typing import List, Set

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
# 分页（最小改动：只要调这里就好）
LIST_PAGES = [
    BASE_LIST_URL,
    f"{BASE_LIST_URL}?page=2",
    f"{BASE_LIST_URL}?page=3",
]

PAGE_LOAD_TIMEOUT = 35          # 页面加载超时（秒）
WAIT = 2.0                      # 页面渲染额外等待（秒）
SCROLL_STEPS = 8                # 下拉次数，触发懒加载
SCROLL_PAUSE = 0.8              # 每次下拉后的等待
HEADLESS = True                 # 调试需要看浏览器过程可设为 False
REQUEST_TIMEOUT = 20            # 图片下载超时（秒）

# ====== 通用 ======
def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    return s.strip()[:100]

def build_driver():
    chrome_options = Options()
    if HEADLESS:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1440,1000")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver

def js_ready(driver):
    try:
        return driver.execute_script("return document.readyState") == "complete"
    except Exception:
        return False

def try_accept_cookies(driver):
    # 常见 Cookie 按钮 id / 文案 兜底点击（忽略异常）
    candidates = [
        (By.ID, "onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, "button[aria-label='Accept All']"),
        (By.XPATH, "//button[contains(., 'Accept') or contains(., '同意') or contains(., '接受')]"),
    ]
    for how, selector in candidates:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((how, selector)))
            btn.click()
            time.sleep(0.5)
            break
        except Exception:
            pass

def smooth_scroll(driver, steps=SCROLL_STEPS, pause=SCROLL_PAUSE):
    last_h = 0
    for _ in range(steps):
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight/3);")
        time.sleep(pause)
        try:
            new_h = driver.execute_script("return document.body.scrollHeight")
        except Exception:
            new_h = last_h
        if new_h == last_h:
            # 再补一次到最底
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(pause)
        last_h = new_h

# ====== 列表抓取 ======
def collect_product_links(driver, list_url: str) -> List[str]:
    print(f"👉 打开列表页: {list_url}")
    driver.get(list_url)

    # 等待文档 ready & 处理 Cookie
    WebDriverWait(driver, 20).until(lambda d: js_ready(d))
    time.sleep(WAIT)
    try_accept_cookies(driver)

    # 分段下拉，触发懒加载
    smooth_scroll(driver)

    # 多选择器兜底：不同模板下产品卡片结构可能不同
    selector_candidates = [
        "div.proBox a",
        "div.product-thumb a",
        "div.product-layout a",
        "div.product-block a",
        "ul.products li a",
        "a.product_img",
        # 直接用链接特征（分类路径可能编码/未编码）
        "a[href*='%E5%86%B7%E6%B0%A3%E6%A9%9F/']",
        "a[href*='/冷氣機/']",
    ]

    anchors = []
    for css in selector_candidates:
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, css))
            )
            found = driver.find_elements(By.CSS_SELECTOR, css)
            if found:
                anchors = found
                break
        except Exception:
            continue

    links: Set[str] = set()

    # 优先从选到的元素里提取
    for a in anchors:
        href = a.get_attribute("href")
        if not href:
            continue
        if is_ac_product_url(href):
            links.add(href)

    # 如果 CSS 没选到或数量太少，用正则从整页源码兜底
    if len(links) < 6:  # 阈值可调
        html = driver.page_source or ""
        # 同时匹配编码/未编码两种路径；过滤明显的列表/搜索页
        pattern = re.compile(
            r'https?://(?:www\.)?mideahk\.com/[^\s"\'<>]*?(?:%E5%86%B7%E6%B0%A3%E6%A9%9F|冷氣機)/[^\s"\'<>/]+/?',
            flags=re.IGNORECASE
        )
        for m in pattern.findall(html):
            if is_ac_product_url(m):
                links.add(m)

    print(f"  收集到 {len(links)} 个产品链接")
    return list(links)

def is_ac_product_url(url: str) -> bool:
    # 粗略排除分类页、分页、锚点等，只保留像 /冷氣機/<slug> 的详情页
    if "page=" in url:
        return False
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    return (len(parts) >= 2) and (("冷氣機" in parts[0]) or ("%E5%86%B7%E6%B0%A3%E6%A9%9F" in parts[0]))

# ====== 详情抓图 ======
def download_image(url, save_path):
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        print(f"    ❌ 下载失败 {url}: {e}")
        return False

def crawl_one_product(driver, product_url, save_root):
    print(f"\n=== 抓取产品: {product_url} ===")
    driver.get(product_url)
    WebDriverWait(driver, 20).until(lambda d: js_ready(d))
    time.sleep(WAIT)

    # 目录名用最后一级 slug
    product_id = product_url.rstrip("/").split("/")[-1]
    product_dir = os.path.join(save_root, safe_name(product_id))
    ensure_dir(product_dir)

    # 可能有懒加载，先滚动一遍
    smooth_scroll(driver, steps=5, pause=0.6)

    imgs = driver.find_elements(By.CSS_SELECTOR, "img.img-center")
    # 兜底：有些详情页可能不是这个类名
    if not imgs:
        imgs = driver.find_elements(By.CSS_SELECTOR, "img")

    seen = set()
    count = 0
    for img in imgs:
        for attr in ["src", "srcset", "data-src", "data-original"]:
            link = img.get_attribute(attr)
            if not link:
                continue
            for u in re.split(r",\s*", link):
                u = u.strip().split(" ")[0]
                if not u or u in seen:
                    continue
                if u.startswith("//"):
                    u = "https:" + u
                seen.add(u)
                fname = safe_name(os.path.basename(urlparse(u).path) or f"{len(seen)}.jpg")
                save_path = os.path.join(product_dir, fname)
                if download_image(u, save_path):
                    count += 1
    print(f"  ✅ 下载 {count} 张图片 -> {product_dir}")
    return count

# ====== 主逻辑 ======
def main():
    ensure_dir(SAVE_ROOT)
    driver = build_driver()
    try:
        # 收集三页所有产品链接（去重）
        links_all: Set[str] = set()
        for list_url in LIST_PAGES:
            try:
                links_all.update(collect_product_links(driver, list_url))
            except Exception as e:
                print(f"⚠️ 列表页失败 {list_url}: {e}")

        product_links = sorted(links_all)
        print(f"\n总计产品链接：{len(product_links)}")

        total_imgs = 0
        for link in product_links:
            try:
                total_imgs += crawl_one_product(driver, link, SAVE_ROOT)
            except Exception as e:
                print(f"⚠️ 产品失败 {link}: {e}")
        print(f"\n🎉 完成。共下载图片：{total_imgs} 张。保存目录：{SAVE_ROOT}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
