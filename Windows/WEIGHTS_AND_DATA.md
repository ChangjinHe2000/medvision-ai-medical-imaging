# 权重与数据说明

本提交目录只包含源码和可公开说明文件，不包含比赛/临床相关私有影像，也不包含模型权重。

## 需要外部提供的资产

如需完整运行 AI 推理，需要准备：

- PPGL ProgressPatchV5 分割权重，通过 `PPGL_V5_CHECKPOINT` 指向外部 `ppgl/weights/model_best.pth`。
- 已纳入仓库的 PPGL 分割推理代码位于 `ai-backend/progress_patch_v5/`。
- TotalSegmentator 离线权重，通过 `TOTALSEG_WEIGHTS_PATH` 指向外部 `totalsegmentator/nnunet/results`。
- 用户训练的脑肿瘤 nnU-Net 稳定快照，通过 `PPGL_NNUNET_MODEL_DIR` 指向外部模型目录。
- 分别用于 PPGL CT 和四序列脑 MRI 的已授权、已脱敏测试影像。

这些文件不应直接提交到公开代码仓库。

## 为什么不上传

- 医学影像和模型权重属于隐私/敏感数据。
- 运行输出中可能包含病例 ID、影像空间信息或诊断相关结果，不适合作为公开代码的一部分。

## 推荐外部目录

```text
MedVisionModels/
├─ MODEL_SHA256SUMS.txt
├─ ppgl/weights/model_best.pth
├─ totalsegmentator/nnunet/results/Dataset*/...
└─ brain-tumour/Dataset001_BrainTumour/
   └─ nnUNetTrainer__nnUNetPlans__3d_fullres/
      ├─ dataset.json
      ├─ plans.json
      ├─ dataset_fingerprint.json
      ├─ model_manifest.json
      └─ fold_0/checkpoint_best.pth
```

病例、运行数据库、日志、临时文件和 RAG 文档应放在另一个外部运行目录，例如 `X:/MedVisionRuntime`，不能与模型目录混放。

## 安全规则

- `MODEL_SHA256SUMS.txt` 只登记模型和模型配置，主动排除 NIfTI 与 macOS 元数据。
- 未确认来源、授权和脱敏状态的 NIfTI 文件不得读取、推理、复制或公开。
- 模型许可证和允许的分发范围必须在公开发布前单独确认。
- 正式配置不得指向仍在变化的 nnU-Net 训练结果目录。

## 许可证与来源边界

- nnU-Net 源码使用 Apache-2.0，详见 <https://github.com/MIC-DKFZ/nnUNet>。
- TotalSegmentator 源码使用 Apache-2.0，但部分任务需要额外许可；当前 `total` 任务不在受限任务列表中，`brain_structures` 等任务不能沿用这一判断。发布前应按实际任务再次核对 <https://github.com/wasserth/TotalSegmentator>。
- 当前脑 MRI 学习数据来自 Medical Segmentation Decathlon `Task01_BrainTumour`，数据许可为 CC BY-SA 4.0；数据不得随源码包发布，使用时仍需保留归属和许可说明，详见 <https://medicaldecathlon.com/>。
- 当前 Ollama 使用的 `Qwen2.5-3B-Instruct` 模型卡标注为 Qwen Research License，包含非商业限制；企业部署应取得适用授权或替换为满足目标用途的模型，详见 <https://huggingface.co/Qwen/Qwen2.5-3B-Instruct>。
- ProgressPatchV5 代码、PPGL 权重、用户训练的脑肿瘤权重以及合作代码尚缺项目级再分发确认。本仓库没有提供它们的公开授权结论。
- MedVision 项目自身尚未添加 `LICENSE`。在作者与合作者共同确定版权归属和许可证前，不应宣称项目已完成正式开源。
