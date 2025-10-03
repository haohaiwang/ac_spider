# -*- coding: utf-8 -*-
"""
Haier 空调产品图片抓取（Playwright 同步版，仅限 detail_top_product_preview 容器）
- 列表页: https://www.haier.com/air_conditioners/
- 翻页: a.next（或“加载更多”），最多 15 页（可改 MAX_PAGES）
- 详情页: 只抓 <div class="detail_top_product_preview"> 容器内的图片
  - 动态点击容器内缩略图，触发大图加载
  - 采集 zoomimg / data-zoomimg / data-src / srcset(取最大) / src（优先高分）
- 以产品为文件夹下载所有图片

已修复/优化：
1) request.get 的 timeout 用毫秒（不再 /1000）
2) 规范化 URL，折叠路径中的双斜杠
3) 下载加入重试 + 退避
4) 严格限定 DOM 范围：仅在 div.detail_top_product_preview 内收集
"""

import os
import re
import time
import random
import pathlib
import posixpath
import urllib.parse
from typing import List, Set, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://www.haier.com/air_conditioners/"
OUTPUT_ROOT = "haier_ac_images"   # 下载根目录
MAX_PAGES = 15              # 最多翻 15 页
HEADLESS = True             # 调试可改为 False
SLOW_MO_MS = 0              # 调试可设 200-400 观察动作
PAGE_TIMEOUT = 30_000       # 页面等待超时(ms)
REQUEST_TIMEOUT = 60_000    # 单个图片请求超时(ms)
SCROLL_PAUSE = (400, 900)   # 滚动与加载的随机等待范围(ms)

# 选择器
SEL_ALLCATEGORY_ICON = "span.allcategory-icon"
SEL_NEXT_PAGE = "a.next"          # “下一页”
SEL_LOAD_MORE = "text=加载更多"    # “加载更多”

# 仅抓 air_conditioners 子目录下的详情页 .shtml
JS_GET_PRODUCT_LINKS = """
() => {
  const anchors = Array.from(document.querySelectorAll('a[href*="/air_conditioners/"]'));
  const urls = anchors
    .map(a => a.href)
    .filter(href => /\\/air_conditioners\\/\\d{8}_\\d+\\.shtml/i.test(href));
  return Array.from(new Set(urls));
}
"""

# ！！！只在这个容器内取图
DETAIL_SCOPE = "div.detail_top_product_preview"

# 在指定 scope 内提取图片 URL 的优先级（zoomimg > data-zoomimg > data-src > srcset(取最大) > src）
JS_COLLECT_SCOPE_IMG_URLS = f"""
(scopeSel) => {{
  const scope = document.querySelector(scopeSel);
  if (!scope) return [];
  const imgs = Array.from(scope.querySelectorAll('img'));
  const takeFromSrcset = (srcset) => {{
    try {{
      const parts = srcset.split(',').map(s=>s.trim());
      const tuples = parts.map(p => {{
        const m = p.match(/(\\S+)\\s+(\\d+)w/);
        return m ? [m[1], parseInt(m[2], 10)] : [p.split(' ')[0], 0];
      }});
      tuples.sort((a,b)=>b[1]-a[1]);
      return tuples.length ? tuples[0][0] : null;
    }} catch (e) {{ return null; }}
  }};
  const urls = imgs.map(img => {{
    return img.getAttribute('zoomimg') ||
           img.getAttribute('data-zoomimg') ||
           img.getAttribute('data-src') ||
           (img.getAttribute('srcset') ? takeFromSrcset(img.getAttribute('srcset')) : null) ||
           img.getAttribute('src');
  }}).filter(Boolean);
  return Array.from(new Set(urls));
}}
"""

# 在 scope 内获取缩略图（用于点击切换大图/变种）
JS_GET_SCOPE_THUMBS = f"""
(scopeSel) => {{
  const scope = document.querySelector(scopeSel);
  if (!scope) return [];
  // 常见缩略图容器：small/slider/thumb/swiper 等（但都限定在 scope 内）
  const imgs = Array.from(scope.querySelectorAll('img'));
  return imgs.filter(i => i.width > 0 || i.height > 0).map((img) => {{
    const rect = img.getBoundingClientRect();
    return {{
      x: rect.x + window.scrollX + (img.clientWidth/2),
      y: rect.y + window.scrollY + (img.clientHeight/2),
      src: img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('srcset') || ''
    }};
  }});
}}
"""

