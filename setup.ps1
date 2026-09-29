<#
  setup.ps1 — Windows 一鍵安裝(對應 Mac 的 setup.sh)。

  全新 Windows 沒有 bash,也沒有 brew,所以這支用 winget 把系統層工具裝好,
  再建 Python 環境、裝套件、裝字型。設計成可重複執行。

  用法(在 PowerShell 裡,cd 到工具包資料夾):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

  逐字稿引擎注意:pip 版 faster-whisper 在「部分」全新 Windows 11 會被 Smart App
  Control 擋掉(未簽章 DLL)— 實測多台都沒被擋,所以先試 pip 版,被擋才引導
  Faster-Whisper-XXL 獨立版。另外 ctranslate2 必須 <4.6(4.6+ 載入模型必崩潰)。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$KIT = Split-Path -Parent $MyInvocation.MyCommand.Path
$FREECUT = Join-Path $KIT "tools\freecut"

Write-Host "=== ai-edit 工具包 安裝 (Windows) ==="
Write-Host "工具包位置: $KIT`n"

function Refresh-Path {
  # winget 裝完不會更新「目前這個」PowerShell 的 PATH,手動重讀一次才找得到新工具。
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [System.Environment]::GetEnvironmentVariable("Path","User")
}

function Find-RealPython {
  # Windows 預設在 WindowsApps 放 python.exe/python3.exe 的 Microsoft Store 空殼
  # (點了只會開商店頁)。Get-Command 找得到它,但不能用。
  # 「裝不裝」跟「拿來用」必須用同一套判斷 — 之前只有後半段排除空殼,
  # 結果空殼騙過安裝判斷 → 跳過安裝 → 後面找不到 python → exit 1,
  # 錯誤訊息還叫人重開 PowerShell 再跑(重跑幾次都一樣)。兩台實測機都踩過。
  foreach ($name in @("python", "python3")) {
    foreach ($c in (Get-Command $name -All -ErrorAction SilentlyContinue)) {
      if ($c.Source -like "*WindowsApps*") { continue }   # 商店空殼,跳過
      try { $v = & $c.Source --version 2>&1 } catch { continue }
      if ($v -match "Python 3\.") { return $c.Source }
    }
  }
  return $null
}

# --- 0. winget 在不在 -------------------------------------------------------
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  Write-Host "[X] 找不到 winget。請先更新 Windows / 從 Microsoft Store 裝『應用程式安裝程式』後再重跑。"
  exit 1
}

# --- 1. 用 winget 裝系統層工具(一次講清楚要裝什麼)------------------------
$deps = @(
  # Python 不能用單純的 Get-Command 當 Probe — 會被 Microsoft Store 空殼騙過,見 Find-RealPython。
  @{ Name = "Python 3.12"; Id = "Python.Python.3.12";  Probe = $null; PyProbe = $true },
  @{ Name = "Node.js LTS"; Id = "OpenJS.NodeJS.LTS";    Probe = "node"   },
  @{ Name = "ffmpeg";      Id = "Gyan.FFmpeg";          Probe = "ffmpeg" },
  # VC++ 執行階段:CTranslate2 這類含 C 擴充的套件常需要。實測機器上有裝
  # (雖然當時是誤判裝的),保險起見一起裝 — 已裝過 winget 會自己跳過。
  @{ Name = "VC++ Redistributable"; Id = "Microsoft.VCRedist.2015+.x64"; Probe = $null;
     RegProbe = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" }
)
Write-Host "--- 系統工具(winget)---"
Write-Host "(過程中可能跳出藍色的「使用者帳戶控制」視窗,請按「是」— 不按流程會停在原地等。"
Write-Host " ffmpeg 有 250MB,下載+解壓可能要 20 分鐘以上,沒有進度條也不是當機。)"
Refresh-Path   # 先重讀 PATH:重跑 setup 時,上次裝好的工具才偵測得到(不然會誤判成沒裝)
foreach ($d in $deps) {
  $installed = $false
  if ($d.Probe -and (Get-Command $d.Probe -ErrorAction SilentlyContinue)) { $installed = $true }
  if ($d.PyProbe -and (Find-RealPython)) { $installed = $true }
  if ($d.RegProbe -and (Test-Path $d.RegProbe)) { $installed = $true }
  if ($installed) {
    Write-Host "[OK] $($d.Name) 已安裝"
    continue
  }
  Write-Host "   安裝 $($d.Name) ..."
  # 故意不吞 winget 的輸出(不接 Out-Null)— 如果之後又有人回報「明明裝過還在重裝」,
  # 這裡的原始輸出跟 exit code 能幫忙判斷是腳本判斷錯,還是 winget/環境本身的問題。
  winget install --id $d.Id -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] winget 裝 $($d.Name) 回報了非 0 結束碼($LASTEXITCODE),可能已經裝過或裝失敗,請看上面的輸出。"
  }
}
Refresh-Path

