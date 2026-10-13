# -*- coding: utf-8 -*-
"""
config.py —— 系统集中配置（唯一允许出现绝对路径的文件）

作者：天咏（明月照今日，风摇过两川。）

说明：
1. 系统内路径（缓存/输出/训练产物）基于本文件位置自动推导，整体迁移无需修改；
2. 外部资源默认路径（预置模型、数据集）集中于"外部资源"一节，系统迁移后仅需修改该节；
3. 模型注册表、阈值、提示词映射、空间先验表、训练超参、指标参考带全部集中在本文件。
"""

import json
import os
from pathlib import Path

# ============ 作者签名（写入所有源文件） ============
AUTHOR_SIGNATURE = "天咏（明月照今日，风摇过两川。）"

# ============ 系统内路径（基于本文件位置自动推导，禁止硬编码） ============
SYSTEM_ROOT = Path(__file__).resolve().parent
MODELS_CACHE_DIR = SYSTEM_ROOT / "models_cache"           # 下载模型缓存（运行时生成）
OUTPUT_DIR = SYSTEM_ROOT / "output"                       # 标记图输出（运行时生成）
TRAINING_OUTPUT_DIR = SYSTEM_ROOT / "training_output"     # 训练产物根目录
SPECIES_OUTPUT_DIR = TRAINING_OUTPUT_DIR / "species"      # 管线 A 产物
DETECTOR_OUTPUT_DIR = TRAINING_OUTPUT_DIR / "detector"    # 管线 B 检测器产物
PSEUDO_LABELS_DIR = TRAINING_OUTPUT_DIR / "pseudo_labels" # 管线 B 伪标注产物（labels/ + report.csv）
TRAINING_LOCK_FILE = TRAINING_OUTPUT_DIR / "training.lock"  # 训练互斥锁

# ============ 外部资源默认路径（系统迁移后仅需修改本节） ============
VIT_BIRD_525_DIR = Path(r"d:\鸟类\bird_species_image_detection")                 # 525 类鸟类 ViT（出厂预置）
RESNET200_CHECKPOINT = Path(r"d:\鸟类\data\model_output\best_model.pth")         # 200 类 ResNet50（项目自训，可不存在）
SPECIES_DATASET_DIR = Path(r"d:\鸟类\鸟类训练数据集_压缩版\鸟类训练数据集_压缩版")  # 200 类数据集根（含 train/val/test）
PART_IMAGES_DIR = SPECIES_DATASET_DIR / "部位检测数据集" / "train" / "images"    # 部位图片库（803 张，无人工框标注）

# ============ 模型下载源（国内网络优先） ============
# 可选值："modelscope"（魔搭，国内直连）或 "hf"（HF 镜像，走 HF_MIRROR_ENDPOINT）
MODEL_DOWNLOAD_SOURCE = "modelscope"
HF_MIRROR_ENDPOINT = "https://hf-mirror.com"
OWL_VIT_MODEL_ID = "google/owlvit-base-patch32"  # OWL-ViT 开放词汇检测模型（约 600MB）
OWL_VIT_THRESHOLD = 0.15                          # 检测置信度阈值（宁缺毋滥）

# ============ 部位定义（12 类语义部位；训练侧 15 类含左右拆分，见 DETECTOR_CLASSES） ============
PART_CATEGORIES = [
    "wing", "eye", "leg", "crown", "forehead", "nape",
    "back", "throat", "breast", "belly", "beak", "tail",
]

# 语义部位中文名（用于描述与标记标签）
PART_CN_NAMES = {
    "wing": "翅膀", "eye": "眼睛", "leg": "腿脚", "crown": "头顶",
    "forehead": "前额", "nape": "后颈", "back": "背部", "throat": "喉部",
    "breast": "胸部", "belly": "腹部", "beak": "喙", "tail": "尾部",
}

# 中文关键词映射（命中即返回对应部位；解析时按关键词长度降序优先，避免"头"抢占"头顶"）
PART_KEYWORDS = {
    "wing": ["翅膀", "翅", "羽翼", "翼"],
    "eye": ["眼睛", "眼部", "眼"],
    "leg": ["腿", "脚", "爪子", "爪"],
    "crown": ["头顶", "冠", "头"],
    "forehead": ["前额", "额头", "额"],
    "nape": ["后颈", "脖颈", "颈", "脖子"],
    "back": ["背部", "背"],
    "throat": ["喉咙", "喉"],
    "breast": ["胸部", "胸脯", "胸"],
    "belly": ["肚子", "腹部", "腹"],
    "beak": ["嘴巴", "喙", "嘴"],
    "tail": ["尾巴", "尾羽", "尾"],
}

