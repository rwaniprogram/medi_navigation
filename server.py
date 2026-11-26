#!/usr/bin/env python3
"""MediNavi(메디내비) 서버 — Python 3 표준 라이브러리만 사용.

- GET  /, /Newdiagnosis.html          프론트엔드
- GET  /api/bootstrap                  서버 연결 확인 · 메타데이터
- POST /api/diagnose                   문진 검증 → 진단 → SQLite 저장 → 결과 반환
- GET  /api/history?user_id=...        내 기록 목록 (X-MediNavi-Key 헤더 필요)
- GET  /api/history/<session_id>       기록 상세 (X-MediNavi-Key 헤더 필요)
- DELETE /api/history/<session_id>     기록 삭제 (X-MediNavi-Key 헤더 필요)
- GET  /healthz                        헬스체크

임상 지식베이스(증상·질환·규칙)는 Newdiagnosis.html 안의
<script type="application/json" id="medinavi-kb"> 블록 하나를 서버와 브라우저가 함께 읽는다.
진단은 서버 엔진이 기준(source of truth)이며, 서버 미연결 시 브라우저가 같은 규칙의 내장 엔진으로 폴백한다.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "Newdiagnosis.html")
DB_PATH = os.environ.get("MEDINAVI_DB", os.path.join(BASE_DIR, "medinavi.db"))
RETENTION_DAYS = int(os.environ.get("MEDINAVI_RETENTION_DAYS", "180"))  # 0 이면 자동 삭제 안 함
MAX_BODY = 64 * 1024
KST = timezone(timedelta(hours=9))
USER_ID_RE = re.compile(r"^[0-9A-Za-z_\-가-힣]{1,32}$")
KEY_RE = re.compile(r"^[0-9a-f]{32,64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------------------
# Knowledge base (shared with the browser)
# --------------------------------------------------------------------------------------
def load_kb(html_path=HTML_PATH):
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r'<script type="application/json" id="medinavi-kb">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Newdiagnosis.html 에서 medinavi-kb 블록을 찾을 수 없습니다.")
    return json.loads(m.group(1))


# --------------------------------------------------------------------------------------
# Engine — keep in lock-step with the JS engine in Newdiagnosis.html (id="medinavi-engine")
# --------------------------------------------------------------------------------------
LEVEL_RANK = {"routine": 1, "soon": 2, "urgent": 3, "emergency": 4}
TRIAGE = {
    "emergency": ("응급 — 지금 바로 119 또는 응급실", "지금 바로 119에 연락하거나 가까운 응급실을 방문하세요. 이 문진을 마치느라 지체하지 마세요."),
    "urgent": ("우선 진료 — 오늘 중 진료 권장", "오늘 중(늦어도 24시간 이내) 의료기관 진료를 받으세요. 증상이 급격히 나빠지면 119에 연락하세요."),
    "soon": ("외래 진료 예약 권장", "가까운 시일(1~2주) 안에 권장 진료과 외래 진료를 예약하세요. 그 사이 악화되면 더 빨리 진료를 받으세요."),
    "routine": ("경과 관찰 · 필요 시 외래", "증상이 가볍다면 휴식하며 경과를 지켜보세요. 악화되거나 새로운 증상이 생기면 진료를 받으세요."),
}
DISCLAIMER = "본 결과는 의학적 진단이 아니라 진료 전 참고용 정보입니다. 최종 진단과 치료는 반드시 의료진의 진찰을 통해 결정하세요."
FU_ANS = ("yes", "no", "unsure")
FU_KO = {"yes": "예", "no": "아니오", "unsure": "잘 모르겠어요"}
NEG_RE = re.compile(r"[^.,!?\n]{0,6}?(없|아니|않|안 )")


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _fmt(x):
    """Render numbers the way JavaScript's String(x) does for our value ranges."""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _round2(x):
    v = math.floor(x * 100 + 0.5) / 100
    return int(v) if float(v).is_integer() else v


