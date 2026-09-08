# MedVision AI 医学影像工作站 · Windows 适配版

仓库名称：`medvision-ai-medical-imaging-main-windows`。

本版本基于原作者的 Linux 版 MedVision 项目进行 Windows 本地适配与功能整合。原项目提供了医学影像工作站的基础代码与界面，本版本继续完善 Windows 启停、双模型工作流、账号权限、知识文档提交与管理员处理等功能。原项目来源及授权待确认项见 [来源说明](docs/UPSTREAM.md)。

当前运行主线为 **Vue + FastAPI + 本地推理服务**。历史 Java/微信小程序不是本 Windows 发行目录的运行组成部分。项目用于科研和学习，分割、指标与 AI 文本需要人工复核。

## 功能与边界

| 功能 | 当前行为 |
| --- | --- |
| PPGL CT | CT 上传、TotalSegmentator 器官分割、ProgressPatchV5 肿瘤分割、指标与二维/三维结果；界面支持分别提交两类分割任务 |
| 脑肿瘤 MRI | FLAIR、T1、T1CE、T2 四序列校验；使用已训练 nnU-Net 模型，输出指标、Overlay 和三维模型 |
| 病例与报告 | 登录、角色权限、病例访问隔离、审计、回收站、结构化报告与医生复核 |
| AI 助手 | 通过 Ollama 提供本地聊天与病例上下文问答 |
| 医学知识库 | PostgreSQL + pgvector；所有登录用户可提交公开文档，管理员负责解析、向量化、审核启用和停用 |

下载源码不等于具备推理条件：仓库不附带医学影像、分割权重、Ollama 模型、账号或数据库。当前统一启动器要求 PPGL 权重及指定 TotalSegmentator 离线权重就绪；即便只看页面，也不能跳过这部分检查。脑肿瘤推理、Ollama 和 RAG 可分别配置。

## Windows 环境

- Windows 10/11 x64，PowerShell。
- Conda；本版环境名为 `MedVision`，Python 3.10。
- Node.js 24.x、npm 11.x；精确前端依赖由 `package-lock.json` 固定。
- GPU 推理使用支持 CUDA 的 NVIDIA 显卡及匹配驱动；环境文件安装 PyTorch 2.5.1 和 CUDA 12.1 运行时。
- Ollama：使用 AI 助手时需要。
- PostgreSQL 与 pgvector：使用医学知识库时需要；账号和病例在单机环境默认使用独立 SQLite 数据库。

依赖与复现范围见 [环境说明](docs/ENVIRONMENT.md)。

## 安装步骤

### 1. 获取 Windows 版

将下列 `YOUR_GITHUB_ACCOUNT` 换为本 Windows 版发布者的 GitHub 账号；这不是原作者仓库地址。

```powershell
git clone https://github.com/YOUR_GITHUB_ACCOUNT/medvision-ai-medical-imaging-main-windows.git
cd medvision-ai-medical-imaging-main-windows
```

也可以下载此仓库的 ZIP，解压后进入包含 `start_system.py` 的目录。

### 2. 安装依赖

```powershell
conda env create -f environment.yml
conda activate MedVision
python -m pip check
```

然后安装前端依赖：

```powershell
cd frontend-vue-prototype
npm ci
cd ..
```

`requirements.txt` 是已有 Python 环境补装应用依赖的入口，不包含 PyTorch/CUDA 安装步骤，也不是完整传递依赖锁文件。首次 GPU 安装使用 `environment.yml`，无需再次执行 `pip install -r requirements.txt`。

### 3. 只创建一次本机配置

```powershell
if (-not (Test-Path -LiteralPath .env.local)) {
    Copy-Item .env.example .env.local
}
notepad .env.local
```

配置文件使用 UTF-8。将模板中所有 `X:/MedVisionRuntime`、`X:/MedVisionModels` 替换为你实际的项目外目录，不能只改其中一行。模板是普通键值文本，不展开 `$HOME`、`$env:...` 或 Shell 命令。

