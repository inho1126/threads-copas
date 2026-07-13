# Coupang → RedNote → Threads Studio

쿠팡 상품 검색부터 후킹 문구, RedNote 미디어, Threads 발행과 기록까지 한 작업으로 연결하는 로컬 스튜디오입니다. 실제 Threads OAuth와 발행은 AWS의 전용 Threads API 서버가 맡고, 상품 조사·문구 생성·로그인된 Chrome을 이용한 RedNote 검색은 로컬에서 실행합니다.

## 바로가기

- 사용 문서: [docs/threads-publisher-guide.md](docs/threads-publisher-guide.md)
- 로컬 주소: 런처가 출력하는 `http://127.0.0.1:<port>` (`8765` 우선)
- AWS Threads API 서버: `uvicorn codex_coupang_workbench.threads_api:app --host 0.0.0.0 --port 8765`
- Meta Redirect URI: `https://sinabro-ai.com/threads-copas/api/threads/auth/callback`

## 설치

로컬 스튜디오는 Python 환경과 함께 Node.js 24 이상, Codex CLI가 필요합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
node --version
codex --version
```

Threads 글 생성은 현재 머신의 Codex CLI 로그인 인증을 사용합니다.

```bash
codex login
```

## 실행

권장 실행 명령은 하나입니다.

```bash
python3 scripts/run_studio.py
```

런처는 Node.js 24 이상과 Codex CLI를 확인하고, 외부에서 접근할 수 없는 임의의 loopback 포트에 RedNote sidecar를 먼저 시작합니다. sidecar health check가 성공한 뒤 로컬 스튜디오를 `127.0.0.1:8765`에서 시작하며, 이미 사용 중이면 빈 loopback 포트를 자동 선택해 실제 주소를 출력합니다. sidecar 인증 키는 실행할 때마다 메모리에서 새로 만들며 화면이나 로그에 출력하지 않습니다. `Ctrl+C`, `SIGINT`, `SIGTERM`, 또는 자식 프로세스의 조기 종료 시 두 프로세스를 함께 종료하고 회수합니다.

RedNote에서 내려받은 파일의 기본 위치는 `~/Downloads/rednote`입니다. 다른 위치를 쓰려면 실행 전에 설정합니다.

```bash
export REDNOTE_OUTPUT_ROOT="$HOME/Downloads/rednote"
python3 scripts/run_studio.py
```

기존 로컬 화면만 수동으로 실행하는 방법도 유지됩니다. 이 명령은 RedNote sidecar를 대신 시작하거나 관리하지 않으므로 전체 스튜디오 사용에는 위 런처를 권장합니다.

```bash
uvicorn codex_coupang_workbench.main:app --reload --port 8765
```

브라우저에서 엽니다.

```text
http://127.0.0.1:8765
```

`Chrome 확인`과 RedNote 검색은 사용자가 로그인해 둔 Google Chrome 프로필을 사용합니다. macOS의 `시스템 설정 → 개인정보 보호 및 보안 → 자동화`에서 실행에 사용하는 터미널 앱(필요한 경우 Python 또는 Node)이 Google Chrome을 제어하도록 허용하세요. Apple Events 권한을 거부했다면 이 설정에서 다시 켠 뒤 스튜디오를 재시작합니다.

## 6단계 통합 흐름

1. **계정** — `현재 계정 가져오기`로 OAuth를 완료하고, 연결된 Threads 계정 중 실제 발행 대상을 선택합니다.
2. **상품** — 상품명으로 쿠팡 상품을 검색해 최대 10개 결과를 비교합니다. 상품을 선택하면 해당 상품의 파트너스 딥링크를 만들며, 필요하면 URL 직접 입력과 로그인된 Chrome 확인을 사용할 수 있습니다.
3. **문구** — 호기심, 현실 공감, 문제 해결, 솔직한 발견, 스토리, 구매 전환의 고정 6개 페르소나와 선택형 커스텀 페르소나로 짧은 한국어 후킹 본문을 생성합니다. 선택한 버전만 다시 만들 수도 있습니다.
4. **RedNote** — 상품명을 바탕으로 실제 검색에 쓸 중국어 검색어를 정확히 하나 생성합니다. `검색어 다시 만들기`를 누르면 기존 검색어를 교체하며, 로그인된 Chrome 세션에서 영상 게시물만 최대 10개 찾습니다.
5. **미디어** — 선택한 게시물의 원본 소스 스트림을 MP4로 저장하고, 서로 다른 대표 장면 3~5장을 JPG로 자동 추출합니다. Threads에는 MP4 1개 또는 사용자가 고른 JPG 2~5장 중 한 형식만 게시합니다.
6. **게시** — 계정, 본문, 미디어 순서, 잠긴 쿠팡 파트너스 광고 고지와 딥링크 댓글을 최종 미리보기에서 확인한 뒤 게시합니다. 결과는 게시 기록에 저장되며 지표 새로고침과 안전한 댓글 재시도를 제공합니다.

광고 고지와 딥링크는 첫 댓글용으로 서버가 고정합니다.

```text
이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.

