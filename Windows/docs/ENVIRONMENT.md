# Windows 环境与复现范围

正式安装入口是根目录 `environment.yml`，默认 Conda 环境名为 `MedVision`。原开发电脑的环境名可能是 `PPGL`，不应把该名称或其安装盘符写成所有使用者的要求。

| 组件 | 当前声明 |
| --- | --- |
| Python | 3.10.20 |
| PyTorch / torchvision / torchaudio | 2.5.1 / 0.20.1 / 2.5.1 |
| CUDA 运行时 | pytorch-cuda 12.1 |
| nnUNetv2 | 2.6.2 |
| TotalSegmentator | 2.11.0 |
| SimpleITK | 2.5.2 |
| Node.js / npm | 24.x / 11.x |
| 前端依赖 | 使用 package-lock.json，执行 npm ci |

直接依赖与当前开发环境核对一致，`python -m pip check` 通过。此次没有创建新的 Conda 环境、下载所有依赖或验证另一台 Windows 电脑；不能把本机通过解释为全新环境安装已通过。

`environment.yml` 中 pip 段与 `requirements.txt` 固定直接依赖。PyTorch/CUDA 由 Conda 安装，`requirements.txt` 不包含 GPU 运行时。`pytest` 位于开发环境描述，不是运行应用必须安装的依赖。现有文件不是涵盖所有传递依赖、包构建号和哈希的完整环境锁。

`pgvector` Python 包与 PostgreSQL 服务端扩展是两个组件。RAG 需要事先安装 PostgreSQL 及匹配的 pgvector 扩展；应用迁移负责创建表，不负责下载数据库服务。

## 配置读取

- 统一启动器及两个应用数据库初始化脚本读取项目根目录 `.env.local`。
- 显式进程环境变量优先于 `.env.local`，便于隔离测试和部署覆盖。
- 配置模板中的 `X:` 是待替换示例，不是固定安装位置。
- `.env.local` 不执行命令，不展开 `$HOME` 等变量。
- `start.cmd` 使用 `PPGL_PYTHON_BIN` 或 PATH 中的 Python；直接命令名与绝对可执行路径均支持。
- 脑肿瘤正式模型目录读取 `PPGL_NNUNET_MODEL_DIR`；模板兼容项 `PPGL_GLIOMA_MODEL_DIR` 应保持相同。

当前启动器仍要求 PPGL/TotalSegmentator 权重通过检查。无权重的纯 UI 演示模式尚未实现；需要单独工作，不应在 README 中承诺“装完依赖立即完整运行”。
