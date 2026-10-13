# -*- coding: utf-8 -*-
"""
model_adapters.py —— 出厂预置模型适配器（仅默认配置，非架构绑定）

作者：天咏（明月照今日，风摇过两川。）

包含四类适配器：
- VitBird525Adapter：525 类鸟类 ViT（transformers 本地离线加载）——物种识别默认
- ResNet50_200Adapter：200 类 ResNet50 checkpoint——项目自训模型
- OwlViTAdapter：开放词汇零样本检测——部位定位默认
- TrainedFrcnnAdapter：管线 B 产物 Faster R-CNN——训练后的升级项

更换模型：继承抽象基类写新适配器 → config 注册表登记 → 改激活项，核心链路零修改。
"""

import os
from pathlib import Path
from typing import List, Optional, Tuple

import torch
from PIL import Image

import config
from model_backends import (
    Box,
    ModelLoadError,
    PartDetectorBackend,
    SpeciesBackend,
    SpeciesPrediction,
    union_boxes,
)


def get_device() -> torch.device:
    """优先使用 GPU，不可用时回退 CPU。"""
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _owlvit_post_process(processor, outputs, threshold, target_sizes):
    """OWL-ViT 检测后处理，兼容不同 transformers 版本方法名。

    各版本方法名不同：4.44 为 post_process_object_detection，
    更早版本为 post_process，更新版本为 post_process_grounded_object_detection；
    依次尝试，全部缺失时抛中文错误。
    """
    candidates = ("post_process_object_detection", "post_process",
                  "post_process_grounded_object_detection")
    for name in candidates:
        fn = getattr(processor, name, None)
        if fn is None:
            continue
        try:
            return fn(outputs=outputs, threshold=threshold, target_sizes=target_sizes)
        except TypeError:
            continue
    raise ModelLoadError(
        "当前 transformers 版本缺少 OWL-ViT 后处理方法，请升级 transformers。")


def species_en_to_cn(label_en: str) -> str:
    """英文物种名 → 中文名；未收录时回退为英文原名。"""
    key = label_en.strip().upper()
    if key in config.SPECIES_CN_NAMES:
        return config.SPECIES_CN_NAMES[key]
    # 首字母大写形式再查一次（兼容不同大小写风格）
    titled = label_en.strip().title()
    if titled.upper() in config.SPECIES_CN_NAMES:
        return config.SPECIES_CN_NAMES[titled.upper()]
    return label_en


def _load_torch_checkpoint(path: Path) -> dict:
    """加载训练 checkpoint，兼容裸 state_dict 与常见包装字段。

    包装键兼容 state_dict / model_state_dict / model_state（管线 B 检测器
    产物的键名）/ model / weights。
    """
    try:
        ckpt = torch.load(str(path), map_location="cpu", weights_only=True)
    except Exception:
        # 旧版 torch 保存格式（含非张量对象）回退
        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
    if isinstance(ckpt, dict):
        for key in ("state_dict", "model_state_dict", "model_state",
                    "model", "weights"):
            if key in ckpt and isinstance(ckpt[key], dict):
                return ckpt[key]
    return ckpt


def resolve_model_path(model_id: str) -> str:
    """按 config 指定的下载源解析模型路径。

    优先魔搭 ModelScope（国内直连），失败回退 HF 镜像；
    已有本地缓存时 transformers 自动复用，不重复下载。
    """
    if config.MODEL_DOWNLOAD_SOURCE == "modelscope":
        try:
            from modelscope import snapshot_download

            return snapshot_download(model_id)
        except Exception:
            pass  # 魔搭无该镜像或未安装，回落 HF
    os.environ.setdefault("HF_ENDPOINT", config.HF_MIRROR_ENDPOINT)
    return model_id


