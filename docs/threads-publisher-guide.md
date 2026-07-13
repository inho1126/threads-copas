# Coupang → RedNote → Threads Studio 사용 문서

## 목적

이 스튜디오는 쿠팡 상품 검색과 파트너스 딥링크 생성, 한국어 후킹 문구, RedNote 미디어, Threads 발행과 기록을 하나의 작업으로 연결합니다. 로컬 서비스는 화면·상품·문구·미디어를 맡고, AWS Threads API 서버는 OAuth·토큰·임시 미디어·실제 게시를 맡습니다.

기본 흐름은 단순합니다.

```text
로컬 Settings 저장
→ STEP 01 Threads 계정 연결 및 선택
→ STEP 02 쿠팡 상품명 검색·딥링크 생성
→ STEP 03 6개 페르소나+커스텀 문구 선택
→ STEP 04 중국어 검색어 1개로 RedNote 영상 검색
→ STEP 05 MP4 또는 대표 JPG 구성
→ STEP 06 잠긴 광고 댓글 확인 후 Threads 게시
→ 게시 기록·지표·재시도 관리
```

## 필요한 것

1. Meta Developers 계정
2. Threads API를 사용하는 Meta 앱
3. Threads App ID
4. Threads App Secret
5. Redirect URI
6. 발행할 Threads 계정
7. Codex CLI 로그인
8. Node.js 24 이상
9. 쿠팡 파트너스 Access Key, Secret Key, 필요한 경우 Sub ID
10. 쿠팡과 RedNote에 로그인한 로컬 Google Chrome 프로필

Meta 앱 설정의 OAuth Redirect URI에는 AWS API 서버 주소를 등록합니다.

```text
https://sinabro-ai.com/threads-copas/api/threads/auth/callback
```

## 서버 실행

AWS 인스턴스에서는 Threads API 서버만 실행합니다. 이 서버는 화면을 제공하지 않고 env 값만 사용합니다.

```bash
export THREADS_BRIDGE_API_KEY="긴_랜덤_문자열"
export THREADS_APP_ID="Meta 앱 ID"
export THREADS_APP_SECRET="Meta 앱 시크릿"
export THREADS_REDIRECT_URI="https://sinabro-ai.com/threads-copas/api/threads/auth/callback"
export THREADS_PUBLIC_BASE_URL="https://sinabro-ai.com/threads-copas"
uvicorn codex_coupang_workbench.threads_api:app --host 0.0.0.0 --port 8765
```

로컬에서는 프로젝트 루트에서 한 명령으로 화면, 쿠팡 조회/초안 생성, RedNote sidecar를 함께 실행합니다.

```bash
python3 scripts/run_studio.py
```

런처는 Node.js 24 이상과 Codex CLI를 먼저 확인합니다. 외부에 노출되지 않는 임의의 `127.0.0.1` 포트에서 sidecar를 시작하고 health check가 성공한 뒤, 스튜디오를 `127.0.0.1:8765`에서 시작합니다. `8765`가 이미 사용 중이면 빈 loopback 포트를 자동 선택하고 실제 주소를 출력합니다. 매 실행마다 sidecar 키를 메모리에서 새로 만들고 로그에는 출력하지 않습니다. `Ctrl+C`, `SIGINT`, `SIGTERM`, 또는 어느 한 프로세스의 조기 종료 시 두 프로세스를 모두 정리합니다.

RedNote 파일은 기본적으로 `~/Downloads/rednote`에 저장됩니다. 위치를 바꾸려면 다음처럼 실행합니다.

```bash
REDNOTE_OUTPUT_ROOT="$HOME/Downloads/rednote" python3 scripts/run_studio.py
```

기존 수동 실행 명령도 사용할 수 있지만 RedNote sidecar를 함께 관리하지 않습니다.

```bash
uvicorn codex_coupang_workbench.main:app --reload --port 8765
```

브라우저에서 아래 주소를 엽니다.

```text
http://127.0.0.1:8765
```

## 1. 로컬 Settings 저장

