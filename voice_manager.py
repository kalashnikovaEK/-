"""
voice_manager.py
────────────────
VoiceGateway 재연결 / 상태 감지 / 무음 스트림 관리를 담당하는 매니저 클래스.

주요 기능
─────────
1. connect()                    : 채널에 최초 접속 + 무음 스트림 시작
2. reconnect()                  : 연결 끊김 감지 후 지수 백오프로 재접속 (무제한)
3. ensure_playing()              : 재생 중단 시 무음 스트림 재시작
4. watchdog loop                 : 5초 주기로 VoiceClient / 게이트웨이 상태 확인
5. handle_voice_server_update()  : Discord 음성 서버(리전) 변경 감지 시 연결 검증
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

import discord

from audio import LoopingSilentAudio

logger = logging.getLogger(__name__)

# ── 설정 상수 ─────────────────────────────────────────────────────────────────
WATCHDOG_INTERVAL: float = 5.0        # 상태 체크 주기 (초) — 15초 -> 5초
RECONNECT_BASE_DELAY: float = 5.0     # 재연결 최초 대기 시간 (초)
RECONNECT_MAX_DELAY: float = 120.0    # 재연결 최대 대기 시간 (초)
RECONNECT_MAX_ATTEMPTS: int = 0       # 최대 재시도 횟수 (0 = 무제한, 장시간 단절 대응)
LATENCY_BAD_THRESHOLD: float = 300.0  # 게이트웨이 latency==0(또는 NaN) 지속 허용시간 (초) = 5분


class VoiceManager:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._channel: Optional[discord.VoiceChannel] = None
        self._vc: Optional[discord.VoiceClient] = None
        self._audio: Optional[LoopingSilentAudio] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # ── 상태 추적 (watchdog) ───────────────────────────────────────────
        self._last_good_state: float = time.monotonic()  # 마지막으로 정상(latency>0) 확인된 시각
        self._latency_bad_since: Optional[float] = None  # latency==0/NaN 이 시작된 시각

    # ── 공개 인터페이스 ────────────────────────────────────────────────────────

    async def start(self, channel: discord.VoiceChannel) -> None:
        """채널에 접속하고 watchdog 루프를 시작한다."""
        self._channel = channel
        self._shutdown = False
        await self._connect()
        self._start_watchdog()
        logger.info("[VoiceManager] Started in channel: %s", channel.name)

    async def stop(self) -> None:
        """모든 태스크를 정리하고 음성 채널에서 퇴장한다."""
        self._shutdown = True
        self._cancel_task(self._watchdog_task)
        self._cancel_task(self._reconnect_task)
        await self._disconnect()
        logger.info("[VoiceManager] Stopped.")

    @property
    def is_connected(self) -> bool:
        return self._vc is not None and self._vc.is_connected()

    @property
    def is_playing(self) -> bool:
        return bool(self._vc and self._vc.is_playing())

    @property
    def is_reconnecting(self) -> bool:
        return bool(self._reconnect_task and not self._reconnect_task.done())

    @property
    def channel_name(self) -> str:
        return self._channel.name if self._channel else "없음"

    # ── 연결 / 해제 ───────────────────────────────────────────────────────────

    async def _connect(self) -> bool:
        """
        음성 채널에 접속하고 무음 스트림을 시작한다.
        성공 시 True 반환.
        """
        if self._channel is None:
            return False

        try:
            # 이미 연결된 VoiceClient 가 있으면 move_to 로 채널 전환
            if self._vc and self._vc.is_connected():
                await self._vc.move_to(self._channel)
            else:
                self._vc = await self._channel.connect(
                    timeout=30.0,
                    reconnect=True,   # discord.py 내장 재연결 활성화
                )

            self._start_silent_stream()
            self._last_good_state = time.monotonic()
            self._latency_bad_since = None
            logger.info("[VoiceManager] Connected to: %s", self._channel.name)
            return True

        except discord.ClientException as e:
            logger.warning("[VoiceManager] ClientException during connect: %s", e)
        except asyncio.TimeoutError:
            logger.warning("[VoiceManager] Connection timed out: %s", self._channel.name)
        except Exception as e:
            logger.error("[VoiceManager] Unexpected error during connect: %s", e, exc_info=True)

        return False

    async def _disconnect(self) -> None:
        if self._audio:
            self._audio.cleanup()
            self._audio = None

        if self._vc:
            try:
                await self._vc.disconnect(force=True)
            except Exception:
                pass
            self._vc = None

    # ── 무음 스트림 ────────────────────────────────────────────────────────────

    def _start_silent_stream(self) -> None:
        """
        기존 재생을 중단하고 새 LoopingSilentAudio 를 시작한다.
        after 콜백에서 스트림이 끊기면 자동으로 ensure_playing() 을 예약한다.
        """
        if not self._vc or not self._vc.is_connected():
            return

        # 이전 소스 정리
        if self._vc.is_playing() or self._vc.is_paused():
            self._vc.stop()

        if self._audio:
            self._audio.cleanup()

        self._audio = LoopingSilentAudio()

        def after_play(error: Optional[Exception]) -> None:
            if error:
                logger.warning("[VoiceManager] Audio playback error: %s", error)
            if not self._shutdown:
                # 이벤트 루프에 재시작 예약
                asyncio.run_coroutine_threadsafe(
                    self._on_stream_ended(), self.bot.loop
                )

        self._vc.play(self._audio, after=after_play)
        logger.debug("[VoiceManager] Silent stream started.")

    async def _on_stream_ended(self) -> None:
        """스트림이 종료되면 재연결 또는 재시작을 시도한다."""
        await asyncio.sleep(0.5)
        if self._shutdown:
            return
        if self.is_connected:
            self._start_silent_stream()
        else:
            await self._schedule_reconnect()

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def _start_watchdog(self) -> None:
        self._cancel_task(self._watchdog_task)
        self._last_good_state = time.monotonic()
        self._latency_bad_since = None
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="voice-watchdog"
        )

    async def _watchdog_loop(self) -> None:
        """
        5초 주기로 다음을 점검한다.
        ─ VoiceClient 연결 끊김       → 재연결 예약
        ─ 무음 스트림 재생 중단        → 무음 스트림 재시작
        ─ 게이트웨이 latency==0/NaN
          상태가 5분 이상 지속        → 강제 재연결 예약 (네트워크 단절 대응)
        """
        logger.debug("[Watchdog] Loop started (interval=%.1fs)", WATCHDOG_INTERVAL)
        while not self._shutdown:
            await asyncio.sleep(WATCHDOG_INTERVAL)

            if self._shutdown:
                break

            now = time.monotonic()

            # ── 1) 게이트웨이 latency 점검 ────────────────────────────────
            latency = self.bot.latency
            if latency == 0 or latency is None or math.isnan(latency):
                if self._latency_bad_since is None:
                    self._latency_bad_since = now
                    logger.warning("[Watchdog] Gateway latency==0/NaN detected.")

                bad_elapsed = now - self._latency_bad_since
                if bad_elapsed >= LATENCY_BAD_THRESHOLD:
                    logger.error(
                        "[Watchdog] Gateway latency==0/NaN for %.0fs (>= %.0fs) -- "
                        "forcing reconnect.",
                        bad_elapsed, LATENCY_BAD_THRESHOLD,
                    )
                    self._latency_bad_since = None
                    self._last_good_state = now
                    await self._schedule_reconnect()
                    continue
            else:
                self._latency_bad_since = None
                self._last_good_state = now

            # ── 2) VoiceClient 연결 / 재생 상태 점검 ──────────────────────
            if not self.is_connected:
                logger.warning("[Watchdog] Voice not connected – scheduling reconnect.")
                await self._schedule_reconnect()
            elif not self._vc.is_playing():
                logger.debug("[Watchdog] Stream not playing – restarting.")
                self._start_silent_stream()

    # ── Voice Server Update 대응 ──────────────────────────────────────────────

    async def handle_voice_server_update(self) -> None:
        """
        Discord 가 음성 서버(리전)를 변경(VOICE_SERVER_UPDATE)했을 때 호출된다.
        discord.py 가 내부적으로 음성 웹소켓을 재연결하지만, 드물게 연결이
        멈춰버리는 경우가 있어 약간의 지연 후 상태를 재검증한다.
        """
        if self._shutdown:
            return

        logger.info(
            "[VoiceManager] Voice server update detected (channel=%s) – "
            "verifying connection in 10s.",
            self.channel_name,
        )
        await asyncio.sleep(10)

        if self._shutdown:
            return

        if not self.is_connected:
            logger.warning(
                "[VoiceManager] Connection lost after voice server update – reconnecting."
            )
            await self._schedule_reconnect()
        elif not self._vc.is_playing():
            logger.warning(
                "[VoiceManager] Stream not playing after voice server update – restarting."
            )
            self._start_silent_stream()

    # ── 재연결 로직 (지수 백오프) ──────────────────────────────────────────────

    async def _schedule_reconnect(self) -> None:
        """재연결 태스크가 없을 때만 새로 생성한다."""
        if self._reconnect_task and not self._reconnect_task.done():
            return  # 이미 재연결 중

        self._reconnect_task = asyncio.create_task(
            self._reconnect_with_backoff(), name="voice-reconnect"
        )

    async def _reconnect_with_backoff(self) -> None:
        delay = RECONNECT_BASE_DELAY
        attempt = 0

        while not self._shutdown:
            attempt += 1

            if RECONNECT_MAX_ATTEMPTS and attempt > RECONNECT_MAX_ATTEMPTS:
                logger.error(
                    "[Reconnect] Exceeded max attempts (%d). Giving up.",
                    RECONNECT_MAX_ATTEMPTS,
                )
                break

            logger.info(
                "[Reconnect] Attempt %d (delay=%.1fs) → %s",
                attempt, delay, self._channel.name if self._channel else "?",
            )

            await self._disconnect()
            await asyncio.sleep(delay)

            success = await self._connect()
            if success:
                logger.info("[Reconnect] Reconnected successfully on attempt %d.", attempt)
                return

            # 지수 백오프 (최대 RECONNECT_MAX_DELAY 초)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

        logger.error("[Reconnect] Reconnect loop ended without success.")

    # ── 유틸 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()
