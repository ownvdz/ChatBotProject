import os
import json
import time
import asyncio
import datetime
import requests
import xml.etree.ElementTree as ET
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# .env 파일에서 토큰 가져오기
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
# 버스/지하철 API 모두 국토교통부(TAGO) 서비스라 이름을 통일함.
# 예전 .env에 PUBLIC_BUS_API_KEY로 남아있어도 당장 동작하도록 폴백 유지.
TAGO_API_KEY = os.getenv("PUBLIC_TAGO_API_KEY") or os.getenv("PUBLIC_BUS_API_KEY")
ODSAY_API_KEY = os.getenv("ODSAY_API_KEY")
# 개발 중인 디스코드 서버(길드) ID. 있으면 그 서버에만 슬래시 명령어를 즉시 동기화한다.
# 전역(글로벌) 동기화는 디스코드 전체에 반영되기까지 최대 1시간이 걸려서,
# "새로 추가한 명령어가 안 보인다"는 문제의 흔한 원인이 된다.
_guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
GUILD_ID = int(_guild_id_raw) if _guild_id_raw.isdigit() else None

WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# =============================================================================
# 📘 국토교통부(TAGO) 버스 관련 4개 OpenAPI 공식 명세 요약
# -----------------------------------------------------------------------------
# ⚠️ 인천시 자체 API가 계속 HTTP_ERROR를 내서 전국 단위 TAGO API로 완전히 교체함.
#    성공 코드는 "0"이 아니라 "00", 루트 태그는 <response>. (인천시 API와 다름)
#
# 서비스 호스트: https://apis.data.go.kr/1613000
#
#  1) ArvlInfoInqireService (버스도착정보조회)
#     - getSttnAcctoSpcifyRouteBusArvlPrearngeInfoList: 정류소(nodeId) + 노선(routeId) 지정 →
#       실제 도착예정 "초"(arrtime), 남은 정류장 수(arrprevstationcnt) 제공. ⭐ 가장 정확한 도착정보
#     - 필수 파라미터: cityCode, nodeId, routeId
#
#  2) BusSttnInfoInqireService (버스정류소정보조회)
#     - getSttnNoList: cityCode + nodeNm(정류소명, 옵션)으로 정류소 검색 → nodeid 조회 가능
#
#  3) BusRouteInfoInqireService (버스노선정보조회)
#     - getRouteAcctoThrghSttnList: cityCode + routeId → 노선의 전체 경유 정류소를
#       순서(nodeord)와 상하행구분(updowncd: 0=상행, 1=하행)까지 포함해서 준다.
#       ⭐ 511번 두 방향(주안역↔정석항공과학고)의 정확한 정류소ID를 여기서 확보한다.
#
#  4) BusLcInfoInqireService (버스위치정보조회)
#     - getRouteAcctoBusLcList: 노선의 모든 버스 실시간 GPS 위치 + 최근 통과 정류소
#
# 공통 에러(게이트웨이 레벨)는 여전히 <OpenAPI_ServiceResponse> 포맷으로 옴.
# =============================================================================

TAGO_HOST = "https://apis.data.go.kr/1613000"

DEFAULT_CITY_CODE = os.getenv("BUS_CITY_CODE", "23")
DEFAULT_ROUTE_ID = os.getenv("BUS_ROUTE_ID", "ICB365000073")

# 실시간 도착정보를 조회할 목표 정류장 (양방향 종점)
TARGET_STATION_NAMES = ["주안역환승정류장", "정석항공과학고"]

# 같은 이름의 정류장이 물리적으로 여러 곳(nodeord)에 있을 때, 필요 없는 쪽은 여기 적어서 제외한다.
# 정석항공과학고는 26번(방향1 도착쪽)은 필요 없고 8번(방향2 출발쪽)만 보여주면 됨.
EXCLUDED_NODEORDS = {
    "정석항공과학고": {"26"},
}

