三棒产物依次为 brief（`brief.json`）、diagnosis（`diagnosis.json`）和可预览 site（`site/`）；由于预构建浏览器包 `vendor/app.bundle.js` 为 7.3 MB，`site/` 仅保留构建报告，需先在 `openbot/examples/org-health-local` 下执行 `python3 builder/build_site.py --data demo/diagnosis.json --out demo/site --bundle demo/vendor/app.bundle.js` 重建完整站点，再运行 `python3 serve.py --root demo/site` 并在浏览器打开 http://127.0.0.1:8765/ 预览。

站点内容摘要见 `site-summary.md`：不打开浏览器即可核对页面会展示的指标（标题、各期次数值、单位）、口径说明条目与洞察/建议条数；重建站点后用 `python3 demo/make_site_summary.py` 重新生成（只用标准库，不改动 site 产物）。
