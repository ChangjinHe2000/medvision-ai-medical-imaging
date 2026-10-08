# 功能展示素材

本目录用于 GitHub README 的功能介绍，不包含患者数据库、真实 CT / 胸片图像、模型权重或私有运行配置。

## 配图

三张图为原创示意图，均提供 1600×900 的 PNG 和可编辑 SVG：

| 图 | PNG | SVG |
| --- | --- | --- |
| 患者中心功能总览 | [feature-map.png](assets/feature-map.png) | [feature-map.svg](assets/feature-map.svg) |
| 核心架构与数据流 | [architecture.png](assets/architecture.png) | [architecture.svg](assets/architecture.svg) |
| 数据覆盖与工程验证 | [validation.png](assets/validation.png) | [validation.svg](assets/validation.svg) |

架构箭头表示主要数据流，省略具体请求往返及部分内部组件。验证图的指标来自 2026-10-05 本地交付记录，条件见 [VERIFICATION.md](VERIFICATION.md)。

## 实际界面截图

| 页面 | 原图 |
| --- | --- |
| 患者概览 | [patient-overview.png](assets/screenshots/patient-overview.png) |
| 临床时间线 | [clinical-timeline.png](assets/screenshots/clinical-timeline.png) |
| 影像与 AI | [imaging-ai.png](assets/screenshots/imaging-ai.png) |
| 患者助手答复 | [copilot-answer.png](assets/screenshots/copilot-answer.png) |
| 来源与证据 | [copilot-evidence.png](assets/screenshots/copilot-evidence.png) |
| 执行记录 | [ai-trace.png](assets/screenshots/ai-trace.png) |

这些图片来自既有产品验收中的正常登录和实际操作，患者为虚构的 `DEMO-P001`。原始视口 1920×1080，保持原始截图字节，没有改写患者数据、模型结果或界面文字；SHA-256 与来源在 [asset-manifest.json](asset-manifest.json)。

截图对应较早的合成演示会话。它们说明界面功能，不代表当前真实影像、患者规模、全部新版页面或医学效果。当前真实 CT / 胸片的验证情况以单独工程摘要为准。

## 复用与署名

本目录的原创配图与原创说明采用 Apache-2.0，条款见 [LICENSE](LICENSE)。这份素材许可仅适用于有权授权的本目录原创内容，不对整个仓库代码、第三方界面、模型或数据作统一许可声明。

截图中的 Vue、Element Plus 和 OHIF 等第三方界面继续保留各自许可；OHIF 为 MIT，不主张这些界面为作者从零实现。真实数据和模型均不随这些素材分发。

个人网站可引用同一套配图和截图，保留截图为合成演示以及指标为工程验证的说明。图片公开不等于发布可运行的临床服务。