# 左右对称部位（定位时合并左右实例为并集框）
SYMMETRIC_PARTS = ("wing", "eye", "leg")

# OWL-ViT 英文提示词（每部位 2~3 条；对称部位用整体提示词取并集框）
PART_PROMPTS = {
    "wing": ["a wing of a bird", "bird wing", "the wings of a bird"],
    "eye": ["an eye of a bird", "the eyes of a bird"],
    "leg": ["the leg of a bird", "bird leg"],
    "crown": ["the crown of a bird", "top of a bird's head"],
    "forehead": ["the forehead of a bird", "front of a bird's head"],
    "nape": ["the nape of a bird", "back of a bird's neck"],
    "back": ["the back of a bird", "bird back feathers"],
    "throat": ["the throat of a bird", "bird throat"],
    "breast": ["the breast of a bird", "chest of a bird"],
    "belly": ["the belly of a bird", "abdomen of a bird"],
    "beak": ["the beak of a bird", "bird beak"],
    "tail": ["the tail of a bird", "bird tail feathers"],
}
BIRD_PROMPTS = ["a bird", "bird"]

# 空间先验表：部位未检出时，按鸟体框比例估计（cx, cy, w, h 均为相对鸟体框的 0~1 比例）
SPACE_PRIORS = {
    "crown":    (0.50, 0.10, 0.50, 0.22),
    "forehead": (0.55, 0.18, 0.40, 0.18),
    "eye":      (0.58, 0.22, 0.30, 0.14),
    "nape":     (0.45, 0.20, 0.40, 0.18),
    "back":     (0.45, 0.42, 0.60, 0.36),
    "throat":   (0.58, 0.34, 0.30, 0.18),
    "breast":   (0.55, 0.52, 0.42, 0.28),
    "belly":    (0.50, 0.72, 0.48, 0.30),
    "beak":     (0.72, 0.26, 0.28, 0.14),
    "wing":     (0.45, 0.48, 0.75, 0.45),
    "tail":     (0.40, 0.88, 0.40, 0.22),
    "leg":      (0.55, 0.90, 0.30, 0.18),
}

# ============ 检测器训练 15 类（与部位数据集 part_names.txt 顺序一致） ============
DETECTOR_CLASSES = [
    "back", "beak", "belly", "breast", "crown", "forehead",
    "left_eye", "left_leg", "left_wing", "nape",
    "right_eye", "right_leg", "right_wing", "tail", "throat",
]
NUM_DETECTOR_CLASSES = len(DETECTOR_CLASSES) + 1  # 15 部位 + 1 背景

# 语义部位 → 检测器类别索引（对称部位含左右两个训练类）
PART_TO_DETECTOR_IDX = {
    "back": [0], "beak": [1], "belly": [2], "breast": [3], "crown": [4],
    "forehead": [5], "nape": [9], "tail": [13], "throat": [14],
    "eye": [6, 10], "leg": [7, 11], "wing": [8, 12],
}