def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\\s+", " ", name).strip()
    return name or "untitled"

def url_filename(url: str, idx: Optional[int] = None) -> str:
    path = urllib.parse.urlparse(url).path
    base = pathlib.Path(path).name or f"image_{int(time.time()*1000)}.jpg"
    if idx is not None:
        stem = pathlib.Path(base).stem
        suf  = pathlib.Path(base).suffix or ".jpg"
        return f"{stem}_{idx}{suf}"
    return base

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def sleep_ms(ms: int):
    time.sleep(ms/1000.0)

def polite_pause():
    sleep_ms(random.randint(*SCROLL_PAUSE))

def scroll_to_bottom(page):
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    polite_pause()

def fetch_text(page, selectors: List[str]) -> str:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            txt = el.inner_text(timeout=1500).strip()
            if txt:
                return txt
        except PWTimeoutError:
            continue
        except Exception:
            continue
    return ""

def get_product_title(page) -> str:
    title = fetch_text(page, [
        "h1", ".title h1", ".pro_tit", ".detail_top_product_title h1",
        "h1.product-title", ".product-title", ".detail_title h1"
    ])
    if not title:
        title = fetch_text(page, [".breadcrumb li:last-child", ".crumbs li:last-child"])
    return sanitize_filename(title) or "haier_product"

def normalize_url(base_page_url: str, raw: str) -> str:
    """绝对化 + 折叠路径中的多余斜杠（不动 scheme/netloc）"""
    if not raw:
        return ""
    u = raw.strip()
    if u.startswith("//"):
        u = "https:" + u
    if not u.lower().startswith(("http://", "https://")):
        u = urllib.parse.urljoin(base_page_url, u)
    p = urllib.parse.urlparse(u)
    norm_path = posixpath.normpath(p.path).replace("//", "/")
    if p.path.endswith("/") and not norm_path.endswith("/"):
        norm_path += "/"
    u = urllib.parse.urlunparse((p.scheme, p.netloc, norm_path, p.params, p.query, p.fragment))
    return u

def collect_images_in_scope(page, product_url: str) -> List[str]:
    """仅在 DETAIL_SCOPE 内收集，并通过点击 scope 内缩略图触发更多图。"""
    urls: List[str] = []

    # 1) 先收集 scope 内已有的大图/图片
    try:
        cur = page.evaluate(JS_COLLECT_SCOPE_IMG_URLS, DETAIL_SCOPE)
        urls.extend(cur or [])
    except Exception:
        pass

    # 2) 在 scope 内点击可能的缩略图，等待主图变化，再收集
    try:
        thumbs = page.evaluate(JS_GET_SCOPE_THUMBS, DETAIL_SCOPE) or []
        if thumbs:
            try:
                page.locator(DETAIL_SCOPE).first.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            polite_pause()

        def scope_signature() -> str:
            # 用于判断 scope 内图片是否发生变化
            try:
                return page.evaluate("""
                (scopeSel) => {
                  const scope = document.querySelector(scopeSel);
                  if (!scope) return '';
                  const img = scope.querySelector('img');
                  if (!img) return '';
                  return (img.currentSrc || img.src || img.getAttribute('data-src') || '') + '|' + (img.getAttribute('zoomimg') || '');
                }""", DETAIL_SCOPE) or ""
            except Exception:
                return ""

        for t in thumbs:
            try:
                before = scope_signature()
                page.mouse.click(t["x"], t["y"])
                for _ in range(20):
                    polite_pause()
                    after = scope_signature()
                    if after and after != before:
                        break
                cur = page.evaluate(JS_COLLECT_SCOPE_IMG_URLS, DETAIL_SCOPE)
                urls.extend(cur or [])
            except Exception:
                continue
    except Exception:
        pass

    # 3) 规范化 & 去重
    final_urls: List[str] = []
    seen: Set[str] = set()
    for u in urls:
        if not u:
            continue
        u = u.strip()
        if not u or u in ("about:blank",) or u.startswith("data:"):
            continue
        if u.startswith("//"):
            u = "https:" + u
        if u not in seen:
            seen.add(u)
            final_urls.append(u)

    return final_urls

