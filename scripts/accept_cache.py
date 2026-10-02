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
EXE_PROD = ROOT / "deploy-strata" / "bin-030" / "strata.exe"      # current production (v100-030)
SRC_ACCEPT = Path(os.environ.get("ACCEPT_SRC", ROOT / "strata-accept"))
BODY = json.loads((ROOT / "briefs" / "dumps-growing-lcp" / "bisect-body.json").read_text(encoding="utf-8"))["request"]["body"]
PORT = 18101
URL = f"http://127.0.0.1:{PORT}"
COLD = ["--prompt-cache", "0", "--tip-cache-gib", "0", "--prompt-cache-root", "0", "--no-root-disk"]
OUT.mkdir(parents=True, exist_ok=True)
PIDFILE = OUT / "server.pid"
_OWNED_SERVER = None  # Popen handle for this invocation; taskkill may be denied on Windows.


def start(variant, extra=()):
    global _OWNED_SERVER
    stop()
    cfg = json.loads(PROD_CFG.read_text(encoding="utf-8-sig"))
    args = [a for a in cfg["args"] if a != "--vision"]           # vision off: not under test, saves VRAM/RAM
    exe, cwd = (EXE_PROD, ROOT / "strata-prod-030") if variant == "prod" else (EXE_ACCEPT, SRC_ACCEPT)
    if "--root-dir" in args:                                    # never share production's root dir
        args[args.index("--root-dir") + 1] = str(OUT / f"roots-{variant}")
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
    _OWNED_SERVER = pr
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


def _pid_alive(pid):
    """True unless tasklist independently proves the exact PID is gone.

    A tasklist failure cannot prove exit, so it conservatively reports alive
    (the PID marker is preserved and the failure stays loud)."""
    try:
        q = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)
    except Exception:
        return True
    if q.returncode != 0:
        return True
    return f'"{pid}"' in (q.stdout or "")


def stop():
    """Unload and terminate our live server; check taskkill for a stale marker.

    Returns True when no server remains (marker unlinked, including the
    nothing-to-stop case). Raises RuntimeError with the marker preserved when
    the kill fails and the exact PID is still alive, or the marker is not a
    PID at all. Never silently claims cleanup."""
    global _OWNED_SERVER
    if _OWNED_SERVER is not None:
        pr = _OWNED_SERVER
        if PIDFILE.exists() and PIDFILE.read_text().strip() != str(pr.pid):
            raise RuntimeError("stop: PID marker differs from the owned server; marker preserved")
        if pr.poll() is None:
            # Release the engine via its API before terminating Python. The
            # Popen handle belongs to us and works where taskkill is denied.
            req = urllib.request.Request(URL + "/unload", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=60) as response:
                reply = json.load(response)
            if reply.get("status") not in ("unloaded", "not loaded"):
                raise RuntimeError("stop: owned server did not unload; marker preserved")
            pr.terminate()
            pr.wait(timeout=10)
        if PIDFILE.exists():
            PIDFILE.unlink()
        _OWNED_SERVER = None
        return True
    if not PIDFILE.exists():
        return True
    pid = PIDFILE.read_text().strip()
    if not pid.isdigit():
        raise RuntimeError(f"stop: refusing taskkill on non-numeric PID marker {pid!r}; marker preserved")
    try:
        kill = subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    except Exception as e:
        raise RuntimeError(f"stop: could not run taskkill for PID {pid}: {e}; marker preserved") from e
    if kill.returncode != 0 and _pid_alive(pid):
        raise RuntimeError(f"stop: taskkill rc={kill.returncode} for PID {pid}, process still alive; marker preserved")
    PIDFILE.unlink()  # killed, or the exact PID had already exited (stale marker)
    if kill.returncode == 0:
        time.sleep(3)
    return True


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
        rec["ckpt_lines"] = [l[:220] for l in lines  # tail: the exact mount marker, wherever it lands
                             if re.search(r"resume from chain at \d+ \(checkpoint row\)", l)]
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


def case_ja(eng, rep):
    """Japanese answer speed (0.1.27's CJK draft vocab), greedy and sampled."""
    q = ["日本の四季それぞれの特徴を、身近な例を挙げて丁寧に説明してください。", "ローグライクゲームの歴史と代表作を、年代順に日本語で解説してください。",
         "プログラミング初心者に向けて、変数と関数の違いを日本語でやさしく説明してください。"][rep]
    out = []
    for temp in (0, 0.7):
        b = {"model": BODY["model"], "max_tokens": 400, "temperature": temp, "messages": [{"role": "user", "content": q}]}
        r, _ = eng.req(b, f"ja.r{rep}.t{temp}")
        out.append(r)
    return out


