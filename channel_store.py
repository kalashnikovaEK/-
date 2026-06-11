"""
channel_store.py
─────────────────
봇이 마지막으로 접속했던 음성 채널 ID를 로컬 JSON 파일에 저장/로드한다.

⚠️ 주의 (Cloudtype 등 컨테이너 환경)
─────────────────────────────────
컨테이너의 로컬 파일시스템은 "재시작(프로세스 크래시 후 재기동)"에는
보존되지만, "재배포 / 이미지 교체 / 컨테이너 재생성"에는 보존되지
않을 수 있다 (Cloudtype 기본 디스크는 임시 스토리지).

따라서:
  - 영구 보존이 필요하면 VOICE_STATE_FILE 환경변수를
    Cloudtype의 영구 디스크(Volume) 마운트 경로로 지정해야 한다.
  - 영구 디스크를 쓰지 않는다면, AUTO_JOIN_CHANNEL_ID 환경변수가
    "재배포/컨테이너 교체" 상황의 1차 복구 수단이 된다.
  - 이 파일은 "프로세스 재시작" 및 "/vcmove 로 이동한 채널을
    다음 기동 시 우선 적용" 용도의 보조 수단으로 사용한다.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

VOICE_STATE_FILE: str = os.getenv("VOICE_STATE_FILE", "voice_state.json")


def save_channel(channel_id: int) -> None:
    """현재 접속 중인 음성 채널 ID 를 파일에 저장한다 (best-effort)."""
    try:
        tmp_path = f"{VOICE_STATE_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"channel_id": channel_id}, f)
        os.replace(tmp_path, VOICE_STATE_FILE)
        logger.debug("[ChannelStore] Saved channel_id=%d to %s", channel_id, VOICE_STATE_FILE)
    except OSError as e:
        logger.warning("[ChannelStore] Failed to save channel state: %s", e)


def load_channel() -> Optional[int]:
    """저장된 음성 채널 ID 를 로드한다. 없으면 None."""
    try:
        with open(VOICE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        channel_id = data.get("channel_id")
        return int(channel_id) if channel_id is not None else None
    except FileNotFoundError:
        logger.debug("[ChannelStore] No saved channel state file (%s).", VOICE_STATE_FILE)
        return None
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("[ChannelStore] Failed to load channel state: %s", e)
        return None


def clear_channel() -> None:
    """저장된 음성 채널 정보를 삭제한다 (예: /leave 시)."""
    try:
        if os.path.exists(VOICE_STATE_FILE):
            os.remove(VOICE_STATE_FILE)
            logger.debug("[ChannelStore] Cleared channel state file.")
    except OSError as e:
        logger.warning("[ChannelStore] Failed to clear channel state: %s", e)
