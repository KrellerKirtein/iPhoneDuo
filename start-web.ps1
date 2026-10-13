[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 1421,
    [string]$DataDir,
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$previousDirectory = Get-Location
$previousPath = $env:PATH
$previousCargoHome = $env:CARGO_HOME
$previousRustupHome = $env:RUSTUP_HOME
$startupExitCode = 0

# 外部命令失败即终止准备，避免依赖未就绪时仍显示启动成功。
function Invoke-StartupCommand {
    param([string]$Executable, [string[]]$Arguments, [string]$FailureMessage)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage（退出码 $LASTEXITCODE）"
    }
}

try {
    Set-Location -LiteralPath $PSScriptRoot

    # 支持从任意目录或资源管理器启动，不依赖 Codex 的进程环境。
    $nodeCommand = Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $nodeExecutable = if ($nodeCommand) { $nodeCommand.Source } else {
        Join-Path $env:ProgramFiles 'nodejs\node.exe'
    }
    if (-not (Test-Path -LiteralPath $nodeExecutable -PathType Leaf)) {
        throw '请先安装 Node.js 22.22 或更新版本，然后重新启动。'
    }
    $nodeVersion = & $nodeExecutable -p 'process.versions.node'
    if ($LASTEXITCODE -ne 0 -or [version]$nodeVersion -lt [version]'22.22.0') {
        throw '需要 Node.js 22.22 或更新版本。'
    }
    $env:PATH = (Split-Path -Parent $nodeExecutable) + ';' + $env:PATH

    # 优先复用仓库本地工具链；环境变量只对本次启动及子进程生效。
    $localToolchain = Join-Path $PSScriptRoot 'artifacts\toolchain'
    $localCargo = Join-Path $localToolchain 'cargo\bin\cargo.exe'
    if ((Test-Path -LiteralPath $localCargo -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $localToolchain 'rustup\toolchains') -PathType Container)) {
        $env:CARGO_HOME = Join-Path $localToolchain 'cargo'
        $env:RUSTUP_HOME = Join-Path $localToolchain 'rustup'
        $env:PATH = (Split-Path -Parent $localCargo) + ';' + $env:PATH
    } elseif (-not (Get-Command cargo.exe -CommandType Application -ErrorAction SilentlyContinue)) {
        $userCargoDirectory = if ($env:CARGO_HOME) { Join-Path $env:CARGO_HOME 'bin' } else {
            Join-Path $env:USERPROFILE '.cargo\bin'
        }
        if (Test-Path -LiteralPath (Join-Path $userCargoDirectory 'cargo.exe') -PathType Leaf) {
            $env:PATH = $userCargoDirectory + ';' + $env:PATH
        }
    }
    $cargoCommand = Get-Command cargo.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cargoCommand) {
        throw '未找到 Rust 工具链。请安装 Rust stable（MSVC）及 Visual Studio C++ Build Tools 后重试。'
    }
    Invoke-StartupCommand $cargoCommand.Source @('--version') 'Rust 工具链不可用'

    # 首次缺少前端依赖时按锁文件安装；已有依赖时直接进入统一启动器。
    $requiredPackages = @('vite', 'react', 'react-dom', 'lucide-react', '@tauri-apps/api')
    $missingPackages = @($requiredPackages | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "node_modules/$_/package.json"))
    })
    if ($missingPackages.Count -gt 0) {
        Write-Host '首次启动：安装前端依赖……'
        $pnpmCommand = Get-Command pnpm.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pnpmCommand) {
            Invoke-StartupCommand $pnpmCommand.Source @('install', '--frozen-lockfile') '前端依赖安装失败'
        } else {
            $npmExecutable = Join-Path (Split-Path -Parent $nodeExecutable) 'npm.cmd'
            if (-not (Test-Path -LiteralPath $npmExecutable -PathType Leaf)) {
                throw '未找到 pnpm 或 npm，请安装完整的 Node.js 开发环境。'
            }
            $packageManager = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package.json') -Raw | ConvertFrom-Json).packageManager
            Invoke-StartupCommand $npmExecutable @('exec', '--yes', "--package=$packageManager", '--', 'pnpm', 'install', '--frozen-lockfile') '前端依赖安装失败'
        }
    }

    $serverArguments = @('scripts/dev-web.mjs', '--port', [string]$Port)
    if ($NoOpen) { $serverArguments += '--no-open' }
    if ($DataDir) { $serverArguments += @('--data-dir', $DataDir) }
    Write-Host '正在启动前后端。首次编译可能需要几分钟；启动后按 Ctrl+C 一起停止。'
    & $nodeExecutable @serverArguments
    $startupExitCode = $LASTEXITCODE
} catch {
    Write-Host ('启动失败：' + $_.Exception.Message) -ForegroundColor Red
    $startupExitCode = 1
} finally {
    $env:PATH = $previousPath
    $env:CARGO_HOME = $previousCargoHome
    $env:RUSTUP_HOME = $previousRustupHome
    Set-Location -LiteralPath $previousDirectory
}
exit $startupExitCode
