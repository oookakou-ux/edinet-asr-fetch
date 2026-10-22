# -*- coding: utf-8 -*-
"""
証券コードの正規化（C-54）

東証のコードは**4文字の英数字**で、数字だけではない。
2024年以降の新規上場では英字入りコードが割り当てられている（例: 285A キオクシア、130A、160A）。
当リポジトリは当初 `code[:4].isdigit()` や `\\d{4}` で受理していたため、
**英字入りコードを全経路で静かに取り落としていた**。

ここに正規化を一本化する。固定の判定を各所に散らすと、また同じ取りこぼしが起きる
（知見3-1「正は参照集合で定義する」と同型）。

対応する入力表記:
  '6258'      → '6258'
  '285A'      → '285A'   ← 従来落ちていた
  '62580'     → '6258'   （JPXの5桁表記=4桁+チェック0）
  '285A0'     → '285A'
  '6258.0'    → '6258'   （Excelで数値化された値）
  '6258.T'    → '6258'   （yfinanceのティッカー）
  'コード' / '銘柄名' / '' → None（ヘッダー行を弾く）
"""
import re

# 先頭は必ず数字、残り3文字は数字または英大文字
_CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")


def norm_code(raw):
    """証券コードを4文字に正規化する。受理できなければ None を返す。"""
    s = str(raw if raw is not None else "").strip().upper()
    if not s:
        return None
    s = s.replace(" ", "").replace("　", "")
    if s.endswith(".0"):                 # Excel由来の数値化(6258.0)
        s = s[:-2]
    if "." in s:                         # ティッカー表記(6258.T)
        s = s.split(".", 1)[0]
    # JPXの5桁表記は「4桁コード+チェック0」。先頭4文字が妥当なコードなら切る
    if len(s) == 5 and s.endswith("0") and _CODE_RE.match(s[:4]):
        s = s[:4]
    if len(s) > 4:
        s = s[:4]
    return s if _CODE_RE.match(s) else None


def is_code(raw):
    return norm_code(raw) is not None


def has_alpha(code):
    """英字入りコードか（取りこぼし監視の集計用）"""
    c = norm_code(code)
    return bool(c) and any(ch.isalpha() for ch in c)


def reconcile(label, n_in, accepted, rejected_samples=None, log=print):
    """**入力数と採用数を必ず突合して出す**（知見3-6）。

    パースに失敗した入力はエラーを出さずに消えるため、欠損の中で最も見つけにくい。
    英字コードの取りこぼしはまさにこれで、件数を出していれば早期に気づけた。"""
    n_ok = len(accepted) if hasattr(accepted, "__len__") else int(accepted)
    n_alpha = sum(1 for c in accepted if has_alpha(c)) if hasattr(accepted, "__iter__") else 0
    msg = "{}: 入力{:,} → 採用{:,} (除外{:,} / 英字コード{:,})".format(
        label, n_in, n_ok, max(0, n_in - n_ok), n_alpha)
    if rejected_samples:
        msg += " 除外例: {}".format(list(rejected_samples)[:5])
    log(msg)
    return n_ok, n_alpha
