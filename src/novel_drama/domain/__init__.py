"""领域层。

约定：本包内只放实体、值对象、枚举、领域规则与领域异常，**不依赖**项目内其他层
（application / adapters / interface / infrastructure），也不做任何文件与网络 I/O。
分层守卫见 `scripts/check_layering.py`，设计见 `docs/architecture-domain-model.md`。
"""
