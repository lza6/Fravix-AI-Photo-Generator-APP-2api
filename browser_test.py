"""
demo.html 真实浏览器路径验证（Playwright/Chromium）
覆盖: 首屏加载 → 风格库渲染 → 选风格 → 上传图片 → 点生成 → 出图 → 统计面板刷新
产出: browser_demo.png / browser_result.png
"""

import os
import sys
from pathlib import Path
import time

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8955/"
IMG = os.environ.get("FRAVIX_TEST_IMAGE", str(Path(__file__).with_name("media") / "test_face_real.jpg"))


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(URL, wait_until="networkidle", timeout=45000)
        page.wait_for_selector(".style", timeout=20000)
        n = page.locator(".style").count()
        print(f"[1] 风格卡渲染 {n} 个")
        assert n > 100, f"风格数量异常: {n}"

        badge = page.locator("#hBadge").inner_text()
        print(f"[2] 顶栏状态: {badge}")
        assert "风格" in badge

        page.locator('.style[data-id="ai_lookbook"]').click()
        route = page.locator("#hRoute").inner_text()
        print(f"[3] 选中风格 → {route}")
        assert "image" in route

        page.set_input_files("#file", IMG)
        page.wait_for_selector("#prev:not([hidden])", timeout=10000)
        print("[4] 图片已载入预览")

        page.screenshot(path="browser_demo.png", full_page=True)

        page.click("#go")
        print("[5] 已点击生成，等待上游出图…")
        t0 = time.time()
        page.wait_for_selector("#out img, #out video", timeout=240000)
        page.wait_for_function("() => { const i=document.querySelector('#out img'); return i && i.complete && i.naturalWidth>0 }", timeout=120000)
        print(f"[6] 结果已渲染，用时 {time.time()-t0:.1f}s")

        src = page.locator("#out img").get_attribute("src")
        meta = page.locator("#meta").inner_text().replace("\n", " | ")[:220]
        print(f"[7] 产出: {src[:90]}…")
        print(f"[8] 元信息: {meta}")

        page.wait_for_timeout(3500)  # 等一次 /stats 轮询
        reqs = page.locator("#sReq").inner_text()
        rate = page.locator("#sRate").inner_text()
        print(f"[9] 统计面板: 请求={reqs} 成功率={rate}")

        page.screenshot(path="browser_result.png", full_page=True)
        browser.close()

        if errors:
            print("浏览器控制台错误:", errors[:5])
            return 1
        print("浏览器路径验证通过")
        return 0


if __name__ == "__main__":
    sys.exit(main())
