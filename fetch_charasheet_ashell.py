# -*- coding: utf-8 -*-
"""
キャラクター保管所 抽出スクリプト   a-Shell / iPhone 向け統合版 (CoC6版)

特徴:
  - BeautifulSoup 不要 (標準ライブラリの正規表現でリンク抽出)
  - coc_parser.py 不要 (パーサをこの1ファイルに統合)
  - requests が無い環境では標準の urllib に自動フォールバック
  - 必要なのは skill_base_v6.json だけ (同じフォルダに置く)

使い方:
  python fetch_charasheet_ashell.py "遺伝子の探索者"

出力:
  characters.json (同じフォルダ)
"""
import sys, time, json, re, os

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---- HTTP取得 (requests があれば使い、無ければ urllib) ----
try:
    import requests
    _HAS_REQ = True
except Exception:
    _HAS_REQ = False
    import urllib.request

HEADERS = {"User-Agent": "personal-backup-script/1.0 (single-run)"}

def fetch_bytes(url):
    if _HAS_REQ:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.content
    else:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

def fetch_text(url):
    raw = fetch_bytes(url)
    for enc in ("utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")

BASE = "https://charasheet.vampire-blood.net"
LIST_URL = BASE + "/list_coc.html"
WAIT_SEC = 1.5

# ---- 技能テーブル ----
def load_skill_base(path=None):
    if path is None:
        path = os.path.join(_HERE, "skill_base_v6.json")
    with open(path, encoding="utf-8") as f:
        base = json.load(f)
    skill_map = {s["name"]: s for s in base["skills"]}
    alias_map = {}
    for a in base.get("aliases", []):
        for al in a["aliases"]:
            alias_map[al] = a["canonical"]
    return base, skill_map, alias_map

# ---- パーサ (coc_parserから統合) ----
def normalize_skill_name(raw, alias_map):
    """技能名の表記ゆれを正規名へ寄せる"""
    return alias_map.get(raw, raw)


def split_subname(name):
    """芸術(ネイルアート) → ('芸術', 'ネイルアート')。括弧なし→(name, None)"""
    m = re.match(r"^(.+?)[(（](.*?)[)）]\s*$", name)
    if m:
        base = m.group(1).strip()
        sub = m.group(2).strip()
        return base, (sub if sub else None)
    return name, None


def calc_initial(skill_def, stats):
    """初期値を算出。formula技能は能力値から計算"""
    if skill_def is None:
        return None
    if skill_def.get("formula"):
        f = skill_def["formula"]
        for k, v in stats.items():
            f = f.replace(k, str(v))
        try:
            return eval(f)  # DEX*2 / EDU*5 のみ
        except Exception:
            return None
    return skill_def.get("initial")


# --- フィールド抽出 ---

def _grab(pattern, text, default=""):
    m = re.search(pattern, text)
    return m.group(1).strip() if m else default


def parse_header(text):
    """ヘッダ部(名前・職業・年齢など)を抽出"""
    title = _grab(r"タイトル：(.*)", text)
    name = _grab(r"キャラクター名：(.*)", text)
    job = _grab(r"職業：(.*)", text)
    age_sex = _grab(r"年齢：(.*)", text)
    age, sex = "", ""
    if age_sex:
        m = re.match(r"\s*(\d+)\s*/?\s*性別：?(.*)", age_sex)
        if m:
            age = m.group(1)
            sex = m.group(2).strip()
        else:
            age = age_sex
    return {
        "title": title,
        "name": name,
        "job": job,
        "age": age,
        "sex": sex,
        "origin": _grab(r"出身：(.*)", text),
    }


def parse_stats(text):
    """=合計= 行から8能力値、上部からHP/MP/SANを取得"""
    stats = {}
    m = re.search(r"=合計=([^\n]*)", text)
    if m:
        nums = re.findall(r"-?\d+", m.group(1))
        keys = ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU", "HP", "MP"]
        for k, v in zip(keys, nums):
            stats[k] = int(v)

    hp = _grab(r"HP：(\d+)", text)
    mp = _grab(r"MP：(\d+)", text)
    san = _grab(r"SAN：([\d/]+)", text)
    if hp:
        stats["HP"] = int(hp)
    if mp:
        stats["MP"] = int(mp)
    if san:
        parts = san.split("/")
        cur = parts[0].strip() if len(parts) > 0 else ""
        mx = parts[1].strip() if len(parts) > 1 else ""
        # 現在値が空なら最大値で代用、最大値が空なら現在値で代用
        if cur == "" and mx != "":
            cur = mx
        if mx == "" and cur != "":
            mx = cur
        stats["SAN_current"] = int(cur) if cur.isdigit() else None
        stats["SAN_max"] = int(mx) if mx.isdigit() else None

    # 簡易用ブロックから 幸運/アイデア/知識/DB を補完
    stats["アイデア"] = _to_int(_grab(r"ｱｲﾃﾞｱ:(\d+)", text))
    stats["幸運"] = _to_int(_grab(r"幸\s*運:(\d+)", text))
    stats["知識"] = _to_int(_grab(r"知\s*識:(\d+)", text))
    db = _grab(r"ﾀﾞﾒｰｼﾞﾎﾞｰﾅｽ:([^\n　]+)", text)
    if not db:
        db = _grab(r"ダメージボーナス：([^\n]+)", text)
    stats["DB"] = db
    return stats


