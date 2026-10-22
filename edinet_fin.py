# -*- coding: utf-8 -*-
"""
C-140: 金融庁 EDINET API（v2）から、有価証券報告書の数値（XBRL を CSV にしたもの）を取る。

書籍の手順（5ステップ＋6手順）を**時点どおりに**組み直すには、全銘柄の過去の年次決算が要る。
EDINET DB（セッションの接続）は1社10期で2万〜5万字あり全銘柄を取れず、絞り込みは最新期でしか
かけられない（過去時点の選別に使うと先読み）。→ ユーザー判断「**B1**」（2026-10-19）で一次情報から取る。

⚠ **サンドボックスからは届かない**（egress で拒否・2026-10-19 確認）。CI だけで動く。
⚠ キーは GitHub の secret `EDINET_API_KEY`。**ログに出さない** ─ 例外文字列は URL を含むので `_mask` で伏せる。
⚠ まず `--probe` で**応答と CSV の形を印字して確かめてから**読み方を書く（C-51 の信用残と同じ手順・知見3-538）。
"""
import csv
import io
import os
import re
import sys
import zipfile
from collections import Counter

import requests

API = "https://api.edinet-fsa.go.jp/api/v2"
ASR = "120"                  # 書類種別コード: 有価証券報告書
PROBE_DATES = ("2025-06-27", "2025-06-26", "2025-06-25")   # 3月期の有報が集中する日

# 印字する要素の目印（probe 用。読み方を決めるための候補で、まだ確定していない）
WANT = re.compile(r"(NetSales|Revenue|OperatingIncome|OperatingProfit|DividendPaidPerShare|"
                  r"EarningsLossPerShare|PayoutRatio|ShareholdersEquity|EquityAttributableToOwners|"
                  r"NumberOfIssuedShares|IssuedShares|SecurityCodeDEI|FiscalYearEnd|"
                  r"AccountingStandards|WhetherConsolidated)", re.I)


def _key():
    """secret に貼るときに混ざりやすい前後の空白・改行・引用符を落とす"""
    return (os.environ.get("EDINET_API_KEY") or "").strip().strip('"').strip("'").strip()


def key_shape():
    """キーそのものは出さず、形だけ（文字数・英数字だけか・元の値に空白や引用符が混ざっていたか）"""
    raw = os.environ.get("EDINET_API_KEY") or ""
    k = _key()
    import hashlib
    return {"len": len(k), "alnum": k.isalnum(), "trimmed": raw != k,
            # 取り違え検出用の指紋（SHA-256 の先頭8桁・元に戻せない）。secret を更新すると変わる
            "fp": hashlib.sha256(k.encode()).hexdigest()[:8] if k else None,
            # 2026-10-21: 発行元の見分け（キーの中身は出さない）─ EDINET DB（民間・edinetdb.jp）のキーは
            #   「edb_」で始まり、金融庁 EDINET API では使えない（EDINET DB の回答）。10-19 に最初に登録された
            #   キーは36文字で記号入り＝「edb_」＋32文字と同じ形だった
            "edb_prefix": k.startswith("edb_"),
            "hex": bool(k) and all(c in "0123456789abcdefABCDEF" for c in k),
            "lower": k == k.lower()}


def try_auth(d=PROBE_DATES[0], log=print):
    """キーの渡し方を3通り試し、それぞれの応答の StatusCode を印字する（キーが悪いのか渡し方が悪いのかを分ける）"""
    k = _key()
    ways = (("クエリ Subscription-Key", {"Subscription-Key": k}, {}),
            ("クエリ subscription-key", {"subscription-key": k}, {}),
            ("ヘッダ Ocp-Apim-Subscription-Key", {}, {"Ocp-Apim-Subscription-Key": k}))
    out = {}
    for name, extra, headers in ways:
        try:
            p = {"date": str(d), "type": 2}
            p.update(extra)
            r = requests.get(API + "/documents.json", params=p, headers=headers, timeout=60)
            try:
                j = r.json()
            except Exception:
                j = {}
            sc = j.get("StatusCode") if isinstance(j, dict) else None
            st = (j.get("metadata") or {}).get("status") if isinstance(j, dict) else None
            n = len(j.get("results") or []) if isinstance(j, dict) else 0
            out[name] = (r.status_code, sc, st, n)
            log("probe: 渡し方 {} → HTTP {} / StatusCode {} / metadata.status {} / 件数 {}".format(
                name, r.status_code, sc, st, n))
        except Exception as e:
            out[name] = ("例外", _mask(e))
            log("probe: 渡し方 {} → 例外 {}".format(name, _mask(e)))
    return out


def _mask(s):
    """キーを含みうる文字列（例外・URL）を伏せる。URL に入るときは %XX / + に符号化されるので、その形も伏せる（レビュー8回目）"""
    from urllib.parse import quote, quote_plus
    k = _key()
    s = str(s)
    if k:
        for f in sorted({k, quote(k, safe=""), quote_plus(k)}, key=len, reverse=True):
            s = s.replace(f, "***")
    return s


class Rejected(RuntimeError):
    """EDINET がキーを拒否した（StatusCode 401 など）─ その日だけの失敗ではないので、取得そのものを止める"""


def _get(path, params, timeout=60):
    p = dict(params)
    p["Subscription-Key"] = _key()
    r = requests.get(API + path, params=p, timeout=timeout)
    r.raise_for_status()
    return r


