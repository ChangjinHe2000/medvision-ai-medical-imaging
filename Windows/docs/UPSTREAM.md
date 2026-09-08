# 项目来源与 Windows 版说明

- Windows 版仓库名称：`medvision-ai-medical-imaging-main-windows`。
- Windows 版 GitHub 账号及维护者署名：发布前填写。
- 基础项目：原作者的 Linux 版 MedVision / PPGL 医学影像项目。
- 原项目 README 记录的仓库地址：https://github.com/ChangjinHe2000/medvision-ai-medical-imaging
- 本次迁移前交流中使用的原项目地址：https://github.com/ChangjinHe2000/ppgl-assist-ai-medical-imaging

这些地址依据随项目提供的说明和合作记录保留，本次发布整理没有重新核实仓库重命名情况。发布者应确认最终上游链接，并补充原作者期望使用的署名及对应上游提交或版本。本地接收的目录未包含 Git 元数据，无法从该目录还原精确上游 commit。

## 版本关系

原作者版本以 Linux 为运行环境；本版本是在其代码基础上完成的 Windows 本地适配与功能整合。前端界面、业务代码和推理组件包含原项目成果。Windows 统一启停、环境配置、双模型接入、权限与审计、知识库提交和管理员处理等部分在当前合作过程中进行了修改。

尚未逐文件完成作者归属梳理，上述说明不将任何无法确认的代码归为 Windows 版原创。最终贡献边界应以作者确认和历史记录为准。

## 授权待办

当前目录没有项目级 LICENSE。发布整理不会自动补写 MIT、Apache-2.0 等许可证，也不会删除原作者或第三方的版权声明。

发布者需要与原作者/合作者确认代码再分发授权、项目许可证及署名方式，再添加双方认可的 LICENSE。模型、医学数据、第三方依赖和文档的许可分别处理，不能用一个项目 LICENSE 替代。模型来源说明见 [WEIGHTS_AND_DATA.md](../WEIGHTS_AND_DATA.md)。

## 发布目录边界

Windows 发行目录保留正式 Vue + FastAPI 工作流。历史 Java/小程序、Linux 迁移过程记录、本机配置和运行资产不纳入该发行目录；这不改变相关原始代码的作者归属。
