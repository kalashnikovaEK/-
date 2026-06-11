"""
audio.py
────────
무음 PCM 오디오 소스 (잠수 유저 판단 방지용 Silent Stream)
discord.py AudioSource 기반으로 48kHz / Stereo / 16-bit 무음 프레임을 지속 송출.
"""

import discord
import struct
import logging

logger = logging.getLogger(__name__)

# 48000Hz × 2ch × 2bytes × 0.02s(20ms) = 3840 bytes per frame
SILENT_FRAME = b"\x00" * 3840


class SilentAudioSource(discord.AudioSource):
    """
    20ms 단위로 무음 PCM 프레임을 반환하는 AudioSource.
    VoiceClient 가 자동으로 20ms 간격으로 read() 를 호출한다.
    """

    def read(self) -> bytes:
        return SILENT_FRAME

    def is_opus(self) -> bool:
        return False  # PCM 원시 데이터이므로 False


class LoopingSilentAudio(discord.AudioSource):
    """
    SilentAudioSource 와 동일하나 재생 종료 콜백 없이 무한 루프.
    VoiceClient.play() 의 after 콜백에서 다시 play() 를 호출하는 방식을 사용할 수도
    있지만, 단일 소스로 무한 재생하는 것이 더 안정적이다.
    """

    def __init__(self):
        self._stopped = False

    def read(self) -> bytes:
        if self._stopped:
            return b""   # 빈 바이트 → 재생 종료 신호
        return SILENT_FRAME

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self._stopped = True
        logger.debug("[Audio] LoopingSilentAudio cleanup called")
