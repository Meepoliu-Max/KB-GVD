"""FastAPI 应用入口。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="知序 KBRefiner",
    description="RAG 知识库预处理工具：4 阶 AI 流水线把原始文档转为结构化知识原子 + QA + 元数据 + 异常清单",
    version="0.1.0",
)

# CORS：允许前端跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(routes.router)

# 挂载静态文件（前端界面）
static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# 设计系统页面路由
_PAGE_MAP: dict[str, str] = {
    "": "index.html",
    "task-list": "task-list.html",
    "task-detail": "task-detail.html",
    "task-new": "task-new.html",
    "task-cancel": "task-cancel.html",
    "task-completed": "task-completed.html",
    "patterns": "patterns.html",
}


@app.get("/")
async def index():
    """提供前端首页（设计系统任务列表）。"""
    return FileResponse(static_dir / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "deepseek_model": settings.deepseek_model,
    }


@app.get("/{page}")
async def static_page(page: str):
    """提供设计系统页面。"""
    filename = _PAGE_MAP.get(page)
    if filename is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": f"Page '{page}' not found"})
    return FileResponse(static_dir / filename)