# ============ 物种中文名映射（525 类模型标签为英文；未收录时回退英文名） ============
SPECIES_CN_NAMES = {
    "BARN SWALLOW": "家燕", "BARN OWL": "仓鸮", "GOLDEN EAGLE": "金雕",
    "BALD EAGLE": "白头海雕", "OSPREY": "鹗（鱼鹰）", "PEREGRINE FALCON": "游隼",
    "COMMON STARLING": "紫翅椋鸟", "EUROPEAN GOLDFINCH": "红额金翅雀",
    "HOUSE SPARROW": "家麻雀", "EURASIAN MAGPIE": "欧亚喜鹊",
    "EURASIAN BULLFINCH": "红腹灰雀", "BLACK-CAPPED CHICKADEE": "黑顶山雀",
    "NORTHERN CARDINAL": "主红雀（北美红雀）", "AMERICAN ROBIN": "旅鸫",
    "BLUE JAY": "冠蓝鸦", "CROW": "乌鸦", "MOURNING DOVE": "哀鸽",
    "ROCK DOVE": "原鸽（野鸽）", "ZEBRA DOVE": "斑姬地鸠",
    "EUROPEAN TURTLE DOVE": "欧斑鸠", "WILD TURKEY": "野火鸡",
    "PEACOCK": "孔雀", "MALLARD DUCK": "绿头鸭", "MANDRIN DUCK": "鸳鸯",
    "WOOD DUCK": "林鸳鸯", "BLACK SWAN": "黑天鹅", "MUTE SWAN": "疣鼻天鹅",
    "TRUMPTER SWAN": "黑嘴天鹅", "SNOW GOOSE": "雪雁", "CANADA GOOSE": "加拿大黑雁",
    "COMMON LOON": "普通潜鸟", "ATLANTIC PUFFIN": "海鹦", "PUFFIN": "海鹦",
    "FLAMINGO": "火烈鸟", "AMERICAN FLAMINGO": "美洲火烈鸟",
    "GREAT BLUE HERON": "大蓝鹭", "BLUE HERON": "大蓝鹭",
    "SNOWY EGRET": "雪鹭", "GREY HERON": "苍鹭", "CATTLE EGRET": "牛背鹭",
    "CHINESE POND HERON": "池鹭", "SQUACCO HERON": "白翅黄池鹭",
    "MARABOU STORK": "秃鹳", "JABIRU": "裸颈鹳", "SHOEBILL": "鲸头鹳",
    "ASIAN OPENBILL STORK": "钳嘴鹳", "WHITE STORK": "白鹳",
    "BALD IBIS": "隐鹮", "GLOSSY IBIS": "彩鹮", "SCARLET IBIS": "美洲红鹮",
    "CRESTED IBIS": "朱鹮", "ASIAN CRESTED IBIS": "朱鹮",
    "BLACK-FACED SPOONBILL": "黑脸琵鹭", "ROSEATE SPOONBILL": "粉红琵鹭",
    "KINGFISHER": "翠鸟", "COMMON KINGFISHER": "普通翠鸟",
    "MALACHITE KINGFISHER": "孔雀石翠鸟", "BELTED KINGFISHER": "白腹鱼狗",
    "KOOKABURRA": "笑翠鸟", "HYACINTH MACAW": "紫蓝金刚鹦鹉",
    "SCARLET MACAW": "五彩金刚鹦鹉", "MILITARY MACAW": "军金刚鹦鹉",
    "COCKATOO": "凤头鹦鹉", "SULPHUR-CRESTED COCKATOO": "葵花凤头鹦鹉",
    "RAINBOW LORIKEET": "彩虹吸蜜鹦鹉", "ROSY FACED LOVEBIRD": "桃脸牡丹鹦鹉",
    "ALEXANDRINE PARAKEET": "亚历山大鹦鹉", "BUDGERIGAR": "虎皮鹦鹉",
    "GOLDEN PHEASANT": "红腹锦鸡", "RING-NECKED PHEASANT": "环颈雉",
    "SILVER PHEASANT": "白鹇", "RED JUNGLEFOWL": "红原鸡",
    "COMMON QUAIL": "鹌鹑", "CHUKAR PARTRIDGE": "石鸡",
    "OSTRICH": "鸵鸟", "EMU": "鸸鹋", "CASSOWARY": "食火鸡",
    "KIWI": "几维鸟", "PENGUIN": "企鹅", "EMPEROR PENGUIN": "帝企鹅",
    "GREAT HORNBILL": "双角犀鸟", "AFRICAN PIED HORNBILL": "非洲斑犀鸟",
    "WHITE-CRESTED HORNBILL": "白冠犀鸟", "TOUCAN": "巨嘴鸟",
    "COLLARED ARACARI": "领簇舌巨嘴鸟", "WOODPECKER": "啄木鸟",
    "DOWNY WOODPECKER": "绒啄木鸟", "NORTHERN FLICKER": "北扑翅䴕",
    "HOOPOE": "戴胜", "HOOPOES": "戴胜",
    "COMMON CUCKOO": "大杜鹃", "ROADRUNNER": "走鹃",
    "TAWNY FROGMOUTH": "茶色蟆口鸱", "GREAT POTOO": "大林鸱",
    "BARN OWL": "仓鸮", "LONG-EARED OWL": "长耳鸮", "GREAT GRAY OWL": "乌林鸮",
    "SNOWY OWL": "雪鸮", "EURASIAN EAGLE-OWL": "雕鸮",
    "COMMON SWIFT": "普通雨燕", "HUMMINGBIRD": "蜂鸟",
    "RUBY-THROATED HUMMINGBIRD": "红喉北蜂鸟",
    "COMMON KINGFISHER": "普通翠鸟", "BEE-EATER": "蜂虎",
    "ASIAN GREEN BEE-EATER": "亚洲绿喉蜂虎", "CARMINE BEE-EATER": "洋红蜂虎",
    "ROLLER": "佛法僧", "INDIAN ROLLER": "棕胸佛法僧", "LILAC ROLLER": "淡紫色佛法僧",
    "HOOPOES": "戴胜", "ALBATROSS": "信天翁", "FRIGATEBIRD": "军舰鸟",
    "FRIGATE": "军舰鸟", "PELICAN": "鹈鹕", "DALMATIAN PELICAN": "卷羽鹈鹕",
    "GANNET": "塘鹅", "NORTHERN GANNET": "北方塘鹅",
    "CORMORANT": "鸬鹚", "DOUBLE-CRESTED CORMORANT": "双冠鸬鹚",
    "ANHINGA": "美洲蛇鹈", "VULTURE": "秃鹫", "TURKEY VULTURE": "红头美洲鹫",
    "BLACK VULTURE": "黑美洲鹫", "KING VULTURE": "王鹫",
    "CONDOR": "神鹰", "CALIFORNIA CONDOR": "加州神鹰",
    "FALCON": "隼", "AMERICAN KESTREL": "美洲隼", "MERLIN": "灰背隼",
    "HAWK": "鹰", "RED-TAILED HAWK": "红尾鵟", "COOPER'S HAWK": "库氏鹰",
    "NORTHERN GOSHAWK": "苍鹰", "HARRIER": "鹞", "OSPREY": "鹗",
    "DOVE": "鸽鸠", "PIGEON": "鸽", "NICOBAR PIGEON": "尼柯巴鸠",
    "PARROT": "鹦鹉", "MACAW": "金刚鹦鹉",
}

