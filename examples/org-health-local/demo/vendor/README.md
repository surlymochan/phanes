# 预构建浏览器包（demo 专用）

`app.bundle.js` 是 `app.js` 的浏览器包，供没有 `openbot/node_modules` 的 fleet lane 在 Builder 棒使用：

```bash
python3 builder/build_site.py --data demo/diagnosis.json --out demo/site --bundle demo/vendor/app.bundle.js
```

- 构建源：main `c4e897ea`，命令 `cd openbot/examples/org-health-local && bun build app.js --target=browser --minify --outfile=demo/vendor/app.bundle.js`（需先在 `openbot/` 下 `bun install`）。
- 体积约 7.5 MB，主要是 `@openbot/i18n` 的全部语言文案；`app.js` 或 i18n 文案改动后需按上面命令重建。
- 仅用于合成数据演示，不包含任何数据。
