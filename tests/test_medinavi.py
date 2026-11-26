"""MediNavi 검증 테스트 (표준 라이브러리 unittest). 실행: python -m unittest discover -s tests -v"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"


def answers(**kw):
    a = dict(age="50_60", sex="m", region="resp", symptoms=["cough"], onset="gradual", duration="acute",
             course="same", finding="none", vitals={"temp": None, "hr": None, "spo2": None}, risks=[], chronics=[],
             pregnancy=None, nrs=3, text="")
    a.update(kw)
    return a


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = server.Engine(server.load_kb())

    def dx(self, **kw):
        a = answers(**kw)
        self.assertEqual(self.e.validate(a), [])
        return self.e.diagnose(a, "test")

    def test_chronic_cough_ggo_not_cold(self):  # 명세 5-4
        r = self.dx(duration="chronic", finding="ggo")
        ids = [c["id"] for c in r["candidates"]]
        self.assertEqual(ids[0], "lung_lesion")
        self.assertNotIn("uri", ids)
        self.assertIn("호흡기내과", r["candidates"][0]["dept"])
        self.assertTrue(any("CT" in t for t in r["candidates"][0]["tests"]))

    def test_acute_cold_is_top(self):
        r = self.dx(symptoms=["cough", "sputum"], duration="acute")
        self.assertEqual(r["candidates"][0]["id"], "uri")

    def test_red_flags(self):  # 명세 5-3
        self.assertEqual(self.dx(symptoms=["chest_pain"])["triage"]["level"], "emergency")
        self.assertEqual(self.dx(region="neuro", symptoms=["weakness"])["triage"]["level"], "emergency")
        r = self.dx(nrs=9)
        self.assertIn("nrs", [f["code"] for f in r["red_flags"]])
        r = self.dx(vitals={"temp": 38.5, "hr": None, "spo2": 91})
        self.assertIn("spo2", [f["code"] for f in r["red_flags"]])
        self.assertIn("38℃ 넘는 열 · 오한", r["summary"]["risks"])

    def test_text_keywords_and_negation(self):
        r = self.dx(symptoms=["sputum"], text="작년 검진 CT에서 간유리 음영이 있다고 들었어요")
        self.assertIn("간유리", r["summary"]["keywords"])
        r = self.dx(symptoms=["sputum"], text="기침은 없어요")
        self.assertNotIn("기침", r["summary"]["keywords"])

    def test_validation_rejects_bad_input(self):
        self.assertIn("symptoms", self.e.validate(answers(symptoms=["headache"])))  # 다른 영역 증상
        self.assertIn("nrs", self.e.validate(answers(nrs=11)))
        self.assertIn("vitals.temp", self.e.validate(answers(vitals={"temp": 50})))
        self.assertIn("text", self.e.validate(answers(text="x" * 1001)))

    def test_followups_narrow_down(self):  # 정밀 문진: 감기 vs 폐렴을 추가 질문으로 가른다
        base = dict(age="30_40", symptoms=["cough", "sputum"], onset="sudden", duration="acute",
                    vitals={"temp": 38.3, "hr": None, "spo2": None}, risks=["fever"])
        r0 = self.dx(**base)
        self.assertEqual({c["id"] for c in r0["candidates"][:2]}, {"uri", "pneumonia"})
        a = answers(**base)
        a["followups"] = {}
        q = self.e.next_question(a)
        self.assertIsNotNone(q)
        cold = dict(a, followups={"r_nose": "yes", "r_walk_dyspnea": "no", "r_pleuritic": "no"})
        pneu = dict(a, followups={"r_nose": "no", "r_walk_dyspnea": "yes", "r_pleuritic": "yes", "r_yellow_sputum": "yes"})
        self.assertEqual(self.e.validate(cold), [])
        rc, rp = self.e.diagnose(cold, "t"), self.e.diagnose(pneu, "t")
        self.assertEqual(rc["candidates"][0]["id"], "uri")
        self.assertGreater(rc["candidates"][0]["pct"], r0["candidates"][0]["pct"])
        self.assertEqual(rp["candidates"][0]["id"], "pneumonia")
        self.assertTrue(any("정밀 문진" in line for line in rp["note"].splitlines()))

    def test_followup_loop_terminates(self):
        a = answers(symptoms=["cough"], duration="subacute", followups={})
        for _ in range(20):
            q = self.e.next_question(a)
            if not q:
                break
            self.assertNotIn(q["id"], a["followups"])
            a["followups"][q["id"]] = "unsure"
        self.assertLessEqual(len(a["followups"]), self.e.kb["fuConfig"]["max"])
        self.assertIn("followups", self.e.validate(answers(followups={"nope": "yes"})))
        self.assertIn("followups", self.e.validate(answers(followups={"r_nose": "maybe"})))
        self.assertIn("followups", self.e.validate(answers(followups=["r_nose"])))

    def test_js_engine_parity(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 미설치")
        html = open(os.path.join(ROOT, "Newdiagnosis.html"), encoding="utf-8").read()
        import re
        js = re.search(r'<script id="medinavi-engine">(.*?)</script>', html, re.S).group(1)
        cases = [answers(duration=d, finding=f, nrs=n, text=t, vitals={"temp": tp, "hr": None, "spo2": None})
                 for d in ("acute", "chronic") for f in ("ggo", "none") for n in (2, 9)
                 for t in ("", "새벽 기침, 결절") for tp in (None, 38.4)]
        import random
        rnd = random.Random(42)
        kb = self.e.kb
        fu_ids = [q["id"] for q in kb["followups"]]
        for _ in range(600):  # 무작위 문진 + 무작위 정밀 문진 답변
            region = rnd.choice([r["id"] for r in kb["regions"]])
            syms = [x["id"] for x in kb["symptoms"][region]]
            cases.append(answers(age=rnd.choice(["u20", "30_40", "50_60", "70p"]), sex=rnd.choice(["m", "f"]), region=region,
                                 symptoms=rnd.sample(syms, rnd.randint(1, 3)), onset=rnd.choice(["sudden", "gradual"]),
                                 duration=rnd.choice(["acute", "subacute", "chronic"]), course=rnd.choice(["worse", "same", "better", "waxing"]),
                                 risks=rnd.sample(["fever", "smoking", "weight_loss", "night_sweats"], rnd.randint(0, 2)),
                                 nrs=rnd.randint(0, 10), followups={k: rnd.choice(["yes", "no", "unsure"]) for k in rnd.sample(fu_ids, rnd.randint(0, 6))}))
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "e.js"), "w", encoding="utf-8").write(js)
            open(os.path.join(d, "c.json"), "w", encoding="utf-8").write(json.dumps({"kb": self.e.kb, "cases": cases}, ensure_ascii=False))
            out = subprocess.check_output([node, "-e",
                "const fs=require('fs');eval(fs.readFileSync('e.js','utf8'));const d=JSON.parse(fs.readFileSync('c.json','utf8'));"
                "const E=globalThis.MediNaviEngine.create(d.kb);process.stdout.write(JSON.stringify(d.cases.map(c=>[E.diagnose(c,'t'),E.nextQuestion(c)])))"], cwd=d)
        js_res = json.loads(out)
        py_res = [json.loads(json.dumps([self.e.diagnose(c, "t"), self.e.next_question(c)])) for c in cases]
        self.assertEqual(py_res, js_res)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        cls.tmp = tempfile.mkdtemp()
        env = dict(os.environ, PORT=str(cls.port), MEDINAVI_DB=os.path.join(cls.tmp, "medinavi.db"))
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/healthz" % cls.port, timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path, method="GET", body=None, key=KEY):
        h = {"Content-Type": "application/json"}
        if key:
            h["X-MediNavi-Key"] = key
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_port_env_and_pages(self):  # 명세 5-6
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port) as r:
            self.assertIn("medinavi-kb", r.read().decode())
        st, j = self.req("/api/bootstrap")
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])

    def test_diagnose_store_history_delete(self):  # 명세 5-5
        st, j = self.req("/api/diagnose", "POST", {"user_id": "tester", "answers": answers(duration="chronic", finding="ggo")})
        self.assertEqual(st, 200)
        sid = j["session_id"]
        self.assertEqual(j["result"]["candidates"][0]["id"], "lung_lesion")
        st, j = self.req("/api/history?user_id=tester")
        self.assertIn(sid, [i["session_id"] for i in j["items"]])
        st, j = self.req("/api/history/" + sid)
        self.assertEqual(j["result"]["candidates"][0]["id"], "lung_lesion")
        # another device (different key) cannot see it
        st, j = self.req("/api/history?user_id=tester", key="f" * 32)
        self.assertEqual(j["items"], [])
        self.assertEqual(self.req("/api/history/" + sid, key="f" * 32)[0], 404)
        self.assertEqual(self.req("/api/history/" + sid, key=None)[0], 401)
        self.assertEqual(self.req("/api/history/" + sid, "DELETE")[0], 200)
        self.assertEqual(self.req("/api/history/" + sid)[0], 404)

    def test_rejects_invalid(self):
        st, j = self.req("/api/diagnose", "POST", {"user_id": "tester", "answers": answers(region="nope")})
        self.assertEqual(st, 422)
        st, j = self.req("/api/diagnose", "POST", {"user_id": "<script>", "answers": answers()})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
