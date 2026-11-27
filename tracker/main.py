#!/usr/bin/env python3
"""장 초반 주가 추적 (GitHub Actions)
- 08:30 KST 시작 → 노션 아침 리포트에서 '오늘의 섹터' 종목 확보
- 09:03 한국경제TV 해외고수기법·매수유망패턴 포착 종목 1회 수집(이후 추가 없음)
- 09:05~10:10 5분 간격 KIS 시세 조회 → 텔레그램 발송
- results/YYYYMMDD.json 저장(워크플로가 커밋) → Claude가 읽어 노션 일지로 정리
필요한 Secrets: KIS_APP_KEY, KIS_APP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NOTION_TOKEN
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTION_PARENT = "424111d4-4844-813c-896a-dd21ca73fd33"
KIS_BASE = "https://openapi.koreainvestment.com:9443"
WOWTV_PAGES = [
    ("해외고수기법", "https://www.wowtv.co.kr/Finance/Trading/OverseasTechnique?menuSeq=571",
     ["니콜라스 이론", "윌리암오닐신고", "증권왕 로브단타", "사께다삼천"]),
    ("매수유망패턴", "https://www.wowtv.co.kr/Finance/Trading/BuyingProspects?menuSeq=569",
     ["급등패턴", "상승초기형", "바닥탈출 후 상승패턴"]),
]
CAPTURE_AT = (9, 3)
TICK_START, TICK_END, INTERVAL_MIN = (9, 5), (10, 10), 5
MAX_SECTOR, MAX_CAPTURED = 8, 10
HOLIDAYS = {
    "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-03", "2027-05-05", "2027-05-13",
    "2027-07-19", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16", "2027-10-04", "2027-10-11",
    "2027-12-27", "2027-12-31",
}
WEEKDAY_KO = "월화수목금토일"

now = lambda: dt.datetime.now(KST)
LOG = {"errors": [], "notes": []}


def err(msg):
    print("ERROR:", msg, flush=True)
    LOG["errors"].append(f"{now():%H:%M:%S} {msg}")


def sleep_until(h, m, s=0):
    t = now().replace(hour=h, minute=m, second=s, microsecond=0)
    while (left := (t - now()).total_seconds()) > 0:
        time.sleep(min(left, 30))


# ---------------- 종목 마스터(이름→코드) ----------------
def load_master():
    """KIS 종목 마스터 파일(코스피·코스닥)에서 {종목명: 코드}"""
    m = {}
    for url, tail in (("https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip", 228),
                      ("https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip", 222)):
        try:
            z = zipfile.ZipFile(io.BytesIO(requests.get(url, timeout=30).content))
            raw = z.read(z.namelist()[0]).decode("cp949", errors="ignore")
            for line in raw.splitlines():
                head = line[:len(line) - tail]
                code, name = head[0:9].strip(), head[21:].strip()
                if len(code) == 6 and code.isdigit() and name:
                    m.setdefault(name, code)
        except Exception as e:
            err(f"종목 마스터 실패 {url}: {e}")
    return m


def to_code(name, master):
    name = name.strip()
    if re.fullmatch(r"\d{6}", name):
        return name
    if name in master:
        return master[name]
    key = name.replace(" ", "")
    for n, c in master.items():
        if n.replace(" ", "") == key:
            return c
    return None


# ---------------- 노션: 오늘의 섹터 종목 ----------------
def notion_get(path, token):
    r = requests.get("https://api.notion.com/v1/" + path, timeout=30,
                     headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"})
    r.raise_for_status()
    return r.json()


def block_children(bid, token):
    out, cur = [], None
    while True:
        j = notion_get(f"blocks/{bid}/children?page_size=100" + (f"&start_cursor={cur}" if cur else ""), token)
        out += j["results"]
        if not j.get("has_more"):
            return out
        cur = j["next_cursor"]


def block_text(b):
    data = b.get(b["type"], {})
    rt = data.get("rich_text") or data.get("text") or []
    txt = "".join(x.get("plain_text", "") for x in rt) if isinstance(rt, list) else ""
    if b["type"] == "table_row":
        txt = " | ".join("".join(x.get("plain_text", "") for x in cell) for cell in data.get("cells", []))
    return txt


def notion_post(path, token, body):
    r = requests.post("https://api.notion.com/v1/" + path, json=body, timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"})
    r.raise_for_status()
    return r.json()


def page_title(p):
    for v in p.get("properties", {}).values():
        if v.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in v["title"]).strip()
    return ""


def find_today_page(title, token):
    """1) 장전 리포트 하위 목록에서 찾기 2) 실패하면 검색 API로 찾기 3) 그래도 없으면 진단 정보 기록"""
    try:
        for b in block_children(NOTION_PARENT, token):
            if b["type"] == "child_page" and b["child_page"]["title"].strip() == title:
                return b["id"]
    except Exception as e:
        err(f"노션 상위 페이지 읽기 실패: {e}")
    try:
        res = notion_post("search", token, {"query": title, "filter": {"value": "page", "property": "object"}})
        for p in res.get("results", []):
            if page_title(p) == title:
                return p["id"]
        me = notion_get("users/me", token)
        seen = notion_post("search", token, {"page_size": 10, "filter": {"value": "page", "property": "object"}})
        LOG["notes"].append("노션 진단: 통합 워크스페이스=" + str(me.get("bot", {}).get("workspace_name"))
                            + " / 접근 가능한 페이지 예시=" + ", ".join(page_title(p) for p in seen.get("results", []))[:200])
    except Exception as e:
        err(f"노션 검색 실패: {e}")
    return None


def sector_stocks(master):
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        err("NOTION_TOKEN 없음 → 섹터 종목 생략")
        return []
    d = now()
    title = f"{d:%Y.%m.%d}({WEEKDAY_KO[d.weekday()]}) 장전 리포트"
    try:
        page_id = find_today_page(title, token)
        if not page_id:
            LOG["notes"].append(f"오늘 리포트 없음 또는 접근 불가({title})")
            return []
        lines, stack = [], [page_id]
        while stack:  # 하위 블록까지 펼쳐 읽기(콜아웃·토글 포함)
            for b in block_children(stack.pop(0), token):
                lines.append((b["type"], block_text(b)))
                if b.get("has_children") and b["type"] not in ("child_page", "child_database"):
                    stack.append(b["id"])
    except Exception as e:
        err(f"노션 읽기 실패: {e}")
        return []
    # 1순위: '추적 대상: 005930, 000660' 줄
    for _, t in lines:
        if "추적 대상" in t:
            codes = re.findall(r"\b\d{6}\b", t)
            if codes:
                return [{"code": c, "name": None, "src": "섹터"} for c in dict.fromkeys(codes)][:MAX_SECTOR]
    # 2순위: '06 오늘의 섹터' 구간의 '대장:'·'종목:' 이름
    names, on = [], False
    for typ, t in lines:
        if typ.startswith("heading") and "오늘의 섹터" in t:
            on = True
            continue
        if on and typ.startswith("heading_3") and re.match(r"0[7-9]", t.strip()):
            break
        if on:
            for seg in re.findall(r"(?:평소 대장|최근 대장)\s*:\s*([^/(]+)", t):
                names += [x.strip() for x in seg.split(",") if x.strip()]
    out = []
    for n in dict.fromkeys(names):
        c = to_code(n, master)
        if c:
            out.append({"code": c, "name": n, "src": "섹터"})
        else:
            LOG["notes"].append(f"섹터 종목 코드 미확인: {n}")
    return out[:MAX_SECTOR]


# ---------------- 한국경제TV 포착 종목 (09:03 1회) ----------------
def capture_wowtv(master):
    from playwright.sync_api import sync_playwright
    found, raw = [], {}
    os.makedirs(os.path.join(ROOT, "results", "debug"), exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1400, "height": 2000}, locale="ko-KR")
        for page_name, url, tabs in WOWTV_PAGES:
            try:
                pg.goto(url, wait_until="networkidle", timeout=45000)
            except Exception as e:
                err(f"{page_name} 열기 실패: {e}")
                continue
            for tab in tabs:
                try:
                    pg.get_by_text(tab, exact=True).first.click(timeout=8000)
                    pg.wait_for_timeout(3000)
                except Exception as e:
                    err(f"{page_name}/{tab} 탭 실패: {e}")
                    continue
                # 매수유망패턴처럼 탭 안에 세부 조건(라디오 버튼)이 있으면 조건마다 따로 읽는다
                radios = [r for r in pg.query_selector_all("input[type=radio]")  # 숨겨진 커스텀 라디오도 포함(보이는 영역 안의 것만)
                          if r.evaluate("e => !!((e.closest('label,li,div') || e.parentElement).offsetParent)")]
                conds = radios if radios else [None]
                for i, rd in enumerate(conds):
                    label = tab
                    try:
                        if rd is not None:
                            txt = rd.evaluate("e => (e.closest('label,li,div') || e.parentElement).innerText || ''")
                            label = f"{tab}/{(txt.strip().splitlines() or ['조건' + str(i + 1)])[0][:30]}"
                            rd.evaluate("e => { e.click(); e.dispatchEvent(new Event('change', {bubbles: true})); }")  # 화면 밖 커스텀 라디오도 클릭
                            pg.wait_for_timeout(3000)
                        rows = pg.eval_on_selector_all(
                            "table tbody tr", "rs => rs.map(r => Array.from(r.cells).map(c => c.innerText.trim()))")
                    except Exception as e:
                        err(f"{page_name}/{label} 읽기 실패: {e}")
                        rows = []
                    raw[f"{page_name}/{label}"] = rows
                    for cells in rows:
                        code = next((re.search(r"\b(\d{6})\b", c).group(1) for c in cells if re.search(r"\b\d{6}\b", c)), None)
                        name = next((c for c in cells if c and re.search(r"[가-힣A-Za-z]", c) and len(c) <= 20), None)
                        code = code or (to_code(name, master) if name else None)
                        if code:
                            found.append({"code": code, "name": name, "src": tab})
            safe = re.sub(r"\W", "_", page_name)
            pg.screenshot(path=os.path.join(ROOT, "results", "debug", f"{now():%Y%m%d}_{safe}.png"), full_page=True)
        br.close()
    LOG["wowtv_raw"] = raw
    # 탭별로 번갈아 뽑아 최대 10종목
    by_tab, picked, seen = {}, [], set()
    for f in found:
        by_tab.setdefault(f["src"], []).append(f)
    while len(picked) < MAX_CAPTURED and any(by_tab.values()):
        for t in list(by_tab):
            if len(picked) >= MAX_CAPTURED:
                break
            if by_tab[t]:
                f = by_tab[t].pop(0)
                if f["code"] not in seen:
                    seen.add(f["code"])
                    picked.append(f)
    if not picked:
        LOG["notes"].append("포착 종목 0개(목록 구조 확인 필요: results/debug 스크린샷·wowtv_raw 참고)")
    return picked


# ---------------- KIS 시세 ----------------
class KIS:
    def __init__(self):
        self.key, self.secret = os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"]
        r = requests.post(KIS_BASE + "/oauth2/tokenP", timeout=30,
                          json={"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        r.raise_for_status()
        self.token = r.json()["access_token"]

    def quote(self, code):
        for attempt in range(2):
            r = requests.get(KIS_BASE + "/uapi/domestic-stock/v1/quotations/inquire-price", timeout=15,
                             headers={"authorization": f"Bearer {self.token}", "appkey": self.key,
                                      "appsecret": self.secret, "tr_id": "FHKST01010100", "custtype": "P"},
                             params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
            j = r.json()
            if j.get("rt_cd") == "0":
                o = j["output"]
                return {"price": int(o["stck_prpr"]), "chg": float(o["prdy_ctrt"]), "open": int(o["stck_oprc"])}
            time.sleep(1)
        raise RuntimeError(j.get("msg1", "KIS 오류"))


# ---------------- 텔레그램 ----------------
def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        err("텔레그램 Secrets 없음")
        return False
    for _ in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                              data={"chat_id": chat, "text": text})
            if r.ok:
                return True
        except Exception as e:
            err(f"텔레그램 실패: {e}")
        time.sleep(2)
    return False


def fmt(x):
    return f"{x:+.2f}"


def save(state):
    path = os.path.join(ROOT, "results", f"{now():%Y%m%d}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state.update({"errors": LOG["errors"], "notes": LOG["notes"], "wowtv_raw": LOG.get("wowtv_raw", {})})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    d = now()
    if d.weekday() >= 5 or d.strftime("%Y-%m-%d") in HOLIDAYS:
        print("휴장일 → 종료")
        return
    state = {"date": d.strftime("%Y-%m-%d"), "interval_min": INTERVAL_MIN, "sector": [], "captured": [],
             "ticks": [], "telegram_ok": 0, "telegram_fail": 0}
    master = load_master()
    names = {v: k for k, v in master.items()}
    state["sector"] = sector_stocks(master)
    kis = KIS()

    sleep_until(*CAPTURE_AT)
    try:
        cap = capture_wowtv(master)
    except Exception as e:
        err(f"포착 수집 실패: {e}")
        cap = []
    sec_codes = {s["code"] for s in state["sector"]}
    for c in cap:
        if c["code"] in sec_codes:
            for s in state["sector"]:
                if s["code"] == c["code"]:
                    s["src"] += f"·{c['src']}"
        else:
            state["captured"].append(c)
    for s in state["sector"] + state["captured"]:
        s["name"] = s.get("name") or names.get(s["code"], s["code"])
    save(state)

    t = now().replace(hour=TICK_START[0], minute=TICK_START[1], second=0, microsecond=0)
    end = now().replace(hour=TICK_END[0], minute=TICK_END[1], second=0, microsecond=0)
    step = INTERVAL_MIN
    first = True
    while t <= end:
        if (now() - t).total_seconds() > 120:  # 늦게 시작해 이미 지난 회차는 건너뜀
            LOG["notes"].append(f"{t:%H:%M} 회차 건너뜀(실행 지연)")
            t += dt.timedelta(minutes=step)
            continue
        sleep_until(t.hour, t.minute)
        tick = {"time": f"{t:%H:%M}", "quotes": {}}
        for s in state["sector"] + state["captured"]:
            try:
                tick["quotes"][s["code"]] = kis.quote(s["code"])
            except Exception as e:
                err(f"{t:%H:%M} {s['name']} 시세 실패: {e}")
            time.sleep(0.15)
        state["ticks"].append(tick)
        lines = [f"[추적 {t:%H:%M}] 전일 대비 %"]
        for label, group in (("섹터", state["sector"]), ("포착(09:03)", state["captured"])):
            if not group:
                continue
            lines.append(f"■ {label}")
            for s in group:
                q = tick["quotes"].get(s["code"])
                tag = f" ({s['src']})" if first and label.startswith("포착") else ""
                lines.append(f"{s['name']} {fmt(q['chg']) if q else '—'}{tag}")
        if first and LOG["notes"]:
            lines.append("※ " + " / ".join(LOG["notes"])[:300])
        ok = telegram("\n".join(lines))
        state["telegram_ok" if ok else "telegram_fail"] += 1
        save(state)
        first = False
        t += dt.timedelta(minutes=step)
    save(state)
    print("완료", json.dumps({k: state[k] for k in ("telegram_ok", "telegram_fail")}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err(f"중단: {e}")
        telegram(f"[추적] 오류로 중단: {e}")
        sys.exit(1)
