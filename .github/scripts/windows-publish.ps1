param(
    # 逗号分隔的服务名：gateway,image,search,reader,trends,amap,context7,weather,flyai
    [Parameter(Mandatory = $true)]
    [string]$Services,

    # ACR 命名空间前缀，例如 <registry>/seanfield
    [Parameter(Mandatory = $true)]
    [string]$ImagePrefix,

    [Parameter(Mandatory = $true)]
    [string]$ImageTag
)

# 只构建并推送镜像；检查和测试在 dev 上的 check-* 任务里跑。
# 多个服务用 docker buildx bake 并行构建并直接推送，不再逐个 build / push。
$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$contexts = @{ gateway = "gateway"; image = "services\image"; search = "services\search"; reader = "services\reader"; trends = "services\trends"; amap = "services\amap"; context7 = "services\context7"; weather = "services\weather"; flyai = "services\flyai" }

$selected = @($Services -split "," | Where-Object { $_ })
if ($selected.Count -eq 0) { throw "没有要构建的服务" }
foreach ($service in $selected) {
    if (-not $contexts.ContainsKey($service)) { throw "未知服务：$service" }
}

# 代理与 PyPI 镜像源以 build-arg 传入：bake 不会像 docker build 那样自动带上代理环境变量。
$buildArgs = @{}
foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "PIP_INDEX_URL")) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value) { $buildArgs[$name] = $value }
}

$targets = @{}
foreach ($service in $selected) {
    $targets[$service] = @{
        context = (Join-Path $root $contexts[$service])
        tags    = @("${ImagePrefix}/agent-tools-${service}:${ImageTag}")
        args    = $buildArgs
    }
}
$bake = @{ group = @{ default = @{ targets = $selected } }; target = $targets }
$bakeFile = Join-Path $env:RUNNER_TEMP "agent-tools-bake-$env:GITHUB_RUN_ID.json"
ConvertTo-Json -InputObject $bake -Depth 6 | Set-Content -Path $bakeFile -Encoding utf8
Write-Host "bake 目标：$($selected -join ', ')"

# 拉 Docker Hub 基础镜像、下载依赖、推 ACR 偶发失败，整体重试 3 次；已建好的层会命中缓存，重试只补失败的部分。
for ($attempt = 1; $attempt -le 3; $attempt++) {
    docker buildx bake -f $bakeFile --push --provenance=false --progress=plain
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -eq 3) { throw "镜像构建推送失败（已重试 3 次）" }
    $delay = 20 * $attempt
    Write-Host "镜像构建推送失败，第 $attempt 次，${delay}s 后重试..."
    Start-Sleep -Seconds $delay
}
Remove-Item -Force $bakeFile -ErrorAction SilentlyContinue
foreach ($service in $selected) {
    Write-Host "pushed ${ImagePrefix}/agent-tools-${service}:${ImageTag}"
}