ERROR_MESSAGES = {
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

REQUEST_HEADERS = {
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# 노선 경유 정류소 목록(BusRouteInfoInqireService)은 잘 안 바뀌므로 캐싱해서 재사용한다.
_route_stops_cache = None


def load_timetable():
    json_path = "timetable.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        print(f"경고: {json_path} 파일을 찾을 수 없습니다. 빈 데이터를 반환합니다.")
        return {}


# =============================================================================
# 📘 국토교통부(TAGO) 지하철정보 서비스 (SubwayInfo) 공식 명세 요약
# -----------------------------------------------------------------------------
# ⚠️ 버스 API들과 달리 이건 실시간 도착정보가 아니라 "고정 시간표"(주1회 갱신)다.
#    그래서 "몇 분 후 도착"이 아니라, 시간표에서 지금 이후의 다음 출발시각을
#    찾아 현재시각과 비교해 "약 n분 후 출발"로 계산해서 보여준다.
#
# 서비스 호스트: https://apis.data.go.kr/1613000/SubwayInfo
#  1) GetKwrdFndSubwaySttnList(subwayStationName) → subwayStationId 검색
#  2) GetSubwaySttnAcctoSchdulList(subwayStationId, dailyTypeCode, upDownTypeCode)
#     → 그 역의 그 요일구분/방향의 전체 고정 시간표(depTime, endSubwayStationNm 등)
#     dailyTypeCode: 01=평일, 02=토요일, 03=일요일 / upDownTypeCode: U=상행, D=하행
#     ⚠️ U/D 중 어느 쪽이 "운연 방면"인지는 문서에 안 나와 있어서, 두 방향을
#        직접 조회해보고 응답의 endSubwayStationNm(종점역명)이 목표 방면과
#        일치하는 쪽을 골라 쓴다.
# =============================================================================

# 역 이름/노선ID 검색 결과 캐시 (역 목록은 자주 안 바뀌므로), (station_name, line_keyword) 기준
_subway_station_cache = {}


def _normalize(text: str) -> str:
    return text.replace(" ", "")


def get_subway_station_matches(station_name: str, line_keyword: str = None):
    """키워드기반 지하철역 목록 조회. line_keyword가 있으면 노선명이 일치하는 항목만
    우선 필터링해서 반환 (예: line_keyword='인천2호선' → subwayRouteName에 '인천'과
    '2호선'이 다 들어있는 항목).
    ⚠️ TAGO 역명 DB가 '역' 글자가 있을 수도, 없을 수도 있어서('마전' vs '마전역')
    입력값 그대로 → '역' 붙인 버전 → '역' 뗀 버전 순으로 결과가 나올 때까지 시도한다."""
    cache_key = (station_name, line_keyword)
    if cache_key in _subway_station_cache:
        return True, _subway_station_cache[cache_key]

    def _search(keyword):
        url = f"{TAGO_HOST}/SubwayInfo/GetKwrdFndSubwaySttnList"
        params = {
            "serviceKey": TAGO_API_KEY,
            "subwayStationName": keyword,
            "numOfRows": "20",
            "pageNo": "1",
            "_type": "xml",
        }
        return _fetch_tago_xml(url, params)

    candidates = [station_name]
    if station_name.endswith("역"):
        candidates.append(station_name[:-1])
    else:
        candidates.append(station_name + "역")

    items = []
    for keyword in candidates:
        ok, items = _search(keyword)
        if not ok:
            return False, items
        if items:
            break

    result = items
    if line_keyword:
        norm_keyword = _normalize(line_keyword)
        line_matches = [
            i for i in items
            if norm_keyword in _normalize(i.get("subwayRouteName", ""))
        ]
        result = line_matches or items  # 노선 필터링 결과가 없으면 전체 결과라도 반환

    _subway_station_cache[cache_key] = result
    return True, result


def get_subway_schedule(station_id: str, daily_type: str, up_down: str):
    """지하철역별 시간표 목록조회."""
    url = f"{TAGO_HOST}/SubwayInfo/GetSubwaySttnAcctoSchdulList"
    params = {
        "serviceKey": TAGO_API_KEY,
        "subwayStationId": station_id,
        "dailyTypeCode": daily_type,
        "upDownTypeCode": up_down,
        "numOfRows": "300",
        "pageNo": "1",
        "_type": "xml",
    }
    return _fetch_tago_xml(url, params)


def get_all_direction_schedules(station_id: str, daily_type: str):
    """해당 역의 U/D 시간표를 모두 가져와 하나로 합친다 (방향 구분은 나중에 각 열차의
    실제 종점역명으로 직접 판단한다 — upDownTypeCode만으로는 깔끔하게 안 갈리는 걸
    마전역에서 확인했기 때문).
    반환값: (성공여부, 결과 또는 에러메시지, 평일 데이터로 대체됐는지 여부)
    ⚠️ 일부 역은 주말(토/일) 운행정보가 TAGO에 아직 등록 안 되어 totalCount=0으로
    돌아오는 경우가 실제로 있다 (검단사거리역에서 확인됨). API 에러가 아니라 진짜
    데이터 공백이라, 이럴 땐 평일 시간표를 참고용으로라도 보여준다."""

    def _fetch(dt):
        all_items = []
        any_ok = False
        last_error = "조회 실패"
        for up_down in ("U", "D"):
            ok, items = get_subway_schedule(station_id, dt, up_down)
            if ok:
                any_ok = True
                all_items.extend(items)
            else:
                last_error = items
        if not any_ok:
            return False, last_error
        if not all_items:
            return False, "운행정보가 없습니다."
        return True, all_items

    ok, result = _fetch(daily_type)
    used_fallback = False

    if not ok and daily_type != "01":
        # 주말 데이터가 비어있는 경우, 평일 데이터라도 참고용으로 보여준다.
        ok, result = _fetch("01")
        used_fallback = ok

    if not ok:
        return False, result, False

    # 중복 제거 (같은 열차가 U/D 양쪽에 다 걸려서 들어올 가능성 대비)
    seen = set()
    deduped = []
    for it in result:
        key = (it.get("depTime", ""), it.get("endSubwayStationNm", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    return True, deduped, used_fallback


def group_by_destination(items: list, top_n: int = 2):
    """열차들을 각자의 실제 종점역명(endSubwayStationNm)으로 묶어서,
    운행 편수가 많은 순서로 top_n개 방향만 반환한다.
    (역 대부분에서 '진짜 두 종점 방향'이 편수가 가장 많고, 중간에 회차하는
    단거리 열차는 편수가 적어서 자연스럽게 뒤로 밀린다.)"""
    groups = {}
    for it in items:
        end_name = it.get("endSubwayStationNm", "") or "(종점 미상)"
        groups.setdefault(end_name, []).append(it)
    return sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]


def get_today_daily_type_code():
    weekday = datetime.datetime.now().weekday()  # 0=월 ... 6=일
    if weekday == 5:
        return "02"  # 토요일
    if weekday == 6:
        return "03"  # 일요일
    return "01"  # 평일


def _parse_tago_response(xml_bytes):
    """TAGO 공통 응답 파싱: <response><header><resultCode> 구조, 성공코드는 '00'.
    item의 모든 하위 태그를 그대로 dict로 만들어 반환 (API마다 필드가 달라 범용으로 처리)."""
    root = ET.fromstring(xml_bytes)

    if root.tag == "OpenAPI_ServiceResponse":
        err_msg = root.findtext(".//errMsg", "SERVICE ERROR")
        auth_msg = root.findtext(".//returnAuthMsg", "")
        reason_code = root.findtext(".//returnReasonCode", "")
        friendly = ERROR_MESSAGES.get(reason_code, auth_msg or err_msg)
        return False, f"{err_msg} - {friendly}"

    result_code = root.findtext(".//resultCode")
    if result_code is None:
        return False, "API 응답 형식이 올바르지 않습니다. (resultCode 없음)"
    if result_code != "00":
        result_msg = root.findtext(".//resultMsg", "")
        if "없습니다" in result_msg or "NODATA" in result_msg.upper():
            return True, []
        friendly = ERROR_MESSAGES.get(result_code.lstrip("0") or "0", f"알 수 없는 에러코드({result_code})")
        return False, f"{friendly}" + (f" - {result_msg}" if result_msg else "")

    items = [{child.tag: (child.text or "") for child in item} for item in root.findall(".//item")]
    return True, items


def _fetch_tago_xml(url, params, max_retries=2, backoff_sec=1.0):
    """요청 + 파싱 + 일시적 오류 재시도를 한 번에 처리."""
    last_error = "알 수 없는 오류"
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=5)
        except requests.exceptions.RequestException as e:
            print(f"❌ 네트워크 오류 (시도 {attempt + 1}/{max_retries + 1}): {e}")
            last_error = "버스 API 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요."
            time.sleep(backoff_sec)
            continue

        if response.status_code != 200:
            print(f"❌ HTTP 오류 {response.status_code} (시도 {attempt + 1}/{max_retries + 1})")
            last_error = f"HTTP {response.status_code} 오류가 발생했습니다."
            time.sleep(backoff_sec)
            continue

        try:
            ok, result = _parse_tago_response(response.content)
        except ET.ParseError:
            print(f"❌ XML 파싱 실패. 응답 원문: {response.text[:300]}")
            last_error = "API 응답을 해석할 수 없습니다. 서비스키를 확인해주세요."
            time.sleep(backoff_sec)
            continue

        if ok:
            return True, result

        if any(hint in result for hint in TRANSIENT_ERROR_HINTS) and attempt < max_retries:
            print(f"⚠️ 일시적 오류로 판단, {backoff_sec}초 후 재시도 ({attempt + 1}/{max_retries}): {result}")
            last_error = result
            time.sleep(backoff_sec)
            continue

        return False, result

    return False, last_error


# --- 1) 버스도착정보조회: 정류소별 특정노선버스 도착예정정보 ---
def get_arrival_info(city_code: str, node_id: str, route_id: str):
    url = f"{TAGO_HOST}/ArvlInfoInqireService/getSttnAcctoSpcifyRouteBusArvlPrearngeInfoList"
    params = {
        "serviceKey": TAGO_API_KEY,
        "cityCode": city_code,
        "nodeId": node_id,
        "routeId": route_id,
        "_type": "xml",
    }
    return _fetch_tago_xml(url, params)


# --- 3) 버스노선정보조회: 노선별 경유 정류소 목록 (양방향, 순서 포함) ---
def get_route_stops(city_code: str, route_id: str, num_of_rows: int = 100):
    url = f"{TAGO_HOST}/BusRouteInfoInqireService/getRouteAcctoThrghSttnList"
    params = {
        "serviceKey": TAGO_API_KEY,
        "cityCode": city_code,
        "routeId": route_id,
        "numOfRows": str(num_of_rows),
        "pageNo": "1",
        "_type": "xml",
    }
    return _fetch_tago_xml(url, params)


def get_cached_route_stops(city_code: str, route_id: str, force_refresh: bool = False):
    """get_route_stops 결과를 캐싱해서 재사용."""
    global _route_stops_cache
    if _route_stops_cache is not None and not force_refresh:
        return True, _route_stops_cache
    ok, result = get_route_stops(city_code, route_id)
    if ok:
        _route_stops_cache = result
    return ok, result


def find_node_ids_by_name(stops: list, target_name: str):
    """정류소 목록에서 이름이 일치하는 모든 nodeid를 반환.
    ⚠️ 같은 이름이 양방향에 각각 다른 nodeid로 존재할 수 있어 전부 반환하고,
    호출부에서 각각 도착정보를 조회해 합친다."""
    return [s.get("nodeid", "") for s in stops if s.get("nodenm", "").strip() == target_name.strip() and s.get("nodeid")]


def find_stop_record(stops: list, node_id: str):
    """nodeid로 해당 정류소의 nodeord/updowncd 레코드를 찾는다."""
    for s in stops:
        if s.get("nodeid") == node_id:
            return s
    return None


def build_name_to_nodeords(stops: list):
    """정류소명 → nodeord 목록 매핑을 만든다. (전체 노선 기준, 방향 구분 없이)
    ⚠️ nodeord는 알고 보니 방향마다 1부터 다시 시작하는 게 아니라 노선 전체를
    관통하는 연속된 번호였고(예: 8→17→26), 같은 이름의 정류장이 노선 안에
    여러 번(양방향 각각) 등장하기 때문에 이름 하나에 순번 하나만 저장하면
    나중 값이 앞 값을 덮어써서 틀린 매칭이 나온다. 그래서 리스트로 전부 보관한다."""
    mapping = {}
    for s in stops:
        name = s.get("nodenm", "").strip()
        try:
            ordv = int(s.get("nodeord", ""))
        except (TypeError, ValueError):
            continue
        mapping.setdefault(name, []).append(ordv)
    return mapping


def estimate_vehicle_plate(target_nodeord: int, arr_prev_cnt: str, location_items: list, name_to_nodeords: dict):
    """도착정보의 '남은 정류장 수'와 위치정보의 '현재 정류장 순서'를 비교해서
    가장 그럴듯한 버스의 차량번호를 추정한다. 정확히 일치하는 버스가 없으면 None.
    같은 이름의 정류장이 여러 nodeord로 존재할 수 있어 후보를 전부 검사한다."""
    try:
        arr_prev_cnt_int = int(arr_prev_cnt)
    except (TypeError, ValueError):
        return None

    best_plate = None
    best_diff = None
    for loc in location_items:
        candidate_ords = name_to_nodeords.get(loc.get("nodenm", "").strip(), [])
        for cur_ord in candidate_ords:
            remaining = target_nodeord - cur_ord
            if remaining < 0:
                continue  # 이미 목표 정류장을 지나간 경우는 후보에서 제외
            diff = abs(remaining - arr_prev_cnt_int)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_plate = loc.get("vehicleno")

    # 남은 정류장 수 차이가 1 이하일 때만 신뢰할 만한 매칭으로 인정
    if best_plate and best_diff is not None and best_diff <= 1:
        return best_plate
    return None


# --- 4) 버스위치정보조회: 노선 전체 버스 실시간 GPS 위치 ---
def get_route_bus_locations(city_code: str, route_id: str, num_of_rows: int = 100):
    url = f"{TAGO_HOST}/BusLcInfoInqireService/getRouteAcctoBusLcList"
    params = {
        "serviceKey": TAGO_API_KEY,
        "cityCode": city_code,
        "routeId": route_id,
        "numOfRows": str(num_of_rows),
        "pageNo": "1",
        "_type": "xml",
    }
    return _fetch_tago_xml(url, params)



# =============================================================================
# 🗺️ ODsay 대중교통 길찾기 API 연동
# -----------------------------------------------------------------------------
# ODsay는 "어느 버스/지하철을 타고 어디서 환승하는지" 경로 탐색은 되지만,
# 실시간 "몇 분 후 도착" 정보는 자체 제공하지 않는 지역이 많다. ODsay 공식
# 가이드(실시간 도착정보 연동 가이드)에서도 인천을 포함한 "그 외 지역"은
# 국토교통부 TAGO 공공API를 조합해서 쓰라고 명시하고 있다.
#
# 그래서 구조는:
#   1) ODsay로 경로 탐색 (어느 구간에서 어느 버스/지하철을 타는지)
#   2) ODsay의 localStationID로 그 정류장/역의 TAGO nodeId를 얻음
#      (인천은 localStationID가 TAGO와 같은 "ICB..." 형식이라 그대로 쓸 수 있음)
#   3) 그 nodeId + routeId로 위에서 이미 만든 TAGO 실시간 함수를 그대로 재사용
#
# 출력 포맷은 XML 대신 json으로 받는다 (파싱이 더 간단해서 버스/지하철 TAGO
# 코드와는 다르게 처리한다).
# =============================================================================

ODSAY_HOST = "https://api.odsay.com/v1/api"


def _parse_odsay_error(err):
    """ODsay의 error 필드는 dict({"code","msg"}) 또는 list([{"code","message"}]) 두 형태로 온다.
    (인증 실패 등 게이트웨이 계열 에러는 list + 'message' 키로 오는 경우가 있음)
    (코드, 메시지)로 통일해서 반환."""
    if isinstance(err, list):
        err = err[0] if err else {}
    if not isinstance(err, dict):
        return "", str(err)
    code = str(err.get("code", ""))
    msg = err.get("msg") or err.get("message") or "알 수 없는 에러"
    return code, msg


def _fetch_odsay_json(endpoint: str, params: dict, max_retries: int = 2, backoff_sec: float = 1.0):
    """ODsay API 공통 호출. (성공여부, 결과딕셔너리 또는 에러메시지) 반환."""
    url = f"{ODSAY_HOST}/{endpoint}"
    params = {**params, "apiKey": ODSAY_API_KEY, "output": "json"}

    last_error = "알 수 없는 오류"
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=5)
        except requests.exceptions.RequestException as e:
            last_error = f"네트워크 오류: {e}"
            time.sleep(backoff_sec)
            continue

        if response.status_code != 200:
            last_error = f"HTTP {response.status_code} 오류"
            time.sleep(backoff_sec)
            continue

        try:
            data = response.json()
        except ValueError:
            last_error = f"응답을 JSON으로 해석할 수 없습니다: {response.text[:200]}"
            time.sleep(backoff_sec)
            continue

        if "error" in data:
            code, msg = _parse_odsay_error(data["error"])
            print(f"[ODsay ERROR] endpoint={endpoint} code={code} msg={msg} raw={data['error']}")
            # 인증 실패는 재시도해도 소용없으므로 즉시 반환 + 원인 힌트 제공
            if "ApiKey" in msg:
                return False, (
                    f"{msg} (코드 {code}) — ODsay 키 인증 실패입니다. "
                    "ODsay 콘솔에서 키에 등록한 플랫폼(Server IP / Web URI)이 "
                    "현재 봇이 실행되는 환경과 일치하는지 확인해주세요."
                )
            # 500(서버오류)만 재시도, 나머지(-8,-9,3,4,5,6,-98,-99 등)는 즉시 반환
            if code == "500" and attempt < max_retries:
                last_error = f"{msg} (코드 {code})"
                time.sleep(backoff_sec)
                continue
            return False, f"{msg} (코드 {code})"

        return True, data.get("result", {})

    return False, last_error


def odsay_search_station(name: str, station_class: str = None):
    """대중교통 정류장 검색. station_class: '1'=버스, '2'=지하철 (생략시 둘다)."""
    params = {"stationName": name}
    if station_class:
        params["stationClass"] = station_class
    ok, result = _fetch_odsay_json("searchStation", params)
    if not ok:
        return False, result
    return True, result.get("station", [])


def odsay_search_path(sx: float, sy: float, ex: float, ey: float):
    """대중교통 길찾기. 좌표(경도,위도) 기준."""
    params = {"SX": sx, "SY": sy, "EX": ex, "EY": ey}
    ok, result = _fetch_odsay_json("searchPubTransPathT", params)
    if not ok:
        return False, result
    paths = result.get("path", [])
    if not paths:
        return False, "검색된 경로가 없습니다."
    return True, paths


def odsay_search_bus_lane(bus_no: str, cid: str = None):
    """버스노선 조회 (busNo → localBusID 등 확인용)."""
    params = {"busNo": bus_no}
    if cid:
        params["CID"] = cid
    ok, result = _fetch_odsay_json("searchBusLane", params)
    if not ok:
        return False, result
    return True, result.get("lane", [])


def resolve_station_coords(name: str):
    """이름으로 정류장/역 좌표를 찾는다. (성공여부, {x,y,stationClass,...} 또는 에러메시지)"""
    ok, stations = odsay_search_station(name)
    if not ok:
        return False, stations
    if not stations:
        return False, f"'{name}' 정류장/역을 찾을 수 없습니다."
    return True, stations[0]


TRAFFIC_TYPE_NAMES = {1: "지하철", 2: "버스", 3: "도보"}


def summarize_subpath(sub_path: dict) -> str:
    """길찾기 결과 한 구간을 사람이 읽을 수 있는 한 줄로 요약."""
    traffic_type = sub_path.get("trafficType")
    type_name = TRAFFIC_TYPE_NAMES.get(traffic_type, "이동")

    if traffic_type == 3:  # 도보
        distance = sub_path.get("distance", 0)
        return f"🚶 도보 {distance}m (약 {sub_path.get('sectionTime', '?')}분)"

    lane = sub_path.get("lane", {})
    if isinstance(lane, list):
        lane = lane[0] if lane else {}
    lane_name = lane.get("busNo") or lane.get("name") or "?"
    start_name = sub_path.get("startName", "?")
    end_name = sub_path.get("endName", "?")
    station_count = sub_path.get("stationCount", "?")
    section_time = sub_path.get("sectionTime", "?")

    icon = "🚇" if traffic_type == 1 else "🚌"
    return (
        f"{icon} [{lane_name}] {start_name} → {end_name} "
        f"({station_count}개 정류장, 약 {section_time}분)"
    )



def _strip_station_suffix(name: str) -> str:
    """'마전역' → '마전' 식으로 공백과 '역' 접미사를 제거해 비교용 문자열로 만든다."""
    name = _normalize(name or "")
    return name[:-1] if name.endswith("역") else name


def get_subway_realtime_note(board_name: str, line_name: str, way: str, daily_type: str):
    """/길찾기의 지하철 구간용: 탑승역/노선/방면으로 TAGO 시간표에서 '다음 열차' 문구를 만든다.
    방향은 하차역(endName)이 아니라 ODsay의 way(방면)와 열차의 실제 종점역명을 비교해서
    판단한다. 찾지 못하면 None을 반환 (그 구간은 경로 요약만 표시됨)."""
    if not board_name or not way:
        return None
    ok, matches = get_subway_station_matches(board_name, line_name or None)
    if not ok or not matches:
        return None

    way_key = _strip_station_suffix(way)
    now = datetime.datetime.now()
    now_hhmmss = now.strftime("%H%M%S")
    now_seconds = now.hour * 3600 + now.minute * 60 + now.second

    # /지하철과 동일하게, 같은 이름의 후보가 여러 개일 수 있어 운행정보가 있는 첫 후보를 쓴다.
    for cand in matches[:5]:
        cand_id = cand.get("subwayStationId", "")
        if not cand_id:
            continue
        ok_sch, sched, used_fallback = get_all_direction_schedules(cand_id, daily_type)
        if not (ok_sch and sched):
            continue

        for g_name, g_items in group_by_destination(sched, top_n=2):
            g_key = _strip_station_suffix(g_name)
            if not g_key or not (way_key in g_key or g_key in way_key):
                continue

            upcoming = sorted(
                (it for it in g_items if it.get("depTime", "").isdigit() and it["depTime"] >= now_hhmmss),
                key=lambda it: it["depTime"],
            )
            if not upcoming:
                return f"⏱️ 오늘 남은 열차가 없습니다 ({g_name}행)"

            dep = upcoming[0]["depTime"]
            dep_seconds = int(dep[0:2]) * 3600 + int(dep[2:4]) * 60 + int(dep[4:6])
            remain_min = max((dep_seconds - now_seconds) // 60, 0)
            note = f"⏱️ 시간표상 다음 열차: {dep[0:2]}:{dep[2:4]} 출발 (약 {remain_min}분 후, {g_name}행)"
            if used_fallback:
                note += " · 오늘 운행정보가 없어 평일 시간표 기준"
            return note
    return None


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"길드({GUILD_ID}) 전용 슬래시 명령어 동기화 성공 (즉시 반영)")
        else:
            await self.tree.sync()
            print("전역 슬래시 명령어 동기화 성공 (디스코드 서버 전체 반영까지 최대 1시간 걸릴 수 있음)")

    async def on_ready(self):
        print(f'로그인 성공: {self.user.name} (ID: {self.user.id})')
        print('정상작동')


bot = MyBot()

# -------------------------------------------------------------------------------------------
@bot.tree.command(name="시간표", description="특정 요일의 수업 시간표를 조회합니다.")
@app_commands.describe(day="조회할 요일을 선택하세요 (생략 시 오늘 요일 자동 선택)")
@app_commands.choices(day=[
    app_commands.Choice(name="월요일", value="월요일"),
    app_commands.Choice(name="화요일", value="화요일"),
    app_commands.Choice(name="수요일", value="수요일"),
    app_commands.Choice(name="목요일", value="목요일"),
    app_commands.Choice(name="금요일", value="금요일"),
    app_commands.Choice(name="토요일", value="토요일"),
    app_commands.Choice(name="일요일", value="일요일"),
])
async def show_timetable(interaction: discord.Interaction, day: app_commands.Choice[str] = None):
    timetable_data = load_timetable()

    if day is None:
        today_idx = datetime.datetime.now().weekday()
        selected_day = WEEKDAYS[today_idx]
        title_prefix = f"📅 오늘({selected_day})의 시간표"
    else:
        selected_day = day.value
        title_prefix = f"📅 {selected_day} 시간표"

    schedule = timetable_data.get(selected_day)

    embed = discord.Embed(
        title=title_prefix,
        color=discord.Color.blue()
    )

    if schedule:
        for item in schedule:
            embed.add_field(
                name=f"🕒 {item['time']} - {item['subject']}",
                value=f"📍 장소: {item['room']}",
                inline=False
            )
    else:
        embed.description = "해당 요일에는 등록된 수업이 없습니다."

    embed.set_footer(text="컴퓨터공학과 챗봇 프로젝트 | V1.0")

    await interaction.response.send_message(embed=embed)


# -------------------------------------------------------------------------------------------
@bot.tree.command(name="지하철", description="지정한 호선/역의 양방향 다음 열차 운행정보를 조회합니다.")
@app_commands.describe(line="호선 이름 (예: 인천2호선)", station="역 이름 (예: 검단사거리역)")
async def show_subway(interaction: discord.Interaction, line: str, station: str):
    await interaction.response.defer()

    if not TAGO_API_KEY:
        await interaction.followup.send("⚠️ PUBLIC_TAGO_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")
        return

    ok, matches = await asyncio.to_thread(get_subway_station_matches, station, line)
    if not ok:
        await interaction.followup.send(f"⚠️ 조회 실패: {matches}")
        return
    if not matches:
        await interaction.followup.send(f"⚠️ '{line} {station}' 역 정보를 찾지 못했습니다.")
        return

    station_id = matches[0].get("subwayStationId", "")
    daily_type = get_today_daily_type_code()

    # ⚠️ 검색 결과 중 첫 번째가 항상 정답이 아닐 수 있다(같은 이름의 중복/폐기된 항목이
    # 섞여 있으면 운행정보가 비어있는 엉뚱한 station_id를 고를 수 있음). 그래서 후보를
    # 순서대로 시도해서 실제로 운행정보가 있는 첫 번째 역을 쓴다.
    all_items = None
    used_fallback = False
    last_error = "운행정보가 없습니다."
    matched_station = matches[0]
    for candidate in matches[:5]:
        cand_id = candidate.get("subwayStationId", "")
        if not cand_id:
            continue
        ok, result, fb = await asyncio.to_thread(get_all_direction_schedules, cand_id, daily_type)
        if ok and result:
            all_items = result
            used_fallback = fb
            matched_station = candidate
            station_id = cand_id
            break
        if not ok:
            last_error = result

    if not all_items:
        tried = len(matches[:5])
        await interaction.followup.send(
            f"⚠️ 운행정보 조회 실패: {last_error} (후보 {tried}개 모두 확인함)"
        )
        return

    now = datetime.datetime.now()
    now_hhmmss = now.strftime("%H%M%S")
    now_seconds = now.hour * 3600 + now.minute * 60 + now.second

    # 종점역명별로 묶어서(편수 많은 순) 상위 2개 방향만 표시 — 대부분 이게 진짜 양방향 종점이다.
    direction_groups = group_by_destination(all_items, top_n=2)

    embed = discord.Embed(
        title=f"🚇 {matched_station.get('subwayStationName', station)} ({line}) 다음 열차",
        color=discord.Color.green(),
        timestamp=now,
    )

    if used_fallback:
        embed.description = "⚠️ 오늘(주말) 운행정보가 등록되어 있지 않아, **평일 기준 시간표**를 참고용으로 보여드려요. 실제 오늘 운행 여부와 다를 수 있습니다."

    for end_name, items in direction_groups:
        if used_fallback:
            # 평일 시간표를 참고용으로 보여줄 땐 '오늘 이후'라는 개념이 의미가 없어
            # 남은 시간 계산 없이 첫 열차부터 몇 개만 그대로 보여준다.
            sample = sorted(items, key=lambda it: it.get("depTime", ""))[:3]
            lines = [f"**{it['depTime'][0:2]}:{it['depTime'][2:4]} 출발** (평일 기준)" for it in sample]
            embed.add_field(name=f"{end_name}행", value="\n".join(lines) if lines else "정보 없음", inline=False)
            continue

        upcoming = [it for it in items if it.get("depTime", "").isdigit() and it["depTime"] >= now_hhmmss]
        upcoming.sort(key=lambda it: it["depTime"])

        if not upcoming:
            embed.add_field(name=f"{end_name}행", value="오늘 남은 열차가 없습니다.", inline=False)
            continue

        lines = []
        for it in upcoming[:3]:
            dep = it["depTime"]
            h, m = dep[0:2], dep[2:4]
            dep_seconds = int(dep[0:2]) * 3600 + int(dep[2:4]) * 60 + int(dep[4:6])
            remain_min = max((dep_seconds - now_seconds) // 60, 0)
            lines.append(f"**{h}:{m} 출발** (약 {remain_min}분 후)")

        embed.add_field(name=f"{end_name}행", value="\n".join(lines), inline=False)

    embed.set_footer(text="국토교통부(TAGO) 지하철정보 API 기반 · 실시간 도착정보가 아닌 고정 운행정보(주1회 갱신)입니다")
    await interaction.followup.send(embed=embed)


# --- 🚌 /버스 명령어: 511번 버스, 양방향 정류장 실시간 도착 "분" 정보 ---
@bot.tree.command(name="버스", description="511번 버스의 주안역/정석항공과학고 실시간 도착 예정 정보를 조회합니다.")
async def bus(interaction: discord.Interaction):
    await interaction.response.defer()

    if not TAGO_API_KEY:
        await interaction.followup.send("⚠️ PUBLIC_TAGO_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")
        return

    ok, stops = await asyncio.to_thread(get_cached_route_stops, DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID)
    if not ok:
        await interaction.followup.send(f"⚠️ 노선 정류소 목록 조회 실패: {stops}")
        return

    # 차량번호는 도착정보 API엔 없고 위치정보 API에만 있어서, 한 번만 받아와 매칭에 사용한다.
    loc_ok, location_items = await asyncio.to_thread(get_route_bus_locations, DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID)
    if not loc_ok:
        location_items = []  # 위치정보 실패해도 도착시간 정보는 계속 보여준다
    name_to_nodeords = build_name_to_nodeords(stops)

    embed = discord.Embed(
        title="🚌 511번 버스 실시간 도착 정보",
        color=0x3498db,
        timestamp=datetime.datetime.now(),
    )

    def _minutes(b):
        t = b.get("arrtime", "0")
        return int(t) // 60 if t.isdigit() else 0

    for station_name in TARGET_STATION_NAMES:
        node_ids = find_node_ids_by_name(stops, station_name)
        if not node_ids:
            embed.add_field(
                name=f"📍 {station_name}",
                value="⚠️ 노선 정류소 목록에서 이 정류장을 찾지 못했습니다.",
                inline=False,
            )
            continue

        # ⚠️ 같은 이름의 정류장이 물리적으로 두 곳(양방향)에 있을 수 있어
        # 절대 합치지 않고 nodeord별로 따로 필드를 만든다 (섞이면 헷갈림).
        excluded = EXCLUDED_NODEORDS.get(station_name, set())
        stop_records = sorted(
            (find_stop_record(stops, nid) for nid in node_ids),
            key=lambda s: int(s.get("nodeord", "0") or 0) if s else 0,
        )
        stop_records = [s for s in stop_records if s and s.get("nodeord") not in excluded]

        for stop_record in stop_records:
            if not stop_record:
                continue
            node_id = stop_record.get("nodeid", "")
            node_ord = stop_record.get("nodeord", "?")

            ok, arrivals = await asyncio.to_thread(get_arrival_info, DEFAULT_CITY_CODE, node_id, DEFAULT_ROUTE_ID)
            field_name = f"📍 {station_name}"

            if not ok:
                embed.add_field(name=field_name, value=f"⚠️ 조회 실패: {arrivals}", inline=False)
                continue
            if not arrivals:
                embed.add_field(name=field_name, value="현재 도착 예정인 511번 버스가 없습니다.", inline=False)
                continue

            arrivals.sort(key=_minutes)
            try:
                target_nodeord = int(node_ord)
            except (TypeError, ValueError):
                target_nodeord = None

            lines = []
            for b in arrivals[:3]:
                minutes = _minutes(b)
                time_str = f"**{minutes}분 후 도착**" if minutes > 0 else "**곧 도착**"
                prev_cnt = b.get("arrprevstationcnt", "?")

                plate = None
                if target_nodeord is not None and location_items:
                    plate = estimate_vehicle_plate(target_nodeord, prev_cnt, location_items, name_to_nodeords)
                plate_str = plate if plate else "차량번호 확인불가"

                lines.append(f"{time_str} · {prev_cnt}개 정류장 전 · {plate_str}")

            embed.add_field(name=field_name, value="\n".join(lines), inline=False)

    embed.set_footer(text="국토교통부(TAGO) 버스도착정보 API 기반 · 차량번호는 위치정보와 교차 매칭한 추정값입니다")
    await interaction.followup.send(embed=embed)


# --- 🚍 /버스위치 명령어: 실시간 GPS 위치 (보조 정보) ---
@bot.tree.command(name="버스위치", description="511번 버스 전체의 실시간 GPS 위치를 조회합니다.")
async def bus_location(interaction: discord.Interaction):
    await interaction.response.defer()

    ok, result = await asyncio.to_thread(get_route_bus_locations, DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID)
    if not ok:
        await interaction.followup.send(f"⚠️ 조회 실패: {result}")
        return
    if not result:
        await interaction.followup.send("현재 운행 중인 511번 버스가 없습니다.")
        return

    lines = [
        f"🚍 {b.get('vehicleno', '')} · 최근 통과: {b.get('nodenm', '')} (순서 {b.get('nodeord', '')})"
        for b in result[:10]
    ]
    embed = discord.Embed(
        title="🚍 511번 버스 실시간 위치",
        description="\n".join(lines),
        color=0x3498db,
    )
    await interaction.followup.send(embed=embed)


# --- 🗺️ /길찾기 명령어: ODsay 경로탐색 + TAGO 실시간 정보(버스 구간) ---
@bot.tree.command(name="길찾기", description="정류장/역 이름으로 대중교통 경로를 찾습니다 (버스 구간은 실시간 도착정보 포함).")
@app_commands.describe(start="출발 정류장/역 이름", goal="도착 정류장/역 이름")
async def find_route(interaction: discord.Interaction, start: str, goal: str):
    await interaction.response.defer()

    if not ODSAY_API_KEY:
        await interaction.followup.send("⚠️ ODSAY_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")
        return

    ok, start_station = await asyncio.to_thread(resolve_station_coords, start)
    if not ok:
        await interaction.followup.send(f"⚠️ 출발지 조회 실패: {start_station}")
        return
    ok, goal_station = await asyncio.to_thread(resolve_station_coords, goal)
    if not ok:
        await interaction.followup.send(f"⚠️ 도착지 조회 실패: {goal_station}")
        return

    ok, paths = await asyncio.to_thread(
        odsay_search_path, start_station["x"], start_station["y"], goal_station["x"], goal_station["y"]
    )
    if not ok:
        await interaction.followup.send(f"⚠️ 경로 탐색 실패: {paths}")
        return

    best_path = paths[0]
    sub_paths = best_path.get("subPath", [])
    info = best_path.get("info", {})

    embed = discord.Embed(
        title=f"🗺️ {start} → {goal}",
        description=(
            f"총 {info.get('totalTime', '?')}분 · {info.get('totalDistance', '?')}m"
            f" · 요금 {info.get('payment', '?')}원 · 환승 {info.get('busTransitCount', 0) + info.get('subwayTransitCount', 0)}회"
        ),
        color=discord.Color.teal(),
        timestamp=datetime.datetime.now(),
    )

    daily_type = get_today_daily_type_code()

    for i, sub_path in enumerate(sub_paths, start=1):
        summary = summarize_subpath(sub_path)
        traffic_type = sub_path.get("trafficType")

        realtime_note = None
        if traffic_type == 2:  # 버스 구간 → TAGO 실시간 시도
            lane = sub_path.get("lane", {})
            if isinstance(lane, list):
                lane = lane[0] if lane else {}
            bus_no = lane.get("busNo")
            board_name = sub_path.get("startName")

            if bus_no and board_name:
                ok_lane, lanes = await asyncio.to_thread(odsay_search_bus_lane, bus_no, None)
                ok_stop, stops = await asyncio.to_thread(odsay_search_station, board_name, "1")

                route_id = None
                if ok_lane and lanes:
                    route_id = lanes[0].get("localBusID")
                node_id = None
                if ok_stop and stops:
                    node_id = stops[0].get("localStationID")

                print(
                    f"[길찾기 버스매칭] bus_no={bus_no} board={board_name} route_id={route_id} "
                    f"node_id={node_id} (lane 후보 {len(lanes) if ok_lane else 0}개)"
                )
                if route_id and node_id:
                    ok_arr, arrivals = await asyncio.to_thread(
                        get_arrival_info, DEFAULT_CITY_CODE, node_id, route_id
                    )
                    print(f"[길찾기 버스매칭] TAGO 도착정보 ok={ok_arr} 결과={arrivals if not ok_arr else len(arrivals)}건")
                    if ok_arr and arrivals:
                        arrivals.sort(key=lambda b: int(b.get("arrtime", "0") or 0))
                        first = arrivals[0]
                        minutes = int(first.get("arrtime", "0") or 0) // 60
                        realtime_note = f"⏱️ 실시간: {minutes}분 후 도착 ({first.get('arrprevstationcnt', '?')}개 전)"

        elif traffic_type == 1:  # 지하철 구간 → TAGO 고정 시간표 기준 '다음 열차' (방면이 일치할 때만)
            lane = sub_path.get("lane", {})
            if isinstance(lane, list):
                lane = lane[0] if lane else {}
            realtime_note = await asyncio.to_thread(
                get_subway_realtime_note,
                sub_path.get("startName"),
                lane.get("name", ""),
                sub_path.get("way", ""),
                daily_type,
            )

        field_value = summary + (f"\n{realtime_note}" if realtime_note else "")
        embed.add_field(name=f"{i}단계", value=field_value, inline=False)

    embed.set_footer(text="경로 탐색: ODsay · 실시간 정보(가능한 구간): 국토교통부(TAGO)")
    await interaction.followup.send(embed=embed)

# -------------------------------------------------------------------------------------------
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("오류: .env 파일에서 DISCORD_TOKEN을 찾을 수 없습니다.")