def _to_int(s):
    return int(s) if s and s.isdigit() else None


# 技能トークン: ●または空白 + 《技能名》 + 数値 + ％
_SKILL_RE = re.compile(r"([●○]?)\s*《([^》]*)》\s*(\d+)\s*[％%]")


def parse_skills(text, skill_map, alias_map, stats):
    """技能ブロックから全技能を抽出し、振り分け判定を付与"""
    # ■技能■ 〜 ■戦闘■ の間を対象
    seg = text
    m = re.search(r"■技能■(.*?)(?:■戦闘■|■所持品■|■その他■|■簡易用■|$)", text, re.S)
    if m:
        seg = m.group(1)

    results = []
    seen = set()
    for mark, raw_name, val in _SKILL_RE.findall(seg):
        raw_name = raw_name.strip()
        if not raw_name:  # 空欄《》
            continue
        base_name, subname = split_subname(raw_name)
        canon = normalize_skill_name(base_name, alias_map)
        sd = skill_map.get(canon)
        value = int(val)
        initial = calc_initial(sd, stats)
        is_allocated = (initial is not None) and (value > initial)
        key = (canon, subname)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "name": canon,
            "subname": subname,
            "value": value,
            "initial": initial,
            "hasCheck": mark in ("●", "○"),
            "isAllocated": is_allocated,
            "category": sd["category"] if sd else "その他",
            "known": sd is not None,
        })
    return results


def parse_character(text, chara_id=None, meta=None):
    """テキスト出力1件 → 正規化Characterオブジェクト"""
    _, skill_map, alias_map = load_skill_base()
    header = parse_header(text)
    stats = parse_stats(text)
    skills = parse_skills(text, skill_map, alias_map, stats)

    char = {
        "charaId": chara_id,
        "source": f"https://charasheet.vampire-blood.net/{chara_id}" if chara_id else None,
        "title": header["title"],
        "name": header["name"],
        "job": header["job"],
        "age": header["age"],
        "sex": header["sex"],
        "origin": header["origin"],
        "tags": [],
        "imageId": None,
        "stats": stats,
        "skills": skills,
        "rawText": text,
    }
    if meta:
        char.update({k: v for k, v in meta.items() if v is not None})
    return char



# ---- リスト巡回 (正規表現でID抽出。フルURL対応・.txt/ハッシュ除外) ----
from urllib.parse import quote

# 末尾が /数字 で終わるURL (例: https://.../5380082) を拾う。
# /5380082.txt や /m1a2b... は末尾が数字でないので自動的に除外される。
ID_RE = re.compile(r'href="[^"]*?/(\d+)"')

def collect_ids(tag):
    ids = []
    seen = set()
    page = 1
    tag_q = quote(tag)
    while True:
        url = f"{LIST_URL}?tag={tag_q}&order=&page={page}"
        print(f"[list] page {page}")
        html = fetch_text(url)
        found = ID_RE.findall(html)
        page_ids = []
        for cid in found:
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
                page_ids.append(cid)
        print(f"        このページで {len(page_ids)} 件 (累計 {len(ids)})")
        if len(page_ids) == 0:
            break
        # 総件数チェック
        m = re.search(r"of\s+(\d+)\s+results", html)
        total = int(m.group(1)) if m else None
        if total and len(ids) >= total:
            break
        if len(page_ids) < 20:  # 最終ページは件数が少ない
            break
        page += 1
        time.sleep(WAIT_SEC)
    return ids

def fetch_all(tag, out_path=None):
    if out_path is None:
        out_path = os.path.join(_HERE, "characters.json")
    ids = collect_ids(tag)
    print(f"\n[list] 収集ID数: {len(ids)}")
    if not ids:
        print("!! IDが取れませんでした。タグ名を確認してください。")
        return
    characters = []
    for i, cid in enumerate(ids, 1):
        txt_url = f"{BASE}/{cid}.txt"
        print(f"[{i}/{len(ids)}] {cid}")
        try:
            txt = fetch_text(txt_url)
            char = parse_character(txt, cid, meta={"tags": [tag]})
            characters.append(char)
        except Exception as e:
            print(f"   !! 失敗 {cid}: {e}")
        time.sleep(WAIT_SEC)
    out = {"system": "CoC6", "tag": tag, "count": len(characters), "characters": characters}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n完了: {len(characters)}件 → {out_path}")

if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "遺伝子の探索者"
    fetch_all(tag)
