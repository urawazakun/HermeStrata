"""GPU acceptance for the Hermes x Strata cache work (DESIGN-HERMES-CACHE.md §5; recipe strata-up/docs/hermes-cache-acceptance.md).
Runs an isolated dev engine on 18101 with the production model/flags and a chosen exe, sends Hermes-shaped
requests, and records the engine's per-request resume/prefill lines.

  python accept_cache.py c1  [--exe ...]      warm root + aux title: second session prefill <= 2 s
  python accept_cache.py c3  [--exe ...]      parent -> isolated child -> parent, parent -> forked child -> parent
  python accept_cache.py serve --variant cold|cached|prod   start a server and leave it up (for step3 scripts)
  python accept_cache.py stop
Results append to <root>\\logs\\accept\\<case>.jsonl ; engine logs next to them."""
import argparse, copy, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("HERMESTRATA_ROOT", Path(__file__).resolve().parent.parent))  # project root
OUT = ROOT / "logs" / "accept"
PROD_CFG = ROOT / "deploy-strata" / "strata-qwen-iq3_s.run.json"
EXE_ACCEPT = Path(os.environ.get("ACCEPT_EXE", ROOT / "build-strata-accept" / "strata.exe"))
EXE_PROD = ROOT / "deploy-strata" / "bin-0121" / "strata.exe"
SRC_ACCEPT = Path(os.environ.get("ACCEPT_SRC", ROOT / "strata-accept"))
BODY = json.loads((ROOT / "briefs" / "dumps-growing-lcp" / "bisect-body.json").read_text(encoding="utf-8"))["request"]["body"]
PORT = 18101
URL = f"http://127.0.0.1:{PORT}"
COLD = ["--prompt-cache", "0", "--tip-cache-gib", "0", "--prompt-cache-root", "0", "--no-root-disk"]
OUT.mkdir(parents=True, exist_ok=True)
PIDFILE = OUT / "server.pid"


