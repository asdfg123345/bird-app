# -*- coding: utf-8 -*-
"""
trainer_detector.py —— 管线 B 第三步：Faster R-CNN 部位检测器训练

作者：天咏（明月照今日，风摇过两川。）

功能：
- 仅使用人工校验通过名单（verified_list.txt）内的图片，按 9:1 图片级划分训练/验证；
- YOLO 归一化标签解析回原图像素框，类别 id +1 对齐 torchvision 背景类约定；
- 训练集随机水平翻转（同步翻转框），不做 Resize（Faster R-CNN 内部多尺度处理）；
- 自实现 mAP@0.5（101 点插值）逐轮评估，与 config.METRIC_BANDS_DETECTOR 比对；
- 产物：best_detector.pth / last_detector.pth / train_log.csv / evaluation_report.md。

入口：run_detector_training(args: dict) -> int；亦支持 python trainer_detector.py 直接运行。
"""

import argparse
import csv
import random
import sys
import traceback
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
from torchvision.transforms import functional as F

# 复用发布包 vision_system 的 config / model_adapters（本文件自开发版视觉系统移植）
_VISION_DIR = Path(__file__).resolve().parents[1] / 'vision_system'
if _VISION_DIR.is_dir() and str(_VISION_DIR) not in sys.path:
    sys.path.insert(0, str(_VISION_DIR))
import config
from model_adapters import get_device


# ---------------------------------------------------------------------------
# 通用工具：随机种子 / 训练互斥锁
# ---------------------------------------------------------------------------

def _set_seed(seed: int) -> None:
    """统一设置 torch / numpy / random 随机种子。"""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


class TrainingLock:
    """训练互斥锁：Windows 用 msvcrt、Linux 用 fcntl，均为非阻塞排他锁。"""

    def __init__(self) -> None:
        self._fh = None
        self._backend = None

    def acquire(self) -> None:
        config.TRAINING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(config.TRAINING_LOCK_FILE, "a+b")
        if self._fh.seek(0, 2) == 0:
            self._fh.write(b"0")
            self._fh.flush()
        try:
            import msvcrt
            self._backend = "msvcrt"
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            self._backend = "fcntl"
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"检测到已有训练任务进行中（锁文件：{config.TRAINING_LOCK_FILE}），本次运行退出。")

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if self._backend == "msvcrt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        finally:
            self._fh = None


# ---------------------------------------------------------------------------
# 数据：校验名单读取与检测数据集
# ---------------------------------------------------------------------------

def _read_verified_list(verified_list: Path):
    """读取人工校验通过名单：每行一个文件名或相对路径，忽略空行与 # 注释。"""
    if not verified_list.is_file():
        return None
    items = []
    for line in verified_list.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        items.append(s)
    return items


def _resolve_image_paths(images_dir: Path, entries):
    """把名单条目解析为真实图片路径：先按相对路径找，找不到再按文件名找。"""
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    paths = []
    for entry in entries:
        candidate = images_dir / entry
        if not candidate.is_file():
            candidate = images_dir / Path(entry).name
        if candidate.is_file() and candidate.suffix.lower() in exts:
            paths.append(candidate)
    # 去重并保持稳定顺序
    seen = set()
    unique = []
    for p in paths:
        if p.name not in seen:
            seen.add(p.name)
            unique.append(p)
    return unique


def _load_yolo_labels(label_path: Path, width: int, height: int):
    """解析 YOLO 标签：归一化 cx cy w h → 原图像素 x1y1x2y2，返回 (boxes, yolo_ids)。"""
    boxes, yolo_ids = [], []
    if not label_path.is_file():
        return boxes, yolo_ids
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            cls_id = int(float(parts[0]))
            cx, cy, bw, bh = [float(v) for v in parts[1:5]]
        except ValueError:
            continue
        x1 = (cx - bw / 2.0) * width
        y1 = (cy - bh / 2.0) * height
        x2 = (cx + bw / 2.0) * width
        y2 = (cy + bh / 2.0) * height
        boxes.append([x1, y1, x2, y2])
        yolo_ids.append(cls_id)
    return boxes, yolo_ids


