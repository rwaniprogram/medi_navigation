# 메디내비(MediNavi) 온라인 진료실

진료실에서 의사가 묻는 순서(접수 → 증상 문진 → 활력징후 측정 → 마무리 질문)를 온라인으로 옮긴 스마트 증상 문진 · 감별진단 웹 서비스입니다.
결과로 **응급 신호 경고**, **권장 대응 단계**, **감별 후보(최대 4개)**, **권장 진료과·검사**, **의사에게 보여줄 진료 요약지**를 제공합니다.

> ⚠ 진료 전 참고용 도구이며 의학적 진단을 대신하지 않습니다. 응급 상황은 119, 자살 위기는 109.

## 빠르게 실행

```bash
python server.py                 # http://localhost:8765
PORT=9000 python server.py       # 포트 지정 (Render 등 클라우드는 PORT 를 자동 주입)
```

`Newdiagnosis.html` 파일만 브라우저로 열어도 동작합니다(서버 없이 내장 엔진 + 브라우저 저장).

## 파일 구성

| 파일 | 역할 |
|---|---|
| `server.py` | 표준 라이브러리 HTTP 서버, 진단 엔진(기준), SQLite 저장, API |
| `Newdiagnosis.html` | 한 화면 1질문 문진 UI, 내장 진단 엔진(폴백), 결과지·요약지, 기록함 |
| `share/medinavi-share.html` | 서버 없이 공유 링크로 올리는 정적 버전(인쇄 버튼 제외) |
| `tools/` | 지식베이스(`kb_gen.py`)·엔진(`engine.js`)·화면(`template.html`)을 합치는 `build.py` |
| `tests/test_medinavi.py` | 엔진·서버·JS/Python 엔진 일치 테스트 |
| `requirements.txt` | 비어 있음(표준 라이브러리만 사용) |
| `render.yaml` | Render 배포 설정 |

증상·질환·가중치·규칙을 바꾸려면 `tools/kb_gen.py` 를 고친 뒤 `python tools/build.py` 를 실행하세요.
서버와 브라우저가 **같은 지식베이스 블록**(`<script id="medinavi-kb">`)을 읽으므로 규칙이 한 곳에서 관리됩니다.

## 정밀 문진 (맞춤 질문)

기본 문진이 끝나면 엔진이 지금 상위 후보들을 가장 잘 가르는 질문을 하나씩 골라 묻습니다(예 / 아니요 / 잘 모르겠어요, 최소 3개 · 최대 7개).

- 질문 81개와 질환별 가중치는 `tools/kb_gen.py`의 `FOLLOWUPS`에 있습니다. 예 → 가중치만큼 가점, 아니요 → 양수 가중치의 절반 감점.
- 다음 질문은 상위 5개 후보의 가능성 비율로 가중한 "답에 따른 점수 차이"가 가장 큰 질문입니다(`nextQuestion` / `next_question`).
- 결과의 %는 후보끼리 비교한 상대 가능성(softmax)이며, 5% 미만 후보는 숨깁니다.
- 답변은 `answers.followups = {"질문 id": "yes" | "no" | "unsure"}`로 `/api/diagnose`에 함께 보냅니다.

## API

| 메서드 · 경로 | 설명 |
|---|---|
| `GET /`, `/Newdiagnosis.html` | 프론트엔드 |
| `GET /api/bootstrap` | 서버 연결 확인 · 지식베이스 버전 |
| `POST /api/diagnose` | `{user_id, answers}` 검증 → 진단 → 저장 → `{session_id, result}` |
| `GET /api/history?user_id=...` | 내 기록 목록 |
| `GET /api/history/<session_id>` | 기록 상세 |
| `DELETE /api/history/<session_id>` | 기록 삭제 |
| `GET /healthz` | 헬스체크 |

기록 조회·삭제에는 브라우저가 자동으로 만드는 기기 키(`X-MediNavi-Key` 헤더)가 필요합니다. 서버에는 키의 해시만 저장되어, 다른 사람이 같은 사용자 ID를 입력해도 남의 기록을 볼 수 없습니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

명세 5장의 검증 항목(만성 기침 + GGO 시 감기 1순위 방지, Red Flag, 저장·조회·삭제, PORT 기동)과 입력 검증, JS·Python 엔진 결과 일치(node 설치 시)를 확인합니다.

## 배포 (GitHub → Render)

1. 이 폴더를 GitHub 저장소에 푸시합니다.
2. Render → New → Web Service → 저장소 연결 (또는 `render.yaml` Blueprint 사용)
   - Start Command: `python server.py`, Health Check: `/healthz`
3. 무료 플랜의 디스크는 재배포 시 초기화됩니다. 기록을 오래 보관하려면 Persistent Disk 를 붙이고 `MEDINAVI_DB=/var/data/medinavi.db` 를 설정하세요.

환경변수: `PORT`(기본 8765), `MEDINAVI_DB`(DB 경로), `MEDINAVI_RETENTION_DAYS`(기본 180일 후 자동 삭제, 0 이면 보관).

## 건강정보 보호

- 입력값은 화이트리스트로 검증하고, 요청 크기 64KB·자유 서술 1000자로 제한합니다.
- 서버 로그에는 경로만 남고 증상 내용은 기록하지 않습니다.
- CSP·nosniff·frame 차단 등 보안 헤더, API 응답 `Cache-Control: no-store`.
- 실제 서비스로 운영하려면 개인정보 처리방침·동의 절차(민감정보), HTTPS(Render 기본 제공), 의료기기 해당 여부 검토가 필요합니다.
