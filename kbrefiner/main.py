"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from kbrefiner.api import (
    access_routes,
    auth_routes,
    deps,
    message_routes,
    routes,
    settings_routes,
)
from kbrefiner.auth import TOKEN_COOKIE, decode_token
from kbrefiner.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时恢复上次运行遗留的中断任务（processing → failed）。"""
    routes.recover_interrupted_tasks()
    yield


app = FastAPI(
    title="知序 KBRefiner",
    description="RAG 知识库预处理工具：4 阶 AI 流水线把原始文档转为结构化知识原子 + QA + 元数据 + 异常清单",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS：允许前端跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def prevent_stale_static_cache(request, call_next):
    """静态资源与 HTML 禁用强缓存。

    页面 JS 与 kbrefiner.js 同源演进，若浏览器命中旧缓存
    （如旧版 kbrefiner.js 未导出 Auth），页面会报 undefined。
    no-cache 允许缓存但每次协商校验，兼顾性能与一致性。
    """
    resp = await call_next(request)
    ctype = resp.headers.get("content-type", "")
    if request.url.path.startswith("/static") or ctype.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

# 注册 API 路由（业务 + 认证/管理后台 + 消息/黑白名单/系统设置）
app.include_router(routes.router)
app.include_router(auth_routes.router)
app.include_router(message_routes.router)
app.include_router(access_routes.router)
app.include_router(settings_routes.router)

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
    "report": "report.html",
    "patterns": "patterns.html",
}


def _page_user(request: Request) -> Optional[dict]:
    """从页面请求（Cookie）解析当前用户，失败返回 None。"""
    token = request.cookies.get(TOKEN_COOKIE, "")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    # 经 deps 模块取存储（而非导入时绑定对象），测试替换 deps._user_store 后此处同步生效
    user = deps._user_store.get(payload.get("sub", ""))
    if not user or user.get("status") != "active":
        return None
    return user


def _guard_admin_page(request: Request) -> Optional[Response]:
    """管理后台页面守卫：未登录/非管理员重定向到后台登录页。"""
    user = _page_user(request)
    if not user or user.get("role") not in ("super_admin", "admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return None


@app.get("/")
async def index(request: Request):
    """提供前端首页（设计系统任务列表）。"""
    if settings.require_login and not _page_user(request):
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(static_dir / "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "llm_model": settings.llm_model,
    }


@app.get("/login")
async def login_page():
    """前台登录页（无需鉴权）。"""
    return FileResponse(static_dir / "login.html")


@app.get("/register")
async def register_page():
    """前台注册页（无需鉴权，是否开放由后端注册开关控制）。"""
    return FileResponse(static_dir / "register.html")


# ===== 管理后台页面（/admin/login 之外的页面均要求管理员身份） =====


@app.get("/admin/login")
async def admin_login_page():
    """后台登录页（无需鉴权）。"""
    return FileResponse(static_dir / "admin-login.html")


@app.get("/admin/dashboard")
async def admin_dashboard_page(request: Request):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-dashboard.html")


@app.get("/admin/users")
async def admin_users_page(request: Request):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-users.html")


@app.get("/admin/users/new")
async def admin_user_add_page(request: Request):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-user-add.html")


@app.get("/admin/users/{user_id}")
async def admin_user_detail_page(request: Request, user_id: str):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-user-detail.html")


@app.get("/admin/messages")
async def admin_messages_page(request: Request):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-messages.html")


@app.get("/admin/access")
async def admin_access_page(request: Request):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-access.html")


@app.get("/admin/settings")
async def admin_settings_page(request: Request):
    resp = _guard_admin_page(request)
    if resp:
        return resp
    return FileResponse(static_dir / "admin-settings.html")


@app.get("/{page}")
async def static_page(request: Request, page: str):
    """提供设计系统页面（require_login 开启时未登录跳登录页）。"""
    if deps.require_login_enabled() and not _page_user(request):
        return RedirectResponse(url="/login", status_code=302)
    filename = _PAGE_MAP.get(page)
    if filename is None:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": f"Page '{page}' not found"})
    return FileResponse(static_dir / filename)
