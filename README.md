# Telegram Bot

AI 기반 텔레그램 봇 — 챗봇, 금융 조회, YouTube 아카이빙, 할일 관리.

## 기능

- **챗봇**: 기본 대화 모드. `/rp`, `/prompt` 명령어로 프롬프트 커스터마이즈 가능
- **금융**: 환율 조회, 환전 계산, 코인 가격 조회 (LLM 자연어 처리)
- **YouTube 아카이빙**: 영상/썸네일 다운로드 → B2 업로드 (main6.py 기반)
- **할일 관리**: 할일 추가/조회/완료 + 예약 시간 알림

## 빠른 시작

```bash
cp .env.example .env
# .env 파일을 수정하세요

# 로컬 실행
pip install -r requirements.txt
python -m bot.main

# Docker 실행
docker compose up -d
```

## 명령어

| 명령어 | 설명 |
|--------|------|
| `/rp <캐릭터>` | RP 모드 활성화 |
| `/prompt <프롬프트>` | 시스템 프롬프트 오버라이드 |
| `/reset` | 프롬프트 및 대화 초기화 |
| `/mode` | 현재 모드 확인 |

명령어 없이 메시지를 보내면 챗봇 모드로 동작합니다. LLM이 자연어를 분석하여 금융 조회, YouTube 아카이빙, 할일 관리를 자동으로 처리합니다.