class PartDetectionDataset(Dataset):
    """部位检测数据集：返回 (图像张量, 目标字典)。

    目标字典含像素坐标 boxes（x1y1x2x2）与 labels；
    YOLO 类别 id（0~14）需 +1 对齐 torchvision 背景约定（0 为背景类）。
    """

    def __init__(self, image_paths, labels_dir: Path, train: bool):
        self.image_paths = list(image_paths)
        self.labels_dir = Path(labels_dir)
        self.train = train

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        img_path = self.image_paths[idx]
        img = Image.open(img_path).convert("RGB")
        width, height = img.size
        boxes, yolo_ids = _load_yolo_labels(
            self.labels_dir / f"{img_path.stem}.txt", width, height)

        # 仅训练集做随机水平翻转，并同步翻转框：x1' = W - x2，x2' = W - x1
        if self.train and boxes and random.random() < 0.5:
            img = F.hflip(img)
            boxes = [[width - b[2], b[1], width - b[0], b[3]] for b in boxes]

        image_tensor = F.to_tensor(img)
        # YOLO id+1 对齐 torchvision 背景约定：labels ∈ [1,15]
        target = {
            "boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
            "labels": torch.tensor([c + 1 for c in yolo_ids], dtype=torch.int64),
        }
        return image_tensor, target


# ---------------------------------------------------------------------------
# 评估：自实现 mAP@0.5（101 点插值）
# ---------------------------------------------------------------------------

def _box_iou(box_a, box_b) -> float:
    """两个 xyxy 框的交并比。"""
    ax1, ay1, ax2, ay2 = [float(v) for v in box_a]
    bx1, by1, bx2, by2 = [float(v) for v in box_b]
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = inter_w * inter_h
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


@torch.no_grad()
def _evaluate_map50(model, loader, device):
    """计算验证集 mAP@0.5，返回 (mAP, {类别id: AP})，类别 id 1~15。"""
    model.eval()
    num_fg = config.NUM_DETECTOR_CLASSES          # 含背景共 16 类
    preds = {c: [] for c in range(1, num_fg)}   # 预测：类别 → [(分数, 框)]
    gts = {c: [] for c in range(1, num_fg)}     # 真值：类别 → [框]
    score_floor = 0.05                            # 评估保留分数下限（mAP 惯例）

    for images, targets in loader:
        images = [im.to(device) for im in images]
        outputs = model(images)
        for out, tgt in zip(outputs, targets):
            for box, label, score in zip(out["boxes"], out["labels"], out["scores"]):
                c = int(label)
                if 1 <= c < num_fg and float(score) >= score_floor:
                    preds[c].append((float(score), box.detach().cpu()))
            for box, label in zip(tgt["boxes"], tgt["labels"]):
                c = int(label)
                if 1 <= c < num_fg:
                    gts[c].append(box)

    aps = {}
    for c in range(1, num_fg):
        gt_boxes = gts[c]
        if not gt_boxes:
            continue                          # 该类无 GT 跳过，不计入 mAP
        ranked = sorted(preds[c], key=lambda item: -item[0])
        matched = [False] * len(gt_boxes)
        tp = np.zeros(len(ranked))
        fp = np.zeros(len(ranked))
        # 预测按分数降序，贪心匹配 IoU≥0.5 的未匹配 GT（每个 GT 只匹配一次）
        for i, (_, pred_box) in enumerate(ranked):
            best_iou, best_j = 0.0, -1
            for j, gt_box in enumerate(gt_boxes):
                if matched[j]:
                    continue
                v = _box_iou(pred_box, gt_box)
                if v > best_iou:
                    best_iou, best_j = v, j
            if best_j >= 0 and best_iou >= 0.5:
                tp[i] = 1.0
                matched[best_j] = True
            else:
                fp[i] = 1.0
        tp_cum = np.cumsum(tp)
        fp_cum = np.cumsum(fp)
        recall = tp_cum / float(len(gt_boxes))
        precision = tp_cum / np.maximum(tp_cum + fp_cum, 1e-9)
        # 101 点插值：对每个召回率阈值取不低于该召回率的最大精确率
        ap = 0.0
        for r in np.linspace(0.0, 1.0, 101):
            mask = recall >= r
            ap += float(precision[mask].max()) if mask.any() else 0.0
        aps[c] = ap / 101.0

    map50 = float(np.mean(list(aps.values()))) if aps else 0.0
    return map50, aps


