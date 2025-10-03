#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daikin RA Product crawler (ids 50-80)
- For each product id, click each color selector (supports <a href="?color=..."> and span.color)
- After each click (or navigation), scrape ONLY images under <div class="swiper-wrapper">
- Save images grouped by variant index (not by color names)

Requirements (pre-installed): selenium, webdriver_manager, requests
"""

import os
import time
import shutil
from urllib.parse import urljoin, urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException, StaleElementReferenceException,
    ElementClickInterceptedException, JavascriptException
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------- Config ----------------------
BASE_URL = "https://www.daikin-china.com.cn/ra/raProductDetail/{id}"
START_ID = 50
END_ID = 80

ROOT_OUTPUT_DIR = "daikin_images"   # 所有产品共用一个根目录
HEADLESS = True

# ⬇⬇ 适度缩短等待，避免长时间空转
PAGE_READY_SECONDS = 15
CLICK_WAIT_SECONDS = 12
REQUEST_TIMEOUT = 30

# ---------------------- Helpers ----------------------
def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def guess_ext_from_url(u: str) -> str:
    path = urlparse(u).path
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ext
    return ".jpg"

def download_image(session: requests.Session, url: str, dest_path: str):
    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36")
        }
        with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            tmp = dest_path + ".part"
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r.raw, f)
            os.replace(tmp, dest_path)
        print(f"[saved] {dest_path}")
    except Exception as e:
        print(f"[warn] download failed: {url} -> {e}")

def build_driver():
    chrome_opts = ChromeOptions()
    if HEADLESS:
        chrome_opts.add_argument("--headless=new")

    # —— 基础稳定性/速度 —— #
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--disable-gpu")
    chrome_opts.add_argument("--disable-dev-shm-usage")
    chrome_opts.add_argument("--window-size=1400,900")
    chrome_opts.add_argument("--lang=zh-CN,zh")
    chrome_opts.set_capability("pageLoadStrategy", "eager")

    # —— 静默 & 减少无关特性带来的报错/耗时 —— #
    chrome_opts.add_argument("--log-level=3")
    chrome_opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    # 禁 WebGL / 3D，避免 fallback 报警告和额外初始化
    chrome_opts.add_argument("--disable-webgl")
    chrome_opts.add_argument("--disable-3d-apis")
    # 限制 WebRTC，避免 STUN/DNS 报错与重试
    chrome_opts.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns,MediaRouter")
    chrome_opts.add_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    # 降噪：禁用后台网络与部分不相关特性
    chrome_opts.add_argument("--disable-background-networking")
    chrome_opts.add_argument("--disable-sync")
    chrome_opts.add_argument("--metrics-recording-only")
    chrome_opts.add_argument("--no-first-run")
    chrome_opts.add_argument("--safebrowsing-disable-auto-update")

    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_opts)
    driver.set_page_load_timeout(PAGE_READY_SECONDS)
    driver.implicitly_wait(2)  # 轻微下调
    return driver

def wait_page_ready(driver):
    WebDriverWait(driver, PAGE_READY_SECONDS).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )
    # 轻量缓冲（原来 1.2s）
    time.sleep(0.4)

def robust_click(driver, elem):
    driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", elem)
    time.sleep(0.1)
    try:
        elem.click()
    except (ElementClickInterceptedException, StaleElementReferenceException):
        driver.execute_script("arguments[0].click();", elem)

def find_color_elements(driver):
    """
    覆盖两类切色控件：
    1) <a href="?color=...">（67 等页常见）
    2) 旧的 span.color / class 包含 color 的元素
    """
    selectors = [
        "a[href*='?color=']",                # 显式颜色链接
        "a.color", "li.color", "div.color",  # 常见主题的额外容器
        "span.color",
        "span[class*='color']",
        "//a[contains(@href,'?color=')]",    # XPATH 兜底
        "//span[contains(@class,'color')]",
    ]
    seen_ids = set()
    results = []
    for sel in selectors:
        try:
            elems = (driver.find_elements(By.XPATH, sel)
                     if sel.startswith("//")
                     else driver.find_elements(By.CSS_SELECTOR, sel))
            for e in elems:
                key = e.id if hasattr(e, "id") else id(e)
                if key not in seen_ids and e.size.get("width", 0) >= 8 and e.size.get("height", 0) >= 8:
                    seen_ids.add(key)
                    results.append(e)
        except Exception:
            pass
    # 去重（按 outerHTML 片段）
    uniq, html_fps = [], set()
    for e in results:
        try:
            fp = (e.get_attribute("outerHTML") or "")[:160]
            h = hash(fp)
            if h not in html_fps:
                html_fps.add(h)
                uniq.append(e)
        except Exception:
            uniq.append(e)
    return uniq

def js_get_swiper_imgs(driver):
    """
    只在 <div class="swiper-wrapper"> 内抓 <img>，不依赖 data-v-*。
    """
    script = r"""
    const wrap = document.querySelector('div.swiper-wrapper');
    if (!wrap) return [];
    const imgs = Array.from(wrap.querySelectorAll('img'));
    const out = [];
    const seen = new Set();
    for (const img of imgs) {
        const s = img.currentSrc || img.src || img.getAttribute('data-src') || '';
        if (!s || s.startsWith('data:')) continue;
        if (!seen.has(s)) {
            seen.add(s);
            out.push(s);
        }
    }
    return out;
    """
    try:
        return driver.execute_script(script) or []
    except JavascriptException:
        return []

def collect_swiper_image_urls(driver):
    urls = js_get_swiper_imgs(driver)
    # 标准化 + 去重
    out, seen = [], set()
    for u in urls:
        full = urljoin(driver.current_url, u)
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out

def wait_swiper_change(driver, prev_urls, prev_url_str, timeout=CLICK_WAIT_SECONDS):
    """
    既检测 swiper 图片变化，也检测 URL 变化；
    如果发生 URL 跳转，则等待新页 ready 且 swiper 有图。
    """
    t0 = time.time()
    prev_set = set(prev_urls or [])
    while time.time() - t0 < timeout:
        # 等待新的图片加载
        now_urls = collect_swiper_image_urls(driver)
        now_set = set(now_urls)
        
        # 如果图片列表发生变化，返回新的图片链接
        if now_set != prev_set:
            return now_urls
        
        # 如果 URL 已经发生变化，说明切换了颜色，等待新图片加载
        if driver.current_url != prev_url_str:
            try:
                wait_page_ready(driver)  # 确保页面完全加载
            except Exception:
                pass
            
            # 等待图像资源更新，最多等待 timeout 秒
            t1 = time.time()
            while time.time() - t1 < timeout:
                now_urls = collect_swiper_image_urls(driver)
                if now_urls:
                    return now_urls
                time.sleep(0.3)  # 稍微增加间隔等待页面加载完毕

            # 如果超时没有找到新的图像，尝试直接抓取
            return collect_swiper_image_urls(driver)
        
        time.sleep(0.3)
    
    return collect_swiper_image_urls(driver)


def has_swiper_or_color_quick(driver):
    """
    快速探测：最多 3 秒内每 250ms 检查一次。
    只要出现 swiper-wrapper 或 颜色控件，即认为有内容可抓。
    """
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            has_wrap = driver.execute_script("return !!document.querySelector('div.swiper-wrapper');")
        except Exception:
            has_wrap = False
        if has_wrap:
            return True
        # 颜色控件有时更早出现
        if find_color_elements(driver):
            return True
        time.sleep(0.25)
    return False

# ---------------------- Per-product ----------------------
def process_single_product(driver, session, pid: int):
    url = BASE_URL.format(id=pid)
    print(f"\n[open] id={pid} -> {url}")
    try:
        driver.get(url)
    except Exception as e:
        print(f"[skip] id={pid} open failed: {e}")
        return

    try:
        wait_page_ready(driver)
    except Exception:
        pass

    # ⬇⬇ 快速空页判断（提速关键）
    if not has_swiper_or_color_quick(driver):
        print(f"[skip] id={pid} has no images and no color selectors (quick check)")
        return

    # 初始 swiper 图片（默认颜色）
    base_swiper_urls = collect_swiper_image_urls(driver)
    # 找颜色按钮
    color_elems = find_color_elements(driver)

    # 如果两者都空，再做一次兜底判定（极少数慢加载）
    if not base_swiper_urls and not color_elems:
        time.sleep(0.8)
        base_swiper_urls = collect_swiper_image_urls(driver)
        color_elems = find_color_elements(driver)
        if not base_swiper_urls and not color_elems:
            print(f"[skip] id={pid} has no images and no color selectors")
            return

    output_dir = os.path.join(ROOT_OUTPUT_DIR, f"product_{pid}")
    ensure_dir(output_dir)

    # 先保存默认（未点击）一组
    if base_swiper_urls:
        variant_dir = os.path.join(output_dir, f"variant_{1:03d}")
        ensure_dir(variant_dir)
        for i, u in enumerate(base_swiper_urls, 1):
            ext = guess_ext_from_url(u)
            dest = os.path.join(variant_dir, f"variant_{1:03d}_{i:03d}{ext}")
            download_image(session, u, dest)

    # 依次点击颜色并抓取对应 swiper-wrapper 下的图片
    for idx in range(len(color_elems)):
        # 每次重找，防止 Vue 重渲染导致引用失效
        elems_now = find_color_elements(driver)
        if not elems_now or idx >= len(elems_now):
            break
        elem = elems_now[idx]

        prev_url = driver.current_url
        prev_swiper_urls = collect_swiper_image_urls(driver)

        print(f"[click] id={pid} color index {idx+1}/{len(color_elems)}")
        try:
            robust_click(driver, elem)
        except Exception as e:
            print(f"[warn] id={pid} click failed at {idx+1}: {e}; try JS click.")
            try:
                driver.execute_script("arguments[0].click();", elem)
            except Exception as e2:
                print(f"[error] id={pid} JS click also failed: {e2}")
                continue

        # 等待 swiper 中的图片或 URL 发生变化
        cur_urls = wait_swiper_change(driver, prev_swiper_urls, prev_url, timeout=CLICK_WAIT_SECONDS)
        print(f"[info] id={pid} swiper images after click: {len(cur_urls)}")

        # 保存（以变体索引命名，不使用颜色名）
        variant_idx = idx + 1  # 1 已用于默认
        variant_dir = os.path.join(output_dir, f"variant_{variant_idx:03d}")
        ensure_dir(variant_dir)

        if not cur_urls:
            print(f"[warn] id={pid} no images found for variant {variant_idx:03d}")
            continue

        # 去重（相对本变体）
        seen = set()
        ordered = []
        for u in cur_urls:
            if u not in seen:
                seen.add(u)
                ordered.append(u)

        for i, u in enumerate(ordered, 1):
            ext = guess_ext_from_url(u)
            dest = os.path.join(variant_dir, f"variant_{variant_idx:03d}_{i:03d}{ext}")
            download_image(session, u, dest)

# ---------------------- Main ----------------------
def main():
    ensure_dir(ROOT_OUTPUT_DIR)
    session = requests.Session()
    driver = build_driver()
    try:
        for pid in range(START_ID, END_ID + 1):
            try:
                process_single_product(driver, session, pid)
            except Exception as e:
                print(f"[error] id={pid} unexpected error: {e}")
                continue
        print(f"\n[done] images saved under: {os.path.abspath(ROOT_OUTPUT_DIR)}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
