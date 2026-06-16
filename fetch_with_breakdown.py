# -*- coding: utf-8 -*-
"""
キャラクター保管所 差分更新+内訳取得スクリプト (CoC6版 / GitHub Actions向け)

方針(前回設計メモ):
  - リストページから各キャラのID+最終更新日時を取得(軽量)
  - 既存 characters.json と更新日時を照合し、更新されたキャラ(+新規)だけ再取得
  - 変わっていないキャラは前回データをそのまま流用(内訳含む)
  - 再取得は: .txt で能力値/技能の合計値、ブラウザ(Playwright)で技能内訳を取得
  - 内訳取得に失敗した場合は、前回の内訳を維持(空に化けさせない)
  - 更新判定: 更新日時を主、.txtのハッシュを保険として併用

内訳の形式(skillに付与):
  breakdown = {initial, job, interest, growth, other}  # ゼロ項目も含めて数値で保持

依存:
  pip install requests playwright
  playwright install chromium

使い方:
  python fetch_with_breakdown.py "遺伝子の探索者"
"""
import sys, time, json, re, os, hashlib
from urllib.parse import quote

_HERE = os.path.dirname(os.path.abspath(__file__))

import requests

HEADERS = {"User-Agent": "personal-backup-script/1.0 (weekly-diff)"}
BASE = "https://charasheet.vampire-blood.net"
LIST_URL = BASE + "/list_coc.html"
WAIT_SEC = 1.5
OUT_PATH = os.path.join(_HERE, "characters.json")


# ====== 軽量取得(.txt / リスト) ======

def fetch_text(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    raw = r.content
    for enc in ("utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ====== 技能初期値テーブル ======

def load_skill_base():
    with open(os.path.join(_HERE, "skill_base_v6.json"), encoding="utf-8") as f:
        base = json.load(f)
    skill_map = {s["name"]: s for s in base["skills"]}
    alias_map = {}
    for a in base.get("aliases", []):
        for al in a["aliases"]:
            alias_map[al] = a["canonical"]
    return base, skill_map, alias_map


# ====== パーサ(.txt → 正規化) ※ashell版から流用 ======

SKILL_RE = re.compile(r"([●○]?)\s*《([^》]*)》\s*(\d+)\s*[％%]")


def split_subname(name):
    m = re.match(r"^(.+?)[(（](.*?)[)）]\s*$", name)
    if m:
        return m.group(1).strip(), (m.group(2).strip() or None)
    return name, None


def calc_initial(sd, stats):
    if sd is None:
        return None
    if sd.get("formula"):
        f = sd["formula"]
        for k, v in stats.items():
            f = f.replace(k, str(v))
        try:
            return eval(f)
        except Exception:
            return None
    return sd.get("initial")


def grab(pattern, text, default=""):
    m = re.search(pattern, text)
    return m.group(1).strip() if m else default


def to_int_or_none(s):
    return int(s) if s and s.isdigit() else None


def parse_stats(text):
    stats = {}
    m = re.search(r"=合計=([^\n]*)", text)
    if m:
        nums = re.findall(r"-?\d+", m.group(1))
        keys = ["STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU", "HP", "MP"]
        for k, v in zip(keys, nums):
            stats[k] = int(v)
    hp = grab(r"HP：(\d+)", text)
    mp = grab(r"MP：(\d+)", text)
    san = grab(r"SAN：([\d/]+)", text)
    if hp:
        stats["HP"] = int(hp)
    if mp:
        stats["MP"] = int(mp)
    if san:
        parts = san.split("/")
        cur = parts[0].strip() if parts else ""
        mx = parts[1].strip() if len(parts) > 1 else ""
        if cur == "" and mx != "":
            cur = mx
        if mx == "" and cur != "":
            mx = cur
        stats["SAN_current"] = int(cur) if cur.isdigit() else None
        stats["SAN_max"] = int(mx) if mx.isdigit() else None
    stats["アイデア"] = to_int_or_none(grab(r"ｱｲﾃﾞｱ:(\d+)", text))
    stats["幸運"] = to_int_or_none(grab(r"幸\s*運:(\d+)", text))
    stats["知識"] = to_int_or_none(grab(r"知\s*識:(\d+)", text))
    db = grab(r"ﾀﾞﾒｰｼﾞﾎﾞｰﾅｽ:([^\n　]+)", text) or grab(r"ダメージボーナス：([^\n]+)", text)
    stats["DB"] = db
    return stats


def parse_header(text):
    age_sex = grab(r"年齢：(.*)", text)
    age, sex = "", ""
    m = re.match(r"\s*(\d+)\s*/?\s*性別：?(.*)", age_sex)
    if m:
        age, sex = m.group(1), m.group(2).strip()
    else:
        age = age_sex
    return {
        "title": grab(r"タイトル：(.*)", text),
        "name": grab(r"キャラクター名：(.*)", text),
        "job": grab(r"職業：(.*)", text),
        "age": age, "sex": sex, "origin": grab(r"出身：(.*)", text),
    }


def parse_skills(text, skill_map, alias_map, stats):
    seg = text
    m = re.search(r"■技能■(.*?)(?:■戦闘■|■所持品■|■その他■|■簡易用■|$)", text, re.S)
    if m:
        seg = m.group(1)
    out, seen = [], set()
    for mark, raw_name, val in SKILL_RE.findall(seg):
        raw_name = raw_name.strip()
        if not raw_name:
            continue
        base, sub = split_subname(raw_name)
        canon = alias_map.get(base, base)
        sd = skill_map.get(canon)
        value = int(val)
        initial = calc_initial(sd, stats)
        is_alloc = (initial is not None) and (value > initial)
        key = (canon, sub)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": canon, "subname": sub, "value": value, "initial": initial,
            "hasCheck": mark in ("●", "○"), "isAllocated": is_alloc,
            "category": sd["category"] if sd else "その他", "known": sd is not None,
        })
    return out


