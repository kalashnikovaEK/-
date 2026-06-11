"""
bot.py
──────
Discord VC Keep-Alive Bot  ·  메인 진입점

슬래시 커맨드
─────────────
/join     [channel]  : 지정 채널(또는 현재 접속 채널)에 입장
/leave               : 채널 퇴장
/status              : 현재 연결 상태 확인
/vcstatus  (관리자)  : VoiceManager 상세 상태 확인
/vcmove    (관리자)  : 명령어를 사용한 사람의 현재 음성 채널로 봇 이동
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from channel_store import clear_channel, load_channel, save_channel
from keep_alive import start_keep_alive
from voice_manager import VoiceManager

# ── 로깅 설정 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
load_dotenv()

TOKEN: str = os.environ["DISCORD_TOKEN"]
GUILD_ID: Optional[int] = (
    int(os.environ["GUILD_ID"]) if os.getenv("GUILD_ID") else None
)
AUTO_JOIN_CHANNEL_ID: Optional[int] = (
    int(os.environ["AUTO_JOIN_CHANNEL_ID"])
    if os.getenv("AUTO_JOIN_CHANNEL_ID")
    else None
)

# 시작 시 채널 탐색을 몇 번까지 재시도할지 (길드 캐시가 채워지길 기다리기 위함)
RESUME_LOOKUP_RETRIES: int = 5
RESUME_LOOKUP_DELAY: float = 2.0  # 초

# ── Bot 설정 ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, enable_debug_events=True)
voice_managers: dict[int, VoiceManager] = {}   # guild_id → VoiceManager


# ── 이벤트 핸들러 ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)

    # 슬래시 커맨드 동기화
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info("Slash commands synced to guild %d", GUILD_ID)
    else:
        await bot.tree.sync()
        logger.info("Slash commands synced globally")

    # ── 자동 입장 / 마지막 채널 복구 ───────────────────────────────────────
    # 우선순위: voice_state.json 에 저장된 "마지막 접속 채널"
    #          → 없으면 AUTO_JOIN_CHANNEL_ID (환경변수, 컨테이너 교체 시에도 유지됨)
    saved_channel_id = load_channel()
    if saved_channel_id:
        target_channel_id: Optional[int] = saved_channel_id
        source = "저장된 마지막 채널(voice_state.json)"
    elif AUTO_JOIN_CHANNEL_ID:
        target_channel_id = AUTO_JOIN_CHANNEL_ID
        source = "AUTO_JOIN_CHANNEL_ID"
    else:
        target_channel_id = None
        source = ""

    if target_channel_id:
        channel = await _resolve_channel_with_retry(target_channel_id)

        if isinstance(channel, discord.VoiceChannel):
            logger.info(
                "[Resume] %s 기준으로 '%s' 채널 자동 입장 시도.", source, channel.name
            )
            await _join_channel(channel)
        else:
            logger.warning(
                "[Resume] 채널 ID=%d (%s) 를 찾을 수 없거나 음성 채널이 아닙니다.",
                target_channel_id, source,
            )
            # 저장된 채널이 더 이상 유효하지 않다면 다음 기동을 위해 정리한다.
            if saved_channel_id and target_channel_id == saved_channel_id:
                clear_channel()


async def _resolve_channel_with_retry(channel_id: int) -> Optional[discord.abc.GuildChannel]:
    """
    on_ready 직후에는 길드/채널 캐시가 아직 채워지지 않았을 수 있으므로
    잠시 간격을 두고 몇 차례 재시도한다.
    """
    for attempt in range(1, RESUME_LOOKUP_RETRIES + 1):
        channel = bot.get_channel(channel_id)
        if channel is not None:
            return channel

        if attempt < RESUME_LOOKUP_RETRIES:
            logger.debug(
                "[Resume] 채널 ID=%d 캐시에 없음 (시도 %d/%d) – %.1f초 후 재시도.",
                channel_id, attempt, RESUME_LOOKUP_RETRIES, RESUME_LOOKUP_DELAY,
            )
            await asyncio.sleep(RESUME_LOOKUP_DELAY)

    # 캐시에 없으면 API 로 직접 조회 시도
    try:
        return await bot.fetch_channel(channel_id)
    except discord.HTTPException as e:
        logger.warning("[Resume] fetch_channel(%d) 실패: %s", channel_id, e)
        return None


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """
    봇 자신이 채널에서 강제 퇴장(kick) 당하거나
    채널이 삭제된 경우를 감지하여 재연결을 트리거한다.
    """
    if member.id != bot.user.id:
        return

    guild_id = member.guild.id
    vm = voice_managers.get(guild_id)
    if vm is None:
        return

    # 채널에서 나간 경우(before 있음 / after 없음) → 재연결 시도
    if before.channel is not None and after.channel is None:
        logger.warning(
            "[Event] Bot was disconnected from '%s' in guild %d – triggering reconnect.",
            before.channel.name, guild_id,
        )
        asyncio.create_task(vm._schedule_reconnect())


@bot.event
async def on_socket_raw_receive(msg) -> None:
    """
    Discord 게이트웨이의 raw 메시지를 검사하여 VOICE_SERVER_UPDATE
    (음성 서버/리전 변경)를 감지한다.

    discord.py 가 내부적으로 음성 웹소켓 재연결을 처리하지만,
    드물게 연결이 멈춰버리는 경우가 있어 VoiceManager 에 알려
    연결 상태를 재검증하도록 한다.

    참고: discord.py 2.x 에서는 (구버전의) on_socket_response 이벤트가
    제거되었으므로, enable_debug_events=True 와 함께 제공되는
    on_socket_raw_receive(원시 JSON 문자열)를 사용한다.
    """
    if isinstance(msg, (bytes, bytearray)):
        return  # zlib 압축 바이너리 프레임은 처리하지 않음

    # 매 게이트웨이 메시지마다 호출되므로, json.loads 전에 빠르게 필터링한다.
    if "VOICE_SERVER_UPDATE" not in msg:
        return

    try:
        payload = json.loads(msg)
    except (TypeError, json.JSONDecodeError):
        return

    if payload.get("t") != "VOICE_SERVER_UPDATE":
        return

    data = payload.get("d") or {}
    raw_guild_id = data.get("guild_id")
    endpoint = data.get("endpoint")

    logger.warning(
        "[VoiceServerUpdate] guild=%s endpoint=%s",
        raw_guild_id, endpoint,
    )

    if not raw_guild_id:
        return

    vm = voice_managers.get(int(raw_guild_id))
    if vm is not None:
        asyncio.create_task(vm.handle_voice_server_update())


@bot.event
async def on_disconnect() -> None:
    logger.warning("[Gateway] Disconnected from Discord gateway.")


@bot.event
async def on_resumed() -> None:
    logger.info("[Gateway] Session resumed.")


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

async def _join_channel(channel: discord.VoiceChannel) -> None:
    guild_id = channel.guild.id

    # 기존 매니저 정리
    if guild_id in voice_managers:
        await voice_managers[guild_id].stop()

    vm = VoiceManager(bot)
    voice_managers[guild_id] = vm
    await vm.start(channel)

    # 마지막으로 접속한 채널을 저장 (재시작 시 복구용)
    save_channel(channel.id)


# ── 슬래시 커맨드 ─────────────────────────────────────────────────────────────

@bot.tree.command(name="join", description="지정 음성 채널에 입장합니다.")
@app_commands.describe(channel="입장할 음성 채널 (생략 시 현재 접속 채널)")
async def cmd_join(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    target = channel
    if target is None:
        # 호출한 유저의 현재 음성 채널
        if interaction.user.voice and interaction.user.voice.channel:
            target = interaction.user.voice.channel
        else:
            await interaction.followup.send(
                "❌ 음성 채널을 지정하거나, 먼저 채널에 입장해주세요."
            )
            return

    await _join_channel(target)
    await interaction.followup.send(
        f"✅ **{target.name}** 채널에 입장했습니다. 무음 스트림이 시작됩니다."
    )


@bot.tree.command(name="leave", description="음성 채널에서 퇴장합니다.")
async def cmd_leave(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild_id
    vm = voice_managers.pop(guild_id, None)
    if vm:
        await vm.stop()
        clear_channel()
        await interaction.response.send_message("👋 채널에서 퇴장했습니다.")
    else:
        await interaction.response.send_message("❌ 현재 음성 채널에 없습니다.")


@bot.tree.command(name="status", description="봇의 음성 연결 상태를 확인합니다.")
async def cmd_status(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild_id
    vm = voice_managers.get(guild_id)

    if vm and vm.is_connected:
        vc = vm._vc
        channel_name = vc.channel.name if vc and vc.channel else "?"
        playing = vc.is_playing() if vc else False
        await interaction.response.send_message(
            f"🟢 **연결 중** — 채널: `{channel_name}` | 무음 스트림: `{'재생 중' if playing else '중단'}`",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message("🔴 **연결 없음**", ephemeral=True)


@bot.tree.command(name="vcstatus", description="[관리자] VC 연결 상세 상태를 확인합니다.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_vcstatus(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild_id
    vm = voice_managers.get(guild_id)

    if not vm:
        await interaction.response.send_message("VoiceManager 없음", ephemeral=True)
        return

    latency = bot.latency
    if latency == latency:  # NaN 이 아니면
        latency_text = f"{latency * 1000:.0f}ms"
    else:
        latency_text = "측정 불가(NaN)"

    msg = (
        f"연결 상태: `{vm.is_connected}`\n"
        f"재생 중: `{vm.is_playing}`\n"
        f"채널: `{vm.channel_name}`\n"
        f"재연결 중: `{vm.is_reconnecting}`\n"
        f"게이트웨이 지연(latency): `{latency_text}`"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="vcmove", description="[관리자] 봇을 명령어 사용자의 현재 음성 채널로 이동시킵니다.")
@app_commands.default_permissions(manage_guild=True)
async def cmd_vcmove(interaction: discord.Interaction) -> None:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "음성 채널에 먼저 입장한 뒤 사용해주세요.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    target = interaction.user.voice.channel
    await _join_channel(target)
    await interaction.followup.send(f"✅ **{target.name}** 채널로 이동 완료.")


# ── 종료 핸들러 ───────────────────────────────────────────────────────────────

async def shutdown() -> None:
    logger.info("[Shutdown] Graceful shutdown started.")
    for vm in list(voice_managers.values()):
        await vm.stop()
    voice_managers.clear()
    await bot.close()
    logger.info("[Shutdown] Done.")


def _handle_signal(sig, frame) -> None:
    logger.info("[Signal] Received %s", signal.Signals(sig).name)
    asyncio.create_task(shutdown())


# ── 메인 엔트리 ───────────────────────────────────────────────────────────────

async def main() -> None:
    # 1. Keep-Alive 웹서버 먼저 시작
    runner = await start_keep_alive()

    # 2. OS 시그널 핸들러 등록 (SIGTERM: Cloudtype 컨테이너 종료 시 전달)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown()))
        except NotImplementedError:
            # Windows 에서는 add_signal_handler 미지원
            signal.signal(sig, _handle_signal)

    # 3. 봇 실행 (reconnect=True 로 게이트웨이 자동 재연결 활성화)
    try:
        await bot.start(TOKEN, reconnect=True)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
