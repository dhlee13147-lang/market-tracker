# 장 초반 주가 추적 (GitHub Actions → 텔레그램)

평일 09:03에 한국경제TV 해외고수기법·매수유망패턴 포착 종목을 1회 수집하고, 아침 장전 리포트(노션)의 '오늘의 섹터' 종목과 함께 09:05~10:10 5분 간격으로 KIS 시세를 텔레그램에 보냅니다. 결과는 `results/YYYYMMDD.json`으로 커밋되고, Claude가 이를 읽어 노션 리포트에 일지로 정리합니다. 매매 기능은 없습니다.

## 설정 순서
1. 이 폴더 내용을 새 **공개** 저장소에 올립니다(예: `premarket-tracker`).
2. 저장소 Settings → Secrets and variables → Actions → New repository secret 에 아래 5개를 넣습니다.

| 이름 | 값 |
|---|---|
| `KIS_APP_KEY` / `KIS_APP_SECRET` | 한국투자증권 KIS Developers에서 발급한 실전 앱키·시크릿 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 @BotFather 로 만든 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 봇에게 메시지를 한 번 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 에서 확인한 chat.id |
| `NOTION_TOKEN` | notion.so/my-integrations 에서 만든 내부 통합 토큰. **'장전 리포트' 페이지 ⋯ → 연결(Connections)에 이 통합을 추가**해야 읽을 수 있습니다 |

3. Actions 탭에서 워크플로를 활성화하고, **Run workflow**로 한 번 수동 실행해 봅니다(장중이 아니면 시세 회차는 지난 시각이라 건너뛰고, 포착 수집·노션 읽기·텔레그램 연결만 확인됩니다).
4. 첫 실행 뒤 `results/debug/*.png` 스크린샷과 `results/YYYYMMDD.json`의 `wowtv_raw`를 확인합니다. 한국경제TV 목록 구조가 예상과 다르면 `capture_wowtv()`의 선택자를 조정해야 합니다.

## 동작
- 시작: 평일 08:30 KST(깃헙 예약 지연 대비). 휴장일(목록 내장)·주말은 바로 종료.
- 섹터 종목: 오늘 리포트 페이지에서 `추적 대상: 005930, ...` 줄을 먼저 찾고, 없으면 '06 오늘의 섹터' 구간의 '평소 대장·최근 대장' 이름을 종목 마스터로 코드 변환(최대 8개).
- 포착 종목: 09:03 1회, 7개 탭에서 번갈아 최대 10개. 이후 새 포착은 추가하지 않음.
- 발송: 09:05~10:10 5분 간격 14회, 전일 대비 등락률. 늦게 시작해 지난 회차는 건너뜀.
- 간격·종목 수는 `tracker/main.py` 상단 상수로 조정합니다.