class Engine:
    def __init__(self, kb):
        self.kb = kb
        self.version = kb["version"]
        self.sym_label, self.sym_flag = {}, {}
        for region, items in kb["symptoms"].items():
            for s in items:
                self.sym_label[s["id"]] = s["label"]
                if s.get("flag"):
                    self.sym_flag[s["id"]] = s
        self.fu = kb.get("followups", [])
        self.fu_by_id = {q["id"]: q for q in self.fu}
        self.fuc = kb.get("fuConfig") or {"min": 3, "max": 7, "temp": 3, "topK": 5, "minGain": 0.5, "stopShare": 92}

    # helpers -------------------------------------------------------------------------
    @staticmethod
    def _lab(lst, id_):
        for x in lst:
            if x["id"] == id_:
                return x["label"]
        return id_

    def _dur_label(self, id_):
        for d in self.kb["durations"]:
            if d["id"] == id_:
                return "%s(%s)" % (d["label"], d["desc"])
        return id_

    @staticmethod
    def _ids(lst):
        return [x["id"] for x in lst]

    @staticmethod
    def _text_has(text, word):
        w = word.lower()
        start = 0
        while True:
            i = text.find(w, start)
            if i == -1:
                return False
            if not NEG_RE.match(text[i + len(w): i + len(w) + 8]):
                return True
            start = i + 1

    def extract(self, text):
        t = (text or "").lower()
        hits, seen = [], set()
        for k in self.kb["keywords"]:
            for word in k["words"]:
                if self._text_has(t, word):
                    key = k["kind"] + ":" + k["id"]
                    if key not in seen:
                        seen.add(key)
                        hits.append({"kind": k["kind"], "id": k["id"], "word": word})
                    break
        return hits

    def validate(self, a):
        kb = self.kb
        if not isinstance(a, dict):
            return ["입력 형식이 올바르지 않습니다."]
        errs = []
        if a.get("age") not in self._ids(kb["ages"]):
            errs.append("age")
        if a.get("sex") not in self._ids(kb["sexes"]):
            errs.append("sex")
        region = a.get("region")
        if region not in self._ids(kb["regions"]):
            errs.append("region")
        region_syms = self._ids(kb["symptoms"].get(region, [])) if isinstance(region, str) else []
        syms = a.get("symptoms")
        if not isinstance(syms, list) or len(syms) < 1 or any(s not in region_syms for s in syms):
            errs.append("symptoms")
        if a.get("onset") not in self._ids(kb["onsets"]):
            errs.append("onset")
        if a.get("duration") not in self._ids(kb["durations"]):
            errs.append("duration")
        if a.get("course") not in self._ids(kb["courses"]):
            errs.append("course")
        if a.get("finding") is not None and a.get("finding") not in self._ids(kb["findings"]):
            errs.append("finding")
        for key, lst in (("risks", kb["risks"]), ("chronics", kb["chronics"])):
            v = a.get(key)
            if not isinstance(v, list) or any(x not in self._ids(lst) for x in v):
                errs.append(key)
        if a.get("pregnancy") is not None and a.get("pregnancy") not in self._ids(kb["pregnancy"]):
            errs.append("pregnancy")
        nrs = a.get("nrs")
        if not _num(nrs) or nrs % 1 != 0 or nrs < 0 or nrs > 10:
            errs.append("nrs")
        text = a.get("text")
        if not isinstance(text, str) or len(text) > 1000:
            errs.append("text")
        vitals = a.get("vitals") or {}
        if not isinstance(vitals, dict):
            errs.append("vitals")
            vitals = {}
        for k in ("temp", "hr", "spo2"):
            x = vitals.get(k)
            if x is None:
                continue
            lo, hi = kb["vitalLimits"][k]
            if not _num(x) or x < lo or x > hi:
                errs.append("vitals." + k)
        fu = a.get("followups")
        if fu is not None:
            if not isinstance(fu, dict) or len(fu) > 20 or any(k not in self.fu_by_id or v not in FU_ANS for k, v in fu.items()):
                errs.append("followups")
        return errs

    def _answers_of(self, a):
        fu = a.get("followups")
        if not isinstance(fu, dict):
            return {}
        return {k: v for k, v in fu.items() if k in self.fu_by_id and isinstance(v, str) and v in FU_ANS}

    def analyze(self, a):
        kb = self.kb
        lab = self._lab
        v = a.get("vitals") or {}
        temp, hr, spo2 = v.get("temp"), v.get("hr"), v.get("spo2")
        temp = temp if _num(temp) else None
        hr = hr if _num(hr) else None
        spo2 = spo2 if _num(spo2) else None
        nrs = int(a["nrs"])
        pregnancy = a.get("pregnancy")
        chronics = list(a["chronics"])
        hits = self.extract(a.get("text"))
        sym, text_sym, finding = list(a["symptoms"]), [], []
        risks, auto_risks = list(a["risks"]), []
        if a.get("finding") and a["finding"] != "none":
            finding.append(a["finding"])
        for h in hits:
            if h["kind"] == "sym" and h["id"] not in sym and h["id"] not in text_sym:
                text_sym.append(h["id"])
            if h["kind"] == "finding" and h["id"] not in finding:
                finding.append(h["id"])
            if h["kind"] == "risk" and h["id"] not in risks:
                risks.append(h["id"])
                auto_risks.append(h["id"] + ":text")
        if temp is not None and temp >= 38 and "fever" not in risks:
            risks.append("fever")
            auto_risks.append("fever:temp")
        all_sym = sym + text_sym
        vital = []
        if spo2 is not None and spo2 <= 94:
            vital.append("spo2_low")
        if temp is not None and temp >= 38:
            vital.append("temp_high")
        if hr is not None and (hr >= 120 or hr <= 45):
            vital.append("hr_abnormal")

        # ---- Red flags ----
        flags = []

        def flag(level, code, title, reason):
            flags.append({"level": level, "code": code, "title": title, "reason": reason})

        for id_ in all_sym:
            s = self.sym_flag.get(id_)
            if not s:
                continue
            src = "" if id_ in sym else " (자유 서술에서 언급)"
            flag(s["flag"], "sym:" + id_, s["label"] + src, s["flagReason"])
        if nrs >= 9:
            flag("urgent", "nrs", "NRS %d점의 극심한 통증·불편" % nrs, "견디기 어려운 극심한 통증은 원인과 관계없이 우선 진료가 필요합니다.")
        if spo2 is not None:
            if spo2 <= 92:
                flag("emergency", "spo2", "산소포화도 %s%%" % _fmt(spo2), "산소포화도 92% 이하는 저산소증으로 즉시 응급 평가가 필요합니다(측정 오류가 아닌지 다시 확인하세요).")
            elif spo2 <= 94:
                flag("urgent", "spo2", "산소포화도 %s%%" % _fmt(spo2), "산소포화도 93~94%는 정상보다 낮아 당일 진료가 권장됩니다.")
        if temp is not None:
            if temp >= 40:
                flag("urgent", "temp", "체온 %s℃ 고열" % _fmt(temp), "40℃ 이상의 고열은 당일 진료가 필요합니다.")
            elif temp < 35:
                flag("urgent", "temp", "체온 %s℃ 저체온" % _fmt(temp), "35℃ 미만의 저체온은 당일 진료가 필요합니다.")
        if hr is not None and (hr >= 130 or hr <= 40):
            flag("urgent", "hr", "맥박 %s회/분" % _fmt(hr), "안정 시 맥박이 130회 이상이거나 40회 이하이면 당일 평가가 필요합니다.")
        if "fever" in risks and "neck_stiff" in all_sym:
            flag("emergency", "combo:meningitis", "발열 + 목 경직", "발열과 목 경직이 함께 있으면 뇌수막염 등 응급 질환을 배제해야 합니다.")
        if "fever" in risks and "immuno" in chronics:
            flag("urgent", "combo:immuno_fever", "면역 저하 상태의 발열", "항암치료·면역억제제 사용 중 발열은 중증 감염 위험이 높아 당일 진료가 필요합니다.")
        if pregnancy in ("yes", "unknown") and any(x in all_sym for x in ("rlq", "ruq", "severe_abd", "epigastric")):
            flag("urgent", "combo:preg_abd", "임신 가능성이 있는 상태의 복통", "가임기 복통은 자궁외 임신 등 응급 질환 배제를 위해 임신 검사와 당일 진료가 권장됩니다.")
        flags.sort(key=lambda f: -LEVEL_RANK[f["level"]])  # stable, same as JS

        # ---- Scoring ----
        ctx = {"dur": [a["duration"]], "finding": finding, "sym": all_sym, "risk": risks, "age": [a["age"]],
               "sex": [a["sex"]], "onset": [a["onset"]], "course": [a["course"]], "chronic": chronics,
               "vital": vital, "preg": [pregnancy] if pregnancy else []}

        def rule_match(cond):
            return all(any(x in ctx[k] for x in vals) for k, vals in cond.items())

        text = (a.get("text") or "").lower()
        fua = self._answers_of(a)
        cands = []
        for idx, c in enumerate(kb["conditions"]):
            if c.get("sexOnly") and c["sexOnly"] != a["sex"]:
                continue
            if c.get("requires") and not any(x in all_sym for x in c["requires"]):
                continue
            score, evidence, reasons = c["base"], 0, []
            for id_, w in c["sym"].items():
                if id_ in sym:
                    score += w
                    evidence += 1 if w > 0 else 0
                    reasons.append("증상: " + self.sym_label[id_])
                elif id_ in text_sym:
                    score += w * 0.5
                    evidence += 1 if w > 0 else 0
                    reasons.append("자유 서술: " + self.sym_label[id_])
            for id_, w in (c.get("find") or {}).items():
                if id_ in finding:
                    score += w
                    evidence += 1
                    reasons.append("검진 소견: " + lab(kb["findings"], id_))
            if evidence == 0:
                continue

            def apply(mp, values, prefix, labeler):
                nonlocal score
                if not mp:
                    return
                for val in values:
                    if val is None or val not in mp:
                        continue
                    w = mp[val]
                    score += w
                    reasons.append(prefix + labeler(val) + (" (불일치 · 감점)" if w < 0 else ""))

            apply(c.get("dur"), [a["duration"]], "지속 기간: ", self._dur_label)
            apply(c.get("onset"), [a["onset"]], "발병 양상: ", lambda x: lab(kb["onsets"], x))
            apply(c.get("course"), [a["course"]], "경과: ", lambda x: lab(kb["courses"], x))
            apply(c.get("risk"), risks, "동반 위험 인자: ", lambda x: lab(kb["risks"], x))
            apply(c.get("chronic"), chronics, "기저질환: ", lambda x: lab(kb["chronics"], x))
            apply(c.get("age"), [a["age"]], "연령대: ", lambda x: lab(kb["ages"], x))
            apply(c.get("sex"), [a["sex"]], "성별: ", lambda x: lab(kb["sexes"], x))
            apply(c.get("preg"), [pregnancy] if pregnancy else [], "임신 여부: ", lambda x: lab(kb["pregnancy"], x))
            for word, w in (c.get("kw") or {}).items():
                if self._text_has(text, word):
                    score += w
                    reasons.append("자유 서술 키워드: '%s'" % word)
            for qid, w in (c.get("fu") or {}).items():
                ans = fua.get(qid)
                if ans == "yes":
                    score += w
                    reasons.append("정밀 문진: " + self.fu_by_id[qid]["q"] + " → 예" + (" (불일치 · 감점)" if w < 0 else ""))
                elif ans == "no" and w > 0:
                    score -= w * 0.5
                    reasons.append("정밀 문진: " + self.fu_by_id[qid]["q"] + " → 아니오 (감점)")
            if c["region"] == a["region"]:
                score += 1
            for r in kb["adjust"]:
                if c["id"] not in r["target"] or not rule_match(r["if"]):
                    continue
                if r.get("add"):
                    score += r["add"]
                if r.get("mult") and score > 0:
                    score *= r["mult"]
                up = (r.get("add") and r["add"] > 0) or (r.get("mult") and r["mult"] > 1)
                reasons.append(("가점 규칙: " if up else "감점 규칙: ") + r["reason"])
            score = _round2(score)
            if score <= 0:
                continue
            cands.append({"idx": idx, "c": c, "score": score, "reasons": reasons})
        cands.sort(key=lambda x: (-x["score"], x["idx"]))
        es, total = self._exp_shares(cands)
        for x, e in zip(cands, es):
            x["e"] = e
            x["pct"] = int(math.floor(100 * e / total + 0.5))
        return {"sym": sym, "text_sym": text_sym, "finding": finding, "risks": risks, "auto_risks": auto_risks,
                "hits": hits, "flags": flags, "temp": temp, "hr": hr, "spo2": spo2, "fua": fua, "scored": cands,
                "nrs": nrs, "pregnancy": pregnancy, "chronics": chronics}

    def _exp_shares(self, lst):
        if not lst:
            return [], 1
        mx = lst[0]["score"]
        es, total = [], 0
        for x in lst:
            v = math.exp((x["score"] - mx) / self.fuc["temp"])
            es.append(v)
            total += v
        return es, total

    def next_question(self, a):
        an = self.analyze(a)
        lst, asked, cfg = an["scored"], len(an["fua"]), self.fuc
        if not lst or asked >= cfg["max"] or (asked >= cfg["min"] and lst[0]["pct"] >= cfg["stopShare"]):
            return None
        top = lst[:cfg["topK"]]
        tsum = 0
        for x in top:
            tsum += x["e"]
        best, best_gain = None, 0
        for q in self.fu:
            if q["id"] in an["fua"]:
                continue
            if q.get("sex") and q["sex"] != a["sex"]:
                continue
            ws = [(x["c"].get("fu") or {}).get(q["id"], 0) for x in top]
            if not any(w != 0 for w in ws):
                continue
            mean = 0
            for x, w in zip(top, ws):
                mean += x["e"] / tsum * w
            gain = 0
            for x, w in zip(top, ws):
                gain += x["e"] / tsum * abs(w - mean)
            gain = math.floor(gain * 10000 + 0.5) / 10000
            if gain > best_gain:
                best_gain, best = gain, q
        if not best or best_gain < cfg["minGain"]:
            return None
        return {"id": best["id"], "q": best["q"], "hint": best.get("hint"), "asked": asked, "max": cfg["max"],
                "leaders": [{"id": x["c"]["id"], "name": x["c"]["name"], "pct": x["pct"]} for x in top[:3]]}

    def diagnose(self, a, now):
        kb = self.kb
        lab = self._lab
        an = self.analyze(a)
        sym, text_sym, finding, risks, auto_risks = an["sym"], an["text_sym"], an["finding"], an["risks"], an["auto_risks"]
        hits, flags, temp, hr, spo2 = an["hits"], an["flags"], an["temp"], an["hr"], an["spo2"]
        nrs, pregnancy, chronics = an["nrs"], an["pregnancy"], an["chronics"]
        cands = []
        for i, x in enumerate(an["scored"][:4]):
            if i > 0 and x["pct"] < 5:
                continue
            c = x["c"]
            cands.append({"id": c["id"], "name": c["name"], "score": x["score"], "pct": x["pct"],
                          "fit": int(math.floor(95 * (1 - math.exp(-x["score"] / 9)) + 0.5)),
                          "desc": c["desc"], "dept": c["dept"], "tests": list(c["tests"]),
                          "reasons": x["reasons"], "urgency": c["urgency"]})

        # ---- Triage ----
        level = "routine"
        for f in flags:
            if LEVEL_RANK[f["level"]] > LEVEL_RANK[level]:
                level = f["level"]
        if cands and cands[0]["fit"] >= 60:  # the leading candidate's urgency (emergency → urgent; true emergencies come from red flags)
            u = "urgent" if cands[0]["urgency"] == "emergency" else cands[0]["urgency"]
            if LEVEL_RANK[u] > LEVEL_RANK[level]:
                level = u
        if (a["duration"] == "chronic" or nrs >= 7 or finding or "weight_loss" in risks) and LEVEL_RANK[level] < LEVEL_RANK["soon"]:
            level = "soon"
        triage = {"level": level, "title": TRIAGE[level][0], "message": TRIAGE[level][1]}

        summary = {
            "age": lab(kb["ages"], a["age"]), "sex": lab(kb["sexes"], a["sex"]), "region": lab(kb["regions"], a["region"]),
            "symptoms": [self.sym_label[i] for i in sym], "text_symptoms": [self.sym_label[i] for i in text_sym],
            "onset": lab(kb["onsets"], a["onset"]), "duration": self._dur_label(a["duration"]),
            "course": lab(kb["courses"], a["course"]),
            "finding": lab(kb["findings"], a["finding"]) if a.get("finding") else "해당 없음(질문 생략)",
            "text_findings": [lab(kb["findings"], f) for f in finding if f != a.get("finding")],
            "risks": [lab(kb["risks"], i) for i in risks], "auto_risks": auto_risks,
            "chronics": [lab(kb["chronics"], i) for i in chronics],
            "pregnancy": lab(kb["pregnancy"], pregnancy) if pregnancy else None,
            "vitals": {"temp": temp, "hr": hr, "spo2": spo2},
            "nrs": nrs, "text": a.get("text") or "", "keywords": [h["word"] for h in hits],
            "followups": [{"q": q["q"], "a": FU_KO[an["fua"][q["id"]]]} for q in self.fu if q["id"] in an["fua"]],
        }
        result = {"kb_version": self.version, "created_at": now, "triage": triage, "red_flags": flags,
                  "candidates": cands, "summary": summary, "disclaimer": DISCLAIMER}
        result["note"] = self._note(result)
        return result

    @staticmethod
    def _note(r):
        s = r["summary"]
        vt = s["vitals"]

        def lst(x):
            return ", ".join(x) if x else "없음"

        vit = " · ".join([
            "체온 " + (_fmt(vt["temp"]) + "℃" if vt["temp"] is not None else "미측정"),
            "맥박 " + (_fmt(vt["hr"]) + "회/분" if vt["hr"] is not None else "미측정"),
            "SpO₂ " + (_fmt(vt["spo2"]) + "%" if vt["spo2"] is not None else "미측정"),
        ])
        L = ["[메디내비 진료 요약 노트]", "작성: " + r["created_at"], ""]
        L.append("■ 기본 정보: %s / %s%s" % (s["age"], s["sex"], (" / 임신: " + s["pregnancy"]) if s["pregnancy"] else ""))
        L.append("■ 주요 불편 영역: " + s["region"])
        L.append("■ 주요 증상: " + lst(s["symptoms"]) + ((" (서술에서 추가: " + ", ".join(s["text_symptoms"]) + ")") if s["text_symptoms"] else ""))
        L.append("■ 발병 · 경과: %s / %s / %s" % (s["onset"], s["duration"], s["course"]))
        L.append("■ 통증·불편 강도: NRS %d/10" % s["nrs"])
        L.append("■ 활력징후(자가 측정): " + vit)
        L.append("■ 건강검진 폐 영상 소견: " + s["finding"] + ((" (서술에서 추가: " + ", ".join(s["text_findings"]) + ")") if s["text_findings"] else ""))
        L.append("■ 동반 위험 인자: " + lst(s["risks"]))
        L.append("■ 기저질환 · 복용약: " + lst(s["chronics"]))
        L.append("■ 추가 서술: " + (s["text"] if s["text"] else "없음"))
        if s.get("followups"):
            L.append("■ 정밀 문진:")
            for f in s["followups"]:
                L.append("  - " + f["q"] + " → " + f["a"])
        L.append("")
        if r["red_flags"]:
            fl = "; ".join("[%s] %s" % ("응급" if f["level"] == "emergency" else "우선", f["title"]) for f in r["red_flags"])
        else:
            fl = "해당 없음"
        L.append("■ 응급 징후 선별: " + fl)
        L.append("■ 권장 대응: " + r["triage"]["title"])
        if r["candidates"]:
            c = r["candidates"][0]
            L.append("■ 1순위 감별 후보: %s (상대 가능성 %d%%)" % (c["name"], c["pct"]))
            L.append("■ 권장 진료과: " + c["dept"])
            L.append("■ 참고 검사: " + ", ".join(c["tests"]))
            if len(r["candidates"]) > 1:
                L.append("■ 기타 감별 후보: " + ", ".join("%s %d%%" % (x["name"], x["pct"]) for x in r["candidates"][1:]))
        else:
            L.append("■ 감별 후보: 입력 정보만으로 특정하기 어려움 — 진찰 필요")
        L.append("")
        L.append("※ " + r["disclaimer"])
        return "\n".join(L)