# ============ 标记绘制参数 ============
ANNOTATOR = {
    "color_bgr": (0, 0, 255),        # 红色（OpenCV BGR 顺序）
    "thickness": 3,                  # 边框线宽（px）
    "circle_pad": 1.2,               # 圆圈半径 = 短边一半 × 该系数
    "font_path": Path(r"C:\Windows\Fonts\msyh.ttc"),  # 微软雅黑（中文标签）
    "font_size": 18,                 # 标签字号
    "label_bg_pad": 4,               # 标签底色内边距（px）
    "output_name_suffix": "_marked", # 输出文件名后缀
}

# ============ 训练超参（管线 A：200 类物种识别微调） ============
# 云端魔搭 24G 显存建议：batch_size 64、num_workers 4~8（经 CLI --batch-size/--num-workers 覆盖）
TRAIN_SPECIES = {
    "backbone": "resnet50",          # 可选 "resnet50" / "vit_bird_525"（注册表思路，不写死）
    "epochs": 30,
    "batch_size": 32,                # 本地默认；云端建议 64
    "num_workers": 0,                # Windows 默认 0；云端建议 4~8
    "lr_head": 1e-3,                 # 分类头学习率
    "lr_backbone": 1e-4,             # 预训练骨干学习率（参数组 ×0.1）
    "weight_decay": 0.01,
    "warmup_epochs": 2,              # 线性升温轮数
    "freeze_backbone_epochs": 0,     # >0 时先冻结骨干只训头 N 轮，再解冻全量微调
    "early_stop_patience": 10,       # 验证 Top-1 连续无提升轮数上限
    "amp": True,                     # 混合精度
    "grad_clip": 5.0,                # 梯度裁剪 max_norm
    "color_jitter": False,           # 颜色抖动（羽毛颜色本身是识别特征，默认关）
    "image_size": 224,
    "seed": 42,
}

# 训练超参（管线 B：15 类部位检测器；云端建议 batch_size 8、num_workers 4~8）
TRAIN_DETECTOR = {
    "epochs": 20,
    "batch_size": 4,                 # 检测显存开销高，本地默认 4；云端建议 8
    "num_workers": 0,
    "lr": 1e-4,
    "weight_decay": 1e-4,
    "val_ratio": 0.1,                # 图片级 9:1 划分
    "amp": True,
    "score_threshold": 0.15,
    "seed": 42,
}

