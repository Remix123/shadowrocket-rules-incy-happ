# Shadowrocket → INCY 规则转换与 GitHub Pages 发布

这个项目定时读取 [Johnshall/Shadowrocket-ADBlock-Rules-Forever](https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever) `release` 分支根目录下的全部 `.conf` 文件，将可映射的路由规则分别输出为 INCY 与 Happ 路由 JSON，并发布到 GitHub Pages。

上游每天北京时间 08:00 构建，本 workflow 每天北京时间 10:30 运行；也可以从 GitHub Actions 页面手动触发。主页 `index.html` 和 `manifest.json` 会列出 JSON 地址、来源和转换情况。INCY 文件位于站点根目录，Happ 文件位于 `happ/` 子目录。

## 发布到 GitHub Pages

1. 将 `incy-shadowrocket-converter` 目录内容（包括隐藏的 `.github` 目录）放入一个 GitHub 仓库根目录。
2. 在仓库 **Settings → Pages → Build and deployment** 中选择 **GitHub Actions**。
3. 确认 Actions 有权限写入 Pages 并创建 OIDC token，然后运行 `Convert and publish INCY routing profiles`。
4. 完成首次部署后，主页和规则 JSON 将位于 `https://<owner>.github.io/<repo>/`。例如 Happ 文件是 `https://<owner>.github.io/<repo>/happ/sr_cnip.json`。若仓库名为 `<owner>.github.io`，则没有 `<repo>/` 路径。

本工作区目前没有绑定 GitHub 仓库，因此 workflow 尚未实际部署；部署 URL 会按运行时的 `GITHUB_REPOSITORY` 自动生成。

## 本地运行

```sh
python3 scripts/convert.py --output site
python3 -m unittest discover -s tests -v
```

脚本使用 Python 标准库，不依赖第三方包。转换器从 GitHub API 动态发现上游根目录 `.conf` 文件，并展开允许列表中的 `raw.githubusercontent.com` RULE-SET。外部非 GitHub raw 规则集会记入报告，不会从任意地址下载。

## 映射范围与限制

- 可转换：`DOMAIN-SUFFIX`、`DOMAIN`、`IP-CIDR`、`IP-CIDR6`、`GEOIP`、`FINAL`、`MATCH`、可下载的 GitHub raw `RULE-SET`，以及 `[General]` 中的 `skip-proxy` / `bypass-tun`。
- `DIRECT` 映射到直连，`PROXY` 和自定义策略组映射到代理，`REJECT*` 映射到阻断。策略组会合并到 INCY 的单一代理策略。
- INCY 的路由 JSON 采用直连、代理、阻断数组，不能保留 Shadowrocket 的逐条 first-match 顺序；`DOMAIN` 精确匹配与 `DOMAIN-SUFFIX` 后缀匹配也无法在当前数组结构中区分。
- `DOMAIN-KEYWORD`、正则、端口、协议、进程等条件会列在各规则 `.report.json` 的 `unsupported` 中。`[URL Rewrite]`、`[MITM]`、`[Host]` 等非路由功能不会被伪装成已转换。
- 每个 JSON 旁边都有 `.report.json`。网站首页会标出未映射条目数；发布文件前应查看报告。
- Happ 输出为路由 profile JSON（`happ/<规则名>.json`），不是供 Xray core 直接运行的完整服务器配置。Happ 官方路由导入格式是 `happ://routing/onadd/{base64}`；同目录的 `.link.txt` 文件包含可复制到 Happ 的导入链接。
- INCY 和 Happ 使用各自的 JSON 文件名与清单入口，方便选择客户端；当前公开文档中的路由 profile 字段兼容。Happ 的导入方式与纯 JSON 文件链接不同，因此页面同时提供 Happ JSON URL 和导入链接。

上游规则仓库当前标注为 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)。生成的规则数据是其转换版本，主页与 `manifest.json` 会保留来源和许可证链接；上游引用的其他规则仍可能带有各自的许可要求。
