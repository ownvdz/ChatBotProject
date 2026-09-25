"""
인하대/인천대 대중교통·학사 통합 서비스 — FastAPI 백엔드

디스코드 봇에서 웹 서비스로 전환한 버전이다.

- 로직(TAGO/ODsay 호출, 정류장·열차 매칭, 시간 계산)은 함수로 두고, 엔드포인트는 결과를 JSON으로
  돌려주기만 한다. 응답에는 이모지/마크다운 없이 구조화된 값만 담아서, 나중에 모바일 앱이 같은
  /api/* 를 그대로 쓸 수 있게 했다. 화면 문구는 프론트(static/app.js)가 만든다.
- API 키는 서버(.env)에만 있다. 외부 API 오류 원문(URL/키가 섞일 수 있음)은 로그에만 남기고,
  클라이언트에는 안전한 한글 문구만 내려준다.
- Python 3.10 호환 (Oracle Ubuntu 22.04 기본 버전).

개발 실행:  uvicorn main:app --reload --host 127.0.0.1 --port 8000
"""
import concurrent.futures
import datetime as dt
import functools
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# =============================================================================
# 설정
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TIMETABLE_PATH = BASE_DIR / "timetable.json"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("transit")


def _env(name: str, default: str = "") -> str:
    """빈 문자열/공백도 '없음'으로 보고 기본값을 쓴다 (.env에 `KEY=` 만 적어둔 경우 대비)."""
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# 버스/지하철 API 모두 국토교통부(TAGO). 예전 이름(PUBLIC_BUS_API_KEY)도 폴백으로 인정한다.
TAGO_API_KEY = _env("PUBLIC_TAGO_API_KEY") or _env("PUBLIC_BUS_API_KEY")
ODSAY_API_KEY = _env("ODSAY_API_KEY")
KAKAO_REST_API_KEY = _env("KAKAO_REST_API_KEY")  # 목적지 자유 검색용 (선택, 없어도 정류장/역 검색은 동작)
DEFAULT_CITY_CODE = _env("BUS_CITY_CODE", "23")
DEFAULT_ROUTE_ID = _env("BUS_ROUTE_ID", "ICB365000073")

# 0 이하로 두면 해당 제한을 끈다.
RATE_LIMIT_PER_MIN = _env_int("RATE_LIMIT_PER_MIN", 40)             # /api 전체, IP당 1분
ROUTE_RATE_LIMIT_PER_MIN = _env_int("ROUTE_RATE_LIMIT_PER_MIN", 10)  # /api/route(ODsay 쿼터 보호), IP당 1분
GEOCODE_RATE_LIMIT_PER_MIN = _env_int("GEOCODE_RATE_LIMIT_PER_MIN", 20)  # /api/geocode/reverse, IP당 1분
PLACES_RATE_LIMIT_PER_MIN = _env_int("PLACES_RATE_LIMIT_PER_MIN", 60)  # /api/places/search(자동완성), IP당 1분

if not TAGO_API_KEY:
    log.warning("PUBLIC_TAGO_API_KEY 가 없습니다. 버스/지하철 API는 503을 반환합니다.")
if not ODSAY_API_KEY:
    log.warning("ODSAY_API_KEY 가 없습니다. /api/route 는 503을 반환합니다.")
if not KAKAO_REST_API_KEY:
    log.info("KAKAO_REST_API_KEY 가 없습니다. 목적지 검색은 정류장/역 이름만 가능합니다.")

# 시간 계산은 서버 시간대와 무관하게 항상 한국 시간. (Oracle 서버 기본 시간대가 UTC일 수 있음)
# tzdata 가 없어도 동작하도록, 못 찾으면 고정 +09:00 을 쓴다 (한국은 서머타임이 없다).
try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover - tzdata 미설치 환경
    KST = dt.timezone(dt.timedelta(hours=9), "KST")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# ---- 외부 API 호스트 / 공통 -------------------------------------------------
# TAGO: 성공 코드는 "00", 루트 태그 <response>. 게이트웨이 공통 에러는 <OpenAPI_ServiceResponse>.
TAGO_HOST = "https://apis.data.go.kr/1613000"
ODSAY_HOST = "https://api.odsay.com/v1/api"
HTTP_TIMEOUT = 5  # 초