def list_docs(d, raw=None):
    """その日に提出された書類の一覧（type=2: メタデータ付き）。`raw` に dict を渡すと応答の素性を入れて返す"""
    r = _get("/documents.json", {"date": str(d), "type": 2})
    j = r.json()
    if raw is not None:
        raw.update({"http": r.status_code, "type": r.headers.get("Content-Type"),
                    "keys": sorted(j.keys()) if isinstance(j, dict) else type(j).__name__,
                    "head": _mask(r.text[:300])})
    if not isinstance(j, dict):
        raise RuntimeError("EDINET の一覧が dict でない（{}）".format(type(j).__name__))
    # 🔴 EDINET はキーが無効でも HTTP 200 で {"StatusCode": 401, ...} を返す（2026-10-19 診断）。
    #    これを「その日は0件」と読むと、全期間が黙って空になる（知見3-543・3-545）
    sc = j.get("StatusCode")
    if sc is not None and str(sc) != "200":
        raise Rejected("EDINET が拒否: StatusCode {} {}".format(sc, _mask(j.get("message"))))
    # 🔴 キー以外のエラー（400・404・500）は HTTP 200 のまま metadata.status に入る（2026-10-22 レビュー7回目）。
    #    status を見ないと「その日は0件」と読み、月が完了になって二度と取りに行かない（成功は status 200・10-21 診断）
    meta = j.get("metadata") or {}
    if str(meta.get("status")) != "200":
        raise RuntimeError("EDINET の一覧が失敗: metadata.status {} {}".format(meta.get("status"), _mask(meta.get("message"))))
    res = j.get("results")
    if res is None:
        # 書類の無い日（休日）は件数0。件数0と書いていないのに results が無いのは、0件の日ではない（レビュー8回目）
        if str((meta.get("resultset") or {}).get("count")) != "0":
            raise RuntimeError("EDINET の一覧に results が無い（件数 {}）".format((meta.get("resultset") or {}).get("count")))
        res = []
    if not isinstance(res, list):
        raise RuntimeError("EDINET の一覧の results が list でない（{}）".format(type(res).__name__))
    return meta, res


def fetch_csv_zip(doc_id):
    """書類の XBRL を CSV にした ZIP（type=5）"""
    return _get("/documents/%s" % doc_id, {"type": 5}, timeout=120).content