# ============ 指标参考带（验收与排障依据，日志每轮与当前值比对） ============
METRIC_BANDS_SPECIES = {
    "initial_loss": 5.3,             # 初始 CE 损失理论值 ln(200)≈5.3
    "initial_loss_warn_high": 6.5,   # 首轮损失高于此值警惕
    "top1_first5_min": 0.40,         # 前 5 轮验证 Top-1 应升至 40% 以上
    "top1_first5_bad": 0.20,         # 低于 20% 说明 lr/数据/类别映射有误
    "top1_resnet_band": (0.70, 0.90),  # ResNet50 收敛带
    "top1_vit_band": (0.85, 0.95),     # ViT 鸟域起点收敛带
    "top5_gap": (0.10, 0.15),        # Top-5 约比 Top-1 高 10~15 个百分点
    "overfit_warn_pp": 15,           # 训练-验证 Top-1 差超过 15 个百分点警惕
    "final_train_loss": 0.5,         # 训练损失终值参考
    "final_val_loss": 1.0,           # 验证损失终值参考
}

METRIC_BANDS_DETECTOR = {
    "pseudo_recall_big": (0.60, 0.85),   # 大部位（wing/tail/belly/beak）伪标注检出率
    "pseudo_recall_big_bad": 0.50,
    "pseudo_conf_median": (0.20, 0.40),  # 伪标注置信度中位数
    "pseudo_recall_small": (0.40, 0.70), # 小部位（eye/crown）检出率
    "map50_band": (0.40, 0.70),          # 收敛后验证 mAP@0.5（15 类平均）
    "map50_bad": 0.30,
    "ap_big_min": 0.60,                  # 大部位单类 AP 下限
    "ap_small_band": (0.30, 0.50),       # 小部位单类 AP 正常区间
}

# ============ GUI 默认设置 ============
GUI_DEFAULT_MARKER = "frame"         # 默认标记样式（frame 红框 / circle 圆圈）
GUI_WINDOW_TITLE = "鸟类视觉分析系统 —— 识物种 · 定部位 · 答问题"


# ============ 模型注册表与激活项（切换模型只改 ACTIVE_* 或 CLI 传参） ============
# 说明：适配器类在本文件底部惰性导入，避免与 model_adapters 循环引用；
#       新增模型 = 继承抽象基类写适配器 → 在下方注册表登记 → 改激活项。
def _build_registries():
    """构建模型注册表（惰性导入适配器，规避循环引用）。"""
    from model_adapters import (
        OwlViTAdapter,
        ResNet50_200Adapter,
        TrainedFrcnnAdapter,
        VitBird525Adapter,
    )

    species_models = {
        "vit_bird_525": {
            "adapter": VitBird525Adapter,
            "params": {},            # 默认读 config.VIT_BIRD_525_DIR
            "display_name": "525类鸟类ViT（本地预置，离线）",
        },
        "resnet50_200": {
            "adapter": ResNet50_200Adapter,
            "params": {},            # 默认读 config.RESNET200_CHECKPOINT
            "display_name": "200类ResNet50（项目自训）",
        },
    }
    part_models = {
        "owlvit": {
            "adapter": OwlViTAdapter,
            "params": {},
            "display_name": "OWL-ViT 开放词汇检测（零样本）",
        },
        "trained_frcnn": {
            "adapter": TrainedFrcnnAdapter,
            "params": {},
            "display_name": "Faster R-CNN 部位检测器（管线B产物）",
        },
    }
    return species_models, part_models


# 说明：注册表惰性构建（模块 __getattr__），规避"model_adapters 先于 config
#       被导入"时的循环引用；新增模型 = 继承抽象基类写适配器 → 在
#       _build_registries 登记 → 改激活项，核心链路零修改。
_REGISTRY_CACHE = {}


def _get_species_registry() -> dict:
    """获取出厂物种模型注册表（惰性构建并缓存，供本模块内函数复用）。

    注意：模块级 __getattr__ 只对外部 config.SPECIES_MODELS 属性访问生效，
    本模块内部代码须通过本函数取注册表，不能直接裸名引用。
    """
    if "SPECIES_MODELS" not in _REGISTRY_CACHE:
        species_models, part_models = _build_registries()
        _REGISTRY_CACHE["SPECIES_MODELS"] = species_models
        _REGISTRY_CACHE["PART_MODELS"] = part_models
    return _REGISTRY_CACHE["SPECIES_MODELS"]


def _get_part_registry() -> dict:
    """获取出厂部位模型注册表（惰性构建并缓存，供本模块内函数复用）。"""
    if "PART_MODELS" not in _REGISTRY_CACHE:
        _get_species_registry()   # 一次构建同时填充两张表
    return _REGISTRY_CACHE["PART_MODELS"]


