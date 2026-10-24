"""通用 OpenAI 兼容适配器。

用途有两个：
1. 接任何已经跑在外面的兼容端点（别人的服务、另一台机器、mock 服务）；
2. 做测试替身——冒烟测试用它跑完整链路，不需要加载真模型。

它不管理进程，因此 `managed=False`，出问题时只报「连不上」，不尝试自愈。
"""
from .base import Adapter


class OpenAIAdapter(Adapter):
    name = "openai"
    label = "任意 OpenAI 兼容端点"
    managed = False

    def status(self):
        return {
            "adapter": self.name, "label": self.label, "managed": False,
            "base": self.base, "engine_running": None,
            "model": self.cfg.get("default_model"),
        }
