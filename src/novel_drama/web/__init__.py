"""展示层：本地 Web 工作台的 HTTP 接口。"""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
