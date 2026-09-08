from __future__ import annotations

import torch
import torch.nn as nn
from monai.networks.nets import UNet


class ProgressPatchV5UNet(nn.Module):
    """Inference-only architecture matching the selected V5 checkpoint."""

    def __init__(self, out_channels: int = 11):
        super().__init__()
        self.unet = UNet(
            spatial_dims=3,
            in_channels=1,
            out_channels=out_channels,
            channels=(32, 64, 128, 256, 320, 320),
            strides=(2, 2, 2, 2, 2),
            num_res_units=2,
            norm="instance",
            act="leakyrelu",
        )

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        return self.unet(image)


def build_model(out_channels: int = 11) -> nn.Module:
    return ProgressPatchV5UNet(out_channels=out_channels)