| 配置 | 设置要求 |
| --- | --- |
| `PPGL_DATA_ROOT` 及各病例、日志、临时目录 | 同一个项目外运行目录；子目录应位于该根目录中 |
| `PPGL_AUTH_DATABASE_URL` | 单机使用模板中的 SQLite URL；替换示例盘符，首次创建管理员和启动时使用同一连接 |
| `PPGL_PYTHON_BIN` | 在激活的 MedVision 环境中可用 `python`；双击启动时建议填写该环境 `python.exe` 的完整路径 |
| `PPGL_NODE_BIN`、`PPGL_NPM_BIN` | PATH 已提供 Node/npm 时不用填写，否则填写实际可执行文件路径 |
| `PPGL_V5_CHECKPOINT` | PPGL `model_best.pth` 的外部路径 |
| `TOTALSEG_WEIGHTS_PATH` | TotalSegmentator 离线 `nnunet/results` 路径 |
| `PPGL_NNUNET_MODEL_DIR` | 脑肿瘤模型文件夹，是正式后端读取模型目录的配置 |
| `PPGL_GLIOMA_MODEL_DIR` | 模板保留的兼容项，填写为与上一项相同的目录，避免不一致 |
| `PPGL_GLIOMA_ENABLED` | 脑肿瘤模型准备完成后设为 `true` |

可以运行 `python -c "import sys; print(sys.executable)"` 查看当前 Python 路径。系统原有学习环境可能叫 `PPGL`，这是本机选择；新安装文档统一使用 `MedVision`。

### 4. 准备分割模型

建议结构：

```text
MedVisionModels/
├── ppgl/weights/model_best.pth
├── totalsegmentator/nnunet/results/Dataset*/...
└── brain-tumour/Dataset001_BrainTumour/
    └── nnUNetTrainer__nnUNetPlans__3d_fullres/
        ├── dataset.json
        ├── plans.json
        ├── model_manifest.json
        └── fold_0/checkpoint_best.pth
```

完整快照及来源边界见 [模型与数据说明](WEIGHTS_AND_DATA.md)。目前 PPGL 和自训练脑肿瘤权重没有随源码提供，不能把其他模型的 `.pth` 直接改名替代。启动器检查的 TotalSegmentator 数据集编号为 291、292、293、294、295、298、300，缺少时需先准备匹配权重。

当前正式脑肿瘤接口使用 `Dataset001_BrainTumour / 3d_fullres / fold 0 / checkpoint_best.pth`。病例上传序列顺序为 FLAIR、T1、T1CE、T2，对应 nnU-Net 通道 0000、0001、0002、0003；这是本项目现用模型约定，更换模型需核对其 `dataset.json`。

### 5. 创建首个管理员

配置好 `.env.local` 后，在激活的环境中执行：

```powershell
python scripts/windows/initialize_enterprise_database.py --create-admin --username admin
```

按提示输入两次至少 12 字符的密码。终端不显示密码字符。系统没有默认密码，网页也不开放管理员注册。后续账号由管理员在“用户与权限”中创建；已有用户时初始化脚本会拒绝重复创建。

### 6. 可选：启用 Ollama

先安装并启动 Ollama，按需下载当前配置使用的模型：

```powershell
ollama pull qwen2.5:3b-instruct-q4_0
```

编辑同一个 `.env.local`：

```dotenv
PPGL_ENABLE_OLLAMA=true
PPGL_OLLAMA_BASE_URL=http://127.0.0.1:11434
PPGL_OLLAMA_MODEL=qwen2.5:3b-instruct-q4_0
```

模型许可证与代码许可证分别管理，来源链接见 [模型与数据说明](WEIGHTS_AND_DATA.md)。

### 7. 可选：建立 RAG 知识库

先安装 PostgreSQL，并为对应 PostgreSQL 主版本安装 Windows 可用的 pgvector 扩展。Python 的 `pgvector` 包只是客户端依赖，不会自动安装服务器扩展。

首次建立专用数据库和角色：

```powershell
python scripts/windows/initialize_postgresql.py --database medvision --app-role medvision_app
```

