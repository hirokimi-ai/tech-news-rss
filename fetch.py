#!/usr/bin/env python3
"""Apple / AI / テック系の RSS をまとめて取得し、data.js と data.json を書き出す。

使い方: python3 fetch.py          （取得のみ）
        python3 fetch.py --open   （取得後にブラウザで開く）
フィードの追加・削除は feeds.txt を編集する。
"""
import html
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
PER_FEED = 40      # 1 フィードあたりの最大件数
DESC_LEN = 220     # 概要の最大文字数

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def load_feeds():
    feeds = []
    for line in (ROOT / "feeds.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fid, cat, lang, name, url = [p.strip() for p in line.split("|", 4)]
        feeds.append({"id": fid, "cat": cat, "lang": lang, "name": name, "url": url})
    return feeds


def text(el, path):
    if el is None:
        return ""
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def first_img(s):
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", s or "", re.I)
    return html.unescape(m.group(1)) if m else ""


def parse_date(s):
    if not s:
        return 0
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def image_of(item, raw_html):
    for path in ("media:thumbnail", "media:content", "media:group/media:content",
                 "media:group/media:thumbnail"):
        el = item.find(path, NS)
        if el is not None and el.get("url"):
            if el.tag.endswith("content") and el.get("medium") not in (None, "image") \
                    and not re.search(r"\.(jpe?g|png|webp|gif)", el.get("url"), re.I):
                continue
            return el.get("url")
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image"):
        return enc.get("url")
    return first_img(raw_html)


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    tag = root.tag.lower()
    if tag.endswith("feed"):  # Atom
        for e in root.findall("atom:entry", NS):
            link = ""
            for l in e.findall("atom:link", NS):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            raw = text(e, "atom:summary") or text(e, "atom:content")
            out.append({
                "t": strip_html(text(e, "atom:title")), "l": link, "raw": raw,
                "ts": parse_date(text(e, "atom:published") or text(e, "atom:updated")),
                "img": image_of(e, raw),
            })
    else:
        items = root.findall("channel/item") or root.findall("rss1:item", NS)
        for it in items:
            raw = (text(it, "content:encoded") or text(it, "description")
                   or text(it, "rss1:description"))
            out.append({
                "t": strip_html(text(it, "title") or text(it, "rss1:title")),
                "l": text(it, "link") or text(it, "rss1:link") or text(it, "guid"),
                "raw": raw,
                "ts": parse_date(text(it, "pubDate") or text(it, "dc:date")),
                "img": image_of(it, raw),
            })
    return out


def download(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        # 古い Python の SSL や 308 リダイレクトで失敗する環境向けに curl で再試行
        r = subprocess.run(["curl", "-sfL", "-A", UA, "--max-time", "20", url],
                           capture_output=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout
        raise


def fetch(feed):
    try:
        try:
            entries = parse(download(feed["url"]))
        except ET.ParseError:
            # 転送が途中で切れて XML が壊れることがあるので 1 回だけ取り直す
            time.sleep(3)
            entries = parse(download(feed["url"]))
        return feed, entries, None
    except Exception as e:  # noqa: BLE001 — 1 つの失敗で全体を止めない
        return feed, [], f"{type(e).__name__}: {e}"[:160]


def main():
    feeds = load_feeds()
    now = int(datetime.now(timezone.utc).timestamp())
    items, meta = [], []
    print("フィードを取得中...")
    with ThreadPoolExecutor(max_workers=12) as ex:
        for feed, entries, err in ex.map(fetch, feeds):
            ok = err is None and len(entries) > 0
            meta.append({k: feed[k] for k in ("id", "cat", "lang", "name")} |
                        {"ok": ok, "n": len(entries), "err": err or ""})
            print(f"  {'✓' if ok else '✗'} {feed['name']} ({feed['cat']}) "
                  f"{len(entries) if ok else err or '0 件'}")
            entries.sort(key=lambda x: x["ts"], reverse=True)
            for e in entries[:PER_FEED]:
                if not e["t"] or not e["l"]:
                    continue
                desc = strip_html(e["raw"])
                if len(desc) > DESC_LEN:
                    desc = desc[:DESC_LEN].rstrip() + "…"
                ts = e["ts"] if 0 < e["ts"] <= now + 3600 else now
                items.append({"f": feed["id"], "t": e["t"], "l": e["l"], "d": desc,
                              "img": e["img"] or "", "ts": ts})

    # 同じ URL の重複を除く（新しい方を残す）
    items.sort(key=lambda x: x["ts"], reverse=True)
    seen, uniq = set(), []
    for it in items:
        if it["l"] in seen:
            continue
        seen.add(it["l"])
        uniq.append(it)

    data = {"generated": now, "feeds": meta, "items": uniq}
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (ROOT / "data.json").write_text(js, encoding="utf-8")
    (ROOT / "data.js").write_text(f"window.FEED_DATA={js};\n", encoding="utf-8")
    okn = sum(m["ok"] for m in meta)
    print(f"完了: {okn}/{len(meta)} フィード, {len(uniq)} 件")

    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open((ROOT / "index.html").as_uri())


if __name__ == "__main__":
    main()
