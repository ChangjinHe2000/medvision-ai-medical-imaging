# MedVision Vue 前端说明

## 1. 正式业务范围

本目录是项目唯一正式 Web 前端，基于 Vue 3、Vite 和 Element Plus，统一接入 FastAPI `/api/v1`。当前界面同时承载：

- PPGL CT：CT 上传、TotalSegmentator 全器官分割、ProgressPatchV5 肿瘤分割、指标及二维/三维阅片；
- 脑肿瘤 MRI：FLAIR、T1、T1CE、T2 四序列上传、nnU-Net 分割、ED/NET/ET/TC/WT 指标及二维/三维阅片；
- 企业功能：Cookie 登录、角色菜单、用户管理、审计、回收站、站内私聊、AI 助手和医学知识库；
- 报告复核：读取已有辅助报告并保存医生、日期和复核意见。

`frontend-javaweb` 不参与正式启动，浏览器也不会直接访问模型、权重或病例目录。

## 2. 环境要求

- Windows 10/11；
- Node.js 24.x；
- npm 11.x；
- FastAPI 运行在默认的 `127.0.0.1:8000`。

PowerShell 如果禁止执行 `npm.ps1`，使用 `npm.cmd` 即可，无需修改系统执行策略。

## 3. 配置

统一启动时使用项目根目录 `.env.local`，无需另建前端配置。单独执行 `npm run dev` 时，才在本目录创建只包含公开前端参数的 `.env.local`：

```text
VITE_API_BASE_URL=/api
VITE_API_PROXY_TARGET=http://127.0.0.1:8000
VITE_CSRF_COOKIE_NAME=medvision_csrf
```

浏览器始终请求同源 `/api`，Vite 再代理到 FastAPI。这样可以正常携带 HttpOnly 会话 Cookie 和 CSRF Cookie。

不要把后端地址写死到 Vue 页面，也不要把密码、Token、患者信息、数据路径或权重路径写入 `.env.local`。

## 4. 安装、测试和启动

```powershell
cd <项目目录>\frontend-vue-prototype
npm.cmd ci
npm.cmd test
npm.cmd run build
npm.cmd run dev
```

浏览器访问 `http://127.0.0.1:5173`。启动前端并不会自动启动 FastAPI、Ollama 或推理 Worker。

## 5. 初始管理员

网页不提供公开管理员注册。全新数据库需由部署负责人在项目根目录运行：

```powershell
conda activate MedVision
python scripts\windows\initialize_enterprise_database.py --create-admin
```

脚本默认管理员用户名为 `admin`，可用 `--username` 修改；密码必须通过终端设置，项目没有默认密码。正式环境保持：

```text
PPGL_ALLOW_WEB_ADMIN_SETUP=false
```

## 6. 登录、会话与权限

- 登录、恢复会话和退出分别调用 `/api/v1/auth/login`、`/api/v1/auth/me`、`/api/v1/auth/logout`；
- 修改请求自动发送 `X-CSRF-Token`；
- 401 会统一返回登录页，403 由页面显示中文错误；
- 管理员可见用户、审计和回收站菜单；
- 医生可创建、上传、提交任务和复核；
- 只读用户只能查看获授权病例，不能通过地址栏进入上传页；
- AI 对话按账号保存在浏览器本地，刷新或重新登录仍保留，只有该账号主动退出登录时清除。

浏览器保存的 AI 对话不得包含患者身份信息。

## 7. 主要路由

| 路径 | 功能 |
|---|---|
| `/login` | 登录 |
| `/dashboard` | CT/MRI 真实业务总览 |
| `/cases` | 统一病例列表、重命名、软删除和任务入口 |
| `/upload` | PPGL CT 上传 |
| `/glioma/upload` | 脑肿瘤四序列 MRI 上传 |
| `/cases/:caseId` | CT 分割结果 |
| `/glioma/cases/:caseId` | MRI 分割、指标和二维/三维结果 |
| `/cases/:caseId/3d` | CT 二维/三维联合阅片 |
| `/cases/:caseId/report` | 报告与医生复核 |
| `/assistant` | 流式 AI 医学助手 |
| `/knowledge` | 医学知识库 |
| `/messages` | 账号私聊 |
| `/admin/users` | 用户管理，仅管理员 |
| `/admin/audit` | 审计查询与导出，仅管理员 |
| `/admin/trash` | 病例恢复和彻底删除，仅管理员 |
| `/admin/knowledge` | 文档解析、向量化与审核，仅管理员 |

所有已登录用户都能在 `/knowledge` 页面提交公开文档，只有管理员能执行后续处理。

## 8. 关键目录

```text
src/
├── api/          # /api/v1 请求适配、Cookie/CSRF 和错误处理
├── config/       # 浏览器运行配置
├── layout/       # MedVision 主布局和角色菜单
├── router/       # 服务端会话路由守卫
├── store/        # 账号会话和账号级 AI 对话
├── views/        # CT、MRI、企业功能和报告页面
└── components/   # 二维/三维和首页组件
tests/            # Node 内置测试运行的前端契约测试
```

## 9. 当前验收状态

- `npm.cmd test`：验证 FastAPI 8000、Cookie/CSRF、正式 API 前缀、角色路由和退出清理逻辑；
- `npm.cmd run build`：验证生产构建；
- 代码中不再调用 Spring Boot 8080、`/api/ai`、`/api/brain` 或 `demo-case`；
- 是否达到“系统首次可用”，仍以真实 CT 与 MRI 各完成一次上传、推理、指标、二维和三维查看为准。前端构建通过不等于 GPU 推理验收完成。

## 10. 常见问题

1. `npm.ps1` 被系统禁止：改用 `npm.cmd`。
2. 登录页提示尚未创建管理员：运行命令行初始化脚本，不要开启网页注册。
3. 页面接口 401：会话已失效，重新登录。
4. 页面接口 403：账号角色或病例授权不足。
5. 页面接口 404：对应分割、指标、报告或可视化文件尚未生成。
6. 页面无法连接后端：确认 FastAPI 位于 8000，且 `.env.local` 的代理地址正确。
7. 其他电脑访问：Vite 可监听 `0.0.0.0:5173`，但仍需 Windows 防火墙和路由器允许访问；不要直接对公网暴露开发服务器。