# --- 2. Python 環境 + 套件 --------------------------------------------------
# 找真的 python — 跟第 1 段的安裝判斷共用 Find-RealPython,不會再自相矛盾。
$PY = Find-RealPython
if (-not $PY) {
  Write-Host "[X] 找不到可用的 Python。看看上面 winget 裝 Python 那段有沒有錯誤訊息;"
  Write-Host "    剛裝好的話,把 PowerShell 關掉重開再跑一次 setup.ps1。"
  Write-Host "    重跑還是這樣,就手動裝(裝完重開 PowerShell 再跑 setup):"
  Write-Host "    winget install --id Python.Python.3.12 --source winget --scope user --silent ``"
  Write-Host "      --accept-source-agreements --accept-package-agreements --disable-interactivity"
  exit 1
}
Write-Host "[OK] python: $(& $PY --version)"

$venv = Join-Path $FREECUT ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
  Write-Host "   建立 Python 環境 (tools\freecut\.venv) ..."
  & $PY -m venv $venv
}
$VPY = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $VPY)) { Write-Host "[X] Python 環境沒建成功。確認 Python 真的裝好再重跑。"; exit 1 }

# pip 一律讓輸出看得見 — 之前用 -q + Out-Null,PyPI 塞車時畫面靜止 12 分鐘,
# 學員(跟 AI)都以為當機。有進度在動就不會誤判。
& $VPY -m pip install --upgrade pip | Out-Null
Write-Host "   安裝核心套件 (requests, pillow, numpy, opencc, jieba) ..."
# opencc-python-reimplemented: 簡轉繁,純 Python(沒 C++ DLL)。刻意不用 PyPI 的
# `opencc`,那個帶未簽章 DLL,在 Windows 可能又被 Smart App Control 擋。
# jieba: 中文斷詞,純 Python。字幕斷行用,不讓 Whisper 的單字 token 把詞切兩半(「自/然」)。
& $VPY -m pip install requests pillow numpy opencc-python-reimplemented jieba

