# Windows 版发布准备

仓库名：`medvision-ai-medical-imaging-main-windows`。GitHub 账号尚待填写。来源与许可证待办见 [UPSTREAM.md](UPSTREAM.md)。

从已激活的 Python 环境运行：

```powershell
python scripts/windows/prepare_release.py --output ../release-output/medvision-ai-medical-imaging-main-windows
```

输出目录必须是项目外的新目录。工具不会覆盖现有目录，不修改本机 `.env.local`，不建立 Git 仓库，也不推送 GitHub。它按明确的源码范围复制，并扫描生成的发布目录；扫描不通过时以非零状态退出，输出目录仅供修正检查，不能直接发布。

发行目录包含：根目录 README、环境模板、依赖配置、启停入口、Vue 源码及锁文件、FastAPI 源码、迁移、Windows 脚本、测试及选定的来源/环境/发布说明。存在的根级 LICENSE、NOTICE 或 COPYING 会一并保留。

发行目录排除：

- `.env.local` 等真实配置、证书、数据库凭据。
- 数据集、病例、模型、推理结果、数据库、日志、临时文件与备份。
- `node_modules`、`dist`、`target`、Python 缓存、Git 历史。
- 历史 `frontend-javaweb` 与小程序。
- 旧架构封面、历史迁移/清理记录、旧知识库评估结果。

源码中的权限与隐私单元测试包含明确构造的虚拟数据，应与真实病例区分。自动扫描使用规则匹配，不能证明所有文字、图片、第三方授权或 Git 历史均无问题。发布工具从文件生成新目录，未审查任何远端历史。

发布目录会生成不含秘密值的 `PUBLICATION_CHECK.json`，记录自动扫描结果、文件数、大小和仍需人工确认的署名/许可证事项。自动扫描通过不等于项目许可证已确认。

本次已从本地历史源码删除记录旧账号/路径的 `boot.txt`，并将小程序 AppID 替换为占位、清除登录页演示账号信息。当前数据库、当前账号密码及本机配置保持原样。

上传前：填入 Windows 版 GitHub 账号，确认上游署名和授权，检查新目录确实不含私有资产。只将新生成的 Windows 发行目录作为仓库根目录；不要上传其上级工作空间、清理备份或本机运行目录。
