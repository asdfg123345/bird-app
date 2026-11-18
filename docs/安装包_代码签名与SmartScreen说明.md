# 安装包代码签名与 SmartScreen 提示说明

## 一、现象

未签名的 `鸟类_安装包_v1.7.1.exe` 首次在 Windows 10/11 上运行时，Microsoft Defender
SmartScreen 可能弹出蓝色窗口：

> Windows 已保护你的电脑
> Microsoft Defender SmartScreen 阻止了无法识别的应用启动。运行此应用可能会导致你的电脑存在风险。
> 发行者：发布者未知

这是 **基于信誉的拦截**，不是病毒告警：安装包没有数字签名，也没有建立下载信誉，
SmartScreen 无法确认发布者身份，因此默认拦截。点击「**仍要运行**」即可继续安装，
安装包本身功能与安全性不受影响。

## 二、用户侧临时处理（本机 / 内部分发）

任选其一：

1. **直接点「仍要运行」**（推荐）。窗口上两个按钮即「仍要运行 / 不运行」；
   旧版界面可能先显示「更多信息 → 仍要运行」。
2. 右键安装包 → **属性 → 常规 → 勾选「解除锁定」→ 确定**，然后再双击。
   原理：清除文件从网络下载时附带的「网络标记」（Zone.Identifier）。
3. 命令行绕过（等价于 1）：
   ```powershell
   Unblock-File -LiteralPath "D:\...\鸟类_安装包_v1.7.1.exe"
   Start-Process "D:\...\鸟类_安装包_v1.7.1.exe"
   ```
4. 校验完整性后再运行（发布方应提供 SHA-256）：
   ```powershell
   Get-FileHash -Algorithm SHA256 "鸟类_安装包_v1.7.1.exe"
   ```
   本版安装包 SHA-256：
   `A5A4D6C7AF6B3096A89B7ED7753CAF8035BA4A92F3E5CE9864AED953CB2F1B90`

> 不建议让最终用户关闭 SmartScreen（设置 → 应用和浏览器控制 → 基于信誉的保护）。
> 关闭安全功能会降低整机防护，且每台新机器都要重复设置。

## 三、发布侧根治：代码签名

### 3.1 证书选择

| 类型 | 获取难度 | SmartScreen 效果 | 适用 |
|---|---|---|---|
| **EV 代码签名证书** | 需企业资质审核 + 硬件令牌/云 HSM | 签名即接近即时信任，通常不再拦截 | 对外正式分发（推荐） |
| OV 代码签名证书 | 需企业资质审核 + 硬件令牌 | 签名后仍需积累下载信誉，初期部分机器提示一次 | 内部/小范围分发 |
| 自签名证书 | 免费、几分钟 | **无效**：不受信任的签名等于没签，仍会拦截 | 仅开发测试 |

注意：自 2023 年起 CA/B Forum 要求代码签名私钥必须存放在硬件令牌 / USB Key / 云 HSM 中，
不能用普通 PFX 文件长期保存（PFX 仅作临时导入或云签发导出使用）。

### 3.2 签名哪些文件

必须同时签：

1. **安装包** `鸟类_安装包_v1.7.1.exe` —— 用户双击运行的是它；
2. **主程序** `发布程序/鸟类/鸟类.exe` —— 安装包内嵌的程序，安装后首次运行同样会做信誉检查；
3. （可选）`拖入文件自动爬取.bat` 不参与签名；`model_trainer.py` 等脚本不需要签名。

顺序很重要：

```
PyInstaller 冻结 → 签 鸟类.exe → Inno Setup 编译 → 签 安装包.exe → 发布
```

签名后再编译安装包，签名才会随安装包分发；先编译再签安装包只能覆盖安装包本身，
内层 `鸟类.exe` 仍是未签名状态。

### 3.3 签名命令

本仓库已提供封装脚本 `packaging/sign_release.py`：

```powershell
# 列出证书库里可用的代码签名证书
python packaging/sign_release.py --list-certs

# 证书已装进「当前用户\个人」且只有一张（硬件令牌已插入）：
python packaging/sign_release.py

# 指定证书指纹（多张证书时）：
python packaging/sign_release.py --thumbprint 1A2B3C4D5E6F...

# 使用 PFX（临时/云 HSM 导出）：
python packaging/sign_release.py --pfx D:\cert\bird.pfx --password ********
```

等价的 signtool 原生命令（脚本内部即执行它）：

```powershell
signtool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /sha1 <证书指纹> `
    "release\鸟类_v1.7.1\发布程序\鸟类\鸟类.exe"

signtool verify /pa /v "release\鸟类_v1.7.1\发布程序\鸟类\鸟类.exe"
```

签名必须带 RFC3161 时间戳（`/tr`），否则证书过期后历史安装包会全部失效。

### 3.4 与构建流水线集成

`packaging/build_installer.py` 支持构建时自动签名：

```powershell
python packaging/build_installer.py --sign-thumbprint 1A2B3C...
# 或
python packaging/build_installer.py --sign-pfx D:\cert\bird.pfx --sign-password ********
```

流水线会：

1. 冻结构建、部署组件、预置数据库；
2. **先签** `发布程序/鸟类/鸟类.exe`；
3. 冒烟测试（签名不影响运行）；
4. Inno Setup 编译安装包；
5. **再签** 安装包；
6. 生成 `安装输出/SHA256SUMS.txt`。

### 3.5 签名后仍提示怎么办

- **OV 证书**：新证书没有信誉，少数机器首次仍提示。解决办法：
  - 持续发布（同一证书签名、下载量积累后信誉建立）；
  - 向 Microsoft 提交「误报/信誉」：
    <https://www.microsoft.com/wdsi/filesubmission>（选择 Software developer / 误报）；
  - 在 Releases 页面附 SHA-256、VirusTotal 链接、签名证书主体名称。
- **EV 证书**：通常签名即生效；若仍提示，检查是否签错文件（内层 exe 未签、
  或签名后又被重新打包导致签名失效）。
- 验证签名是否生效：
  ```powershell
  Get-AuthenticodeSignature "鸟类_安装包_v1.7.1.exe" | Format-List Status,SignerCertificate
  signtool verify /pa /v "鸟类_安装包_v1.7.1.exe"
  ```

## 四、常见误区

| 做法 | 结果 |
|---|---|
| 改文件名/加版本号 | 无效，信誉按文件哈希与签名主体判定 |
| 压缩成 ZIP 分发 | 无效：解压后 exe 继承网络标记，且信誉检查照旧 |
| 关掉用户机器的 SmartScreen | 不安全，每台机器都要手动设置，不可推广 |
| 自签名证书 | 无效：证书链不受信任 |
| 只签安装包、不签内层 鸟类.exe | 安装后首次启动主程序仍可能提示 |

## 五、本版状态

- 当前 `鸟类_安装包_v1.7.1.exe`：**未签名**（`Status = NotSigned`），因此会出现 SmartScreen 提示；
- 本机已具备 `signtool.exe`（Windows SDK 10.0.26100），但**未安装代码签名证书**；
- 获得证书后按第 3.3 或 3.4 节操作即可签名，无需改动代码。
