import asyncio
from datetime import datetime
import pytz
from email.utils import format_datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
from playwright.async_api import async_playwright

TARGET_URL = "https://news.naver.com/opinion/series?whole=Y&sid=WORLD&order=update"
OUTPUT_FILE = "naver_series_world.xml"
KST = pytz.timezone("Asia/Seoul")

async def fetch_exact_pubdate(context, link: str, fallback_date_str: str) -> datetime:
    """기사 상세 페이지에서 초 단위 발행 시각을 정확히 추출합니다."""
    page = await context.new_page()
    try:
        # 빠른 속도를 위해 미디어 리소스 차단
        await page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2}", lambda route: route.abort())
        await page.goto(link, wait_until="domcontentloaded", timeout=15000)

        # 1. data-date-time 속성(YYYY-MM-DD HH:MM:SS) 탐색
        time_el = await page.query_selector("span[data-date-time]")
        if time_el:
            dt_str = await time_el.get_attribute("data-date-time")
            if dt_str:
                dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S")
                return KST.localize(dt)

        # 2. 본문 날짜 텍스트 (예: 2026.09.10. 오후 03:24) 탐색
        text_el = await page.query_selector(".media_end_head_info_datestamp_time")
        if text_el:
            t_str = (await text_el.inner_text()).strip()
            is_pm = "오후" in t_str
            cleaned = t_str.replace("오전", "").replace("오후", "").strip()
            parts = cleaned.split()
            if len(parts) == 2:
                ymd = parts[0].rstrip(".")
                h, m = map(int, parts[1].split(":"))
                if is_pm and h < 12:
                    h += 12
                elif not is_pm and h == 12:
                    h = 0
                dt = datetime.strptime(f"{ymd} {h:02d}:{m:02d}:00", "%Y.%m.%d %H:%M:%S")
                return KST.localize(dt)

    except Exception:
        pass
    finally:
        await page.close()

    # 파싱 실패 시 기본 날짜(00:00:00) 대체
    try:
        clean_date = fallback_date_str.rstrip(".")
        dt = datetime.strptime(clean_date, "%Y.%m.%d")
        return KST.localize(dt)
    except Exception:
        return datetime.now(KST)

async def scrape_articles():
    articles_data = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(TARGET_URL, wait_until="networkidle")
        await page.wait_for_selector("li.article_item", timeout=10000)

        items = await page.query_selector_all("li.article_item")
        candidate_items = []

        for item in items:
            link_el = await item.query_selector("a.link")
            title_el = await item.query_selector("p.article_text")
            press_el = await item.query_selector("span.press_name")
            date_el = await item.query_selector("span.timestamp")

            if not link_el or not title_el:
                continue

            raw_title = (await title_el.inner_text()).strip() if title_el else ""
            press = (await press_el.inner_text()).strip() if press_el else ""
            date_str = (await date_el.inner_text()).strip() if date_el else ""
            link = await link_el.get_attribute("href")

            # 언론사나 날짜가 누락된 구버전/서브 회차 링크 필터링
            if not press or not date_str or not raw_title or not link:
                continue

            formatted_title = f"[{press}] {raw_title} ({date_str})"
            candidate_items.append({
                "title": formatted_title,
                "link": link,
                "date_str": date_str,
                "press": press
            })

        # 비동기 병렬로 본문 시·분·초 파싱
        sem = asyncio.Semaphore(5)
        async def enrich_item(item):
            async with sem:
                exact_dt = await fetch_exact_pubdate(context, item["link"], item["date_str"])
                item["pub_date"] = exact_dt
                return item

        tasks = [enrich_item(item) for item in candidate_items]
        articles_data = await asyncio.gather(*tasks)

        await browser.close()

    return articles_data

def build_rss_feed(articles):
    # [핵심] 가장 늦은 시각(가장 최근 글)이 0번 인덱스로 오도록 정렬
    sorted_articles = sorted(articles, key=lambda x: x["pub_date"], reverse=True)

    # RSS 2.0 XML 루트 구성
    rss = ET.Element("rss", {
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "version": "2.0"
    })
    channel = ET.SubElement(rss, "channel")

    # 메타데이터 설정
    title = ET.SubElement(channel, "title")
    title.text = "네이버 뉴스 연재 - 세계"

    link = ET.SubElement(channel, "link")
    link.text = TARGET_URL

    description = ET.SubElement(channel, "description")
    description.text = "네이버 뉴스 연재(세계) 최신 업데이트 RSS 피드"

    language = ET.SubElement(channel, "language")
    language.text = "ko"

    if sorted_articles:
        last_build = ET.SubElement(channel, "lastBuildDate")
        last_build.text = format_datetime(sorted_articles[0]["pub_date"])

    # 정렬된 순서 그대로 최신 글부터 <item> 태그 추가
    for art in sorted_articles:
        item = ET.SubElement(channel, "item")
        
        i_title = ET.SubElement(item, "title")
        i_title.text = art["title"]

        i_link = ET.SubElement(item, "link")
        i_link.text = art["link"]

        i_desc = ET.SubElement(item, "description")
        i_desc.text = f"언론사: {art['press']} / 등록일: {art['date_str']}"

        i_guid = ET.SubElement(item, "guid", {"isPermaLink": "true"})
        i_guid.text = art["link"]

        i_pubdate = ET.SubElement(item, "pubDate")
        i_pubdate.text = format_datetime(art["pub_date"])

    # 예쁘게 들여쓰기(Pretty Print)하여 파일 저장
    xml_str = ET.tostring(rss, encoding="utf-8")
    parsed_xml = minidom.parseString(xml_str)
    pretty_xml = parsed_xml.toprettyxml(indent="  ", encoding="utf-8")

    with open(OUTPUT_FILE, "wb") as f:
        f.write(pretty_xml)

    if sorted_articles:
        print(f"최신순 정렬 완료: {OUTPUT_FILE}")
        print(f"1위(최상단): {sorted_articles[0]['title']} -> {sorted_articles[0]['pub_date']}")
        print(f"끝위(최하단): {sorted_articles[-1]['title']} -> {sorted_articles[-1]['pub_date']}")

if __name__ == "__main__":
    articles = asyncio.run(scrape_articles())
    build_rss_feed(articles)
