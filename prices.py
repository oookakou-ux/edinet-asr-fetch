# -*- coding: utf-8 -*-
"""
東証の銘柄の日次株価（終値・調整後終値・株式分割）を Yahoo Finance から取り、1つのファイルにまとめる。

- 銘柄 = data/edinet の証券コード ∪ JPX の上場銘柄一覧 ∪ 1306（TOPIX 連動 ETF）
- 終値は yfinance の auto_adjust=False（分割だけ調整）、調整後終値は分割と配当を調整したもの
- 出力 prices.npz（pickle を使わない）:
    dates (datetime64[D]) / codes (U4) / close, adj (float32, 日付×銘柄・欠けは NaN) /
    split_date (datetime64[D]) / split_code (U4) / split_ratio (float64)
- ファイルはワークフローが暗号化してから置く（価格そのものは公開しない）
"""
import glob
import io
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

import code_utils as CU

START = os.environ.get("PX_START") or "2019-03-01"
END = os.environ.get("PX_END") or None          # 遡り（2026-10-23）: 2019-03 より前だけを取るときに終わりを切る
BENCH = "1306"
CHUNK, RETRY = 150, 3
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
UA = {"User-Agent": "Mozilla/5.0 (research; contact via GitHub)"}


NOTES = []      # JPX の取得の経過（最後に prices/run.json に残す ─ ログの先頭は長くて読み返しにくい）


def log(m):
    print(m, flush=True)


def edinet_codes():
    out = set()
    for f in sorted(glob.glob("data/edinet/asr_*.csv.gz")):
        df = pd.read_csv(f, usecols=["code4"], dtype=str)
        out |= {c for c in (CU.norm_code(x) for x in df["code4"].dropna()) if c}
    return out


def _jpx_get(url):
    r = requests.get(url, headers=UA, timeout=60)
    msg = "JPX {} → HTTP {} {}bytes".format(url.rsplit("/", 1)[-1], r.status_code, len(r.content))
    log(msg)
    NOTES.append(msg)
    r.raise_for_status()
    return r


def jpx_codes():
    """JPX の上場銘柄一覧（取れなければ空 ─ 取れなかったことはログに出す）。
    User-Agent が無いと拒否されることがある。ファイルの場所が変わっていれば一覧のページから探す"""
    import re
    for attempt in ("direct", "page"):
        try:
            url = JPX_URL
            if attempt == "page":
                page = _jpx_get(JPX_PAGE).text
                m = re.search(r'href="([^"]*data_j\.xls[x]?)"', page)
                if not m:
                    log("⚠ JPX の一覧のページに data_j.xls のリンクが無い")
                    return set()
                url = m.group(1) if m.group(1).startswith("http") else "https://www.jpx.co.jp" + m.group(1)
            df = pd.read_excel(io.BytesIO(_jpx_get(url).content), dtype=str)
            col = next(c for c in df.columns if "コード" in str(c))
            kind = next((c for c in df.columns if "市場" in str(c) and "区分" in str(c)), None)
            if kind is not None and os.environ.get("JPX_STOCKS_ONLY") == "1":
                n0 = len(df)
                df = df[df[kind].astype(str).str.contains("株式")]     # ETF・REIT・出資証券などを外す（母集団は株式だけ）
                msg = "JPX: 株式だけ {:,}/{:,}（区分の列 {}）".format(len(df), n0, kind)
                log(msg)
                NOTES.append(msg)
            out = {c for c in (CU.norm_code(x) for x in df[col].dropna()) if c}
            log("JPX の上場銘柄一覧 {:,}銘柄（{}）".format(len(out), attempt))
            return out
        except Exception as e:
            msg = "⚠ JPX の上場銘柄一覧を取れない（{}: {}・{}）".format(attempt, type(e).__name__, str(e)[:160])
            log(msg)
            NOTES.append(msg)
    log("⚠ EDINET の証券コードだけで取る")
    return set()


def _norm(s):
    s = s.dropna()
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    return pd.Series(s.values, index=pd.DatetimeIndex(idx).normalize())


def _pass(codes, chunk):
    import yfinance as yf
    C, A, S, D = {}, {}, {}, {}
    items = sorted(codes)
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        df = None
        for att in range(RETRY):
            try:
                df = yf.download([c + ".T" for c in part], start=START, end=END, progress=False, auto_adjust=False,
                                 actions=True, group_by="ticker", threads=True)
                if df is not None and not df.empty:
                    break
            except Exception as e:
                log("価格 {}〜 取得失敗({}): {}".format(part[0], att + 1, type(e).__name__))
            time.sleep(3.0 * (att + 1))
        if df is None or df.empty:
            continue
        for c in part:
            try:
                sub = df[c + ".T"] if isinstance(df.columns, pd.MultiIndex) else df
                cl, ad = _norm(sub["Close"]), _norm(sub["Adj Close"])
                sp = _norm(sub["Stock Splits"]) if "Stock Splits" in sub else pd.Series(dtype=float)
                dv = _norm(sub["Dividends"]) if "Dividends" in sub else pd.Series(dtype=float)
            except Exception:
                continue
            if len(cl):
                C[c], A[c], S[c], D[c] = cl, ad, sp[sp > 0], dv[dv > 0]
        time.sleep(1.0)
    return C, A, S, D


