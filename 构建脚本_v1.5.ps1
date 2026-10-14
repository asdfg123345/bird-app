# ============================================================
# 鸟类 v1.5 一键构建脚本（PyInstaller + Inno Setup，含数字水印）
# 作者签名：天咏（明月照今日，风摇过两川。）
# 用法：powershell -ExecutionPolicy Bypass -File 构建脚本_v1.5.ps1
# 步骤：准备打包源码（含水印注入）→ PyInstaller 构建 → 部署数据文件
#       与附件 → 生成预置数据库 → Inno Setup 编译安装包 → 构建验证
# ============================================================

$ErrorActionPreference = "Stop"
# 本脚本所在目录（即 v1.5 发布目录）
$ReleaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PkgDir = Join-Path $ReleaseDir "打包源码"
$DistDir = Join-Path $ReleaseDir "发布程序\鸟类"
$AppExe = Join-Path $DistDir "鸟类.exe"
# 显式指定系统 Python（装有 PyInstaller 6.18.0）：沙箱外 PATH 首位是
# miniconda base（无 PyInstaller），直接用 python 命令会解析到错误解释器
$Python = "C:\Users\MECHREVO\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $Python)) { throw "未找到系统 Python：$Python" }

Write-Host "== 鸟类 v1.5 构建（数字水印版）==" -ForegroundColor Cyan

# 步骤 1：准备打包源码（复制 + 注入冻结适配 + 数字水印注入 + 生成版本资源与 iss）
Write-Host "`n[1/6] 准备打包源码（含水印注入）..." -ForegroundColor Yellow
& $Python (Join-Path $ReleaseDir "准备打包源码_v1.5.py")
if ($LASTEXITCODE -ne 0) { throw "准备打包源码失败（退出码 $LASTEXITCODE）" }
# 读取构建指纹存档，供验证步骤比对
$FP = Get-Content (Join-Path $ReleaseDir "构建指纹_v1.5.txt") -Encoding UTF8
$WatermarkId = ($FP | Where-Object { $_ -like "watermark_id=*" }) -replace "watermark_id=", ""
Write-Host "数字水印标识：$WatermarkId" -ForegroundColor Cyan

# 步骤 2：PyInstaller 构建（onedir + console；构建会重建 发布程序\鸟类 目录）
Write-Host "`n[2/6] PyInstaller 构建（可能需要数分钟）..." -ForegroundColor Yellow
Push-Location $ReleaseDir
try {
    & $Python -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $ReleaseDir "发布程序") `
        --workpath (Join-Path $ReleaseDir "build_tmp") `
        (Join-Path $ReleaseDir "鸟类.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败（退出码 $LASTEXITCODE）" }
} finally {
    Pop-Location
}
if (-not (Test-Path $AppExe)) { throw "未找到构建产物：$AppExe" }
Write-Host "构建产物：$AppExe"

# 步骤 3：部署数据文件与附件（PyInstaller 重建会清空 dist，必须重新部署）
Write-Host "`n[3/6] 部署数据文件与附件..." -ForegroundColor Yellow
Copy-Item (Join-Path $PkgDir "model_trainer.py") $DistDir -Force
Copy-Item (Join-Path $PkgDir "model_library.py") $DistDir -Force
Copy-Item (Join-Path $PkgDir "发布附件\*") $DistDir -Force
# 内置视觉分析系统源码（视觉分析页签的问答引擎，随安装包分发）
$VisionSrc = Join-Path $PkgDir "vision_system"
if (Test-Path $VisionSrc) {
    $VisionDst = Join-Path $DistDir "vision_system"
    if (Test-Path $VisionDst) { Remove-Item $VisionDst -Recurse -Force }
    Copy-Item $VisionSrc $VisionDst -Recurse -Force
    Write-Host "已部署：model_trainer.py、model_library.py、启动爬虫界面.bat、拖入文件自动爬取.bat、使用说明.txt、vision_system\"
} else {
    Write-Host "[警告] 打包源码中缺少 vision_system 目录，视觉分析页将无法自动探测引擎" -ForegroundColor Yellow
}

