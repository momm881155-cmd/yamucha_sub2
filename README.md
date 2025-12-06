# yamucha4954 ➜※ 一時的に使用中 メインAPI切れの為

.github/workflows/…（hourly_orevideo.yml）

GitHub Actions の定期実行ワークフロー本体。

これがないと自動ポストされないので必須。

✅ bot_orevideo.py

Xにポストする メインのボットスクリプト。

workflow から直接叩かれてるので必須。

✅ goxplorer2.py

bot_orevideo.py の中で from goxplorer2 import ... されてる スクレイパ＆選抜ロジック。

これがないと URL 集めが動かないので必須。

✅ requirements.txt

Actions で pip install -r requirements.txt してるので必要。

ライブラリ追加/削除するときもここをいじる。

✅ README.md

動作には関係ないけど、用途やセットアップを書く場所。


✅ .gitignore

__pycache__ や .venv、ログとかをリポジトリに入れないための設定。
