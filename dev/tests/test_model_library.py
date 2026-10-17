#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 项目：iNaturalist 鸟类数据采集与图像下载系统
# 模块：共享模型库模块（model_library）的单元测试
# 作者签名：天咏（明月照今日，风摇过两川。）
# ============================================================

# 天咏 2026-10-11
"""model_library 共享模型库模块的单元测试。

覆盖内容：命名取号规则、注册/查询、删除与激活指针、损坏注册表恢复、
预览图生成、settings 读写往返、训练模式预设常量。
全部测试使用临时目录作为模型库根目录，不污染真实模型库。

运行方式（在本包上级目录执行）：
    python -m unittest tests.test_model_library -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# 保证无论从哪个工作目录运行，都能导入上级目录中的 model_library 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_library as ml  # noqa: E402

# 可选依赖：Pillow（缺失时预览图相关断言自动跳过）
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


class ModelLibraryTestBase(unittest.TestCase):
    """公共夹具：为每个测试提供独立的临时模型库目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)  # 临时目录即模型库根目录
        self.addCleanup(self._tmp.cleanup)

    def _make_model_dir(self, dir_name):
        """在临时库中创建一个模型目录并放入假权重文件，返回目录路径。"""
        model_dir = self.root / dir_name
        model_dir.mkdir(parents=True)
        (model_dir / "best.pth").write_bytes(b"fake-weights")
        return model_dir


class NextModelNameTests(ModelLibraryTestBase):
    """命名取号规则测试。"""

    def test_同基名同日期连续取号001到003(self):
        day = datetime(2026, 10, 11)
        dir_name, date_str, version = ml.next_model_name("resnet50", date=day, root=self.root)
        self.assertEqual((dir_name, date_str, version),
                         ("resnet50_10月11日_001", "10月11日", 1))
        # 注册第一个模型后再次取号应递增为 002
        self._make_model_dir(dir_name)
        ml.register_model(self.root / dir_name, class_count=3, root=self.root)
        dir_name2, _, version2 = ml.next_model_name("resnet50", date=day, root=self.root)
        self.assertEqual((dir_name2, version2), ("resnet50_10月11日_002", 2))
        # 继续注册第二个模型后取号应递增为 003
        self._make_model_dir(dir_name2)
        ml.register_model(self.root / dir_name2, class_count=3, root=self.root)
        dir_name3, _, version3 = ml.next_model_name("resnet50", date=day, root=self.root)
        self.assertEqual((dir_name3, version3), ("resnet50_10月11日_003", 3))

    def test_不同日期重新从001取号(self):
        day1 = datetime(2026, 10, 11)
        dir_name, _, _ = ml.next_model_name("resnet50", date=day1, root=self.root)
        self._make_model_dir(dir_name)
        ml.register_model(self.root / dir_name, class_count=1, root=self.root)
        # 换一天取号应重置为 001
        day2 = datetime(2026, 10, 12)
        dir_name2, date_str2, version2 = ml.next_model_name("resnet50", date=day2, root=self.root)
        self.assertEqual((dir_name2, date_str2, version2),
                         ("resnet50_10月12日_001", "10月12日", 1))

    def test_月日不补零(self):
        day = datetime(2026, 3, 5)
        dir_name, date_str, _ = ml.next_model_name("resnet50", date=day, root=self.root)
        self.assertEqual(date_str, "3月5日")
        self.assertEqual(dir_name, "resnet50_3月5日_001")

    def test_缺省日期取当天(self):
        today = datetime.now()
        _, date_str, _ = ml.next_model_name("vgg16", root=self.root)
        self.assertEqual(date_str, f"{today.month}月{today.day}日")


class RegisterAndQueryTests(ModelLibraryTestBase):
    """注册与查询测试。"""

    def test_注册后可查询且字段正确(self):
        model_dir = self._make_model_dir("resnet50_10月11日_001")
        record = ml.register_model(model_dir, class_count=200, accuracy=0.862,
                                   mode="low", root=self.root)
        self.assertEqual(record["name"], "resnet50_10月11日_001")
        self.assertEqual(record["base_name"], "resnet50")
        self.assertEqual(record["date_str"], "10月11日")
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["class_count"], 200)
        self.assertEqual(record["accuracy"], 0.862)
        self.assertEqual(record["mode"], "low")
        self.assertEqual(record["source"], "本机训练")
        self.assertTrue(record["trained_at"])
        self.assertEqual(Path(record["path"]), model_dir.resolve())
        # list_models 与 get_model 均可见
        names = [item["name"] for item in ml.list_models(root=self.root)]
        self.assertIn("resnet50_10月11日_001", names)
        got = ml.get_model("resnet50_10月11日_001", root=self.root)
        self.assertIsNotNone(got)
        self.assertEqual(got["class_count"], 200)
        self.assertIsNone(ml.get_model("不存在的模型", root=self.root))

    def test_目录不存在时报中文错误(self):
        with self.assertRaises(ValueError) as ctx:
            ml.register_model(self.root / "no_such_dir", class_count=1, root=self.root)
        self.assertIn("不存在", str(ctx.exception))

    def test_目录名不符合规则时用目录名兜底(self):
        model_dir = self._make_model_dir("my_custom_model")
        record = ml.register_model(model_dir, class_count=5, root=self.root)
        self.assertEqual(record["base_name"], "my_custom_model")
        self.assertEqual(record["version"], 1)
        self.assertTrue(record["date_str"].endswith("日"))