def fetch(codes):
    C, A, S, D = _pass(codes, CHUNK)
    for k, wait in ((50, 10.0), (20, 30.0)):
        miss = sorted(set(codes) - set(C))
        if not miss:
            break
        log("取れなかった {}銘柄をもう一度（束 {}）".format(len(miss), k))
        time.sleep(wait)
        C2, A2, S2, D2 = _pass(miss, k)
        C.update(C2), A.update(A2), S.update(S2), D.update(D2)
    return C, A, S, D


def save(C, A, S, path, D=None):
    codes = sorted(C)
    dates = pd.DatetimeIndex(sorted(set().union(*[set(C[c].index) | set(A[c].index) for c in codes]))) \
        if codes else pd.DatetimeIndex([])
    close = pd.DataFrame({c: C[c] for c in codes}).reindex(index=dates, columns=codes)
    adj = pd.DataFrame({c: A[c] for c in codes}).reindex(index=dates, columns=codes)
    sd, sc, sr = [], [], []
    for c in codes:
        for d, v in S.get(c, pd.Series(dtype=float)).items():
            sd.append(d), sc.append(c), sr.append(float(v))
    # 分配金・配当（1株あたり・権利落ち日）─ 調整後終値が壊れた銘柄で配当込みを組み直すため（2026-10-22: 1321 の調整後終値が
    #   1:100 の分割の後に負になっていた）。⚠ 分割の前の額か後の額かは Yahoo のまま（読む側で確かめる）
    dd, dc, da = [], [], []
    for c in codes:
        for d, v in (D or {}).get(c, pd.Series(dtype=float)).items():
            dd.append(d), dc.append(c), da.append(float(v))
    np.savez(path, dates=dates.values.astype("datetime64[D]"), codes=np.array(codes, dtype="U4"),
             close=close.to_numpy(dtype=np.float32), adj=adj.to_numpy(dtype=np.float32),
             split_date=np.array(sd, dtype="datetime64[D]"), split_code=np.array(sc, dtype="U4"),
             split_ratio=np.array(sr, dtype=np.float64),
             div_date=np.array(dd, dtype="datetime64[D]"), div_code=np.array(dc, dtype="U4"),
             div_amount=np.array(da, dtype=np.float64))
    return len(codes), len(dates), len(sd)


def main():
    extra = [x.strip().upper() for x in os.environ.get("EXTRA", "").split(",") if x.strip()]
    if extra:
        # コードを指定して取る（ETF など・2026-10-22 C-144 の 1321/1545/1570）。英数字4文字だけ通す
        ed, jp, mode = set(), set(), "extra"
        codes = sorted({c for c in (CU.norm_code(x) for x in extra) if c} | {BENCH})
        log("銘柄（指定）: {}".format(codes))
    elif os.environ.get("CODES") == "have":
        ed, jp, mode = set(), set(), "have"
    else:
        ed, jp = edinet_codes(), jpx_codes()
        mode = os.environ.get("CODES", "all")
    if mode == "extra":
        pass
    elif mode == "have":
        # 遡り（2026-10-23）: 前に取れた銘柄（prices/have.txt）だけを、PX_START〜PX_END の期間で取り直す
        have = {l.strip() for l in open("prices/have.txt", encoding="utf-8") if l.strip()}
        codes = sorted(have | {BENCH})
    elif mode == "missing":
        # 前に取れた銘柄（prices/have.txt・ティッカーのコードだけ）を除いた残りだけ取る（Yahoo の回数制限を避ける・2026-10-22）
        have = {l.strip() for l in open("prices/have.txt", encoding="utf-8") if l.strip()}
        codes = sorted(((ed | jp) - have) | {BENCH})
        log("前に取れた {:,}銘柄を除く".format(len(have)))
    elif mode == "jpx_new":
        # 足りない分だけ取る: JPX の一覧にあって EDINET に無い銘柄（新規上場など）＋ 1306（同じ日の突き合わせ用）。
        # 全銘柄を1時間に3回取り直すと Yahoo の回数制限で半分落ちた（2026-10-22）
        if not jp:
            log("🔴 JPX の一覧が取れない ─ 足りない分を決められない")
            return 1
        codes = sorted((jp - ed) | {BENCH})
    else:
        codes = sorted(ed | jp | {BENCH})
    log("銘柄（{}）: EDINET {:,} ∪ JPX {:,} → {:,}".format(mode, len(ed), len(jp), len(codes)))
    C, A, S, D = fetch(codes)
    miss = sorted(set(codes) - set(C))
    log("取れた {:,} / {:,}（取れない {:,}・先頭の文字別 {}）".format(
        len(C), len(codes), len(miss), dict(pd.Series([m[0] for m in miss]).value_counts()) if miss else {}))
    if BENCH not in C:
        log("🔴 1306 が取れない")
        return 1
    n, d, s = save(C, A, S, "prices.npz", D)
    log("書いた: 銘柄 {:,} × 日付 {:,}・分割 {:,}件（{}〜{}）".format(n, d, s, C[BENCH].index.min().date(),
                                                         C[BENCH].index.max().date()))
    import json
    with open("prices/run.json", "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "start": START, "end": END, "missing": miss,
                   "edinet_codes": len(ed), "jpx_codes": len(jp), "requested": len(codes), "got": len(C),
                   "dates": d, "splits": s, "jpx_notes": NOTES}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
