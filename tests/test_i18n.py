"""PortGenerator i18n 一致性单测（校验 plugin.json 与代码 key 清单）"""

import json
import string
import unittest
from pathlib import Path

from src.port_main import I18N_KEYS

PLUGIN_JSON = Path(__file__).resolve().parent.parent / "plugin.json"


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class TestI18n(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
        cls.i18n = cls.manifest["I18n"]

    def test_supported_languages(self):
        self.assertIn("en_US", self.i18n)
        self.assertIn("zh_CN", self.i18n)

    def test_key_sets_match(self):
        en = set(self.i18n["en_US"])
        zh = set(self.i18n["zh_CN"])
        self.assertEqual(en, zh, f"中英文 key 不一致: {en ^ zh}")

    def test_code_keys_covered(self):
        # 代码引用的 key + manifest 引用的 key 必须恰好覆盖所有翻译，且没有多余项
        manifest_keys = {
            self.manifest[field][len("i18n:") :] for field in ("Name", "Description") if self.manifest[field].startswith("i18n:")
        }
        self.assertEqual(set(I18N_KEYS) | manifest_keys, set(self.i18n["en_US"]))
        self.assertEqual(len(I18N_KEYS), len(set(I18N_KEYS)), "I18N_KEYS 存在重复")

    def test_manifest_name_description(self):
        for field in ("Name", "Description"):
            value = self.manifest[field]
            self.assertTrue(value.startswith("i18n:"), f"{field} 缺少 i18n: 前缀")
            key = value[len("i18n:") :]
            for lang in self.i18n:
                self.assertIn(key, self.i18n[lang], f"{field} 引用的 key {key} 在 {lang} 中缺失")

    def test_placeholders_match(self):
        for key in self.i18n["en_US"]:
            en = _placeholders(self.i18n["en_US"][key])
            zh = _placeholders(self.i18n["zh_CN"][key])
            self.assertEqual(en, zh, f"key {key} 的占位符不一致: {en} vs {zh}")


if __name__ == "__main__":
    unittest.main()