REQUEST_HEADERS = {
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

TAGO_ERROR_MESSAGES = {
    "1": "어플리케이션 에러 (APPLICATION_ERROR)",
    "4": "HTTP 처리 오류 (HTTP_ERROR)",
    "5": "기관 서버 응답 시간 초과 (SERVICETIMEOUT_ERROR)",
    "10": "잘못된 요청 파라미터입니다. (INVALID_REQUEST_PARAMETER_ERROR)",
    "12": "해당 오픈API 서비스가 없거나 폐기되었습니다. (NO_OPENAPI_SERVICE_ERROR)",
    "20": "서비스 접근이 거부되었습니다. (SERVICE_ACCESS_DENIED_ERROR)",
    "22": "서비스 요청 제한 횟수를 초과했습니다. (LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR)",
    "30": "등록되지 않은 서비스키입니다. 인증키를 다시 확인해주세요. (SERVICE_KEY_IS_NOT_REGISTERED_ERROR)",
    "31": "기한이 만료된 서비스키입니다. (DEADLINE_HAS_EXPIRED_ERROR)",
    "32": "등록되지 않은 IP입니다. (UNREGISTERED_IP_ERROR)",
    "99": "잘못된 요청 파라메터 에러이거나 알 수 없는 에러입니다.",
}
TRANSIENT_ERROR_HINTS = ("HTTP_ERROR", "SERVICETIMEOUT", "APPLICATION_ERROR")

# ---- 캐시 TTL (TAGO 가이드: 도착정보 10~20초, 노선/정류소 일 1회, 지하철 시간표 주 1회 갱신) ----
ARRIVAL_TTL_SEC = 15
LOCATION_TTL_SEC = 15
ROUTE_INFO_TTL_SEC = 24 * 3600
SCHEDULE_TTL_SEC = 6 * 3600
SUBWAY_STATION_TTL_SEC = 24 * 3600
NEGATIVE_TTL_SEC = 600          # '검색 결과 없음'은 짧게만 기억
STATION_SEARCH_TTL_SEC = 3600   # ODsay searchStation
PATH_TTL_SEC = 300              # ODsay 경로 (실시간 정보는 여기에 포함하지 않음)

REALTIME_DEADLINE_SEC = 12      # 실시간 조회가 이 시간을 넘기면 포기하고 경로 요약만 내려준다

# ---- 버스 매칭 ---------------------------------------------------------------
BUS_ROUTE_NO = "511"
BOARD_MATCH_RADIUS_M = 150  # 탑승 정류장: ODsay 좌표와 TAGO 좌표 차이를 넉넉히 허용
END_MATCH_RADIUS_M = 60     # 하차 정류장: 방향 판정용이라 이웃 정류소가 섞이지 않게 좁게

# /api/bus 에서 보여줄 정류장 (양방향 종점). toward = 그 정류장에서 타면 가는 방면
TARGET_STOPS = [
    {"name": "주안역환승정류장", "toward": "정석항공과학고"},
    {"name": "정석항공과학고", "toward": "주안역환승정류장"},
]
# 같은 이름의 정류장이 노선 안에 여러 번(nodeord) 나올 때, 필요 없는 쪽을 제외한다.
# 정석항공과학고는 26번(방향1 도착쪽)은 필요 없고 8번(방향2 출발쪽)만 보여준다.
EXCLUDED_NODEORDS = {"정석항공과학고": {"26"}}

# ---- 지하철 ------------------------------------------------------------------
# /api/subway 를 인자 없이 호출했을 때 보여줄 기본 구간 (탑승역, 도착역(표시용), 방면=진행 방향 종점)
DEFAULT_SUBWAY_LINE = "인천2호선"
DEFAULT_SUBWAY_ROUTES = [
    ("마전역", "주안역", "운연"),
    ("주안역", "마전역", "검단오류"),
]


# =============================================================================
# 공통 유틸: 오류, 캐시, 스레드풀, 요청 제한
# =============================================================================
class ApiError(Exception):
    """클라이언트에 그대로 보여줘도 안전한 메시지만 담는다. (URL/키/내부 예외 원문 금지)"""

    def __init__(self, message: str, status: int = 502, code: str = "upstream_error",
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.headers = headers or {}


_SECRET_RE = re.compile(r"(serviceKey|apiKey)=[^&\s'\"]+", re.IGNORECASE)


def _redact(value: Any) -> str:
    """로그에 남길 문자열에서 API 키를 가린다. (requests 예외 메시지에는 키가 든 URL이 섞일 수 있다)"""
    text = _SECRET_RE.sub(r"\1=***", str(value))
    for secret in (TAGO_API_KEY, ODSAY_API_KEY):
        if secret:
            text = text.replace(secret, "***")
    return text


_MISS = object()


class TTLCache:
    """스레드 안전한 TTL 캐시. 항목 수 상한이 있어서 사용자 입력이 키여도 메모리가 무한히 늘지 않는다.
    꺼낸 값은 읽기 전용으로 다룬다 (수정하려면 복사해서 쓸 것)."""

    def __init__(self, max_items: int = 512):
        self._data: "OrderedDict[Any, Tuple[float, Any]]" = OrderedDict()
        self._max = max_items
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return _MISS
            expires, value = entry
            if expires <= now:
                del self._data[key]
                return _MISS
            return value

    def set(self, key: Any, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


def cached(cache: TTLCache, key: Any, ttl: float, loader: Callable[[], Any],
           store_if: Callable[[Any], bool] = lambda v: True) -> Any:
    """캐시에 있으면 반환, 없으면 loader() 결과를 저장. loader가 예외를 내면 아무것도 저장하지 않는다."""
    hit = cache.get(key)
    if hit is not _MISS:
        return hit
    value = loader()
    if store_if(value):
        cache.set(key, value, ttl)
    return value


# 구간별 실시간 조회 등을 동시에 돌리는 공용 스레드풀.
# (요청 자체는 FastAPI가 일반 def 를 스레드풀에서 돌리므로, 여기서는 그 안에서 병렬화할 때만 쓴다.
#  풀 안에서 다시 이 풀에 작업을 넣지 않는다 → 데드락 방지)
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="transit")


def run_parallel(jobs: Dict[Any, Callable[[], Any]], timeout: float) -> Dict[Any, Tuple[bool, Any]]:
    """jobs 를 동시에 실행해 {키: (성공여부, 값 또는 예외)} 로 돌려준다. 시간 초과 항목은 (False, ApiError)."""
    futures = {key: _pool.submit(fn) for key, fn in jobs.items()}
    concurrent.futures.wait(list(futures.values()), timeout=timeout)
    out: Dict[Any, Tuple[bool, Any]] = {}
    for key, fut in futures.items():
        if not fut.done():
            fut.cancel()
            out[key] = (False, ApiError("응답이 늦어지고 있어요.", 504, "timeout"))
            continue
        exc = fut.exception()
        out[key] = (False, exc) if exc is not None else (True, fut.result())
    return out


def unwrap(result: Tuple[bool, Any]) -> Any:
    """run_parallel 결과 하나를 꺼낸다. 실패면 ApiError 를 던진다 (예상 못 한 예외는 로그 + 500)."""
    ok, value = result
    if ok:
        return value
    if isinstance(value, ApiError):
        raise value
    log.error("예상하지 못한 오류", exc_info=value)
    raise ApiError("서버 내부 오류가 발생했어요.", 500, "internal_error")


class RateLimiter:
    """IP별 슬라이딩 윈도우 제한 (프로세스 메모리). uvicorn 워커 1개로 운영하는 전제."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: Dict[str, deque] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def check(self, key: str) -> Optional[int]:
        """허용이면 None, 초과면 다시 시도할 때까지 남은 초를 반환."""
        if self.limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            self._calls += 1
            if self._calls % 500 == 0:
                self._prune(now)
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(int(self.window - (now - q[0])) + 1, 1)
            q.append(now)
            return None

    def _prune(self, now: float) -> None:
        stale = [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]
        for k in stale:
            del self._hits[k]


_LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> str:
    """요청자 식별용 키. Cloudflare Tunnel(cloudflared)은 서버 로컬에서 접속하므로, 직접 연결한 쪽이
    루프백일 때만 CF-Connecting-IP 를 믿는다 (외부에서 이 헤더를 위조해도 소용없게).
    IPv6 는 /64 단위로 묶는다 (주소 하나만 바꿔가며 제한을 피하는 것 방지)."""
    peer = request.client.host if request.client else "unknown"
    raw = peer
    if peer in _LOOPBACK:
        forwarded = (request.headers.get("cf-connecting-ip") or "").strip()
        if forwarded:
            raw = forwarded
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return peer
    if ip.version == 6:
        return str(ipaddress.ip_network("{}/64".format(ip), strict=False))
    return str(ip)


general_limiter = RateLimiter(RATE_LIMIT_PER_MIN)
route_limiter = RateLimiter(ROUTE_RATE_LIMIT_PER_MIN)
geocode_limiter = RateLimiter(GEOCODE_RATE_LIMIT_PER_MIN)
places_limiter = RateLimiter(PLACES_RATE_LIMIT_PER_MIN)


def _limit_dependency(limiter: RateLimiter):
    async def dependency(request: Request) -> None:
        retry = limiter.check(client_ip(request))
        if retry is not None:
            raise ApiError(
                "요청이 너무 많아요. {}초 뒤에 다시 시도해 주세요.".format(retry),
                429, "rate_limited", {"Retry-After": str(retry)},
            )

    return dependency


# =============================================================================
# 작은 헬퍼들
# =============================================================================
def _normalize(text: str) -> str:
    return (text or "").replace(" ", "")


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_xy(x: Any, y: Any) -> Optional[Tuple[float, float]]:
    """ODsay 좌표(x=경도, y=위도) → (경도, 위도). 값이 없거나 이상하면 None."""
    fx, fy = _to_float(x), _to_float(y)
    return (fx, fy) if fx is not None and fy is not None else None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _iso(now: Optional[dt.datetime] = None) -> str:
    return (now or now_kst()).isoformat(timespec="seconds")


def daily_type_code(now: dt.datetime) -> str:
    """TAGO 지하철 dailyTypeCode: 01=평일, 02=토요일, 03=일요일. (공휴일은 구분하지 않는다)"""
    wd = now.weekday()  # 0=월 ... 6=일
    if wd == 5:
        return "02"
    if wd == 6:
        return "03"
    return "01"


def _require_tago() -> None:
    if not TAGO_API_KEY:
        raise ApiError("서버에 버스/지하철 API 키가 설정되지 않았어요.", 503, "config_missing")


# =============================================================================
# TAGO(국토교통부) 공통 호출
# =============================================================================
_thread_local = threading.local()


def _session() -> requests.Session:
    """스레드마다 Session 하나 (연결 재사용, 스레드 간 공유 없음)."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(REQUEST_HEADERS)
        _thread_local.session = s
    return s


class _TagoResultError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.message = message
        self.transient = transient


def _parse_tago_response(xml_bytes: bytes) -> List[Dict[str, str]]:
    """TAGO 공통 응답 파싱. item 의 모든 하위 태그를 dict 로 만든다 (API마다 필드가 달라 범용 처리).
    성공(코드 '00')이면 item 목록, 실패면 _TagoResultError. '데이터 없음'은 빈 목록."""
    root = ET.fromstring(xml_bytes)

    if root.tag == "OpenAPI_ServiceResponse":  # 게이트웨이 레벨 오류
        err_msg = root.findtext(".//errMsg", "SERVICE ERROR")
        auth_msg = root.findtext(".//returnAuthMsg", "")
        reason_code = root.findtext(".//returnReasonCode", "")
        friendly = TAGO_ERROR_MESSAGES.get(reason_code, auth_msg or err_msg)
        message = "{} - {}".format(err_msg, friendly)
        raise _TagoResultError(message, transient=any(h in message for h in TRANSIENT_ERROR_HINTS))

    result_code = root.findtext(".//resultCode")
    if result_code is None:
        raise _TagoResultError("API 응답 형식이 올바르지 않습니다. (resultCode 없음)")
    if result_code != "00":
        result_msg = root.findtext(".//resultMsg", "")
        if "없습니다" in result_msg or "NODATA" in result_msg.upper():
            return []
        friendly = TAGO_ERROR_MESSAGES.get(result_code.lstrip("0") or "0", "알 수 없는 에러코드({})".format(result_code))
        message = friendly + (" - {}".format(result_msg) if result_msg else "")
        raise _TagoResultError(message, transient=any(h in message for h in TRANSIENT_ERROR_HINTS))

    return [{child.tag: (child.text or "") for child in item} for item in root.findall(".//item")]


def tago_get(endpoint: str, params: Dict[str, Any], retries: int = 2, backoff: float = 0.8) -> List[Dict[str, str]]:
    """TAGO 호출 + 파싱 + 일시적 오류 재시도. 실패하면 ApiError."""
    _require_tago()
    url = "{}/{}".format(TAGO_HOST, endpoint)
    query = {"serviceKey": TAGO_API_KEY, "_type": "xml", **params}
    last_message = "버스/지하철 정보 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."

    for attempt in range(retries + 1):
        try:
            resp = _session().get(url, params=query, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("TAGO 네트워크 오류 %s (%d/%d): %s", endpoint, attempt + 1, retries + 1, _redact(exc))
        else:
            if resp.status_code != 200:
                log.warning("TAGO HTTP %s %s (%d/%d)", resp.status_code, endpoint, attempt + 1, retries + 1)
                last_message = "공공 API 서버가 오류를 반환했어요 (HTTP {}).".format(resp.status_code)
            else:
                try:
                    return _parse_tago_response(resp.content)
                except ET.ParseError:
                    log.warning("TAGO XML 파싱 실패 %s: %s", endpoint, _redact(resp.text[:200]))
                    last_message = "공공 API 응답을 해석하지 못했어요."
                except _TagoResultError as exc:
                    if not exc.transient:
                        log.warning("TAGO 오류 %s: %s", endpoint, exc.message)
                        raise ApiError(exc.message, 502, "upstream_error") from None
                    log.warning("TAGO 일시 오류 %s (%d/%d): %s", endpoint, attempt + 1, retries + 1, exc.message)
                    last_message = exc.message
        if attempt < retries:
            time.sleep(backoff)

    raise ApiError(last_message, 502, "upstream_error")


# =============================================================================
# ODsay 호출 (경로 탐색). 실시간은 TAGO 담당.
# =============================================================================
# ODsay 의 localBusID/localStationID 는 TAGO ID(ICB...)와 형식이 달라 직접 쓸 수 없다
# (실측: ODsay route 217000005 / station 163000617 ↔ TAGO routeId ICB365000073 / nodeId ICB363000617).
# 그래서 버스 구간은 노선번호 + 정류장 이름 + 좌표로 TAGO 에서 직접 매칭한다.
def _parse_odsay_error(err: Any) -> Tuple[str, str]:
    """ODsay 의 error 필드는 dict({"code","msg"}) 또는 list([{"code","message"}]) 두 형태로 온다.
    (코드, 메시지) 로 통일."""
    if isinstance(err, list):
        err = err[0] if err else {}
    if not isinstance(err, dict):
        return "", str(err)
    code = str(err.get("code", ""))
    msg = err.get("msg") or err.get("message") or "알 수 없는 에러"
    return code, msg


def odsay_get(endpoint: str, params: Dict[str, Any], retries: int = 2, backoff: float = 0.8) -> Dict[str, Any]:
    """ODsay 호출. 성공하면 result 딕셔너리, 실패하면 ApiError."""
    if not ODSAY_API_KEY:
        raise ApiError("서버에 경로 검색 API 키가 설정되지 않았어요.", 503, "config_missing")
    url = "{}/{}".format(ODSAY_HOST, endpoint)
    query = {**params, "apiKey": ODSAY_API_KEY, "output": "json"}
    last_message = "경로 검색 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요."

    for attempt in range(retries + 1):
        try:
            resp = _session().get(url, params=query, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("ODsay 네트워크 오류 %s (%d/%d): %s", endpoint, attempt + 1, retries + 1, _redact(exc))
        else:
            if resp.status_code != 200:
                log.warning("ODsay HTTP %s %s (%d/%d)", resp.status_code, endpoint, attempt + 1, retries + 1)
                last_message = "경로 검색 서버가 오류를 반환했어요 (HTTP {}).".format(resp.status_code)
            else:
                try:
                    data = resp.json()
                except ValueError:
                    log.warning("ODsay JSON 해석 실패 %s: %s", endpoint, _redact(resp.text[:200]))
                    data = None
                    last_message = "경로 검색 응답을 해석하지 못했어요."
                if isinstance(data, dict):
                    if "error" not in data:
                        return data.get("result", {}) or {}
                    code, msg = _parse_odsay_error(data["error"])
                    log.warning("ODsay 오류 endpoint=%s code=%s msg=%s", endpoint, code, msg)
                    if "ApiKey" in msg:
                        # 인증 실패: 키에 등록한 플랫폼(Server IP 등)이 실행 환경과 안 맞을 때 발생. 재시도 무의미.
                        raise ApiError("경로 검색 서비스 인증에 문제가 있어요. 관리자에게 알려 주세요.", 502, "upstream_auth")
                    if code == "500" and attempt < retries:  # 서버 오류만 재시도
                        last_message = msg
                    else:
                        # -98(너무 가까움), -99(결과 없음) 등은 입력 문제라 ODsay 메시지를 그대로 보여준다.
                        status, err_code = (502, "upstream_error") if code == "500" else (404, "no_result")
                        raise ApiError(msg or "경로 검색에 실패했어요.", status, err_code)
        if attempt < retries:
            time.sleep(backoff)

    raise ApiError(last_message, 502, "upstream_error")


_station_search_cache = TTLCache(500)
_path_cache = TTLCache(300)

STATION_CLASS_NAMES = {1: "버스 정류장", 2: "지하철역"}


def odsay_search_station(name: str) -> List[Dict[str, Any]]:
    """정류장/역 이름 검색 (버스+지하철 모두). 결과 없음은 빈 목록."""
    key = _normalize(name)

    def load() -> List[Dict[str, Any]]:
        try:
            return odsay_get("searchStation", {"stationName": name}).get("station", []) or []
        except ApiError as exc:
            if exc.code == "no_result":
                return []
            raise

    return cached(_station_search_cache, key, STATION_SEARCH_TTL_SEC, load, store_if=lambda v: bool(v))


def odsay_search_path(sx: float, sy: float, ex: float, ey: float) -> List[Dict[str, Any]]:
    """대중교통 길찾기. 좌표는 (경도, 위도). 결과 목록은 읽기 전용으로 다룬다."""
    key = (round(sx, 5), round(sy, 5), round(ex, 5), round(ey, 5))

    def load() -> List[Dict[str, Any]]:
        result = odsay_get("searchPubTransPathT", {"SX": sx, "SY": sy, "EX": ex, "EY": ey})
        paths = result.get("path", []) or []
        if not paths:
            raise ApiError("검색된 경로가 없어요.", 404, "no_result")
        return paths

    return cached(_path_cache, key, PATH_TTL_SEC, load)


def resolve_place(name: str) -> Dict[str, Any]:
    """이름으로 정류장/역을 찾는다. 검색 결과의 첫 번째를 쓴다. (알려진 약점: 같은 이름이 섞이면 의도와 다를 수 있음)"""
    stations = odsay_search_station(name)
    if not stations:
        raise ApiError("'{}' 정류장/역을 찾을 수 없어요.".format(name), 404, "place_not_found")
    st = stations[0]
    xy = _to_xy(st.get("x"), st.get("y"))
    if xy is None:
        raise ApiError("'{}' 의 위치 정보를 받지 못했어요.".format(name), 404, "place_not_found")
    return {
        "query": name,
        "name": st.get("stationName") or name,
        "kind": STATION_CLASS_NAMES.get(_safe_int(st.get("stationClass")), "장소"),
        "x": xy[0],
        "y": xy[1],
    }


# =============================================================================
# 역지오코딩: 좌표(현재 위치) → 사람이 읽을 주소
# =============================================================================
# ODsay/TAGO 는 이름→좌표 검색만 되고 좌표→주소 변환이 없어서, "현재 위치" 버튼을 눌렀을 때
# 정확히 어디로 인식됐는지 보여주기 위해 OpenStreetMap Nominatim(무료, 키 불필요)을 쓴다.
# Nominatim 사용 정책(https://operations.osmfoundation.org/policies/nominatim/)을 지키기 위해:
#  - 브라우저를 흉내내지 않는 전용 User-Agent를 보낸다 (공용 세션의 브라우저 UA와는 다른 세션을 쓴다).
#  - 서버 전체에서 초당 1건으로 제한한다 (사용자별이 아니라 프로세스 전체 기준 — 정책이 그렇게 요구함).
#  - 결과는 캐시해서 같은 좌표를 반복 조회하지 않는다.
NOMINATIM_HOST = "https://nominatim.openstreetmap.org"
NOMINATIM_CONTACT = _env("NOMINATIM_CONTACT")  # 선택: 문제 시 연락받을 이메일/URL (User-Agent에 포함)
_geocode_cache = TTLCache(300)
_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0


def _nominatim_headers() -> Dict[str, str]:
    contact = " ({})".format(NOMINATIM_CONTACT) if NOMINATIM_CONTACT else ""
    return {"User-Agent": "IncheonTransitWeb/1.0{}".format(contact), "Accept-Language": "ko"}


def reverse_geocode(lon: float, lat: float) -> str:
    """좌표 → 주소 문자열. 초당 1건 제한을 지키며 직접 요청 (공용 세션/헤더를 쓰지 않는다)."""
    global _nominatim_last_call
    key = (round(lon, 4), round(lat, 4))

    def load() -> str:
        global _nominatim_last_call
        with _nominatim_lock:
            wait = 1.0 - (time.monotonic() - _nominatim_last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = requests.get(
                    "{}/reverse".format(NOMINATIM_HOST),
                    params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "accept-language": "ko"},
                    headers=_nominatim_headers(), timeout=HTTP_TIMEOUT,
                )
            except requests.RequestException as exc:
                log.warning("Nominatim 네트워크 오류: %s", _redact(exc))
                raise ApiError("현재 위치의 주소를 확인하지 못했어요.", 502, "geocode_error") from None
            finally:
                _nominatim_last_call = time.monotonic()
        if resp.status_code != 200:
            log.warning("Nominatim HTTP %s", resp.status_code)
            raise ApiError("현재 위치의 주소를 확인하지 못했어요.", 502, "geocode_error")
        try:
            data = resp.json()
        except ValueError:
            raise ApiError("현재 위치의 주소를 확인하지 못했어요.", 502, "geocode_error")
        address = (data or {}).get("display_name")
        if not address:
            raise ApiError("이 위치의 주소를 찾지 못했어요.", 404, "geocode_no_result")
        return address

    return cached(_geocode_cache, key, 600, load)


# =============================================================================
# 장소 검색 (자유 목적지): ODsay 정류장/역 이름 검색 + 카카오 로컬 키워드 검색을 합친다
# =============================================================================
# ODsay searchStation 은 정류장/역 이름만 찾아서 "회사", "학교 앞 편의점" 같은 임의 목적지는 못 찾는다.
# 그래서 카카오 로컬 API(키워드 검색)를 더해 임의 장소도 찾을 수 있게 한다.
# 카카오 키가 없으면(KAKAO_REST_API_KEY 미설정) 정류장/역 검색 결과만 반환한다 (기능이 죽지 않고 좁아질 뿐).
KAKAO_KEYWORD_HOST = "https://dapi.kakao.com/v2/local/search/keyword.json"
_kakao_cache = TTLCache(300)
PLACE_SEARCH_TTL_SEC = 3600


def kakao_search_place(query: str, limit: int) -> List[Dict[str, Any]]:
    """카카오 로컬 키워드 검색. 키가 없으면 빈 목록. 인증/네트워크 오류는 ApiError로 올리되, 이 기능은
    '있으면 더 좋은' 보조 기능이라 호출부(search_places)에서 실패해도 정류장/역 검색 결과는 그대로 보여준다."""
    if not KAKAO_REST_API_KEY:
        return []

    def load() -> List[Dict[str, Any]]:
        try:
            resp = requests.get(
                KAKAO_KEYWORD_HOST, params={"query": query, "size": min(max(limit, 1), 15)},
                headers={"Authorization": "KakaoAK {}".format(KAKAO_REST_API_KEY), "Accept-Language": "ko"},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("카카오 로컬 API 네트워크 오류: %s", _redact(exc))
            raise ApiError("장소 검색 서버에 연결하지 못했어요.", 502, "upstream_error") from None
        if resp.status_code == 401:
            log.warning("카카오 로컬 API 인증 실패 (KAKAO_REST_API_KEY 확인 필요)")
            raise ApiError("장소 검색 서비스 인증에 문제가 있어요.", 502, "upstream_auth")
        if resp.status_code != 200:
            log.warning("카카오 로컬 API HTTP %s", resp.status_code)
            raise ApiError("장소 검색에 실패했어요.", 502, "upstream_error")
        try:
            data = resp.json()
        except ValueError:
            raise ApiError("장소 검색 응답을 해석하지 못했어요.", 502, "upstream_error")

        out = []
        for d in (data.get("documents", []) or []):
            xy = _to_xy(d.get("x"), d.get("y"))  # 카카오는 x=경도, y=위도 (문자열)로 내려준다
            if xy is None or not d.get("place_name"):
                continue
            out.append({
                "name": d["place_name"],
                "kind": "장소",
                "address": d.get("road_address_name") or d.get("address_name") or None,
                "x": xy[0], "y": xy[1],
            })
        return out

    return cached(_kakao_cache, (_normalize(query), limit), PLACE_SEARCH_TTL_SEC, load, store_if=lambda v: bool(v))


def search_places(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """정류장/역 + (카카오 키가 있으면) 일반 장소를 합쳐서 후보 목록을 만든다. 정류장/역을 먼저 보여준다
    (버스/지하철 이용이 이 프로젝트의 핵심이라 그쪽을 우선순위로 둠)."""
    results: List[Dict[str, Any]] = []
    seen = set()

    try:
        stations = odsay_search_station(query)
    except ApiError:
        stations = []
    for st in stations:
        xy = _to_xy(st.get("x"), st.get("y"))
        name = st.get("stationName")
        if xy is None or not name:
            continue
        key = (round(xy[0], 5), round(xy[1], 5))
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "name": name,
            "kind": STATION_CLASS_NAMES.get(_safe_int(st.get("stationClass")), "정류장/역"),
            "address": None, "x": xy[0], "y": xy[1],
        })
        if len(results) >= limit:
            return results[:limit]

    try:
        places = kakao_search_place(query, limit)
    except ApiError:
        places = []  # 카카오 쪽이 실패해도 위에서 찾은 정류장/역 결과는 그대로 반환
    for p in places:
        key = (round(p["x"], 5), round(p["y"], 5))
        if key in seen:
            continue
        seen.add(key)
        results.append(p)
        if len(results) >= limit:
            break

    return results[:limit]


# =============================================================================
# 버스 (TAGO): 노선/정류소/도착/위치
# =============================================================================
_route_no_cache = TTLCache(200)
_route_stops_cache = TTLCache(50)
_route_info_cache = TTLCache(50)
_arrival_cache = TTLCache(500)
_location_cache = TTLCache(50)


def get_route_ids_by_no(route_no: str) -> List[str]:
    """노선번호(예: 511)로 해당 도시의 routeid 후보를 찾는다."""

    def load() -> List[str]:
        items = tago_get("BusRouteInfoInqireService/getRouteNoList",
                         {"cityCode": DEFAULT_CITY_CODE, "routeNo": route_no, "numOfRows": "50", "pageNo": "1"})
        return [r["routeid"] for r in items
                if str(r.get("routeno", "")).strip() == str(route_no).strip() and r.get("routeid")]

    return cached(_route_no_cache, str(route_no).strip(), ROUTE_INFO_TTL_SEC, load, store_if=lambda v: bool(v))


def get_route_stops(route_id: str) -> List[Dict[str, str]]:
    """노선 경유 정류소 (nodeid/nodenm/nodeord/gpslati/gpslong/updowncd). 일 1회 갱신 데이터라 24시간 캐시.
    nodeord 는 방향마다 1부터 다시 시작하는 게 아니라 노선 전체를 관통하는 연속 번호다
    (511: 양방향이 8→17→26 식으로 이어지고 17번 주안역환승정류장을 공유)."""

    def load() -> List[Dict[str, str]]:
        return tago_get("BusRouteInfoInqireService/getRouteAcctoThrghSttnList",
                        {"cityCode": DEFAULT_CITY_CODE, "routeId": route_id, "numOfRows": "300", "pageNo": "1"})

    return cached(_route_stops_cache, route_id, ROUTE_INFO_TTL_SEC, load, store_if=lambda v: bool(v))


def get_route_info(route_id: str) -> Optional[Dict[str, str]]:
    """첫차/막차/배차간격. 실패하면 None (부가 정보라 오류로 취급하지 않는다)."""
    hit = _route_info_cache.get(route_id)
    if hit is not _MISS:
        return hit
    try:
        items = tago_get("BusRouteInfoInqireService/getRouteInfoIem",
                         {"cityCode": DEFAULT_CITY_CODE, "routeId": route_id})
    except ApiError:
        return None
    if not items:
        return None
    _route_info_cache.set(route_id, items[0], ROUTE_INFO_TTL_SEC)
    return items[0]


def get_arrivals(node_id: str, route_id: str) -> List[Dict[str, str]]:
    """정류소+노선 지정 도착정보 (arrtime: 초, arrprevstationcnt: 남은 정류장 수). 10~20초 갱신이라 15초 캐시."""

    def load() -> List[Dict[str, str]]:
        return tago_get("ArvlInfoInqireService/getSttnAcctoSpcifyRouteBusArvlPrearngeInfoList",
                        {"cityCode": DEFAULT_CITY_CODE, "nodeId": node_id, "routeId": route_id})

    return cached(_arrival_cache, (node_id, route_id), ARRIVAL_TTL_SEC, load)


def get_bus_locations(route_id: str) -> List[Dict[str, str]]:
    """노선 전체 버스의 실시간 GPS 위치와 최근 통과 정류소."""

    def load() -> List[Dict[str, str]]:
        return tago_get("BusLcInfoInqireService/getRouteAcctoBusLcList",
                        {"cityCode": DEFAULT_CITY_CODE, "routeId": route_id, "numOfRows": "100", "pageNo": "1"})

    return cached(_location_cache, route_id, LOCATION_TTL_SEC, load)


def _arrival_item(b: Dict[str, str], vehicle_no: Optional[str] = None) -> Dict[str, Any]:
    seconds = _safe_int(b.get("arrtime")) or 0
    item = {"minutes": seconds // 60, "seconds": seconds, "prev_stations": _safe_int(b.get("arrprevstationcnt"))}
    if vehicle_no is not None:
        item["vehicle_no"] = vehicle_no
    return item


def _sorted_arrivals(arrivals: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(arrivals, key=lambda b: _safe_int(b.get("arrtime")) or 0)


def _stops_near(stops: List[Dict[str, str]], xy: Optional[Tuple[float, float]],
                radius_m: float = BOARD_MATCH_RADIUS_M) -> List[Tuple[Dict[str, str], float]]:
    """좌표(경도, 위도) 반경 안의 정류소 [(정류소, 거리m)] — TAGO 경유정류소의 gpslati/gpslong 사용."""
    if not xy:
        return []
    lon, lat = xy
    out = []
    for st in stops:
        slat, slon = _to_float(st.get("gpslati")), _to_float(st.get("gpslong"))
        if not st.get("nodeid") or slat is None or slon is None:
            continue
        d = _haversine_m(lat, lon, slat, slon)
        if d <= radius_m:
            out.append((st, d))
    return out


def _match_stops_by_name(stops: List[Dict[str, str]], name: str) -> List[Dict[str, str]]:
    """이름이 일치하는 정류소 레코드 전부. 공백 차이는 무시하고, 정확히 일치하는 게 없을 때만 부분일치."""
    key = _normalize(name)
    if not key:
        return []
    exact = [s for s in stops if _normalize(s.get("nodenm", "")) == key and s.get("nodeid")]
    if exact:
        return exact
    return [s for s in stops if s.get("nodeid") and key in _normalize(s.get("nodenm", ""))]


def order_board_candidates(stops: List[Dict[str, str]], board_name: str, end_name: str,
                           board_xy: Optional[Tuple[float, float]] = None,
                           end_xy: Optional[Tuple[float, float]] = None) -> List[Dict[str, str]]:
    """탑승 정류소 후보를 가장 유력한 순서로 정렬해 반환.
    - 하차 정류장이 진행 방향 앞쪽(nodeord가 더 큼)에 있어야 올바른 방향이다.
    - 탑승 좌표가 있으면 반경 안의 정류소 중에서 (방향 일치 → 이름 일치 → 거리) 순으로 고른다.
      이름 표기가 ODsay와 달라도, 도로 양쪽 정류장이 섞여도 좌표+방향으로 구분된다.
    - 좌표가 없거나 반경 안에 없으면 이름 기반으로 되돌아간다."""
    end_stops = {id(e): e for e in _match_stops_by_name(stops, end_name)}
    for e, _d in _stops_near(stops, end_xy, END_MATCH_RADIUS_M):
        end_stops[id(e)] = e
    end_ords = [o for o in (_safe_int(e.get("nodeord")) for e in end_stops.values()) if o is not None]

    def forward_ords(b: Dict[str, str]) -> List[int]:
        bo = _safe_int(b.get("nodeord"))
        return [eo - bo for eo in end_ords if bo is not None and eo > bo]

    nearby = _stops_near(stops, board_xy)
    if nearby:
        name_key = _normalize(board_name)

        def key(item: Tuple[Dict[str, str], float]):
            st, dist = item
            direction_ok = (not end_ords) or bool(forward_ords(st))
            name_ok = bool(name_key) and _normalize(st.get("nodenm", "")) == name_key
            return (0 if direction_ok else 1, 0 if name_ok else 1, dist)

        return [st for st, _d in sorted(nearby, key=key)]

    boards = _match_stops_by_name(stops, board_name)
    return sorted(boards, key=lambda b: min(forward_ords(b)) if forward_ords(b) else float("inf"))


def _fmt_hhmm(v: Optional[str]) -> str:
    v = (v or "").strip()
    return "{}:{}".format(v[:2], v[2:]) if len(v) == 4 and v.isdigit() else ""


def route_timing(route_id: str, now: dt.datetime) -> Optional[Dict[str, Any]]:
    """도착 예정 버스가 없을 때 덧붙일 노선 정보 (첫차/막차/배차간격). 없으면 None."""
    info = get_route_info(route_id)
    if not info:
        return None
    wd = now.weekday()
    interval = (info.get("intervaltime" if wd < 5 else "intervalsattime" if wd == 5 else "intervalsuntime") or "").strip()
    out = {
        "first": _fmt_hhmm(info.get("startvehicletime")) or None,
        "last": _fmt_hhmm(info.get("endvehicletime")) or None,
        "interval_min": int(interval) if interval.isdigit() else None,
    }
    return out if any(v is not None for v in out.values()) else None


def bus_realtime_for_leg(bus_no: str, board_name: str, end_name: str,
                         board_xy: Optional[Tuple[float, float]], end_xy: Optional[Tuple[float, float]],
                         now: dt.datetime) -> Optional[Dict[str, Any]]:
    """길찾기의 버스 구간용: 노선번호 + 탑승/하차 정류장(이름·좌표)으로 TAGO 실시간 도착 정보를 만든다.
    노선/정류소를 매칭하지 못하면 None (그 구간은 경로 요약만 표시). 외부 API 오류는 ApiError."""
    if not bus_no or not board_name:
        return None

    last_error: Optional[ApiError] = None
    route_ids: List[str] = []
    try:
        route_ids = get_route_ids_by_no(bus_no)
    except ApiError as exc:
        last_error = exc
    # 노선번호 검색 API가 실패해도 프로젝트 기본 대상인 511번은 항상 조회 가능하도록 보장
    if not route_ids and str(bus_no).strip() == BUS_ROUTE_NO:
        route_ids = [DEFAULT_ROUTE_ID]

    for route_id in route_ids:
        try:
            stops = get_route_stops(route_id)
        except ApiError as exc:
            last_error = exc
            continue
        if not stops:
            continue
        candidates = order_board_candidates(stops, board_name, end_name, board_xy, end_xy)
        if not candidates:
            continue

        seen = set()
        reached_api = False  # 도착정보 API가 (빈 결과라도) 정상 응답한 적이 있는가
        for cand in candidates[:4]:
            node_id = cand.get("nodeid")
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            try:
                arrivals = get_arrivals(node_id, route_id)
            except ApiError as exc:
                last_error = exc
                continue
            reached_api = True
            log.debug("버스 매칭 route_id=%s nodeid=%s nodenm=%s nodeord=%s 도착 %d건",
                      route_id, node_id, cand.get("nodenm"), cand.get("nodeord"), len(arrivals))
            if arrivals:
                return {
                    "kind": "bus", "status": "ok", "route_no": bus_no,
                    "stop_name": cand.get("nodenm", ""),
                    "arrivals": [_arrival_item(b) for b in _sorted_arrivals(arrivals)[:3]],
                }
        if reached_api:
            # 노선과 정류소는 매칭됐지만 지금 운행 중인 도착 예정 버스가 없는 경우
            return {"kind": "bus", "status": "no_arrival", "route_no": bus_no,
                    "arrivals": [], "route_info": route_timing(route_id, now)}
        break  # 도착정보 조회가 전부 실패 → 아래에서 오류 처리

    if last_error is not None:
        raise last_error
    return None


# ---- /api/bus: 511번 양방향 정류장 도착 + 차량번호 추정 ----------------------
def build_name_to_nodeords(stops: List[Dict[str, str]]) -> Dict[str, List[int]]:
    """정류소명 → nodeord 목록. 같은 이름의 정류장이 노선 안에 여러 번(양방향) 나오므로 리스트로 전부 보관한다.
    (이름 하나에 순번 하나만 저장하면 나중 값이 앞 값을 덮어써서 틀린 매칭이 나온다)"""
    mapping: Dict[str, List[int]] = {}
    for s in stops:
        ordv = _safe_int(s.get("nodeord"))
        if ordv is None:
            continue
        mapping.setdefault(s.get("nodenm", "").strip(), []).append(ordv)
    return mapping


def estimate_vehicle_plate(target_nodeord: int, arr_prev_cnt: Optional[int], location_items: List[Dict[str, str]],
                           name_to_nodeords: Dict[str, List[int]]) -> Optional[str]:
    """도착정보의 '남은 정류장 수'와 위치정보의 '현재 정류장 순서'를 비교해 가장 그럴듯한 버스의 차량번호를 추정한다.
    (차량번호는 도착정보 API엔 없고 위치정보 API에만 있다) 남은 정류장 수 차이가 1 이하일 때만 인정."""
    if arr_prev_cnt is None:
        return None
    best_plate, best_diff = None, None
    for loc in location_items:
        for cur_ord in name_to_nodeords.get(loc.get("nodenm", "").strip(), []):
            remaining = target_nodeord - cur_ord
            if remaining < 0:
                continue  # 이미 목표 정류장을 지나간 경우는 제외
            diff = abs(remaining - arr_prev_cnt)
            if best_diff is None or diff < best_diff:
                best_diff, best_plate = diff, loc.get("vehicleno")
    if best_plate and best_diff is not None and best_diff <= 1:
        return best_plate
    return None


def _target_stop_records(stops: List[Dict[str, str]], name: str) -> List[Dict[str, str]]:
    """정류소 목록에서 이름이 정확히 일치하는 레코드 (제외 nodeord 뺀 뒤, nodeid 중복 제거, nodeord 순)."""
    excluded = EXCLUDED_NODEORDS.get(name, set())
    records = [s for s in stops
               if s.get("nodenm", "").strip() == name.strip() and s.get("nodeid") and s.get("nodeord") not in excluded]
    records.sort(key=lambda s: _safe_int(s.get("nodeord")) or 0)
    seen, unique = set(), []
    for s in records:
        if s["nodeid"] not in seen:
            seen.add(s["nodeid"])
            unique.append(s)
    return unique


def bus_overview() -> Dict[str, Any]:
    _require_tago()
    stops = get_route_stops(DEFAULT_ROUTE_ID)  # 이게 안 되면 화면을 만들 수 없으므로 오류
    name_to_nodeords = build_name_to_nodeords(stops)

    targets = []  # (설정, 정류소 레코드)
    for cfg in TARGET_STOPS:
        for rec in _target_stop_records(stops, cfg["name"]):
            targets.append((cfg, rec))

    # 위치정보와 정류장별 도착정보를 동시에 조회. 위치정보가 실패해도 도착시간은 계속 보여준다.
    jobs: Dict[Any, Callable[[], Any]] = {"loc": functools.partial(get_bus_locations, DEFAULT_ROUTE_ID)}
    for i, (_cfg, rec) in enumerate(targets):
        jobs[i] = functools.partial(get_arrivals, rec["nodeid"], DEFAULT_ROUTE_ID)
    results = run_parallel(jobs, REALTIME_DEADLINE_SEC)

    loc_ok, loc_val = results["loc"]
    location_items: List[Dict[str, str]] = loc_val if loc_ok else []

    entries = []
    found_names = set()
    for i, (cfg, rec) in enumerate(targets):
        found_names.add(cfg["name"])
        node_ord = _safe_int(rec.get("nodeord"))
        entry: Dict[str, Any] = {
            "name": cfg["name"], "toward": cfg["toward"], "node_ord": node_ord,
            "status": "ok", "arrivals": [], "error": None,
        }
        ok, val = results[i]
        if not ok:
            entry["status"] = "error"
            entry["error"] = val.message if isinstance(val, ApiError) else "도착 정보를 불러오지 못했어요."
            if not isinstance(val, ApiError):
                log.error("버스 도착정보 조회 중 예상 못 한 오류", exc_info=val)
        elif not val:
            entry["status"] = "no_arrival"
        else:
            for b in _sorted_arrivals(val)[:3]:
                plate = None
                if node_ord is not None and location_items:
                    plate = estimate_vehicle_plate(node_ord, _safe_int(b.get("arrprevstationcnt")),
                                                   location_items, name_to_nodeords)
                entry["arrivals"].append(_arrival_item(b, plate))
        entries.append(entry)

    for cfg in TARGET_STOPS:
        if cfg["name"] not in found_names:
            entries.append({"name": cfg["name"], "toward": cfg["toward"], "node_ord": None, "status": "not_found",
                            "arrivals": [], "error": "노선 정류소 목록에서 이 정류장을 찾지 못했어요."})

    order = {cfg["name"]: n for n, cfg in enumerate(TARGET_STOPS)}
    entries.sort(key=lambda e: (order.get(e["name"], 99), e["node_ord"] or 0))
    return {
        "route_no": BUS_ROUTE_NO,
        "route_id": DEFAULT_ROUTE_ID,
        "stops": entries,
        "vehicle_no_is_estimate": True,
        "updated_at": _iso(),
    }


def bus_locations_overview() -> Dict[str, Any]:
    _require_tago()
    items = get_bus_locations(DEFAULT_ROUTE_ID)
    buses = [{
        "vehicle_no": b.get("vehicleno", ""),
        "stop_name": b.get("nodenm", ""),
        "stop_ord": _safe_int(b.get("nodeord")),
        "lat": _to_float(b.get("gpslati")),
        "lon": _to_float(b.get("gpslong")),
    } for b in items]
    buses.sort(key=lambda b: (b["stop_ord"] is None, b["stop_ord"] or 0))  # 노선 진행 순서대로
    return {"route_no": BUS_ROUTE_NO, "buses": buses, "updated_at": _iso()}


# =============================================================================
# 지하철 (TAGO SubwayInfo): 실시간이 아니라 고정 시간표(주 1회 갱신)
# =============================================================================
# 시간표에서 지금 이후의 다음 출발시각을 찾아 "약 n분 후 출발"로 계산해서 보여준다.
# dailyTypeCode: 01=평일, 02=토요일, 03=일요일 / upDownTypeCode: U/D 는 방향이 섞여 나와 신뢰하지 않는다.
# 대신 열차 자신의 종점역명(endSubwayStationNm)으로 그룹화해서(상위 2개가 양방향) 방면을 판단한다.
_subway_station_cache = TTLCache(300)
_subway_schedule_cache = TTLCache(200)


def _strip_way(name: str) -> str:
    """'마전역'/'운연 방면'/'검단오류행' → '마전'/'운연'/'검단오류' (방면 비교용)."""
    name = _normalize(name)
    for suffix in ("방면", "행"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name[:-1] if name.endswith("역") else name


def subway_station_matches(station_name: str, line_keyword: Optional[str] = None) -> List[Dict[str, str]]:
    """키워드 기반 지하철역 목록. line_keyword가 있으면 노선명이 일치하는 항목을 우선한다.
    TAGO 역명 DB가 '역' 글자가 있을 수도 없을 수도 있어서('마전' vs '마전역')
    입력값 그대로 → '역' 붙인/뗀 버전 순으로 결과가 나올 때까지 시도한다."""
    cache_key = (_normalize(station_name), _normalize(line_keyword or ""))
    hit = _subway_station_cache.get(cache_key)
    if hit is not _MISS:
        return hit

    candidates = [station_name]
    candidates.append(station_name[:-1] if station_name.endswith("역") else station_name + "역")

    items: List[Dict[str, str]] = []
    for keyword in candidates:
        items = tago_get("SubwayInfo/GetKwrdFndSubwaySttnList",
                         {"subwayStationName": keyword, "numOfRows": "20", "pageNo": "1"})
        if items:
            break

    result = items
    if line_keyword:
        norm_keyword = _normalize(line_keyword)
        line_matches = [i for i in items if norm_keyword in _normalize(i.get("subwayRouteName", ""))]
        result = line_matches or items  # 노선 필터링 결과가 없으면 전체 결과라도 반환

    _subway_station_cache.set(cache_key, result, SUBWAY_STATION_TTL_SEC if result else NEGATIVE_TTL_SEC)
    return result


def _fetch_both_directions(station_id: str, daily_type: str) -> List[Dict[str, str]]:
    """U/D 시간표를 모두 가져와 합친다 (방향은 나중에 각 열차의 종점역명으로 판단). 둘 다 실패해야 오류."""
    items: List[Dict[str, str]] = []
    any_ok, last_exc = False, None
    for up_down in ("U", "D"):
        try:
            items.extend(tago_get("SubwayInfo/GetSubwaySttnAcctoSchdulList", {
                "subwayStationId": station_id, "dailyTypeCode": daily_type,
                "upDownTypeCode": up_down, "numOfRows": "300", "pageNo": "1"}))
            any_ok = True
        except ApiError as exc:
            last_exc = exc
    if not any_ok and last_exc is not None:
        raise last_exc
    return items


def station_schedule(station_id: str, daily_type: str) -> Tuple[List[Dict[str, str]], bool]:
    """(시간표, 평일 시간표로 대체했는지). 시간표가 비어 있으면 ([], False).
    일부 역은 주말 시간표가 TAGO에 비어 있다(검단사거리역: resultCode=00 이지만 totalCount=0).
    API 오류가 아니라 진짜 데이터 공백이라, 이럴 땐 평일 시간표를 참고용으로 대체하고 표시한다."""
    key = (station_id, daily_type)
    hit = _subway_schedule_cache.get(key)
    if hit is not _MISS:
        return hit

    fallback = False
    primary_error: Optional[ApiError] = None
    try:
        items = _fetch_both_directions(station_id, daily_type)
    except ApiError as exc:
        primary_error, items = exc, []

    if not items and daily_type != "01":
        try:
            items = _fetch_both_directions(station_id, "01")
            fallback = bool(items)
        except ApiError as exc:
            primary_error = primary_error or exc
            items = []

    if not items and primary_error is not None:
        raise primary_error

    seen, deduped = set(), []  # 같은 열차가 U/D 양쪽에 걸려 들어올 가능성 대비
    for it in items:
        k = (it.get("depTime", ""), it.get("endSubwayStationNm", ""))
        if k not in seen:
            seen.add(k)
            deduped.append(it)

    result = (deduped, fallback)
    if deduped:
        _subway_schedule_cache.set(key, result, SCHEDULE_TTL_SEC)
    return result


def group_by_destination(items: List[Dict[str, str]], top_n: int = 2) -> List[Tuple[str, List[Dict[str, str]]]]:
    """열차들을 각자의 종점역명으로 묶어 편수 많은 순으로 top_n개 방향만 반환.
    (대부분의 역에서 '진짜 두 종점 방향'이 편수가 가장 많고, 중간 회차 열차는 뒤로 밀린다)"""
    groups: Dict[str, List[Dict[str, str]]] = {}
    for it in items:
        groups.setdefault(it.get("endSubwayStationNm", "") or "(종점 미상)", []).append(it)
    return sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]


def _service_seconds(h: int, m: int, s: int = 0) -> int:
    """운행일 기준 초. 새벽 4시 이전(00~03시)은 전날 운행의 연장이므로 24시간을 더해서 '자정 넘어 오는 막차'가
    저녁 시간보다 뒤에 오게 한다."""
    total = h * 3600 + m * 60 + s
    return total + 86400 if h < 4 else total


def _dep_seconds(dep: Optional[str]) -> Optional[int]:
    dep = (dep or "").strip()
    if len(dep) == 4:
        dep += "00"
    if len(dep) != 6 or not dep.isdigit():
        return None
    return _service_seconds(int(dep[0:2]), int(dep[2:4]), int(dep[4:6]))


def upcoming_trains(items: List[Dict[str, str]], now: dt.datetime, limit: int) -> List[Dict[str, Any]]:
    now_secs = _service_seconds(now.hour, now.minute, now.second)
    timed = []
    for it in items:
        secs = _dep_seconds(it.get("depTime"))
        if secs is not None and secs >= now_secs:
            timed.append(secs)
    timed.sort()
    return [{
        "time": "{:02d}:{:02d}".format((s // 3600) % 24, (s % 3600) // 60),
        "remain_min": max((s - now_secs) // 60, 0),
    } for s in timed[:limit]]


def find_next_trains(board_name: str, line_name: str, way: str, daily_type: str,
                     now: dt.datetime, limit: int = 3) -> Optional[Dict[str, Any]]:
    """탑승역/노선/방면(진행 방향 종점)으로 시간표에서 '지금 이후' 다음 열차들을 찾는다.
    방향은 열차 자신의 종점역명과 way 를 비교해서 판단한다. 못 찾으면 None, API 오류는 ApiError."""
    if not board_name or not way:
        return None
    matches = subway_station_matches(board_name, line_name or None)
    if not matches:
        return None

    way_key = _strip_way(way)
    last_error: Optional[ApiError] = None
    # 같은 이름의 후보가 여러 개일 수 있어(중복/폐기 항목) 운행정보가 있는 첫 후보를 쓴다.
    for cand in matches[:5]:
        cand_id = cand.get("subwayStationId", "")
        if not cand_id:
            continue
        try:
            sched, fallback = station_schedule(cand_id, daily_type)
        except ApiError as exc:
            last_error = exc
            continue
        if not sched:
            continue
        for g_name, g_items in group_by_destination(sched, top_n=2):
            g_key = _strip_way(g_name)
            if not g_key or not (way_key in g_key or g_key in way_key):
                continue
            return {
                "station": cand.get("subwayStationName", board_name),
                "terminus": g_name,
                "trains": upcoming_trains(g_items, now, limit),
                "fallback": fallback,
            }
    if last_error is not None:
        raise last_error
    return None


def subway_realtime_for_leg(board_name: str, line_name: str, way: str, daily_type: str,
                            now: dt.datetime) -> Optional[Dict[str, Any]]:
    """길찾기의 지하철 구간용. 방향은 하차역이 아니라 ODsay 의 way(방면)로 판단한다. 못 찾으면 None."""
    r = find_next_trains(board_name, line_name, way, daily_type, now, limit=2)
    if r is None:
        return None
    return {
        "kind": "subway",
        "status": "ok" if r["trains"] else "no_more_trains",
        "station": r["station"], "terminus": r["terminus"],
        "trains": r["trains"], "fallback": r["fallback"],
    }


def subway_default_overview() -> Dict[str, Any]:
    _require_tago()
    now = now_kst()
    daily_type = daily_type_code(now)
    jobs = {i: functools.partial(find_next_trains, board, DEFAULT_SUBWAY_LINE, way, daily_type, now, 3)
            for i, (board, _dest, way) in enumerate(DEFAULT_SUBWAY_ROUTES)}
    results = run_parallel(jobs, REALTIME_DEADLINE_SEC)

    routes, any_fallback = [], False
    for i, (board, dest, way) in enumerate(DEFAULT_SUBWAY_ROUTES):
        entry: Dict[str, Any] = {"from": board, "to": dest, "way": way, "status": "ok",
                                 "station": None, "terminus": None, "trains": [], "fallback": False}
        ok, val = results[i]
        if not ok:
            entry["status"] = "error"
            if not isinstance(val, ApiError):
                log.error("지하철 기본 구간 조회 중 예상 못 한 오류", exc_info=val)
        elif val is None:
            entry["status"] = "not_found"
        else:
            entry.update(station=val["station"], terminus=val["terminus"], trains=val["trains"],
                         fallback=val["fallback"])
            if not val["trains"]:
                entry["status"] = "no_more_trains"
            any_fallback = any_fallback or val["fallback"]
        routes.append(entry)

    return {"mode": "default", "line": DEFAULT_SUBWAY_LINE, "fallback": any_fallback,
            "routes": routes, "updated_at": _iso(now)}


def subway_station_overview(line: Optional[str], station: str) -> Dict[str, Any]:
    _require_tago()
    matches = subway_station_matches(station, line)
    if not matches:
        raise ApiError("'{}' 역 정보를 찾지 못했어요.".format(("{} ".format(line) if line else "") + station),
                       404, "station_not_found")

    now = now_kst()
    daily_type = daily_type_code(now)

    # 검색 결과의 첫 번째가 항상 정답은 아니다(같은 이름의 중복/폐기 항목이 섞이면 운행정보가 빈 station_id를
    # 고를 수 있음). 후보를 순서대로 시도해서 실제로 운행정보가 있는 첫 역을 쓴다.
    chosen, sched, fallback, last_error = None, [], False, None
    for cand in matches[:5]:
        cand_id = cand.get("subwayStationId", "")
        if not cand_id:
            continue
        try:
            items, fb = station_schedule(cand_id, daily_type)
        except ApiError as exc:
            last_error = exc
            continue
        if items:
            chosen, sched, fallback = cand, items, fb
            break
    if chosen is None:
        if last_error is not None:
            raise last_error
        raise ApiError("운행정보가 없어요.", 404, "no_schedule")

    directions = []
    for end_name, items in group_by_destination(sched, top_n=2):
        trains = upcoming_trains(items, now, 3)
        directions.append({"terminus": end_name, "status": "ok" if trains else "no_more_trains", "trains": trains})

    return {"mode": "station", "station": chosen.get("subwayStationName", station), "line": line or None,
            "fallback": fallback, "directions": directions, "updated_at": _iso(now)}


# =============================================================================
# 길찾기: ODsay 경로 + 구간별 TAGO 실시간 (동시 조회, 실패해도 경로 요약은 유지)
# =============================================================================
TRAFFIC_KINDS = {1: "subway", 2: "bus", 3: "walk"}


def _first_lane(sub_path: Dict[str, Any]) -> Dict[str, Any]:
    lane = sub_path.get("lane", {})
    if isinstance(lane, list):
        lane = lane[0] if lane else {}
    return lane if isinstance(lane, dict) else {}


def route_overview(start: Optional[str], goal: Optional[str], start_x: Optional[float], start_y: Optional[float],
                   goal_x: Optional[float] = None, goal_y: Optional[float] = None,
                   goal_name: Optional[str] = None) -> Dict[str, Any]:
    now = now_kst()

    # 1) 출발/도착 확정 (좌표가 있으면 그대로 쓰고, 없으면 이름으로 검색 — 검색은 동시에)
    start_use_coords = start_x is not None and start_y is not None
    goal_use_coords = goal_x is not None and goal_y is not None
    jobs: Dict[Any, Callable[[], Any]] = {}
    if not start_use_coords:
        jobs["start"] = functools.partial(resolve_place, start or "")
    if not goal_use_coords:
        jobs["goal"] = functools.partial(resolve_place, goal or "")
    resolved = run_parallel(jobs, 15) if jobs else {}

    if start_use_coords:
        start_place = {"query": start or "", "name": start or "현재 위치", "kind": "현재 위치", "x": start_x, "y": start_y}
    else:
        start_place = unwrap(resolved["start"])
    if goal_use_coords:
        goal_place = {"query": goal_name or goal or "", "name": goal_name or goal or "목적지", "kind": "장소",
                      "x": goal_x, "y": goal_y}
    else:
        goal_place = unwrap(resolved["goal"])

    # 2) 경로 탐색: 첫 번째 경로만 사용
    paths = odsay_search_path(start_place["x"], start_place["y"], goal_place["x"], goal_place["y"])
    best = paths[0]
    info = best.get("info", {}) or {}

    # 3) 구간 요약 (항상 만들어서 돌려준다) + 실시간 조회 대상 수집
    steps: List[Dict[str, Any]] = []
    legs: Dict[int, Tuple[str, Dict[str, Any]]] = {}
    daily_type = daily_type_code(now)
    for sp in best.get("subPath", []) or []:
        kind = TRAFFIC_KINDS.get(sp.get("trafficType"))
        if kind is None:
            continue
        distance, minutes = _safe_int(sp.get("distance")), _safe_int(sp.get("sectionTime"))
        if kind == "walk" and not distance and not minutes:
            continue  # 0m 도보(환승 사이 자리 채움) 생략
        lane = _first_lane(sp)
        step = {
            "type": kind,
            "minutes": minutes,
            "distance_m": distance,
            "line": (lane.get("busNo") or lane.get("name")) if kind != "walk" else None,
            "from": sp.get("startName") if kind != "walk" else None,
            "to": sp.get("endName") if kind != "walk" else None,
            "station_count": _safe_int(sp.get("stationCount")) if kind != "walk" else None,
            "way": (sp.get("way") or None) if kind == "subway" else None,
            "realtime": None,
        }
        steps.append(step)
        if kind != "walk":
            legs[len(steps) - 1] = (kind, sp)

    realtime_enabled = bool(TAGO_API_KEY)
    if realtime_enabled and legs:
        rt_jobs: Dict[Any, Callable[[], Any]] = {}
        for idx, (kind, sp) in legs.items():
            lane = _first_lane(sp)
            if kind == "bus":
                rt_jobs[idx] = functools.partial(
                    bus_realtime_for_leg, lane.get("busNo"), sp.get("startName"), sp.get("endName"),
                    _to_xy(sp.get("startX"), sp.get("startY")), _to_xy(sp.get("endX"), sp.get("endY")), now)
            else:
                rt_jobs[idx] = functools.partial(
                    subway_realtime_for_leg, sp.get("startName"), lane.get("name", ""), sp.get("way", ""),
                    daily_type, now)
        for idx, (ok, val) in run_parallel(rt_jobs, REALTIME_DEADLINE_SEC).items():
            if ok:
                steps[idx]["realtime"] = val  # None 이면 매칭 못 한 구간 → 요약만 표시
            else:
                # 실시간이 실패해도 경로 요약은 반드시 보여준다.
                if not isinstance(val, ApiError):
                    log.error("구간 실시간 조회 중 예상 못 한 오류", exc_info=val)
                steps[idx]["realtime"] = {"kind": steps[idx]["type"], "status": "error"}

    transit_legs = sum(1 for s in steps if s["type"] != "walk")
    return {
        "start": {k: start_place[k] for k in ("query", "name", "kind")},
        "goal": {k: goal_place[k] for k in ("query", "name", "kind")},
        "summary": {
            "total_minutes": _safe_int(info.get("totalTime")),
            "total_distance_m": _safe_int(info.get("totalDistance")),
            "fare": _safe_int(info.get("payment")),
            "transfers": max(transit_legs - 1, 0),
        },
        "steps": steps,
        "realtime_enabled": realtime_enabled,
        "updated_at": _iso(now),
    }


# =============================================================================
# 시간표 (timetable.json)
# =============================================================================
_timetable_state: Dict[str, Any] = {"mtime": None, "data": {}}
_timetable_lock = threading.Lock()


def load_timetable() -> Dict[str, List[Dict[str, str]]]:
    """timetable.json 을 읽는다. 파일이 바뀌면(수정 시각 기준) 서버 재시작 없이 다시 읽는다."""
    try:
        mtime = TIMETABLE_PATH.stat().st_mtime
    except OSError:
        log.warning("%s 파일을 찾을 수 없습니다. 빈 시간표를 사용합니다.", TIMETABLE_PATH)
        return {}
    with _timetable_lock:
        if _timetable_state["mtime"] != mtime:
            try:
                with open(TIMETABLE_PATH, "r", encoding="utf-8") as f:
                    _timetable_state["data"] = json.load(f)
                _timetable_state["mtime"] = mtime
            except (OSError, ValueError):
                log.exception("timetable.json 을 읽지 못했습니다.")
                raise ApiError("시간표 파일을 읽지 못했어요.", 500, "timetable_unreadable")
        return _timetable_state["data"]


def timetable_overview(day: Optional[str]) -> Dict[str, Any]:
    now = now_kst()
    today = WEEKDAYS[now.weekday()]
    selected = today
    if day:
        day = day.strip()
        if len(day) == 1:
            day += "요일"  # '월' → '월요일'
        if day not in WEEKDAYS:
            raise ApiError("요일은 월요일~일요일 중에서 골라 주세요.", 422, "invalid_request")
        selected = day
    data = load_timetable()
    classes = [{"time": c.get("time", ""), "subject": c.get("subject", ""), "room": c.get("room", "")}
               for c in data.get(selected, []) or []]
    return {"day": selected, "today": today, "days": WEEKDAYS, "classes": classes}


# =============================================================================
# FastAPI 앱
# =============================================================================
app = FastAPI(
    title="인천 통학 대중교통 API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
api = APIRouter(prefix="/api", dependencies=[Depends(_limit_dependency(general_limiter))])


@app.exception_handler(ApiError)
async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}},
                        status_code=exc.status, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"error": {"code": "invalid_request", "message": "입력한 값이 올바르지 않아요. 내용을 확인해 주세요."}},
                        status_code=422)


@app.exception_handler(Exception)
async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    log.error("처리되지 않은 오류", exc_info=exc)
    return JSONResponse({"error": {"code": "internal_error", "message": "서버 내부 오류가 발생했어요."}}, status_code=500)


_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers.setdefault("Cache-Control", "no-cache")  # 배포 후 Cloudflare/브라우저가 옛 파일을 붙들지 않게
    if not path.startswith("/api/docs"):  # Swagger UI 는 외부 CDN 스크립트를 쓰므로 CSP 제외
        response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """서버 상태 확인 (제한 없음, 외부 API 호출 없음). 키는 '설정 여부'만 알려준다."""
    return {"status": "ok", "time": _iso(), "timezone": "Asia/Seoul",
            "tago_key_configured": bool(TAGO_API_KEY), "odsay_key_configured": bool(ODSAY_API_KEY),
            "kakao_key_configured": bool(KAKAO_REST_API_KEY)}


@api.get("/timetable")
def api_timetable(day: Optional[str] = Query(None, max_length=8, description="월요일~일요일 (생략하면 오늘)")):
    return timetable_overview(day)


@api.get("/places/search", dependencies=[Depends(_limit_dependency(places_limiter))])
def api_places_search(q: str = Query(..., min_length=1, max_length=40, description="검색어 (자동완성용)")):
    """출발/도착 검색창의 자동완성용. 정류장/역 + (카카오 키가 있으면) 일반 장소를 함께 찾는다."""
    q = q.strip()
    if not q:
        raise ApiError("검색어를 입력해 주세요.", 422, "invalid_request")
    return {"query": q, "results": search_places(q, limit=8)}


@api.get("/subway")
def api_subway(
    line: Optional[str] = Query(None, max_length=20, description="호선 이름 (예: 인천2호선)"),
    station: Optional[str] = Query(None, max_length=30, description="역 이름 (예: 검단사거리역)"),
):
    """line/station 을 모두 생략하면 인천2호선 마전역↔주안역 기본 구간."""
    line = (line or "").strip() or None
    station = (station or "").strip() or None
    if not line and not station:
        return subway_default_overview()
    if not station:
        raise ApiError("역 이름도 함께 입력해 주세요. (예: 인천2호선 검단사거리역)", 422, "invalid_request")
    return subway_station_overview(line, station)


@api.get("/bus")
def api_bus():
    """511번 양방향 정류장 실시간 도착."""
    return bus_overview()


@api.get("/bus/locations")
def api_bus_locations():
    """511번 전체 버스의 실시간 위치."""
    return bus_locations_overview()


@api.get("/route", dependencies=[Depends(_limit_dependency(route_limiter))])
def api_route(
    goal: Optional[str] = Query(None, max_length=60, description="도착지 이름 (좌표를 주면 표시용 이름)"),
    start: Optional[str] = Query(None, max_length=40, description="출발 정류장/역 이름 (좌표를 주면 표시용 이름)"),
    start_x: Optional[float] = Query(None, description="출발지 경도 (선택, 현재 위치용)"),
    start_y: Optional[float] = Query(None, description="출발지 위도 (선택, 현재 위치용)"),
    goal_x: Optional[float] = Query(None, description="도착지 경도 (선택, /api/places/search 결과 사용 시)"),
    goal_y: Optional[float] = Query(None, description="도착지 위도 (선택, /api/places/search 결과 사용 시)"),
):
    """ODsay 경로 + 구간별 실시간. 실시간 조회가 실패해도 경로 요약은 항상 내려간다."""
    goal = (goal or "").strip() or None
    start = (start or "").strip() or None
    if (start_x is None) != (start_y is None):
        raise ApiError("출발 좌표는 start_x(경도)와 start_y(위도)를 함께 보내야 해요.", 422, "invalid_request")
    if (goal_x is None) != (goal_y is None):
        raise ApiError("도착 좌표는 goal_x(경도)와 goal_y(위도)를 함께 보내야 해요.", 422, "invalid_request")
    for label, x, y in (("출발", start_x, start_y), ("도착", goal_x, goal_y)):
        if x is not None and y is not None and not (124.0 <= x <= 132.0 and 33.0 <= y <= 39.0):
            raise ApiError("{} 좌표가 대한민국 범위를 벗어났어요.".format(label), 422, "invalid_request")
    if start_x is None and not start:
        raise ApiError("출발지를 입력해 주세요.", 422, "invalid_request")
    if goal_x is None and not goal:
        raise ApiError("도착지를 입력해 주세요.", 422, "invalid_request")
    return route_overview(start, goal, start_x, start_y, goal_x, goal_y, goal_name=goal)


@api.get("/geocode/reverse", dependencies=[Depends(_limit_dependency(geocode_limiter))])
def api_geocode_reverse(
    x: float = Query(..., description="경도 (현재 위치)"),
    y: float = Query(..., description="위도 (현재 위치)"),
):
    """좌표 → 주소. '현재 위치' 버튼을 눌렀을 때 실제로 어디로 인식됐는지 보여주기 위한 용도."""
    if not (124.0 <= x <= 132.0 and 33.0 <= y <= 39.0):
        raise ApiError("좌표가 대한민국 범위를 벗어났어요. (x=경도, y=위도)", 422, "invalid_request")
    return {"address": reverse_geocode(x, y)}


app.include_router(api)
# 정적 파일은 맨 마지막에 마운트한다 (/api/* 를 먼저 매칭시키기 위해). "/" → static/index.html
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":  # `python main.py` 로 바로 실행 (개발용, 127.0.0.1 에만 바인딩)
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=int(_env("PORT", "8000")), reload=False)