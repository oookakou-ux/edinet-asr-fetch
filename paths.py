# -*- coding: utf-8 -*-
"""保存先（このリポジトリは EDINET の有価証券報告書から数値を取るだけ）"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
EDINET_DIR = DATA_DIR / "edinet"
