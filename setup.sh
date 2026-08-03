#!/usr/bin/env bash
# nvo-watcher 설치. 저장소 생성 → 시크릿 등록 → 기준선 실행까지 한 번에.
#
#   cd nvo-watcher && bash setup.sh
#
# 입력한 토큰과 API 키는 화면에 표시되지 않고, 파일이나 셸 히스토리에도 남지 않는다.
# 전부 gh 를 통해 GitHub Secrets 로만 들어간다.

set -euo pipefail

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 사전 점검
say "1/6  준비 상태 확인"

[[ -f watcher.py && -d .github/workflows ]] || \
  die "nvo-watcher 폴더 안에서 실행하세요. (현재: $(pwd))"
ok "프로젝트 폴더 확인"

command -v git >/dev/null || die "git이 없습니다. Xcode 명령줄 도구를 설치하세요: xcode-select --install"
ok "git $(git --version | awk '{print $3}')"

if ! command -v gh >/dev/null; then
  warn "GitHub CLI(gh)가 없습니다."
  if command -v brew >/dev/null; then
    read -r -p "  지금 설치할까요? (brew install gh) [y/N] " a
    [[ "$a" =~ ^[Yy]$ ]] || die "gh 설치 후 다시 실행하세요."
    brew install gh
  else
    die "Homebrew가 없습니다. https://cli.github.com 에서 gh를 설치한 뒤 다시 실행하세요."
  fi
fi
ok "gh $(gh --version | head -1 | awk '{print $3}')"

if ! gh auth status >/dev/null 2>&1; then
  warn "GitHub 로그인이 필요합니다. 브라우저가 열립니다."
  # workflow 스코프가 없으면 .github/workflows 푸시가 거부된다.
  gh auth login -s workflow -w
fi
GH_USER=$(gh api user --jq .login)
ok "GitHub 계정: $GH_USER"

if ! gh auth status 2>&1 | grep -q "workflow"; then
  warn "workflow 권한이 없어 워크플로 파일 푸시가 막힐 수 있습니다. 권한을 추가합니다."
  gh auth refresh -s workflow
fi

python3 watcher.py --test >/dev/null 2>&1 && ok "자체 점검 통과" || warn "자체 점검을 건너뜁니다 (python3 없음)"

# ---------------------------------------------------------------- 저장소
say "2/6  저장소 만들기"

read -r -p "  저장소 이름 [nvo-watcher]: " REPO
REPO="${REPO:-nvo-watcher}"

if [[ -d .git ]]; then
  ok "이미 git 저장소입니다"
else
  git init -b main >/dev/null
  ok "git 초기화"
fi

git add -A
git diff --cached --quiet || git commit -q -m "init: nvo-watcher"
ok "커밋 완료"

if gh repo view "$GH_USER/$REPO" >/dev/null 2>&1; then
  warn "$GH_USER/$REPO 가 이미 있습니다. 그 저장소로 푸시합니다."
  git remote get-url origin >/dev/null 2>&1 || \
    git remote add origin "https://github.com/$GH_USER/$REPO.git"
  git push -u origin main
else
  # 공개 저장소 = Actions 표준 러너 무제한 무료. 비밀은 코드가 아니라 Secrets에 들어간다.
  gh repo create "$REPO" --public --source=. --remote=origin --push
fi
ok "https://github.com/$GH_USER/$REPO"

# ---------------------------------------------------------------- 텔레그램
say "3/6  텔레그램 봇 연결"
echo "  아직 봇이 없다면: 텔레그램에서 @BotFather → /newbot → 이름 지정 → 토큰 복사"
echo

read -r -s -p "  봇 토큰 (입력이 보이지 않습니다): " TG_TOKEN; echo
[[ -n "$TG_TOKEN" ]] || die "토큰이 비어 있습니다."

BOT_NAME=$(curl -fsS --max-time 15 "https://api.telegram.org/bot${TG_TOKEN}/getMe" \
  | sed -n 's/.*"username":"\([^"]*\)".*/\1/p') || die "토큰이 잘못됐거나 텔레그램에 접속할 수 없습니다."
[[ -n "$BOT_NAME" ]] || die "토큰이 잘못된 것 같습니다."
ok "봇 확인: @$BOT_NAME"

