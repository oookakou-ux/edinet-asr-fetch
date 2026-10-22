# edinet-asr-fetch

金融庁 EDINET API（v2）から、有価証券報告書（XBRL を CSV にしたもの）の数値をいくつか取り出し、
提出月ごとに `data/edinet/asr_YYYY-MM.csv.gz` に1通1行で保存する。

- 取得は `.github/workflows/edinet-fetch.yml`（手動起動・月ごとにコミット・止まっても続きから）
- 取り出す項目: 売上（5期）・営業利益・EPS・1株配当・自己資本比率・総資産・発行済株式数・税引前利益・法人税等

出典: 金融庁 EDINET（https://disclosure2.edinet-fsa.go.jp/）の有価証券報告書をもとに作成
