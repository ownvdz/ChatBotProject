import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# .env 파일에서 환경변수(토큰) 로드
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# 임시 시간표 데이터 (추후 DB 연동 또는 JSON 파일로 분리 가능)
TIMETABLE_DATA = {
    "월요일": [
        {"time": "09:00 ~ 10:30", "subject": "자료구조", "room": "공학관 301호"},
        {"time": "11:00 ~ 12:30", "subject": "컴퓨터 네트워크", "room": "공학관 204호"},
    ],
    "화요일": [
        {"time": "13:00 ~ 15:00", "subject": "운영체제", "room": "공학관 502호"},
    ],
    "수요일": [
        {"time": "10:00 ~ 12:00", "subject": "소프트웨어 공학", "room": "공학관 301호"},
        {"time": "14:00 ~ 16:00", "subject": "데이터베이스", "room": "공학관 401호"},
    ],
    "목요일": [
        {"time": "09:00 ~ 10:30", "subject": "자료구조", "room": "공학관 301호"},
    ],
    "금요일": [
        {"time": "15:00 ~ 17:00", "subject": "캡스톤 디자인", "room": "공학관 101호"},
    ]
}

# 봇 클래스 정의
class TimetableBot(commands.Bot):
    def __init__(self):
        # 디스코드 인텐트 설정
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 슬래시 명령어를 디스코드 서버에 동기화
        await self.tree.sync()
        print("슬래시 명령어 동기화 완료!")

    async def on_ready(self):
        print(f'로그인 성공: {self.user.name} (ID: {self.user.id})')
        print('====== 봇이 정상적으로 작동 중입니다 ======')

bot = TimetableBot()

# /시간표 [요일] 슬래시 명령어 작성
@bot.tree.command(name="시간표", description="특정 요일의 수업 시간표를 조회합니다.")
@app_commands.describe(day="조회할 요일을 선택하세요 (예: 월요일, 화요일)")
@app_commands.choices(day=[
    app_commands.Choice(name="월요일", value="월요일"),
    app_commands.Choice(name="화요일", value="화요일"),
    app_commands.Choice(name="수요일", value="수요일"),
    app_commands.Choice(name="목요일", value="목요일"),
    app_commands.Choice(name="금요일", value="금요일"),
])
async def show_timetable(interaction: discord.Interaction, day: app_commands.Choice[str]):
    selected_day = day.value
    schedule = TIMETABLE_DATA.get(selected_day)

    # 시연 시 임베드(Embed) 형태로 정돈되어 보이게 구성
    embed = discord.Embed(
        title=f"📅 {selected_day} 시간표",
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
    
    # 디스코드 채널에 응답
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("오류: .env 파일에서 DISCORD_TOKEN을 찾을 수 없습니다.")