로컬 화면의 `API Settings` 영역에 입력합니다.

- `Threads Service URL`: AWS Threads API 서버의 공개 HTTPS 주소
- `Threads Service API Key`: AWS의 `THREADS_BRIDGE_API_KEY`와 같은 값
- `Coupang Access Key`: 쿠팡 파트너스 Access Key
- `Coupang Secret Key`: 쿠팡 파트너스 Secret Key
- `Coupang Sub ID`: 필요한 경우 입력
- `Codex Model`: 기본값은 `gpt-5.5`

입력 후 `Save Settings`를 누릅니다.

운영용 `Threads Service URL`은 HTTPS만 허용됩니다. 로컬 브리지끼리 개발할 때만 loopback HTTP 주소를 다음 환경변수로 명시적으로 허용할 수 있습니다.

```bash
export THREADS_BRIDGE_ALLOW_INSECURE_LOOPBACK=1
```

Threads 글 생성은 현재 머신에 로그인된 Codex CLI 인증을 사용합니다. Codex 로그인이 필요하면 터미널에서 `codex login`을 먼저 실행합니다.

## 로컬 화면 + AWS Threads API 분리 운영

이 방식에서는 로컬 서비스가 화면, 쿠팡 상품 검색·딥링크, Threads 문구 생성, RedNote 검색·다운로드·대표 장면 추출을 맡고, AWS 서비스가 Meta OAuth callback, Threads 토큰·임시 미디어 저장, 실제 Threads 발행을 맡습니다.

```text
로컬 서비스
→ 쿠팡 상품 검색·딥링크 생성
→ 페르소나별 Threads 본문 생성
→ RedNote MP4·대표 JPG 준비
→ AWS Threads 서비스에 미디어·발행 요청
→ AWS가 Threads API로 게시
```

AWS 서버에는 아래 환경변수를 넣고 `codex_coupang_workbench.threads_api:app`만 실행합니다.

```bash
export THREADS_BRIDGE_API_KEY="긴_랜덤_문자열"
export THREADS_APP_ID="Meta 앱 ID"
export THREADS_APP_SECRET="Meta 앱 시크릿"
export THREADS_REDIRECT_URI="https://sinabro-ai.com/threads-copas/api/threads/auth/callback"
export THREADS_PUBLIC_BASE_URL="https://sinabro-ai.com/threads-copas"
uvicorn codex_coupang_workbench.threads_api:app --host 0.0.0.0 --port 8765
```

systemd를 쓰면 서비스 파일에 아래 줄을 넣습니다.

```ini
Environment="THREADS_BRIDGE_API_KEY=긴_랜덤_문자열"
Environment="THREADS_APP_ID=Meta 앱 ID"
Environment="THREADS_APP_SECRET=Meta 앱 시크릿"
Environment="THREADS_REDIRECT_URI=https://sinabro-ai.com/threads-copas/api/threads/auth/callback"
Environment="THREADS_PUBLIC_BASE_URL=https://sinabro-ai.com/threads-copas"
```

`THREADS_PUBLIC_BASE_URL`은 Meta가 임시 미디어를 읽을 수 있는 외부 공개 HTTPS 주소로 설정합니다. reverse proxy 경로를 포함해 브라우저와 Meta에서 실제 접근 가능한 주소여야 합니다.

