"""네트워크 없이 파싱·중복제거·메시지 포맷을 점검한다.

실제 SEC/Google 응답을 본뜬 고정 샘플을 쓴다. 실행: python watcher.py --test
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sources  # noqa: E402
import watcher  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"

_failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def run() -> int:
    print("=" * 62)
    print("고정 샘플 점검 (네트워크 사용 안 함)")
    print("=" * 62)

    # 1) getcurrent 피드에서 관심 발행사만 골라내는가
    seen: set[str] = set()
    got = sources._parse_getcurrent(
        (FIX / "edgar_getcurrent.xml").read_text(encoding="utf-8"), seen
    )
    titles = [i.title for i in got]
    check("getcurrent: OpenAI S-1을 잡는다", any("OpenAI" in t for t in titles), str(titles))
    check("getcurrent: Anthropic S-1을 잡는다", any("Anthropic" in t for t in titles), str(titles))
    check("getcurrent: 무관한 회사는 버린다", not any("Acme" in t for t in titles), str(titles))
    check("getcurrent: S-1은 긴급 표시", all(i.urgent for i in got))
    check("getcurrent: 정확히 2건", len(got) == 2, f"{len(got)}건")

    # 2) 같은 접수번호를 두 경로에서 봐도 한 번만 남는가
    before = len(seen)
    again = sources._parse_getcurrent(
        (FIX / "edgar_getcurrent.xml").read_text(encoding="utf-8"), seen
    )
    check("접수번호 중복 제거", again == [] and len(seen) == before, f"{len(again)}건 재검출")

    # 3) 노보 SEC 제출 JSON
    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class FakeFetcher:
        def __init__(self, payload): self._p = payload
        def get(self, url, sec=False): return FakeResp(self._p)

    novo_json = json.loads((FIX / "novo_submissions.json").read_text(encoding="utf-8"))
    novo = sources.collect_novo_sec(FakeFetcher(novo_json))
    forms = [i.title for i in novo]
    check("노보 SEC: 6-K와 20-F를 잡는다", len(novo) == 2, str(forms))
    check(
        "노보 SEC: 관심 없는 서식(SD)은 버린다",
        not any("SD" in t.split()[-1] for t in forms),
        str(forms),
    )
    check(
        "노보 SEC: 문서 URL을 만든다",
        all(i.url.startswith("https://www.sec.gov/Archives/edgar/data/353278/") for i in novo),
        str([i.url for i in novo]),
    )
    check("노보 SEC: 긴급 아님", not any(i.urgent for i in novo))

    # 4) 노보 IR 페이지에서 개별 발표 링크만 추출
    class HtmlFetcher:
        def __init__(self, html): self._h = html
        def get(self, url, sec=False):
            return type("R", (), {"text": self._h})()

    ir_html = (FIX / "novo_news.html").read_text(encoding="utf-8")
    ir = sources.collect_novo_ir(HtmlFetcher(ir_html))
    urls = [i.url for i in ir]
    check("노보 IR: 발표 2건 추출", len(ir) == 2, str(urls))
    check(
        "노보 IR: 목록/허브 페이지는 제외",
        not any(u.endswith("news-and-ir-materials.html") for u in urls),
        str(urls),
    )
    check(
        "노보 IR: 상대경로를 절대경로로",
        all(u.startswith("https://www.novonordisk.com/") for u in urls),
        str(urls),
    )

    # 5) 구글 뉴스 RSS
    news = sources._parse_google_news(
        (FIX / "google_news_wsj.xml").read_text(encoding="utf-8"), "WSJ"
    )
    check("뉴스: 2건 파싱", len(news) == 2, f"{len(news)}건")
    check(
        "뉴스: 제목 끝의 매체명을 떼어낸다",
        news and not news[0].title.endswith("- WSJ"),
        news[0].title if news else "",
    )
    check("뉴스: 출처가 WSJ", all(i.source == "WSJ" for i in news))

    # 6) 메시지 렌더링 — HTML 이스케이프가 실제로 되는가
    nasty = sources.Item(
        uid="x",
        kind="news",
        title='Novo & "Wegovy" <script>alert(1)</script>',
        url="https://example.com/a?b=1&c=2",
        source="WSJ",
        published="2026-08-03",
    )
    msg = watcher.render(nasty, "매출이 <b>20%</b> 늘었다 & 가이던스를 올렸다.")
    check("렌더: 제목의 태그를 이스케이프", "<script>" not in msg, msg[:120])
    check("렌더: 요약의 태그도 이스케이프", "<b>20%</b>" not in msg, msg[:200])
    check("렌더: 앰퍼샌드 처리", "&amp;" in msg)
    check("렌더: 링크 유지", 'href="https://example.com/a?b=1&amp;c=2"' in msg, msg[-160:])

    urgent = sources.Item(uid="y", kind="s1", title="OpenAI Inc", url="https://sec.gov/x", urgent=True)
    check("렌더: S-1은 눈에 띄게", "🚨" in watcher.render(urgent, ""))

    long_item = sources.Item(uid="z", kind="news", title="T" * 60, url="https://e.com", source="FT")
    check(
        "렌더: 4096자 제한 준수",
        len(watcher.render(long_item, "가" * 6000)) <= watcher.TG_LIMIT,
    )

    # 7) 상태 파일 왕복
    import tempfile
    orig = watcher.STATE_PATH
    with tempfile.TemporaryDirectory() as td:
        watcher.STATE_PATH = pathlib.Path(td) / "seen.json"
        watcher.save_state({"seen": ["a", "b"]})
        check("상태: 저장 후 복원", watcher.load_state()["seen"] == ["a", "b"])
        watcher.save_state({"seen": [str(n) for n in range(watcher.STATE_CAP + 500)]})
        check(
            "상태: 상한선에서 잘린다",
            len(watcher.load_state()["seen"]) == watcher.STATE_CAP,
        )
    watcher.STATE_PATH = orig

    print("-" * 62)
    if _failures:
        print(f"실패 {len(_failures)}건: {', '.join(_failures)}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(run())
