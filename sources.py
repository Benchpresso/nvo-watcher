"""소스별 수집기.

각 collect_* 함수는 Item 리스트를 돌려준다.
네트워크 실패는 예외를 올리지 않고 빈 리스트 + 경고로 처리한다.
한 소스가 죽어도 나머지 알림은 계속 나가야 하기 때문이다.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("sources")

NOVO_CIK = "0000353278"  # NOVO NORDISK A/S
S1_FORMS = ("S-1", "S-1/A", "F-1", "F-1/A")
NOVO_FORMS = ("6-K", "20-F", "20-F/A", "SC 13D", "SC 13G")

# 공개 S-1이 뜨면 즉시 알아야 하는 회사들. 소문자 부분일치.
WATCHED_ISSUERS = ("openai", "anthropic")

# 언론사별 Google News 검색. 페이월 매체는 제목/리드까지만 들어온다.
NEWS_SITES = {
    "CNBC": "cnbc.com",
    "WSJ": "wsj.com",
    "NYT": "nytimes.com",
    "FT": "ft.com",
}

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


@dataclasses.dataclass
class Item:
    uid: str          # 중복 제거 키. 절대 재사용되지 않아야 한다.
    kind: str         # s1 | novo_sec | novo_ir | news
    title: str
    url: str
    published: str = ""
    source: str = ""
    body: str = ""    # 요약에 넣을 본문 조각 (있으면)
    urgent: bool = False


class Fetcher:
    """SEC 요구사항(User-Agent, 10 req/s 제한)을 지키는 얇은 HTTP 래퍼."""

    def __init__(self, sec_user_agent: str, timeout: int = 25):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._last_sec_call = 0.0
        self.ok = 0
        self.failed = 0

    def get(self, url: str, *, sec: bool = False) -> requests.Response | None:
        if sec:
            # SEC는 초당 10회를 넘기면 차단한다. 여유 있게 0.25초.
            gap = time.monotonic() - self._last_sec_call
            if gap < 0.25:
                time.sleep(0.25 - gap)
            self._last_sec_call = time.monotonic()
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            self.ok += 1
            return r
        except requests.RequestException as exc:
            self.failed += 1
            log.warning("fetch 실패 %s → %s", url, exc)
            return None


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# --------------------------------------------------------------------------
# 1) OpenAI / Anthropic 공개 S-1
# --------------------------------------------------------------------------

def collect_s1(f: Fetcher) -> list[Item]:
    """EDGAR에 OpenAI/Anthropic 이름으로 S-1(또는 F-1)이 공개 제출되는 순간을 잡는다.

    두 갈래로 본다:
      a) getcurrent 피드 — 접수 직후 몇 분 안에 올라온다. 가장 빠르다.
      b) 회사명 검색 — getcurrent 창(하루)을 놓쳤을 때의 백스톱.

    주의: JOBS Act에 따른 비공개(DRS) 제출은 EDGAR에 나타나지 않는다.
    Anthropic은 이미 비공개 제출을 한 것으로 보도됐으므로, 이 알림이 울리는
    시점은 로드쇼 약 15일 전 '공개 전환' 시점이다.
    """
    items: list[Item] = []
    seen_accessions: set[str] = set()

    for form in ("S-1", "F-1"):
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={form}&company=&dateb=&owner=include&count=100&output=atom"
        )
        r = f.get(url, sec=True)
        if r is None:
            continue
        items.extend(_parse_getcurrent(r.text, seen_accessions))

    for name in WATCHED_ISSUERS:
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&company={urllib.parse.quote(name)}&type=S-1&dateb=&owner=include"
            "&count=40&output=atom"
        )
        r = f.get(url, sec=True)
        if r is None:
            continue
        items.extend(_parse_company_atom(r.text, seen_accessions))

    return items


def _matches_watchlist(text: str) -> str | None:
    low = text.lower()
    for name in WATCHED_ISSUERS:
        # 'openai' 가 다른 단어의 일부로 걸리는 경우는 사실상 없다.
        if name in low:
            return name
    return None


def _parse_getcurrent(xml_text: str, seen: set[str]) -> list[Item]:
    out: list[Item] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("getcurrent 파싱 실패: %s", exc)
        return out

    for entry in root.findall("a:entry", ATOM_NS):
        title = _clean(entry.findtext("a:title", default="", namespaces=ATOM_NS))
        if not _matches_watchlist(title):
            continue
        link_el = entry.find("a:link", ATOM_NS)
        url = link_el.get("href", "") if link_el is not None else ""
        updated = _clean(entry.findtext("a:updated", default="", namespaces=ATOM_NS))
        acc = _accession_from_url(url) or title
        if acc in seen:
            continue
        seen.add(acc)
        out.append(
            Item(
                uid=f"s1:{acc}",
                kind="s1",
                title=title,
                url=url,
                published=updated,
                source="SEC EDGAR",
                urgent=True,
            )
        )
    return out


def _parse_company_atom(xml_text: str, seen: set[str]) -> list[Item]:
    out: list[Item] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    company = _clean(
        root.findtext("a:company-info/a:conformed-name", default="", namespaces=ATOM_NS)
    )
    for entry in root.findall("a:entry", ATOM_NS):
        form = _clean(
            entry.findtext("a:content/a:filing-type", default="", namespaces=ATOM_NS)
        )
        if form and form.upper() not in S1_FORMS:
            continue
        title = _clean(entry.findtext("a:title", default="", namespaces=ATOM_NS))
        haystack = f"{company} {title}"
        if not _matches_watchlist(haystack):
            continue
        url = _clean(
            entry.findtext(
                "a:content/a:filing-href", default="", namespaces=ATOM_NS
            )
        )
        if not url:
            link_el = entry.find("a:link", ATOM_NS)
            url = link_el.get("href", "") if link_el is not None else ""
        date = _clean(
            entry.findtext("a:content/a:filing-date", default="", namespaces=ATOM_NS)
        )
        acc = _accession_from_url(url) or f"{company}-{form}-{date}"
        if acc in seen:
            continue
        seen.add(acc)
        out.append(
            Item(
                uid=f"s1:{acc}",
                kind="s1",
                title=f"{company or title} — {form or 'S-1'}",
                url=url,
                published=date,
                source="SEC EDGAR",
                urgent=True,
            )
        )
    return out


def _accession_from_url(url: str) -> str | None:
    m = re.search(r"(\d{10}-?\d{2}-?\d{6})", url or "")
    return m.group(1).replace("-", "") if m else None


# --------------------------------------------------------------------------
# 2) Novo Nordisk SEC 공시
# --------------------------------------------------------------------------

def collect_novo_sec(f: Fetcher, lookback: int = 30) -> list[Item]:
    """data.sec.gov 제출 JSON에서 노보의 최근 공시를 읽는다. 페이월 없음."""
    r = f.get(f"https://data.sec.gov/submissions/CIK{NOVO_CIK}.json", sec=True)
    if r is None:
        return []
    try:
        data = r.json()
    except ValueError:
        log.warning("노보 제출 JSON 파싱 실패")
        return []

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    descs = recent.get("primaryDocDescription") or []

    out: list[Item] = []
    for i in range(min(lookback, len(forms))):
        form = forms[i]
        if form.upper() not in NOVO_FORMS:
            continue
        acc = accessions[i]
        acc_plain = acc.replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(NOVO_CIK)}/"
            f"{acc_plain}/{doc}" if doc else
            f"https://www.sec.gov/Archives/edgar/data/{int(NOVO_CIK)}/{acc_plain}/"
        )
        desc = descs[i] if i < len(descs) else ""
        out.append(
            Item(
                uid=f"novosec:{acc_plain}",
                kind="novo_sec",
                title=f"Novo Nordisk {form}" + (f" — {desc}" if desc else ""),
                url=url,
                published=dates[i] if i < len(dates) else "",
                source="SEC EDGAR",
            )
        )
    return out


# --------------------------------------------------------------------------
# 3) Novo Nordisk 공식 IR / 보도자료
# --------------------------------------------------------------------------

NOVO_NEWS_PAGES = (
    "https://www.novonordisk.com/news-and-media/news-and-ir-materials.html",
    "https://www.novonordisk.com/news-and-media/latest-news.html",
)

# 개별 발표 URL만 골라낸다. 목록/허브 페이지는 제외.
_NOVO_ITEM_RE = re.compile(
    r'href="(?P<href>(?:https://www\.novonordisk\.com)?/news-and-media/'
    r'(?:latest-news|news-details)/[^"#?]+\.html)"',
    re.IGNORECASE,
)


def collect_novo_ir(f: Fetcher) -> list[Item]:
    """노보 공식 사이트에는 RSS가 없어서 발표 페이지 링크를 긁는다.

    DOM 구조가 아니라 URL 패턴에만 의존하므로 디자인이 바뀌어도 잘 버틴다.
    중복 제거도 URL 기준이라, 한 번 보낸 발표를 다시 보내지 않는다.
    """
    found: dict[str, Item] = {}
    for page in NOVO_NEWS_PAGES:
        r = f.get(page)
        if r is None:
            continue
        for m in _NOVO_ITEM_RE.finditer(r.text):
            href = m.group("href")
            if href.startswith("/"):
                href = "https://www.novonordisk.com" + href
            if href in found:
                continue
            slug = href.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
            title = slug.replace("-", " ").strip().capitalize()
            found[href] = Item(
                uid=f"novoir:{href}",
                kind="novo_ir",
                title=title,
                url=href,
                source="Novo Nordisk IR",
            )
    return list(found.values())


# --------------------------------------------------------------------------
# 4) 언론 기사 (CNBC / WSJ / NYT / FT)
# --------------------------------------------------------------------------

def collect_news(f: Fetcher, window: str = "2d") -> list[Item]:
    """Google News RSS를 매체별로 좁혀서 노보 기사만 가져온다.

    WSJ·NYT·FT는 본문이 페이월이라 제목과 리드까지만 확보된다.
    """
    out: list[Item] = []
    for label, domain in NEWS_SITES.items():
        q = urllib.parse.quote(f'"Novo Nordisk" site:{domain} when:{window}')
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        r = f.get(url)
        if r is None:
            continue
        out.extend(_parse_google_news(r.text, label))
    return out


def _parse_google_news(xml_text: str, label: str) -> list[Item]:
    out: list[Item] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("%s 뉴스 피드 파싱 실패: %s", label, exc)
        return out

    for it in root.iterfind(".//item"):
        title = _clean(it.findtext("title"))
        link = _clean(it.findtext("link"))
        if not title or not link:
            continue
        guid = _clean(it.findtext("guid")) or link
        # Google News 제목은 "헤드라인 - 매체" 꼴이다. 매체명을 떼어낸다.
        title = re.sub(rf"\s+-\s+{re.escape(label)}\s*$", "", title)
        out.append(
            Item(
                uid=f"news:{guid}",
                kind="news",
                title=title,
                url=link,
                published=_clean(it.findtext("pubDate")),
                source=label,
                body=_clean(it.findtext("description"))[:1200],
            )
        )
    return out
