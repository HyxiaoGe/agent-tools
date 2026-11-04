from fastapi import APIRouter, BackgroundTasks, Request

from app.limiter import limiter
from app.models import SearchRequest, SearchResponse
from app.searching import SearchFailed, run_search
from app.usage import record_search_credits

router = APIRouter()


@router.post("/search", response_model=SearchResponse)
@limiter.limit("10/second")
async def search(
    request: Request,
    body: SearchRequest,
    background_tasks: BackgroundTasks,
) -> SearchResponse:
    """流程见 app.searching。主 provider 和备用链都失败时照旧抛主 provider 的异常：额度用完 503，其余 500。"""
    try:
        outcome = await run_search(body)
    except SearchFailed as e:
        # 抛回主 provider 的原异常（状态码照旧）；from None：它和 SearchFailed 互为 cause 会成环
        raise e.primary from None
    for provider, credits_used in outcome.credits:
        background_tasks.add_task(record_search_credits, provider, credits_used)
    return outcome.response