# --------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------
_db_lock = threading.Lock()


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with _db_lock, db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                owner_hash   TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                kb_version   TEXT NOT NULL,
                region       TEXT NOT NULL,
                triage_level TEXT NOT NULL,
                top_name     TEXT,
                top_pct      INTEGER,
                answers_json TEXT NOT NULL,
                result_json  TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(user_id, owner_hash, created_at)")
        if RETENTION_DAYS > 0:
            cutoff = (datetime.now(KST) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))


def owner_hash(key):
    return hashlib.sha256(("medinavi:" + key).encode()).hexdigest()


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
ENGINE = None
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MediNavi/1.0"
    sys_version = ""

    # Never write request paths/bodies (health data) to logs beyond method + route.
    def log_message(self, fmt, *args):
        route = urlparse(self.path).path
        if route.startswith("/api/history/"):
            route = "/api/history/<id>"
        sys.stderr.write("%s %s %s\n" % (self.log_date_time_string(), self.command, route))

    def _send(self, status, body, ctype="application/json; charset=utf-8", cache="no-store"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message, details=None):
        payload = {"ok": False, "error": message}
        if details:
            payload["details"] = details
        self._send(status, payload)

    def _owner(self):
        key = (self.headers.get("X-MediNavi-Key") or "").strip().lower()
        if not KEY_RE.match(key):
            return None
        return owner_hash(key)

    # ---- GET ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/Newdiagnosis.html", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", "no-cache")
            except OSError:
                return self._error(500, "프론트엔드 파일을 읽을 수 없습니다.")
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path == "/api/bootstrap":
            return self._send(200, {
                "ok": True, "service": "MediNavi", "server_time": datetime.now(KST).isoformat(timespec="seconds"),
                "kb_version": ENGINE.version, "storage": "sqlite", "retention_days": RETENTION_DAYS,
                "engine": "server",
            })
        if path == "/api/history":
            owner = self._owner()
            if not owner:
                return self._error(401, "기기 키(X-MediNavi-Key)가 필요합니다.")
            user_id = (parse_qs(u.query).get("user_id") or ["guest"])[0]
            if not USER_ID_RE.match(user_id):
                return self._error(400, "사용자 ID 형식이 올바르지 않습니다.")
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT session_id, created_at, region, triage_level, top_name, top_pct FROM sessions "
                    "WHERE user_id = ? AND owner_hash = ? ORDER BY created_at DESC LIMIT 100", (user_id, owner)).fetchall()
            return self._send(200, {"ok": True, "items": [dict(r) for r in rows]})
        m = re.match(r"^/api/history/([0-9a-f]{32})$", path)
        if m:
            owner = self._owner()
            if not owner:
                return self._error(401, "기기 키(X-MediNavi-Key)가 필요합니다.")
            with db_connect() as conn:
                row = conn.execute("SELECT * FROM sessions WHERE session_id = ? AND owner_hash = ?", (m.group(1), owner)).fetchone()
            if not row:
                return self._error(404, "기록을 찾을 수 없습니다.")
            return self._send(200, {"ok": True, "session_id": row["session_id"], "user_id": row["user_id"],
                                    "answers": json.loads(row["answers_json"]), "result": json.loads(row["result_json"])})
        return self._error(404, "Not found")

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/diagnose":
            return self._error(404, "Not found")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._error(415, "Content-Type 은 application/json 이어야 합니다.")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._error(400, "Content-Length 가 올바르지 않습니다.")
        if length <= 0 or length > MAX_BODY:
            return self._error(413, "요청 크기가 허용 범위를 벗어났습니다.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(400, "JSON 형식이 올바르지 않습니다.")
        if not isinstance(payload, dict):
            return self._error(400, "JSON 객체가 필요합니다.")
        answers = payload.get("answers")
        user_id = payload.get("user_id") or "guest"
        if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
            return self._error(400, "사용자 ID는 한글·영문·숫자·_- 1~32자여야 합니다.")
        errs = ENGINE.validate(answers)
        if errs:
            return self._error(422, "문진 입력값이 올바르지 않습니다.", errs)
        now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        result = ENGINE.diagnose(answers, now)
        owner = self._owner()
        session_id = None
        if owner:  # store only when the browser identifies itself with its device key
            session_id = uuid.uuid4().hex
            top = result["candidates"][0] if result["candidates"] else None
            with _db_lock, db_connect() as conn:
                conn.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, user_id, owner, datetime.now(KST).isoformat(timespec="seconds"), ENGINE.version,
                     answers["region"], result["triage"]["level"], top["name"] if top else None,
                     top["pct"] if top else None, json.dumps(answers, ensure_ascii=False),
                     json.dumps(result, ensure_ascii=False)))
        return self._send(200, {"ok": True, "session_id": session_id, "stored": bool(session_id), "result": result})

    # ---- DELETE ----
    def do_DELETE(self):
        m = re.match(r"^/api/history/([0-9a-f]{32})$", urlparse(self.path).path)
        if not m:
            return self._error(404, "Not found")
        owner = self._owner()
        if not owner:
            return self._error(401, "기기 키(X-MediNavi-Key)가 필요합니다.")
        with _db_lock, db_connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE session_id = ? AND owner_hash = ?", (m.group(1), owner))
        if cur.rowcount == 0:
            return self._error(404, "기록을 찾을 수 없습니다.")
        return self._send(200, {"ok": True})


def main():
    global ENGINE
    ENGINE = Engine(load_kb())
    db_init()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("MediNavi server: http://%s:%d  (kb %s, db %s)" % (HOST, PORT, ENGINE.version, DB_PATH), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