此命令会要求数据库 `postgres` 管理员密码和新应用密码（至少 16 字符），不提供默认密码。数据库密码与网页登录密码不同。若角色或数据库已存在，脚本会拒绝覆盖，应该配置已有可用连接。

将专用连接填写到 `.env.local` 的 `PPGL_RAG_DATABASE_URL`，替换 `CHANGE_ME`，密码中的 URL 特殊字符需编码。账号数据库可继续使用 SQLite。

```dotenv
PPGL_RAG_DATABASE_URL=postgresql+psycopg://medvision_app:CHANGE_ME@127.0.0.1:5432/medvision
PPGL_RAG_EMBEDDING_MODEL=qwen3-embedding:0.6b
PPGL_RAG_EMBEDDING_DIMENSION=1024
PPGL_RAG_CHUNK_SIZE=700
PPGL_RAG_CHUNK_OVERLAP=100
```

```powershell
ollama pull qwen3-embedding:0.6b
python scripts/windows/initialize-rag-database.py
```

该初始化脚本读取项目根目录 `.env.local`；已设置的进程环境变量优先。初始化成功后，将 `PPGL_RAG_ENABLED` 和 `PPGL_RAG_INDEXING_ENABLED` 都设为 `true`，重启系统。

所有登录用户可从“医学知识库”右上角提交文档。管理员在“知识文档管理”执行 `解析切片 → 生成向量 → 审核启用`；未经审核启用的文档不会参与检索。切片大小和重叠单位为字符。数据库初始为空，需要准备有授权的公共文档。

### 8. 检查、启动与停止

```powershell
python start_system.py check
python start_system.py
```

也可运行 `start.cmd`。如果双击时环境找不到 Python，请在 `.env.local` 填写 `PPGL_PYTHON_BIN` 的完整路径。

默认网页地址为 `http://127.0.0.1:5173/`，后端就绪地址为 `http://127.0.0.1:8000/ready`。

```powershell
python start_system.py status
python start_system.py stop
```

也可使用 `stop.cmd`。启动器记录自己的进程；启动前已运行的 Ollama 和 PostgreSQL 会保留。服务日志位于配置的 `PPGL_LOG_DIR`。

如需受信局域网访问，可在确认网络范围后运行 `scripts/windows/configure-lan-access.ps1` 并使用 `start.cmd -Lan`。开发服务器不是公网部署方案；公网部署应单独配置静态资源服务、反向代理和 HTTPS。

## 测试与已知限制

2026-09-07 清理后，本机环境通过 11 项 Windows 启动器测试、32 项后端测试和 Vue 生产构建。发布整理后的实际验证结果随发布目录审计报告交付。

这些结果不代表从空白电脑安装已验证，也不代表模型临床性能通过验收。当前需要继续验证的项目包括：全新 Windows 环境安装、真实 PPGL CT 闭环、导入授权文档后的完整 RAG 审核和引用问答流程。Python 环境固定了直接依赖版本，但没有提供全部传递依赖的锁文件；Vue 构建仍有包体积等警告。

运行本地测试：

```powershell
python -m pytest tests -q
cd ai-backend
python -m pytest tests -q
cd ../frontend-vue-prototype
npm test
npm run build
```

## 目录与发布

```text
ai-backend/              FastAPI、分割、RAG、报告与迁移
frontend-vue-prototype/  Windows Web 界面
scripts/windows/        启停、数据库初始化、发布检查
tests/                  启动器与发布工具测试
docs/                   来源、环境与发布说明
environment.yml         Windows Conda 环境
requirements.txt        应用直接依赖
.env.example            公开配置模板
start_system.py         统一启动入口
```

发布时使用 `scripts/windows/prepare_release.py` 生成独立目录，再执行审计。不要直接压缩日常工作目录上传 GitHub。具体排除规则见 [发布说明](docs/RELEASE.md)。

本 Windows 版尚未选定项目级 `LICENSE`。原作者、合作者的署名和已有第三方许可证必须保留，不能仅凭完成 Windows 适配就声明全部源码归本版作者所有。发布地址、作者署名和授权状态见 [来源说明](docs/UPSTREAM.md)。