# --- 3. 逐字稿引擎:先試 pip faster-whisper,擋住就引導 XXL 獨立版 --------
Write-Host "   先試 faster-whisper(pip)... 下載幾百 MB,PyPI 塞車時可能 10 分鐘以上,慢不是當機。"
# ctranslate2 一定要釘 <4.6:4.6+ 在 Windows 載入 Whisper 模型的瞬間直接 access
# violation(0xC0000005)。最陰的是 setup 全綠、import 也過 — 崩潰發生在「載入模型」,
# 學員把影片丟進來、剪到轉逐字稿那步才炸。實測 4.5.0 正常、4.8.1 必炸。
# 放同一行裝:舊 venv 裡已有 4.8 的話,這行也會把它降回來。
# setuptools 也要釘 <81:ctranslate2 4.5.0 執行時 import pkg_resources,
# setuptools 81 起已把它移除 → import faster_whisper 直接 ModuleNotFoundError。
# 也就是說「釘 ctranslate2<4.6」自己需要這條配套,缺了它每台新裝/更新機都會炸(多名學員實測)。
& $VPY -m pip install faster-whisper "ctranslate2<4.6" "setuptools<81"
# 驗證 import 時暫時把 ErrorActionPreference 放回 Continue:
# PS 5.1 在 EAP=Stop 下,原生程式的 stderr 一經重導(2>&1 / 2>$null)就變 NativeCommandError
# 把整支 setup 砍斷 — 連「import 成功但印了個 warning」都會炸,後面字型/偏好檔全部沒跑(多名學員實測)。
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $VPY -c "import faster_whisper" 2>$null | Out-Null
$fwOK = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEAP
if ($fwOK) {
  Write-Host "[OK] faster-whisper 已安裝且可用"
} else {
  Write-Host "[!] faster-whisper 裝了但無法載入(多半是 Smart App Control 擋未簽章 DLL:"
  Write-Host "    「應用程式控制原則已封鎖此檔案」)。這個封鎖有時候幾小時後會自己解除,"
  Write-Host "    不急的話可以先重開機或晚點重跑一次 setup.ps1 試試。還是被擋的話,"
  Write-Host "    改用 Faster-Whisper-XXL 獨立版:"
  Write-Host "    1. https://github.com/Purfview/whisper-standalone-win/releases"
  Write-Host "    2. 下載 Faster-Whisper-XXL 的 Windows 版,解壓縮"
  Write-Host "    3. 整個資料夾放到 tools\whisper-xxl\(裡面要有 faster-whisper-xxl.exe)"
  Write-Host "    詳見 README「Windows 疑難排解」。"
}
# XXL 兩個可放位置都查(tools\whisper-xxl 或 tools\freecut\whisper-xxl)
$xxl = @( (Join-Path $KIT "tools\whisper-xxl"), (Join-Path $FREECUT "whisper-xxl") ) |
  Where-Object { Test-Path $_ } |
  ForEach-Object { Get-ChildItem $_ -Recurse -Filter "faster-whisper-xxl.exe" -ErrorAction SilentlyContinue } |
  Select-Object -First 1
if ($xxl) { Write-Host "[OK] Faster-Whisper-XXL 獨立版已就位:$($xxl.FullName)" }

# --- 3.5 先把逐字稿模型抓下來(趁現在,不要留給第一支影片)-------------------
# 不先抓的話,學員第一次剪片會卡在一段沒輸出的 460MB 模型下載,以為當機(實測)。
# HF_HUB_DISABLE_XET=1:HF 的 Xet 下載通道在部分網路 0 KB/s 完全卡死,關掉就正常。
# 抓不成不算安裝失敗:第一次剪片會自動再抓,只是要多等幾分鐘。
Write-Host "   下載逐字稿模型(Systran/faster-whisper-small,約 460MB — 幾分鐘,有進度就是還在動)..."
$env:HF_HUB_DISABLE_XET = "1"
& $VPY -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small')"
if ($LASTEXITCODE -eq 0) {
  Write-Host "[OK] 逐字稿模型已就位,第一次剪片不用再等下載"
} else {
  Write-Host "[!] 模型這次沒抓成(多半是網路)— 不影響安裝,第一次剪片時會自動下載,屆時要多等幾分鐘"
}

# --- 4. 字型 ---------------------------------------------------------------
Write-Host "`n--- 字型 ---"
$fontScript = Join-Path $KIT "scripts\windows\install-font.ps1"
if (Test-Path $fontScript) {
  & $fontScript
} else {
  Write-Host "[!] 找不到 install-font.ps1,請手動裝思源宋體。"
}

# --- 5. 個人偏好檔(範本在 tools/ 底下)------------------------------------
$prefTpl = Join-Path $KIT "tools\我的剪輯偏好.範本.md"
$pref    = Join-Path $KIT "我的剪輯偏好.md"
if ((-not (Test-Path $pref)) -and (Test-Path $prefTpl)) {
  Copy-Item $prefTpl $pref
  Write-Host "[OK] 建立個人偏好檔:我的剪輯偏好.md"
}

