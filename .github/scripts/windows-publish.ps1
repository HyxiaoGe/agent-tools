param(
    # 逗号分隔的服务名：gateway,image,search,reader,trends
    [Parameter(Mandatory = $true)]
    [string]$Services,

    # ACR 命名空间前缀，例如 <registry>/seanfield
    [Parameter(Mandatory = $true)]
    [string]$ImagePrefix,

    [Parameter(Mandatory = $true)]
    [string]$ImageTag
)

# 只构建并推送镜像；检查和测试在 dev 上的 check-* 任务里跑。
$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$contexts = @{ gateway = "gateway"; image = "services\image"; search = "services\search"; reader = "services\reader"; trends = "services\trends" }

# 拉 Docker Hub 基础镜像、推 ACR 偶发 EOF，各重试 3 次。
function Invoke-WithRetry {
    param([string]$Label, [scriptblock]$Action)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        & $Action
        if ($LASTEXITCODE -eq 0) { return }
        if ($attempt -eq 3) { throw "$Label 失败（已重试 3 次）" }
        $delay = 20 * $attempt
        Write-Host "$Label 失败，第 $attempt 次，${delay}s 后重试..."
        Start-Sleep -Seconds $delay
    }
}

foreach ($service in ($Services -split "," | Where-Object { $_ })) {
    if (-not $contexts.ContainsKey($service)) { throw "未知服务：$service" }
    $ref = "${ImagePrefix}/agent-tools-${service}:${ImageTag}"
    $context = Join-Path $root $contexts[$service]
    Invoke-WithRetry -Label "$service 构建" -Action { docker build --provenance=false -t $ref $context }
    Invoke-WithRetry -Label "$service 推送" -Action { docker push $ref }
    Write-Host "pushed $ref"
}
