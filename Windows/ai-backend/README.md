# MedVision FastAPI 后端（Windows）

正式入口为项目根目录的 `start_system.py`，后端由统一启动器读取根目录 `.env.local` 后启动。Vue 通过开发代理访问 FastAPI 的 `/api/v1` 接口；账号、会话、病例访问控制由 FastAPI 处理。

## 启动与停止

从项目根目录、已安装依赖的 Python 环境运行：

```powershell
python start_system.py check
python start_system.py
python start_system.py status
python start_system.py stop
```

也可以运行根目录 `start.cmd` / `stop.cmd`。环境创建和本机配置说明见 [根目录 README](../README.md)。默认前端端口为 5173，后端端口为 8000，地址以启动器输出及本机配置为准。

## 后端组成

- `backend/main.py`：FastAPI 应用与 API 路由装配。
- `backend/enterprise/`：用户、权限、统一病例、审计与报告数据。
- `backend/workers/`、`backend/services/`：任务编排及 Windows 子进程管理。
- `backend/ct/`、`run_totalseg_pipeline.py`、`progress_patch_v5/`：TotalSegmentator 与 PPGL 推理、指标和三维产物。
- `backend/glioma/`：四序列 MRI 校验、nnU-Net 推理、脑肿瘤定量和可视化。
- `backend/rag/`、`migrations/`：公共知识文档提交、管理员处理、PostgreSQL/pgvector 检索与数据库结构。

共享推理代码中可能保留 `jetson_fast` 等历史参数名称，它们可能被现有调用使用，不能仅凭名称删除。

## 运行资产

病例、数据库、日志、模型和 RAG 原始文档由 `.env.local` 配置到项目外目录。主要变量包括 `PPGL_DATA_ROOT`、`PPGL_AUTH_DATABASE_URL`、`PPGL_V5_CHECKPOINT`、`PPGL_NNUNET_MODEL_DIR`、`TOTALSEG_WEIGHTS_PATH`、`PPGL_RAG_DATABASE_URL` 和 `PPGL_RAG_KNOWLEDGE_DIR`。

普通用户可提交知识文档；只有管理员可解析、生成向量、审核启用、停用和删除。文档启用后才进入检索。`PPGL_RAG_ENABLED` 控制检索，`PPGL_RAG_INDEXING_ENABLED` 控制解析和向量化。真实数据库密码只保存在本机私有配置。

原 Linux Shell 启动器、独立 Jetson 节点服务和 MiniCPM 服务已从本目录清理。当前本地大模型服务使用 Ollama。
