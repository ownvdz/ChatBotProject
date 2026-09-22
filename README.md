# 통학길 — 대중교통·학사 웹 서비스

디스코드 봇(`main.py`)을 FastAPI 웹 서비스로 전환한 버전이다. 첨부해주신 기존 `main.py`는 그대로
백업 폴더에 보관하면 되고, 여기 있는 `main.py`가 정제된 새 버전이다.

## 1. 기존 코드 정리 내역

### 그대로 옮긴 것 (로직은 동일, 함수 형태로 재사용)
- TAGO 호출/파싱, `OpenAPI_ServiceResponse` 에러 처리, `resultCode` 처리
- ODsay 호출, `error` 필드가 dict/list 두 형태로 오는 것 처리
- 버스 매칭: `getRouteNoList` → 경유정류소(24h 캐시) → 탑승 정류소 결정(반경+방향+이름) → 도착정보(15초 캐시)
- 지하철 매칭: 역 후보 검색 → 시간표(평일 대체 포함) → 종점역명 그룹화 → `way`(방면) 매칭
- 캐시 TTL 값들 (도착 15초, 노선/정류소 24시간 등), nodeord 기반 방향 판정, 511번 기본 노선 보장

### 정리(제거)한 것
- `discord.py`, `discord.ext.commands`, 슬래시 커맨드 데코레이터, `interaction.response.*` 등 디스코드 전용 코드 전부
- 디스코드 임베드 문구 생성 함수(마크다운/이모지로 채팅에 보여주던 텍스트) — 웹에서는 구조화된 JSON을 내려주고
  화면 문구는 `static/app.js`가 만들도록 역할을 나눴다
- 디버그용 `print()`/주석 처리된 실험 코드, 미사용 헬퍼 함수
- 환경변수 중 `DISCORD_TOKEN`, `DISCORD_GUILD_ID` 등 디스코드 전용 값

### 새로 추가한 것
- FastAPI 앱 구조(`/api/*` + 정적 파일 서빙), Pydantic 없이 Query 파라미터 검증
- 에러를 항상 `{"error": {"code", "message"}}` 형태의 안전한 JSON으로 통일 (키/URL 노출 방지)
- 길찾기 구간별 실시간 조회를 `ThreadPoolExecutor`로 동시 처리 (기존엔 순차 처리라 느렸음)
- 구간 실시간 조회가 실패해도 경로 요약은 항상 반환 (기존엔 헬퍼 예외 시 전체 실패)
- IP별 요청 제한(1분당, `/api/route`는 더 엄격하게), TAGO/ODsay 일시 오류 재시도
- 시간대를 `Asia/Seoul`로 고정 (서버가 UTC여도 동일하게 동작)

## 2. 프로젝트 구조

```
transit-web/
├── main.py             # FastAPI 앱 (API + 정적 파일 서빙)
├── requirements.txt
├── .env.example         # 실제 키는 .env로 복사해서 채우기 (.env는 git 제외)
├── .gitignore
├── timetable.json        # 기존 파일 그대로
├── DEPLOY.md             # Oracle 서버 배포 가이드 (systemd + cloudflared)
└── static/
    ├── index.html        # 탭 구조 (길찾기/지하철/버스/시간표)
    ├── app.js             # 순수 JS, 빌드 도구 없음, 서버 응답은 textContent로만 출력
    └── style.css          # 모바일 우선, 정류장 전광판 컨셉
```

## 3. 로컬 실행 (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env   # PUBLIC_TAGO_API_KEY, ODSAY_API_KEY 입력
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

브라우저에서 `http://127.0.0.1:8000` 접속. (현재 위치 버튼은 HTTPS가 아니라서 로컬에선 동작하지 않는다 —
Chrome은 `localhost`를 예외로 허용하니 `127.0.0.1` 대신 `http://localhost:8000`으로 열면 테스트할 수 있다.)

API 문서(Swagger UI)는 `http://127.0.0.1:8000/api/docs` 에서 볼 수 있다.

서버 배포는 `DEPLOY.md` 참고.

## 4. API 요약

모든 응답은 성공 시 데이터를, 실패 시 `{"error": {"code": "...", "message": "한글 메시지"}}`를 반환한다
(HTTP 상태 코드도 같이 내려간다: 404/422/429/502/503 등).

| 엔드포인트 | 설명 |
|---|---|
| `GET /api/health` | 상태 확인. 키 설정 여부(`tago_key_configured`, `odsay_key_configured`)만 알려줌 |
| `GET /api/timetable?day=월요일` | 학사 시간표. `day` 생략 시 오늘 |
| `GET /api/subway` | 인천2호선 마전역↔주안역 기본 구간 다음 열차 |
| `GET /api/subway?line=인천2호선&station=검단사거리역` | 지정 역의 다음 열차 |
| `GET /api/bus` | 511번 양방향 정류장 실시간 도착 |
| `GET /api/bus/locations` | 511번 전체 버스 실시간 위치 |
| `GET /api/route?start=마전역&goal=주안역환승정류장` | 대중교통 길찾기 + 구간별 실시간 |
| `GET /api/route?goal=주안역&start_x=126.68&start_y=37.55` | 좌표(현재 위치) 출발 길찾기 |
| `GET /api/geocode/reverse?x=126.68&y=37.55` | 좌표 → 주소 (현재 위치 버튼이 실제로 어딜 잡았는지 표시용, OpenStreetMap Nominatim) |

프론트는 이 API만 호출한다. 나중에 모바일 앱을 만들 때도 동일한 `/api/*`를 그대로 쓰면 된다
(키는 서버에만 있고, 앱은 서버 주소만 알면 된다).

## 5. 로드맵에서 아직 안 한 것 (다음 단계 후보)

- 목적지 장소명 검색 (카카오 로컬 API 등) — 지금은 ODsay `searchStation`(정류장/역 이름)만 지원
- 경로가 여러 개 중 선택 (지금은 `paths[0]`만 사용, ODsay 알려진 약점 그대로 유지)
- 게시판(SQLite CRUD), 사용자별 자주 타는 노선 등록, 학식/학사일정 크롤링, 모바일 앱
- ODsay `way` 값과 TAGO 종점역명 일치 여부, 511 아닌 노선에서 `getRouteNoList` 동작 — 아직 실데이터 미검증이라
  다른 노선을 추가하기 전에 먼저 확인 필요