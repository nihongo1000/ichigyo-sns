#!/usr/bin/env python3
"""SNS自動投稿の記録（設定・書き方のルール・投稿の記録）を、お客さんの WordPress の
非公開ページ1枚にしまう／取り出す。GitHub の置き場所を使わないための道具。

使い方:
  python3 sns_state.py pull <作業フォルダ>   # WordPress → フォルダ（無ければ NO_STATE）
  python3 sns_state.py push <作業フォルダ>   # フォルダ → WordPress（STATE_SAVED <sha>）
  python3 sns_state.py check                 # 読み書きできるかだけ確かめる

鍵は環境の設定から読む: WP_SITE（無ければ WP_URL）, WP_USER, WP_APP_PASSWORD。
鍵の値は表示しない。しまう前に、鍵の値が記録に混じっていないか確かめる。
"""
import base64, hashlib, io, json, os, sys, tarfile, urllib.error, urllib.parse, urllib.request

SLUG = "sns-autopost-state"
TITLE = "SNS自動投稿の記録（消さないでください・非公開）"
HEAD = "SNS_STATE_V1"
KEEP_EXT = (".md", ".csv", ".json", ".txt")
SKIP_DIRS = {"scripts", "posts", "ig", "fonts", ".git", "__pycache__"}
MAX_FILE = 300_000
# キットの写し（毎回 R章で置き直す）はしまわない。ブログに部品の中身を置かないため
SKIP_PATHS = {"ROUTINE.md", "SETUP_LINK.md", "config.template.json", "config.example.json"}
SKIP_PREFIX = ("examples/",)
MAX_TOTAL = 4_000_000
SECRET_VARS = ("WP_APP_PASSWORD", "THREADS_TOKEN", "FB_GRAPH_TOKEN", "X_API_KEY", "X_API_SECRET",
               "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET", "SNS_PASS")


def die(msg):
    print(msg)
    sys.exit(1)


def site():
    s = (os.environ.get("WP_SITE") or os.environ.get("WP_URL") or "").strip().rstrip("/")
    if not s:
        die("ENV_MISSING WP_SITE")
    for v in ("WP_USER", "WP_APP_PASSWORD"):
        if not os.environ.get(v, "").strip() or os.environ[v].strip().startswith("ここに"):   # 見本の文字のままは「鍵なし」
            die(f"ENV_MISSING {v}")
    return s


def auth():
    raw = f"{os.environ['WP_USER'].strip()}:{os.environ['WP_APP_PASSWORD'].strip()}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def call(method, route, body=None, query=None):
    base = site()
    q = dict(query or {})
    urls = [f"{base}/wp-json/wp/v2/{route}" + ("?" + urllib.parse.urlencode(q) if q else "")]
    q2 = dict(q, rest_route=f"/wp/v2/{route}")
    urls.append(f"{base}/?" + urllib.parse.urlencode(q2))
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for u in urls:
        req = urllib.request.Request(u, data=data, method=method, headers={
            "Authorization": auth(), "Content-Type": "application/json",
            "User-Agent": "sns-state/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                continue
            die(f"WP_HTTP {e.code} {route}")
        except urllib.error.URLError as e:
            die(f"WP_NET {getattr(e, 'reason', e)}")
    die(f"WP_HTTP {getattr(last, 'code', '?')} {route}")


def find_page():
    res = call("GET", "pages", query={"slug": SLUG, "status": "private,draft,publish",
                                      "context": "edit", "per_page": 5})
    if res:
        return res[0]
    res = call("GET", "pages", query={"slug": SLUG + "-new", "status": "private,draft",
                                      "context": "edit", "per_page": 5})
    return res[0] if res else None


def pack(work):
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(work):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                p = os.path.join(root, f)
                rel = os.path.relpath(p, work).replace(os.sep, "/")
                if rel in SKIP_PATHS or rel.startswith(SKIP_PREFIX):
                    continue
                if not f.endswith(KEEP_EXT) or os.path.getsize(p) > MAX_FILE:
                    continue
                total += os.path.getsize(p)
                if total > MAX_TOTAL:
                    die("STATE_TOO_BIG")
                tar.add(p, arcname=os.path.relpath(p, work))
    blob = buf.getvalue()
    # 鍵が混じっていないか（中身は表示しない）
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        text = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
    for v in SECRET_VARS:
        val = os.environ.get(v, "").strip()
        if len(val) >= 8 and (val.encode() in text or val.replace(" ", "").encode() in text):
            die(f"FAIL secret_in_state {v}")
    return blob


def push(work):
    blob = pack(work)
    sha = hashlib.sha256(blob).hexdigest()[:16]
    content = f"{HEAD} sha={sha}\n" + base64.b64encode(blob).decode()
    old = find_page()
    # 新しいページを作って確かめてから古いページを消す（WordPress の古い版が積み重ならないように）
    tmp_slug = SLUG + "-new"
    for p in call("GET", "pages", query={"slug": tmp_slug, "status": "private,draft", "per_page": 5}) or []:
        call("DELETE", f"pages/{p['id']}", query={"force": "true"})
    new = call("POST", "pages", {"title": TITLE, "slug": tmp_slug, "status": "private", "content": content})
    back = call("GET", f"pages/{new['id']}", query={"context": "edit"})
    raw = (back or {}).get("content", {}).get("raw", "")
    if f"sha={sha}" not in raw.split("\n", 1)[0]:
        call("DELETE", f"pages/{new['id']}", query={"force": "true"})
        die("FAIL state_not_saved")
    if old:
        call("DELETE", f"pages/{old['id']}", query={"force": "true"})
    call("POST", f"pages/{new['id']}", {"slug": SLUG})
    print(f"STATE_SAVED {sha} page={new['id']} kb={len(content)//1024}")


def pull(work):
    page = find_page()
    if not page:
        print("NO_STATE")
        return
    raw = page.get("content", {}).get("raw", "").strip()
    first, _, b64 = raw.partition("\n")
    if not first.startswith(HEAD):
        die("FAIL state_broken")
    blob = base64.b64decode("".join(b64.split()))
    want = first.split("sha=")[-1].strip()
    if hashlib.sha256(blob).hexdigest()[:16] != want:
        die("FAIL state_broken")
    os.makedirs(work, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.name.startswith(("/", "..")) or "/../" in m.name:
                continue
            if hasattr(tarfile, "data_filter"):
                tar.extract(m, work, filter="data")
            else:
                tar.extract(m, work)
    print(f"STATE_LOADED {want}")


def check():
    me = call("GET", "users/me", query={"context": "edit"})
    caps = (me or {}).get("capabilities", {})
    if not caps.get("edit_pages"):
        die("FAIL no_edit_pages")
    print("STATE_OK")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("pull", "push", "check"):
        die(__doc__)
    if sys.argv[1] == "check":
        check()
    elif len(sys.argv) < 3:
        die(__doc__)
    elif sys.argv[1] == "pull":
        pull(sys.argv[2])
    else:
        push(sys.argv[2])
