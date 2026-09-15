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

INCHEON_LINE2_STATIONS = [
    ("마전역", "운연"),   # 마전역 → 주안역 (운연 방면)
    ("주안역", "검단오류"),  # 주안역 → 마전역 (검단오류 방면)
]

# 역 이름/노선ID 검색 결과 캐시 (역 목록은 자주 안 바뀌므로)
_subway_station_cache = {}


def get_subway_station_matches(station_name: str):
    """키워드기반 지하철역 목록 조회. 인천 2호선인 항목만 우선 필터링해서 반환.
    ⚠️ TAGO 역명 DB가 '역' 글자 없이 저장된 경우가 있어(예: '마전역' 대신 '마전'),
    원래 이름으로 못 찾으면 '역'을 뗀 이름으로 한 번 더 시도한다."""
    if station_name in _subway_station_cache:
        return True, _subway_station_cache[station_name]

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

    ok, items = _search(station_name)
    if not ok:
        return False, items

    if not items and station_name.endswith("역"):
        ok, items = _search(station_name[:-1])
        if not ok:
            return False, items

    line2_matches = [
        i for i in items
        if "인천" in i.get("subwayRouteName", "") and "2호선" in i.get("subwayRouteName", "")
    ]
    result = line2_matches or items  # 인천2호선 필터링 결과가 없으면 전체 결과라도 반환
    _subway_station_cache[station_name] = result
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