def parse_character(text, chara_id, tag):
    _, skill_map, alias_map = load_skill_base()
    h = parse_header(text)
    stats = parse_stats(text)
    skills = parse_skills(text, skill_map, alias_map, stats)
    return {
        "charaId": chara_id,
        "source": f"{BASE}/{chara_id}",
        "title": h["title"], "name": h["name"], "job": h["job"],
        "age": h["age"], "sex": h["sex"], "origin": h["origin"],
        "tags": [tag], "imageId": None,
        "stats": stats, "skills": skills, "rawText": text,
    }


# ====== リスト巡回(ID + 更新日時) ======

# リスト行から ID と 更新日時 を取得する。
# 保管所のリストHTMLは各行にキャラURL(/数字)と更新日時(YYYY/MM/DD HH:MM)を含む。
ROW_ID_RE = re.compile(r'/(\d+)"')
DATE_RE = re.compile(r'(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})')


def collect_list(tag):
    """各キャラの {charaId: updatedAt} を返す。更新日時が取れない場合はNone。"""
    result = {}
    order = []
    page = 1
    tag_q = quote(tag)
    while True:
        url = f"{LIST_URL}?tag={tag_q}&order=&page={page}"
        print(f"[list] page {page}")
        html = fetch_text(url)
        # テーブル行ごとに分割してID+日時のペアを拾う
        # 行単位が取りづらいHTMLのため、ID出現位置ごとに近傍の日時を探す
        page_ids = []
        for m in re.finditer(r'/(\d+)"', html):
            cid = m.group(1)
            if cid in result:
                continue
            # ID出現位置の後ろ400文字以内で最初の日時を探す
            tail = html[m.end():m.end()+400]
            dm = DATE_RE.search(tail)
            updated = dm.group(1) if dm else None
            result[cid] = updated
            order.append(cid)
            page_ids.append(cid)
        print(f"        このページ {len(page_ids)} 件 (累計 {len(result)})")
        if len(page_ids) == 0:
            break
        tm = re.search(r"of\s+(\d+)\s+results", html)
        total = int(tm.group(1)) if tm else None
        if total and len(result) >= total:
            break
        if len(page_ids) < 20:
            break
        page += 1
        time.sleep(WAIT_SEC)
    return result, order


# ====== 内訳取得(Playwright) ======