def case_needle(eng, rep):
    """A fact planted at ~1/3 depth of a ~30K-token document must be found."""
    import random
    rnd = random.Random(rep)
    filler = [f"第{i}節。倉庫の在庫記録によると、区画{rnd.randint(1, 999)}の箱は{rnd.randint(1, 99)}個で、点検は{rnd.randint(1, 28)}日に行われた。"
              for i in range(1100)]
    key = ["紫の鍵番号は 4172", "銀の暗号は ORCA-58", "青い扉の番号は 903"][rep]
    filler.insert(len(filler) // 3, f"重要: {key} である。")
    q = "\n".join(filler) + f"\n\n質問: 文書中の「{key.split('は')[0]}」は何？値だけ答えて。"
    r, m = eng.req({"model": BODY["model"], "max_tokens": 300, "temperature": 0, "messages": [{"role": "user", "content": q}]}, f"needle.r{rep}")
    r["found"] = key.split()[-1] in ((m.get("content") or "") + (m.get("reasoning_content") or ""))
    print(json.dumps({"tag": r["tag"], "found": r["found"], "prompt": r.get("prompt")}), flush=True)
    return [r]


# --- tail-resume acceptance (Muse-2, step 2; ADDITIONS only, existing cases untouched) ---
# Server API inspection (serve/server.py @5174993): no /tokenize or /render endpoint.
# Exposed: GET /props returns the chat_template source; prompts are rendered server-side
# via Service.prepare (ChatTemplate.render + tok.encode, openai_to_messages). The oracle
# below is therefore faithful-local: same openai_to_messages + same pack template +
# same pack tokenizer (tools/strata_tokenizer), CPU-only, no GPU/server. This mirrors
# tools/probe_turn_prefix.py. Raw emitted prompt+generated is NEVER concatenated as
# history; only actually-sent bodies are rendered, and when the raw previous end is not
# a prefix the last stable turn boundary L is used and recorded (method="turn-boundary").
# Isolation for tail (chain-only, no tip masking, no disk): --tip-cache-gib 0 and
# --no-root-disk, plus --prompt-cache 16, --prompt-cache-root 1024, --prompt-cache-every 4096.
TAIL_LO, TAIL_HI, TAIL_TOL = 18000, 22000, 64
TAIL_EVERY = 4096
TAIL_PREFILL = 2048  # forced in TAIL_ISOLATION; the simulation below assumes exactly this
TAIL_SHORT = 64      # forced in TAIL_ISOLATION; segments this short read via windows (no periodic)
TAIL_ISOLATION = ["--tip-cache-gib", "0", "--no-root-disk", "--prompt-cache", "16",
                  "--prompt-cache-root", "1024", "--prompt-cache-every", "4096",
                  "--prefill", "2048", "--short-read", "64"]
TAIL_EVERY_TOL = TAIL_EVERY - TAIL_TOL
TAIL_ROOT_THRESH = 1024
TAIL_U2 = "その1行目をもう少し具体的に。"
TAIL_U3 = "了解。まとめて1行で。"
TAIL_MUT_PREFIX = "[訂正] "
TAIL_SYNTH_A = "了解。"
TAIL_SYNTH_U = "続けて。"
_TAIL_PREV_P1 = {}  # rep -> previous rep's p1 ids (cross-rep inheritance guard; CPU-only)


def tail_body_with(messages, max_tokens=48):
    """Tail-only body: consistent reasoning_effort='low' on every tail request so the
    pack template renders every turn the same way (existing cases use body_with)."""
    b = body_with(messages, max_tokens)
    b["reasoning_effort"] = "low"
    eb = b.get("extra_body")
    if isinstance(eb, dict):
        ctk = eb.get("chat_template_kwargs")
        if isinstance(ctk, dict):
            ctk["reasoning_effort"] = "low"
    return b


def tail_lcp(a, b):
    n, i = (len(a), len(b)), 0
    m = n[0] if n[0] < n[1] else n[1]
    while i < m and a[i] == b[i]:
        i += 1
    return i


def tail_last_turn(ids, turn_id):
    for i in range(len(ids) - 1, -1, -1):
        if ids[i] == turn_id:
            return i
    return None


def tail_genuine_root(ids, turn_id, thr=TAIL_ROOT_THRESH):
    """Mirror generate.cpp: FIRST turn-token at index>0 and strictly before the last
    boundary; accepted as root only if >= thr, else no genuine root (None)."""
    last = tail_last_turn(ids, turn_id)
    if last is None:
        return None
    for i in range(1, last):
        if ids[i] == turn_id:
            return i if i >= thr else None
    return None


def tail_parse_resume(line):
    m = re.search(r"resume from (\S+) at (\d+), suffix (\d+), restore ([\d.]+) ms", line)
    if not m:
        return None
    d = {"resume": m[1], "k": int(m[2]), "suffix": int(m[3]), "restore_ms": float(m[4])}
    if s := re.search(r"snapshot ([\d.]+) ms", line):
        d["snapshot_ms"] = float(s[1])
    return d


def tail_parse_prompt(line):
    m = re.search(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms.*?(\d+) generated in (\d+) ms \(([\d.]+) tok/s\)", line)
    if not m:
        return None
    return {"prompt": int(m[1]), "reused": int(m[2]), "read": int(m[3]), "prefill_ms": int(m[4]),
            "gen": int(m[5]), "tok_s": float(m[7])}


def tail_prefix_ok(n, lo=TAIL_LO, hi=TAIL_HI):
    return isinstance(n, int) and lo <= n <= hi


def tail_find_sections(measure, lo=TAIL_LO, hi=TAIL_HI, lo_n=50, hi_n=1200):
    """Adaptive/binary search on monotonic measure(n_sec)->size for size in [lo, hi].
    measure: callable(n_sec)->int (CPU-only renders). Bounded (~14 calls). Returns
    (n_sec, size, note) with note 'ok' or a setup-failure reason (never send GPU then)."""
    try:
        f_lo, f_hi = measure(lo_n), measure(hi_n)
    except Exception as e:
        return (lo_n, -1, f"measure failed: {e}")
    for v in (f_lo, f_hi):
        if not tail_is_int(v) or v < 0:
            return (lo_n, v, f"measure returned bad size {v!r}")
    if f_lo > hi:
        return (lo_n, f_lo, f"even minimum n={lo_n} gives {f_lo} above HI {hi}")
    if f_hi < lo:
        return (hi_n, f_hi, f"even maximum n={hi_n} gives {f_hi} below LO {lo}")
    if lo <= f_lo <= hi:
        return (lo_n, f_lo, "ok")
    if lo <= f_hi <= hi:
        return (hi_n, f_hi, "ok")
    a, fa, b = lo_n, f_lo, hi_n  # invariant: f(a) < lo, f(b) > hi
    for _ in range(12):
        m = (a + b) // 2
        if m <= a or m >= b:
            break
        try:
            fm = measure(m)
        except Exception as e:
            return (m, -1, f"measure failed at n={m}: {e}")
        if not tail_is_int(fm) or fm < 0:
            return (m, fm, f"measure returned bad size {fm!r} at n={m}")
        if lo <= fm <= hi:
            return (m, fm, "ok")
        if fm < lo:
            a, fa = m, fm
        else:
            b = m
    return (a, fa, f"no n in [{lo_n},{hi_n}] lands {fa} in [{lo},{hi}] after search")


def tail_expected_reuse(prev_ids, cur_ids, turn_id):
    """Independent expectation from faithful ids. Never uses rec['suffix'] (tautology).
    L is the LCP length (NOT the boundary itself); boundary is reported separately and
    must satisfy boundary <= LCP (checked via 'consistent')."""
    l = tail_lcp(prev_ids, cur_ids)
    b = tail_last_turn(prev_ids, turn_id)
    base = {"L": l, "suffix": len(cur_ids) - l, "boundary": b, "lcp": l,
            "prev_len": len(prev_ids), "cur_len": len(cur_ids)}
    if b is None:
        return {**base, "method": "lcp-no-boundary", "consistent": True}
    if prev_ids == cur_ids[:len(prev_ids)]:
        return {**base, "method": "raw-prefix", "consistent": bool(b <= l)}
    return {**base, "method": "turn-boundary", "consistent": bool(b <= l)}


def tail_is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def tail_check_counts(rec, ids_len):
    """Engine metrics vs independent len(ids): prompt==len(ids), reused==k, read==prompt-reused.
    Rejects negative/non-int/inconsistent values (never silent pass)."""
    for k in ("prompt", "reused", "read", "k"):
        v = rec.get(k)
        if not tail_is_int(v) or v < 0:
            return False, f"bad metric {k}={v!r} (need non-negative int)"
    if rec["prompt"] != ids_len:
        return False, f"prompt={rec['prompt']} != independent len(ids)={ids_len} (wrong tokenizer/template?)"
    if rec["read"] != rec["prompt"] - rec["reused"]:
        return False, f"read={rec['read']} != prompt-reused={rec['prompt'] - rec['reused']}"
    if rec["reused"] != rec["k"]:
        return False, f"reused={rec['reused']} != k={rec['k']}"
    return True, "counts consistent"


def tail_row_text(rec):
    return (rec.get("ckpt_lines") or []) + (rec.get("resume_lines") or [])


def tail_exact_row(lines, k):
    """Exact successful-mount marker `resume from chain at K (checkpoint row)` with K == rec['k'].
    A generic 'checkpoint' substring is insufficient."""
    if not tail_is_int(k) or k < 0:
        return False
    pat = re.compile(r"resume from chain at (\d+) \(checkpoint row\)")
    for l in (lines or []):
        m = pat.search(l or "")
        if m and int(m[1]) == k:
            return True
    return False


def tail_check_turn(rec, exp_L, exp_suffix, ids_len, exp_boundary=None, tol=TAIL_TOL):
    """Strict tail-turn gate. Missing/bad metrics/logs fail (never silent pass)."""
    for k in ("prompt", "read", "resume", "k", "resume_lines"):
        if rec.get(k) is None:
            return False, f"missing metric/log: {k}"
    ok, why = tail_check_counts(rec, ids_len)
    if not ok:
        return False, why
    if exp_boundary is not None and not (tail_is_int(exp_boundary) and tail_is_int(exp_L) and exp_boundary <= exp_L):
        return False, f"inconsistent expectation: boundary={exp_boundary} > LCP L={exp_L}"
    if rec.get("resume") != "chain":
        return False, f"expected chain resume, got {rec.get('resume')!r} (tip would mask failure)"
    if not tail_exact_row(tail_row_text(rec), rec.get("k")):
        return False, "missing/mismatched exact marker `resume from chain at K (checkpoint row)` with K==k"
    k = rec["k"]
    if not (exp_L - tol <= k <= exp_L):
        return False, f"k={k} not near expected L={exp_L} (+-{tol})"
    if rec["read"] > exp_suffix + tol:
        return False, f"read={rec['read']} > independent suffix={exp_suffix}+{tol}"
    return True, "chain at L, read within suffix+tol"


def tail_check_mutated(rec, K_exp, root, change_pos, ids_len, tol=0):
    """Mutated middle-message gate: k must be the newest checkpoint below the change, above root."""
    for k in ("prompt", "read", "resume", "k", "resume_lines"):
        if rec.get(k) is None:
            return False, f"missing metric/log: {k}"
    ok, why = tail_check_counts(rec, ids_len)
    if not ok:
        return False, why
    if root is None or K_exp is None or change_pos is None:
        return False, "missing independent expectation (root/K_exp/change_pos)"
    if not all(tail_is_int(v) and v >= 0 for v in (K_exp, root, change_pos)):
        return False, f"bad expectation K_exp={K_exp!r} root={root!r} change={change_pos!r}"
    if not (K_exp > root):
        return False, f"K_exp={K_exp} not greater than root={root}"
    if not (K_exp < change_pos):
        return False, f"K_exp={K_exp} not below change={change_pos}"
    if rec.get("resume") != "chain":
        return False, f"expected chain resume, got {rec.get('resume')!r}"
    k = rec["k"]
    if not tail_exact_row(tail_row_text(rec), k):
        return False, "missing/mismatched exact marker `resume from chain at K (checkpoint row)` with K==k"
    if k == root:
        return False, f"k={k} is the old root, not the newest checkpoint below the change"
    if abs(k - K_exp) > tol:
        if k < K_exp:
            return False, f"k={k} older than newest K_exp={K_exp} below the change"
        return False, f"k={k} != K_exp={K_exp} (at/above the change)"
    if rec["read"] != rec["prompt"] - K_exp:
        return False, f"read={rec['read']} != prompt-K_exp={rec['prompt'] - K_exp}"
    return True, f"chain at newest checkpoint {K_exp} below the change, above root {root}"


def tail_mutate_early(text):
    """Distinct change at the very beginning of the assistant content (earliest divergence)."""
    return TAIL_MUT_PREFIX + (text or "")


def tail_simulate(n, resume, root_at, turn_at, every=TAIL_EVERY, chunk=TAIL_PREFILL, short=TAIL_SHORT):
    """CPU simulation of one request's chain checkpoints (single GPU, no images, no
    STRATA_CKPT_REREAD), matching generate.cpp: segments {root_at, turn_at, n-1} past
    `resume` (pp_next = resume+every); a segment of length<=short reads via windows (no
    periodic); otherwise on_chunk dones step chunk from the segment start and each first
    done>=pp_next records done with pp_next=done+every (drift accumulates, exactly as the
    engine does); root_at/turn_at record at their segment ends. Returns sorted positions."""
    pts, pp, at = set(), resume + every, resume
    for to in (root_at, turn_at, n - 1):
        if to is None or not tail_is_int(to) or to <= at:
            continue
        if to - at > short:
            d = at + chunk
            while True:
                done = d if d < to else to
                if done >= pp:
                    pts.add(done)
                    pp = done + every
                if done == to:
                    break
                d += chunk
        if to == root_at or to == turn_at:
            pts.add(to)
        at = to
    return sorted(pts)


def tail_matching(cands, ids_a, ids_b):
    """Candidate positions (prefixes of ids_a) that match ids_b: c <= LCP. Max is the
    engine's longest matching checkpoint."""
    l = tail_lcp(ids_a, ids_b)
    return sorted(c for c in cands if tail_is_int(c) and 0 < c <= l)


def tail_mutated_proof(p1, p2, p3, p4, turn_id, root, k1, k2, k3,
                       every=TAIL_EVERY, chunk=TAIL_PREFILL, short=TAIL_SHORT, cap=16):
    """Independently prove K_exp is the newest checkpoint below the change (Sol design:
    middle assistant mutated near content start). Derives every request's chain
    candidates with tail_simulate (no fixed due grid, no invented root), prunes past
    each actual resume exactly like generate.cpp, and takes the maximum actually
    matching length <= lcp. Returns dict(ok, reason, K_exp, ...); caller fails setup
    unless ok. No-eviction is shown with CONSERVATIVE upper bounds for all three
    requests (exact identity sets never gain invented positions): inherited
    below-root checkpoints when resuming root (at most floor(root/every)+1) plus 1/2/3
    reserved omitted request-end positions for C1/C2/C3; any bound reaching cap fails."""
    out = {"ok": False}
    n1, n2, n3 = len(p1), len(p2), len(p3)
    G1 = tail_last_turn(p1, turn_id)
    if G1 is None or not (0 < G1 < n1):
        return {**out, "reason": f"P1 has no interior last turn (G1={G1}, n1={n1})"}
    if root is None or not (tail_is_int(root) and 0 < root < G1):
        return {**out, "reason": f"no genuine root below G1 ({root!r})"}
    if k1 not in (0, root):
        return {**out, "reason": f"unexpected T1 resume k1={k1} (fresh marker admits only 0/root)"}
    if p3[G1] != turn_id or tail_lcp(p1, p3) <= G1:
        return {**out, "reason": f"G1={G1} is not a shared turn boundary of p1/p3"}
    l14 = tail_lcp(p1, p4)
    if not (l14 < n1):  # initial tail excluded: any request-end tail holds the FULL prompt
        return {**out, "reason": f"lcp(p1,p4)={l14} covers all of P1 (n1={n1}): tail unexcluded"}
    if not (G1 < l14):
        return {**out, "reason": f"change must start after the turn (G1={G1}, l14={l14})"}
    C1 = tail_simulate(n1, k1, root if k1 == 0 else -1, G1, every, chunk, short)
    if G1 not in C1:
        return {**out, "reason": f"L1 {G1} not in T1 candidates {C1}"}
    inh = (root // every + 1) if k1 == root else 0  # overcount is fine; never inserted
    b1 = len(C1) + inh + 1
    if not (b1 < cap):
        return {**out, "reason": f"T1 conservative count {b1} (exact {len(C1)}+inh {inh}+1) >= cap {cap}",
                "bounds": {"b1": b1, "inh": inh}}
    l12 = tail_lcp(p1, p2)
    m1 = tail_matching(C1, p1, p2)
    if not m1:
        return {**out, "reason": "nothing of T1 matches P2"}
    K2, L2 = max(m1), tail_last_turn(p2, turn_id)
    if L2 is None or not (0 < L2 < n2) or p3[L2] != turn_id or tail_lcp(p2, p3) < L2:
        return {**out, "reason": f"L2={L2} is not a shared turn boundary of p2/p3"}
    if not (n2 - k2 < every):
        return {**out, "reason": f"T2 suffix from actual k2 {n2 - k2} >= every {every}"}
    C2 = sorted({c for c in C1 if c <= k2 and c <= l12} | set(tail_simulate(n2, k2, -1, L2, every, chunk, short)))
    b2 = len(C2) + inh + 2
    if not (b2 < cap):
        return {**out, "reason": f"T2 conservative count {b2} (exact {len(C2)}+inh {inh}+2) >= cap {cap}",
                "bounds": {"b1": b1, "b2": b2, "inh": inh}}
    l23 = tail_lcp(p2, p3)
    m2 = tail_matching(C2, p2, p3)
    if not m2:
        return {**out, "reason": "nothing matches P3"}
    K3, L3 = max(m2), tail_last_turn(p3, turn_id)
    if L3 is None or not (L2 < L3 < len(p3)):
        return {**out, "reason": f"need later generation turn L3={L3} past L2={L2}"}
    if not (n3 - k3 < every):
        return {**out, "reason": f"T3 suffix from actual k3 {n3 - k3} >= every {every}"}
    C3 = sorted({c for c in C2 if c <= k3 and c <= l23} | set(tail_simulate(n3, k3, -1, L3, every, chunk, short)))
    b3 = len(C3) + inh + 3
    if not (b3 < cap):
        return {**out, "reason": f"T3 conservative count {b3} (exact {len(C3)}+inh {inh}+3) >= cap {cap}",
                "bounds": {"b1": b1, "b2": b2, "b3": b3, "inh": inh}}
    change = tail_lcp(p3, p4)
    if not (G1 < change < L2):
        return {**out, "reason": f"need G1={G1} < change={change} < L2={L2}"}
    m3 = tail_matching(C3, p3, p4)
    if not m3:
        return {**out, "reason": "nothing matches the mutated prompt"}
    K4 = max(m3)
    if not (K4 > root):
        return {**out, "reason": f"K_exp={K4} not greater than root={root}"}
    if K4 != G1:
        return {**out, "reason": f"newest matching {K4} != L1 {G1}: premise broken for this prefix"}
    return {**out, "ok": True, "reason": "candidate set proven: newest below change is L1",
            "L1": G1, "L2": L2, "L3": L3, "change": change, "K_exp": K4, "K2": K2, "K3": K3,
            "n1": n1, "every": every, "C1": C1,
            "bounds": {"b1": b1, "b2": b2, "b3": b3, "inh": inh}}


def tail_render_ids(body, template, tok):
    """Faithful ids for an actually-sent body (mirrors Service.prepare without vision)."""
    import sys as _sys
    saved = list(_sys.path)
    try:
        # Callers in tests inject template/tok and a pre-normalized body; here body is the
        # OpenAI dict, so normalize with the serving frontend available to this source tree.
        for cand in (str(SRC_ACCEPT), str(SRC_ACCEPT / "tools")):
            if cand not in _sys.path:
                _sys.path.insert(0, cand)
        from serve.frontend import openai_to_messages as _o2m
        messages, tools, kw = _o2m(body)
    finally:
        _sys.path[:] = saved
    prompt = template.render(messages, tools=tools, **kw)
    return tok.encode(prompt, parse_special=True), messages, tools, kw


def tail_load_faithful():
    """CPU-only load of the pack tokenizer + template the server uses. No GPU/server."""
    import sys as _sys
    detail = {}
    try:
        cfg = json.loads(PROD_CFG.read_text(encoding="utf-8-sig"))
        tdir = Path(cfg.get("tokenizer") or "")
    except Exception as e:
        return {"ok": False, "reason": f"prod cfg tokenizer unreadable: {e}"}
    if not tdir.is_dir():
        tdir = ROOT / "strata-work" / "pack-qwen-iq3_s" / "tokenizer"
    detail["tokenizer_dir"] = str(tdir)
    try:
        saved = list(_sys.path)
        for cand in (str(SRC_ACCEPT), str(SRC_ACCEPT / "tools")):
            if cand not in _sys.path:
                _sys.path.insert(0, cand)
        from serve.frontend import ChatTemplate as _CT
        import strata_tokenizer as _ST
        vocab = json.loads((tdir / "vocab.json").read_text(encoding="utf-8"))
        tokens = [None] * len(vocab)
        for t, i in vocab.items():
            tokens[i] = t
        merges = (tdir / "merges.txt").read_text(encoding="utf-8").split("\n")
        types = json.loads((tdir / "token_type.json").read_text(encoding="utf-8"))
        tok = _ST.Tokenizer(tokens, merges, types)
        tpath = tdir / "chat_template.jinja"
        if not tpath.exists():
            tpath = SRC_ACCEPT / "serve" / "chat_template.jinja"
        template = _CT(tpath)
        enc = tok.encode("<|im_start|>", parse_special=True)
        if len(enc) != 1:  # never guess: the oracle needs the exact single turn token
            return {"ok": False, "reason": f"<|im_start|> is not one token: {enc}", "detail": detail}
        detail.update(template=str(tpath), vocab=len(tokens), turn_id=enc[0])
        return {"ok": True, "tok": tok, "template": template, "turn_id": enc[0], "detail": detail}
    except Exception as e:
        return {"ok": False, "reason": f"faithful load failed: {e}", "detail": detail}
    finally:
        try:
            _sys.path[:] = saved
        except Exception:
            pass


def tail_build_long_user(rep, marker, n_sections=750):
    import random
    rnd = random.Random(1000 + rep)
    filler = [f"第{i}節。倉庫の在庫記録によると、区画{rnd.randint(1, 999)}の箱は{rnd.randint(1, 99)}個で、点検は{rnd.randint(1, 28)}日に行われた。"
              for i in range(n_sections)]
    head = f"（合言葉: TAILMARKER-{rep}-{marker}号。覚えておいて）ローグライクの食料システムについて、設計上の注意点を2行で。\n"
    return head + "\n".join(filler) + "\n\n上記文書を踏まえ、要点を2行で答えて。"


def case_tail(eng, rep):
    import time as _time
    out = []
    faithful = tail_load_faithful()
    if not faithful.get("ok"):
        r = {"tag": f"tail.r{rep}.faithful", "pass": False,
             "expect": "faithful pack tokenizer+template required for independent LCP",
             "reason": faithful.get("reason", "faithful unavailable")}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        return [r]
    tok, template, turn_id = faithful["tok"], faithful["template"], faithful["turn_id"]
    tok_detail = dict(faithful.get("detail") or {})
    tok_detail["chunk"] = TAIL_PREFILL  # forced via TAIL_ISOLATION; simulation assumes it
    tok_detail["short"] = TAIL_SHORT
    marker = f"{rep}-{_time.time_ns() % 1000000:06d}"

    def render(body):
        ids, _, _, _ = tail_render_ids(body, template, tok)
        return ids

    def fail(rec):
        print(json.dumps({k: v for k, v in rec.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
        return [rec]

    def p1_at(n):
        u = tail_build_long_user(rep, marker, n)
        b = tail_body_with([{"role": "user", "content": u}], 48)
        return b, render(b)

    def synth_proof(b, ids):
        """Full candidate-set proof on synthetic follow-ups (no engine answers needed:
        header geometry is content-independent). Assumes healthy resumes."""
        rt = tail_genuine_root(ids, turn_id, TAIL_ROOT_THRESH)
        L1t = tail_last_turn(ids, turn_id)
        if rt is None or L1t is None or not (rt < L1t):
            return None, f"bad boundaries root={rt} L1={L1t}"
        u = tail_build_long_user(rep, marker, n_sec)
        aS = {"role": "assistant", "content": TAIL_SYNTH_A}
        p2s = render(tail_body_with([{"role": "user", "content": u}, aS,
                                     {"role": "user", "content": TAIL_SYNTH_U}], 48))
        a2S = {"role": "assistant", "content": TAIL_SYNTH_A}
        p3s = render(tail_body_with([{"role": "user", "content": u}, aS,
                                     {"role": "user", "content": TAIL_SYNTH_U}, a2S,
                                     {"role": "user", "content": TAIL_U3}], 48))
        aSm = {"role": "assistant", "content": tail_mutate_early(TAIL_SYNTH_A)}
        p4s = render(tail_body_with([{"role": "user", "content": u}, aSm,
                                     {"role": "user", "content": TAIL_SYNTH_U}, a2S,
                                     {"role": "user", "content": TAIL_U3}], 48))
        k1a = 0 if (rep == 0 and not _TAIL_PREV_P1) else rt
        C1s = tail_simulate(len(ids), k1a, rt if k1a == 0 else -1, L1t)
        m1s = tail_matching(C1s, ids, p2s)
        if not m1s:
            return None, "synth: nothing of T1 matches P2"
        k2a = max(m1s)
        L2s = tail_last_turn(p2s, turn_id)
        if L2s is None:
            return None, "synth: P2 has no last turn"
        C2s = sorted({c for c in C1s if c <= k2a and c <= tail_lcp(ids, p2s)} |
                      set(tail_simulate(len(p2s), k2a, -1, L2s)))
        m2s = tail_matching(C2s, p2s, p3s)
        if not m2s:
            return None, "synth: nothing matches P3"
        k3a = max(m2s)
        return tail_mutated_proof(ids, p2s, p3s, p4s, turn_id, rt, k1a, k2a, k3a), None

    # Size the long prefix via faithful ids (CPU-only; no server yet): adaptive/binary
    # search on the monotonic section count, then bounded synth-proof fine adjustment
    # (a stray sim periodic inside the ~dozens-wide match window is positional and rare;
    # stepping the section count moves the window far away).
    n_sec, p1_body, p1_ids = 750, None, None
    n_sec, size, note = tail_find_sections(lambda n: len(p1_at(n)[1]))
    setup_note = "" if note == "ok" else note
    if note == "ok":
        for _ in range(4):
            b, ids = p1_at(n_sec)
            if not (TAIL_LO <= len(ids) <= TAIL_HI):
                setup_note = f"re-measure {len(ids)} outside {TAIL_LO}..{TAIL_HI}"
                break
            pr, perr = synth_proof(b, ids)
            if perr is not None:
                setup_note = perr
                n_sec = max(50, n_sec - 40)
                continue
            if not pr.get("ok"):
                setup_note = "synth proof: " + str(pr.get("reason"))
                n_sec = max(50, n_sec - 40)
                continue
            p1_body, p1_ids = b, ids
            setup_note = ""
            break
    if p1_body is None or not (TAIL_LO <= len(render(p1_body)) <= TAIL_HI):
        # Fail BEFORE any GPU request: wrong tokenizer/template/sizing must not burn server time.
        p1_body = tail_body_with([{"role": "user", "content": tail_build_long_user(rep, marker, n_sec)}], 48)
        p1_ids = render(p1_body)
        return fail({"tag": f"tail.r{rep}.T1.setup", "pass": False, "marker": marker, "n_sections": n_sec,
                         "prompt_ids": len(p1_ids), "tok_detail": tok_detail, "isolation": TAIL_ISOLATION,
                         "expect": f"faithful prefix {TAIL_LO}..{TAIL_HI} tokens before any GPU request",
                         "reason": setup_note or f"faithful size {len(p1_ids)} outside {TAIL_LO}..{TAIL_HI}"})
    L1_boundary = tail_last_turn(p1_ids, turn_id)
    root_est = tail_genuine_root(p1_ids, turn_id, TAIL_ROOT_THRESH)
    if root_est is None:
        return fail({"tag": f"tail.r{rep}.T1.setup", "pass": False, "marker": marker, "prompt_ids": len(p1_ids),
                         "tok_detail": tok_detail, "reason": "no genuine root (first turn after 0, >=threshold, before last boundary)"})
    if L1_boundary is None or not (root_est < L1_boundary):
        return fail({"tag": f"tail.r{rep}.T1.setup", "pass": False, "marker": marker, "prompt_ids": len(p1_ids),
                         "tok_detail": tok_detail, "reason": f"bad boundaries root={root_est} L1={L1_boundary}"})
    # Cross-rep guard: the unique head marker must precede every near-tail checkpoint, so this
    # rep cannot inherit a previous rep's long-prefix cache. Earlier reps diverge at the head.
    cross = None
    for pr, pp in _TAIL_PREV_P1.items():
        xc = tail_lcp(pp, p1_ids)
        cross = xc if cross is None or xc > cross else cross
        if xc >= L1_boundary:
            return fail({"tag": f"tail.r{rep}.T1.setup", "pass": False, "marker": marker,
                             "reason": f"cross-rep LCP {xc} >= L1 {L1_boundary}: marker not unique/early"})
    _TAIL_PREV_P1[rep] = list(p1_ids)

    r1, m1 = eng.req(p1_body, f"tail.r{rep}.T1")
    r1.update(tag=f"tail.r{rep}.T1", marker=marker, n_sections=n_sec, cross_rep_lcp=cross,
              prompt_ids=len(p1_ids), L1_boundary=L1_boundary, root_est=root_est,
              isolation=TAIL_ISOLATION, turn_id=turn_id, tok_detail=tok_detail,
              expect=f"prefix {TAIL_LO}..{TAIL_HI} tokens (engine prompt==len(ids))")
    r1["prefix_ok"] = tail_prefix_ok(r1.get("prompt"))
    okc, whyc = tail_check_counts(r1, len(p1_ids))
    k1ok = r1.get("k") in (0, root_est)
    r1["pass"] = bool(r1["prefix_ok"] and okc and k1ok)
    if not r1["pass"]:
        r1["reason"] = (f"prompt={r1.get('prompt')} outside {TAIL_LO}..{TAIL_HI}" if not r1["prefix_ok"]
                        else whyc if not okc else f"k1={r1.get('k')} not in (0, root {root_est})")
    print(json.dumps({k: v for k, v in r1.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
    out.append(r1)
    if not r1["pass"]:
        return out
    a1 = assistant_of(m1)

    # T2: same long prefix + assistant + small follow-up; must resume at L1 (turn-boundary).
    b2 = tail_body_with([{"role": "user", "content": tail_build_long_user(rep, marker, n_sec)}, a1,
                    {"role": "user", "content": TAIL_U2}], 48)
    p2_ids = render(b2)
    if not (len(p2_ids) - L1_boundary < TAIL_EVERY - TAIL_TOL):
        r2s = {"tag": f"tail.r{rep}.T2.setup", "pass": False, "prompt_ids": len(p2_ids),
               "reason": f"T2 suffix {len(p2_ids) - L1_boundary} >= every-tol: periodic in T2 unexcluded"}
        print(json.dumps(r2s, ensure_ascii=False), flush=True)
        out.append(r2s)
        return out
    exp2 = tail_expected_reuse(p1_ids, p2_ids, turn_id)
    if not exp2.get("consistent"):
        r2s = {"tag": f"tail.r{rep}.T2.setup", "pass": False, "prompt_ids": len(p2_ids),
               "reason": f"inconsistent expectation boundary={exp2['boundary']} > LCP L={exp2['L']}"}
        print(json.dumps(r2s, ensure_ascii=False), flush=True)
        out.append(r2s)
        return out
    r2, m2 = eng.req(b2, f"tail.r{rep}.T2")
    ok2, why2 = tail_check_turn(r2, exp2["L"], exp2["suffix"], len(p2_ids), exp2["boundary"])
    r2.update(tag=f"tail.r{rep}.T2", exp_L=exp2["L"], exp_suffix=exp2["suffix"], method=exp2["method"],
              boundary=exp2["boundary"], lcp=exp2["lcp"], prompt_ids=len(p2_ids),
              expect=f"resume chain near L={exp2['L']} ({exp2['method']}), read<=suffix+{TAIL_TOL}")
    r2["pass"], r2["reason"] = bool(ok2), why2
    print(json.dumps({k: v for k, v in r2.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
    out.append(r2)
    if not r2["pass"]:
        return out
    a2 = assistant_of(m2)

    # T3: append again; must resume at L2 (P2's last turn boundary).
    b3 = tail_body_with([{"role": "user", "content": tail_build_long_user(rep, marker, n_sec)}, a1,
                    {"role": "user", "content": TAIL_U2}, a2, {"role": "user", "content": TAIL_U3}], 48)
    p3_ids = render(b3)
    L2 = tail_last_turn(p2_ids, turn_id)
    if L2 is None or not (len(p3_ids) - L2 < TAIL_EVERY - TAIL_TOL):
        r3s = {"tag": f"tail.r{rep}.T3.setup", "pass": False, "prompt_ids": len(p3_ids),
               "reason": f"T3 suffix vs L2={L2} not shorter than every-tol: periodic in T3 unexcluded"}
        print(json.dumps(r3s, ensure_ascii=False), flush=True)
        out.append(r3s)
        return out
    exp3 = tail_expected_reuse(p2_ids, p3_ids, turn_id)
    if not exp3.get("consistent"):
        r3s = {"tag": f"tail.r{rep}.T3.setup", "pass": False, "prompt_ids": len(p3_ids),
               "reason": f"inconsistent expectation boundary={exp3['boundary']} > LCP L={exp3['L']}"}
        print(json.dumps(r3s, ensure_ascii=False), flush=True)
        out.append(r3s)
        return out
    r3, m3 = eng.req(b3, f"tail.r{rep}.T3")
    ok3, why3 = tail_check_turn(r3, exp3["L"], exp3["suffix"], len(p3_ids), exp3["boundary"])
    r3.update(tag=f"tail.r{rep}.T3", exp_L=exp3["L"], exp_suffix=exp3["suffix"], method=exp3["method"],
              boundary=exp3["boundary"], lcp=exp3["lcp"], L2=L2, prompt_ids=len(p3_ids),
              expect=f"resume chain near L={exp3['L']} ({exp3['method']}), read<=suffix+{TAIL_TOL}")
    r3["pass"], r3["reason"] = bool(ok3), why3
    print(json.dumps({k: v for k, v in r3.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
    out.append(r3)
    if not r3["pass"]:
        return out

    # M4 mutated (Sol design): mutate the middle ASSISTANT a1 near the start of its content.
    # L1 is the start of that assistant history turn; T2/T3 end/boundary checkpoints and the
    # initial request tail are all beyond the early change, so the newest match must be L1.
    a1m = dict(a1)
    a1m["content"] = tail_mutate_early(a1.get("content") or "")
    b4 = tail_body_with([{"role": "user", "content": tail_build_long_user(rep, marker, n_sec)}, a1m,
                    {"role": "user", "content": TAIL_U2}, a2, {"role": "user", "content": TAIL_U3}], 48)
    p4_ids = render(b4)
    proof = tail_mutated_proof(p1_ids, p2_ids, p3_ids, p4_ids, turn_id, root_est,
                               r1["k"], r2["k"], r3["k"])
    if not proof.get("ok"):
        r4s = {"tag": f"tail.r{rep}.M4mut.setup", "pass": False, "prompt_ids": len(p4_ids),
               "proof": {k: v for k, v in proof.items() if k != "ok"},
               "expect": "prove K_exp=L1 is the newest checkpoint below the change before any GPU request",
               "reason": f"setup unproven: {proof.get('reason')}"}
        print(json.dumps(r4s, ensure_ascii=False), flush=True)
        out.append(r4s)
        return out
    K_exp = proof["K_exp"]
    r4, _ = eng.req(b4, f"tail.r{rep}.M4mut")
    ok4, why4 = tail_check_mutated(r4, K_exp, root_est, proof["change"], len(p4_ids))
    r4.update(tag=f"tail.r{rep}.M4mut", K_exp=K_exp, root=root_est, change_pos=proof["change"],
              L1=proof["L1"], L2=proof["L2"], lcp_p1p4=tail_lcp(p1_ids, p4_ids), n1=proof["n1"],
              every=proof["every"], bounds=proof.get("bounds"), prompt_ids=len(p4_ids), mutation="assistant-a1-early",
              expect=f"resume chain at newest checkpoint below change: K_exp=L1={K_exp} above root={root_est}")
    r4["pass"], r4["reason"] = bool(ok4), why4
    print(json.dumps({k: v for k, v in r4.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
    out.append(r4)
    return out


# --- twolines acceptance (Muse-2; ADDITIONS only, existing cases untouched) ---
# Two alternating conversations (agent line A + chat line B) on one engine. Brief:
# briefs/BRIEF-TWO-LINES.md. GPU run: --variant cached only, isolated dev engine on
# 18101 (never strata-prod*, deploy-strata, or port 18100).
# Runner overrides for the two-lines engine worktree (Muse-1 branch `two-lines`):
#   ACCEPT_EXE=<worktree>\build-strata-030\strata.exe ACCEPT_SRC=<worktree>\strata-030
# (the code below only reads the existing ACCEPT_EXE/ACCEPT_SRC selection; nothing hardcoded.)
#
# Oracle (independent, faithful-local, CPU): every actually-sent body is re-rendered
# with the same openai_to_messages + pack template + pack tokenizer the server uses
# (tail_render_ids/tail_load_faithful; no GPU/server). Per line, the suffix is
# measured ONLY against the previous SAME-line prompt: exp_L = LCP(prev,cur) with
# the previous stable last-im_start boundary required at or below it
# (tail_expected_reuse, reused unchanged); exp_suffix = len(cur)-exp_L. Pass needs
# read <= exp_suffix+TWOLINES_TOL (fixed 256: a 28K re-read can never hide in it)
# plus shared metric validation (prompt==len(ids), reused==k, read==prompt-k,
# suffix==read, non-bool non-negative ints, 0<=k<prompt, explicit known source).
# No k<=L upper bound: a verified longer generation tip (past the previous raw
# prompt length) legitimately reads LESS. Any resume source (tip/chain/root) is
# accepted inside the read bound (tips expected after alternation); missing/wrong
# records fail, never silent-pass. Follow-up expectations are computed BEFORE the
# request from the actually-to-be-sent body; an inconsistent same-line
# boundary/LCP fails setup before eng.req.
# Histories use only text-level transcripts: the actual returned assistant message
# (content + tool_calls + reasoning_content preserved) plus deterministic tool
# results; raw generated token ids are never manufactured as history. When the
# engine emits no tool_calls on line A, the actual response is kept and ONE explicit
# synthetic assistant tool_call + matching tool result is appended (flagged
# synth_toolcall) so every A prompt stays agent-shaped and ends with a tool result,
# like the Hermes line in production.
TWOLINES_TOL = 256
TWOLINES_CYCLES = 6
TWOLINES_MAX_TOKENS = 64
TWOLINES_A_LO, TWOLINES_A_HI = 28000, 32000        # A first prompt band (~30K)
TWOLINES_B_LO, TWOLINES_B_HI = 14000, 16000        # B first prompt band (~15K)
# Isolation, appended LAST so its options win. Tips ON (8 GiB LRU) because the fix
# under test is tip capture/restore across lines; root disk OFF (no cross-run
# roots); chain 16 / root-threshold 1024 / periodic 4096; prefill chunk 2048 +
# short-read 64 (same as tail so window behaviour is comparable). The upstream
# conversation parking cache is OPT-IN (generate.cpp: conversation_cache_mib
# default 0 = off, documented in usage; mutually exclusive with SavedState
# capture/restore) and is passed as --conversation-cache-mib 0 explicitly so an
# inherited/opted-in parking configuration can never mask this case.
TWOLINES_ISOLATION = ["--tip-cache-gib", "8", "--no-root-disk", "--prompt-cache", "16",
                      "--prompt-cache-root", "1024", "--prompt-cache-every", "4096",
                      "--prefill", "2048", "--short-read", "64",
                      "--conversation-cache-mib", "0"]
TWOLINES_SEED_N = 8             # synthetic tool exchanges seeding A before cycle 0
TWOLINES_TOOL = "read_file"     # must exist in BODY tools (proven in setup)
TWOLINES_B_SYS = "You are a helpful assistant answering briefly."
TWOLINES_B_FOLLOW = ["Noted. And the oldest pending bin?",
                     "Understood. Flag any overdue inspections.",
                     "Got it. Summarize the totals in one line.",
                     "Clear. Which bin needs attention first?",
                     "Thanks. Close with the final counts."]
_TWOLINES_PREV = {}             # rep -> {"A": ids, "B": ids} cross-rep guard (CPU-only)


def twolines_filler(n_sec, seed):
    import random as _rnd
    rnd = _rnd.Random(seed)
    return "\n".join(
        f"Ledger entry {i}: bin {rnd.randint(1, 999)} holds {rnd.randint(1, 99)} crates, inspected day {rnd.randint(1, 28)}."
        for i in range(n_sec))


def twolines_b_body(messages, max_tokens=TWOLINES_MAX_TOKENS):
    """B-only body: plain chat (no tools), reasoning low like tail_body_with."""
    b = body_with(messages, max_tokens, system=TWOLINES_B_SYS, tools=False)
    b["reasoning_effort"] = "low"
    eb = b.get("extra_body")
    if isinstance(eb, dict):
        ctk = eb.get("chat_template_kwargs")
        if isinstance(ctk, dict):
            ctk["reasoning_effort"] = "low"
    return b


def twolines_assistant_of(msg):
    """History append: the ACTUAL returned message (never manufactured token ids)."""
    a = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("reasoning_content"):
        a["reasoning_content"] = msg["reasoning_content"]
    if msg.get("tool_calls"):
        a["tool_calls"] = msg["tool_calls"]
    return a


def twolines_seed_messages(rep, marker, per_result):
    msgs = []
    for i in range(TWOLINES_SEED_N):
        cid = f"call_A_seed_r{rep}_{i}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": TWOLINES_TOOL,
             "arguments": json.dumps({"path": f"seed-{rep}-{i}.log"})}}]})
        head = f"[TWOLINES r{rep} lineA seed {marker}.] " if i == 0 else ""
        msgs.append({"role": "tool", "tool_call_id": cid,
                     "content": head + twolines_filler(per_result, 9000 + rep * 1000 + 10 + i)})
    return msgs


def twolines_a0_msgs(rep, marker, per_result):
    msgs = [{"role": "user", "content": f"(TWOLINES r{rep} lineA cyc0 {marker}.) Begin the warehouse audit: list the first pending crate briefly."}]
    msgs += twolines_seed_messages(rep, marker, per_result)
    return msgs


def twolines_a0_body(rep, marker, per_result):
    return tail_body_with(twolines_a0_msgs(rep, marker, per_result), TWOLINES_MAX_TOKENS)


def twolines_b0_msgs(rep, marker, n_sec):
    u = f"(TWOLINES r{rep} lineB {marker}.) Summarize this ledger in two lines.\n" + twolines_filler(n_sec, 9000 + rep * 1000 + 30)
    return [{"role": "user", "content": u}]


def twolines_b0_body(rep, marker, n_sec):
    return twolines_b_body(twolines_b0_msgs(rep, marker, n_sec), TWOLINES_MAX_TOKENS)


def twolines_a_next(prev_msgs, m, rep, marker, cyc):
    """Advance line A: actual assistant message + tool result(s); prompt ends role tool.

    The next audit instruction rides inside the first tool result (never a trailing
    user message), so every sent A body ends with role tool like the Hermes line."""
    nxt = list(prev_msgs) + [twolines_assistant_of(m)]
    calls = (m.get("tool_calls") or [])
    synth = False
    if not calls:
        synth = True
        cid = f"call_A_synth_r{rep}_{cyc}"
        nxt.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": TWOLINES_TOOL,
             "arguments": json.dumps({"path": f"synth-{rep}-{cyc}.log"})}}]})
        calls = [{"id": cid}]
    instr = f"(TWOLINES r{rep} lineA cyc{cyc + 1}.) Next audit step: confirm briefly.\n"
    for j, c in enumerate(calls):
        cid = c.get("id") or f"call_A_r{rep}_{cyc}_{j}"
        nsec = 70 + 10 * cyc if j == 0 else 0  # growth budget on the first result only
        content = instr + twolines_filler(nsec, 9000 + rep * 1000 + 20 + cyc) if j == 0 else "ok"
        nxt.append({"role": "tool", "tool_call_id": cid, "content": content})
    return nxt, synth, len(calls)


def twolines_b_next(prev_msgs, m, rep, cyc):
    nxt = list(prev_msgs) + [twolines_assistant_of(m)]
    nxt.append({"role": "user", "content": f"(TWOLINES r{rep} lineB cyc{cyc + 1}.) {TWOLINES_B_FOLLOW[cyc]}"})
    return nxt


# Engine resume sources this case accepts: the exact generate.cpp enum strings
# (none = cold start, e.g. `resume from none at 0`; chain/tip/root; root-disk is an
# admitted metric though this case's isolation disables disk). Anything else fails.
TWOLINES_SOURCES = ("none", "chain", "tip", "root", "root-disk")
TWOLINES_A_END_LO, TWOLINES_A_END_HI = 38000, 44000  # synthetic full-growth A band (~40K)


def twolines_check_shape(msgs, line):
    """Transcript-shape gate, enforced before EVERY sent body (both lines, both
    tool-call branches): A ends role tool with every tool_call_id matching an
    assistant call; B holds only plain user/assistant roles."""
    if not msgs:
        return False, "empty history"
    if line == "A":
        if msgs[-1].get("role") != "tool":
            return False, f"A must end role tool, ends {msgs[-1].get('role')!r}"
        call_ids = set()
        for x in msgs:
            for t in (x.get("tool_calls") or []):
                i = t.get("id")
                if i:
                    call_ids.add(i)
        for x in msgs:
            if x.get("role") == "tool" and x.get("tool_call_id") not in call_ids:
                return False, f"tool result {x.get('tool_call_id')!r} matches no assistant call"
        return True, "A ends tool, call ids match"
    for x in msgs:
        if x.get("role") not in ("user", "assistant"):
            return False, f"B has non-plain role {x.get('role')!r}"
        if x.get("tool_calls") or x.get("role") == "tool":
            return False, "B must stay plain user/assistant"
    return True, "B plain user/assistant"


def twolines_synth_final_a(rep, marker, a_per):
    """Synthetic full-growth A body (placeholder assistant tool turns, no engine
    needed) so preflight proves the ~40K final by rendering, not by guessing."""
    msgs = twolines_a0_msgs(rep, marker, a_per)
    for cyc in range(TWOLINES_CYCLES - 1):
        m = {"content": "ok", "reasoning_content": "ok",
             "tool_calls": [{"id": f"call_A_grow_r{rep}_{cyc}", "type": "function",
                             "function": {"name": TWOLINES_TOOL, "arguments": json.dumps({"path": f"grow-{rep}-{cyc}.log"})}}]}
        msgs, _, _ = twolines_a_next(msgs, m, rep, marker, cyc)
    return tail_body_with(msgs, TWOLINES_MAX_TOKENS)


def twolines_check_metrics(rec, ids_len):
    """Defensive engine-metric validation shared by initial + follow-up checks:
    every metric present, non-bool non-negative ints, tail counts, suffix==read,
    0<=k<prompt, explicit known source. Never silent-pass."""
    for k in ("prompt", "reused", "read", "k", "suffix"):
        if rec.get(k) is None:
            return False, f"missing metric: {k}"
    for k in ("prompt", "reused", "read", "k", "suffix"):
        if not tail_is_int(rec[k]) or rec[k] < 0:
            return False, f"bad metric {k}={rec[k]!r} (need non-negative int)"
    ok, why = tail_check_counts(rec, ids_len)
    if not ok:
        return False, why
    if rec["suffix"] != rec["read"]:
        return False, f"suffix={rec['suffix']} != read={rec['read']}"
    if not (0 <= rec["k"] < rec["prompt"]):
        return False, f"k={rec['k']} outside 0<=k<prompt={rec['prompt']}"
    src = rec.get("resume")
    if not src or src not in TWOLINES_SOURCES:
        return False, f"missing/unknown source resume={src!r} (need one of {TWOLINES_SOURCES})"
    return True, f"metrics consistent (source {src})"


def twolines_check_follow(rec, exp_L, exp_suffix, ids_len, exp_boundary=None, tol=TWOLINES_TOL):
    """Follow-up gate: shared metrics + validated same-line expectation +
    read<=suffix+tol. Any resume source is accepted inside the read bound. No
    k<=L upper bound: a verified longer generation tip (past the previous raw
    prompt length) legitimately reads LESS. Missing/bad metrics fail.
    (resume_lines presence is not required: tip diagnostics may bound it away.)"""
    ok, why = twolines_check_metrics(rec, ids_len)
    if not ok:
        return False, why
    for name, v in (("exp_L", exp_L), ("exp_suffix", exp_suffix)):
        if not tail_is_int(v) or v < 0:
            return False, f"bad expectation {name}={v!r}"
    if exp_suffix != ids_len - exp_L:
        return False, f"exp_suffix={exp_suffix} != ids_len-exp_L={ids_len - exp_L}"
    if exp_boundary is None or not tail_is_int(exp_boundary) or exp_boundary < 0:
        return False, f"missing/invalid stable boundary {exp_boundary!r}"
    if not (exp_boundary <= exp_L):
        return False, f"inconsistent expectation: boundary={exp_boundary} > LCP L={exp_L}"
    if rec["read"] > exp_suffix + tol:
        return False, f"read={rec['read']} > independent same-line suffix={exp_suffix}+{tol}"
    return True, f"read within same-line suffix+{tol} (source {rec.get('resume')})"


def twolines_calibrate(render, rep, marker):
    """Bounded deterministic sizing (CPU-only): B filler sections for ~15K, A seed
    per-result sections for ~30K first prompt. Returns (b_n, b_size, a_per, a_size, note)."""
    b_n, b_size, b_note = tail_find_sections(
        lambda n: len(render(twolines_b0_body(rep, marker, n))), TWOLINES_B_LO, TWOLINES_B_HI, 100, 1200)
    if b_note != "ok":
        return (b_n, b_size, 0, 0, f"B sizing unproven: {b_note} (size {b_size})")
    a_per, a_size, a_note = tail_find_sections(
        lambda p: len(render(twolines_a0_body(rep, marker, p))), TWOLINES_A_LO, TWOLINES_A_HI, 20, 200)
    if a_note != "ok":
        return (b_n, b_size, a_per, a_size, f"A sizing unproven: {a_note} (size {a_size})")
    return (b_n, b_size, a_per, a_size, "ok")


def twolines_preflight():
    """CPU-only sizing/setup validation BEFORE any server starts (no GPU here)."""
    faithful = tail_load_faithful()
    if not faithful.get("ok"):
        return {"ok": False, "reason": faithful.get("reason", "faithful unavailable")}
    tok, template, turn_id = faithful["tok"], faithful["template"], faithful["turn_id"]

    def render(body):
        return tail_render_ids(body, template, tok)

    try:
        ids0, _, tools0, _ = render(tail_body_with([{"role": "user", "content": "hi"}], TWOLINES_MAX_TOKENS))
    except Exception as e:
        return {"ok": False, "reason": f"agent base render failed: {e}"}
    names = [(t.get("name") if isinstance(t, dict) else t) for t in (tools0 or [])]
    if not tools0 or TWOLINES_TOOL not in names:
        return {"ok": False, "reason": f"tool shape unproven: {len(tools0 or [])} tools, need {TWOLINES_TOOL!r}"}
    if not (10500 <= len(ids0) <= 13000):
        return {"ok": False, "reason": f"agent base {len(ids0)} outside 10500..13000 (system+tool schema ~11.7K expected)"}
    if tail_last_turn(ids0, turn_id) is None:
        return {"ok": False, "reason": "agent base has no turn boundary"}
    b_n, b_size, a_per, a_size, note = twolines_calibrate(lambda b: render(b)[0], 0, "preflight")
    if note != "ok":
        return {"ok": False, "reason": note}
    try:
        a_final = len(render(twolines_synth_final_a(0, "preflight", a_per))[0])
    except Exception as e:
        return {"ok": False, "reason": f"synthetic final A render failed: {e}"}
    if not (TWOLINES_A_END_LO <= a_final <= TWOLINES_A_END_HI):
        return {"ok": False, "reason": f"synthetic final A {a_final} outside {TWOLINES_A_END_LO}..{TWOLINES_A_END_HI}"}
    return {"ok": True, "base": len(ids0), "n_tools": len(tools0), "b_n": b_n, "b_size": b_size,
            "a_per": a_per, "a_size": a_size, "a_final": a_final, "turn_id": turn_id,
            "detail": dict(faithful.get("detail") or {})}


class TwolinesEngine(Engine):
    """twolines-only request adapter. Identical HTTP behavior to Engine, but the
    post-DONE log wait polls (bounded) until the CURRENT request's metric pair
    arrives: positive-budget full tips flush AFTER DONE, and start()'s readiness
    probe also leaves a post-DONE pair, so the FIRST resume+prompt pair may be a
    stale probe's. Metrics are paired in log order (every resume line replaces
    the pending resume; the next prompt line completes that pair) and only a
    pair whose prompt length equals the independently rendered expect_prompt is
    accepted; resume from one request is never combined with prompt from
    another. Missing/mismatched metrics by the deadline fail explicitly with the
    seen pairs kept as evidence; counts are never fabricated and no next request
    starts with incomplete metrics. Existing Engine.req is untouched for old
    cases. Port is always the isolated dev 18101 (URL); only text metrics are
    read, no GPU work here."""

    WAIT_S = 60.0
    POLL_S = 0.5

    def req(self, body, tag, expect_prompt=None, _send=None, _now=None, _sleep=None, _deadline=None, _poll=None):
        import time as _time
        now = _now or _time.monotonic
        sleep = _sleep or _time.sleep
        deadline_s = self.WAIT_S if _deadline is None else _deadline
        poll_s = self.POLL_S if _poll is None else _poll
        self.skip()
        t0 = now()
        if _send is None:
            r = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(r, timeout=1800))
        else:
            resp = _send(body)
        wall = now() - t0
        rec = {"tag": tag, "wall_s": round(wall, 2)}
        if expect_prompt is not None:
            rec["expect_prompt"] = expect_prompt
        lines, pairs = [], []
        pend, saw_prompt_line, accepted = None, False, None
        end = now() + deadline_s
        while accepted is None:
            for l in self.new_lines():
                lines.append(l)
                mr = tail_parse_resume(l)
                if mr:
                    pend = mr  # latest resume replaces pending; paired by the next prompt line
                mp = tail_parse_prompt(l)
                if mp:
                    saw_prompt_line = True
                    if pend is not None:
                        pairs.append((pend, mp))
                        if (expect_prompt is None or mp["prompt"] == expect_prompt) and accepted is None:
                            accepted = (pend, mp)
                        pend = None
            if accepted is not None:
                break
            if now() >= end:
                break
            sleep(poll_s)
        rec["wait_s"] = round(now() - t0, 2)
        rec["seen_pairs"] = [{"resume": r["resume"], "k": r["k"], "suffix": r["suffix"],
                              "prompt": p["prompt"], "reused": p["reused"], "read": p["read"]} for r, p in pairs]
        if pend is not None:
            rec["pending_resume"] = {"resume": pend["resume"], "k": pend["k"], "suffix": pend["suffix"]}
        if accepted is not None:
            mr, mp = accepted
            rec.update(resume=mr["resume"], k=mr["k"], suffix=mr["suffix"], restore_ms=mr["restore_ms"])
            if "snapshot_ms" in mr:
                rec["snapshot_ms"] = mr["snapshot_ms"]
            rec.update(prompt=mp["prompt"], reused=mp["reused"], read=mp["read"], prefill_ms=mp["prefill_ms"],
                       gen=mp["gen"], tok_s=mp["tok_s"])
        elif expect_prompt is None:
            missing = "+".join([s for s, h in (("resume", pend is not None or bool(pairs)),
                                                ("prompt", saw_prompt_line)) if not h])
            rec["timeout"] = f"missing {missing} metrics after {deadline_s}s"
        else:
            rec["timeout"] = (f"no pair with prompt {expect_prompt} after {deadline_s}s "
                              f"({len(pairs)} other pair(s), pending={pend is not None})")
        rec["resume_lines"] = [l[:220] for l in lines if "resume" in l or "tip" in l.lower() or "root" in l.lower()][-6:]
        rec["ckpt_lines"] = [l[:220] for l in lines
                             if re.search(r"resume from chain at \d+ \(checkpoint row\)", l)]
        msg = resp["choices"][0]["message"]
        rec["answer"] = (msg.get("content") or "")[:120]
        print(json.dumps({k: v for k, v in rec.items() if k != "resume_lines"}, ensure_ascii=False), flush=True)
        return rec, msg


def case_twolines(eng, rep):
    import time as _time
    out = []
    faithful = tail_load_faithful()
    if not faithful.get("ok"):
        r = {"tag": f"twolines.r{rep}.faithful", "pass": False,
             "expect": "faithful pack tokenizer+template required for independent oracle",
             "reason": faithful.get("reason", "faithful unavailable")}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        return [r]
    tok, template, turn_id = faithful["tok"], faithful["template"], faithful["turn_id"]
    tok_detail = dict(faithful.get("detail") or {})

    def render(body):
        ids, _, _, _ = tail_render_ids(body, template, tok)
        return ids

    def fail(rec):
        # Preserve earlier measured records: append, never discard out.
        print(json.dumps({k: v for k, v in rec.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
        out.append(rec)
        return out

    marker = f"{rep}-{_time.time_ns() % 1000000:06d}"

    b_n, b_size, a_per, a_size, note = twolines_calibrate(render, rep, marker)
    if note != "ok":
        return fail({"tag": f"twolines.r{rep}.setup", "pass": False, "marker": marker,
                     "b_n": b_n, "b_size": b_size, "a_per": a_per, "a_size": a_size,
                     "isolation": TWOLINES_ISOLATION, "tok_detail": tok_detail,
                     "expect": f"faithful A first {TWOLINES_A_LO}..{TWOLINES_A_HI}, B first {TWOLINES_B_LO}..{TWOLINES_B_HI} before any GPU request",
                     "reason": note})
    try:
        a0_ids, _, a0_tools, _ = tail_render_ids(twolines_a0_body(rep, marker, a_per), template, tok)
    except Exception as e:
        return fail({"tag": f"twolines.r{rep}.setup", "pass": False, "marker": marker,
                     "reason": f"A0 agent render failed (tool shape unproven): {e}"})
    names = [(t.get("name") if isinstance(t, dict) else t) for t in (a0_tools or [])]
    if TWOLINES_TOOL not in names:
        return fail({"tag": f"twolines.r{rep}.setup", "pass": False, "marker": marker,
                     "reason": f"A0 tool shape unproven: need {TWOLINES_TOOL!r} in {len(a0_tools or [])} tools"})
    b0_body = twolines_b0_body(rep, marker, b_n)
    b0_ids = render(b0_body)
    if not (TWOLINES_A_LO <= len(a0_ids) <= TWOLINES_A_HI) or not (TWOLINES_B_LO <= len(b0_ids) <= TWOLINES_B_HI):
        return fail({"tag": f"twolines.r{rep}.setup", "pass": False, "marker": marker,
                     "a0": len(a0_ids), "b0": len(b0_ids),
                     "reason": f"re-measure outside bands A {TWOLINES_A_LO}..{TWOLINES_A_HI} / B {TWOLINES_B_LO}..{TWOLINES_B_HI}"})
    boundA0, boundB0 = tail_last_turn(a0_ids, turn_id), tail_last_turn(b0_ids, turn_id)
    if boundA0 is None or boundB0 is None:
        return fail({"tag": f"twolines.r{rep}.setup", "pass": False, "marker": marker,
                     "reason": f"missing stable turn boundary A={boundA0} B={boundB0}"})
    crossA = crossB = None
    for pr, pp in _TWOLINES_PREV.items():
        for line, cur_ids, cur_bound in (("A", a0_ids, boundA0), ("B", b0_ids, boundB0)):
            xc = tail_lcp(pp[line], cur_ids)
            if line == "A":
                crossA = xc if crossA is None or xc > crossA else crossA
            else:
                crossB = xc if crossB is None or xc > crossB else crossB
            if xc >= cur_bound:
                return fail({"tag": f"twolines.r{rep}.setup", "pass": False, "marker": marker,
                             "reason": f"cross-rep LCP {xc} >= {line} boundary {cur_bound}: marker not unique/early"})
    _TWOLINES_PREV[rep] = {"A": list(a0_ids), "B": list(b0_ids)}

    prev = {"A": {"msgs": twolines_a0_msgs(rep, marker, a_per), "ids": a0_ids,
                  "root": tail_genuine_root(a0_ids, turn_id), "synth": False, "ncalls": 0, "kept": False},
            "B": {"msgs": twolines_b0_msgs(rep, marker, b_n), "ids": b0_ids,
                  "root": tail_genuine_root(b0_ids, turn_id), "synth": False, "ncalls": 0, "kept": False}}
    for cyc in range(TWOLINES_CYCLES):
        for line in ("A", "B"):
            st = prev[line]
            ok, why = twolines_check_shape(st["msgs"], line)
            if not ok:
                return fail({"tag": f"twolines.r{rep}.{line}{cyc}.shape", "pass": False, "marker": marker,
                             "reason": f"transcript shape unproven before request: {why}"})
            body = tail_body_with(st["msgs"], TWOLINES_MAX_TOKENS) if line == "A" else twolines_b_body(st["msgs"], TWOLINES_MAX_TOKENS)
            try:
                cur_ids = render(body)
            except Exception as e:
                return fail({"tag": f"twolines.r{rep}.{line}{cyc}.render", "pass": False, "marker": marker,
                             "prev_len": len(st["ids"]),
                             "reason": f"faithful render failed before request: {e}"})
            exp = None
            if cyc > 0:
                exp = tail_expected_reuse(st["ids"], cur_ids, turn_id)
                if not (tail_is_int(exp["L"]) and exp["L"] >= 0 and tail_is_int(exp["suffix"])
                        and exp["suffix"] == len(cur_ids) - exp["L"] and exp.get("consistent")
                        and exp["boundary"] is not None and tail_is_int(exp["boundary"]) and exp["boundary"] >= 0):
                    return fail({"tag": f"twolines.r{rep}.{line}{cyc}.setup", "pass": False, "marker": marker,
                                 "prev_len": len(st["ids"]), "prompt_ids": len(cur_ids),
                                 "exp_L": exp["L"], "exp_suffix": exp["suffix"], "method": exp["method"],
                                 "boundary": exp["boundary"], "lcp": exp["lcp"],
                                 "expect": f"same-line suffix bound (+{TWOLINES_TOL})",
                                 "reason": f"inconsistent same-line boundary/LCP before request: boundary={exp['boundary']} L={exp['L']}"})
            tag = f"twolines.r{rep}.{line}{cyc}"
            try:
                r, m = eng.req(body, tag, expect_prompt=len(cur_ids))
            except Exception as e:
                return fail({"tag": tag, "line": line, "cyc": cyc, "pass": False, "marker": marker,
                             "prev_len": len(st["ids"]),
                             "isolation": TWOLINES_ISOLATION, "turn_id": turn_id,
                             "reason": f"request failed: {e}"})
            base = {"tag": tag, "line": line, "cyc": cyc, "prompt_ids": len(cur_ids),
                    "prev_len": len(st["ids"]), "source": r.get("resume"), "shape": "ok",
                    "used_synth": st["synth"], "used_ncalls": st["ncalls"], "kept_reasoning": st["kept"],
                    "cross_rep_lcp": crossA if line == "A" else crossB,
                    "isolation": TWOLINES_ISOLATION, "turn_id": turn_id, "tok_detail": tok_detail}
            if cyc == 0:
                okc, whyc = twolines_check_metrics(r, len(cur_ids))
                fresh, root = r.get("k"), st["root"]
                fresh_ok = okc and ((fresh == 0) or (root is not None and fresh == root))
                r.update(base, expect="cold/setup exempt (metrics+source+fresh only)")
                r["pass"] = bool(okc and fresh_ok)
                r["reason"] = ("metrics ok; " if okc else f"metrics: {whyc}; ") + (f"fresh k={fresh} (root {root})" if fresh_ok else f"k1={fresh} not in (0, root {root})")
            else:
                ok, why = twolines_check_follow(r, exp["L"], exp["suffix"], len(cur_ids), exp["boundary"])
                r.update(base, exp_L=exp["L"], exp_suffix=exp["suffix"], method=exp["method"],
                         boundary=exp["boundary"], lcp=exp["lcp"], prompt_ids=len(cur_ids),
                         expect=f"read<=same-line suffix+{TWOLINES_TOL} ({exp['method']})")
                r["pass"], r["reason"] = bool(ok), why
            print(json.dumps({k: v for k, v in r.items() if k not in ("resume_lines", "ckpt_lines")}, ensure_ascii=False), flush=True)
            out.append(r)
            if not r["pass"]:
                return out
            if cyc + 1 >= TWOLINES_CYCLES:
                continue  # last cycle per line: no next history to build
            kept = bool((m.get("reasoning_content") or "").strip())
            if line == "A":
                nxt, synth, ncalls = twolines_a_next(st["msgs"], m, rep, marker, cyc)
                st.update(msgs=nxt, ids=cur_ids, synth=synth, ncalls=ncalls, kept=kept)
            else:
                st.update(msgs=twolines_b_next(st["msgs"], m, rep, cyc), ids=cur_ids, kept=kept)
    return out


def run_twolines(a, extra):
    """Dedicated twolines runner: start + TwolinesEngine creation + all reps run
    UNDER one try/finally stop, so even a startup failure stops the server.
    JSONL is always persisted; exit is nonzero unless every record passes (empty
    means failure too). Existing cases are untouched by this branch."""
    res = []
    err = None
    cleanup_err = None
    try:
        log = start(a.variant, extra)
        eng = TwolinesEngine(log)
        for rep in range(a.reps):
            res += case_twolines(eng, rep)
    except SystemExit as e:
        # start() signals startup failure via sys.exit: cleanup still runs, persist explicit failure
        err = f"runner startup failed (exit {e.code})"
    except Exception as e:
        err = f"runner failed: {e}"
    finally:
        try:
            stop()
        except Exception as e:
            cleanup_err = f"cleanup failed: {e}"
    if err is not None:
        r = {"tag": "twolines.runner", "pass": False, "variant": a.variant, "reason": err}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        res.append(r)
    if cleanup_err is not None:
        r = {"tag": "twolines.cleanup", "pass": False, "variant": a.variant, "reason": cleanup_err}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        res.append(r)
    want = 12 * a.reps
    if err is None and all(r.get("pass") for r in res) and len(res) != want:
        r = {"tag": "twolines.count", "pass": False, "variant": a.variant,
             "reason": f"expected {want} measured records, got {len(res)}"}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        res.append(r)
    with open(OUT / f"twolines-{a.variant}.jsonl", "a", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(dict(r, variant=a.variant, t=time.strftime("%H:%M:%S")), ensure_ascii=False) + "\n")
    if not res or any(not r.get("pass") for r in res):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["c1", "c3", "ja", "needle", "tail", "twolines", "serve", "stop"])
    ap.add_argument("--variant", default="cached", choices=["cold", "cached", "prod"])
    ap.add_argument("--extra", default="")
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    if a.what == "stop":
        try:
            stop()
        except Exception as e:
            sys.exit(f"stop failed: {e}")
        return
    extra = a.extra.split()
    if a.what == "tail":
        if a.variant != "cached":  # prod runs the old binary, cold disables the chain: neither tests this branch
            sys.exit("tail requires --variant cached")
        if a.reps < 1:  # zero tests must not pass
            sys.exit("tail needs --reps >= 1")
        extra = extra + list(TAIL_ISOLATION)  # complete list last so its options win
    if a.what == "twolines":
        if a.variant != "cached":  # tips under test; prod runs the old binary, cold disables tips
            sys.exit("twolines requires --variant cached")
        if a.reps < 1:  # zero tests must not pass
            sys.exit("twolines needs --reps >= 1")
        pre = twolines_preflight()  # CPU-only sizing/setup BEFORE starting any server (no GPU here)
        if not pre.get("ok"):
            print(json.dumps({"tag": "twolines.preflight", "pass": False,
                              **{k: v for k, v in pre.items() if k != "ok"}}, ensure_ascii=False), flush=True)
            sys.exit(2)
        print(json.dumps({"tag": "twolines.preflight", "pass": True,
                          **{k: v for k, v in pre.items() if k != "ok"}}, ensure_ascii=False), flush=True)
        extra = extra + list(TWOLINES_ISOLATION)  # complete list last so its options win
        return run_twolines(a, extra)
    log = start(a.variant, extra)
    if a.what == "serve":
        return
    eng = Engine(log)
    fn = {"c1": case1, "c3": case3, "ja": case_ja, "needle": case_needle, "tail": case_tail}[a.what]
    if a.what == "tail":
        try:  # setup/render errors still stop the isolated server and exit nonzero
            res = []
            for rep in range(a.reps):
                res += fn(eng, rep)
        finally:
            try:
                stop()
            except Exception as e:
                print(json.dumps({"tag": "tail.cleanup", "pass": False, "variant": a.variant,
                                  "reason": f"cleanup failed: {e}"}, ensure_ascii=False), flush=True)
                res.append({"tag": "tail.cleanup", "pass": False, "variant": a.variant,
                            "reason": f"cleanup failed: {e}"})
        with open(OUT / f"{a.what}-{a.variant}.jsonl", "a", encoding="utf-8") as f:
            for r in res:
                f.write(json.dumps(dict(r, variant=a.variant, t=time.strftime("%H:%M:%S")), ensure_ascii=False) + "\n")
        if any(not r.get("pass") for r in res):
            sys.exit(1)
        return
    res = []
    for rep in range(a.reps):
        res += fn(eng, rep)
    with open(OUT / f"{a.what}-{a.variant}.jsonl", "a", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(dict(r, variant=a.variant, t=time.strftime("%H:%M:%S")), ensure_ascii=False) + "\n")
    try:
        stop()
    except Exception as e:
        sys.exit(f"stop failed: {e}")


if __name__ == "__main__":
    main()