# --- 6. 把內部運作用的檔案在檔案總管裡藏起來(純外觀,git 跟指令都不受影響)---
# 使用者打開資料夾只會看到 審片.html、我的影片、素材庫、我的剪輯偏好.md、錯誤回報。
# 想看隱藏檔:檔案總管 → 檢視 → 顯示 → 隱藏的項目。可重複執行。
# README.md 刻意留在外面不藏 — 學員照 README 走到一半跑完 setup,回頭想再看就找不到了。
foreach ($f in @("LICENSE",".gitignore","setup.sh","setup.ps1","scripts","tools","CLAUDE.md","AGENTS.md")) {
  $p = Join-Path $KIT $f
  if (Test-Path $p) {
    try { (Get-Item $p -Force).Attributes = (Get-Item $p -Force).Attributes -bor [IO.FileAttributes]::Hidden } catch {}
  }
}

# --- 7. 把「要告知使用者的話」寫成檔案(這份刻意不藏)------------------------
# 為什麼要寫成檔案:下面那段收尾訊息是印在 PowerShell 視窗的,但這個工具包的前提是
# 「使用者不開終端機,所有指令由 AI 跑」— 所以那幾段話學員其實看不到,除非 AI 記得
# 轉述,而 SKILL.md 只叫 AI「看 exit code」。實測炸過:AI 看到成功就自己改寫成摘要,
# 「檔案被藏起來」沒轉達,學員打開檔案總管發現檔案不見,以為裝壞了。
$note = Join-Path $KIT "安裝完成說明.md"
@"
# 安裝完成說明

（這份由 setup 自動產生，每次重跑會更新。看完可以直接刪，不影響功能。）

## 狀態：[OK] 裝好了，可以開始剪片了

### 接下來 3 步

1. 用 Claude Code 打開這個資料夾（Select folder 選 ai-video-editor 本身）
2. 開新對話，把你的口播影片拖進來
3. 打一句：幫我剪這支影片

## 資料夾裡少了幾個檔案是正常的

setup 會把工具包內部運作用的檔案（``tools\``、``scripts\``、``setup.sh``、
``setup.ps1``、``CLAUDE.md``、``AGENTS.md``、``LICENSE``）在檔案總管裡隱藏起來，
讓你打開這個資料夾只看到會用到的東西。**功能完全不受影響**，只是不顯示而已。

想看回來：

- Windows：檔案總管「檢視」→「顯示」→ 勾「隱藏的項目」
- Mac：按 ``Cmd + Shift + .``（句點）

## 卡住的話

直接問 AI，或看 ``README.md``。想找真人：LINE 官方帳號 @headhomeuni
"@ | Set-Content -Path $note -Encoding UTF8

Write-Host "`n==============================" -ForegroundColor Green
Write-Host "   [ OK ] 裝好了,可以開始剪片了" -ForegroundColor Green
Write-Host "==============================`n" -ForegroundColor Green
Write-Host "接下來 3 步:"
Write-Host "  1. 用 Claude Code 打開這個資料夾(Select folder 選 ai-video-editor 本身)"
Write-Host "  2. 開新對話,把你的口播影片拖進來"
Write-Host "  3. 打一句:幫我剪這支影片`n"
Write-Host "看到上面綠色的 [ OK ] 橫幅就是「裝好了」的訊號。詳見 README.md 或直接問 AI。"
Write-Host "(資料夾裡少了幾個檔案是正常的 — 內部運作用的檔案被藏起來了,不影響功能;"
Write-Host " 檔案總管 → 檢視 → 顯示 → 隱藏的項目 可以隨時看到它們。)"
Write-Host ""
Write-Host "★ 給跑這支腳本的 AI:上面這段是寫給「使用者」看的,不是給你判斷用的。"
Write-Host "  同樣的內容已寫進 $note — 請把它的內容轉述給使用者,"
Write-Host "  特別是「檔案被藏起來是正常的」那段。只回報成功不算交代完。"