def download_images_via_api(api, urls: List[str], out_dir: str, referer: str):
    ensure_dir(out_dir)

    def backoff_sleep(attempt: int):
        # 200ms, 600ms, 1400ms, 3000ms + 抖动
        base = [0.2, 0.6, 1.4, 3.0]
        t = base[min(attempt, len(base)-1)] + random.random() * 0.3
        time.sleep(t)

    for i, raw in enumerate(urls, 1):
        url = normalize_url(referer, raw)
        if not url:
            continue

        fname = url_filename(url, idx=i)
        fpath = os.path.join(out_dir, fname)
        if os.path.exists(fpath):
            continue

        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                resp = api.get(
                    url,
                    timeout=REQUEST_TIMEOUT,  # 单位：毫秒
                    headers={
                        "Referer": referer,
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                    fail_on_status_code=False,
                )

                if resp.ok:
                    with open(fpath, "wb") as f:
                        f.write(resp.body())
                    print(f"[OK] {fname}")
                    break
                else:
                    print(f"[WARN] HTTP {resp.status} -> {url}")
                    if attempt < max_attempts - 1:
                        backoff_sleep(attempt)
            except Exception as e:
                if attempt < max_attempts - 1:
                    print(f"[RETRY] {url} -> {e}")
                    backoff_sleep(attempt)
                else:
                    print(f"[ERR] download fail after retries: {url} -> {e}")

def click_if_visible(page, selector: str, timeout_ms: int = 1500) -> bool:
    try:
        loc = page.locator(selector)
        if loc.count() > 0:
            loc.first.click(timeout=timeout_ms)
            polite_pause()
            return True
    except PWTimeoutError:
        return False
    except Exception:
        return False
    return False

def try_next_page(page) -> bool:
    """优先点击 '下一页'，否则尝试 '加载更多'；返回是否成功翻页/加载更多。"""
    if click_if_visible(page, SEL_NEXT_PAGE, 2500):
        return True
    if click_if_visible(page, "text=下一页", 2500):
        return True
    if click_if_visible(page, SEL_LOAD_MORE, 2500):
        scroll_to_bottom(page)
        return True
    return False

def main():
    os.makedirs(OUTPUT_ROOT, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO_MS)
        context = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        api = p.request.new_context()

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # 点击筛选入口（若存在）
        click_if_visible(page, SEL_ALLCATEGORY_ICON, 1500)

        collected_all: Set[str] = set()
        page_index = 1

        while page_index <= MAX_PAGES:
            print(f"\n=== 抓取列表第 {page_index} 页 ===")

            # 触发懒加载，多滚几次
            for _ in range(3):
                scroll_to_bottom(page)

            # 当前页产品详情链接
            try:
                links: List[str] = page.evaluate(JS_GET_PRODUCT_LINKS) or []
            except Exception:
                links = []

            new_links = [u for u in links if u not in collected_all]
            print(f"[INFO] 发现 {len(new_links)} 个新产品链接，共{len(links)}个（去重后累计 {len(collected_all) + len(new_links)}）")

            # 逐个产品抓图（仅限 detail_top_product_preview 容器）
            for product_url in new_links:
                try:
                    print(f"\n--- 抓取产品：{product_url} ---")
                    dpage = context.new_page()
                    dpage.set_default_timeout(PAGE_TIMEOUT)
                    dpage.goto(product_url, wait_until="domcontentloaded")
                    dpage.wait_for_load_state("networkidle")

                    # 将目标容器滚入视口，触发容器内懒加载
                    try:
                        dpage.locator(DETAIL_SCOPE).first.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    polite_pause()

                    title = get_product_title(dpage)
                    folder = os.path.join(OUTPUT_ROOT, title)
                    ensure_dir(folder)
                    print(f"[INFO] 文件夹：{folder}")

                    img_urls = collect_images_in_scope(dpage, product_url)
                    print(f"[INFO] 收到容器内图片 {len(img_urls)} 张")
                    download_images_via_api(api, img_urls, folder, referer=product_url)

                    dpage.close()
                    collected_all.add(product_url)
                except PWTimeoutError as e:
                    print(f"[WARN] 打开产品页超时: {product_url} -> {e}")
                except Exception as e:
                    print(f"[ERR] 抓取产品失败: {product_url} -> {e}")

            # 下一页
            page_index += 1
            if page_index > MAX_PAGES:
                print("[INFO] 达到最大页数限制，结束。")
                break

            moved = try_next_page(page)
            if not moved:
                print("[INFO] 没有发现可点击的下一页/加载更多，结束。")
                break

        browser.close()

if __name__ == "__main__":
    main()