say "4/6  chat_id 찾기"
echo "  텔레그램에서 \033[1m@$BOT_NAME\033[0m 을 열고 아무 메시지나 한 번 보내세요."
echo "  (이 과정을 거쳐야 봇이 먼저 말을 걸 수 있습니다.)"
read -r -p "  보냈으면 엔터 → "

CHAT_ID=""
for i in 1 2 3 4 5; do
  CHAT_ID=$(curl -fsS --max-time 15 "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" \
    | grep -oE '"chat":\{"id":-?[0-9]+' | tail -1 | grep -oE '\-?[0-9]+$' || true)
  [[ -n "$CHAT_ID" ]] && break
  warn "아직 메시지가 안 보입니다. 5초 후 재시도 ($i/5)"
  sleep 5
done
[[ -n "$CHAT_ID" ]] || die "chat_id를 찾지 못했습니다. 봇에게 메시지를 보낸 뒤 다시 실행하세요."
ok "chat_id: $CHAT_ID"

curl -fsS --max-time 15 -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  -d "text=✅ nvo-watcher 연결됐습니다. 이제부터 새 소식이 여기로 옵니다." >/dev/null \
  && ok "테스트 메시지를 보냈습니다 — 텔레그램을 확인하세요"

# ---------------------------------------------------------------- 나머지 시크릿
say "5/6  나머지 설정"
echo "  SEC는 요청마다 이름과 연락 가능한 이메일을 요구합니다. 없으면 EDGAR가 차단합니다."
echo "  (GitHub Secrets에만 저장되며 공개되지 않습니다.)"
read -r -p "  이름: " SEC_NAME
read -r -p "  이메일: " SEC_MAIL
[[ -n "$SEC_NAME" && "$SEC_MAIL" == *@* ]] || die "이름과 올바른 이메일이 필요합니다."

echo
echo "  Anthropic API 키 (한글 요약용, 없으면 그냥 엔터 → 제목과 링크만 전송)"
echo "  발급: https://console.claude.com/settings/keys   ※ Max 구독과는 별도 과금입니다"
read -r -s -p "  API 키: " ANTHROPIC_KEY; echo

gh secret set TELEGRAM_BOT_TOKEN --body "$TG_TOKEN"                >/dev/null && ok "TELEGRAM_BOT_TOKEN 등록"
gh secret set TELEGRAM_CHAT_ID   --body "$CHAT_ID"                 >/dev/null && ok "TELEGRAM_CHAT_ID 등록"
gh secret set SEC_USER_AGENT     --body "$SEC_NAME $SEC_MAIL"      >/dev/null && ok "SEC_USER_AGENT 등록"
if [[ -n "$ANTHROPIC_KEY" ]]; then
  gh secret set ANTHROPIC_API_KEY --body "$ANTHROPIC_KEY"          >/dev/null && ok "ANTHROPIC_API_KEY 등록"
else
  warn "API 키 없음 — 요약 없이 제목과 링크만 전송됩니다"
fi
unset TG_TOKEN ANTHROPIC_KEY

# ---------------------------------------------------------------- 기준선
say "6/6  기준선 잡기"
echo "  지금 보이는 항목을 '이미 본 것'으로 기록합니다."
echo "  (이걸 안 하면 첫 실행에서 과거 항목 수십 건이 한꺼번에 옵니다.)"

gh workflow run watch.yml -f mode=seed >/dev/null 2>&1 \
  && ok "seed 실행을 시작했습니다" \
  || warn "자동 실행 실패 — Actions 탭에서 watch → Run workflow → mode: seed 를 직접 눌러주세요"

cat <<EOF

────────────────────────────────────────────────────────────
 설치 완료

 저장소   https://github.com/$GH_USER/$REPO
 실행기록 https://github.com/$GH_USER/$REPO/actions

 이제 10분마다 자동으로 돌면서 새 소식만 텔레그램으로 보냅니다.

 다음에 하실 일
   1. Actions 탭에서 방금 돌린 seed 실행이 초록불인지 확인
   2. 로그의 's1 / novo_sec / novo_ir / news' 건수를 보고
      네 소스가 다 살아있는지 확인
   3. 메시지 모양을 보고 싶으면 Run workflow → mode: dry-run

 감시 대상을 바꾸려면 sources.py 의 WATCHED_ISSUERS 와 NEWS_SITES 를
 고치고 push 하면 다음 실행부터 반영됩니다.
────────────────────────────────────────────────────────────
EOF