def _write_report(report_path: Path, best_map: float, best_aps: dict, epochs: int) -> None:
    """生成中文评估报告（各类 AP 表 + mAP + 参考带比对结论）。"""
    bands = config.METRIC_BANDS_DETECTOR
    lines = ["# 部位检测器评估报告", ""]
    lines.append(f"- 最佳验证 mAP@0.5：{best_map:.4f}")
    lines.append(f"- 训练轮数：{epochs}")
    lines.append(f"- 类别数：{len(config.DETECTOR_CLASSES)}（背景类不计入）")
    lines.append("")
    lines.append("## 各类 AP@0.5")
    lines.append("")
    lines.append("| 类别 | 类别中文名 | AP@0.5 |")
    lines.append("| --- | --- | --- |")
    for cls_id, cls_name in enumerate(config.DETECTOR_CLASSES, start=1):
        semantic = cls_name
        for side in ("left_", "right_"):
            if semantic.startswith(side):
                semantic = semantic[len(side):]
        cn = config.PART_CN_NAMES.get(semantic, cls_name)
        ap = best_aps.get(cls_id, None)
        lines.append(f"| {cls_name} | {cn} | {f'{ap:.4f}' if ap is not None else '无真值'} |")
    lines.append("")
    lines.append("## 参考带比对结论")
    lines.append("")
    if best_map < bands["map50_bad"]:
        lines.append(f"- mAP@0.5 为 {best_map:.4f}，低于警戒线 {bands['map50_bad']:.2f}，"
                     "建议复核伪标注质量、训练轮数与学习率。")
    elif best_map < bands["map50_band"][0]:
        lines.append(f"- mAP@0.5 为 {best_map:.4f}，低于参考带 "
                     f"{bands['map50_band'][0]:.2f}~{bands['map50_band'][1]:.2f}，仍有提升空间。")
    elif best_map <= bands["map50_band"][1]:
        lines.append(f"- mAP@0.5 为 {best_map:.4f}，处于参考带 "
                     f"{bands['map50_band'][0]:.2f}~{bands['map50_band'][1]:.2f} 内，模型收敛正常。")
    else:
        lines.append(f"- mAP@0.5 为 {best_map:.4f}，高于参考带上限，请留意验证集规模较小导致的乐观估计。")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 训练入口
# ---------------------------------------------------------------------------

