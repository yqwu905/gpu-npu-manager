"""托管前端页面：web/ 构建出的静态文件挂在 /ui 下，访问根路径时跳转过去。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles


def mount_web(app: FastAPI, web_dir: str) -> bool:
    """web_dir 里有 index.html 时才挂载；还没构建前端时只提供接口。"""
    path = Path(web_dir)
    if not (path / "index.html").is_file():
        return False
    app.mount("/ui", StaticFiles(directory=path, html=True), name="web")

    @app.get("/", include_in_schema=False)
    def _index():
        return RedirectResponse("/ui/")

    # 浏览器默认请求 /favicon.ico（如接口文档页），指向前端的图标
    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon():
        return RedirectResponse("/ui/favicon.svg")

    return True
