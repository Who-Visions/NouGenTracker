#!/usr/bin/env python3
"""Put NouGen//Trckr live at trckr.nougenai.com, with a rollback point and a live smoke test.

Run on a fleet node that holds the Keymaker (stdlib only, plus npx for wrangler):

    python3 workers/trckr/deploy/deploy_prod.py            # deploy + smoke
    python3 workers/trckr/deploy/deploy_prod.py --smoke    # smoke only, nothing changes
    python3 workers/trckr/deploy/deploy_prod.py --rollback # back to the version recorded before the last deploy

Steps: find or create the D1 database, apply migrations, record the live version, deploy
--env production (the custom domain makes the DNS record), load the three secrets, then
check health, prices, the OAuth metadata, the 401 challenge, the Google hand-off and one
priced turn through /mcp. Secrets go from the Keymaker straight into wrangler's stdin;
nothing secret is printed or written to disk. Every name below can be overridden by env.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = HERE.parent
ENV = os.environ.get("TRCKR_ENV", "")  # "" = top level, which is production
ENVARGS = ["--env", ENV] if ENV else []
DB_NAME = os.environ.get("TRCKR_D1_NAME", "nougen-trckr")
BASE = os.environ.get("TRCKR_BASE_URL", "https://trckr.nougenai.com").rstrip("/")
EYE_CALLBACK = os.environ.get("TRCKR_EYE_CALLBACK", "https://eye.nougenai.com/oauth/google/callback")
WRANGLER = os.environ.get("TRCKR_WRANGLER", "npx --yes wrangler@4").split()
STATE = Path(os.environ.get("TRCKR_STATE_DIR", Path.home() / ".nougen" / "trckr")).expanduser()
KEYS = {
    "token": os.environ.get("TRCKR_KM_CF_TOKEN", "CLOUDFLARE_API_TOKEN"),
    "account": os.environ.get("TRCKR_KM_CF_ACCOUNT", "CLOUDFLARE_ACCOUNT_ID"),
    "signing": os.environ.get("TRCKR_KM_SIGNING", "TRCKR_SIGNING_SECRET"),
    "google_id": os.environ.get("TRCKR_KM_GOOGLE_ID", "GOOGLE_OAUTH_CLIENT_ID"),
    "google_secret": os.environ.get("TRCKR_KM_GOOGLE_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET"),
}


def log(msg):
    print(f"[trckr-deploy] {msg}", flush=True)


def die(msg):
    log(f"STOP: {msg}")
    sys.exit(1)


# ---------- Keymaker ----------
def shards_dir():
    for cand in (os.environ.get("NOUGEN_SHARDS_DIR"), Path.home() / "The Observatory" / "NouGen" / "nougenshards", Path.home() / ".nougen" / "src" / "nougenshards"):
        if cand and (Path(cand) / "src" / "nougen_shards").is_dir():
            return Path(cand)
    die("Keymaker not found; set NOUGEN_SHARDS_DIR to the nougenshards checkout")


def keymaker(names):
    root = shards_dir()
    py = next((p for p in (root / ".venv" / "bin" / "python", root / ".venv" / "Scripts" / "python.exe") if p.exists()), Path(sys.executable))
    code = "import sys,json;sys.path.insert(0,'src');from nougen_shards import keymaker as k;print(json.dumps({n:(k.get_secret(n) or '') for n in json.loads(sys.stdin.read())}))"
    out = subprocess.run([str(py), "-c", code], cwd=root, input=json.dumps(names), capture_output=True, text=True)
    line = (out.stdout.strip().splitlines() or [""])[-1]
    try:
        return json.loads(line)
    except ValueError:
        die("Keymaker read failed (output withheld)")


# ---------- wrangler ----------
def wrangler(args, env, cfg, stdin=None, check=True):
    cmd = WRANGLER + args + ["--config", str(cfg)]
    out = subprocess.run(cmd, cwd=WORKER, env=env, input=stdin, capture_output=True, text=True, shell=(os.name == "nt"))
    if check and out.returncode != 0:
        tail = "\n".join((out.stderr or out.stdout).strip().splitlines()[-12:])
        die(f"wrangler {' '.join(args[:3])} failed:\n{tail}")
    return out


def cf_env(km):
    if not km.get(KEYS["token"]) or not km.get(KEYS["account"]):
        die(f"Keymaker is missing {KEYS['token']} or {KEYS['account']}")
    env = dict(os.environ)
    env["CLOUDFLARE_API_TOKEN"] = km[KEYS["token"]]
    env["CLOUDFLARE_ACCOUNT_ID"] = km[KEYS["account"]]
    env["WRANGLER_SEND_METRICS"] = "false"
    return env


def resolved_config(db_id):
    """A throwaway copy of wrangler.jsonc with the production D1 id filled in."""
    text = (WORKER / "wrangler.jsonc").read_text()
    text = text.replace("REPLACE_WITH_PROD_D1_ID", db_id)
    cfg = WORKER / ".wrangler.deploy.jsonc"
    cfg.write_text(text)
    return cfg


def ensure_d1(env):
    cfg = WORKER / "wrangler.jsonc"
    out = wrangler(["d1", "list", "--json"], env, cfg)
    dbs = json.loads(out.stdout[out.stdout.find("[") :])
    hit = next((d for d in dbs if d.get("name") == DB_NAME), None)
    if hit:
        log(f"D1 {DB_NAME} exists ({hit['uuid']})")
        return hit["uuid"]
    wrangler(["d1", "create", DB_NAME], env, cfg)
    out = wrangler(["d1", "list", "--json"], env, cfg)
    hit = next((d for d in json.loads(out.stdout[out.stdout.find("[") :]) if d.get("name") == DB_NAME), None)
    if not hit:
        die("D1 create reported success but the database is not listed")
    log(f"D1 {DB_NAME} created ({hit['uuid']})")
    return hit["uuid"]


def live_version(env, cfg):
    out = wrangler(["deployments", "list", *ENVARGS, "--json"], env, cfg, check=False)
    if out.returncode != 0:
        return None
    try:
        deps = json.loads(out.stdout[out.stdout.find("[") :])
    except ValueError:
        return None
    if not deps:
        return None
    latest = sorted(deps, key=lambda d: d.get("created_on", ""))[-1]
    vs = latest.get("versions") or []
    return vs[0].get("version_id") if vs else None


# ---------- smoke ----------
def http(method, path, body=None, headers=None, follow=True):
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", "accept": "application/json", "user-agent": "nougen-trckr-deploy", **(headers or {})})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_access(signing_secret, email):
    """Same token auth.js signs after Google sign-in: HS256 over sha256('nougen-trckr-oauth:'+secret)."""
    key = hashlib.sha256(f"nougen-trckr-oauth:{signing_secret}".encode()).digest()
    iat = int(time.time())
    head = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64u(json.dumps({"email": email, "kind": "access", "iss": BASE, "iat": iat, "exp": iat + 600}, separators=(",", ":")).encode())
    sig = b64u(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def smoke(signing_secret, email):
    rec = {}
    for _ in range(12):
        s, _, b = http("GET", "/healthz")
        if s == 200:
            break
        time.sleep(10)
    rec["healthz"] = s
    if s != 200:
        die(f"/healthz answered {s}")
    s, _, b = http("GET", "/.well-known/oauth-authorization-server")
    rec["oauth_metadata"] = s
    s, h, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    rec["mcp_unauthed"] = s
    rec["mcp_challenge"] = "resource_metadata" in (h.get("WWW-Authenticate") or h.get("www-authenticate") or "")
    s, h, _ = http("GET", "/login", follow=False)
    loc = h.get("Location") or h.get("location") or ""
    rec["google_handoff"] = s in (302, 303) and "accounts.google.com" in loc and "trckr%7E" in loc.replace("~", "%7E")
    s, h, _ = http("GET", f"{EYE_CALLBACK}?state=trckr~smoke&code=x", follow=False)
    loc = h.get("Location") or h.get("location") or ""
    rec["eye_relay"] = loc.startswith(f"{BASE}/oauth/google/callback")

    # Prices: a cold board triggers the first read; give the pools a minute.
    models = []
    for _ in range(9):
        s, _, b = http("GET", "/v1/prices")
        prov = json.loads(b).get("providers", []) if s == 200 else []
        rec["pools"] = {p["provider"]: len(p["models"]) for p in prov}
        models = [m for p in prov if p["provider"] == "anthropic" for m in p["models"]]
        if models:
            break
        http("GET", "/")
        time.sleep(10)
    if not models:
        die(f"no Anthropic prices read yet: {rec.get('pools')}")

    tok = mint_access(signing_secret, email)
    auth = {"authorization": f"Bearer {tok}"}
    s, _, b = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "trckr-deploy", "version": "1"}}}, auth)
    rec["mcp_initialize"] = s
    model = models[0].get("model") or models[0].get("id")
    turn = {"provider": "anthropic", "model": model, "input_tokens": 1200, "output_tokens": 800, "cache_read_tokens": 30000, "cache_write_5m_tokens": 2000}
    s, _, b = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "trckr_turn_cost", "arguments": turn}}, auth)
    res = (json.loads(b).get("result") or {}) if s == 200 else {}
    sc = res.get("structuredContent") or {}
    rec["priced_turn"] = {"model": model, "status": sc.get("status"), "cost_usd": sc.get("cost_usd"), "source_url": ((sc.get("price") or {}).get("ref") or {}).get("source_url")}
    if sc.get("status") not in ("priced", "partial"):
        die(f"priced turn failed: {rec['priced_turn']}")
    return rec


# ---------- main ----------
def main():
    args = set(sys.argv[1:])
    STATE.mkdir(parents=True, exist_ok=True)
    km = keymaker(list(KEYS.values()))
    signing = km.get(KEYS["signing"]) or ""
    email = (os.environ.get("TRCKR_SMOKE_EMAIL") or "whoentertains@gmail.com").split(",")[0]

    if "--smoke" in args:
        if not signing:
            die(f"--smoke needs {KEYS['signing']} in the Keymaker")
        print(json.dumps({"smoke": smoke(signing, email)}, indent=2))
        return

    if not shutil.which(WRANGLER[0]):
        die(f"{WRANGLER[0]} not on PATH")
    env = cf_env(km)
    db_id = ensure_d1(env)
    cfg = resolved_config(db_id)
    try:
        if "--rollback" in args:
            prev = json.loads((STATE / "last_deploy.json").read_text()).get("previous_version")
            if not prev:
                die("no previous version recorded (first deploy); delete the worker instead if needed")
            wrangler(["rollback", prev, *ENVARGS, "--message", "trckr rollback", "-y"], env, cfg)
            log(f"rolled back to {prev}")
            return

        wrangler(["d1", "migrations", "apply", DB_NAME, "--remote", *ENVARGS], env, cfg, stdin="y\n")
        log("migrations applied")
        prev = live_version(env, cfg)
        log(f"live version before deploy: {prev or 'none (first deploy)'}")

        out = wrangler(["deploy", *ENVARGS], env, cfg)
        version = next((ln.split(":", 1)[1].strip() for ln in out.stdout.splitlines() if "Version ID" in ln), None)
        log(f"deployed version {version}")

        generated = False
        if not signing:
            signing, generated = secrets.token_urlsafe(48), True
        if not km.get(KEYS["google_id"]) or not km.get(KEYS["google_secret"]):
            die(f"Keymaker is missing {KEYS['google_id']} or {KEYS['google_secret']}")
        bulk = json.dumps({"TRCKR_SIGNING_SECRET": signing, "GOOGLE_CLIENT_ID": km[KEYS["google_id"]], "GOOGLE_CLIENT_SECRET": km[KEYS["google_secret"]]})
        wrangler(["secret", "bulk", *ENVARGS], env, cfg, stdin=bulk)
        log("3 secrets set" + (" (signing secret generated; it is not in the Keymaker)" if generated else ""))
        version = live_version(env, cfg) or version

        receipt = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "url": BASE, "worker_env": ENV, "d1": {"name": DB_NAME, "id": db_id}, "version": version, "previous_version": prev, "rollback": f"python3 {Path(__file__).name} --rollback", "signing_secret_generated": generated}
        (STATE / "last_deploy.json").write_text(json.dumps(receipt, indent=2))
        receipt["smoke"] = smoke(signing, email)
        (STATE / "last_deploy.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt, indent=2))
    finally:
        cfg.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