class RemoveAndActiveTests(ModelLibraryTestBase):
    """删除与激活模型测试。"""

    def test_删除后目录与记录被清理且激活置空(self):
        model_dir = self._make_model_dir("resnet50_10月11日_001")
        ml.register_model(model_dir, class_count=2, root=self.root)
        ml.set_active_species_model("resnet50_10月11日_001", root=self.root)
        active = ml.get_active_species_model(root=self.root)
        self.assertEqual(active["name"], "resnet50_10月11日_001")
        # 删除后：目录消失、记录消失、激活指针置空
        self.assertTrue(ml.remove_model("resnet50_10月11日_001", root=self.root))
        self.assertFalse(model_dir.exists())
        self.assertEqual(ml.list_models(root=self.root), [])
        self.assertIsNone(ml.get_model("resnet50_10月11日_001", root=self.root))
        self.assertIsNone(ml.get_active_species_model(root=self.root))
        # 注册表落盘内容中激活指针也应为 null
        reg = json.loads((self.root / "registry.json").read_text(encoding="utf-8"))
        self.assertIsNone(reg["active_species_model"])

    def test_删除不存在的模型返回False(self):
        self.assertFalse(ml.remove_model("不存在的模型", root=self.root))

    def test_激活不存在的模型时报错(self):
        with self.assertRaises(ValueError):
            ml.set_active_species_model("不存在的模型", root=self.root)


class RegistryRecoveryTests(ModelLibraryTestBase):
    """注册表损坏恢复测试。"""

    def test_损坏注册表自动备份并重建(self):
        reg_path = self.root / "registry.json"
        reg_path.write_text("{ 这不是合法的 JSON !!", encoding="utf-8")
        # 不应抛异常，且返回初始结构
        reg = ml.load_registry(root=self.root)
        self.assertEqual(reg["version"], 1)
        self.assertEqual(reg["models"], [])
        self.assertIsNone(reg["active_species_model"])
        # 原文件应已备份为 registry.json.bak.<时间戳>
        backups = list(self.root.glob("registry.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("这不是合法的 JSON", backups[0].read_text(encoding="utf-8"))
        # 落盘的注册表应已重建为初始结构
        reloaded = json.loads(reg_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["models"], [])

    def test_文件缺失时返回初始结构并落盘(self):
        reg = ml.load_registry(root=self.root)
        self.assertEqual(reg["models"], [])
        self.assertTrue((self.root / "registry.json").exists())


@unittest.skipUnless(HAS_PIL, "未安装 Pillow，跳过预览图测试")
class PreviewTests(ModelLibraryTestBase):
    """预览图生成测试（Pillow 可用时执行）。"""

    def _make_sample(self, file_name, color):
        """生成一张小样例图片并返回其路径。"""
        sample = self.root / file_name
        Image.new("RGB", (60, 40), color).save(sample)
        return sample

    def test_多张样图拼接预览(self):
        samples = [self._make_sample(f"样例{i}.png", (i * 60, 100, 150))
                   for i in range(1, 4)]
        out = self.root / "preview.jpg"
        result = ml.make_preview(samples, out, tile=112)
        self.assertIsNotNone(result)
        self.assertTrue(out.exists())

    def test_空样图列表仍可生成纯色块预览(self):
        out = self.root / "空预览.jpg"
        result = ml.make_preview([], out)
        self.assertIsNotNone(result)
        self.assertTrue(out.exists())

    def test_失效样图路径用色块补位(self):
        out = self.root / "坏样例预览.jpg"
        result = ml.make_preview([self.root / "不存在.jpg"], out)
        self.assertIsNotNone(result)
        self.assertTrue(out.exists())

    def test_默认占位预览可生成(self):
        out = self.root / "默认预览.jpg"
        result = ml.make_default_preview(out)
        self.assertIsNotNone(result)
        self.assertTrue(out.exists())


class SettingsTests(ModelLibraryTestBase):
    """settings.json 读写测试。"""

    def test_缺失时返回空字典(self):
        self.assertEqual(ml.read_settings(root=self.root), {})

    def test_写入读取往返(self):
        payload = {"视觉系统根目录": "./vision_data", "置信度阈值": 0.5, "开关列表": [1, 2, 3]}
        self.assertTrue(ml.write_settings(payload, root=self.root))
        self.assertEqual(ml.read_settings(root=self.root), payload)


class TrainingModePresetTests(unittest.TestCase):
    """训练模式预设常量测试（纯数据，不依赖临时目录）。"""

    def test_预设常量结构完整(self):
        for key in ("adaptive", "low", "high", "custom"):
            self.assertIn(key, ml.TRAINING_MODE_PRESETS)
            preset = ml.TRAINING_MODE_PRESETS[key]
            for field in ("label", "epochs", "optimizer", "patience",
                          "batch", "lr", "amp", "description"):
                self.assertIn(field, preset)
        self.assertEqual(set(ml.TRAINING_MODE_LABELS), set(ml.TRAINING_MODE_PRESETS))
        self.assertEqual(ml.TRAINING_MODE_LABELS["low"], "低速模式")
        self.assertEqual(ml.TRAINING_MODE_LABELS["adaptive"], "适应模式（ad）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
