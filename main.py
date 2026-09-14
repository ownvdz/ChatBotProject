import os
import json
import time
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
BUS_API_KEY = os.getenv("PUBLIC_BUS_API_KEY")

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

# 511번 버스의 실제 경유 정류장 순서 (사용자 제공, 확인용/디버깅용 참고 데이터)
# 방향1: 주안역환승정류장 → 정석항공과학고
ROUTE_511_DIRECTION_1 = [
    "주안역환승정류장", "주안1동행정복지센터", "도화IC", "인천광역시종합건설본부",
    "대명아파트", "인천기계공고", "용일사거리", "제운사거리", "학산소극장", "정석항공과학고",
]
# 방향2: 정석항공과학고 → 주안역환승정류장
ROUTE_511_DIRECTION_2 = [
    "정석항공과학고", "학산소극장", "제운사거리", "용일사거리", "인천기계공고",
    "주안센트럴파라곤아파트", "제일시장", "주안사거리", "교보생명", "주안역환승정류장",
]

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


# 지하철 API / 아직은 임시 데이터 사용
def get_subway_info(station_name: str):
    return [
        {"line": "1호선", "destination": "청량리행 (상행)", "status": "3분 후 도착 (전역 출발)"},
        {"line": "1호선", "destination": "인천행 (하행)", "status": "8분 후 도착 (2개 전역)"},
    ]


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
        "serviceKey": BUS_API_KEY,
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
        "serviceKey": BUS_API_KEY,
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
        "serviceKey": BUS_API_KEY,
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
        "serviceKey": BUS_API_KEY,
        "cityCode": city_code,
        "routeId": route_id,
        "numOfRows": str(num_of_rows),
        "pageNo": "1",
        "_type": "xml",
    }
    return _fetch_tago_xml(url, params)


def get_city_code_list():
    url = f"{TAGO_HOST}/BusRouteInfoInqireService/getCtyCodeList"
    params = {"serviceKey": BUS_API_KEY, "_type": "xml"}
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
@bot.tree.command(name="지하철", description="지정한 지하철역의 실시간 도착 정보를 조회합니다.")
@app_commands.describe(station="조회할 지하철역 이름을 입력하세요 (예: 인천대입구, 부평)")
async def show_subway(interaction: discord.Interaction, station: str):
    subway_data = get_subway_info(station)

    embed = discord.Embed(
        title=f"🚇 '{station}역' 실시간 도착 정보",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now()
    )

    for info in subway_data:
        embed.add_field(
            name=f"[{info['line']}] {info['destination']}",
            value=f"⏱️ **{info['status']}**",
            inline=False
        )

    embed.set_footer(text="⚠️ 현재 임시(Mock) 데이터입니다. 실 API 연동 예정 | V1.1")
    await interaction.response.send_message(embed=embed)


# --- 🚌 /버스 명령어: 511번 버스, 양방향 정류장 실시간 도착 "분" 정보 ---
@bot.tree.command(name="버스", description="511번 버스의 주안역/정석항공과학고 실시간 도착 예정 정보를 조회합니다.")
async def bus(interaction: discord.Interaction):
    await interaction.response.defer()

    if not BUS_API_KEY:
        await interaction.followup.send("⚠️ PUBLIC_BUS_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")
        return

    ok, stops = get_cached_route_stops(DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID)
    if not ok:
        await interaction.followup.send(f"⚠️ 노선 정류소 목록 조회 실패: {stops}")
        return

    # 차량번호는 도착정보 API엔 없고 위치정보 API에만 있어서, 한 번만 받아와 매칭에 사용한다.
    loc_ok, location_items = get_route_bus_locations(DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID)
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

            ok, arrivals = get_arrival_info(DEFAULT_CITY_CODE, node_id, DEFAULT_ROUTE_ID)
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

    ok, stops = get_cached_route_stops(DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID, force_refresh=True)
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

    ok, result = search_stations_by_name(DEFAULT_CITY_CODE, keyword)
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

    ok, result = get_route_bus_locations(DEFAULT_CITY_CODE, DEFAULT_ROUTE_ID)
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

    ok, result = get_city_code_list()
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