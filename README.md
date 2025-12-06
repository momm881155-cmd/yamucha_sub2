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



evideo Auto Poster Bot

このプロジェクトは、定期的に新しいURLを収集して X(Twitter) に自動投稿するボットです。
投稿履歴は state.json と Google スプレッドシートに保存され、同じURLが二度投稿されないようになっています。

🚀 なにをしているボットか（ざっくり）
スプレッドシート ＋ orevideo から新しい URL を集める
　↓
まだ投稿していない URL だけ選ぶ
　↓
X (Twitter) に自動投稿
　↓
投稿した URL を記録して次回は使わない


これを GitHub Actions が自動で定期実行します。

🔍 集めているURLの種類
種類	例	用途
gofileリンク	https://gofile.io/d/XXXXX
	投稿のメイン
twimg動画(mp4)	https://video.twimg.com/
...	gofile不足時の補充
📍 どこからURLを取得している？
取得元	内容	優先度
Google スプレッドシート (B列)	最新の gofile URL を下から順に	⭐ 最優先
orevideo サイト	https://orevideo.pythonanywhere.com/
	◇ 2番手
twimg	それでも不足した場合のみ	△ 予備

シートの gofile → orevideo の gofile → twimg の順に選びます。

🧠 URLの重複対策

投稿に使った URL は次の場所に記録され、次回以降は使用されません：

state.json

スプレッドシート E列 （「post成功」）

また、リンク切れ の gofile はシートの D列に自動でマークされます。

⚙️ 実行の仕組み

GitHub Actions (.github/workflows/hourly_orevideo.yml) が
1時間おきに自動起動 → bot_orevideo.py を実行します。

GitHub Actions 起動
　→ bot_orevideo.py 実行
　→ goxplorer2.py でURL収集
　→ Xに投稿
　→ state.json更新 & シート更新

📦 主なファイル
ファイル	役割
.github/workflows/hourly_orevideo.yml	自動実行の設定
bot_orevideo.py	投稿処理の本体
goxplorer2.py	URL収集とフィルタリング
state.json	投稿履歴の記憶
requirements.txt	必要なライブラリ一覧

必要に応じて、
環境変数（X API / Google Sheets）を設定すれば動作します。
