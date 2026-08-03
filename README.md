# nvo-watcher

OpenAI·Anthropic의 **공개 S-1**과 **Novo Nordisk** 소식을 감시해서, 한국어로 요약해 텔레그램으로 보낸다.

감시 대상 네 가지:

| 소스 | 어디서 | 페이월 |
|---|---|---|
| OpenAI·Anthropic 공개 S-1 | SEC EDGAR `getcurrent` + 회사명 검색 | 없음 |
| 노보 SEC 공시 (6-K/20-F 등) | `data.sec.gov` 제출 JSON | 없음 |
| 노보 공식 IR·보도자료 | novonordisk.com 발표 페이지 | 없음 |
| 노보 관련 기사 | Google News (CNBC·WSJ·NYT·FT) | **있음** — 제목과 리드까지만 |

---

## 먼저 알아둘 것

**1. Anthropic은 이미 비공개(confidential) S-1을 제출한 것으로 보도됐다.**
JOBS Act에 따른 비공개 초안(DRS)은 EDGAR에 공개되지 않는다. 즉 이 봇이 울리는
시점은 제출 순간이 아니라 **공개 전환 시점** — 통상 로드쇼 시작 약 15일 전이다.
그 전에 미리 아는 방법은 공시 시스템에는 없다.

**2. "즉시"는 10~20분 간격을 뜻한다.**
GitHub Actions의 예약 실행은 최소 5분 간격이지만, 혼잡할 때는 밀리거나 건너뛴다.
현실적으로 10~20분으로 보면 된다. 초 단위가 필요하면 상시 구동 서버가 필요하다.

**3. 스케줄 자동 중지.** 저장소에 60일간 커밋이 없으면 GitHub이 예약 워크플로를
비활성화한다. 이 봇은 상태 파일을 되커밋하므로 정상 동작 중에는 문제가 없다.

**4. 페이월.** WSJ·NYT·FT 본문은 가져올 수 없다. 요약은 제목과 리드 기준이며,
그래서 짧다. 본문까지 원하면 각 매체 구독 계정의 RSS를 따로 붙여야 한다.

---

## 설치

### 1) 저장소 만들기

이 폴더를 통째로 **비공개(private)** 저장소에 올린다.

```bash
git init && git add . && git commit -m "init"
gh repo create nvo-watcher --private --source=. --push
```

### 2) 텔레그램 봇 만들기

1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 대화 → `/newbot` → 이름 정하기
2. 받은 토큰을 복사 (`123456:ABC-DEF...` 꼴)
3. **만든 봇과 대화를 시작하고 아무 메시지나 한 번 보낸다** (이걸 안 하면 봇이 나에게 말을 걸 수 없다)
4. chat_id 확인:

```bash
curl -s "https://api.telegram.org/bot<토큰>/getUpdates" | python3 -m json.tool | grep -A2 '"chat"'
```

`"id": 123456789` 가 chat_id다.

### 3) Secrets 등록

저장소 → Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 | 필수 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 토큰 | 필수 |
| `TELEGRAM_CHAT_ID` | 위에서 확인한 숫자 | 필수 |
| `SEC_USER_AGENT` | `이름 you@example.com` | 필수 — SEC 규정 |
| `ANTHROPIC_API_KEY` | console.anthropic.com에서 발급 | 선택 |

`SEC_USER_AGENT`는 SEC가 **요구하는** 값이다. 이름과 연락 가능한 이메일이 없으면
EDGAR가 요청을 차단한다.

`ANTHROPIC_API_KEY`가 없으면 봇은 요약 없이 제목과 링크만 보낸다. 동작은 한다.
참고로 Max 구독과 API는 별개 과금이다 — API 키는 따로 발급받아야 한다.
모델을 바꾸려면 Variables에 `SUMMARY_MODEL`을 넣는다 (기본 `claude-sonnet-5`).

### 4) 기준선 잡기

첫 실행에서 과거 항목이 수십 건 쏟아지는 걸 막으려면, Actions 탭 →
watch → Run workflow → mode `seed` 를 한 번 돌린다. 지금 보이는 항목이 전부
"이미 본 것"으로 기록되고, 이후 새로 올라오는 것만 알림이 온다.

그다음 mode `dry-run`으로 한 번 더 돌려 로그에서 메시지 모양을 확인하면 좋다.

---

## 로컬에서 돌려보기

```bash
pip install -r requirements.txt

python watcher.py --test        # 네트워크 없이 파싱·포맷 점검 (24개 검사)

export SEC_USER_AGENT="이름 you@example.com"
python watcher.py --dry-run     # 실제로 수집·요약하되 전송은 안 함
```

`--test`는 CI에서도 매 실행 전에 돌아간다. SEC나 Google이 응답 형식을 바꾸면
파싱이 조용히 망가지는 대신 여기서 걸린다.

---

## 손볼 만한 곳

**감시 회사 추가** — `sources.py`의 `WATCHED_ISSUERS`에 소문자로 넣는다.
`("openai", "anthropic", "xai")` 처럼.

**언론사 추가/교체** — `sources.py`의 `NEWS_SITES`. `"블룸버그": "bloomberg.com"`.

**감시 종목 교체** — `NOVO_CIK`와 `sources.collect_news`의 검색어를 바꾼다.
CIK는 `https://www.sec.gov/cgi-bin/browse-edgar?company=<회사명>&action=getcompany`에서 찾는다.

**요약 말투** — `watcher.py`의 `SUMMARY_SYSTEM`. 지금은 3문장, 숫자 보존,
추측 금지, 투자 권유 금지로 묶어 뒀다.

**노보 IR 스크래핑이 멈추면** — `sources.py`의 `_NOVO_ITEM_RE`가 발표 URL 패턴을
따라간다. 노보가 URL 구조를 바꾸면 이 정규식만 고치면 된다. DOM이 아니라 URL에만
의존하므로 디자인 개편에는 영향을 받지 않는다.
IR 알림을 확실히 받으려면 [노보 이메일 구독](https://www.novonordisk.com/news-and-media/stay-informed.html)도
같이 켜 두는 걸 권한다. 공식 배포라 스크래핑보다 빠르고 정확하다.

---

## 동작 방식

```
watcher.py  ──> sources.collect_s1()        EDGAR getcurrent + 회사명 검색
            ──> sources.collect_novo_sec()  data.sec.gov 제출 JSON
            ──> sources.collect_novo_ir()   novonordisk.com 발표 링크
            ──> sources.collect_news()      Google News (매체별)
                     │
                     ▼
              state/seen.json 과 대조해 새 항목만 남김
                     │
                     ▼
              Anthropic API 로 한국어 3문장 요약
                     │
                     ▼
              텔레그램 sendMessage (HTML, 전부 이스케이프)
                     │
                     ▼
              전송 성공한 uid만 state/seen.json 에 기록 후 되커밋
```

전송에 실패한 항목은 `seen`에 기록하지 않는다. 다음 실행에서 다시 시도한다.

수집한 텍스트는 전부 **요약 대상 데이터**로만 다룬다. 기사나 공시 본문에 지시문처럼
보이는 문장이 들어 있어도 모델이 따르지 않도록 시스템 프롬프트에 못 박아 두었고,
텔레그램으로 나가는 모든 사용자 데이터는 HTML 이스케이프를 거친다.