class VitBird525Adapter(SpeciesBackend):
    """525 类鸟类 ViT（本地目录，transformers 加载，全程离线）。"""

    def __init__(self, model_dir: Optional[str] = None, **kwargs):
        super().__init__()
        self.model_dir = Path(model_dir) if model_dir else config.VIT_BIRD_525_DIR
        self._model = None
        self._processor = None
        self._id2label = {}

    def is_available(self) -> bool:
        if not self.model_dir.is_dir():
            return False
        has_config = (self.model_dir / "config.json").exists()
        has_weights = (self.model_dir / "model.safetensors").exists() or \
            list(self.model_dir.glob("*.bin"))
        return has_config and has_weights

    def load(self) -> None:
        if not self.is_available():
            raise ModelLoadError(
                f"525 类鸟类 ViT 模型文件缺失：{self.model_dir}"
                "（请检查 config.VIT_BIRD_525_DIR 路径）")
        try:
            from transformers import AutoImageProcessor, ViTForImageClassification

            self._processor = AutoImageProcessor.from_pretrained(
                str(self.model_dir), local_files_only=True)
            self._model = ViTForImageClassification.from_pretrained(
                str(self.model_dir), local_files_only=True)
            self._model.eval().to(get_device())
            self._id2label = self._model.config.id2label or {}
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(f"525 类鸟类 ViT 加载失败：{exc}")
        self._loaded = True

    @torch.no_grad()
    def predict(self, pil_image: Image.Image, top_k: int = 3) -> List[SpeciesPrediction]:
        if not self._loaded:
            self.load()
        inputs = self._processor(images=pil_image.convert("RGB"), return_tensors="pt")
        inputs = {k: v.to(get_device()) for k, v in inputs.items()}
        logits = self._model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)[0]
        k = min(top_k, probs.shape[0])
        values, indices = torch.topk(probs, k)
        results = []
        for v, i in zip(values.tolist(), indices.tolist()):
            label_en = str(self._id2label.get(i, f"CLASS_{i}"))
            results.append(SpeciesPrediction(
                label_en=label_en,
                label_cn=species_en_to_cn(label_en),
                confidence=round(float(v), 4),
            ))
        return results


