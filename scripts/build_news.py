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
import time
import unicodedata
import urllib.error
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


# 表示名だけを書くとそれがそのまま検索語になる。社名が一般的な語と衝突する場合は
# (表示名, 検索語) の形で検索語を別に指定する。
COMPANIES = [
    "日本ペイントグループ",
    "ハウス食品グループ",
    # 「F-LINE」単独だとサンダルブランド(SUBU ORIGINALS F-LINE)や横浜F・マリノスの
    # 記事が混ざるため、食品共同物流会社 F-LINE株式会社(f-line.tokyo.jp)に絞り込む
    ("F-LINE", "F-LINE 物流"),
    "ハウス物流サービス",
    "モノタロウ",
    "山善",
    "アイカ工業",
    "セイノー情報サービス",
    "菱友システムズ",
    "小野運送店",
]


def company_entries():
    """(表示名, 検索語) の組を順に返す。"""
    for entry in COMPANIES:
        if isinstance(entry, tuple):
            yield entry
        else:
            yield entry, entry

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
    # 相場がなぜ動いたかを説明する市況記事。指標が大きく動いた日の理由がここに出る。
    "marketNews": {
        "urls": [gnews_search("東京株式市場 OR 日経平均 OR 円相場 OR ニューヨーク株式市場")],
        "limit": 20,
    },
    # 相場そのものより広い、経済・金融の大きなニュース。
    # GoogleニュースのBUSINESSトピックは日用品レビューや芸能人の車の話まで入って
    # くるので、金融政策・景気などの語で絞った検索を主にし、トピックは予備に回す。
    "economyNews": {
        "urls": [gnews_search("日銀 OR FRB OR 金融政策 OR 利上げ OR 利下げ OR "
                              "インフレ OR 物価 OR 景気 OR GDP OR 経済対策"),
                 gnews_topic("BUSINESS")],
        "limit": 20,
        "keywords": ["日銀", "FRB", "金融政策", "利上げ", "利下げ", "金利", "インフレ",
                     "物価", "景気", "GDP", "経済", "財政", "円安", "円高", "為替",
                     "株価", "市場", "投資", "決算", "貿易", "関税"],
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


PAREN_TAIL_RE = re.compile(r"\([^()]*\)\s*$")
NOISE_RE = re.compile(r"[\s\-—–・,.!?:;\"'\u3000]")


def dedupe_key(title):
    """見出しの表記ゆれを吸収した比較用キー。

    Googleニュースは同じ記事を、末尾の媒体名が全角括弧か半角括弧かだけ違う形で
    重複して返すことがある。NFKC正規化で全角/半角をそろえ、末尾の括弧書きと
    記号類を落としてから比べる。
    """
    t = unicodedata.normalize("NFKC", title)
    t = PAREN_TAIL_RE.sub("", t).strip()
    return NOISE_RE.sub("", t)


def dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = dedupe_key(item["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def split_gnews_title(title):
    """Googleニュースの見出し「タイトル - 媒体名」を分解する。"""
    idx = title.rfind(" - ")
    if idx == -1:
        return title, ""
    source = title[idx + 3:].strip()
    # 「motorsport.com 日本版｜モータースポーツ情報サイト」のように極端に長い
    # 媒体名があり、カード上部の情報行を圧迫するので頭だけ使う
    source = re.split(r"[｜|]", source)[0].strip()
    if len(source) > 20:
        source = source[:19] + "…"
    return title[:idx].strip(), source


# Googleニュースは短時間に多くのリクエストを送ると503を返す。1回の実行で十数件
# 取りに行くため、取得の間隔を空けたうえで、503/429は待って再試行する。
REQUEST_INTERVAL_SEC = 1.5
RETRY_WAITS_SEC = (3, 8, 20)
_last_fetch_at = 0.0


def fetch(url, timeout=25):
    global _last_fetch_at
    for attempt in range(len(RETRY_WAITS_SEC) + 1):
        wait = REQUEST_INTERVAL_SEC - (time.monotonic() - _last_fetch_at)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/xml,*/*",
            "Accept-Language": "ja,en;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == len(RETRY_WAITS_SEC):
                raise
            pause = RETRY_WAITS_SEC[attempt]
            print(f"    HTTP {exc.code}。{pause}秒待って再試行 ({attempt + 1}回目)", file=sys.stderr)
            time.sleep(pause)
        finally:
            _last_fetch_at = time.monotonic()


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
        items = dedupe(items)
        print(f"  [{name}] OK {len(items)}件 <- {url}", file=sys.stderr)
        return {"sourceUrl": url, "items": items[: cfg["limit"]]}
    print(f"  [{name}] すべての取得先が失敗", file=sys.stderr)
    return {"sourceUrl": None, "items": []}


def load_company():
    """会社情報は1社ずつGoogleニュースを検索し、まとめて1セクションにする。"""
    items = []
    for name, query in company_entries():
        try:
            found = parse_feed(fetch(gnews_search(query)), True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [company] NG {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for item in found[:3]:
            item["company"] = name
            items.append(item)
        print(f"  [company] OK {name} {len(found[:3])}件 (検索語: {query})", file=sys.stderr)
    items.sort(key=lambda i: i["pubDate"], reverse=True)
    return {"sourceUrl": "https://news.google.com/rss/search", "items": dedupe(items)}


def load_previous():
    """前回の news-data.json を読む。取得に失敗したセクションの穴埋めに使う。"""
    try:
        with open("news-data.json", encoding="utf-8") as f:
            return json.load(f).get("sections", {})
    except (OSError, ValueError):
        return {}


def main():
    previous = load_previous()

    sections = {name: load_section(name, cfg) for name, cfg in SECTIONS.items()}
    sections["company"] = load_company()

    # 配信元の一時的な障害（Googleニュースの503など）で空になったセクションは、
    # 前回取得できていた記事をそのまま残す。記事には公開日時があるので、
    # 古くなったものはページ側の鮮度フィルタで自然に消える。
    for name, section in sections.items():
        if section["items"]:
            continue
        kept = previous.get(name, {}).get("items") or []
        if kept:
            section["items"] = kept
            section["stale"] = True
            print(f"  [{name}] 取得できなかったため前回の{len(kept)}件を維持", file=sys.stderr)

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