def __getattr__(name):
    """惰性返回模型注册表（出厂预置 + 共享模型库合并视图）。

    说明：物种/部位两张表都走合并视图，保证控制台模型库中登记的模型在
    GUI 下拉、qa_server 服务与 part_locator 解析三处都能被识别。
    """
    if name == "SPECIES_MODELS":
        return get_all_species_models()
    if name == "PART_MODELS":
        return get_all_part_models()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 回退顺序：激活模型不可用时按列表顺序依次尝试，全部不可用则中文报错
SPECIES_FALLBACK_ORDER = ["vit_bird_525", "resnet50_200"]
PART_FALLBACK_ORDER = ["owlvit", "trained_frcnn"]

# 当前激活模型（换模型只改这两行，或用 CLI --species-model / --part-model 覆盖）
ACTIVE_SPECIES_MODEL = "vit_bird_525"
ACTIVE_PART_MODEL = "owlvit"


# ============ 共享模型库接入（外部 iNaturalist 项目维护，本系统只读） ============
# 共享模型库根目录含 registry.json 与各模型子目录（best_model.pt / last_model.pt /
# labels.json / meta.json）；本系统只读取、不写入、不修改其内容。
# 说明：全部路径基于 SYSTEM_ROOT 与环境变量推导，不硬编码盘符。
SHARED_LIBRARY_ENV_VAR = "BIRD_SHARED_MODEL_LIBRARY"   # 环境变量：显式指定库根目录

# 探测结果缓存：{"resolved": 是否已探测过, "path": 库根目录 Path 或 None}
# （"探测过但未找到"（path=None）也是有效结论，须与"未探测"区分，避免重复扫盘）
_SHARED_LIBRARY_CACHE = {"resolved": False, "path": None}


def _is_shared_library_root(candidate) -> bool:
    """判断候选路径是否为共享模型库根目录（以 registry.json 存在为准）。"""
    try:
        return (Path(candidate) / "registry.json").is_file()
    except (OSError, TypeError, ValueError):
        return False


def reset_shared_model_library_cache() -> None:
    """清空共享模型库探测缓存（供单测与运行期目录变更后强制重新探测）。"""
    _SHARED_LIBRARY_CACHE["resolved"] = False
    _SHARED_LIBRARY_CACHE["path"] = None


def find_shared_model_library(force_refresh: bool = False):
    """探测外部共享模型库根目录，返回 Path；找不到返回 None（不报错）。

    探测顺序（候选目录含 registry.json 才算命中）：
      1. 环境变量 BIRD_SHARED_MODEL_LIBRARY 指向的目录（显式指定，优先级最高）；
      2. 本系统上一级的 iNaturalist/iNaturalist/data/model_library；
      3. 本系统上一级的 data/model_library；
      4. 上一级下通配兜底扫描 */iNaturalist*/data/model_library、*/data/model_library。
    结果缓存到模块级变量；force_refresh=True 时忽略缓存重新探测。
    """
    if not force_refresh and _SHARED_LIBRARY_CACHE["resolved"]:
        return _SHARED_LIBRARY_CACHE["path"]

    root = None
    # ① 环境变量优先；指向无效（无 registry.json）时继续向后探测
    env_value = os.environ.get(SHARED_LIBRARY_ENV_VAR, "").strip()
    if env_value and _is_shared_library_root(env_value):
        root = Path(env_value)

    # ② ③ 固定相对位置（基于本系统目录推导上一级，不硬编码盘符）
    if root is None:
        parent_dir = SYSTEM_ROOT.parent
        for candidate in (
            parent_dir / "iNaturalist" / "iNaturalist" / "data" / "model_library",
            parent_dir / "data" / "model_library",
        ):
            if _is_shared_library_root(candidate):
                root = candidate
                break

    # ④ 上一级目录通配兜底扫描（iNaturalist 系优先，其余 data 库次之；
    #    sorted 保证多命中时结果确定）
    if root is None:
        parent_dir = SYSTEM_ROOT.parent
        try:
            for pattern in ("*/iNaturalist*/data/model_library",
                            "*/data/model_library"):
                for candidate in sorted(parent_dir.glob(pattern)):
                    if candidate == SYSTEM_ROOT:
                        continue  # 跳过本系统自身目录，避免自指
                    if _is_shared_library_root(candidate):
                        root = candidate
                        break
                if root is not None:
                    break
        except OSError:
            pass  # 上一级不可读时按未找到处理

    _SHARED_LIBRARY_CACHE["resolved"] = True
    _SHARED_LIBRARY_CACHE["path"] = root
    return root