class ResNet50_200Adapter(SpeciesBackend):
    """200 类 ResNet50（torchvision 结构重建 + checkpoint 加载）。

    权重路径（按优先级）：注册表 params 的 weights_path（共享模型库条目
    即走此通道）> 旧参数 checkpoint_path > config.RESNET200_CHECKPOINT。

    类别名来源（按优先级）：显式参数 class_names > 显式 labels_path（共享
    模型库 labels.json，格式 {类名: 序号}，加载时反转为 序号→类名，类别名
    直接为中文名）> checkpoint 同目录映射 json > 数据集 train 目录文件夹名
    （中文名直接可用）。
    """

    def __init__(self, checkpoint_path: Optional[str] = None,
                 weights_path: Optional[str] = None,
                 labels_path: Optional[str] = None,
                 class_names: Optional[List[str]] = None, **kwargs):
        super().__init__()
        # 权重路径：weights_path（注册表 params 新键，实例级覆盖）优先于
        # 旧参数 checkpoint_path；均未传时保持既有行为（读 config 常量）
        resolved = weights_path or checkpoint_path
        self.checkpoint_path = Path(resolved) if resolved \
            else config.RESNET200_CHECKPOINT
        self._labels_path = Path(labels_path) if labels_path else None
        self._class_names = list(class_names) if class_names else None
        self._model = None

    def is_available(self) -> bool:
        return self.checkpoint_path.is_file()

    @staticmethod
    def _load_labels_json(json_path: Path,
                          num_classes: int) -> Optional[List[str]]:
        """读取 {类名: 序号} 格式的 labels.json，反转为按序号排列的类名表。

        兼容 model_trainer 产物：labels.json 记录 类名→序号，此处反转为
        序号→类名（中文名）；序号缺失、越界或格式异常时返回 None（交由
        上层回退到其他类别名来源，保证不崩溃）。
        """
        try:
            import json
            mapping = json.loads(Path(json_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(mapping, dict) or not mapping:
            return None
        names: List[Optional[str]] = [None] * num_classes
        for label, idx in mapping.items():
            try:
                pos = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= pos < num_classes:
                names[pos] = str(label)
        if any(name is None for name in names):
            return None  # 存在未覆盖的序号，视为无效映射
        return names

    def _resolve_class_names(self, num_classes: int) -> List[str]:
        """解析类别名列表；解析失败时用 类别_i 占位，保证不崩溃。"""
        if self._class_names and len(self._class_names) == num_classes:
            return self._class_names
        # 显式 labels_path（共享模型库产物：{类名: 序号}，需反转为 序号→类名）
        if self._labels_path is not None:
            names = self._load_labels_json(self._labels_path, num_classes)
            if names:
                self._class_names = names
                return names
        # 同级映射 json（trainer_species 产物 class_to_idx.json 的反向表）
        for json_name in ("class_names.json", "classes.json"):
            json_path = self.checkpoint_path.parent / json_name
            if json_path.is_file():
                try:
                    import json
                    names = json.loads(json_path.read_text(encoding="utf-8"))
                    if isinstance(names, list) and len(names) == num_classes:
                        self._class_names = [str(n) for n in names]
                        return self._class_names
                except Exception:
                    pass
        # 同级 labels.json（model_trainer 产物兜底：字典格式 {类名: 序号}）
        sibling_labels = self.checkpoint_path.parent / "labels.json"
        if self._labels_path != sibling_labels and sibling_labels.is_file():
            names = self._load_labels_json(sibling_labels, num_classes)
            if names:
                self._class_names = names
                return names
        # 数据集 train 目录名（中文文件夹名，按 ImageFolder 排序规则）
        train_dir = config.SPECIES_DATASET_DIR / "train"
        if train_dir.is_dir():
            names = sorted([p.name for p in train_dir.iterdir() if p.is_dir()])
            if len(names) == num_classes:
                self._class_names = names
                return self._class_names
        return [f"类别_{i}" for i in range(num_classes)]

    def load(self) -> None:
        if not self.is_available():
            raise ModelLoadError(
                f"200 类 ResNet50 权重文件缺失：{self.checkpoint_path}"
                "（请先运行 train-species 训练，或检查 config.RESNET200_CHECKPOINT）")
        try:
            from torchvision import models

            state = _load_torch_checkpoint(self.checkpoint_path)
            # 从 fc 权重推断类别数（与 labels.json 条目数一致，即 len(labels)）
            fc_key = next((k for k in state if k.endswith("fc.weight")), None)
            if fc_key is None:
                raise ModelLoadError("checkpoint 中未找到 fc 权重，无法推断类别数")
            num_classes = int(state[fc_key].shape[0])
            self._model = models.resnet50(weights=None)
            in_features = self._model.fc.in_features
            self._model.fc = torch.nn.Linear(in_features, num_classes)
            # strict=True（默认）：键与形状必须完全匹配 model_trainer 产物
            self._model.load_state_dict(state, strict=True)
            self._model.eval().to(get_device())
            self._class_names = self._resolve_class_names(num_classes)
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(f"200 类 ResNet50 加载失败：{exc}")
        self._loaded = True

    @torch.no_grad()
    def predict(self, pil_image: Image.Image, top_k: int = 3) -> List[SpeciesPrediction]:
        if not self._loaded:
            self.load()
        from torchvision import transforms

        preprocess = transforms.Compose([
            transforms.Resize((config.TRAIN_SPECIES["image_size"],
                               config.TRAIN_SPECIES["image_size"])),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        ])
        tensor = preprocess(pil_image.convert("RGB")).unsqueeze(0).to(get_device())
        logits = self._model(tensor)
        probs = torch.softmax(logits, dim=-1)[0]
        k = min(top_k, probs.shape[0])
        values, indices = torch.topk(probs, k)
        results = []
        for v, i in zip(values.tolist(), indices.tolist()):
            name = self._class_names[i] if i < len(self._class_names) else f"类别_{i}"
            # 文件夹名形如 中文名（英文名：X）→ 拆分中英文
            label_cn, label_en = name, name
            if "（" in name and "）" in name:
                label_cn = name.split("（")[0]
                label_en = name.split("英文名：")[-1].rstrip("）") \
                    if "英文名：" in name else name
            results.append(SpeciesPrediction(
                label_en=label_en, label_cn=label_cn, confidence=round(float(v), 4)))
        return results


class VitHFAdapter(SpeciesBackend):
    """通用 HuggingFace ViT 图像分类适配器（本地目录，离线加载）。

    兼容两种权重形态：
      ① 标准 HuggingFace 目录（config.json + model.safetensors）——直接
         from_pretrained 加载；
      ② 本机训练产物目录（config.json + best_model.pt/last_model.pt，权重为
         ViTForImageClassification.state_dict）——按 config 重建网络后载入，
         按当前分类头形状过滤形状不匹配的键（避免类别数不同导致 RuntimeError）。

    构造参数 model_dir 必填（由注册表 params 传入）；labels_path 可选
    （共享库 labels.json，{类名: 序号}，用于给出可读类别名）。
    """

    def __init__(self, model_dir: Optional[str] = None,
                 labels_path: Optional[str] = None,
                 class_names: Optional[List[str]] = None, **kwargs):
        super().__init__()
        self.model_dir = Path(model_dir) if model_dir else None
        self._labels_path = Path(labels_path) if labels_path else None
        self._class_names = list(class_names) if class_names else None
        self._model = None
        self._processor = None
        self._names: List[str] = []

    def is_available(self) -> bool:
        if self.model_dir is None or not self.model_dir.is_dir():
            return False
        if not (self.model_dir / "config.json").is_file():
            return False  # 无 config 无法重建 HF ViT 结构
        return (self.model_dir / "model.safetensors").is_file() \
            or bool(list(self.model_dir.glob("*.bin"))) \
            or (self.model_dir / "best_model.pt").is_file() \
            or (self.model_dir / "last_model.pt").is_file()

    def _resolve_pt_weight(self) -> Optional[Path]:
        """定位本机训练产物权重文件（best_model.pt 优先，退 last_model.pt）。"""
        for name in ("best_model.pt", "last_model.pt"):
            candidate = self.model_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _names_from_labels(self, num_classes: int) -> List[str]:
        """按 labels.json（{类名: 序号}）反转为 序号→类名 列表；失败返回空表。"""
        if self._class_names and len(self._class_names) == num_classes:
            return self._class_names
        candidates = []
        if self._labels_path is not None:
            candidates.append(self._labels_path)
        candidates.append(self.model_dir / "labels.json")
        for path in candidates:
            try:
                import json
                mapping = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(mapping, dict) or not mapping:
                continue
            names: List[Optional[str]] = [None] * num_classes
            for label, idx in mapping.items():
                try:
                    pos = int(idx)
                except (TypeError, ValueError):
                    continue
                if 0 <= pos < num_classes:
                    names[pos] = str(label)
            if all(n is not None for n in names):
                return [str(n) for n in names]
        return []

    def load(self) -> None:
        if not self.is_available():
            raise ModelLoadError(
                f"HuggingFace ViT 模型文件缺失：{self.model_dir}"
                "（需含 config.json 与 model.safetensors / best_model.pt）")
        try:
            from transformers import (
                AutoConfig,
                AutoImageProcessor,
                ViTForImageClassification,
            )

            cfg = AutoConfig.from_pretrained(str(self.model_dir),
                                             local_files_only=True)
            # 处理器：优先目录自带 preprocessor_config.json，缺失时用默认 ViT 处理器
            try:
                self._processor = AutoImageProcessor.from_pretrained(
                    str(self.model_dir), local_files_only=True)
            except Exception:
                from transformers import ViTImageProcessor
                self._processor = ViTImageProcessor()

            hf_weights = self.model_dir / "model.safetensors"
            has_bin = bool(list(self.model_dir.glob("*.bin")))
            if hf_weights.is_file() or has_bin:
                self._model = ViTForImageClassification.from_pretrained(
                    str(self.model_dir), local_files_only=True)
                num_classes = int(getattr(self._model.config, "num_labels", 0)) or None
            else:
                # 本机训练产物：按 config 重建网络，再按形状过滤后载入权重
                weight_path = self._resolve_pt_weight()
                if weight_path is None:
                    raise ModelLoadError(f"未找到可用权重文件：{self.model_dir}")
                state = _load_torch_checkpoint(weight_path)
                cls_key = next((k for k in state if k.endswith("classifier.weight")), None)
                if cls_key is None:
                    raise ModelLoadError("checkpoint 中未找到 classifier 权重，无法推断类别数")
                num_classes = int(state[cls_key].shape[0])
                cfg.num_labels = num_classes
                self._model = ViTForImageClassification(cfg)
                reference = self._model.state_dict()
                filtered = {k: v for k, v in state.items()
                            if k in reference and tuple(v.shape) == tuple(reference[k].shape)}
                self._model.load_state_dict(filtered, strict=False)

            self._model.eval().to(get_device())
            # 类别名：labels.json 优先，其次模型 config 的 id2label，最后占位
            self._names = []
            if num_classes:
                self._names = self._names_from_labels(num_classes)
            if not self._names:
                id2label = dict(getattr(self._model.config, "id2label", {}) or {})
                if id2label:
                    total = max(int(k) for k in id2label) + 1
                    self._names = [str(id2label.get(i, f"CLASS_{i}")) for i in range(total)]
            if not self._names and num_classes:
                self._names = [f"类别_{i}" for i in range(num_classes)]
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(f"HuggingFace ViT 加载失败：{exc}")
        self._loaded = True

    @torch.no_grad()
    def predict(self, pil_image: Image.Image, top_k: int = 3) -> List[SpeciesPrediction]:
        if not self._loaded:
            self.load()
        inputs = self._processor(images=pil_image.convert("RGB"), return_tensors="pt")
        inputs = {k: v.to(get_device()) for k, v in inputs.items()}
        logits = self._model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)[0]
        k = min(top_k, probs.shape[0])
        values, indices = torch.topk(probs, k)
        results = []
        for v, i in zip(values.tolist(), indices.tolist()):
            name = self._names[i] if i < len(self._names) else f"CLASS_{i}"
            # 内置鸟类对照表命中时给出中文名，否则中英文同名显示
            results.append(SpeciesPrediction(
                label_en=name,
                label_cn=species_en_to_cn(name),
                confidence=round(float(v), 4),
            ))
        return results


class ResNeSt101Adapter(SpeciesBackend):
    """ResNeSt101 分类适配器（timm 结构重建 + checkpoint 加载，软依赖）。

    权重路径：注册表 params 的 weights_path（共享模型库条目走此通道）；
    支持 DataParallel 保存的权重（自动去除 module. 前缀）。
    类别名来源：显式 labels_path（共享库 labels.json，{类名: 序号}）→
    权重同目录 labels.json → 兜底 类别_i。

    timm 为可选依赖：未安装时 is_available() 返回 False，注册表加载失败会
    自动回退到其他物种模型（不影响系统启动，也不影响其余功能）。
    """

    # timm 中的 ResNeSt101 模型名（含 Split-Attention 结构）
    MODEL_NAME = "resnest101e"

    def __init__(self, weights_path: Optional[str] = None,
                 labels_path: Optional[str] = None,
                 class_names: Optional[List[str]] = None,
                 model_name: Optional[str] = None, **kwargs):
        super().__init__()
        self.weights_path = Path(weights_path) if weights_path else None
        self._labels_path = Path(labels_path) if labels_path else None
        self._class_names = list(class_names) if class_names else None
        self.model_name = model_name or self.MODEL_NAME
        self._model = None
        self._names: List[str] = []

    @staticmethod
    def _timm_available() -> bool:
        """timm 是否可导入（软依赖探测）。"""
        try:
            import timm  # noqa: F401
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        return bool(self.weights_path and self.weights_path.is_file()) \
            and self._timm_available()

    def _resolve_names(self, num_classes: int) -> List[str]:
        """解析类别名（labels.json 反转 → 占位）。"""
        if self._class_names and len(self._class_names) == num_classes:
            return self._class_names
        candidates = []
        if self._labels_path is not None:
            candidates.append(self._labels_path)
        if self.weights_path is not None:
            candidates.append(self.weights_path.parent / "labels.json")
        for path in candidates:
            try:
                import json
                mapping = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(mapping, dict) or not mapping:
                continue
            names: List[Optional[str]] = [None] * num_classes
            for label, idx in mapping.items():
                try:
                    pos = int(idx)
                except (TypeError, ValueError):
                    continue
                if 0 <= pos < num_classes:
                    names[pos] = str(label)
            if all(n is not None for n in names):
                return [str(n) for n in names]
        return [f"类别_{i}" for i in range(num_classes)]

    def load(self) -> None:
        if not self._timm_available():
            raise ModelLoadError(
                "ResNeSt101 需要 timm 库，请先安装：pip install timm")
        if not (self.weights_path and self.weights_path.is_file()):
            raise ModelLoadError(f"ResNeSt101 权重文件缺失：{self.weights_path}")
        try:
            import timm

            state = _load_torch_checkpoint(self.weights_path)
            # 去除 DataParallel 的 module. 前缀，便于与 timm 裸网络键名对齐
            state = {k[7:] if str(k).startswith("module.") else str(k): v
                     for k, v in state.items()}
            fc_key = next((k for k in state if k.endswith("fc.weight")), None)
            if fc_key is None:
                raise ModelLoadError("checkpoint 中未找到 fc 权重，无法推断类别数")
            num_classes = int(state[fc_key].shape[0])
            self._model = timm.create_model(self.model_name, pretrained=False,
                                            num_classes=num_classes)
            reference = self._model.state_dict()
            filtered = {k: v for k, v in state.items()
                        if k in reference and tuple(v.shape) == tuple(reference[k].shape)}
            missing, _unexpected = self._model.load_state_dict(filtered, strict=False)
            # 匹配率过低说明结构不符，视为加载失败（避免静默使用随机权重）
            if len(reference) and len(missing) > max(10, int(len(reference) * 0.1)):
                raise ModelLoadError(
                    f"权重与 timm {self.model_name} 结构匹配度过低"
                    f"（缺失 {len(missing)}/{len(reference)} 个键）")
            self._model.eval().to(get_device())
            self._names = self._resolve_names(num_classes)
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(f"ResNeSt101 加载失败：{exc}")
        self._loaded = True

    @torch.no_grad()
    def predict(self, pil_image: Image.Image, top_k: int = 3) -> List[SpeciesPrediction]:
        if not self._loaded:
            self.load()
        import timm

        data_cfg = timm.data.resolve_data_config({}, model=self._model)
        transform = timm.data.create_transform(**data_cfg, is_training=False)
        tensor = transform(pil_image.convert("RGB")).unsqueeze(0).to(get_device())
        logits = self._model(tensor)
        probs = torch.softmax(logits, dim=-1)[0]
        k = min(top_k, probs.shape[0])
        values, indices = torch.topk(probs, k)
        results = []
        for v, i in zip(values.tolist(), indices.tolist()):
            name = self._names[i] if i < len(self._names) else f"类别_{i}"
            results.append(SpeciesPrediction(
                label_en=name, label_cn=name, confidence=round(float(v), 4)))
        return results


class OwlViTAdapter(PartDetectorBackend):
    """OWL-ViT 开放词汇零样本检测（部位定位默认项，解决部位数据无框标注问题）。"""

    def __init__(self, model_id: Optional[str] = None,
                 threshold: Optional[float] = None, **kwargs):
        super().__init__()
        self.model_id = model_id or config.OWL_VIT_MODEL_ID
        self.threshold = threshold if threshold is not None else config.OWL_VIT_THRESHOLD
        self.cache_dir = config.MODELS_CACHE_DIR / "hf"
        self._model = None
        self._processor = None

    def is_available(self) -> bool:
        # 支持联网下载：缓存命中或未命中均视为可用，加载失败时走注册表回退
        return True

    def load(self) -> None:
        try:
            from transformers import OwlViTForObjectDetection, OwlViTProcessor

            resolved = resolve_model_path(self.model_id)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._processor = OwlViTProcessor.from_pretrained(
                resolved, cache_dir=str(self.cache_dir))
            self._model = OwlViTForObjectDetection.from_pretrained(
                resolved, cache_dir=str(self.cache_dir))
            self._model.eval().to(get_device())
        except Exception as exc:
            raise ModelLoadError(
                f"OWL-ViT 模型加载失败：{exc}。请检查网络，或在 config 中切换"
                "MODEL_DOWNLOAD_SOURCE（modelscope / hf 镜像）。")
        self._loaded = True

    def _query(self, pil_image: Image.Image,
               prompts: List[str]) -> List[Tuple[Box, float]]:
        """一组英文提示词 → 高于阈值的 (框, 置信度) 列表。

        注意：OWL-ViT 的多提示词须拆成 batch（每条提示词一个批次项、单图广播），
        返回结果每条提示词独立成项；若把多提示词塞进单批次，后处理会把
        所有提示词的候选框混在一起取 top，导致不同部位返回同一个框。
        """
        if not self._loaded:
            self.load()
        image = pil_image.convert("RGB")
        # text=[[p] for p in prompts]：N 个批次项各含 1 条提示词；images 单图自动广播
        inputs = self._processor(
            text=[[p] for p in prompts], images=image, return_tensors="pt")
        inputs = {k: v.to(get_device()) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self._model(**inputs)
        target_sizes = torch.tensor([image.size[::-1]])
        results = _owlvit_post_process(self._processor, outputs, self.threshold, target_sizes)
        detected: List[Tuple[Box, float]] = []
        for entry in results:
            for box, score in zip(entry["boxes"], entry["scores"]):
                x1, y1, x2, y2 = [float(v) for v in box.tolist()]
                detected.append((Box(x1, y1, x2, y2), float(score)))
        return detected

    def detect(self, pil_image: Image.Image, part_en: str) -> Optional[Box]:
        prompts = config.PART_PROMPTS.get(part_en)
        if not prompts:
            return None
        detected = self._query(pil_image, prompts)
        if not detected:
            return None
        # 对称部位天然返回左右多实例，统一取并集框覆盖整体区域
        return union_boxes([box for box, _ in detected])

    def detect_bird(self, pil_image: Image.Image) -> Optional[Box]:
        detected = self._query(pil_image, list(config.BIRD_PROMPTS))
        if not detected:
            return None
        return union_boxes([box for box, _ in detected])


class TrainedFrcnnAdapter(PartDetectorBackend):
    """Faster R-CNN 部位检测器（管线 B 产物；共享模型库条目亦走此适配器）。

    权重路径：注册表 params 的 weights_path（共享模型库条目）优先于旧参数
    checkpoint_path；均未传时读 config.DETECTOR_OUTPUT_DIR/best_detector.pth。
    类别数：显式 num_classes 参数 > 从权重 cls_score 形状推断 > config 常量
    （共享库条目由注册表 class_count 传入，保证与训练产物一致）。
    部位名称沿用 config.PART_TO_DETECTOR_IDX 约定的 15 部位索引（与管线 B 一致）。
    """

    def __init__(self, checkpoint_path: Optional[str] = None,
                 weights_path: Optional[str] = None,
                 num_classes: Optional[int] = None,
                 score_threshold: Optional[float] = None, **kwargs):
        super().__init__()
        resolved = weights_path or checkpoint_path
        self.checkpoint_path = Path(resolved) if resolved \
            else config.DETECTOR_OUTPUT_DIR / "best_detector.pth"
        self.num_classes = int(num_classes) if num_classes else None
        self.score_threshold = score_threshold if score_threshold is not None \
            else config.TRAIN_DETECTOR["score_threshold"]
        self._model = None

    def is_available(self) -> bool:
        return self.checkpoint_path.is_file()

    def load(self) -> None:
        if not self.is_available():
            raise ModelLoadError(
                f"部位检测器权重缺失：{self.checkpoint_path}"
                "（请先运行 train-detector 完成管线 B 训练）")
        try:
            from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
            from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

            state = _load_torch_checkpoint(self.checkpoint_path)
            # 兼容 DataParallel 保存的权重（module. 前缀）
            state = {str(k)[7:] if str(k).startswith("module.") else str(k): v
                     for k, v in state.items()}
            # 类别数：显式参数 > 权重推断 > 配置常量
            num_classes = self.num_classes
            if not num_classes:
                cls_key = next((k for k in state
                                if k.endswith("roi_heads.box_predictor.cls_score.weight")), None)
                if cls_key is not None:
                    num_classes = int(state[cls_key].shape[0])
            if not num_classes:
                num_classes = config.NUM_DETECTOR_CLASSES
            self._model = fasterrcnn_resnet50_fpn_v2(weights=None)
            in_features = self._model.roi_heads.box_predictor.cls_score.in_features
            self._model.roi_heads.box_predictor = FastRCNNPredictor(
                in_features, num_classes)
            self._model.load_state_dict(state)
            self._model.eval().to(get_device())
        except ModelLoadError:
            raise
        except Exception as exc:
            raise ModelLoadError(f"部位检测器加载失败：{exc}")
        self._loaded = True

    @torch.no_grad()
    def _infer(self, pil_image: Image.Image):
        if not self._loaded:
            self.load()
        from torchvision.transforms import functional as F

        tensor = F.to_tensor(pil_image.convert("RGB")).to(get_device())
        output = self._model([tensor])[0]
        return output["boxes"], output["labels"], output["scores"]

    def detect(self, pil_image: Image.Image, part_en: str) -> Optional[Box]:
        want = set(config.PART_TO_DETECTOR_IDX.get(part_en, []))
        if not want:
            return None
        boxes, labels, scores = self._infer(pil_image)
        selected = [
            Box(*[float(v) for v in box.tolist()])
            for box, label, score in zip(boxes, labels, scores)
            if int(label) in want and float(score) >= self.score_threshold
        ]
        return union_boxes(selected)

    def detect_bird(self, pil_image: Image.Image) -> Optional[Box]:
        boxes, _, scores = self._infer(pil_image)
        selected = [
            Box(*[float(v) for v in box.tolist()])
            for box, score in zip(boxes, scores)
            if float(score) >= self.score_threshold
        ]
        return union_boxes(selected)