def start(variant, extra=()):
    stop()
    cfg = json.loads(PROD_CFG.read_text(encoding="utf-8-sig"))
    args = [a for a in cfg["args"] if a != "--vision"]           # vision off: not under test, saves VRAM/RAM
    exe, cwd = (EXE_PROD, ROOT / "strata-prod") if variant == "prod" else (EXE_ACCEPT, SRC_ACCEPT)
    if variant == "cold":
        args += COLD
    args += list(extra)
    stamp = time.strftime("%H%M%S")
    log = OUT / f"engine-{variant}-{stamp}.log"
    cfg.update(exe=str(exe), cwd=str(cwd), args=args, port=PORT, log=str(log))
    cfg.pop("vision", None)
    cp = OUT / f"cfg-{variant}.run.json"
    cp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    pr = subprocess.Popen([sys.executable, str(cwd / "serve" / "server.py"), "--engine", "strata", "--config", str(cp),
                           "--host", "127.0.0.1", "--port", str(PORT)], cwd=cwd,
                          stdout=open(OUT / f"server-{variant}-{stamp}.out", "w"), stderr=subprocess.STDOUT,
                          creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    PIDFILE.write_text(str(pr.pid))
    t0 = time.time()
    while time.time() - t0 < 900:
        try:                                                   # ready = a 1-token completion succeeds
            probe = {"model": BODY["model"], "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            r = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(probe).encode(), {"Content-Type": "application/json"})
            json.load(urllib.request.urlopen(r, timeout=60))
            break
        except Exception:
            pass
        if pr.poll() is not None:
            sys.exit(f"server exited rc={pr.returncode}; see {log}")
        time.sleep(3)
    else:
        stop()
        sys.exit(f"server readiness timed out after 900s; see {log}")
    print(f"[{variant}] up in {time.time() - t0:.0f}s  log={log}  args+={' '.join(args[-8:])}", flush=True)
    return log


def stop():
    if PIDFILE.exists():
        subprocess.run(["taskkill", "/PID", PIDFILE.read_text().strip(), "/T", "/F"], capture_output=True)
        PIDFILE.unlink()
        time.sleep(3)


class Engine:
    def __init__(self, log):
        self.log, self.pos = Path(log), 0
        self.skip()

    def skip(self):
        self.pos = self.log.stat().st_size if self.log.exists() else 0

    def new_lines(self):
        with open(self.log, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        self.pos += len(data)
        return data.decode("utf-8", "replace").splitlines()

    def req(self, body, tag):
        self.skip()
        t0 = time.time()
        r = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(r, timeout=1800))
        wall = time.time() - t0
        time.sleep(1.0)                                        # snapshot line lands after the reply
        lines = self.new_lines()
        rec = {"tag": tag, "wall_s": round(wall, 2)}
        for l in lines:
            if m := re.search(r"resume from (\S+) at (\d+), suffix (\d+), restore ([\d.]+) ms", l):
                rec.update(resume=m[1], k=int(m[2]), suffix=int(m[3]), restore_ms=float(m[4]))
                if s := re.search(r"snapshot ([\d.]+) ms", l):
                    rec["snapshot_ms"] = float(s[1])
            if m := re.search(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms.*?(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)", l):
                rec.update(prompt=int(m[1]), reused=int(m[2]), read=int(m[3]), prefill_ms=int(m[4]), gen=int(m[5]), tok_s=float(m[7]))
        rec["resume_lines"] = [l[:220] for l in lines if "resume" in l or "tip" in l.lower() or "root" in l.lower()][:6]
        msg = resp["choices"][0]["message"]
        rec["answer"] = (msg.get("content") or "")[:120]
        print(json.dumps({k: v for k, v in rec.items() if k != "resume_lines"}, ensure_ascii=False), flush=True)
        return rec, msg


def body_with(messages, max_tokens=48, system=None, tools=True):
    b = copy.deepcopy(BODY)
    b["messages"] = ([{"role": "system", "content": system}] if system else [BODY["messages"][0]]) + messages
    b["max_tokens"], b["temperature"] = max_tokens, 0
    if not tools:
        b.pop("tools", None)
    return b


def aux_title(text):
    return {"model": BODY["model"], "max_tokens": 32, "temperature": 0.3,
            "messages": [{"role": "system", "content": "Generate a short title (3-7 words) for this conversation. Reply as JSON {\"title\": ...}."},
                         {"role": "user", "content": text}],
            "response_format": {"type": "json_object"}}


def assistant_of(msg):
    a = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        a["tool_calls"] = msg["tool_calls"]
    return a


def case1(eng, rep):
    out = []
    qa, qb = [("1+1は？数字だけ答えて。", "2+3は？数字だけ答えて。"), ("日本の首都は？一語で。", "フランスの首都は？一語で。"),
              ("7x8は？数字だけ。", "9x9は？数字だけ。")][rep]
    r, m = eng.req(body_with([{"role": "user", "content": qa}]), f"c1.r{rep}.A")
    out.append(r)
    r, _ = eng.req(aux_title(qa + "\n" + (m.get("content") or "")), f"c1.r{rep}.aux")
    out.append(r)
    r, _ = eng.req(body_with([{"role": "user", "content": qb}]), f"c1.r{rep}.B")
    r["pass"] = r["wall_s"] <= 2.0 or r.get("prefill_ms", 1e9) <= 2000
    out.append(r)
    return out


CHILD_SYS = ("# Role: implementer (child agent)\nYou implement exactly the task you are given and report files, tests and "
             "numbers. No recommendations.\n\n") + BODY["messages"][0]["content"][:9000]


def case3(eng, rep):
    out = []
    topics = ["ローグライクの食料システム", "FOV の対称性", "セーブの決定論"][rep]
    p1 = [{"role": "user", "content": f"（合言葉: 青いペンギン{rep}号。覚えておいて）{topics}について、設計上の注意点を2行で。"}]
    r, a1 = eng.req(body_with(p1, 96), f"c3.r{rep}.parent1")
    out.append(r)
    r, _ = eng.req(body_with([{"role": "user", "content": "hello.py に print('hi') を書く手順を1行で。"}], 48, system=CHILD_SYS), f"c3.r{rep}.child_isolated")
    out.append(r)
    p2 = p1 + [assistant_of(a1), {"role": "user", "content": "その1行目をもう少し具体的に。"}]
    r, a2 = eng.req(body_with(p2, 96), f"c3.r{rep}.parent2_after_child")
    r["expect"] = "resume tip, suffix small"
    out.append(r)
    fork = p2 + [assistant_of(a2), {"role": "user", "content": "[forked child, depth 1/2] You are a copy of the agent above, now working ONLY on this task: 最初に教わった合言葉をそのまま1行で答えよ。 Do it yourself."}]
    r, fm = eng.req(body_with(fork, 400), f"c3.r{rep}.fork_child")
    r["recall"] = f"青いペンギン{rep}" in ((fm.get("content") or "") + (fm.get("reasoning_content") or ""))
    r["expect"] = "resume tip of parent2 (incl. generated), suffix small"
    out.append(r)
    p3 = p2 + [assistant_of(a2), {"role": "user", "content": "了解。まとめて1行で。"}]
    r, _ = eng.req(body_with(p3, 48), f"c3.r{rep}.parent3_after_fork")
    r["expect"] = "resume parent2 tip, suffix small"
    out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["c1", "c3", "serve", "stop"])
    ap.add_argument("--variant", default="cached", choices=["cold", "cached", "prod"])
    ap.add_argument("--extra", default="")
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    if a.what == "stop":
        return stop()
    log = start(a.variant, a.extra.split())
    if a.what == "serve":
        return
    eng = Engine(log)
    fn = {"c1": case1, "c3": case3}[a.what]
    res = []
    for rep in range(a.reps):
        res += fn(eng, rep)
    with open(OUT / f"{a.what}-{a.variant}.jsonl", "a", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(dict(r, variant=a.variant, t=time.strftime("%H:%M:%S")), ensure_ascii=False) + "\n")
    stop()


if __name__ == "__main__":
    main()
