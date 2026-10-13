param(
    [string]$VerifiedOn = (Get-Date -Format 'yyyy-MM-dd')
)

$ErrorActionPreference = 'Stop'
# 从官网公开目录提取中国大陆门店；结构异常时停止，避免覆盖已有目录。
$source = 'https://www.apple.com.cn/retail/storelist/'
$html = (Invoke-WebRequest $source -UseBasicParsing -TimeoutSec 30).Content
$match = [regex]::Match($html, '<script id="__NEXT_DATA__" type="application/json">(.*?)</script>')
if (-not $match.Success) { throw '官网门店目录结构已变化。' }
$data = $match.Groups[1].Value | ConvertFrom-Json
$region = @($data.props.pageProps.storeList | Where-Object { $_.locale -eq 'zh_CN' })
if ($region.Count -ne 1) { throw '未找到唯一的中国大陆门店目录。' }
$stores = @($region[0].state | ForEach-Object {
    $province = $_.name
    $_.store | ForEach-Object {
        if ($_.id -notmatch '^R\d{3,5}$' -or -not $_.name -or -not $_.address.city) {
            throw '官网返回了不完整的门店数据。'
        }
        [ordered]@{
            id = $_.id
            name = $_.name.Trim()
            province = $province
            city = $_.address.city
            address = (@($_.address.address1, $_.address.address2) | Where-Object { $_ }) -join '，'
        }
    }
})
if ($stores.Count -eq 0 -or @($stores.id | Sort-Object -Unique).Count -ne $stores.Count) {
    throw '官网门店目录为空或包含重复编号。'
}
$catalog = [ordered]@{ source = $source; updatedAt = $VerifiedOn; stores = $stores }
$json = $catalog | ConvertTo-Json -Depth 5
$target = Join-Path $PSScriptRoot '../src/retail-stores.ts'
$content = "/** 官网中国大陆门店目录快照，由 scripts/update-retail-stores.ps1 更新。 */`nexport const retailStoreCatalog = $json;`n"
[IO.File]::WriteAllText($target, $content, [Text.UTF8Encoding]::new($false))
Write-Output "已更新 $($stores.Count) 家中国大陆门店（$VerifiedOn）。"