# 步骤 4：生成预置空数据库（数据目录固定到 exe 所在目录，验证冻结适配）
Write-Host "`n[4/6] 生成预置数据库..." -ForegroundColor Yellow
if (Test-Path (Join-Path $DistDir "data\db")) {
    Remove-Item (Join-Path $DistDir "data\db") -Recurse -Force
}
& $AppExe init-db
if ($LASTEXITCODE -ne 0) { throw "init-db 失败（退出码 $LASTEXITCODE）" }
$dbMain = Join-Path $DistDir "data\db\bird_observations.db"
if (-not (Test-Path $dbMain)) { throw "预置数据库未生成：$dbMain（冻结适配可能未生效）" }
Write-Host "预置数据库已生成：$dbMain"

# 步骤 5：Inno Setup 编译安装包（安装包 PE 资源与向导版权行含水印）
Write-Host "`n[5/6] Inno Setup 编译安装包..." -ForegroundColor Yellow
$ISCC = "C:\Program Files\Inno Setup 7\ISCC.exe"
if (-not (Test-Path $ISCC)) { throw "未找到 Inno Setup 编译器：$ISCC" }
& $ISCC (Join-Path $ReleaseDir "封装脚本_v1.5.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败（退出码 $LASTEXITCODE）" }

$SetupExe = Join-Path $ReleaseDir "鸟类_安装包_v1.5.exe"
if (-not (Test-Path $SetupExe)) { throw "安装包未生成：$SetupExe" }

# 步骤 6：构建验证（体积回归 + 程序水印自检 + PE 资源水印核对）
Write-Host "`n[6/6] 构建验证..." -ForegroundColor Yellow
# 6.1 体积回归：超过 100MB 说明 torch 依赖泄漏（正常约 26~27MB）
$sizeMB = [math]::Round((Get-Item $SetupExe).Length / 1MB, 1)
if ($sizeMB -gt 100) {
    throw "安装包体积异常（$sizeMB MB > 100MB）：torch 依赖泄漏，请检查 spec excludes"
}
Write-Host "体积回归通过：安装包 $sizeMB MB（< 100MB 红线，正常 26~27MB）" -ForegroundColor Green

# 6.2 程序水印自检：copyright 子命令输出必须含完整水印标识
$wmOut = & $AppExe copyright
if ($LASTEXITCODE -ne 0 -or -not ($wmOut -match [regex]::Escape($WatermarkId))) {
    throw "程序数字水印自检失败：copyright 输出未包含 $WatermarkId"
}
Write-Host "程序水印自检通过：copyright 输出含 $WatermarkId" -ForegroundColor Green

# 6.3 exe PE 版本资源核对：公司 / 版权 / 水印 Comments 三项齐备
$vi = (Get-Item $AppExe).VersionInfo
$viComments = "$($vi.Comments)"
if ("$($vi.CompanyName)" -ne "天咏" -or
    -not ("$($vi.LegalCopyright)" -like "*2026 天咏*") -or
    -not ($viComments -like "*$WatermarkId*")) {
    throw "exe PE 版本资源水印核对失败：公司='$($vi.CompanyName)' 版权='$($vi.LegalCopyright)' 备注='$viComments'"
}
Write-Host "exe PE 资源水印核对通过：公司=天咏 / 版权=© 2026 天咏 / 备注=数字水印 $WatermarkId" -ForegroundColor Green

# 6.4 安装包 PE 版本资源核对（Inno 写入的安装包元数据；字段值可能带
#     空白填充，比较前先 Trim）
$svi = (Get-Item $SetupExe).VersionInfo
$sviCompany = "$($svi.CompanyName)".Trim()
$sviCopyright = "$($svi.LegalCopyright)".Trim()
$sviDesc = "$($svi.FileDescription)".Trim()
if ($sviCompany -ne "天咏" -or
    -not ($sviCopyright -like "*2026 天咏*") -or
    -not ($sviDesc -like "*$WatermarkId*")) {
    throw "安装包 PE 资源水印核对失败：公司='$sviCompany' 版权='$sviCopyright' 描述='$sviDesc'"
}
Write-Host "安装包 PE 资源水印核对通过：公司=天咏 / 版权=© 2026 天咏 / 描述=数字水印 $WatermarkId" -ForegroundColor Green

Write-Host "`n== 构建完成（数字水印 $WatermarkId）==" -ForegroundColor Cyan
Write-Host "安装包：$SetupExe（$sizeMB MB）"
Write-Host "发布单元：$DistDir"
