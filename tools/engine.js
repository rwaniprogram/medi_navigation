/* MediNavi 감별진단 엔진 (브라우저 내장판). server.py 의 Python 엔진과 같은 규칙·같은 결과를 내도록 유지한다. */
(function (root) {
  "use strict";
  var LEVEL_RANK = { routine: 1, soon: 2, urgent: 3, emergency: 4 };
  var TRIAGE = {
    emergency: { title: "응급 — 지금 바로 119 또는 응급실", message: "지금 바로 119에 연락하거나 가까운 응급실을 방문하세요. 이 문진을 마치느라 지체하지 마세요." },
    urgent: { title: "우선 진료 — 오늘 중 진료 권장", message: "오늘 중(늦어도 24시간 이내) 의료기관 진료를 받으세요. 증상이 급격히 나빠지면 119에 연락하세요." },
    soon: { title: "외래 진료 예약 권장", message: "가까운 시일(1~2주) 안에 권장 진료과 외래 진료를 예약하세요. 그 사이 악화되면 더 빨리 진료를 받으세요." },
    routine: { title: "경과 관찰 · 필요 시 외래", message: "증상이 가볍다면 휴식하며 경과를 지켜보세요. 악화되거나 새로운 증상이 생기면 진료를 받으세요." }
  };
  var DISCLAIMER = "본 결과는 의학적 진단이 아니라 진료 전 참고용 정보입니다. 최종 진단과 치료는 반드시 의료진의 진찰을 통해 결정하세요.";
  var FU_ANS = ["yes", "no", "unsure"], FU_KO = { yes: "예", no: "아니오", unsure: "잘 모르겠어요" };
  var NEG_RE = /^[^.,!?\n]{0,6}?(없|아니|않|안 )/;

  function inArr(arr, v) { return !!arr && arr.indexOf(v) !== -1; }
  function round2(x) { return Math.floor(x * 100 + 0.5) / 100; }

  function create(KB) {
    var symLabel = {}, symFlag = {}, symRegion = {};
    Object.keys(KB.symptoms).forEach(function (r) {
      KB.symptoms[r].forEach(function (s) { symLabel[s.id] = s.label; symRegion[s.id] = r; if (s.flag) symFlag[s.id] = s; });
    });
    function lab(list, id) { for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i].label; return id; }
    function durLabel(id) { for (var i = 0; i < KB.durations.length; i++) if (KB.durations[i].id === id) return KB.durations[i].label + "(" + KB.durations[i].desc + ")"; return id; }
    function idsOf(list) { return list.map(function (x) { return x.id; }); }
    var FU = KB.followups || [], FUC = KB.fuConfig || { min: 3, max: 7, temp: 3, topK: 5, minGain: 0.5, stopShare: 92 }, fuById = {};
    FU.forEach(function (q) { fuById[q.id] = q; });

    function textHas(text, word) {
      var w = word.toLowerCase(), from = 0, i;
      while ((i = text.indexOf(w, from)) !== -1) {
        if (!NEG_RE.test(text.substr(i + w.length, 8))) return true;
        from = i + 1;
      }
      return false;
    }

    function extract(text) {
      var t = (text || "").toLowerCase(), hits = [], seen = {};
      KB.keywords.forEach(function (k) {
        for (var j = 0; j < k.words.length; j++) {
          if (textHas(t, k.words[j])) {
            var key = k.kind + ":" + k.id;
            if (!seen[key]) { seen[key] = 1; hits.push({ kind: k.kind, id: k.id, word: k.words[j] }); }
            break;
          }
        }
      });
      return hits;
    }

    function validate(a) {
      var errs = [];
      if (!a || typeof a !== "object") return ["입력 형식이 올바르지 않습니다."];
      if (!inArr(idsOf(KB.ages), a.age)) errs.push("age");
      if (!inArr(idsOf(KB.sexes), a.sex)) errs.push("sex");
      if (!inArr(idsOf(KB.regions), a.region)) errs.push("region");
      var regionSyms = KB.symptoms[a.region] ? idsOf(KB.symptoms[a.region]) : [];
      if (!Array.isArray(a.symptoms) || a.symptoms.length < 1 || a.symptoms.some(function (s) { return !inArr(regionSyms, s); })) errs.push("symptoms");
      if (!inArr(idsOf(KB.onsets), a.onset)) errs.push("onset");
      if (!inArr(idsOf(KB.durations), a.duration)) errs.push("duration");
      if (!inArr(idsOf(KB.courses), a.course)) errs.push("course");
      if (a.finding !== null && a.finding !== undefined && !inArr(idsOf(KB.findings), a.finding)) errs.push("finding");
      if (!Array.isArray(a.risks) || a.risks.some(function (s) { return !inArr(idsOf(KB.risks), s); })) errs.push("risks");
      if (!Array.isArray(a.chronics) || a.chronics.some(function (s) { return !inArr(idsOf(KB.chronics), s); })) errs.push("chronics");
      if (a.pregnancy !== null && a.pregnancy !== undefined && !inArr(idsOf(KB.pregnancy), a.pregnancy)) errs.push("pregnancy");
      if (typeof a.nrs !== "number" || a.nrs % 1 !== 0 || a.nrs < 0 || a.nrs > 10) errs.push("nrs");
      if (typeof a.text !== "string" || a.text.length > 1000) errs.push("text");
      var v = a.vitals || {};
      ["temp", "hr", "spo2"].forEach(function (k) {
        var x = v[k];
        if (x === null || x === undefined) return;
        var lim = KB.vitalLimits[k];
        if (typeof x !== "number" || !isFinite(x) || x < lim[0] || x > lim[1]) errs.push("vitals." + k);
      });
      if (a.followups !== null && a.followups !== undefined) {
        var fu = a.followups;
        if (typeof fu !== "object" || Array.isArray(fu) || Object.keys(fu).length > 20 ||
            Object.keys(fu).some(function (k) { return !fuById[k] || !inArr(FU_ANS, fu[k]); })) errs.push("followups");
      }
      return errs;
    }

    function analyze(a) {
      var v = a.vitals || {}, temp = v.temp, hr = v.hr, spo2 = v.spo2;
      var hits = extract(a.text);
      var sym = a.symptoms.slice(), textSym = [], finding = [], risks = a.risks.slice(), autoRisks = [];
      if (a.finding && a.finding !== "none") finding.push(a.finding);
      hits.forEach(function (h) {
        if (h.kind === "sym" && !inArr(sym, h.id) && !inArr(textSym, h.id)) textSym.push(h.id);
        if (h.kind === "finding" && !inArr(finding, h.id)) finding.push(h.id);
        if (h.kind === "risk" && !inArr(risks, h.id)) { risks.push(h.id); autoRisks.push(h.id + ":text"); }
      });
      if (typeof temp === "number" && temp >= 38 && !inArr(risks, "fever")) { risks.push("fever"); autoRisks.push("fever:temp"); }
      var allSym = sym.concat(textSym);
      var vital = [];
      if (typeof spo2 === "number" && spo2 <= 94) vital.push("spo2_low");
      if (typeof temp === "number" && temp >= 38) vital.push("temp_high");
      if (typeof hr === "number" && (hr >= 120 || hr <= 45)) vital.push("hr_abnormal");

      // ---- Red flags ----
      var flags = [];
      function flag(level, code, title, reason) { flags.push({ level: level, code: code, title: title, reason: reason }); }
      allSym.forEach(function (id) {
        var s = symFlag[id];
        if (!s) return;
        var src = inArr(sym, id) ? "" : " (자유 서술에서 언급)";
        flag(s.flag, "sym:" + id, s.label + src, s.flagReason);
      });
      if (a.nrs >= 9) flag("urgent", "nrs", "NRS " + a.nrs + "점의 극심한 통증·불편", "견디기 어려운 극심한 통증은 원인과 관계없이 우선 진료가 필요합니다.");
      if (typeof spo2 === "number") {
        if (spo2 <= 92) flag("emergency", "spo2", "산소포화도 " + spo2 + "%", "산소포화도 92% 이하는 저산소증으로 즉시 응급 평가가 필요합니다(측정 오류가 아닌지 다시 확인하세요).");
        else if (spo2 <= 94) flag("urgent", "spo2", "산소포화도 " + spo2 + "%", "산소포화도 93~94%는 정상보다 낮아 당일 진료가 권장됩니다.");
      }
      if (typeof temp === "number") {
        if (temp >= 40) flag("urgent", "temp", "체온 " + temp + "℃ 고열", "40℃ 이상의 고열은 당일 진료가 필요합니다.");
        else if (temp < 35) flag("urgent", "temp", "체온 " + temp + "℃ 저체온", "35℃ 미만의 저체온은 당일 진료가 필요합니다.");
      }
      if (typeof hr === "number" && (hr >= 130 || hr <= 40)) flag("urgent", "hr", "맥박 " + hr + "회/분", "안정 시 맥박이 130회 이상이거나 40회 이하이면 당일 평가가 필요합니다.");
      if (inArr(risks, "fever") && inArr(allSym, "neck_stiff")) flag("emergency", "combo:meningitis", "발열 + 목 경직", "발열과 목 경직이 함께 있으면 뇌수막염 등 응급 질환을 배제해야 합니다.");
      if (inArr(risks, "fever") && inArr(a.chronics, "immuno")) flag("urgent", "combo:immuno_fever", "면역 저하 상태의 발열", "항암치료·면역억제제 사용 중 발열은 중증 감염 위험이 높아 당일 진료가 필요합니다.");
      if ((a.pregnancy === "yes" || a.pregnancy === "unknown") && ["rlq", "ruq", "severe_abd", "epigastric"].some(function (x) { return inArr(allSym, x); }))
        flag("urgent", "combo:preg_abd", "임신 가능성이 있는 상태의 복통", "가임기 복통은 자궁외 임신 등 응급 질환 배제를 위해 임신 검사와 당일 진료가 권장됩니다.");
      flags.sort(function (x, y) { return LEVEL_RANK[y.level] - LEVEL_RANK[x.level]; });

      // ---- Scoring ----
      var ctx = { dur: [a.duration], finding: finding, sym: allSym, risk: risks, age: [a.age], sex: [a.sex], onset: [a.onset], course: [a.course], chronic: a.chronics, vital: vital, preg: a.pregnancy ? [a.pregnancy] : [] };
      function ruleMatch(cond) {
        return Object.keys(cond).every(function (k) { return cond[k].some(function (x) { return inArr(ctx[k], x); }); });
      }
      var text = (a.text || "").toLowerCase(), fua = answersOf(a);
      var cands = [];
      KB.conditions.forEach(function (c, idx) {
        if (c.sexOnly && c.sexOnly !== a.sex) return;
        if (c.requires && !c.requires.some(function (x) { return inArr(allSym, x); })) return;
        var score = c.base, evidence = 0, reasons = [];
        Object.keys(c.sym).forEach(function (id) {
          var w = c.sym[id];
          if (inArr(sym, id)) { score += w; if (w > 0) evidence++; reasons.push("증상: " + symLabel[id]); }
          else if (inArr(textSym, id)) { score += w * 0.5; if (w > 0) evidence++; reasons.push("자유 서술: " + symLabel[id]); }
        });
        if (c.find) Object.keys(c.find).forEach(function (id) {
          if (inArr(finding, id)) { score += c.find[id]; evidence++; reasons.push("검진 소견: " + lab(KB.findings, id)); }
        });
        if (evidence === 0) return;
        function apply(map, values, prefix, labeler) {
          if (!map) return;
          values.forEach(function (val) {
            if (val === null || val === undefined || !(val in map)) return;
            var w = map[val];
            score += w;
            reasons.push(prefix + labeler(val) + (w < 0 ? " (불일치 · 감점)" : ""));
          });
        }
        apply(c.dur, [a.duration], "지속 기간: ", durLabel);
        apply(c.onset, [a.onset], "발병 양상: ", function (x) { return lab(KB.onsets, x); });
        apply(c.course, [a.course], "경과: ", function (x) { return lab(KB.courses, x); });
        apply(c.risk, risks, "동반 위험 인자: ", function (x) { return lab(KB.risks, x); });
        apply(c.chronic, a.chronics, "기저질환: ", function (x) { return lab(KB.chronics, x); });
        apply(c.age, [a.age], "연령대: ", function (x) { return lab(KB.ages, x); });
        apply(c.sex, [a.sex], "성별: ", function (x) { return lab(KB.sexes, x); });
        apply(c.preg, a.pregnancy ? [a.pregnancy] : [], "임신 여부: ", function (x) { return lab(KB.pregnancy, x); });
        if (c.kw) Object.keys(c.kw).forEach(function (word) {
          if (textHas(text, word)) { score += c.kw[word]; reasons.push("자유 서술 키워드: '" + word + "'"); }
        });
        if (c.fu) Object.keys(c.fu).forEach(function (qid) {
          var w = c.fu[qid], ans = fua[qid];
          if (ans === "yes") { score += w; reasons.push("정밀 문진: " + fuById[qid].q + " → 예" + (w < 0 ? " (불일치 · 감점)" : "")); }
          else if (ans === "no" && w > 0) { score -= w * 0.5; reasons.push("정밀 문진: " + fuById[qid].q + " → 아니오 (감점)"); }
        });
        if (c.region === a.region) score += 1;
        KB.adjust.forEach(function (r) {
          if (!inArr(r.target, c.id) || !ruleMatch(r["if"])) return;
          if (r.add) score += r.add;
          if (r.mult && score > 0) score *= r.mult;
          reasons.push((r.add && r.add > 0) || (r.mult && r.mult > 1) ? "가점 규칙: " + r.reason : "감점 규칙: " + r.reason);
        });
        score = round2(score);
        if (score <= 0) return;
        cands.push({ idx: idx, c: c, score: score, reasons: reasons });
      });
      cands.sort(function (x, y) { return (y.score - x.score) || (x.idx - y.idx); });
      var ex = expShares(cands);
      cands.forEach(function (x, i) { x.e = ex.e[i]; x.pct = Math.floor(100 * ex.e[i] / ex.sum + 0.5); });
      return { sym: sym, textSym: textSym, finding: finding, risks: risks, autoRisks: autoRisks, hits: hits, flags: flags,
        temp: temp, hr: hr, spo2: spo2, fua: fua, scored: cands };
    }

    function answersOf(a) {
      var fu = (a.followups && typeof a.followups === "object") ? a.followups : {}, out = {};
      Object.keys(fu).forEach(function (k) { if (fuById[k] && inArr(FU_ANS, fu[k])) out[k] = fu[k]; });
      return out;
    }
    function expShares(list) {
      var e = [], sum = 0;
      if (!list.length) return { e: e, sum: 1 };
      var mx = list[0].score;
      for (var i = 0; i < list.length; i++) { var v = Math.exp((list[i].score - mx) / FUC.temp); e.push(v); sum += v; }
      return { e: e, sum: sum };
    }

    function nextQuestion(a) {
      var an = analyze(a), list = an.scored, asked = Object.keys(an.fua).length;
      if (!list.length || asked >= FUC.max || (asked >= FUC.min && list[0].pct >= FUC.stopShare)) return null;
      var top = list.slice(0, FUC.topK), tsum = 0;
      top.forEach(function (x) { tsum += x.e; });
      var best = null, bestGain = 0;
      FU.forEach(function (q) {
        if (q.id in an.fua) return;
        if (q.sex && q.sex !== a.sex) return;
        var ws = top.map(function (x) { return (x.c.fu && x.c.fu[q.id]) || 0; });
        if (!ws.some(function (w) { return w !== 0; })) return;
        var mean = 0, gain = 0, i;
        for (i = 0; i < top.length; i++) mean += top[i].e / tsum * ws[i];
        for (i = 0; i < top.length; i++) gain += top[i].e / tsum * Math.abs(ws[i] - mean);
        gain = Math.floor(gain * 10000 + 0.5) / 10000;
        if (gain > bestGain) { bestGain = gain; best = q; }
      });
      if (!best || bestGain < FUC.minGain) return null;
      return { id: best.id, q: best.q, hint: best.hint || null, asked: asked, max: FUC.max,
        leaders: top.slice(0, 3).map(function (x) { return { id: x.c.id, name: x.c.name, pct: x.pct }; }) };
    }

    function diagnose(a, now) {
      var an = analyze(a), sym = an.sym, textSym = an.textSym, finding = an.finding, risks = an.risks, autoRisks = an.autoRisks,
        hits = an.hits, flags = an.flags, temp = an.temp, hr = an.hr, spo2 = an.spo2;
      var cands = [];
      an.scored.slice(0, 4).forEach(function (x, i) {
        if (i > 0 && x.pct < 5) return;
        var c = x.c;
        cands.push({ id: c.id, name: c.name, score: x.score, pct: x.pct, fit: Math.floor(95 * (1 - Math.exp(-x.score / 9)) + 0.5),
          desc: c.desc, dept: c.dept, tests: c.tests.slice(), reasons: x.reasons, urgency: c.urgency });
      });

      // ---- Triage ----
      var level = "routine";
      function raise(l) { if (LEVEL_RANK[l] > LEVEL_RANK[level]) level = l; }
      flags.forEach(function (f) { raise(f.level); });
      if (cands.length && cands[0].fit >= 60) raise(cands[0].urgency === "emergency" ? "urgent" : cands[0].urgency);
      if (a.duration === "chronic" || a.nrs >= 7 || finding.length > 0 || inArr(risks, "weight_loss")) raise("soon");
      var triage = { level: level, title: TRIAGE[level].title, message: TRIAGE[level].message };

      // ---- Summary & note ----
      var summary = {
        age: lab(KB.ages, a.age), sex: lab(KB.sexes, a.sex), region: lab(KB.regions, a.region),
        symptoms: sym.map(function (id) { return symLabel[id]; }),
        text_symptoms: textSym.map(function (id) { return symLabel[id]; }),
        onset: lab(KB.onsets, a.onset), duration: durLabel(a.duration), course: lab(KB.courses, a.course),
        finding: a.finding ? lab(KB.findings, a.finding) : "해당 없음(질문 생략)",
        text_findings: finding.filter(function (f) { return f !== a.finding; }).map(function (f) { return lab(KB.findings, f); }),
        risks: risks.map(function (id) { return lab(KB.risks, id); }),
        auto_risks: autoRisks,
        chronics: a.chronics.map(function (id) { return lab(KB.chronics, id); }),
        pregnancy: a.pregnancy ? lab(KB.pregnancy, a.pregnancy) : null,
        vitals: { temp: (typeof temp === "number") ? temp : null, hr: (typeof hr === "number") ? hr : null, spo2: (typeof spo2 === "number") ? spo2 : null },
        nrs: a.nrs, text: a.text || "", keywords: hits.map(function (h) { return h.word; }),
        followups: FU.filter(function (q) { return q.id in an.fua; }).map(function (q) { return { q: q.q, a: FU_KO[an.fua[q.id]] }; })
      };
      var result = { kb_version: KB.version, created_at: now, triage: triage, red_flags: flags, candidates: cands, summary: summary, disclaimer: DISCLAIMER };
      result.note = buildNote(result);
      return result;
    }

    function buildNote(r) {
      var s = r.summary, L = [];
      function vit() {
        var p = [];
        p.push("체온 " + (s.vitals.temp !== null ? s.vitals.temp + "℃" : "미측정"));
        p.push("맥박 " + (s.vitals.hr !== null ? s.vitals.hr + "회/분" : "미측정"));
        p.push("SpO₂ " + (s.vitals.spo2 !== null ? s.vitals.spo2 + "%" : "미측정"));
        return p.join(" · ");
      }
      function list(x) { return x.length ? x.join(", ") : "없음"; }
      L.push("[메디내비 진료 요약 노트]");
      L.push("작성: " + r.created_at);
      L.push("");
      L.push("■ 기본 정보: " + s.age + " / " + s.sex + (s.pregnancy ? " / 임신: " + s.pregnancy : ""));
      L.push("■ 주요 불편 영역: " + s.region);
      L.push("■ 주요 증상: " + list(s.symptoms) + (s.text_symptoms.length ? " (서술에서 추가: " + s.text_symptoms.join(", ") + ")" : ""));
      L.push("■ 발병 · 경과: " + s.onset + " / " + s.duration + " / " + s.course);
      L.push("■ 통증·불편 강도: NRS " + s.nrs + "/10");
      L.push("■ 활력징후(자가 측정): " + vit());
      L.push("■ 건강검진 폐 영상 소견: " + s.finding + (s.text_findings.length ? " (서술에서 추가: " + s.text_findings.join(", ") + ")" : ""));
      L.push("■ 동반 위험 인자: " + list(s.risks));
      L.push("■ 기저질환 · 복용약: " + list(s.chronics));
      L.push("■ 추가 서술: " + (s.text ? s.text : "없음"));
      if (s.followups.length) { L.push("■ 정밀 문진:"); s.followups.forEach(function (f) { L.push("  - " + f.q + " → " + f.a); }); }
      L.push("");
      L.push("■ 응급 징후 선별: " + (r.red_flags.length ? r.red_flags.map(function (f) { return "[" + (f.level === "emergency" ? "응급" : "우선") + "] " + f.title; }).join("; ") : "해당 없음"));
      L.push("■ 권장 대응: " + r.triage.title);
      if (r.candidates.length) {
        var c = r.candidates[0];
        L.push("■ 1순위 감별 후보: " + c.name + " (상대 가능성 " + c.pct + "%)");
        L.push("■ 권장 진료과: " + c.dept);
        L.push("■ 참고 검사: " + c.tests.join(", "));
        if (r.candidates.length > 1) L.push("■ 기타 감별 후보: " + r.candidates.slice(1).map(function (x) { return x.name + " " + x.pct + "%"; }).join(", "));
      } else {
        L.push("■ 감별 후보: 입력 정보만으로 특정하기 어려움 — 진찰 필요");
      }
      L.push("");
      L.push("※ " + r.disclaimer);
      return L.join("\n");
    }

    return { validate: validate, diagnose: diagnose, nextQuestion: nextQuestion, extract: extract, version: KB.version };
  }

  root.MediNaviEngine = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