def run_detector_training(args: dict) -> int:
    """管线 B 检测器训练入口，返回 0 正常、2 锁冲突/参数错误、130 中断/异常。"""
    args = dict(args or {})
    td = config.TRAIN_DETECTOR
    images_dir = Path(args.get("images_dir") or config.PART_IMAGES_DIR)
    labels_dir = Path(args.get("labels_dir") or (config.PSEUDO_LABELS_DIR / "labels"))
    verified_list = Path(args.get("verified_list") or (config.PSEUDO_LABELS_DIR / "verified_list.txt"))
    out_dir = Path(args.get("out_dir") or config.DETECTOR_OUTPUT_DIR)
    epochs = int(args.get("epochs", td["epochs"]))
    batch_size = int(args.get("batch_size", td["batch_size"]))
    num_workers = int(args.get("num_workers", td["num_workers"]))
    lr = float(args.get("lr", td["lr"]))
    weight_decay = float(args.get("weight_decay", td["weight_decay"]))
    amp = bool(args.get("amp", td["amp"]))
    seed = int(args.get("seed", td.get("seed", 42)))
    val_ratio = float(td.get("val_ratio", 0.1))
    _set_seed(seed)

    lock = TrainingLock()
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"【错误】{exc}")
        return 2

    model = None
    try:
        # 校验名单：文件不存在直接报错退出
        entries = _read_verified_list(verified_list)
        if entries is None:
            print(f"【错误】校验名单不存在：{verified_list}，请先运行 review_labels.py 完成人工校验。")
            return 2
        image_paths = _resolve_image_paths(images_dir, entries)
        if not image_paths:
            print(f"【错误】名单内图片在 {images_dir} 中均未找到，请检查路径与名单内容。")
            return 2

        # 9:1 图片级划分：固定种子洗牌，前 90% 训练，其余验证
        shuffled = list(image_paths)
        random.Random(42).shuffle(shuffled)
        split = int(len(shuffled) * (1.0 - val_ratio))
        split = max(1, min(split, len(shuffled) - 1)) if len(shuffled) > 1 else len(shuffled)
        train_paths, val_paths = shuffled[:split], shuffled[split:]
        print("=" * 60)
        print("管线 B 第三步：Faster R-CNN 部位检测器训练")
        print(f"图片目录：{images_dir} | 标签目录：{labels_dir}")
        print(f"训练 {len(train_paths)} 张 / 验证 {len(val_paths)} 张 | 设备：{get_device()}")
        print(f"轮数：{epochs} | 批大小：{batch_size} | 学习率：{lr} | 混合精度：{amp}")
        print("=" * 60)

        out_dir.mkdir(parents=True, exist_ok=True)
        train_ds = PartDetectionDataset(train_paths, labels_dir, train=True)
        val_ds = PartDetectionDataset(val_paths, labels_dir, train=False)
        train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,
                                  num_workers=num_workers, collate_fn=lambda batch: tuple(zip(*batch)))
        val_loader = DataLoader(val_ds, batch_size=1, shuffle=False,
                                num_workers=num_workers, collate_fn=lambda batch: tuple(zip(*batch)))

        # 模型：Faster R-CNN v2 预训练骨干，分类头替换为 15 部位 + 1 背景
        model = fasterrcnn_resnet50_fpn_v2(weights="DEFAULT")
        in_features = model.roi_heads.box_predictor.cls_score.in_features
        model.roi_heads.box_predictor = FastRCNNPredictor(
            in_features, config.NUM_DETECTOR_CLASSES)
        device = get_device()
        model.to(device)
        amp_enabled = bool(amp) and device.type == "cuda"

        optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
        scaler = torch.amp.GradScaler("cuda", enabled=amp_enabled)
        log_csv = out_dir / "train_log.csv"

        best_map = -1.0
        best_aps = {}
        best_state = None
        for epoch in range(1, epochs + 1):
            model.train()
            loss_sum, batch_count = 0.0, 0
            for images, targets in train_loader:
                images = [im.to(device) for im in images]
                targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
                optimizer.zero_grad(set_to_none=True)
                with torch.amp.autocast(device_type="cuda", enabled=amp_enabled):
                    losses = model(images, targets)
                    loss = sum(losses.values())
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                loss_sum += float(loss.item())
                batch_count += 1
            scheduler.step()
            train_loss = loss_sum / batch_count if batch_count > 0 else 0.0
            cur_lr = optimizer.param_groups[0]["lr"]

            map50, aps = _evaluate_map50(model, val_loader, device)
            print(f"[第 {epoch}/{epochs} 轮] 训练损失 {train_loss:.4f} | "
                  f"验证 mAP@0.5 {map50:.4f} | 当前 lr {cur_lr:.2e}")
            for cls_id in sorted(aps):
                cls_name = config.DETECTOR_CLASSES[cls_id - 1]
                print(f"    {cls_name:<12} AP@0.5 = {aps[cls_id]:.4f}")
            bands = config.METRIC_BANDS_DETECTOR
            if map50 < bands["map50_bad"]:
                print(f"  【警惕】mAP@0.5 低于警戒线 {bands['map50_bad']:.2f}，请检查伪标注质量与学习率。")
            elif bands["map50_band"][0] <= map50 <= bands["map50_band"][1]:
                print(f"  mAP@0.5 处于参考带 {bands['map50_band'][0]:.2f}~{bands['map50_band'][1]:.2f} 内。")
            elif map50 > bands["map50_band"][1]:
                print(f"  mAP@0.5 超过参考带上限 {bands['map50_band'][1]:.2f}，请留意过拟合。")

            write_header = not log_csv.exists()
            with open(log_csv, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if write_header:
                    writer.writerow(["epoch", "train_loss", "val_map50", "lr"])
                writer.writerow([epoch, f"{train_loss:.6f}", f"{map50:.6f}", f"{cur_lr:.8f}"])

            if map50 > best_map:
                best_map = map50
                best_aps = dict(aps)
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                torch.save({"model_state": best_state,
                            "class_names": list(config.DETECTOR_CLASSES),
                            "map50": float(best_map)},
                           str(out_dir / "best_detector.pth"))

        torch.save({"model_state": {k: v.detach().cpu() for k, v in model.state_dict().items()},
                    "class_names": list(config.DETECTOR_CLASSES),
                    "map50": float(max(best_map, 0.0))},
                   str(out_dir / "last_detector.pth"))
        # 类别名与元信息落盘（供控制台模型库自动推断类别数与类别名）
        _write_sidecar(out_dir, max(best_map, 0.0))
        _write_report(out_dir / "evaluation_report.md", max(best_map, 0.0), best_aps, epochs)
        print(f"训练结束。最佳 mAP@0.5：{max(best_map, 0.0):.4f}；产物目录：{out_dir}")
        print("【如何注册到主系统】在 config.py 中把 ACTIVE_PART_MODEL 改为 \"trained_frcnn\" 即可。")
        # 自动登记进控制台共享模型库（部位模型接入模型库）；失败只提示，不影响训练产物
        _register_to_shared_library(
            out_dir, max(best_map, 0.0),
            str(args.get("model_name") or "部位检测器"),
            bool(args.get("register_shared", True)))
        return 0
    except KeyboardInterrupt:
        print("\n【中断】收到 Ctrl+C，保存当前模型后退出。")
        _save_interrupted(model, out_dir)
        return 130
    except Exception as exc:
        print(f"\n【错误】训练过程异常：{exc}")
        print(traceback.format_exc())
        _save_interrupted(model, out_dir)
        return 130
    finally:
        lock.release()
        torch.cuda.empty_cache()


def _save_interrupted(model, out_dir: Path) -> None:
    """异常/中断时尽力保存当前检测器模型。"""
    if model is None:
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        torch.save({"model_state": {k: v.detach().cpu() for k, v in model.state_dict().items()},
                    "class_names": list(config.DETECTOR_CLASSES)},
                   str(out_dir / "interrupted_detector.pth"))
        print(f"已保存中断时模型到：{out_dir / 'interrupted_detector.pth'}")
    except Exception as exc:
        print(f"【警告】保存中断模型失败：{exc}")


def _write_sidecar(out_dir: Path, best_map: float) -> None:
    """写类别名与元信息文件（class_names.json / meta.json）。

    class_names.json：检测器索引顺序的类别名列表（背景 + 15 部位），
    控制台模型库据此自动推断类别数与类别名（无需人工填写）。
    meta.json：本系统的检测指标与来源信息，便于在控制台侧查看。
    """
    import json
    from datetime import datetime

    try:
        names = ["background"] + list(config.DETECTOR_CLASSES)
        (out_dir / "class_names.json").write_text(
            json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
        meta = {
            "architecture": "faster_rcnn",
            "task_type": "detection",
            "metric_name": "map50",
            "metrics": {"map50": round(float(best_map), 4)},
            "class_count": config.NUM_DETECTOR_CLASSES,
            "part_names": list(config.DETECTOR_CLASSES),
            "source": "视觉系统管线B",
            "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        (out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"【警告】类别/元信息文件写入失败：{exc}")


def _register_to_shared_library(out_dir: Path, best_map: float,
                                base_name: str, register: bool = True) -> None:
    """把检测器产物登记进控制台共享模型库（打通「训练→入库→部位模型下拉」）。

    实现思路：共享模型库根目录形如 <控制台>/data/model_library，据此反推控制台
    目录后导入其 model_library 模块，复用其命名取号与注册逻辑（避免重复实现
    注册表写入）。任一环节不可用时只打印提示，由用户在控制台「模型库 → 导入
    外部模型」手动导入本产物目录，绝不影响已完成训练的产物。
    """
    if not register:
        print("已按参数跳过控制台模型库登记（--no-register）。")
        return
    lib_root = config.find_shared_model_library()
    if not lib_root:
        print("【提示】未探测到控制台共享模型库，已跳过自动登记；"
              f"可在控制台「模型库 → 导入外部模型」手动导入：{out_dir}")
        return
    console_dir = Path(lib_root).parent.parent   # <控制台>/data/model_library
    try:
        if str(console_dir) not in sys.path:
            sys.path.insert(0, str(console_dir))
        import model_library as console_ml       # 控制台模型库模块（无 torch 依赖）
    except Exception as exc:
        print(f"【提示】加载控制台模型库模块失败，已跳过自动登记（{exc}）；"
              f"可手动导入：{out_dir}")
        return
    try:
        import shutil

        dir_name, _date_str, _version = console_ml.next_model_name(
            base_name, root=lib_root)   # 取号与注册同库，避免落到默认库目录
        target = Path(lib_root) / dir_name
        target.mkdir(parents=True, exist_ok=True)
        for name in ("best_detector.pth", "class_names.json", "meta.json",
                     "evaluation_report.md"):
            src = out_dir / name
            if src.is_file():
                shutil.copy2(src, target / name)
        record = console_ml.register_model(
            target, class_count=config.NUM_DETECTOR_CLASSES,
            accuracy=round(float(best_map), 4), source="视觉系统管线B",
            architecture="faster_rcnn", metric_name="map50",
            root=lib_root)   # 与取号同一模型库，避免写入默认库
        print(f"已登记进控制台模型库：{record.get('name')}"
              f"（架构 faster_rcnn；控制台「视觉分析 → 部位模型」下拉可见，"
              f"重启控制台或在面板点「刷新」后生效）")
    except Exception as exc:
        print(f"【警告】登记进控制台模型库失败：{exc}（可手动导入：{out_dir}）")


def _build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    td = config.TRAIN_DETECTOR
    parser = argparse.ArgumentParser(description="管线 B 第三步：Faster R-CNN 部位检测器训练")
    parser.add_argument("--images-dir", dest="images_dir", default=None,
                        help=f"图片目录（默认 {config.PART_IMAGES_DIR}）")
    parser.add_argument("--labels-dir", dest="labels_dir", default=None,
                        help=f"YOLO 标签目录（默认 {config.PSEUDO_LABELS_DIR / 'labels'}）")
    parser.add_argument("--verified-list", dest="verified_list", default=None,
                        help=f"校验通过名单（默认 {config.PSEUDO_LABELS_DIR / 'verified_list.txt'}）")
    parser.add_argument("--out-dir", dest="out_dir", default=None,
                        help=f"输出目录（默认 {config.DETECTOR_OUTPUT_DIR}）")
    parser.add_argument("--epochs", dest="epochs", type=int, default=td["epochs"])
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=td["batch_size"])
    parser.add_argument("--num-workers", dest="num_workers", type=int, default=td["num_workers"])
    parser.add_argument("--lr", dest="lr", type=float, default=td["lr"])
    parser.add_argument("--weight-decay", dest="weight_decay", type=float, default=td["weight_decay"])
    parser.add_argument("--amp", dest="amp", action="store_true", default=td["amp"], help="开启混合精度")
    parser.add_argument("--no-amp", dest="amp", action="store_false", help="关闭混合精度")
    parser.add_argument("--seed", dest="seed", type=int, default=td.get("seed", 42))
    parser.add_argument("--model-name", dest="model_name", default="部位检测器",
                        help="登记进控制台模型库时的模型基名（默认 部位检测器）")
    parser.add_argument("--no-register", dest="register_shared", action="store_false",
                        default=True, help="训练结束后不自动登记进控制台模型库")
    return parser


if __name__ == "__main__":
    sys.exit(run_detector_training(vars(_build_arg_parser().parse_args())))
