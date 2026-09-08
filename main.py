import os
import json
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# .env 파일에서 토큰 가져오기
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# json에서 데이터 가져오기
def load_timetable():
    json_path = "timetable.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        print(f"경고: {json_path} 파일을 찾을 수 없습니다. 빈 데이터를 반환합니다.")
        return {}

# -------------------------------------------------------------------------------------------
class TimetableBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("슬래시 명령어 동기화 완료!")

    async def on_ready(self):
        print(f'로그인 성공: {self.user.name} (ID: {self.user.id})')
        print('====== 봇이 정상적으로 작동 중입니다 ======')

bot = TimetableBot()

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
# 2. day: app_commands.Choice[str] = None 으로 수정 (기본값 설정)
async def show_timetable(interaction: discord.Interaction, day: app_commands.Choice[str] = None):
    timetable_data = load_timetable()

    if day is None:
        today_idx = datetime.datetime.now().weekday()  # 0: 월, 1: 화, ...
        selected_day = WEEKDAYS[today_idx]
        title_prefix = f"📅 오늘({selected_day})의 시간표"
    else:
        selected_day = day.value
        title_prefix = f"📅 {selected_day} 시간표"
        
    schedule = timetable_data.get(selected_day)

    # 3. title에 준비해 둔 title_prefix 변수 적용
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

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("오류: .env 파일에서 DISCORD_TOKEN을 찾을 수 없습니다.")