https://link.coupang.com/a/example
```

## 테스트

```bash
pytest tests -v
```

## 데이터 저장 위치

로컬 화면에서 만든 상품/초안 데이터는 로컬 SQLite에 저장됩니다.

```text
workbench_data/workbench.sqlite3
```

AWS Threads API 서버의 Threads 토큰과 발행 기록은 AWS SQLite에 저장됩니다.

```text
workbench_data/threads_api.sqlite3
```

`workbench_data/`, `.env`, SQLite DB, 로그 파일은 `.gitignore`에 포함되어 있으니 외부에 공유하거나 커밋하지 마세요.

## 로컬 화면 + AWS Threads API 분리 운영

AWS는 Threads OAuth callback, 토큰·임시 미디어 저장, Threads 발행을 담당하고 로컬은 화면, 쿠팡 상품 조회, 문구 생성, RedNote 검색·다운로드·대표 장면 추출을 담당합니다.

```text
로컬 화면/API
→ 쿠팡 상품 검색·딥링크 생성
→ 페르소나별 Threads 본문 생성
→ 로그인된 Chrome으로 RedNote 영상 검색
→ MP4 다운로드·대표 JPG 추출 및 미디어 선택
→ AWS Threads 서비스에 미디어와 발행 요청
→ AWS가 Threads API로 게시
```

AWS에는 전용 API 서버만 실행합니다. 화면, Settings API, 쿠팡 조회 API, 초안 생성 API는 노출하지 않습니다.

```bash
export THREADS_BRIDGE_API_KEY="긴_랜덤_문자열"
export THREADS_APP_ID="Meta 앱 ID"
export THREADS_APP_SECRET="Meta 앱 시크릿"
export THREADS_REDIRECT_URI="https://sinabro-ai.com/threads-copas/api/threads/auth/callback"
export THREADS_PUBLIC_BASE_URL="https://sinabro-ai.com/threads-copas"
uvicorn codex_coupang_workbench.threads_api:app --host 0.0.0.0 --port 8765
```

systemd로 실행한다면 서비스 파일에 아래 줄을 추가합니다.

```ini
Environment="THREADS_BRIDGE_API_KEY=긴_랜덤_문자열"
Environment="THREADS_APP_ID=Meta 앱 ID"
Environment="THREADS_APP_SECRET=Meta 앱 시크릿"
Environment="THREADS_REDIRECT_URI=https://sinabro-ai.com/threads-copas/api/threads/auth/callback"
Environment="THREADS_PUBLIC_BASE_URL=https://sinabro-ai.com/threads-copas"
```

`THREADS_PUBLIC_BASE_URL`은 Meta가 임시 JPEG/MP4를 가져갈 수 있는 외부 공개 HTTPS 주소입니다. 임시 미디어 capability URL은 기본 24시간 후 만료되며, 만료된 파일은 다시 업로드해야 합니다. URL을 아는 사람은 만료 전까지 파일을 읽을 수 있으므로 공유하거나 로그에 남기지 마세요.

AWS API 서버에서 열리는 엔드포인트는 Threads 브리지 API만입니다.

```text
GET  /api/health
GET  /api/threads/profiles
POST /api/threads/profiles
GET  /api/threads/auth/start
GET  /api/threads/auth/import/start
GET  /api/threads/auth/callback
GET  /api/threads/publish-records
POST /api/threads/media-uploads
POST /api/threads/media-uploads/start
POST /api/threads/media-uploads/{upload_id}/parts
POST /api/threads/media-uploads/{upload_id}/complete
GET  /api/threads/media/{media_id}
HEAD /api/threads/media/{media_id}
DELETE /api/threads/media/{media_id}
POST /api/threads/remote-publish
POST /api/threads/remote-media-publish
POST /api/threads/profiles/{profile_key}/refresh
POST /api/threads/profiles/{profile_key}/disconnect
POST /api/threads/publish-records/{job_id}/insights
POST /api/threads/publish-records/{job_id}/permalink
DELETE /api/threads/publish-records/{job_id}
```

대용량 MP4는 요청 본문이 nginx 기본 제한을 넘지 않도록 512 KiB 단위로 분할 업로드합니다. 413 오류를 해결하려면 로컬 Studio와 함께 AWS의 `codex_coupang_workbench.threads_api:app`도 이 버전으로 배포·재시작해야 합니다.

Threads 지표 조회에는 Meta 앱 OAuth scope에 `threads_manage_insights`가 필요합니다. 기존에 연결한 프로필은 scope 추가 후 `현재 계정 가져오기`로 다시 연결해야 지표 새로고침이 성공합니다.

로컬 화면은 기존 앱을 실행합니다.

```bash
uvicorn codex_coupang_workbench.main:app --reload --port 8765
```

로컬 Settings에는 아래 값만 저장합니다. `Threads Service URL`은 운영 환경에서 반드시 공개 HTTPS 주소여야 합니다.

```text
Threads Service URL = https://sinabro-ai.com/threads-copas
Threads Service API Key = AWS의 THREADS_BRIDGE_API_KEY 값
Coupang Access Key = 쿠팡 파트너스 Access Key
Coupang Secret Key = 쿠팡 파트너스 Secret Key
Coupang Sub ID = 필요한 경우 사용하는 Sub ID
Codex Model = gpt-5.5
```

HTTP 주소는 기본적으로 거부됩니다. 로컬 브리지끼리만 개발할 때는 loopback 주소에 한해 명시적으로 허용할 수 있습니다.

```bash
export THREADS_BRIDGE_ALLOW_INSECURE_LOOPBACK=1
```

이 모드에서는 로컬의 `현재 계정 가져오기`, 프로필 목록, 발행 버튼, 발행 기록 API가 AWS Threads 서비스로 위임됩니다. 쿠팡 상품 확인과 초안 생성은 로컬에서 실행됩니다.

쿠팡 상세 페이지가 `Access Denied`로 막혀 상품명이 비어 있으면 `Chrome 확인`을 누릅니다. 이 기능은 macOS의 로컬 Google Chrome을 열어 현재 Chrome 프로필의 쿠팡 세션으로 상품명만 읽어오며, 처음 실행 시 macOS가 터미널 또는 Python의 Chrome 제어 권한을 물을 수 있습니다.

## 발행 안전 및 복구

- RedNote 다운로드는 게시물의 원본 소스 스트림을 저장할 뿐, 워터마크 제거 기능을 제공하지 않습니다. 내려받을 수 있다는 사실도 재게시 권리를 뜻하지 않으므로 직접 제작했거나 사용 허락과 Threads 재게시 권리를 확인한 이미지·영상만 선택하고, 워터마크나 출처 표시를 제거하지 마세요.
- 최종 확인 화면의 쿠팡 파트너스 고지 문구와 딥링크 댓글은 서버가 고정해 만듭니다. 고지를 삭제하거나 우회하지 마세요. 관계 법령, 쿠팡 파트너스 정책 또는 플랫폼 정책이 본문 첫 부분의 고지를 요구하는 경우에는 댓글 고지만으로 충분하다고 가정하지 말고 본문에도 눈에 띄게 표시하세요.
- 본문 게시에는 성공했지만 고지 댓글 게시만 실패하면 `댓글만 다시 게시`를 사용하세요. 저장된 Threads post ID와 같은 idempotency key를 재사용해 댓글만 재시도하므로 본문을 중복 게시하지 않습니다.
- AWS 발행 작업은 SQLite 기반 lease와 fencing token으로 한 작업의 동시 실행을 막고, 같은 idempotency key의 재요청을 기존 작업에 연결합니다. 브라우저 새로고침이나 일시적인 네트워크 오류 뒤에도 새 작업을 만들지 말고 기존 게시 기록의 재시도 동작을 사용하세요.
- `PUBLISH_OUTCOME_UNKNOWN`은 Meta에 게시 요청이 전달된 뒤 응답을 확정하지 못했다는 뜻입니다. 중복 게시 위험이 있으므로 자동 또는 수동 재시도를 하지 말고, 선택한 Threads 계정에서 본문과 댓글의 실제 게시 여부를 직접 확인하세요.
