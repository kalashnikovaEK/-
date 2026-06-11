# 🤖 Discord VC Keep-Alive Bot

24시간 음성 채널에서 무음 스트림을 송출하여 봇이 잠수 처리되지 않도록 유지하는 Discord 봇입니다.

## ✨ 주요 기능

| 기능 | 설명 |
|------|------|
| **무음 오디오 스트림** | 48kHz Stereo 무음 PCM 을 지속 송출하여 잠수(AFK) 판단 방지 |
| **Voice Gateway 재연결** | 지수 백오프 방식으로 자동 재연결 (최대 120초 간격) |
| **Watchdog Loop** | 15초마다 연결 상태 점검, 끊김 즉시 감지 |
| **Keep-Alive 웹서버** | Cloudtype 포트 바인딩용 aiohttp HTTP 서버 (`/health`) |
| **슬래시 커맨드** | `/join` `/leave` `/status` |
| **Persistent Process** | SIGTERM/SIGINT 시 안전하게 정리 후 종료 |

---

## 📁 프로젝트 구조

```
discord-vc-bot/
├── src/
│   ├── bot.py            # 메인 진입점, 슬래시 커맨드, 이벤트 핸들러
│   ├── voice_manager.py  # 음성 연결 관리, 재연결 로직
│   ├── audio.py          # 무음 PCM AudioSource
│   └── keep_alive.py     # aiohttp Keep-Alive 웹서버
├── Dockerfile            # Cloudtype 배포용 멀티스테이지 빌드
├── cloudtype.yml         # Cloudtype 배포 설정
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 🚀 배포 방법 (Cloudtype)

### 1. 사전 준비

- [Discord Developer Portal](https://discord.com/developers/applications) 에서 봇 생성
- **Privileged Gateway Intents** → `Server Members Intent`, `Voice State` 활성화
- 봇을 서버에 초대할 때 `bot` + `applications.commands` + `Connect` + `Speak` 권한 부여

### 2. GitHub 업로드

```bash
git init
git add .
git commit -m "init: discord vc bot"
git remote add origin https://github.com/YOUR_NAME/discord-vc-bot.git
git push -u origin main
```

### 3. Cloudtype 연동

1. [Cloudtype](https://cloudtype.io) → **새 프로젝트 → GitHub 연결**
2. 저장소 선택 후 **Container** 타입 선택 (Dockerfile 자동 감지)
3. **환경변수** 탭에서 아래 값 입력:

| 키 | 값 | 필수 |
|----|----|------|
| `DISCORD_TOKEN` | 봇 토큰 | ✅ |
| `GUILD_ID` | 슬래시 커맨드 즉시 동기화 서버 ID | 선택 |
| `AUTO_JOIN_CHANNEL_ID` | 시작 시 자동 입장 채널 ID | 선택 |
| `PORT` | `8080` (기본값) | 선택 |

4. **배포** 클릭

---

## 💻 로컬 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# .env 파일 생성
cp .env.example .env
# DISCORD_TOKEN 등 실제 값 입력

# 봇 실행
python src/bot.py
```

> **Windows 주의**: `libsodium` 이 없으면 음성 기능이 동작하지 않습니다.  
> `pip install PyNaCl` 으로 설치하거나 WSL 을 사용하세요.

---

## 🎮 슬래시 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/join [channel]` | 지정 채널 또는 현재 접속 채널에 입장 |
| `/leave` | 채널 퇴장 및 스트림 중단 |
| `/status` | 현재 연결 상태 및 스트림 여부 확인 |

---

## 🔧 재연결 로직 상세

```
연결 끊김 감지 (on_voice_state_update 이벤트 또는 Watchdog)
          ↓
_schedule_reconnect() 호출
          ↓
지수 백오프: 5s → 10s → 20s → 40s → 80s → 120s (최대)
          ↓
_connect() 성공 → 무음 스트림 재시작
          ↓ 실패
다음 시도 (최대 20회)
```

**Watchdog** 는 15초마다 아래를 체크합니다:
- `VoiceClient.is_connected()` → False 이면 재연결 예약
- `VoiceClient.is_playing()` → False 이면 스트림 재시작

---

## 📝 환경변수 설명

```env
DISCORD_TOKEN=          # 봇 토큰 (필수)
GUILD_ID=               # 특정 서버에만 슬래시 커맨드 즉시 등록 (선택)
AUTO_JOIN_CHANNEL_ID=   # 봇 시작 시 자동 입장할 채널 ID (선택)
PORT=8080               # Keep-Alive 웹서버 포트
```

---

## ⚙️ Keep-Alive 웹서버 엔드포인트

| 경로 | 응답 |
|------|------|
| `GET /` | `Discord VC Bot is running ✅` |
| `GET /health` | `{"status":"ok","timestamp":"...","uptime":...}` |

---

## 📦 의존성

- [discord.py](https://github.com/Rapptz/discord.py) >= 2.3.2 (voice 포함)
- [aiohttp](https://docs.aiohttp.org/) >= 3.9.0
- [python-dotenv](https://github.com/theskumar/python-dotenv) >= 1.0.0
- [PyNaCl](https://pynacl.readthedocs.io/) >= 1.5.0
- **ffmpeg** (Dockerfile 에서 자동 설치)

---

## 📄 License

MIT