AWS API 서버는 아래 API만 제공합니다.

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
```

대용량 MP4는 nginx 요청 제한을 넘지 않도록 512 KiB 단위로 분할 업로드합니다. 413 오류를 해결하려면 로컬 Studio와 함께 AWS의 `codex_coupang_workbench.threads_api:app`도 이 버전으로 배포·재시작해야 합니다.

로컬 화면의 `API Settings`에는 아래처럼 저장합니다.

```text
Threads Service URL = https://sinabro-ai.com/threads-copas
Threads Service API Key = AWS의 THREADS_BRIDGE_API_KEY 값
Coupang Access Key = 쿠팡 파트너스 Access Key
Coupang Secret Key = 쿠팡 파트너스 Secret Key
Codex Model = gpt-5.5
```

로컬의 `현재 계정 가져오기`, 프로필 목록, 토큰 갱신, 발행 버튼, 발행 기록 조회는 AWS Threads API 서버로 위임됩니다. 쿠팡 상품 확인과 초안 생성은 로컬에서 실행됩니다.

## 2. STEP 01 — Threads 계정 연결 및 선택

1. 계정 화면에서 `현재 계정 가져오기`를 누릅니다.
2. 열린 Meta/Threads OAuth 화면에서 발행할 계정으로 승인합니다.
3. AWS Redirect URI로 돌아온 콜백이 프로필과 토큰을 저장하면 스튜디오로 돌아옵니다.
4. 연결된 Threads 계정 목록에서 이번 작업의 발행 대상을 선택합니다.

OAuth state는 짧은 시간만 유효하고 한 번만 사용할 수 있습니다. 이전 콜백 URL을 새로고침하거나 공유하지 말고, 연결이 실패했다면 `현재 계정 가져오기`부터 다시 시작하세요. 선택한 계정은 최종 미리보기와 게시 기록에도 함께 저장됩니다.

## 3. STEP 02 — 쿠팡 상품 검색과 딥링크

1. 상품명 검색창에 한국어 상품명을 입력합니다.
2. 최대 10개의 쿠팡 상품 결과에서 이미지, 상품명, 가격 등 확인 가능한 정보를 비교합니다.
3. 게시할 상품을 선택하면 해당 상품과 선택한 채널 ID 기준으로 파트너스 딥링크가 생성됩니다.

검색 대신 쿠팡 URL을 직접 입력할 수도 있습니다. 상세 페이지가 `Access Denied`로 막혀 상품명이 비어 있으면 `Chrome에서 확인`을 눌러 로그인된 로컬 Google Chrome 세션에서 상품 정보를 읽습니다. 그래도 확인되지 않을 때만 `상품명 직접 입력`을 사용하세요.

## 4. STEP 03 — 페르소나별 한국어 문구

`문구 생성`은 Codex CLI를 비대화형으로 호출해 상품을 직접 설명하기보다 궁금증을 남기는 짧은 한국어 본문을 만듭니다. 고정 페르소나는 다음 6개입니다.

- 호기심
- 현실 공감
- 문제 해결
- 솔직한 발견
- 스토리
- 구매 전환

필요하면 `커스텀 페르소나`에 원하는 짧은 말투 지시를 한 개 추가할 수 있습니다. 생성된 카드 중 본문 하나를 선택하고, 결과가 마음에 들지 않으면 `이 버전 다시 만들기`로 선택한 페르소나만 다시 생성합니다. Codex CLI가 없거나 로그인/호출에 실패하면 로컬 템플릿으로 전환됩니다.

가격, 배송일, 리뷰 수, 재고처럼 바뀌기 쉬운 정보와 확인되지 않은 사용 후기는 생성 문구에서 주장하지 않습니다.

## 5. STEP 04 — RedNote 중국어 영상 검색

상품명을 바탕으로 RedNote에서 실제로 사용할 중국어 검색어를 정확히 하나 만듭니다. `검색어 다시 만들기`를 누르면 검색어가 추가되는 것이 아니라 기존 검색어를 새 검색어 하나로 교체합니다.

`영상 검색`은 사용자가 평소 로그인해 둔 Google Chrome 프로필로 RedNote를 열고, 검색 결과 중 영상 게시물만 최대 10개 가져옵니다. 로그인 상태가 풀렸다면 Chrome에서 RedNote에 다시 로그인한 뒤 검색하세요.

`Chrome에서 확인`과 RedNote 검색은 macOS 로컬 실행 전용 기능입니다. `시스템 설정 → 개인정보 보호 및 보안 → 자동화`에서 실행에 사용하는 터미널 앱(필요한 경우 Python 또는 Node)이 Google Chrome을 제어하도록 허용하세요. Apple Events 권한을 거부했다면 이 설정에서 다시 켠 뒤 스튜디오를 재시작합니다.

## 6. STEP 05 — MP4 다운로드와 대표 JPG 구성

검색 결과에서 영상 게시물 하나를 선택하고 `선택 영상 다운로드`를 누릅니다.

- 게시물의 원본 소스 스트림을 MP4로 `~/Downloads/rednote`에 저장합니다.
- 영상의 서로 다른 중요 장면 3~5장을 JPG로 자동 추출합니다.
- Threads 미디어는 **MP4 1개** 또는 **선택한 JPG 2~5장** 중 한 형식만 사용할 수 있습니다.
- 이미지 모드에서는 최종 게시 순서까지 확인한 뒤 선택을 저장합니다.

이 기능은 워터마크 제거 기능이 아닙니다. 원본 소스에 워터마크나 출처 표시가 포함되어 있으면 그대로 유지되며, 이를 제거하거나 우회하지 마세요.

## 7. STEP 06 — 최종 확인과 발행

최종 미리보기에서 다음 항목을 모두 확인합니다.

- 선택한 Threads 계정
- 쿠팡 상품과 선택한 한국어 본문
- MP4 1개 또는 순서가 정해진 JPG 2~5장
- 쿠팡 파트너스 광고 고지와 선택 상품 딥링크가 들어간 첫 댓글

광고 고지와 딥링크 댓글은 서버가 잠가 생성하므로 수정·삭제할 수 없습니다. 확인 체크박스를 선택한 뒤 `Threads에 게시`를 누르면 AWS가 미디어 게시물을 만들고 같은 게시물에 광고 고지 댓글을 답니다.

선택한 미디어는 AWS 임시 저장소에 업로드된 뒤 Meta가 읽을 수 있는 capability URL로 바뀝니다. URL과 파일의 기본 수명은 24시간이며, 만료된 뒤 게시하거나 복구하려면 미디어를 다시 업로드해야 합니다. URL을 아는 사람은 만료 전까지 파일을 읽을 수 있으므로 공유하거나 로그에 남기지 마세요.

본문은 게시됐지만 댓글만 실패한 경우에는 같은 기록의 `댓글만 다시 게시`를 사용합니다. 저장된 Threads post ID와 같은 idempotency key로 댓글만 재시도하므로 본문을 다시 만들지 않습니다.

AWS는 SQLite 기반 lease와 fencing token으로 같은 작업의 동시 발행을 막고, idempotency key로 재요청을 기존 작업에 연결합니다. 브라우저를 새로고침했더라도 새 게시 작업을 만들지 말고 기록에 남은 작업을 이어서 처리하세요.

`PUBLISH_OUTCOME_UNKNOWN`이 표시되면 Meta에 요청이 전달된 뒤 응답을 확정하지 못한 상태입니다. 중복 게시 가능성이 있으므로 자동 재시도도, 버튼을 이용한 수동 재시도도 하지 마세요. 선택한 Threads 계정을 직접 열어 본문과 댓글이 실제 게시됐는지 확인해야 합니다.

## 8. 게시 기록 확인

`게시 기록` 영역에서 확인할 수 있습니다.

저장되는 값:

- 발행 시각
- 상품명
- 쿠팡 URL
- 발행 프로필
- Threads username
- Threads post ID
- 조회수
- 좋아요
- 댓글 수
- 리포스트 수
- 인용 수
- 공유 수
- 지표 마지막 갱신 시각
- 실제 발행 본문과 댓글 문구

각 기록의 `지표 새로고침`을 누르면 AWS Threads API 서버가 Meta Threads Insights API를 호출해 최신 지표를 저장하고 화면에 반영합니다.

지표 조회에는 Meta 앱 OAuth scope `threads_manage_insights`가 필요합니다. 이 권한을 추가하기 전에 연결한 프로필은 `현재 계정 가져오기`로 다시 연결해야 지표 조회 권한이 토큰에 포함됩니다.

발행 기록 API:

```text
GET /api/threads/publish-records
POST /api/threads/publish-records/{job_id}/insights
POST /api/threads/publish-records/{job_id}/permalink
DELETE /api/threads/publish-records/{job_id}
```

## 토큰 갱신

프로필 목록에서 `토큰 갱신` 버튼을 누르면 해당 프로필의 long-lived token을 갱신합니다.

Threads 토큰은 만료될 수 있으므로 주기적으로 갱신해야 합니다.

## 주의사항

- 발행은 자동으로 실행되지 않습니다. 반드시 `Threads에 게시` 버튼을 눌러야 합니다.
- 최종 확인에 표시되는 쿠팡 파트너스 고지 문구와 딥링크 댓글은 서버가 고정해 생성합니다. 삭제하거나 우회하지 마세요.
- 관계 법령, 쿠팡 파트너스 정책 또는 플랫폼 정책이 본문 첫 부분의 고지를 요구할 수 있습니다. 댓글 고지만으로 충분하다고 가정하지 말고, 적용되는 기준에 따라 본문에도 눈에 띄게 고지하세요.
- RedNote 다운로드는 원본 소스 스트림 저장만 지원하며 워터마크 제거 기능은 제공하지 않습니다. 다운로드 가능 여부는 재게시 권리와 무관하므로 직접 제작했거나 사용 허락과 Threads 재게시 권리를 확인한 미디어만 선택하고 워터마크 또는 출처 표시를 제거하지 마세요.
- 임시 미디어 URL은 기본 24시간 동안만 유효하며 만료되면 다시 업로드해야 합니다.
- 가격, 배송일, 리뷰 수처럼 자주 바뀌는 정보는 글에서 제외하도록 생성됩니다.
- 쿠팡 상품명이 자동 확인되지 않으면 로컬 앱에서 `Chrome 확인`을 먼저 시도하고, 그래도 실패하면 `상품명 직접 입력`을 사용하세요.
- Threads App Secret은 AWS 환경변수에만 둡니다.
- Threads Access Token과 발행 기록은 AWS SQLite DB에 저장됩니다.
- AWS 서버에는 반드시 `THREADS_BRIDGE_API_KEY`를 설정하고, 로컬에는 같은 값을 `Threads Service API Key`로 저장하세요.
- `workbench_data/workbench.sqlite3`, `workbench_data/threads_api.sqlite3` 파일을 외부에 공유하지 마세요.

## 로컬 데이터 위치

```text
workbench_data/workbench.sqlite3
```

서버 로그:

```text
workbench_data/server.log
```

## 문제 해결

AWS API 서버에서 `Threads API env settings are required`가 나오면:

- `THREADS_APP_ID`
- `THREADS_APP_SECRET`
- `THREADS_REDIRECT_URI`

세 환경변수가 서버 프로세스에 들어갔는지 확인합니다.

`Threads profile is not connected`가 나오면:

- `현재 계정 가져오기`를 눌러 OAuth 연결을 완료해야 합니다.

발행 후 기록이 안 보이면:

- 게시 기록의 `새로고침`을 누릅니다.
- `/api/threads/publish-records`가 정상 응답하는지 확인합니다.

본문은 보이는데 쿠팡 파트너스 댓글이 없으면:

- 같은 작업에서 `댓글만 다시 게시`를 누릅니다.
- 일반 발행 버튼을 반복해서 눌러 본문을 다시 만들지 마세요.
- 미디어 URL이 24시간을 넘겨 만료됐다면 미디어를 다시 선택하고 업로드한 뒤 재시도합니다.

`PUBLISH_OUTCOME_UNKNOWN`이 나오면:

- 게시 요청 결과를 확정할 수 없으므로 재시도하지 않습니다.
- 선택한 Threads 계정을 직접 열어 본문과 광고 고지 댓글의 게시 여부를 확인합니다.
- 실제 게시 여부를 확인하기 전에는 같은 콘텐츠로 새 작업도 만들지 마세요.