def _load_shared_registry_records(root=None) -> list:
    """读取共享库注册表并返回 models 记录列表。

    注册表缺失、损坏（非法 JSON）、结构不合法（无 models 列表）时一律
    返回空列表，绝不抛异常（共享库异常不能影响本系统启动）。
    """
    if root is None:
        root = find_shared_model_library()
    if not root:
        return []
    reg_path = Path(root) / "registry.json"
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("models") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def get_shared_species_models(root=None) -> dict:
    """读取共享模型库中的物种分类模型，转为与 SPECIES_MODELS 同构的 dict。

    合格条目判定：模型目录下存在可用权重文件（best_model.pt / last_model.pt /
    model.safetensors / 其余 .pt/.pth/.safetensors/.bin），且记录的架构属于
    可推理架构（见下方 arch_adapters 映射）。仅入库管理的架构（分割、LLM/RNN、
    mmpretrain ViT 等）会被跳过，不进入推理候选。

    条目 id 与出厂注册表重名时自动追加后缀"（共享库）"避让。
    参数 root 为共享模型库根目录；None 时自动探测（find_shared_model_library）。
    库缺失/损坏/无合格条目时返回 {}（绝不抛异常）。
    """
    records = _load_shared_registry_records(root)
    if not records:
        return {}
    # 惰性导入适配器（与 _build_registries 一致，规避循环引用）
    from model_adapters import (
        ResNeSt101Adapter,
        ResNet50_200Adapter,
        VitHFAdapter,
    )

    # 架构 → 适配器类；未登记或值为 None 的架构视为「仅入库管理」，不接入推理
    arch_adapters = {
        "resnet50": ResNet50_200Adapter,
        "vit_bird_525": VitHFAdapter,
        "vit_hf": VitHFAdapter,
        "resnest101": ResNeSt101Adapter,
    }
    # ViT 系列按目录加载（transformers 需要 config.json），其余按权重文件加载
    dir_based = ("vit_bird_525", "vit_hf")
    weight_suffixes = (".pt", ".pth", ".safetensors", ".bin")

    factory_ids = set(_get_species_registry().keys())  # 出厂条目 id，用于重名避让
    shared: dict = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "").strip()
        model_dir = Path(str(record.get("path") or ""))
        if not name or not model_dir.is_dir():
            continue  # 记录缺名字或目录不存在，跳过
        arch = str(record.get("architecture") or "resnet50").strip() or "resnet50"
        adapter_cls = arch_adapters.get(arch)
        if adapter_cls is None:
            continue  # 仅入库管理的架构（分割/LLM 等）不接入推理
        # 权重文件：best_model.pt → last_model.pt → model.safetensors → 其余候选
        weights_path = None
        for cand_name in ("best_model.pt", "last_model.pt", "model.safetensors"):
            cand = model_dir / cand_name
            if cand.is_file():
                weights_path = cand
                break
        if weights_path is None:
            try:
                for item in sorted(model_dir.iterdir()):
                    if item.is_file() and item.suffix.lower() in weight_suffixes:
                        weights_path = item
                        break
            except OSError:
                weights_path = None
        if weights_path is None:
            continue  # 无可用权重文件，非合格条目
        labels_path = model_dir / "labels.json"
        # 条目 id：与出厂条目或已登记共享条目重名时追加后缀
        entry_id = name if name not in factory_ids and name not in shared \
            else f"{name}（共享库）"
        if entry_id in factory_ids or entry_id in shared:
            continue  # 后缀仍冲突（极端情况）时跳过，宁缺毋滥
        # 显示名：模型名 + 主指标（如 resnet50_10月11日_001（Top-1 86.2%））
        display_name = name
        value = record.get("accuracy")
        if isinstance(value, (int, float)) and 0 < value <= 1:
            metric_name = str(record.get("metric_name") or "val_acc")
            label = {"val_acc": "Top-1", "top1_acc": "Top-1", "top5_acc": "Top-5",
                     "macro_f1": "宏F1"}.get(metric_name)
            if metric_name == "val_loss":
                display_name = f"{name}（loss {value:.4f}）"
            elif label:
                display_name = f"{name}（{label} {value * 100:.1f}%）"
            else:
                display_name = f"{name}（{metric_name} {value:.4f}）"
        # 构造参数：ViT 系列传目录；其余传权重文件；labels.json 可选
        if arch in dir_based:
            params = {"model_dir": str(model_dir)}
        else:
            params = {"weights_path": str(weights_path)}
        if labels_path.is_file():
            params["labels_path"] = str(labels_path)
        shared[entry_id] = {
            "adapter": adapter_cls,
            "params": params,
            "display_name": display_name,
        }
    return shared


