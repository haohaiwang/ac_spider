import asyncio
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://www.gree.com/cmsProduct/list/41"
CATEGORIES = ["挂式空调", "柜式空调", "特种空调"]  # 三个分页
DOWNLOAD_IMAGES = True  # 如需下载图片，设为 True
IMG_DIR = "gree_images"

# --- 一些通用工具 ---
def unique(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def looks_like_product_detail(href: str) -> bool:
    return "/cmsProduct/view/" in href

def normalize_img_url(src: str, base: str) -> str:
    if not src:
        return ""
    src = src.strip()
    # 常见懒加载属性兜底
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        return src
    return urljoin(base, src)

def sanitize_filename(name: str, replacement: str = "_") -> str:
    """
    清洗为安全的文件/文件夹名：去除控制字符、替换非法字符、修剪首尾空白与点
    """
    if not name:
        return ""
    # 去除控制字符
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    # Windows/Unix 常见非法字符
    name = re.sub(r'[<>:"/\\|?*]+', replacement, name)
    # 连续空白归一
    name = re.sub(r"\s+", " ", name).strip()
    # 避免只有点或结尾点/空格
    name = name.strip(" .")
    return name

async def scroll_to_bottom(page, step_px=1200, pause=0.25, max_rounds=20):
    last_height = await page.evaluate("() => document.body.scrollHeight")
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        await page.evaluate(f"window.scrollBy(0, {step_px});")
        await asyncio.sleep(pause)
        new_height = await page.evaluate("() => document.body.scrollHeight")
        if new_height <= last_height:
            # 再等一下给懒加载机会
            await asyncio.sleep(0.4)
            new_height = await page.evaluate("() => document.body.scrollHeight")
            if new_height <= last_height:
                break
        last_height = new_height

async def ensure_category_open(page):
    """有的站点类目下拉需要先点开 .allcategory-icon 才能点击具体类目"""
    try:
        icon = page.locator("span.allcategory-icon")
        if await icon.count() > 0 and await icon.is_visible():
            await icon.first.click()
            await asyncio.sleep(0.2)
    except Exception:
        pass

async def click_category(page, category_text: str):
    await ensure_category_open(page)
    # 直接用“文本定位”，必要时多尝试几次
    for _ in range(2):
        el = page.get_by_text(category_text, exact=True)
        if await el.count() > 0:
            await el.first.click()
            return True
        # 兜底：有些类目是 <a> 或 <li> 内文本
        el2 = page.locator(f"xpath=//*[text()='{category_text}']")
        if await el2.count() > 0:
            await el2.first.click()
            return True
        await asyncio.sleep(0.2)
    return False

async def click_view_more_until_exhausted(page, wait_timeout=60000):
    """反复点击“查看更多”，直到按钮消失或产品数不再增长"""
    while True:
        try:
            # 当前已渲染的“了解更多”详情链接数（用它判断是否加载了新卡片）
            prev = await page.locator("a[href*='/cmsProduct/view/']").count()
            more = page.locator("a.view-more.tdn", has_text="查看更多")
            if (await more.count()) == 0:
                # 兜底：按文本匹配
                more = page.get_by_text("查看更多", exact=True).locator("xpath=ancestor::a[1] | self::a")
                if (await more.count()) == 0:
                    break
            if not await more.first.is_visible():
                break
            await more.first.click()
            # 等到详情链接数变多或超时
            try:
                await page.wait_for_function(
                    "(prev) => document.querySelectorAll(\"a[href*='/cmsProduct/view/']\").length > prev",
                    arg=prev,
                    timeout=wait_timeout
                )
            except PlaywrightTimeoutError:
                # 点了也没新增，则视为加载完
                break
            await asyncio.sleep(0.4)
        except Exception:
            break

async def extract_detail_links(page, base: str):
    # 只保留 /cmsProduct/view/ 的详情页链接，过滤推荐区/底部重复
    hrefs = await page.eval_on_selector_all(
        "a[href*='/cmsProduct/view/']",
        "els => Array.from(new Set(els.map(e => e.href)))"
    )
    hrefs = [h for h in hrefs if looks_like_product_detail(h)]
    hrefs = unique(hrefs)
    return hrefs

async def get_text_content(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        if await loc.count() > 0:
            return (await loc.text_content() or "").strip()
    except Exception:
        pass
    return ""

async def extract_images_from_detail(page, detail_url: str):
    """优先取 div.clip img；若为空，再取“产品介绍”模块里的 img"""
    await scroll_to_bottom(page)  # 先触发懒加载
    imgs = set()

    # 1) div.clip 下的所有图片
    clip_imgs = await page.eval_on_selector_all(
        "div.clip img",
        """
        els => els.map(img => ({
            src: img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy') || '',
            srcset: img.getAttribute('srcset') || ''
        }))
        """
    )
    def unpack(imgrec):
        if imgrec.get("src"):
            return imgrec["src"]
        if imgrec.get("srcset"):
            # 取 srcset 中分辨率最高的那张
            parts = [p.strip().split(" ")[0] for p in imgrec["srcset"].split(",") if p.strip()]
            if parts:
                return parts[-1]
        return ""
    for it in clip_imgs:
        s = unpack(it)
        if s:
            imgs.add(s)

    # 2) 回退：找“产品介绍”模块的图片
    if not imgs:
        # a) 直接按标题中文定位
        intro_section = page.locator("xpath=//*[contains(@class,'product') or contains(@class,'intro') or contains(@class,'detail')][.//h3[contains(.,'产品介绍')] or .//h4[contains(.,'产品介绍')] or .//*[contains(text(),'产品介绍')]]")
        if await intro_section.count() > 0:
            extra_imgs = await intro_section.first.evaluate_all(
                "node => Array.from(node.querySelectorAll('img')).map(img => img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy') || '')"
            )
            for s in extra_imgs:
                if s:
                    imgs.add(s)

        # b) 再兜底：页面所有内容区的大图（避免把顶部 logo、图标抓进来，做个简单过滤）
        if not imgs:
            all_imgs = await page.eval_on_selector_all(
                "img",
                "els => els.map(img => ({src: img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy') || '', w: img.naturalWidth || 0, h: img.naturalHeight || 0}))"
            )
            for it in all_imgs:
                src = it.get("src") or ""
                w = it.get("w") or 0
                h = it.get("h") or 0
                # 过滤明显小图标
                if src and (w >= 200 or h >= 200 or src.endswith(".webp") or src.endswith(".jpg") or src.endswith(".png")):
                    imgs.add(src)

    # 规范化为绝对 URL
    imgs_abs = [normalize_img_url(s, detail_url) for s in imgs]
    # 去掉可能的空字符串与重复
    imgs_abs = [u for u in unique(imgs_abs) if u]
    return imgs_abs

async def maybe_download_images(image_urls, out_dir=IMG_DIR, timeout=60):
    os.makedirs(out_dir, exist_ok=True)
    import httpx
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        tasks = []
        for url in image_urls:
            # 用 URL 文件名，若无扩展名加上 .jpg
            name = os.path.basename(urlparse(url).path) or f"img_{int(time.time()*1000)}"
            if not os.path.splitext(name)[1]:
                name += ".jpg"
            dest = os.path.join(out_dir, name)
            tasks.append(asyncio.create_task(download_one(client, url, dest)))
        await asyncio.gather(*tasks)

async def download_one(client, url, dest):
    try:
        r = await client.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
    except Exception:
        pass

async def run():
    results = []  # {category, product_title, product_model, save_dir, product_url, image_url}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await context.new_page()

        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)

        for cat in CATEGORIES:
            print(f"== 处理类目: {cat} ==")
            # 点击类目
            ok = await click_category(page, cat)
            # 即便没点到，也许默认就是该类；继续走查看更多逻辑
            await click_view_more_until_exhausted(page)

            # 收集详情链接
            detail_links = await extract_detail_links(page, BASE_URL)
            print(f"  发现详情页 {len(detail_links)} 条")

            # 逐个详情页抓图（序号在每个类目内从 01 开始）
            for idx, durl in enumerate(detail_links, 1):
                try:
                    await page.goto(durl, wait_until="domcontentloaded", timeout=90000)

                    # 产品标题（通常在 h1/h2/h3 中）
                    title = await get_text_content(page, "h1") or await get_text_content(page, "h2") or await get_text_content(page, "h3")

                    # 产品型号来自 #product-details-name；若无则回退到标题
                    model_raw = await get_text_content(page, "#product-details-name")
                    model = sanitize_filename(model_raw) or sanitize_filename(title) or "Unknown"
                    cat_safe = sanitize_filename(cat)
                    folder_name = f"{idx:02d}{model}"
                    save_dir = os.path.join(IMG_DIR, cat_safe, folder_name)

                    imgs = await extract_images_from_detail(page, durl)

                    # 下载到该产品专属文件夹
                    if DOWNLOAD_IMAGES and imgs:
                        await maybe_download_images(imgs, out_dir=save_dir)

                    # 记录
                    for u in imgs:
                        results.append({
                            "category": cat,
                            "product_title": title,
                            "product_model": model,
                            "save_dir": save_dir,
                            "product_url": durl,
                            "image_url": u
                        })

                    print(f"    [{idx}/{len(detail_links)}] {title or '(无标题)'} | 型号: {model} -> {len(imgs)} 图，已保存至 {save_dir}")
                except Exception as e:
                    print(f"    打开或解析失败: {durl} | {e}")

            # 回到列表页，准备下一个类目
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)

        await browser.close()

    # 保存 CSV
    if results:
        df = pd.DataFrame(results)
        df.to_csv("gree_ac_images.csv", index=False, encoding="utf-8-sig")
        print(f"已保存：gree_ac_images.csv，共 {len(results)} 条图片记录")
    else:
        print("未抓到任何图片，请检查选择器或再次运行。")

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
