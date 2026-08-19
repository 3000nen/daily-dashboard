#!/usr/bin/env python3
"""各ニュースRSSをサーバー側で取得し、news-data.json にまとめる。

ブラウザから直接RSSを読むことはCORS制限でできないため、これまではRSS変換API
(rss2json)やCORSプロキシを挟んでいたが、無料枠の制限や障害でたびたび取得できなく
なっていた。実際に調べたところ、配信元のRSS自体はサーバーからは全て正常に取得
できる。そこで市場データ(market-data.json)と同じ方式にし、GitHub Actionsが
サーバー側で取得してJSONに保存、ページは同一オリジンのそのファイルを読むだけに
する。これで外部サービスへの依存と制限がなくなる。

鮮度の絞り込み（6→12→18→24時間）と表示件数はページ側で行うので、ここでは
各セクションの記事を正規化して新しい順に並べるところまでを担当する。
"""

import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

JST = timezone(timedelta(hours=9))
GNEWS_PARAMS = "hl=ja&gl=JP&ceid=JP:ja"


def gnews_search(query):
    return ("https://news.google.com/rss/search?q="
            + urllib.parse.quote(query) + "&" + GNEWS_PARAMS)


def gnews_topic(topic):
    return ("https://news.google.com/rss/headlines/section/topic/"
            + topic + "?" + GNEWS_PARAMS)


COMPANIES = ["日本ペイントグループ", "ハウス食品グループ", "F-LINE",
             "ハウス物流サービス", "モノタロウ", "山善", "アイカ工業"]

# urls は先頭から順に試し、記事が取れた時点で採用する（1件目が本命）
SECTIONS = {
    "news": {
        "urls": ["https://news.google.com/rss?" + GNEWS_PARAMS,
                 "https://www3.nhk.or.jp/rss/news/cat0.xml"],
        "limit": 30,
    },
    "work": {
        "urls": ["https://www.lnews.jp/feed",
                 gnews_search("物流 OR ロジスティクス OR サプライチェーン OR 物流DX")],
        "limit": 20,
    },
    "ai": {
        "urls": ["https://rss.itmedia.co.jp/rss/2.0/itmedia_all.xml",
                 gnews_search("生成AI OR ChatGPT OR Claude OR Copilot")],
        "limit": 30,
        "keywords": ["AI", "Claude", "ChatGPT", "Copilot", "Anthropic", "OpenAI",
                     "Gemini", "生成AI", "エージェント", "LLM"],
    },
    "sports": {
        "urls": [gnews_topic("SPORTS"),
                 "https://www3.nhk.or.jp/rss/news/cat7.xml"],
        "limit": 20,
    },
    "gadget": {
        "urls": ["https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf",
                 gnews_search("ガジェット OR スマートフォン OR ノートPC")],
        "limit": 20,
    },
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def child_text(node, *names):
    """名前空間を無視して、最初に見つかった子要素のテキストを返す。"""
    for child in node:
        if strip_ns(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def child_link(node):
    """RSSは<link>本文、Atomは<link href>にURLが入る。"""
    for child in node:
        if strip_ns(child.tag) != "link":
            continue
        if (child.text or "").strip():
            return child.text.strip()
        href = child.get("href")
        if href:
            return href.strip()
    return ""


def to_iso_utc(raw):
    """RFC822（RSS）でもISO8601（RDF/Atom）でもUTCのISO8601に正規化する。"""
    if not raw:
        return None
    raw = raw.strip()
    for parse in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            dt = parse(raw)
        except (TypeError, ValueError, IndexError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)  # 国内メディアなので日本時間とみなす
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def clean(text, limit=110):
    text = WS_RE.sub(" ", TAG_RE.sub(" ", text or "")).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def split_gnews_title(title):
    """Googleニュースの見出し「タイトル - 媒体名」を分解する。"""
    idx = title.rfind(" - ")
    if idx == -1:
        return title, ""
    return title[:idx].strip(), title[idx + 3:].strip()


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        "Accept-Language": "ja,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def parse_feed(raw, from_gnews):
    root = ET.fromstring(raw)
    items = []
    for node in root.iter():
        if strip_ns(node.tag) not in ("item", "entry"):
            continue
        title = child_text(node, "title")
        if not title:
            continue
        source = ""
        if from_gnews:
            title, source = split_gnews_title(title)
        pub = to_iso_utc(child_text(node, "pubDate", "published", "updated", "date"))
        if not pub:
            continue  # 日時が読めないと鮮度を判断できないので採らない
        items.append({
            "title": clean(title, 200),
            "link": child_link(node),
            "pubDate": pub,
            "source": source,
            # Googleニュースの説明文は関連記事へのリンク集なので使わない
            "summary": "" if from_gnews
                       else clean(child_text(node, "description", "summary", "content")),
        })
    return items


def load_section(name, cfg):
    for url in cfg["urls"]:
        try:
            items = parse_feed(fetch(url), "news.google.com" in url)
        except Exception as exc:  # noqa: BLE001 - 失敗しても次の候補を試す
            print(f"  [{name}] NG {type(exc).__name__}: {exc} <- {url}", file=sys.stderr)
            continue
        if cfg.get("keywords"):
            items = [i for i in items if any(k in i["title"] for k in cfg["keywords"])]
        if not items:
            print(f"  [{name}] 記事0件 <- {url}", file=sys.stderr)
            continue
        items.sort(key=lambda i: i["pubDate"], reverse=True)
        print(f"  [{name}] OK {len(items)}件 <- {url}", file=sys.stderr)
        return {"sourceUrl": url, "items": items[: cfg["limit"]]}
    print(f"  [{name}] すべての取得先が失敗", file=sys.stderr)
    return {"sourceUrl": None, "items": []}


def load_company():
    """会社情報は1社ずつGoogleニュースを検索し、まとめて1セクションにする。"""
    items = []
    for company in COMPANIES:
        try:
            found = parse_feed(fetch(gnews_search(company)), True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [company] NG {company}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for item in found[:3]:
            item["company"] = company
            items.append(item)
        print(f"  [company] OK {company} {len(found[:3])}件", file=sys.stderr)
    items.sort(key=lambda i: i["pubDate"], reverse=True)
    return {"sourceUrl": "https://news.google.com/rss/search", "items": items}


def main():
    sections = {name: load_section(name, cfg) for name, cfg in SECTIONS.items()}
    sections["company"] = load_company()

    data = {
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sections": sections,
    }
    with open("news-data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")

    total = sum(len(s["items"]) for s in sections.values())
    empty = [n for n, s in sections.items() if not s["items"]]
    print(f"合計 {total}件 / 空のセクション: {empty or 'なし'}", file=sys.stderr)
    if total == 0:
        sys.exit(1)  # 全滅のときだけ失敗扱い（一部が空でも書き出す）


if __name__ == "__main__":
    main()