def get_all_species_models(root=None) -> dict:
    """出厂 SPECIES_MODELS 与共享模型库条目的合并视图。

    出厂条目在前、共享条目在后；id 重名时共享条目不覆盖出厂条目。
    参数 root 含义同 get_shared_species_models。
    """
    merged = dict(_get_species_registry())  # 出厂条目（含惰性构建）
    for entry_id, entry in get_shared_species_models(root).items():
        if entry_id not in merged:  # 出厂条目优先，共享条目只做增量补充
            merged[entry_id] = entry
    return merged


def get_shared_part_models(root=None) -> dict:
    """读取共享模型库中的部位检测模型，转为与 PART_MODELS 同构的 dict。

    合格条目判定：记录的架构为 faster_rcnn（检测类），且模型目录下存在可用
    权重文件（best_detector.pth / last_detector.pth / best_model.pt / 其余
    .pt/.pth）。分类或分割等其它架构不属于部位检测器，一律跳过。

    部位名称沿用本系统 config.PART_TO_DETECTOR_IDX 约定的 15 部位索引，
    因此共享库条目应为同一套部位标注体系训练的检测器（管线 B 产物）。
    库缺失/损坏/无合格条目时返回 {}（绝不抛异常）。
    """
    records = _load_shared_registry_records(root)
    if not records:
        return {}
    # 惰性导入适配器（与 _build_registries 一致，规避循环引用）
    from model_adapters import TrainedFrcnnAdapter

    shared: dict = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        # 仅接入检测类架构；其余架构（分类/分割/通用）不属于部位检测器
        if str(record.get("architecture") or "").strip() != "faster_rcnn":
            continue
        name = str(record.get("name") or "").strip()
        model_dir = Path(str(record.get("path") or ""))
        if not name or not model_dir.is_dir():
            continue  # 记录缺名字或目录不存在，跳过
        # 权重文件：检测器产物优先，兼容分类式命名与其余候选
        weights_path = None
        for cand_name in ("best_detector.pth", "last_detector.pth",
                          "best_model.pt", "last_model.pt"):
            cand = model_dir / cand_name
            if cand.is_file():
                weights_path = cand
                break
        if weights_path is None:
            try:
                for item in sorted(model_dir.iterdir()):
                    if item.is_file() and item.suffix.lower() in (".pt", ".pth"):
                        weights_path = item
                        break
            except OSError:
                weights_path = None
        if weights_path is None:
            continue  # 无可用权重文件，非合格条目
        # 条目 id：与已登记共享条目重名时追加后缀（出厂条目 id 无需避让，
        # 因为检测类条目与出厂 owlvit/trained_frcnn 不会同名）
        entry_id = name if name not in shared else f"{name}（共享库）"
        if entry_id in shared:
            continue
        # 显示名：模型名 + 主指标（部位检测默认 mAP@0.5）
        display_name = name
        value = record.get("accuracy")
        if isinstance(value, (int, float)) and 0 < value <= 1:
            metric_name = str(record.get("metric_name") or "map50")
            label = {"map50": "mAP@0.5", "map": "mAP",
                     "mAP": "mAP"}.get(metric_name, metric_name)
            display_name = f"{name}（{label} {value * 100:.1f}%）"
        params = {"weights_path": str(weights_path)}   # 实例级权重路径覆盖
        if record.get("class_count"):
            params["num_classes"] = int(record["class_count"])
        shared[entry_id] = {
            "adapter": TrainedFrcnnAdapter,
            "params": params,
            "display_name": display_name,
        }
    return shared


def get_all_part_models(root=None) -> dict:
    """出厂 PART_MODELS 与共享模型库部位检测器的合并视图。

    出厂条目在前、共享条目在后；id 重名时共享条目不覆盖出厂条目。
    参数 root 含义同 get_shared_part_models。
    """
    merged = dict(_get_part_registry())  # 出厂条目（含惰性构建）
    for entry_id, entry in get_shared_part_models(root).items():
        if entry_id not in merged:  # 出厂条目优先，共享条目只做增量补充
            merged[entry_id] = entry
    return merged