def fetch_breakdown_map(chara_id, page):
    """
    Playwrightで開いたページから技能内訳を取得。
    列順を決め打ちせず、テーブルのヘッダ行(初期値/職業/興味/成長/その他)を見て
    各列のインデックスを特定してから値を読む(構造変化に強い)。
    戻り値: { 技能名: {initial,job,interest,growth,other} }。失敗時は {}。
    """
    url = f"{BASE}/{chara_id}"
    bd = {}
    # 見出しキーワード → breakdownのキー
    # 注意: 技能表の左端には「成長」チェックボックス列があるため、
    #       growthは「成長分」に限定し、ただの「成長」に誤マッチしないようにする。
    COLMAP = [
        ("initial", ["初期値", "初期"]),
        ("job", ["職業"]),
        ("interest", ["興味", "趣味"]),
        ("growth", ["成長分"]),
        ("other", ["その他", "補正"]),
    ]
    try:
        page.goto(url, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(1500)
        tables = page.query_selector_all("table")
        for table in tables:
            rows = table.query_selector_all("tr")
            if not rows:
                continue
            # ヘッダ行から各列の意味を特定(改行・空白を除去してマッチ)
            header_cells = rows[0].query_selector_all("th, td")
            header_texts = [re.sub(r"\s+", "", (c.inner_text() or "")) for c in header_cells]
            col_idx = {}  # breakdownキー -> 列番号
            for ci, htext in enumerate(header_texts):
                for key, kws in COLMAP:
                    if key in col_idx:
                        continue
                    if any(kw in htext for kw in kws):
                        col_idx[key] = ci
            # 初期値列が見つからないテーブルは技能表でないと判断しスキップ
            if "initial" not in col_idx:
                continue
            init_ci = col_idx["initial"]
            # 初期値より左にマッチした列(チェックボックス列「成長」等の誤マッチ)は破棄
            col_idx = {k: v for k, v in col_idx.items() if v >= init_ci}
            # 合計列も特定(整合性チェック用)
            total_ci = None
            for ci, htext in enumerate(header_texts):
                if "合計" in htext:
                    total_ci = ci
                    break
            # データ行を走査
            for row in rows[1:]:
                cells = row.query_selector_all("th, td")
                if not cells:
                    continue
                # 技能名: 《》を含むセル / input(技能名欄)のvalue / 先頭の非数値セル
                label = None
                for cell in cells:
                    t = (cell.inner_text() or "").strip()
                    if "《" in t or (t and not t.replace("%", "").strip().lstrip("-").isdigit()):
                        label = t.replace("《", "").replace("》", "").strip()
                        break
                    # 技能名が入力欄(杖など武道系)の場合: input value を技能名候補に
                    inp = cell.query_selector("input[type='text']")
                    if inp:
                        iv = (inp.get_attribute("value") or "").strip()
                        if iv and not iv.lstrip("-").isdigit():
                            label = iv
                            break
                if not label:
                    continue

                def cell_int(ci):
                    if ci is None or ci >= len(cells):
                        return 0
                    cell = cells[ci]
                    inp = cell.query_selector("input")
                    raw = (inp.get_attribute("value") if inp else cell.inner_text()) or ""
                    raw = raw.strip()
                    return int(raw) if raw.lstrip("-").isdigit() else 0

                entry = {}
                for key, _ in COLMAP:
                    entry[key] = cell_int(col_idx.get(key))

                # 整合性チェック: 内訳合計が合計値と一致しない場合、差分をotherに寄せる
                total_val = cell_int(total_ci) if total_ci is not None else None
                bd_sum = sum(entry.values())
                if total_val and total_val > 0 and bd_sum != total_val:
                    diff = total_val - bd_sum
                    # 差分が正なら未取得分があるとみなしotherへ加算(負なら無視)
                    if diff > 0:
                        entry["other"] = entry.get("other", 0) + diff

                if any(entry.values()):
                    base, _ = split_subname(label)
                    bd[base] = entry
    except Exception as e:
        print(f"     !! 内訳取得失敗 {chara_id}: {e}")
        return {}
    return bd


# ====== メイン: 差分更新 ======

def load_existing():
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                data = json.load(f)
            by_id = {c["charaId"]: c for c in data.get("characters", []) if c.get("charaId")}
            return data, by_id
        except Exception:
            pass
    return None, {}


def txt_hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def main(tag):
    print(f"=== 差分更新開始: タグ「{tag}」 ===")
    prev_data, prev_by_id = load_existing()
    list_map, order = collect_list(tag)
    print(f"[list] 総数 {len(list_map)} / 既存 {len(prev_by_id)}")

    # Playwright起動(内訳取得が必要な場合のみ使う)
    from playwright.sync_api import sync_playwright

    characters = []
    n_reuse = n_update = n_new = n_bd = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for i, cid in enumerate(order, 1):
            updated = list_map.get(cid)
            prev = prev_by_id.get(cid)
            # 更新判定: 既存があり、更新日時が一致するなら流用
            reuse = False
            if prev and updated and prev.get("_updatedAt") == updated:
                reuse = True
            if reuse:
                characters.append(prev)
                n_reuse += 1
                continue
            # 再取得が必要
            try:
                txt = fetch_text(f"{BASE}/{cid}.txt")
            except Exception as e:
                print(f"[{i}] {cid} txt取得失敗: {e} → 前回分維持")
                if prev:
                    characters.append(prev)
                continue
            # ハッシュ保険: 更新日時が無くても中身が同じなら流用
            h = txt_hash(txt)
            if prev and prev.get("_txtHash") == h:
                characters.append(prev)
                n_reuse += 1
                continue
            char = parse_character(txt, cid, tag)
            char["_updatedAt"] = updated
            char["_txtHash"] = h
            # 内訳をブラウザで取得
            bd_map = fetch_breakdown_map(cid, page)
            if bd_map:
                for sk in char["skills"]:
                    if sk["name"] in bd_map:
                        sk["breakdown"] = bd_map[sk["name"]]
                n_bd += 1
            elif prev:
                # 内訳取得失敗時は前回の内訳を技能名で引き継ぐ
                prev_bd = {s["name"]: s.get("breakdown") for s in prev.get("skills", []) if s.get("breakdown")}
                for sk in char["skills"]:
                    if sk["name"] in prev_bd:
                        sk["breakdown"] = prev_bd[sk["name"]]
            if prev:
                n_update += 1
            else:
                n_new += 1
            characters.append(char)
            print(f"[{i}/{len(order)}] {cid} 更新 (内訳{'OK' if bd_map else '—'})")
            time.sleep(WAIT_SEC)
        browser.close()

    out = {"system": "CoC6", "tag": tag, "count": len(characters), "characters": characters}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n=== 完了 ===")
    print(f"流用 {n_reuse} / 更新 {n_update} / 新規 {n_new} / 内訳取得 {n_bd}")
    print(f"出力: {OUT_PATH} ({len(characters)}件)")


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "遺伝子の探索者"
    main(tag)