def get_schedule_towards(station_id: str, daily_type: str, dest_keyword: str):
    """해당 역의 U/D 시간표를 모두 가져온 뒤, 각 열차 자신의 종점역명
    (endSubwayStationNm)에 목표 방면 키워드가 실제로 들어있는 열차만 걸러서 반환한다.
    ⚠️ upDownTypeCode(U/D)로 미리 방향을 갈라서 통째로 보여줬더니 검단오류행/운연행이
    섞여 나왔다 — U/D 구분만으로는 방향이 깔끔하게 안 갈리는 것으로 보여, 대신
    열차 하나하나의 실제 종점을 직접 확인해서 필터링하는 방식으로 바꿨다."""
    all_items = []
    any_ok = False
    last_error = "조회 실패"
    for up_down in ("U", "D"):
        ok, items = get_subway_schedule(station_id, daily_type, up_down)
        if ok:
            any_ok = True
            all_items.extend(items)
        else:
            last_error = items

    if not any_ok:
        return False, last_error

    matched = [it for it in all_items if dest_keyword in it.get("endSubwayStationNm", "")]
    if not matched:
        return False, f"'{dest_keyword}' 방면으로 가는 열차를 찾지 못했습니다."

    # U/D를 둘 다 합쳤기 때문에 혹시 같은 열차가 중복으로 들어올 경우를 대비해 정리
    seen = set()
    deduped = []
    for it in matched:
        key = (it.get("depTime", ""), it.get("endSubwayStationNm", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    return True, deduped


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


# --- 2) 버스정류소정보조회: 정류소명으로 검색 ---
def search_stations_by_name(city_code: str, keyword: str, num_of_rows: int = 20):
    url = f"{TAGO_HOST}/BusSttnInfoInqireService/getSttnNoList"
    params = {
        "serviceKey": TAGO_API_KEY,
        "cityCode": city_code,
        "nodeNm": keyword,
        "numOfRows": str(num_of_rows),
        "pageNo": "1",
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


def get_city_code_list():
    url = f"{TAGO_HOST}/BusRouteInfoInqireService/getCtyCodeList"
    params = {"serviceKey": TAGO_API_KEY, "_type": "xml"}
    return _fetch_tago_xml(url, params)


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("슬래시 명령어 동기화 성공")

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
@bot.tree.command(name="지하철", description="인천2호선 마전역/주안역의 다음 열차 출발 시간표를 조회합니다.")
async def show_subway(interaction: discord.Interaction):
    await interaction.response.defer()

    if not TAGO_API_KEY:
        await interaction.followup.send("⚠️ PUBLIC_TAGO_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")
        return

    now = datetime.datetime.now()
    daily_type = get_today_daily_type_code()
    now_hhmmss = now.strftime("%H%M%S")
    now_seconds = now.hour * 3600 + now.minute * 60 + now.second

    embed = discord.Embed(
        title="🚇 인천2호선 다음 열차 시간표",
        color=discord.Color.green(),
        timestamp=now,
    )

    for station_name, dest_keyword in INCHEON_LINE2_STATIONS:
        field_name = f"[{station_name} → {dest_keyword} 방면]"

        ok, matches = await asyncio.to_thread(get_subway_station_matches, station_name)
        if not ok:
            embed.add_field(name=field_name, value=f"⚠️ 조회 실패: {matches}", inline=False)
            continue
        if not matches:
            embed.add_field(name=field_name, value="⚠️ 역 정보를 찾지 못했습니다. /지하철역검색으로 확인해주세요.", inline=False)
            continue

        station_id = matches[0].get("subwayStationId", "")
        ok, result = await asyncio.to_thread(get_schedule_towards, station_id, daily_type, dest_keyword)
        if not ok:
            embed.add_field(name=field_name, value=f"⚠️ {result}", inline=False)
            continue

        upcoming = [it for it in result if it.get("depTime", "").isdigit() and it["depTime"] >= now_hhmmss]
        upcoming.sort(key=lambda it: it["depTime"])

        if not upcoming:
            embed.add_field(name=field_name, value="오늘 남은 열차가 없습니다.", inline=False)
            continue

        lines = []
        for it in upcoming[:3]:
            dep = it["depTime"]
            h, m = dep[0:2], dep[2:4]
            dep_seconds = int(dep[0:2]) * 3600 + int(dep[2:4]) * 60 + int(dep[4:6])
            remain_min = max((dep_seconds - now_seconds) // 60, 0)
            end_name = it.get("endSubwayStationNm", "")
            lines.append(f"**{h}:{m} 출발** (약 {remain_min}분 후) · {end_name}행")

        embed.add_field(name=field_name, value="\n".join(lines), inline=False)

    embed.set_footer(text="국토교통부(TAGO) 지하철정보 API 기반 · 실시간 도착정보가 아닌 고정 시간표(주1회 갱신)입니다")
    await interaction.followup.send(embed=embed)


# --- 🔍 /지하철역검색 명령어: 이름으로 지하철역ID 검색 ---
@bot.tree.command(name="지하철역검색", description="[개발용] 지하철역 이름으로 subwayStationId를 검색합니다.")
@app_commands.describe(keyword="검색할 역 이름 (예: 마전역, 주안역)")
async def subway_station_search(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer()

    ok, result = await asyncio.to_thread(get_subway_station_matches, keyword)
    if not ok:
        await interaction.followup.send(f"⚠️ 조회 실패: {result}")
        return
    if not result:
        await interaction.followup.send(f"'{keyword}'로 검색된 역이 없습니다.")
        return

    lines = [
        f"{i.get('subwayStationName', '')} ({i.get('subwayRouteName', '')}) → id={i.get('subwayStationId', '')}"
        for i in result[:20]
    ]
    await interaction.followup.send("🔍 검색 결과:\n" + "\n".join(lines))


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
                value="⚠️ 노선 정류소 목록에서 이 정류장을 찾지 못했습니다. /버스디버그로 확인해주세요.",
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


# --- 🔧 /버스디버그 명령어: 511번 노선의 전체 경유 정류소(양방향) 원본 데이터 ---
@bot.tree.command(name="버스디버그", description="[개발용] 511번 노선의 전체 경유 정류소 목록(양방향)을 표시합니다.")
async def bus_debug(interaction: discord.Interaction):
    await interaction.response.defer()

    ok, stops = await asyncio.to_thread(get_cached_route_stops, DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID, force_refresh=True)
    if not ok:
        await interaction.followup.send(f"⚠️ 조회 실패: {stops}")
        return
    if not stops:
        await interaction.followup.send("경유 정류소 데이터가 없습니다.")
        return

    updown_map = {"0": "상행", "1": "하행"}
    sorted_stops = sorted(stops, key=lambda s: (s.get("updowncd", ""), int(s.get("nodeord", "0") or 0)))
    lines = []
    for s in sorted_stops[:40]:
        lines.append(
            f"[{updown_map.get(s.get('updowncd', ''), s.get('updowncd', '?'))}] "
            f"{s.get('nodeord', '?')}. {s.get('nodenm', '')} (nodeid={s.get('nodeid', '')})"
        )

    embed = discord.Embed(
        title=f"🔧 노선 {DEFAULT_ROUTE_ID} 경유 정류소 (도시코드 {DEFAULT_CITY_CODE})",
        description="```\n" + "\n".join(lines) + "\n```",
        color=0x95a5a6,
    )
    embed.set_footer(text="사용자가 제공한 정류장 순서와 비교해서 맞는지 확인해주세요.")
    await interaction.followup.send(embed=embed)


# --- 🔍 /정류소검색 명령어 ---
@bot.tree.command(name="정류소검색", description="[개발용] 정류소 이름으로 nodeId를 검색합니다 (TAGO 기준).")
@app_commands.describe(keyword="검색할 정류소 이름 (예: 주안역, 정석항공과학고)")
async def station_search(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer()

    ok, result = await asyncio.to_thread(search_stations_by_name, DEFAULT_CITY_CODE, keyword)
    if not ok:
        await interaction.followup.send(f"⚠️ 조회 실패: {result}")
        return
    if not result:
        await interaction.followup.send(f"'{keyword}'가 포함된 정류소를 찾지 못했습니다.")
        return

    lines = [f"{s.get('nodenm', '')} → nodeid={s.get('nodeid', '')}" for s in result[:20]]
    await interaction.followup.send("🔍 검색 결과:\n" + "\n".join(lines))


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


# --- 🌆 /도시코드확인 명령어 ---
@bot.tree.command(name="도시코드확인", description="[개발용] TAGO 도시코드 목록에서 인천 코드가 맞는지 확인합니다.")
async def city_code_check(interaction: discord.Interaction):
    await interaction.response.defer()

    ok, result = await asyncio.to_thread(get_city_code_list)
    if not ok:
        await interaction.followup.send(f"⚠️ 조회 실패: {result}")
        return

    lines = [f"{item.get('citycode', '')}: {item.get('cityname', '')}" for item in result]
    matched = [l for l in lines if l.startswith(f"{DEFAULT_CITY_CODE}:")]
    header = f"현재 설정된 도시코드({DEFAULT_CITY_CODE})는: {matched[0] if matched else '목록에서 찾지 못했습니다 — 값을 다시 확인해주세요'}\n\n"
    await interaction.followup.send(
        header + "전체 도시코드 목록:\n" + ("\n".join(lines) if lines else "결과 없음")
    )


# -------------------------------------------------------------------------------------------
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("오류: .env 파일에서 DISCORD_TOKEN을 찾을 수 없습니다.")