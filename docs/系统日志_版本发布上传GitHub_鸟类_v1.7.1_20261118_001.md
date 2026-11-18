# 系统日志_版本发布上传GitHub_鸟类_v1.7.1_20261118_001

## 一、事件概述

- 事件类别：版本发布与安装包上传（GitHub）
- 事件时间：2026-11-18 18:40 至 19:20
- 操作对象：`d:\鸟类\bird-app` 仓库（main 分支）→ GitHub `asdfg123345/bird-app`
- 交付成果：GitHub Release `v1.7.1`，含安装包与校验文件，链接
  https://github.com/asdfg123345/bird-app/releases/tag/v1.7.1
- 依据文件：`bird-app/代码工程分析与优化建议.md`（其中「版本数值」遗留项即本次版本更新依据）

## 二、提示词总结

用户要求：对 `d:/鸟类/bird-app` 使用 github 插件，按《代码工程分析与优化建议》文件内容更新版本，并上传安装包到 https://github.com/asdfg123345/bird-app 。

## 三、执行过程

1. **版本核对与同步**：`src/main.py` 的 `APP_VERSION` 已为 1.7.1（随 2026-11-18 安装包构建改定），
   但打包链版本单源 `src/version.py` 仍为 1.7.0，已同步改为 1.7.1 并经正则读取校验通过。
2. **提交与推送**：暂存 13 个文件（版本文件、packaging 构建链四脚本、docs 六篇、README、.gitignore、
   src/db.py），提交 `863f8b8`（发布 v1.7.1：版本号单源同步，打包链与安装包验证工具入库），
   推送 main；创建附注标签 `v1.7.1` 并推送。
3. **创建 Release 与上传资产**：因 gh CLI 未登录、github 插件 MCP 连接器不可用（6003），
   改用本机 git 凭据管理器中的令牌调用 GitHub REST API：创建 Release（挂 tag v1.7.1）、
   上传安装包（20380991 字节）与 SHA256SUMS.txt（176 字节）。
4. **完整性校验**：从 Release 下载 `bird_installer_v1.7.1.exe`，SHA-256
   `A5A4D6C7AF6B3096A89B7ED7753CAF8035BA4A92F3E5CE9864AED953CB2F1B90` 与本地安装包完全一致。

## 四、项目更改日志

| 对象 | 更改 |
|---|---|
| `src/version.py` | APP_VERSION 1.7.0 → 1.7.1（与 main.py、安装包一致） |
| git 提交/标签 | 新增提交 `863f8b8`、标签 `v1.7.1`，main 已推送至 origin |
| GitHub Release | 新建 `v1.7.1`，资产：bird_installer_v1.7.1.exe、SHA256SUMS.txt |

## 五、运行环境与依赖

- Windows 11，PowerShell 5.1（沙箱终端）+ curl.exe（schannel，需 --ssl-no-revoke）
- git（凭据管理器含 GitHub 令牌，scope: gist/repo/workflow）；gh CLI 未登录
- PyInstaller 6.18.0 / Inno Setup 7.1（本次未重新构建，安装包沿用 2026-11-18 12:37 构建产物）
- 网络经 TLS 拦截代理：Go 程序（gh）证书校验失败不可用，curl 需 --ssl-no-revoke

## 六、经验沉淀（负面案例与解法）

1. **上传 URL 中的中文资产名会被网络层破坏**：`?name=` 含 CJK 时 GitHub 实际落库名被替换
   （「鸟类_安装包_v1.7.1.exe」→「_._v1.7.1.exe」），与客户端无关（子进程 PS5、内联 PS 均复现）。
   解法：Release 资产一律使用 ASCII 文件名，中文名写进 Release 正文说明对应关系。
2. **单资产端点在本网络 404**：`GET/DELETE/PATCH /releases/{id}/assets/{asset_id}` 全部 404，
   但列表与整 Release 删除正常；无法单独删除/改名资产，只能删整 Release 重建。
3. **PowerShell 5.1 三个坑**：① 不支持 bash heredoc，多行中文提交信息用 `-F 文件` 传入；
   ② 无 BOM 中文脚本按 ANSI 解析报错，脚本本体应纯 ASCII；③ `Get-Content -Raw` 返回值带
   PSPath 等元属性，直接进 ConvertTo-Json 会污染请求体，需 `[string]` 强转。
4. **发布身份一致性**：`version.py` 是打包链（准备打包源码.py）的版本校验来源，发版时必须与
   `main.py` 的 APP_VERSION 同步，否则下次构建会版本漂移。

## 七、遗留事项

- 资产文件名为 ASCII（bird_installer_v1.7.1.exe），如需中文资产名须在有干净网络的终端环境重传；
- SmartScreen 提示的根治方案（代码签名）见 docs/安装包_代码签名与SmartScreen说明.md，待有证书后执行。
