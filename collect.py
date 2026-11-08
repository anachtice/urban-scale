# -*- coding: utf-8 -*-
"""
Urban Scale 수집기

하는 일
  1. config/queries.json 의 검색어로 네이버 뉴스 검색 API에서 최근 기사를 모은다.
  2. 서울·신도시·경기·인천 기사만 남긴다 (config/regions.json).
  3. 분야를 정한다 (config/categories.json).
  4. 제목이 비슷한 기사끼리 이슈로 묶고, 다룬 언론사 수로 순위를 매긴다 (config/settings.json 의 '이슈 묶기').
  5. docs/data/날짜.json 과 docs/data/index.json 에 저장한다. 사이트(docs/index.html)가 이 파일을 읽는다.
  6. 용어 사전에 없는데 최근 제목에 자주 나온 말을 골라 위키백과에서 뜻을 찾는다 (docs/data/new-terms.json).

실행
  python collect.py               네이버 API로 수집 (NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 필요. .env 파일도 읽음)
  python collect.py --source rss  키 없이 구글 뉴스 RSS로 시험 수집 (내 컴퓨터에서 시험할 때만)
  python collect.py --reprocess   새로 모으지 않고, 저장된 오늘·어제 기사에 바뀐 사전을 다시 적용

외부 패키지 없이 파이썬 기본 기능만 쓴다.
"""

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "docs", "data")
USER_AGENT = "Mozilla/5.0 (urban-scale; personal study)"

AREA_ORDER = ["서울", "신도시", "경기·인천", "수도권"]
NATIONAL = "전국·정책"
RAW_KEYS = ("id", "t", "d", "u", "o", "p", "at", "q")


# ───────────────────────── 설정 읽기 ─────────────────────────