def _decode(b):
    for enc in ("utf-16", "utf-8-sig", "cp932"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def parse_csv_zip(content):
    """ZIP 内の CSV を全部読み、見出し行の列名をキーにした行のリストを返す（`_file` に元のファイル名）"""
    rows = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv"):
                continue
            rd = csv.reader(io.StringIO(_decode(z.read(name))), delimiter="\t")
            head = None
            for cells in rd:
                cells = [c.strip() for c in cells]
                if head is None:
                    head = cells
                    continue
                if len(cells) < len(head):
                    continue
                r = dict(zip(head, cells))
                r["_file"] = name.rsplit("/", 1)[-1]
                rows.append(r)
    return rows


def _col(head, word):
    """見出しのうち `word` を含む最初の列名（列名は probe で確かめるまで決め打ちしない）"""
    for h in head:
        if word in h:
            return h
    return None


def probe(log=print):
    """応答と CSV の形を印字する（**何も保存しない**）"""
    if not _key():
        log("probe: EDINET_API_KEY が無い ─ GitHub の Settings → Secrets and variables → Actions に登録してください")
        return 1
    log("probe: キーの形 {}".format(key_shape()))
    try_auth(log=log)
    asr, used = [], None
    for d in PROBE_DATES:
        raw = {}
        try:
            meta, res = list_docs(d, raw)
        except Exception as e:
            log("probe: {} の一覧を取れない ({})".format(d, _mask(e)))
            continue
        log("probe: {} HTTP {} {} / 応答のキー {} / metadata={} 件数={}".format(
            d, raw.get("http"), raw.get("type"), raw.get("keys"), meta, len(res)))
        if not res:
            log("probe:   応答の先頭 {}".format(raw.get("head")))
        if res:
            log("probe:   1件目のキー {}".format(sorted(res[0].keys())))
            log("probe:   書類種別の内訳 {}".format(Counter(str(x.get("docTypeCode")) for x in res).most_common(8)))
        asr = [x for x in res if str(x.get("docTypeCode")) == ASR and str(x.get("csvFlag")) == "1"
               and x.get("secCode")]
        log("probe:   有報（CSVあり・証券コードあり）{}件".format(len(asr)))
        for x in asr[:3]:
            log("probe:     {} {} {} 期間 {}〜{} 提出 {}".format(
                x.get("docID"), x.get("secCode"), x.get("filerName"),
                x.get("periodStart"), x.get("periodEnd"), x.get("submitDateTime")))
        if asr:
            used = d
            break
    if not asr:
        log("probe: 有報が見つからない")
        return 1
    for x in asr[:2]:
        try:
            content = fetch_csv_zip(x["docID"])
        except Exception as e:
            log("probe: {} の CSV を取れない ({})".format(x.get("docID"), _mask(e)))
            continue
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            log("probe: {} ZIP {}bytes / 中身 {}".format(x["docID"], len(content), z.namelist()[:12]))
        rows = parse_csv_zip(content)
        if not rows:
            log("probe:   行が読めない")
            continue
        head = [k for k in rows[0].keys() if k != "_file"]
        log("probe:   列 {} / {}行".format(head, len(rows)))
        ce, cc, cv = _col(head, "要素ID"), _col(head, "コンテキストID"), _col(head, "値")
        if not (ce and cc and cv):
            log("probe:   列名が想定と違う ─ 先頭3行 {}".format(rows[:3]))
            continue
        hit = [r for r in rows if WANT.search(r.get(ce, ""))]
        log("probe:   目印に当たる行 {}件（先頭80件）".format(len(hit)))
        for r in hit[:80]:
            log("probe:     {} | {} | {} | {}".format(r.get(ce), r.get(cc), r.get(cv), r.get("_file")))
    log("probe: 使った日 {}".format(used))
    return 0


PROBE_PAT = re.compile(r"(OperatingIncome|OperatingProfit|OperatingRevenue|Revenue|NetSales|Sales|OrdinaryIncome|"
                       r"ProfitLossBeforeTax|IncomeBeforeIncomeTaxes)", re.I)


def probe_docs(ids, log=print, sleep=0.7):
    """C-140 の穴（2026-10-22）: 指定した書類の、売上・営業利益に当たりそうな要素を**要素名・文脈・値**で印字する
    （IFRS・米国基準で営業利益・売上が空欄だった 50社 263通の読み方を決めるため。何も保存しない）"""
    import time
    for d in ids:
        try:
            rows = parse_csv_zip(fetch_csv_zip(d))
        except Exception as e:
            log("probe-docs: {} を取れない ({})".format(d, _mask(e)))
            continue
        head = [k for k in rows[0].keys() if k != "_file"] if rows else []
        ce, cc, cv = _col(head, "要素ID"), _col(head, "コンテキストID"), _col(head, "値")
        name = next((r.get(cv) for r in rows if r.get(ce, "").endswith("FilerNameInJapaneseDEI")), "")
        std = next((r.get(cv) for r in rows if r.get(ce, "").endswith("AccountingStandardsDEI")), "")
        log("probe-docs: ==== {} {} {}".format(d, name, std))
        seen = set()
        for r in rows:
            e, c, v = r.get(ce, ""), r.get(cc, ""), r.get(cv, "")
            if not PROBE_PAT.search(e) or not c.startswith(("CurrentYearDuration", "Prior1YearDuration")):
                continue
            if "Member" in c and "NonConsolidatedMember" not in c:
                continue
            key = (e, c)
            if key in seen:
                continue
            seen.add(key)
            log("probe-docs:   {} | {} | {}".format(e, c, v))
        time.sleep(sleep)
    return 0


SURVEY_DATES = (("2025-06-27", None), ("2025-03-27", 150))   # 3月期の集中日（全件）と12月期の集中日（先頭150件）
_NUM = re.compile(r"^-?[0-9][0-9,]*(\.[0-9]+)?$")
_FS = re.compile(r"^(jppfs_cor|jpigp_cor|jpcrp_cor):.*(Sales|Revenue|OperatingIncome|OperatingProfit|"
                 r"ShareholdersEquity|EquityAttributable|NetAssets|Equity$)", re.I)


def asr_list(d, no_csv=None):
    """その日に提出された有報（国内会社・CSVあり・証券コードあり・取下げ無し）。
    `no_csv` に set を渡すと、**証券コードがあるのに CSV が無い有報**の docID を入れる（取れないが、黙って消さず数を残す）"""
    _meta, res = list_docs(d)
    out = []
    for x in res:
        if str(x.get("docTypeCode")) != ASR or not x.get("secCode") or str(x.get("withdrawalStatus") or "0") != "0":
            continue
        if str(x.get("csvFlag")) == "1":
            out.append(x)
        elif no_csv is not None:
            no_csv.add(x.get("docID"))
    return out


def survey(log=print, sleep=0.5):
    """C-140: 全量を取る前に、**会計基準ごとにどの要素名が使われているか**を数える（何も保存しない）。
    売上・営業利益・EPS・配当・自己資本の要素名は日本基準・IFRS・米国基準で違う。読み方は数えてから決める"""
    import time
    if not _key():
        log("survey: EDINET_API_KEY が無い")
        return 1
    by_std, summ, fs, cov = Counter(), {}, {}, {}
    for d, cap in SURVEY_DATES:
        try:
            docs = asr_list(d)
        except Exception as e:
            log("survey: {} の一覧を取れない ({})".format(d, _mask(e)))
            continue
        docs = docs[:cap] if cap else docs
        log("survey: {} 有報 {}件".format(d, len(docs)))
        for x in docs:
            try:
                rows = parse_csv_zip(fetch_csv_zip(x["docID"]))
            except Exception as e:
                log("survey: {} を取れない ({})".format(x.get("docID"), _mask(e)))
                continue
            finally:
                time.sleep(sleep)
            rows = [r for r in rows if r.get("_file", "").startswith("jpcrp")]
            dei = {r.get("要素ID"): r.get("値") for r in rows if str(r.get("要素ID", "")).startswith("jpdei_cor:")}
            std = "{} / 連結{}".format(dei.get("jpdei_cor:AccountingStandardsDEI") or "?",
                                     dei.get("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI") or "?")
            by_std[std] += 1
            se, fe = set(), set()
            for r in rows:
                e, c, v = r.get("要素ID", ""), r.get("コンテキストID", ""), r.get("値", "")
                if not _NUM.match(v.replace(",", "")):
                    continue
                if "SummaryOfBusinessResults" in e and c in (
                        "CurrentYearDuration", "CurrentYearInstant",
                        "CurrentYearDuration_NonConsolidatedMember", "CurrentYearInstant_NonConsolidatedMember"):
                    se.add("{} [{}]".format(e.split(":", 1)[-1], "個別" if "NonConsolidated" in c else "連結"))
                if c in ("CurrentYearDuration", "CurrentYearInstant") and _FS.search(e) \
                        and "SummaryOfBusinessResults" not in e:
                    fe.add(e)
            summ.setdefault(std, Counter()).update(se)
            fs.setdefault(std, Counter()).update(fe)
            # 5期分の売上がそろうか（③の「直近5期で4回続けて増収」に要る）
            n5 = {r.get("コンテキストID") for r in rows
                  if "SummaryOfBusinessResults" in r.get("要素ID", "")
                  and re.search(r"(NetSales|Revenue|OperatingRevenue)", r.get("要素ID", ""))
                  and _NUM.match(r.get("値", "").replace(",", ""))
                  and re.match(r"^(Current|Prior[1-4])YearDuration$", r.get("コンテキストID", ""))}
            cov.setdefault(std, Counter())["売上5期そろう" if len(n5) >= 5 else "売上{}期".format(len(n5))] += 1
    log("survey: 会計基準 × 連結の内訳 {}".format(by_std.most_common()))
    for std, n in by_std.most_common():
        log("survey: ==== {}（{}社）売上の期数 {}".format(std, n, cov.get(std, Counter()).most_common()))
        log("survey:   主要な経営指標（今期の値がある社数・上位45）")
        for e, k in summ.get(std, Counter()).most_common(45):
            log("survey:     {:4d} {}".format(k, e))
        log("survey:   財務諸表の要素（今期・メンバー無し・上位30）")
        for e, k in fs.get(std, Counter()).most_common(30):
            log("survey:     {:4d} {}".format(k, e))
    return 0


# =========================================================
# 全量の取得（2026-10-21・survey 419社の結果で読み方を固定した ─ C-140「読み方」の表）
# =========================================================
_DUR = ("Current", "Prior1", "Prior2", "Prior3", "Prior4")
SALES_JP = ("jpcrp_cor:NetSalesSummaryOfBusinessResults", "jpcrp_cor:OperatingRevenue1SummaryOfBusinessResults",
            "jpcrp_cor:NetSalesAndOperatingRevenueSummaryOfBusinessResults",
            "jpcrp_cor:OperatingRevenue2SummaryOfBusinessResults")
SALES_IFRS = ("jpcrp_cor:RevenueIFRSSummaryOfBusinessResults", "jpcrp_cor:NetSalesIFRSSummaryOfBusinessResults")
SALES_US = ("jpcrp_cor:RevenuesUSGAAPSummaryOfBusinessResults",)
# 🔴 C-140 の穴（2026-10-22）: 標準の要素が無いとき、**要素名の末尾（接頭辞を除いた名前）**で会社独自の要素を拾う。
#   ソフトバンク・日本ハムは要約の売上が `jpcrp030000-asr_E04426-000:NetSalesIFRSSummaryOfBusinessResults`、
#   トヨタは `…:OperatingRevenuesIFRSKeyFinancialData`。標準の要素を先に見るので、これまで取れていた会社の値は変わらない
SALES_LOCAL = re.compile(r"^(NetSales|Revenue|Revenues|OperatingRevenue\d?|OperatingRevenues|NetSalesAndOperatingRevenue|"
                         r"TotalNetRevenues|SalesRevenues)(IFRS|USGAAP|JMIS)?(SummaryOfBusinessResults|KeyFinancialData)$")
OP_LOCAL = re.compile(r"^(OperatingIncome|OperatingProfit|OperatingIncomeLoss|OperatingProfitLoss)(IFRS|USGAAP|JMIS)?"
                      r"(SummaryOfBusinessResults|KeyFinancialData)?$")
OP = ("jppfs_cor:OperatingIncome", "jpigp_cor:OperatingProfitLossIFRS")
SALES_FS = ("jppfs_cor:NetSales", "jpigp_cor:RevenueIFRS")
EPS = ("jpcrp_cor:BasicEarningsLossPerShareSummaryOfBusinessResults",
       "jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults",
       "jpcrp_cor:BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults")
EQ_RATIO = ("jpcrp_cor:EquityToAssetRatioSummaryOfBusinessResults",
            "jpcrp_cor:RatioOfOwnersEquityToGrossAssetsIFRSSummaryOfBusinessResults",
            "jpcrp_cor:EquityToAssetRatioIFRSSummaryOfBusinessResults",
            "jpcrp_cor:EquityToAssetRatioUSGAAPSummaryOfBusinessResults")
ASSETS = ("jpcrp_cor:TotalAssetsSummaryOfBusinessResults", "jpcrp_cor:TotalAssetsIFRSSummaryOfBusinessResults",
          "jpcrp_cor:TotalAssetsUSGAAPSummaryOfBusinessResults")
EQUITY_FS = ("jppfs_cor:ShareholdersEquity", "jpigp_cor:EquityAttributableToOwnersOfParentIFRS")
# 補助(c)＝アプリ v16 の式（NOPAT＝営業利益×(1−実効税率)）にだけ使う。⚠ survey で数えていない要素なので、
#   読めなければ空のまま（主判定は書籍の原式で税率を使わない）
PRETAX = ("jppfs_cor:IncomeBeforeIncomeTaxes", "jpigp_cor:ProfitLossBeforeTaxIFRS")
TAX = ("jppfs_cor:IncomeTaxes", "jpigp_cor:IncomeTaxExpenseIFRS")
COLS = ["docID", "code4", "edinetCode", "filerName", "submit", "periodStart", "periodEnd", "fy_end", "std", "cons",
        "sales", "sales_p1", "sales_p2", "sales_p3", "sales_p4", "sales_elem", "sales_mixed", "sales_fs", "op",
        "eps", "dps", "dps_dash", "payout_rep", "eq_ratio", "assets", "equity_fs", "shares_fy", "shares_filing",
        "pretax", "tax"]
_DASH = ("－", "-", "―", "ー", "—")


def _f(x):
    x = (x or "").replace(",", "").strip()
    try:
        return float(x)
    except ValueError:
        return None


def extract(rows, meta=None):
    """有報1通の CSV 行から、書籍の手順に要る数値だけを1行にする。
    ⚠ 連結を作る会社は連結の値（コンテキストにメンバー無し）、作らない会社は個別（`_NonConsolidatedMember`）。
    ⚠ 1株配当・発行済株式数は「主要な経営指標等」の**提出会社（個別）**の欄にしかない。未配当の「－」は 0 にして印を残す。
    ⚠ セグメント別の行（同じ要素IDで `…Member` 付き）は読まない ─ コンテキストを完全一致で引く"""
    meta = meta or {}
    v = {}
    for r in rows:
        if not str(r.get("_file", "")).startswith("jpcrp"):
            continue
        v.setdefault((r.get("要素ID"), r.get("コンテキストID")), r.get("値"))
    dei = {e.split(":", 1)[1]: x for (e, _c), x in v.items() if str(e).startswith("jpdei_cor:")}
    std = dei.get("AccountingStandardsDEI") or ""
    cons = str(dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI")).lower() == "true"
    sfx = "" if cons else "_NonConsolidatedMember"

    def get(elems, ctx, dash_ok=False):
        # ⚠ 「－」（その期の値が無い）は次の候補へ進む（2026-10-21 独立レビュー）─ IFRS に移った会社は、
        #   移行前の年の IFRS 売上が「－」で日本基準の売上の行に値がある。「－」で止まると③が通らなくなる。
        #   「－」そのものに意味があるのは1株配当（無配）だけ
        for e in elems:
            x = v.get((e, ctx))
            if x is None or x == "" or (not dash_ok and x.strip() in _DASH):
                continue
            return e, x
        return None, None

    # 移行した会社は前の年が前の基準の行にある（日本基準→IFRS は 2026-10-21 レビュー2回目、米国基準→IFRS は 2026-10-22:
    #   ソニー・トヨタ・TDK 等の移行の年の前期以前が米国基準の売上の行にしか無かった）。前に埋まっていた値は変わらない（後ろに足すだけ）
    # 標準の要素の後ろに、末尾の名前が合う会社独自の要素を足す（文脈は完全一致で引くので、セグメント行は拾わない）
    local = sorted({e for (e, _c) in v if ":" in str(e) and not str(e).startswith(("jpcrp_cor:", "jppfs_cor:", "jpigp_cor:"))})
    local_sales = tuple(e for e in local if SALES_LOCAL.match(e.split(":", 1)[1]))
    # ⚠ IFRS の会社の前の基準の米国基準の行は**会社独自の要素の後ろ**（2026-10-22 取り直し2回目の「前の値が変わった」6件:
    #   トヨタの移行の年は前期を IFRS で組み直した値〔会社独自の KeyFinancialData・29.87兆〕と米国基準の値〔29.93兆〕の
    #   両方があり、米国基準を先に読んでいた）。同じ基準で組み直した値が先
    sales_order = (SALES_IFRS + SALES_JP + local_sales + SALES_US if std == "IFRS"
                   else SALES_US + SALES_JP + local_sales + SALES_IFRS if std == "US GAAP"
                   else SALES_JP + SALES_IFRS + local_sales + SALES_US)
    op_order = tuple(e for e in local if OP_LOCAL.match(e.split(":", 1)[1]))
    out = {"std": std, "cons": cons}
    used = []
    for k, col in zip(_DUR, ("sales", "sales_p1", "sales_p2", "sales_p3", "sales_p4")):
        e, x = get(sales_order, k + "YearDuration" + sfx)
        out[col] = _f(x)
        if e:
            used.append(e.split(":", 1)[1].replace("SummaryOfBusinessResults", ""))
    out["sales_elem"] = used[0] if used else ""
    out["sales_mixed"] = len(set(used)) > 1
    ifrs = std == "IFRS"
    out["sales_fs"] = _f(get(SALES_FS[::-1] if ifrs else SALES_FS, "CurrentYearDuration" + sfx)[1])
    out["op"] = _f(get((OP[::-1] if ifrs else OP) + op_order, "CurrentYearDuration" + sfx)[1])
    out["eps"] = _f(get(EPS[1:] + EPS[:1] if std == "IFRS" else EPS[2:] + EPS[:2] if std == "US GAAP" else EPS,
                        "CurrentYearDuration" + sfx)[1])
    _e, d = get(("jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults",), "CurrentYearDuration_NonConsolidatedMember",
                dash_ok=True)
    out["dps_dash"] = (d or "").strip() in _DASH
    out["dps"] = 0.0 if out["dps_dash"] else _f(d)
    out["payout_rep"] = _f(get(("jpcrp_cor:PayoutRatioSummaryOfBusinessResults",),
                               "CurrentYearDuration_NonConsolidatedMember")[1])
    out["eq_ratio"] = _f(get(EQ_RATIO[1:] + EQ_RATIO[:1] if std == "IFRS" else EQ_RATIO, "CurrentYearInstant" + sfx)[1])
    # ⚠ IFRS は IFRS → 日本基準 → 米国基準の順（2026-10-22: 米国基準の要素を足して逆順にしたら、米国基準から IFRS に移った年の
    #   TDK〔S100ODQV〕が米国基準の総資産を読んだ ─ 取り直しの「前の値が変わった」で見つかった）
    out["assets"] = _f(get(ASSETS[1::-1] + ASSETS[2:] if std == "IFRS" else ASSETS[2:] + ASSETS[:2] if std == "US GAAP"
                           else ASSETS, "CurrentYearInstant" + sfx)[1])
    out["equity_fs"] = _f(get(EQUITY_FS[::-1] if std == "IFRS" else EQUITY_FS, "CurrentYearInstant" + sfx)[1])
    out["shares_fy"] = _f(get(("jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults",),
                              "CurrentYearInstant_NonConsolidatedMember")[1])
    out["shares_filing"] = _f(get(("jpcrp_cor:NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc",),
                                  "FilingDateInstant")[1])
    out["pretax"] = _f(get(PRETAX[::-1] if ifrs else PRETAX, "CurrentYearDuration" + sfx)[1])
    out["tax"] = _f(get(TAX[::-1] if ifrs else TAX, "CurrentYearDuration" + sfx)[1])
    out["fy_end"] = dei.get("CurrentFiscalYearEndDateDEI") or ""
    import code_utils as CU
    out.update({"docID": meta.get("docID", ""), "code4": CU.norm_code(meta.get("secCode")) or "",
                "edinetCode": meta.get("edinetCode", ""), "filerName": meta.get("filerName", ""),
                "submit": meta.get("submitDateTime", ""), "periodStart": meta.get("periodStart", ""),
                "periodEnd": meta.get("periodEnd", "")})
    return {c: out.get(c) for c in COLS}


def _month_days(ym):
    import datetime as _dt
    y, m = map(int, ym.split("-"))
    d = _dt.date(y, m, 1)
    while d.month == m:
        yield d
        d += _dt.timedelta(days=1)


def _done_path():
    import paths as P
    return P.EDINET_DIR / "_done.json"


def _done_dir():
    import paths as P
    return P.EDINET_DIR / "_done"


LIST_V = 2   # 一覧の応答を metadata.status まで検査する版（2026-10-22）。これより前の「完了」は一覧を取り直して確かめる


def _rank(rec):
    return (int(rec.get("v", 1) or 1), bool(rec.get("complete")))


def is_done(rec):
    """完了と認めるのは、**今の版の検査を通った**完了だけ（v1 の「完了」は、失敗した一覧を0件と読んだ日が混ざり得る）"""
    return bool(rec and rec.get("complete")) and _rank(rec)[0] >= LIST_V


def done_months():
    """取り終えた月の記録。**月ごとのファイル** `_done/YYYY-MM.json` が正（2026-10-22）。旧 `_done.json` も読んで重ねる。
    ⚠ 1つのファイルを2本の実行が書くと、push の取り込みで食い違い、先に取った31か月分の記録が消えた（データは無事だった）。
    同じ月に記録が2つあれば、版の新しい方を採り、版が同じなら月ごとのファイルを採る"""
    import json
    out = {}
    try:
        old = json.loads(_done_path().read_text(encoding="utf-8"))
        out.update({k: v for k, v in old.items() if isinstance(v, dict)} if isinstance(old, dict) else {})
    except Exception:
        pass
    d = _done_dir()
    if d.exists():
        for fp in sorted(d.glob("*.json")):
            try:
                v = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(v, dict):
                continue
            ym = fp.stem
            # 月ごとのファイルは旧 _done.json より新しい。版が同じか新しければ、完了かどうかに関わらず月ごとのファイルを採る
            #（「完了」を優先すると、旧ファイルの完了が新しい失敗を隠す・レビュー10回目）
            if ym not in out or _rank(v)[0] >= _rank(out[ym])[0]:
                out[ym] = v
    return out


def fetch_month(ym, log=print, sleep=0.7, deadline=None, today=None):
    """その月に提出された有報を全部取り、`data/edinet/asr_YYYY-MM.csv.gz` に1通1行で書く。
    **途中で止まっても続きから** ─ 既にファイルにある docID は取り直さない。取れなかった書類が残る月は完了にしない。
    `deadline`（エポック秒）を過ぎたら、そこまでの分を書いて未完了で返す（ジョブの時間切れで月の途中の分を失わない）。
    今日より前の日しか一覧を引かない（今日以降の日が入る月は未完了＝まだ終わっていない月を完了にしない）"""
    import datetime as _dt
    import json
    import time
    import pandas as pd
    import paths as P
    P.EDINET_DIR.mkdir(parents=True, exist_ok=True)
    fp = P.EDINET_DIR / "asr_{}.csv.gz".format(ym)
    have = pd.read_csv(fp, dtype=str) if fp.exists() else pd.DataFrame(columns=COLS)
    got = set(have["docID"]) if len(have) else set()
    # 状態が変わった日に、**次の月の一覧**にも同じ書類が載る（取得済み48か月で2件）─ 他の月のファイルにある書類も取り直さない
    for f in P.EDINET_DIR.glob("asr_*.csv.gz"):
        if f != fp:
            try:
                got |= set(pd.read_csv(f, usecols=["docID"], dtype=str)["docID"].dropna())
            except Exception:
                pass
    today = today or _dt.date.today()
    out, failed, seen, no_csv, n_list_fail, n_future, stopped, rejected = [], set(), set(), set(), 0, 0, False, None
    for d in _month_days(ym):
        if d >= today:
            n_future += 1
            continue
        if deadline and time.time() > deadline:
            stopped = True
            break
        try:
            docs = asr_list(d, no_csv)
        except Rejected as e:
            rejected = _mask(e)                 # キーの拒否はその日だけの失敗ではない ─ そこまでを書いて取得を止める
            break
        except Exception as e:
            n_list_fail += 1
            log("fetch: {} の一覧を取れない ({})".format(d, _mask(e)))
            continue
        finally:
            time.sleep(sleep / 2)
        for x in docs:
            seen.add(x["docID"])
            if x["docID"] in got:
                continue
            if deadline and time.time() > deadline:
                stopped = True
                break
            try:
                rows = parse_csv_zip(fetch_csv_zip(x["docID"]))
                if not rows:
                    # 読める CSV が1行も無い ZIP を「取れた」にすると、数値が空の行が残り、二度と取り直さない（レビュー10回目）
                    raise ValueError("ZIP に読める CSV の行が無い")
                out.append(extract(rows, x))
                got.add(x["docID"])      # 一覧は状態が変わった日にも同じ書類を載せる ─ 同じ実行の中で2回取らない
                failed.discard(x["docID"])   # 前の日に取れず、後の日の一覧で取れた書類は「取れない」に残さない
            except Exception as e:
                failed.add(x["docID"])
                log("fetch: {} を取れない ({})".format(x.get("docID"), _mask(e)))
            time.sleep(sleep)
        if stopped:
            break
    df = pd.concat([have, pd.DataFrame(out, columns=COLS)], ignore_index=True) if out else have
    df = df.drop_duplicates("docID", keep="first")
    df.to_csv(fp, index=False, compression="gzip")
    complete = not failed and n_list_fail == 0 and n_future == 0 and not stopped and not rejected
    _done_dir().mkdir(parents=True, exist_ok=True)
    (_done_dir() / "{}.json".format(ym)).write_text(json.dumps(
        {"v": LIST_V, "docs": len(seen), "rows": int(len(df)), "failed": sorted(failed), "list_fail": n_list_fail,
         "future_days": n_future, "stopped": stopped, "rejected": bool(rejected), "no_csv": sorted(no_csv),
         "complete": complete},
        ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    why = "完了" if complete else "🔴 キーを拒否された（{}）".format(rejected) if rejected else "⚠ 未完了（{}もう一度回す）".format(
        "時間切れの前に止めた・" if stopped else "今日以降の日を含む・" if n_future else "")
    log("fetch: {} 有報 {}件 → 行 {}（新しく {}・取れない {}・一覧の失敗 {}日・CSV の無い有報 {}件）{}".format(
        ym, len(seen), len(df), len(out), len(failed), n_list_fail, len(no_csv), why))
    return 0 if complete else 4 if rejected else 3 if stopped else 2


def failing_months(lo, hi, only=None):
    """範囲の中で、**今の版で回して取れなかった**月（一覧の失敗・取れない書類・キーの拒否）。
    時間切れで止めた月・まだ回していない月（v1・記録なし）は含めない ─ それは起動し直せば続きから取る。
    `only` を渡すと、その月（この実行で回した月）だけを見る ─ 届かなかった月の古い失敗で赤にしない（レビュー9回目）"""
    d = done_months()
    keep = set(only) if only is not None else None
    return sorted(ym for ym, v in d.items() if lo <= ym <= hi and (keep is None or ym in keep)
                  and _rank(v)[0] >= LIST_V and not v.get("complete")
                  and (v.get("list_fail") or v.get("failed") or v.get("rejected")))


EXTRACT_V = 4   # 4: IFRS の会社の米国基準の売上は会社独自の要素の後ろ（2026-10-22）／3: 米国基準→IFRS の移行の年の売上・IFRS の総資産を IFRS から（2026-10-22）／2: 会社独自の要約の要素・米国基準の要素
_META = {"docID": "docID", "secCode": "code4", "edinetCode": "edinetCode", "filerName": "filerName",
         "submitDateTime": "submit", "periodStart": "periodStart", "periodEnd": "periodEnd"}
_SAME = ("sales_elem", "sales_mixed")     # 埋まった年が増えれば変わって当然の印（値の食い違いには数えない）


def _same(a, b):
    """CSV から読んだ文字列 a と、抽出し直した値 b が同じか（数は数として比べる）"""
    na = a is None or (isinstance(a, float) and a != a) or str(a) == ""
    nb = b is None or (isinstance(b, float) and b != b) or str(b) == ""
    if na or nb:
        return na and nb
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return str(a) == str(b)


def refetch_docs(ids, log=print, sleep=0.7, deadline=None):
    """C-140 の穴（2026-10-22）: 指定した書類を取り直し、**抽出し直した値**で月のファイルの行を差し替える。
    書類の属性（提出日・期間など）は元の行から引き継ぐ（一覧を引き直さない）。取れなかった書類は元の行を残す。
    ⚠ 標準の要素を先に読むので、**前に埋まっていた値は変わらないはず** ─ 変わったら `changed` に残してログに出す。
    途中で止まっても続きから（`_refetch.json` の `done` は同じ版なら飛ばす）"""
    import json
    import time
    import pandas as pd
    import paths as P
    prog_p = P.EDINET_DIR / "_refetch.json"
    prog = json.loads(prog_p.read_text(encoding="utf-8")) if prog_p.exists() else {}
    if prog.get("v") != EXTRACT_V:
        prog = {"v": EXTRACT_V, "done": [], "failed": [], "filled": {}, "changed": []}
    done = set(prog["done"])
    files, where = {}, {}
    for f in sorted(P.EDINET_DIR.glob("asr_*.csv.gz")):
        df = pd.read_csv(f, dtype=object, keep_default_na=False)     # str 型の列には数を入れられない（pandas 3）
        files[f] = df
        for i, d in enumerate(df["docID"]):
            where.setdefault(d, (f, i))
    todo = [d for d in dict.fromkeys(ids) if d in where and d not in done]
    miss = [d for d in ids if d not in where]
    if miss:
        log("refetch: ファイルに無い書類 {}件（飛ばす）: {}".format(len(miss), miss[:5]))
    log("refetch: 対象 {}件（済み {}件を除く）".format(len(todo), len(done & set(ids))))
    touched, stopped, failed = set(), False, set(prog["failed"])

    def flush():
        for f in touched:
            files[f].to_csv(f, index=False, compression="gzip")
        prog["done"], prog["failed"] = sorted(done), sorted(failed - done)
        prog_p.write_text(json.dumps(prog, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        touched.clear()

    for n, d in enumerate(todo, 1):
        if deadline and time.time() > deadline:
            stopped = True
            break
        f, i = where[d]
        old = files[f].iloc[i].to_dict()
        try:
            rows = parse_csv_zip(fetch_csv_zip(d))
            if not rows:
                raise ValueError("ZIP に読める CSV の行が無い")
            new = extract(rows, {k: old.get(c, "") for k, c in _META.items()})
        except Exception as e:
            failed.add(d)
            log("refetch: {} を取れない ({})".format(d, _mask(e)))
            time.sleep(sleep)
            continue
        for c in COLS:
            if _same(old.get(c), new.get(c)):
                continue
            if old.get(c, "") == "":
                prog["filled"][c] = prog["filled"].get(c, 0) + 1
            elif c not in _SAME:
                prog["changed"].append({"docID": d, "col": c, "old": old.get(c), "new": new.get(c)})
                log("refetch: ⚠ {} {} の値が変わった {} → {}".format(d, c, old.get(c), new.get(c)))
        files[f].iloc[i] = ["" if new.get(c) is None else new.get(c) for c in files[f].columns]
        touched.add(f)
        done.add(d)
        failed.discard(d)
        if n % 200 == 0:
            flush()
            log("refetch: {} / {}".format(n, len(todo)))
        time.sleep(sleep)
    flush()
    left = [d for d in todo if d not in done]
    log("refetch: 取り直し {}件・取れない {}件・残り {}件／空欄が埋まった {}／前の値が変わった {}件".format(
        len(done & set(ids)), len(failed), len(left), prog["filled"], len(prog["changed"])))
    return 3 if stopped else 2 if failed else 0


if __name__ == "__main__":
    if "--refetch-docs" in sys.argv:
        src = sys.argv[sys.argv.index("--refetch-docs") + 1]
        ids = [x.strip() for x in open(src, encoding="utf-8") if re.fullmatch(r"S[0-9A-Z]{7}", x.strip())]
        dl = os.environ.get("EDINET_DEADLINE")
        sys.exit(refetch_docs(ids, deadline=float(dl) if dl else None))
    if "--probe-docs" in sys.argv:
        ids = [x for x in sys.argv[sys.argv.index("--probe-docs") + 1].split(",") if re.fullmatch(r"S[0-9A-Z]{7}", x)]
        sys.exit(probe_docs(ids))
    if "--probe" in sys.argv:
        sys.exit(probe())
    if "--survey" in sys.argv:
        sys.exit(survey())
    if "--fetch-month" in sys.argv:
        ym = sys.argv[sys.argv.index("--fetch-month") + 1]
        if not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", ym):
            sys.exit("月は YYYY-MM")
        if is_done(done_months().get(ym)):
            print("fetch: {} は完了済み".format(ym))
            sys.exit(0)
        dl = os.environ.get("EDINET_DEADLINE")
        sys.exit(fetch_month(ym, deadline=float(dl) if dl else None))
    print("使い方: python edinet_fin.py --probe | --survey | --fetch-month YYYY-MM | --refetch-docs 一覧.txt | --probe-docs ID,ID")