def load_config(name):
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def load_env_file():
    """.env 파일(KEY=값)을 읽어 환경변수로 넣는다. 이미 있는 값은 덮어쓰지 않는다."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and not os.environ.get(key):
                os.environ[key] = value


# ───────────────────────── 글자 다듬기 ─────────────────────────

TAG_RX = re.compile(r"<[^>]+>")
SPACE_RX = re.compile(r"\s+")
HEAD_RX = re.compile(r"^\s*[\[【〈<(]\s*([^\]】〉>)]{1,20}?)\s*[\]】〉>)]\s*")


def clean(text):
    """HTML 태그(<b> 등)와 &quot; 같은 기호를 걷어 낸다."""
    text = html.unescape(text or "")
    text = TAG_RX.sub("", text)
    text = html.unescape(text)
    return SPACE_RX.sub(" ", text).strip()


def head_tag(title):
    """제목 맨 앞의 [사설], (종합) 같은 머리말."""
    m = HEAD_RX.match(title)
    return m.group(1).strip() if m else ""


def article_kind(title, column_words):
    head = head_tag(title)
    if not head:
        return None
    if "사설" in head:
        return "사설"
    if any(w in head for w in column_words):
        return "칼럼"
    return None


def is_unwanted(title, drop_heads):
    head = head_tag(title).replace(" ", "")
    if not head:
        return False
    if head.startswith(("포토", "사진", "영상", "화보")):
        return True
    return any(head == w.replace(" ", "") for w in drop_heads)


# ───────────────────────── 언론사 이름 ─────────────────────────

def host_of(url):
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for prefix in ("www.", "m.", "mobile.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def press_of(url, press_map):
    host = host_of(url)
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in press_map:
            return press_map[candidate]
    return host or "알 수 없음"


# ───────────────────────── 기사 받아 오기 ─────────────────────────

def http_get(url, headers=None, tries=3, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def parse_time(text):
    try:
        return parsedate_to_datetime(text or "").astimezone(KST)
    except (TypeError, ValueError):
        return None


# 네이버 검색 API는 2026년부터 네이버 클라우드 플랫폼의 NAVER API HUB에서 발급한다(주소·인증 머리글이 바뀜).
# 예전 네이버 개발자센터 키를 넣으면 옛 주소로 자동으로 넘어간다.
NAVER_APIS = {
    "hub": ("https://naverapihub.apigw.ntruss.com/search/v1/news", "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
    "openapi": ("https://openapi.naver.com/v1/search/news.json", "X-Naver-Client-Id", "X-Naver-Client-Secret"),
}


def fetch_naver(query, since, client_id, client_secret, limit, api="hub"):
    """네이버 뉴스 검색 API. 최신순으로 100개씩 받다가 since보다 오래된 기사가 나오면 멈춘다."""
    url, id_header, secret_header = NAVER_APIS[api]
    found = []
    for start in range(1, min(limit, 1000) + 1, 100):
        qs = urllib.parse.urlencode({"query": query, "display": 100, "start": start, "sort": "date"})
        raw = http_get(f"{url}?{qs}", {id_header: client_id, secret_header: client_secret})
        items = json.loads(raw.decode("utf-8")).get("items", [])
        reached_old = False
        for it in items:
            pub = parse_time(it.get("pubDate"))
            if pub is None:
                continue
            if pub < since:
                reached_old = True
                continue
            link = it.get("link") or ""
            orig = it.get("originallink") or link
            found.append({
                "title": clean(it.get("title")),
                "desc": clean(it.get("description")),
                "url": link if "n.news.naver.com" in link else orig,
                "orig": orig,
                "pub": pub,
                "press": None,
            })
        if reached_old or len(items) < 100:
            break
        time.sleep(0.1)
    return found


def fetch_google_rss(query, since):
    """시험용: 키 없이 구글 뉴스 RSS에서 제목만 받아 온다. 요약은 없다."""
    qs = urllib.parse.urlencode({"q": f"{query} when:2d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    root = ET.fromstring(http_get("https://news.google.com/rss/search?" + qs))
    found = []
    for it in root.iter("item"):
        source = it.find("source")
        press = (source.text or "").strip() if source is not None else ""
        title = clean(it.findtext("title"))
        suffix = " - " + press
        while press and title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
        pub = parse_time(it.findtext("pubDate"))
        if pub is None or pub < since:
            continue
        link = it.findtext("link") or ""
        found.append({"title": title, "desc": "", "url": link, "orig": link, "pub": pub, "press": press or None})
    return found


def make_id(article):
    orig = article["orig"]
    if orig and "news.google.com" not in orig:
        key = orig
    else:
        key = f'{article["press"]}|{article["title"]}'
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def pick_naver_api(query, since, client_id, client_secret, limit):
    """첫 검색어로 API HUB 주소를 먼저 시험하고, 키가 맞지 않으면 옛 개발자센터 주소를 시험한다."""
    for api in ("hub", "openapi"):
        try:
            return api, fetch_naver(query, since, client_id, client_secret, limit, api)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            print(f"  · {api} 주소에서 키를 받지 않음({e.code})")
    sys.exit("네이버 API 키가 맞지 않습니다(401/403). NAVER API HUB의 Client ID와 Client Secret을 다시 확인해 주세요.")


def collect(source, queries, settings, press_map, since):
    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if source == "naver" and not (client_id and client_secret):
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 없습니다. .env 파일이나 GitHub Secrets에 넣어 주세요.\n"
                 "키 없이 시험하려면: python collect.py --source rss")
    limit = settings["검색어 하나당 최대 기사 수"]
    api = None
    articles, failed = {}, []
    for n, query in enumerate(queries, 1):
        try:
            if source == "naver" and api is None:
                api, items = pick_naver_api(query, since, client_id, client_secret, limit)
                print(f"  네이버 API 주소: {NAVER_APIS[api][0]}")
            elif source == "naver":
                items = fetch_naver(query, since, client_id, client_secret, limit, api)
            else:
                items = fetch_google_rss(query, since)
                time.sleep(0.4)
        except Exception as e:  # 검색어 하나가 실패해도 나머지는 계속한다
            failed.append(query)
            print(f"  ! '{query}' 검색 실패: {e}")
            continue
        for a in items:
            if not a["title"]:
                continue
            if not a["press"]:
                a["press"] = press_of(a["orig"] or a["url"], press_map)
            aid = make_id(a)
            if aid in articles:
                if query not in articles[aid]["q"]:
                    articles[aid]["q"].append(query)
                continue
            a["q"] = [query]
            articles[aid] = a
        if n % 20 == 0:
            print(f"  검색어 {n}/{len(queries)} · 기사 {len(articles)}건")
    return articles, failed


# ───────────────────────── 지역 찾기 ─────────────────────────

NOT_AFTER = r"(?<![가-힣0-9])"  # 앞에 한글·숫자가 붙어 있으면 다른 낱말의 일부로 본다


class RegionMatcher:
    def __init__(self, cfg):
        self.exclude = cfg.get("제외할 말", [])
        self.word_map = defaultdict(list)  # 낱말 -> [(지역, 곳, 함께 나와야 하는 말)]
        for gu, words in cfg["서울"].items():
            for w in words:
                self.word_map[w].append(("서울", gu, None))
        for w in cfg.get("서울 전체", []):
            self.word_map[w].append(("서울", None, None))
        for name, words in cfg["신도시"].items():
            for w in words:
                self.word_map[w].append(("신도시", name, None))
        for city, words in cfg["경기·인천"].items():
            for w in words:
                self.word_map[w].append(("경기·인천", city, None))
        for w in cfg.get("경기·인천 전체", []):
            self.word_map[w].append(("경기·인천", None, None))
        for w in cfg.get("수도권 전체", []):
            self.word_map[w].append(("수도권", None, None))
        for w, rule in cfg.get("조건이 붙는 말", {}).items():
            self.word_map[w].append((rule["지역"], rule.get("곳"), rule.get("함께 나와야 하는 말")))
        self.rx = self._compile(self.word_map)
        self.noncap_rx = self._compile(cfg.get("비수도권", []))

    @staticmethod
    def _compile(words):
        words = sorted(words, key=len, reverse=True)
        if not words:
            return None
        return re.compile(NOT_AFTER + "(" + "|".join(map(re.escape, words)) + ")")

    def find(self, text):
        """지역별 횟수, 곳별 횟수, 비수도권 말이 있는지, 가장 먼저 나온 지역(대표 지역)을 돌려준다."""
        for w in self.exclude:
            text = text.replace(w, " ")
        areas, places, first = Counter(), Counter(), None
        for m in self.rx.finditer(text):
            found = [(area, place) for area, place, need in self.word_map[m.group(1)]
                     if not need or any(x in text for x in need)]
            for area, place in found:
                areas[area] += 1
                if place:
                    places[(area, place)] += 1
            if first is None:
                first = next((a for a in AREA_ORDER[:3] if any(a == f for f, _ in found)), None)
        noncap = bool(self.noncap_rx and self.noncap_rx.search(text))
        return areas, places, noncap, first


def place_label(area, place):
    return f"서울 {place}" if area == "서울" else place


# ───────────────────────── 분야 나누기 ─────────────────────────

class Classifier:
    def __init__(self, cfg):
        self.exclude = cfg.get("제외할 말", [])
        self.cats = [k for k in cfg if not k.startswith("_") and k != "제외할 말"]
        self.word_map = defaultdict(list)
        for cat in self.cats:
            for w in cfg[cat]:
                self.word_map[w].append(cat)
        parts = []
        for w in sorted(self.word_map, key=len, reverse=True):
            esc = re.escape(w)
            if re.fullmatch(r"[A-Za-z0-9 ]+", w):  # 영어 약어는 다른 영어 낱말 안에서 잡히지 않게
                esc = r"(?<![A-Za-z])" + esc + r"(?![A-Za-z])"
            parts.append(esc)
        self.rx = re.compile("|".join(parts))

    def _hits(self, text):
        for w in self.exclude:
            text = text.replace(w, " ")
        per_cat = defaultdict(set)
        for m in self.rx.finditer(text):
            for cat in self.word_map[m.group(0)]:
                per_cat[cat].add(m.group(0))
        return per_cat

    def classify(self, title, desc):
        in_title, in_desc = self._hits(title), self._hits(desc)
        scored = []
        for order, cat in enumerate(self.cats):
            t = in_title.get(cat, set())
            d = in_desc.get(cat, set()) - t
            score = 2 * len(t) + len(d)
            if score:
                scored.append((score, len(t), -order, cat))
        if not scored:
            return None, [], 0
        scored.sort(reverse=True)
        return scored[0][3], [c for s, _, _, c in scored[1:] if s >= 2], scored[0][0]


# ───────────────────────── 이슈 묶기 ─────────────────────────

JOSA = sorted([
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만", "로", "으로", "에서", "에게",
    "까지", "부터", "보다", "처럼", "이나", "라도", "에는", "에도", "로는", "으론", "으로는", "에서는",
    "에서도", "까지는", "이라", "라는", "이라는", "했다", "한다", "된다", "됐다", "이다", "에선", "서도",
], key=len, reverse=True)
SPLIT_RX = re.compile(r"[\s\"'“”‘’`·•…,.!?:;~/|\\\-–—=+*&%^#@()\[\]{}<>「」『』《》〈〉【】]+")
BRACKET_RX = re.compile(r"[\[(【〈<][^\])】〉>]{1,12}[\])】〉>]")
NUMBER_OK_RX = re.compile(r"(구역|단지|지구|차|호선|층|공구|블록|동)$")


def raw_words(title):
    """제목을 띄어쓰기·문장부호로 자른다. 맨 앞 [단독] 같은 머리말과 (종합) 같은 괄호 말은 뺀다."""
    text = HEAD_RX.sub(" ", title)
    text = BRACKET_RX.sub(" ", text)
    text = re.sub(r"(?<=\d)[.,](?=\d)", "", text)
    return [w for w in SPLIT_RX.split(text) if w]


def strip_josa(word, vocab):
    """조사를 뗀다. 두 글자 이상 조사(에서, 까지 등)는 늘 떼고, 한 글자 조사(은·는·이·가 등)는
    뗀 말이 그날 제목에 따로 쓰인 적이 있을 때만 뗀다. 그래서 '인허가'는 '인허'로 잘리지 않는다."""
    for j in JOSA:
        if word.endswith(j):
            stem = word[: -len(j)]
            if len(stem) >= 2 and (len(j) >= 2 or stem in vocab):
                return stem
    return word


def title_tokens(words, vocab, drop):
    """잘라 낸 말에서 조사를 떼고, 버릴 말과 숫자만 있는 말(4.6억 등)을 걸러 낸다."""
    out = []
    for w in words:
        w = strip_josa(w, vocab)
        if w.isascii():
            w = w.upper()
        if len(w) < 2 or w in drop:
            continue
        if w[0].isdigit() and not NUMBER_OK_RX.search(w):
            continue
        out.append(w)
    return list(dict.fromkeys(out))


def core_of(cluster):
    """이슈의 핵심어: 묶인 기사의 30% 이상(최소 2건)에 나온 낱말."""
    size = len(cluster["ids"])
    if cluster.get("core_size") == size:
        return cluster["core"]
    if size <= 2:
        core = set(cluster["cnt"])
    else:
        need = max(2, math.ceil(size * 0.3))
        core = {t for t, k in cluster["cnt"].items() if k >= need} or {t for t, _ in cluster["cnt"].most_common(3)}
    cluster["core"], cluster["core_size"] = core, size
    return core


def make_issues(articles, cfg):
    drop, weak = set(cfg["버릴 말"]), set(cfg["약한 말"])
    n = len(articles)
    if not n:
        return []
    raws = [raw_words(a["t"]) for a in articles]
    vocab = {w for ws in raws for w in ws}
    toks = [title_tokens(ws, vocab, drop) for ws in raws]
    df = Counter()
    for ts in toks:
        df.update(ts)
    common_cut = max(6, int(n * cfg["흔한 말 비율"]))

    def weight(t):
        return math.log((n + 1) / df[t])

    def is_strong(t):
        return t not in weak and 2 <= df[t] <= common_cut

    clusters, by_token = [], defaultdict(set)
    for i in sorted(range(n), key=lambda k: articles[k]["at"]):
        strong = [t for t in toks[i] if is_strong(t)]
        if len(strong) < 2:
            continue  # 묶을 근거가 모자란 기사는 이슈에 넣지 않는다
        hits = Counter(ci for t in strong for ci in by_token[t])
        best, best_score = None, 0.0
        for ci, k in hits.items():
            if k < 2:
                continue
            shared = [t for t in strong if t in core_of(clusters[ci])]
            if len(shared) < 2:
                continue
            score = sum(weight(t) for t in shared)
            if score > best_score:
                best, best_score = ci, score
        if best is None or best_score < cfg["묶는 점수"]:
            clusters.append({"ids": [], "cnt": Counter()})
            best = len(clusters) - 1
        clusters[best]["ids"].append(i)
        clusters[best]["cnt"].update(strong)
        for t in strong:
            by_token[t].add(best)

    # 핵심어 5개 중 3개 이상, 또는 무게가 큰 핵심어 2개가 겹치는 이슈끼리 한 번 더 합친다
    live = [c for c in clusters if len(c["ids"]) >= 2]
    merged = True
    while merged:
        merged = False
        tops = [{t for t, _ in c["cnt"].most_common(5)} for c in live]
        for x in range(len(live)):
            for y in range(x + 1, len(live)):
                shared = tops[x] & tops[y]
                if len(shared) >= 3 or (len(shared) == 2 and sum(map(weight, shared)) >= cfg["묶는 점수"] * 1.4):
                    live[x]["ids"] += live[y]["ids"]
                    live[x]["cnt"] += live[y]["cnt"]
                    live[x].pop("core_size", None)
                    del live[y]
                    merged = True
                    break
            if merged:
                break

    issues = []
    for c in live:
        ids = c["ids"]
        presses = {articles[i]["p"] for i in ids}
        if len(presses) < cfg["이슈로 보여줄 최소 언론사 수"]:
            continue
        core = core_of(c)

        def rep_key(i):
            a = articles[i]
            return (0 if a.get("k") else 1, sum(weight(t) for t in toks[i] if t in core), -abs(len(a["t"]) - 32))

        rep = articles[max(ids, key=rep_key)]
        members = sorted(ids, key=lambda i: articles[i]["at"], reverse=True)
        places = Counter(p for i in ids for p in articles[i]["r"][:2])
        issues.append({
            "t": rep["t"], "u": rep["u"], "p": rep["p"],
            "keys": [t for t, _ in c["cnt"].most_common(12) if t in core][:5],
            "n": len(ids), "np": len(presses),
            "c": Counter(articles[i]["c"] for i in ids).most_common(1)[0][0],
            "a": Counter(articles[i]["a"] for i in ids).most_common(1)[0][0],
            "r": [p for p, _ in places.most_common(3)],
            "first": min(articles[i]["at"] for i in ids),
            "last": max(articles[i]["at"] for i in ids),
            "ids": [articles[i]["id"] for i in members],
            "_core": core,
        })
    issues.sort(key=lambda x: (-x["np"], -x["n"]))
    issues = issues[: cfg["이슈 최대 개수"]]

    # 이슈에 들어가지 못한 사설·칼럼도 핵심어가 2개 이상 겹치면 그 이슈에 붙인다
    placed = {aid for it in issues for aid in it["ids"]}
    for i, a in enumerate(articles):
        if not a.get("k") or a["id"] in placed:
            continue
        best, best_shared = None, 1
        for it in issues:
            shared = len(set(toks[i]) & it["_core"])
            if shared > best_shared:
                best, best_shared = it, shared
        if best:
            best["ids"].append(a["id"])
    by_id = {a["id"]: a for a in articles}
    for rank, it in enumerate(issues, 1):
        it["rank"] = rank
        it["op"] = sum(1 for aid in it["ids"] if by_id[aid].get("k"))
        del it["_core"]
    return issues


# ───────────────────────── 하루치 정리 ─────────────────────────

class Context:
    def __init__(self):
        self.settings = load_config("settings.json")
        self.regions = RegionMatcher(load_config("regions.json"))
        self.classifier = Classifier(load_config("categories.json"))
        press = load_config("press.json")
        self.press_map = press["언론사"]
        self.blocked_press = set(press.get("뺄 언론사", []))


def process_day(date, records, ctx, updated):
    settings = ctx.settings
    kept, seen, dropped = [], set(), Counter()
    for r in sorted(records.values(), key=lambda x: x["at"]):
        title, desc = r["t"], r.get("d") or ""
        if is_unwanted(title, settings["버릴 머리말"]):
            dropped["사진·부고 등"] += 1
            continue
        if r.get("o") and "news.google.com" not in r["o"]:  # 언론사 이름 사전을 고치면 이미 모은 기사에도 바로 반영
            r["p"] = press_of(r["o"], ctx.press_map)
        if r["p"] in ctx.blocked_press:
            dropped["연예·스포츠 매체"] += 1
            continue
        if any(w in title for w in settings.get("제목에 있으면 버릴 말", [])):
            dropped["버릴 제목 말"] += 1
            continue
        same = (r["p"], re.sub(r"\W", "", title)[:40])
        if same in seen:
            dropped["같은 기사"] += 1
            continue
        areas, places, noncap, first = ctx.regions.find(f"{title} {desc}")
        kind = article_kind(title, settings["칼럼 머리말"])
        if noncap and not areas and not kind:  # 사설·칼럼은 지방 사례로 정책을 논하는 경우가 많아 지역으로 거르지 않는다
            dropped["비수도권"] += 1
            continue
        cat, others, score = ctx.classifier.classify(title, desc)
        if not cat or score < settings.get("분야 최소 점수", 2):
            dropped["도시 분야 아님"] += 1  # 요약에만 도시 낱말이 하나 스친 기사도 여기서 빠진다
            continue
        seen.add(same)
        rec = {k: r.get(k) for k in RAW_KEYS}
        rec.update({
            "c": cat,
            "c2": others,
            "a": first or ("수도권" if areas.get("수도권") else NATIONAL),
            "ar": [x for x in AREA_ORDER if areas.get(x)],
            "r": [place_label(a, p) for (a, p), _ in places.most_common(4)],
            "k": kind,
        })
        kept.append(rec)
    issues = make_issues(kept, settings["이슈 묶기"])
    kept.sort(key=lambda x: x["at"], reverse=True)
    return {
        "date": date,
        "updated": updated,
        "count": len(kept),
        "dropped": dict(dropped),
        "categories": ctx.classifier.cats,
        "issues": issues,
        "articles": kept,
    }


# ───────────────────────── 저장 ─────────────────────────

def day_path(date):
    return os.path.join(DATA_DIR, f"{date}.json")


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_day(date):
    path = day_path(date)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {a["id"]: {k: a.get(k) for k in RAW_KEYS} for a in data.get("articles", [])}


def load_index():
    """날짜 목록과 마지막으로 수집에 성공한 시각."""
    path = os.path.join(DATA_DIR, "index.json")
    if not os.path.exists(path):
        return {}, None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    last_run = None
    if data.get("last_run"):
        try:
            last_run = datetime.fromisoformat(data["last_run"])
        except ValueError:
            pass
    return {x["d"]: x for x in data.get("dates", [])}, last_run


def write_index(index, keep_days, now, last_run):
    if keep_days:
        cutoff = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        for d in [d for d in index if d < cutoff]:
            if os.path.exists(day_path(d)):
                os.remove(day_path(d))
            del index[d]
    for d in list(index):  # 파일이 없어진 날짜는 목록에서도 뺀다
        if not os.path.exists(day_path(d)):
            del index[d]
    dates = sorted(index.values(), key=lambda x: x["d"], reverse=True)
    save_json(os.path.join(DATA_DIR, "index.json"), {
        "latest": dates[0]["d"] if dates else None,
        "last_run": last_run.isoformat(timespec="minutes") if last_run else None,
        "dates": dates,
    })


def to_record(aid, a, max_desc):
    desc = a["desc"]
    if len(desc) > max_desc:
        desc = desc[:max_desc].rstrip() + "…"
    return {"id": aid, "t": a["title"], "d": desc, "u": a["url"], "o": a["orig"], "p": a["press"],
            "at": a["pub"].strftime("%Y-%m-%dT%H:%M"), "q": a["q"]}


def report(day):
    arts = day["articles"]
    print(f"\n[{day['date']}] 실은 기사 {day['count']}건 · 뺀 기사 {day['dropped']}")
    print("  분야:", dict(Counter(a["c"] for a in arts).most_common()))
    print("  지역:", dict(Counter(a["a"] for a in arts).most_common()))
    print("  사설·칼럼:", dict(Counter(a["k"] for a in arts if a["k"])))
    for it in day["issues"][:10]:
        print(f"  {it['rank']:>2}. 언론사 {it['np']:>2}곳·기사 {it['n']:>3}건 [{it['c']}] {it['t'][:60]}  #{' #'.join(it['keys'])}")


# ───────────────────────── 용어 사전·관심 필자 ─────────────────────────

GLOSSARY_PATH = os.path.join(DATA_DIR, "glossary.json")
PEOPLE_PATH = os.path.join(DATA_DIR, "people.json")


def publish_glossary():
    """config/glossary.json 을 사이트가 읽는 docs/data/glossary.json 으로 옮긴다."""
    try:
        save_json(GLOSSARY_PATH, load_config("glossary.json"))
    except FileNotFoundError:
        pass


def update_people(now, last_run, ctx, source):
    """config/people.json 의 필자·인물이 나온 칼럼·기사를 모은다. 지역·분야로 거르지 않는다.
    처음 넣은 사람은 '처음 거슬러 받을 날 수'만큼 지난 기사를 한 번 받는다."""
    try:
        cfg = load_config("people.json")
    except FileNotFoundError:
        return
    people = cfg.get("필자") or []
    client_id = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not people or (source == "naver" and not (client_id and client_secret)):
        return
    old = {}
    if os.path.exists(PEOPLE_PATH):
        with open(PEOPLE_PATH, encoding="utf-8") as f:
            old = {p["name"]: p for p in json.load(f).get("people", [])}
    back_days = cfg.get("처음 거슬러 받을 날 수", 365)
    cap = cfg.get("필자당 최대 보관 수", 300)
    max_desc = ctx.settings["요약 최대 글자 수"]
    out, api = [], None
    for person in people:
        name = person["이름"]
        prev = old.get(name, {})
        first_time = not prev.get("backfilled")
        if first_time:
            since = now - timedelta(days=back_days)
        else:
            since = max(now - timedelta(hours=ctx.settings["최근 몇 시간 기사를 모을지"]), (last_run or now) - timedelta(hours=1))
        items = {x["id"]: x for x in prev.get("items", [])}
        ok = True
        for q in person.get("검색어") or [name]:
            try:
                if source == "naver" and api is None:
                    api, found = pick_naver_api(q, since, client_id, client_secret, 1000)
                elif source == "naver":
                    found = fetch_naver(q, since, client_id, client_secret, 1000, api)
                else:
                    found = fetch_google_rss(q, since)
            except Exception as e:
                print(f"  ! 관심 필자 '{name}' 검색 실패: {e}")
                ok = False
                continue
            for a in found:
                if not a["title"]:
                    continue
                a["press"] = a["press"] or press_of(a["orig"] or a["url"], ctx.press_map)
                desc = a["desc"] if len(a["desc"]) <= max_desc else a["desc"][:max_desc].rstrip() + "…"
                items.setdefault(make_id(a), {
                    "id": make_id(a), "t": a["title"], "d": desc, "u": a["url"], "p": a["press"],
                    "at": a["pub"].strftime("%Y-%m-%dT%H:%M"), "k": article_kind(a["title"], ctx.settings["칼럼 머리말"]),
                })
        seen, kept = set(), []
        for x in sorted(items.values(), key=lambda x: x["at"], reverse=True):  # 같은 언론사·같은 제목은 하나만
            key = (x["p"], re.sub(r"\W", "", x["t"])[:40])
            if key not in seen:
                seen.add(key)
                kept.append(x)
        out.append({"name": name, "intro": person.get("소개", ""),
                     "backfilled": bool(prev.get("backfilled")) or (ok and source == "naver"), "items": kept[:cap]})
        print(f"  관심 필자 '{name}': {len(kept)}건" + (" (지난 기사 거슬러 받음)" if first_time and ok and source == "naver" else ""))
    save_json(PEOPLE_PATH, {"updated": now.isoformat(timespec="minutes"), "people": out})


# ───────────────────────── 사전 밖 용어 ─────────────────────────
# 최근 며칠 기사 제목에 자주 나왔는데 용어 사전에 없는 말을 고르고, 위키백과 요약에서 뜻을 찾아 붙인다.
# 사람·지명·기업·동음이의어 문서와 도시와 관계없어 보이는 뜻은 싣지 않는다. 찾은 결과는 저장해 두고 다시 묻지 않는다.

NEW_TERMS_PATH = os.path.join(DATA_DIR, "new-terms.json")
NEW_TERMS_CACHE_PATH = os.path.join(DATA_DIR, "new-terms-cache.json")  # 찾아본 결과(사이트는 읽지 않음)
WIKI_SUMMARY_URL = "https://ko.wikipedia.org/api/rest_v1/page/summary/"
WIKI_USER_AGENT = "UrbanScale/1.0 (https://github.com/anachtice/urban-scale; personal study site)"
SENTENCE_RX = re.compile(r"(?<=[다음함임됨])\.\s+")
BIRTH_RX = re.compile(r"\d{3,4}년\s*\d{1,2}월\s*\d{1,2}일")
PLACE_DESC_RX = re.compile(r"(의 도시|도시$|의 시$|의 군$|의 구$|의 동$|의 읍$|의 면$|행정 구역|법정동|행정동|의 수도|국가$|나라$|섬$|산$|강$|하천|호수|"
                           r"철도역|전철역|지하철역|역$|공항$|항구|다리$|교량|단지$|건축물$|빌딩$|거리$)")
WHO_DESC_RX = re.compile(r"(정치인|배우|가수|선수|기업인|법조인|작가|방송인|국회의원|시장$|도지사|교수|학자|감독|군인|인물|"
                         r"기업|회사|브랜드|기관|공단|공사$|정당|단체|협회|위원회$|은행$|그룹$|방송|신문|언론|"
                         r"드라마|영화|음반|노래|싱글|프로그램|게임|만화|소설|잡지|밴드|캐릭터|동음이의어|성씨|이름$)")
PROPER_RX = re.compile(r"(에 있는|에 위치한|에 위치하는|에 소재한|(기업|회사|건설사|그룹|브랜드|기관|공기업|정당|단체|협회|은행|방송사|신문사)(이다|이며|으로서))")
VERBISH_RX = re.compile(r"(었다|았다|였다|했다|됐다|한다|된다|는다|린다|른다|진다|온다|간다|운다|친다|낸다|있다|없다|아니다|싶다|렵다|롭다|겠다|"
                        r"하고|되고|지고|빠르고|하며|되며|하면|되면|해야|하는|되는|하던|있는|없는|지만|는데|면서|려고|도록|하자|"
                        r"어나|어져|아져|워져|라고|니까|었던|았던|해진|해서|돼서|어서|아서|쌓이는|이는|이고|이며|나온|오른|내린|찾아|돌아|적어|늘어|줄어)$")


def wiki_define(word):
    """위키백과 요약. 반환: (상태, 뜻) — 상태는 ok(뜻 있음) / none(문서 없음) / amb(동음이의어) / off(도시와 관계없음·고유명사)."""
    url = WIKI_SUMMARY_URL + urllib.parse.quote(word, safe="")
    try:
        data = json.loads(http_get(url, headers={"User-Agent": WIKI_USER_AGENT, "Accept": "application/json"}, tries=2))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "none", None
        raise
    if data.get("type") == "disambiguation":
        return "amb", None
    extract = clean(data.get("extract") or "")
    if not extract:
        return "none", None
    title, desc = data.get("title") or word, clean(data.get("description") or "")
    first = SENTENCE_RX.split(extract)[0]
    if BIRTH_RX.search(extract[:120]) or WHO_DESC_RX.search(desc) or PLACE_DESC_RX.search(desc) or PROPER_RX.search(first):
        return "off", {"title": title, "why": desc or first[:60]}
    sents = SENTENCE_RX.split(extract)
    text = sents[0]
    if len(sents) > 1 and len(text) + len(sents[1]) < 230:
        text = f"{text}. {sents[1]}"
    if len(text) > 260:
        text = text[:260].rstrip() + "…"
    elif not text.endswith((".", "…")):
        text += "."
    page = ((data.get("content_urls") or {}).get("desktop") or {}).get("page") \
        or "https://ko.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    return "ok", {"title": title, "text": text, "url": page, "src": "위키백과"}


def place_stems(cfg):
    stems = set()
    for group in ("서울", "신도시", "경기·인천"):
        for key, words in cfg.get(group, {}).items():
            stems.add(key)
            stems.update(words)
    for key in ("서울 전체", "경기·인천 전체", "수도권 전체", "비수도권"):
        stems.update(cfg.get(key, []))
    stems.update(cfg.get("조건이 붙는 말", {}).keys())
    return {re.sub(r"\s+", "", w) for w in stems if len(w) >= 2}


def update_new_terms(now, ctx):
    cfg = ctx.settings.get("사전 밖 용어")
    if not cfg:
        return
    gloss = load_config("glossary.json")
    known = set()
    for t in gloss.get("용어", []):
        known.update([t["용어"], *t.get("다른 이름", [])])
    for c in gloss.get("개념", []):
        known.update([c.get("개념", ""), c.get("한글", "")])
    known = {re.sub(r"\s+", "", k).upper() for k in known if len(k) >= 2}
    stems = place_stems(load_config("regions.json"))
    press_names = set(ctx.press_map.values())
    drop = set(ctx.settings["이슈 묶기"]["버릴 말"]) | set(cfg.get("버릴 말", []))
    days = cfg.get("살필 날 수", 7)

    count, on_days, examples = Counter(), defaultdict(set), defaultdict(list)
    dates = [(now - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(days)]
    used = []
    for d in dates:
        path = day_path(d)
        if not os.path.exists(path):
            continue
        used.append(d)
        with open(path, encoding="utf-8") as f:
            arts = json.load(f).get("articles", [])
        raws = [raw_words(a["t"]) for a in arts]
        vocab = {w for ws in raws for w in ws}
        for a, ws in zip(arts, raws):
            for t in set(title_tokens(ws, vocab, drop)):
                count[t] += 1
                on_days[t].add(d)
                if len(examples[t]) < 6:
                    examples[t].append({"t": a["t"], "u": a.get("u"), "p": a.get("p"), "d": d})

    def skip(t):
        if len(t) > 12 or any(ch.isdigit() for ch in t) or t in press_names:
            return True
        if any(k in t or t in k for k in known):
            return True
        if t.isascii():
            return not (t.isalpha() and 2 <= len(t) <= 6)  # 영어는 약어(UAM, BIM 등)만
        if not re.fullmatch(r"[가-힣A-Za-z]+", t) or VERBISH_RX.search(t):
            return True
        if t.endswith(("값", "일보", "신문", "뉴스", "방송", "건설", "이앤씨", "그룹", "홀딩스", "증권", "은행", "시장", "구청장", "의원", "장관")):
            return True
        if len(t) >= 3 and t.endswith("다") and (ord(t[-2]) - 0xAC00) % 28 in (4, 20):  # 'ㄴ다·ㅆ다'로 끝나는 말(오른다, 올랐다)
            return True
        return any(t.startswith(p) for p in stems)

    min_n, min_days = cfg.get("최소 기사 수", 4), cfg.get("최소 날 수", 2)
    cands = [t for t, n in count.most_common() if n >= min_n and len(on_days[t]) >= min(min_days, len(used)) and not skip(t)]

    cache = {}
    if os.path.exists(NEW_TERMS_CACHE_PATH):
        with open(NEW_TERMS_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
    today = now.strftime("%Y-%m-%d")
    retry = {"ok": cfg.get("다시 찾아볼 날 수(뜻 있음)", 90), "off": 90, "amb": 30, "none": cfg.get("다시 찾아볼 날 수(뜻 없음)", 14)}

    def stale(t):
        c = cache.get(t)
        if not c:
            return True
        try:
            age = (now.date() - datetime.strptime(c["at"], "%Y-%m-%d").date()).days
        except (KeyError, ValueError):
            return True
        return age >= retry.get(c.get("st"), 14)

    budget, looked, stopped = cfg.get("한 번에 찾아볼 수", 30), 0, ""
    for t in cands:
        if looked >= budget:
            break
        if not stale(t):
            continue
        try:
            st, info = wiki_define(t)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
            stopped = f"위키백과 응답 오류({getattr(e, 'code', '') or type(e).__name__}) — 다음 수집 때 이어서 찾음"
            break
        hits = {m for m in cfg.get("도시 관련 낱말", []) if m in f"{info['title']} {info['text']}"} if st == "ok" else set()
        if st == "ok" and len(hits) < cfg.get("도시 관련 낱말 최소 수", 2):  # '제도로'의 '도로'처럼 우연히 겹치는 경우를 줄이려고 2개 이상
            st, info = "off", {"title": info["title"], "why": "도시와 관계 적은 뜻"}
        cache[t] = {"st": st, "at": today, **({"def": info} if st == "ok" else {"why": (info or {}).get("why", "")} if info else {})}
        looked += 1
        time.sleep(1.0)

    def item(t):
        ex, seen_p = [], set()
        for e in sorted(examples[t], key=lambda x: x["d"], reverse=True):
            if e["p"] in seen_p:
                continue
            seen_p.add(e["p"])
            ex.append(e)
        return {"w": t, "n": count[t], "nd": len(on_days[t]), "ex": ex[:3]}

    found, missing, hidden, pending = [], [], 0, 0
    for t in cands:
        c = cache.get(t)
        if not c:
            pending += 1
        elif c["st"] == "ok":
            found.append({**item(t), "def": c["def"]})
        elif c["st"] == "none":
            missing.append({"w": t, "n": count[t], "nd": len(on_days[t])})
        else:
            hidden += 1
    keep = set(cands)
    cache = {t: c for t, c in cache.items() if t in keep or (now.date() - datetime.strptime(c["at"], "%Y-%m-%d").date()).days < 120}
    shown = cfg.get("보여줄 수", 40)
    save_json(NEW_TERMS_PATH, {
        "updated": now.isoformat(timespec="minutes"),
        "from": min(used) if used else None, "to": max(used) if used else None, "days": len(used),
        "min_n": min_n, "items": found[:shown], "missing": missing[:shown], "hidden": hidden, "pending": pending,
        "note": stopped,
    })
    save_json(NEW_TERMS_CACHE_PATH, cache)
    print(f"  사전 밖 용어: 후보 {len(cands)}개 · 뜻 찾음 {len(found)} · 못 찾음 {len(missing)} · 뺌 {hidden} · 이번에 찾아본 수 {looked}"
          + (f" · {stopped}" if stopped else ""))


# ───────────────────────── 학술 논문 (OpenAlex) ─────────────────────────
# config/journals.json 의 학술지에서 최근 논문의 제목·저자·초록·링크를 받는다. PDF 파일은 저장하지 않는다.

OPENALEX_URL = "https://api.openalex.org/works"
PAPERS_PATH = os.path.join(DATA_DIR, "papers.json")
PAPERS_LATEST_PATH = os.path.join(DATA_DIR, "papers-latest.json")
PAPERS_FORMAT = 3  # 저장 내용이 바뀌면 숫자를 올린다(올리면 다음 실행에서 바로 다시 받는다)
TRENDS_PATH = os.path.join(DATA_DIR, "trends.json")
TRENDS_FORMAT = 2


def abstract_text(inverted):
    """OpenAlex는 초록을 {낱말: [위치, ...]} 꼴로 준다. 위치 순서대로 다시 이어 붙인다."""
    if not inverted:
        return ""
    words = {}
    for w, positions in inverted.items():
        for p in positions:
            words[p] = w
    text = " ".join(words[p] for p in sorted(words))
    return re.sub(r"^(Abstract|ABSTRACT)[\s:.]+", "", text).strip()


def paper_record(w, journals_by_id, max_abstract):
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    j = journals_by_id.get((src.get("id") or "").rsplit("/", 1)[-1], {})
    authors = [(a.get("author") or {}).get("display_name") for a in (w.get("authorships") or [])]
    authors = [a for a in authors if a]
    oa = w.get("open_access") or {}
    abstract = abstract_text(w.get("abstract_inverted_index"))
    if len(abstract) > max_abstract:
        abstract = abstract[:max_abstract].rsplit(" ", 1)[0] + " …"
    doi = w.get("doi") or ""
    return {
        "id": (w.get("id") or "").rsplit("/", 1)[-1],
        "t": clean(w.get("title") or w.get("display_name") or ""),
        "j": j.get("약칭") or j.get("이름") or src.get("display_name", ""),
        "jg": j.get("구분", ""),
        "f": j.get("분야", ""),
        "date": w.get("publication_date") or "",
        "au": "; ".join(authors[:3]) + (" 외" if len(authors) > 3 else ""),
        "u": doi or loc.get("landing_page_url") or "",
        "pdf": (w.get("best_oa_location") or {}).get("pdf_url"),
        "oa": oa.get("oa_url") if oa.get("is_oa") else None,
        "ab": abstract,
        "tp": [t.get("display_name") for t in (w.get("topics") or [])[:2] if t.get("display_name")],
        "cite": w.get("cited_by_count") or 0,
    }


def fetch_papers(cfg, journals, now):
    by_id = {j["OpenAlex ID"]: j for j in journals}
    since = (now - timedelta(days=cfg.get("최근 며칠 논문을 보관할지", 90))).strftime("%Y-%m-%d")
    params = {
        "filter": f"primary_location.source.id:{'|'.join(by_id)},from_publication_date:{since},type:article|review",
        "sort": "publication_date:desc",
        "per_page": 100,  # OpenAlex 문서 기준 한 쪽 최대 100편
        "select": "id,doi,title,display_name,publication_date,primary_location,authorships,"
                  "abstract_inverted_index,open_access,best_oa_location,topics,cited_by_count",
    }
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    status = "키 없음"
    if key:
        params["api_key"] = key
        status = "키 사용"
    # 쪽 번호로 넘긴다. cursor 방식은 2026년부터 API 키가 있어야 두 번째 쪽을 준다.
    papers, page = [], 1
    while page <= 30:  # 100편씩 최대 30쪽
        params["page"] = page
        try:
            raw = http_get(OPENALEX_URL + "?" + urllib.parse.urlencode(params))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and "api_key" in params:
                # 키가 틀렸으면 키 없이 처음부터 다시 받는다. 키 값은 기록에 남기지 않는다.
                print(f"  ! OpenAlex가 API 키를 받지 않음({e.code}). 키 없이 다시 받음 — Secrets의 OPENALEX_API_KEY 확인 필요")
                params.pop("api_key")
                status, papers, page = "키 거절됨", [], 1
                continue
            raise
        results = json.loads(raw.decode("utf-8")).get("results") or []
        papers += [paper_record(w, by_id, cfg.get("초록 최대 글자 수", 700)) for w in results]
        if len(results) < params["per_page"]:
            break
        page += 1
        time.sleep(0.2)
    return [p for p in papers if p["t"]], status


def update_papers(now, force=False):
    """논문 목록은 하루 한 번(설정의 '갱신 간격')만 새로 받는다. 학술지 목록을 고치면 바로 다시 받는다."""
    try:
        cfg = load_config("journals.json")
    except FileNotFoundError:
        return
    journals = [j for j in cfg.get("학술지", []) if j.get("사용", True) and j.get("OpenAlex ID")]
    if not journals:
        return
    ml_words = cfg.get("머신러닝 낱말", [])
    sig = hashlib.md5(json.dumps([PAPERS_FORMAT, journals, cfg.get("최근 며칠 논문을 보관할지"), cfg.get("초록 최대 글자 수"), ml_words],
                                 ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:10]
    if os.path.exists(PAPERS_PATH) and not force:
        with open(PAPERS_PATH, encoding="utf-8") as f:
            old = json.load(f)
        try:
            fresh = now - datetime.fromisoformat(old.get("updated", "")) < timedelta(hours=cfg.get("갱신 간격(시간)", 20))
        except ValueError:
            fresh = False
        if fresh and old.get("sig") == sig:
            return
    try:
        papers, key_status = fetch_papers(cfg, journals, now)
    except Exception as e:  # 논문을 못 받아도 뉴스 수집은 그대로 끝낸다
        print(f"\n  ! 논문 목록을 받지 못함: {e} (이전 목록 유지)")
        return
    # 제목·초록에 머신러닝·예측 낱말이 있는 논문 표시 (영어 낱말은 낱말 단위로만 잡는다: 'shap'이 'shape'에 걸리지 않게)
    parts = [r"\b" + re.escape(w) + r"\b" if w.isascii() else re.escape(w) for w in ml_words]
    ml_rx = re.compile("|".join(parts), re.I) if parts else None
    for p in papers:
        p["ml"] = bool(ml_rx and ml_rx.search(f"{p['t']} {p['ab']}"))
    count = Counter(p["j"] for p in papers)
    meta = {"updated": now.isoformat(timespec="minutes"), "sig": sig, "days": cfg.get("최근 며칠 논문을 보관할지", 90),
            "key": key_status}
    save_json(PAPERS_PATH, {
        **meta,
        "journals": [{"name": j["이름"], "short": j.get("약칭") or j["이름"], "group": j.get("구분", ""),
                      "field": j.get("분야", ""), "n": count.get(j.get("약칭") or j["이름"], 0)} for j in journals],
        "papers": papers,
    })
    latest = sorted(papers, key=lambda p: p["date"], reverse=True)[:8]
    save_json(PAPERS_LATEST_PATH, {**meta, "papers": [{k: p[k] for k in ("id", "t", "j", "date", "u")} for p in latest]})
    print(f"\n논문 {len(papers)}편 저장 (학술지 {len(journals)}곳, OpenAlex {key_status}, 머신러닝·예측 "
          f"{sum(p['ml'] for p in papers)}편) · " + ", ".join(f"{k} {v}" for k, v in count.most_common(6)))


# ───────────────────────── 연구 동향 (OpenAlex) ─────────────────────────
# 최근 1년(두 달 전까지)과 그 전 1년의 주제·키워드별 논문 수를 세어 비교하고, 많이 나온 키워드끼리
# 같은 논문에 함께 붙은 횟수로 '키워드 지도'를 만든다. 일주일에 한 번 계산한다.

def oa_works(params, key):
    p = dict(params)
    if key:
        p["api_key"] = key
    return json.loads(http_get(OPENALEX_URL + "?" + urllib.parse.urlencode(p)).decode("utf-8"))


def oa_groups(filt, field, key):
    """filter에 맞는 논문을 field(주제·키워드)로 묶어 센다. {id: (이름, 편수)}"""
    out = {}
    for g in oa_works({"filter": filt, "group_by": field}, key).get("group_by") or []:
        gid = str(g.get("key") or "").rsplit("/", 1)[-1]
        if gid:
            out[gid] = (g.get("key_display_name") or gid, g.get("count") or 0)
    return out


def oa_total(filt, key):
    return (oa_works({"filter": filt, "per_page": 1, "select": "id"}, key).get("meta") or {}).get("count") or 0


def share_changes(recent, prev, n_recent, n_prev):
    """전체 논문 대비 비중이 그 전 1년보다 얼마나 변했는지. 그 전 상위 목록에 없던 것은 목록의 최솟값으로 본다(보수적)."""
    floor = min((c for _, c in prev.values()), default=0)
    rows = []
    for gid, (name, n) in recent.items():
        before = prev.get(gid, (name, None))[1]
        base = before if before is not None else floor
        share_now = n / max(n_recent, 1)
        share_before = (base + 5) / max(n_prev, 1)
        rows.append({"id": gid, "name": name, "n": n, "prev": before,
                     "change": round(share_now / share_before - 1, 3), "new": before is None})
    return rows


def force_layout(n, weights, iters=600, seed=11):
    """Fruchterman-Reingold 배치: 함께 자주 쓰인 키워드는 끌어당기고, 나머지는 서로 밀어낸다. 결과는 0~1 좌표."""
    import random
    rnd = random.Random(seed)
    pos = [[rnd.random(), rnd.random()] for _ in range(n)]
    k = math.sqrt(1.0 / max(n, 1))
    temp = 0.1
    for _ in range(iters):
        disp = [[0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx, dy = pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]
                d = math.hypot(dx, dy) or 1e-6
                f = k * k / d
                disp[i][0] += dx / d * f; disp[i][1] += dy / d * f
                disp[j][0] -= dx / d * f; disp[j][1] -= dy / d * f
        for (i, j), w in weights.items():
            dx, dy = pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]
            d = math.hypot(dx, dy) or 1e-6
            f = d * d / k * w
            disp[i][0] -= dx / d * f; disp[i][1] -= dy / d * f
            disp[j][0] += dx / d * f; disp[j][1] += dy / d * f
        for i in range(n):
            disp[i][0] += (0.5 - pos[i][0]) * 0.6  # 가운데로 약하게 당겨 외톨이 키워드가 멀리 달아나지 않게
            disp[i][1] += (0.5 - pos[i][1]) * 0.6
            length = math.hypot(*disp[i]) or 1e-6
            step = min(length, temp)
            pos[i][0] += disp[i][0] / length * step
            pos[i][1] += disp[i][1] / length * step
        temp = max(temp * 0.985, 0.002)
    def spread(vals):
        """가운데에 몰린 키워드를 고르게 편다: 원래 위치와 순위 위치를 반씩 섞는다(앞뒤 순서는 그대로)."""
        lo, hi = min(vals), max(vals)
        rank = {i: r / max(n - 1, 1) for r, i in enumerate(sorted(range(n), key=lambda i: vals[i]))}
        return [0.5 * (v - lo) / ((hi - lo) or 1) + 0.5 * rank[i] for i, v in enumerate(vals)]

    xs, ys = spread([p[0] for p in pos]), spread([p[1] for p in pos])
    return [[0.07 + 0.86 * x, 0.08 + 0.84 * y] for x, y in zip(xs, ys)]


def trend_scope(sc, journals, windows, n_map, key, now):
    if sc.get("기준") == "학술지":
        fields = sc.get("분야")
        fields = [fields] if isinstance(fields, str) else (fields or [])
        pool = [j for j in journals if not fields or j.get("분야") in fields]
        base = "primary_location.source.id:" + "|".join(j["OpenAlex ID"] for j in pool)
    else:
        base = "primary_topic.subfield.id:" + "|".join(sc.get("OpenAlex 세부분야", []))
    if sc.get("핵심 학술지만"):  # 자료 저장소·학회 초록·책을 빼고 핵심 학술지 논문만
        base += ",primary_location.source.is_core:true,primary_location.source.type:journal"
    scope_kw = sc.get("OpenAlex 키워드") or []
    if scope_kw:
        base += ",keywords.id:" + "|".join(scope_kw)
    base += ",type:article|review"
    f_recent, f_prev = f"{base},{windows[0]}", f"{base},{windows[1]}"
    n_recent, n_prev = oa_total(f_recent, key), oa_total(f_prev, key)
    topics = share_changes(oa_groups(f_recent, "primary_topic.id", key), oa_groups(f_prev, "primary_topic.id", key), n_recent, n_prev)
    kw_recent, kw_prev = oa_groups(f_recent, "keywords.id", key), oa_groups(f_prev, "keywords.id", key)
    for k in scope_kw:  # 범위를 정한 키워드는 모든 논문에 붙어 있어 지도·순위에서 뺀다
        kw_recent.pop(k, None)
    keywords = share_changes(kw_recent, kw_prev, n_recent, n_prev)

    # 키워드 지도: 많이 나온 키워드끼리 같은 논문에 함께 붙은 횟수
    top = sorted(keywords, key=lambda x: -x["n"])[:n_map]
    idx = {x["id"]: i for i, x in enumerate(top)}
    co = {}
    for x in top:
        for other, (_, c) in oa_groups(f"{f_recent},keywords.id:{x['id']}", "keywords.id", key).items():
            if other in idx and other != x["id"]:
                pair = tuple(sorted((idx[x["id"]], idx[other])))
                co[pair] = max(co.get(pair, 0), c)
        time.sleep(0.1)
    weights = {pair: c / math.sqrt(top[pair[0]]["n"] * top[pair[1]]["n"]) for pair, c in co.items()}
    strongest = max(weights.values(), default=1) or 1
    weights = {pair: w / strongest for pair, w in weights.items()}
    pos = force_layout(len(top), weights)
    shown = set()
    for i in range(len(top)):  # 키워드마다 가장 강한 연결 3개만 선으로 그린다
        mine = sorted(((w, pair) for pair, w in weights.items() if i in pair), reverse=True)[:3]
        shown.update(pair for w, pair in mine if w >= 0.08)

    cited = oa_works({"filter": f"{base},from_publication_date:{(now - timedelta(days=730)):%Y-%m-%d}",
                      "sort": "cited_by_count:desc", "per_page": 8,
                      "select": "id,doi,title,display_name,publication_year,primary_location,cited_by_count"}, key)
    min_n = max(10, int(n_recent * 0.002))
    return {
        "name": sc["이름"],
        "n": n_recent, "n_prev": n_prev,
        "topics": sorted(topics, key=lambda x: -x["n"])[:12],
        "rising_topics": sorted([t for t in topics if t["n"] >= min_n], key=lambda x: -x["change"])[:8],
        "keywords": sorted(keywords, key=lambda x: -x["n"])[:20],
        "rising_keywords": sorted([k for k in keywords if k["n"] >= min_n], key=lambda x: -x["change"])[:12],
        "map": {
            "nodes": [{"id": x["id"], "name": x["name"], "n": x["n"], "change": x["change"], "new": x["new"],
                       "x": round(pos[i][0], 4), "y": round(pos[i][1], 4)} for i, x in enumerate(top)],
            "links": [{"a": a, "b": b, "w": round(weights[(a, b)], 3)} for a, b in sorted(shown)],
        },
        "cited": [{
            "t": clean(w.get("title") or w.get("display_name") or ""),
            "j": (((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""),
            "y": w.get("publication_year"),
            "u": w.get("doi") or ((w.get("primary_location") or {}).get("landing_page_url") or ""),
            "cite": w.get("cited_by_count") or 0,
        } for w in cited.get("results") or []],
    }


def update_trends(now, force=False):
    try:
        cfg = load_config("journals.json")
    except FileNotFoundError:
        return
    tcfg = cfg.get("동향") or {}
    scopes = tcfg.get("범위") or []
    journals = [j for j in cfg.get("학술지", []) if j.get("사용", True) and j.get("OpenAlex ID")]
    if not scopes:
        return
    n_map = tcfg.get("지도에 넣을 키워드 수", 30)
    sig = hashlib.md5(json.dumps([TRENDS_FORMAT, scopes, [(j["OpenAlex ID"], j.get("분야")) for j in journals], n_map],
                                 ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:10]
    old = {}
    if os.path.exists(TRENDS_PATH):
        with open(TRENDS_PATH, encoding="utf-8") as f:
            prev_file = json.load(f)
        old = {s["name"]: s for s in prev_file.get("scopes", [])}
        try:
            fresh = now - datetime.fromisoformat(prev_file.get("updated", "")) < timedelta(days=tcfg.get("갱신 간격(일)", 7))
        except ValueError:
            fresh = False
        if fresh and prev_file.get("sig") == sig and not force:
            return
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    recent = (now - timedelta(days=425), now - timedelta(days=61))
    before = (now - timedelta(days=790), now - timedelta(days=426))
    windows = [f"from_publication_date:{a:%Y-%m-%d},to_publication_date:{b:%Y-%m-%d}" for a, b in (recent, before)]
    out = []
    for sc in scopes:
        try:
            out.append(trend_scope(sc, journals, windows, n_map, key, now))
            print(f"  연구 동향 '{sc['이름']}': 최근 1년 {out[-1]['n']:,}편")
        except Exception as e:  # 한 범위가 실패하면 지난번 결과를 그대로 둔다
            print(f"  ! 연구 동향 '{sc['이름']}' 계산 실패: {e}")
            if sc["이름"] in old:
                out.append(old[sc["이름"]])
    if not out:
        return
    save_json(TRENDS_PATH, {
        "updated": now.isoformat(timespec="minutes"), "sig": sig, "key": "키 사용" if key else "키 없음",
        "recent": [f"{recent[0]:%Y-%m-%d}", f"{recent[1]:%Y-%m-%d}"],
        "before": [f"{before[0]:%Y-%m-%d}", f"{before[1]:%Y-%m-%d}"],
        "scopes": out,
    })


# ───────────────────────── 실행 ─────────────────────────

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Urban Scale 수집기")
    parser.add_argument("--source", choices=["naver", "rss"], default="naver",
                        help="naver: 네이버 뉴스 검색 API (기본), rss: 키 없이 시험하는 구글 뉴스 RSS")
    parser.add_argument("--reprocess", action="store_true", help="새로 모으지 않고 저장된 오늘·어제 기사만 다시 분류")
    parser.add_argument("--papers", action="store_true", help="논문 목록을 지금 바로 새로 받기")
    parser.add_argument("--trends", action="store_true", help="연구 동향을 지금 바로 다시 계산하기")
    args = parser.parse_args()

    load_env_file()
    ctx = Context()
    now = datetime.now(KST)
    updated = now.strftime("%Y-%m-%dT%H:%M")
    os.makedirs(DATA_DIR, exist_ok=True)
    index, last_run = load_index()

    touched = {}
    if args.reprocess:
        for k in (1, 0):
            d = (now - timedelta(days=k)).strftime("%Y-%m-%d")
            records = load_day(d)
            if records:
                touched[d] = records
    else:
        groups = load_config("queries.json")["검색어"]
        queries = list(dict.fromkeys(q for qs in groups.values() for q in qs))
        # 지난번 수집 1시간 전부터만 받는다(호출 수를 줄이려고). 처음이거나 오래 쉬었으면 '최근 몇 시간'만큼 받는다.
        since = now - timedelta(hours=ctx.settings["최근 몇 시간 기사를 모을지"])
        if last_run and args.source == "naver":
            since = max(since, last_run - timedelta(hours=1))
        print(f"검색어 {len(queries)}개로 수집 시작 ({'네이버 API' if args.source == 'naver' else '구글 뉴스 RSS 시험'}) · "
              f"{since.strftime('%m-%d %H:%M')} 이후 기사")
        fetched, failed = collect(args.source, queries, ctx.settings, ctx.press_map, since)
        if len(failed) == len(queries):
            sys.exit("모든 검색이 실패했습니다. API 키와 인터넷 연결을 확인해 주세요.")
        if args.source == "naver" and len(failed) <= len(queries) * 0.1:
            last_run = now  # 거의 다 성공했을 때만 다음 수집의 기준 시각으로 삼는다
        by_date = defaultdict(dict)
        for aid, a in fetched.items():
            by_date[a["pub"].strftime("%Y-%m-%d")][aid] = to_record(aid, a, ctx.settings["요약 최대 글자 수"])
        for d, new in by_date.items():
            records = load_day(d)
            for aid, rec in new.items():
                if aid in records:
                    records[aid]["q"] = sorted(set(records[aid].get("q") or []) | set(rec["q"]))
                else:
                    records[aid] = rec
            touched[d] = records
        print(f"받은 기사 {len(fetched)}건 (실패한 검색어 {len(failed)}개)")

    for d in sorted(touched):
        day = process_day(d, touched[d], ctx, updated)
        save_json(day_path(d), day)
        index[d] = {"d": d, "n": day["count"], "u": updated}
        report(day)
    write_index(index, ctx.settings.get("보관 일수", 0), now, last_run)
    publish_glossary()
    update_people(now, last_run, ctx, args.source)
    try:
        update_new_terms(now, ctx)
    except Exception as e:  # 이 단계가 실패해도 기사 수집 결과는 저장되게
        print("  사전 밖 용어 단계 실패:", e)
    update_papers(now, force=args.papers)
    update_trends(now, force=args.trends)
    print("\n저장 완료:", os.path.relpath(DATA_DIR, ROOT))


if __name__ == "__main__":
    main()
