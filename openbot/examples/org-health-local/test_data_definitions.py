"""diagnosis.json 指标键名与口径文档的一致性测试。

从 `schema/diagnosis.schema.json` 的 properties（递归）收集键名，断言：

- 结构定义里的每个键名都在 `docs/scenario/org-diagnosis/data-definitions.md` 的键名总表里出现；
- 文档总表里没有结构定义之外的键名；
- 页面 `app.js` 实际读取的键名与合成示例里出现的键名都在总表里；
- 总表每一行都填了含义、单位、计算口径与来源角色（DA）。

只用标准库；不依赖 Bun、网络或真实组织数据。
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema" / "diagnosis.schema.json"
APP_PATH = ROOT / "app.js"
DATA_PATH = ROOT / "data" / "diagnosis.json"
DOC_RELATIVE = Path("docs") / "scenario" / "org-diagnosis" / "data-definitions.md"

# 页面 app.js 实际读取的键名。结构定义里的 facts 与 series 是唯一的例外：
# 它们只用于数字回溯，页面不读取。
PAGE_READ_KEYS = (
    "scope",
    "口径说明",
    "conclusion",
    "origin",
    "metrics",
    "insights",
    "definitions",
    "risks",
    "recommendations",
    "stage",
    "数据截止",
    "观察月份",
    "来源",
    "schema版本",
    "标题",
    "数值",
    "成本",
    "折算人头",
    "收入万美元每月",
    "整体技术人力成本率",
    "营收业务技术人力成本率",
    "全口径技术成本率含运维设施",
    "2026-01实际",
    "2026-05H1参考",
    "2026-09实际",
    "年末测算",
    "年末目标",
    "整体2026-01实际",
    "整体2026-09实际",
    "类型",
    "内容",
    "优先级",
    "依据",
    "缺失证据",
    "验证方式",
    "来源口径",
    "指标",
    "分档",
    "例证",
)

# 总表的列序：键名 | 页面读取位置 | 含义 | 单位 | 计算口径 | 来源角色
COLUMN_KEY = 0
COLUMN_LOCATION = 1
COLUMN_MEANING = 2
COLUMN_UNIT = 3
COLUMN_BASIS = 4
COLUMN_ROLE = 5
COLUMN_COUNT = 6

SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def find_doc() -> Path:
    """定位仓库内的口径文档；找不到时返回预期路径，由断言报错。"""
    for candidate in (ROOT, *ROOT.parents):
        path = candidate / DOC_RELATIVE
        if path.is_file():
            return path
    return ROOT.parents[2] / DOC_RELATIVE


def collect_schema_keys(schema: dict) -> set[str]:
    """递归收集结构定义里的属性名。

    收集各对象的 `properties` 键名，并递归进入 `properties`、`items`、`prefixItems`、
    `additionalProperties`、`$defs` 与组合子结构。同一对象的 `required` 列的也是该对象的
    属性名（期次锚点只出现在那里），所以一并收集。
    """
    keys: set[str] = set()
    seen: set[int] = set()

    def walk(node: object) -> None:
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, sub in properties.items():
                keys.add(name)
                walk(sub)
        required = node.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str):
                    keys.add(name)
        for name in ("items", "additionalProperties", "contains", "propertyNames"):
            walk(node.get(name))
        for name in ("prefixItems", "allOf", "anyOf", "oneOf"):
            value = node.get(name)
            if isinstance(value, list):
                for item in value:
                    walk(item)
        defs = node.get("$defs")
        if isinstance(defs, dict):
            for sub in defs.values():
                walk(sub)

    walk(schema)
    return keys


def collect_instance_keys(value: object) -> set[str]:
    """递归收集 JSON 实例里的对象键名。"""
    keys: set[str] = set()
    if isinstance(value, dict):
        for name, sub in value.items():
            keys.add(name)
            keys |= collect_instance_keys(sub)
    elif isinstance(value, list):
        for item in value:
            keys |= collect_instance_keys(item)
    return keys


def split_row(line: str) -> list[str]:
    """拆分 Markdown 表格行，返回去掉首尾空白的单元格列表。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def is_separator(line: str) -> bool:
    """判断是不是 Markdown 表格的分隔行。"""
    cells = [cell for cell in split_row(line) if cell]
    return bool(cells) and all(SEPARATOR_CELL.match(cell) for cell in cells)


def parse_doc_rows(text: str) -> list[list[str]]:
    """解析文档里第一列表头为「键名」的表格，返回全部数据行。"""
    lines = text.splitlines()
    rows: list[list[str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.lstrip().startswith("|"):
            index += 1
            continue
        header = split_row(line)
        if not header or header[0] != "键名":
            index += 1
            continue
        index += 1
        if index < len(lines) and is_separator(lines[index]):
            index += 1
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            rows.append(split_row(lines[index]))
            index += 1
    return rows


class DataDefinitionsTest(unittest.TestCase):
    """口径文档的键名总表与结构定义、页面读取保持一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.schema_keys = collect_schema_keys(cls.schema)
        cls.doc_path = find_doc()
        cls.doc_text = cls.doc_path.read_text(encoding="utf-8") if cls.doc_path.is_file() else ""
        cls.rows = parse_doc_rows(cls.doc_text)
        cls.doc_keys = {row[COLUMN_KEY] for row in cls.rows if row}

    def test_doc_exists(self) -> None:
        """口径文档存在。"""
        self.assertTrue(self.doc_path.is_file(), f"口径文档不存在：{self.doc_path}")

    def test_doc_has_key_table(self) -> None:
        """口径文档里有键名总表。"""
        self.assertTrue(self.rows, f"{self.doc_path} 里没有找到键名总表")

    def test_schema_keys_are_documented(self) -> None:
        """结构定义里的每个键名都在文档总表里出现。"""
        missing = sorted(self.schema_keys - self.doc_keys)
        self.assertEqual(missing, [], f"键名总表缺少结构定义里的键名：{missing}")

    def test_doc_has_no_keys_outside_schema(self) -> None:
        """文档总表里没有结构定义之外的键名。"""
        extra = sorted(self.doc_keys - self.schema_keys)
        self.assertEqual(extra, [], f"键名总表出现结构定义之外的键名：{extra}")

    def test_page_read_keys_are_documented(self) -> None:
        """页面实际读取的键名都在文档总表里。"""
        missing = sorted(set(PAGE_READ_KEYS) - self.doc_keys)
        self.assertEqual(missing, [], f"键名总表缺少页面读取的键名：{missing}")

    def test_page_read_keys_exist_in_schema(self) -> None:
        """页面读取的键名都在结构定义里。"""
        extra = sorted(set(PAGE_READ_KEYS) - self.schema_keys)
        self.assertEqual(extra, [], f"页面读取了结构定义之外的键名：{extra}")

    def test_example_keys_are_documented(self) -> None:
        """合成示例里出现的键名都在文档总表里，且都在结构定义里。"""
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        instance_keys = collect_instance_keys(data)
        missing = sorted(instance_keys - self.doc_keys)
        self.assertEqual(missing, [], f"键名总表缺少合成示例里的键名：{missing}")
        extra = sorted(instance_keys - self.schema_keys)
        self.assertEqual(extra, [], f"合成示例出现结构定义之外的键名：{extra}")

    def test_rows_state_meaning_unit_basis_and_role(self) -> None:
        """总表每一行都填了含义、单位、计算口径与来源角色（DA）。"""
        for row in self.rows:
            key = row[COLUMN_KEY]
            self.assertGreaterEqual(len(row), COLUMN_COUNT, f"键名 {key} 的列数不足")
            for index, name in (
                (COLUMN_MEANING, "含义"),
                (COLUMN_UNIT, "单位"),
                (COLUMN_BASIS, "计算口径"),
                (COLUMN_ROLE, "来源角色"),
            ):
                self.assertTrue(row[index], f"键名 {key} 缺少{name}")
            self.assertEqual(row[COLUMN_ROLE], "DA", f"键名 {key} 的来源角色不是 DA")

    def test_rows_mark_page_usage_consistently(self) -> None:
        """总表里标了 app.js 读取位置的键名，就是页面读取的键名。"""
        for row in self.rows:
            key = row[COLUMN_KEY]
            location = row[COLUMN_LOCATION] if len(row) > COLUMN_LOCATION else ""
            reads_page = "app.js" in location
            self.assertEqual(
                reads_page,
                key in PAGE_READ_KEYS,
                f"键名 {key} 的页面读取位置与页面实际读取情况不一致：{location}",
            )

    def test_app_js_reads_documented_keys(self) -> None:
        """页面读取的键名在 app.js 里确实出现（键名拼写没有漂移）。"""
        source = APP_PATH.read_text(encoding="utf-8")
        # 全口径成本率的两个键在页面里是按「整体 + 期次」拼出来的，不是字面量。
        prefixed = ("整体2026-01实际", "整体2026-09实际")
        missing = [
            key
            for key in PAGE_READ_KEYS
            if key not in prefixed and key not in source
        ]
        self.assertEqual(missing, [], f"app.js 里找不到这些键名：{missing}")
        self.assertIn("整体${periods.start}", source)
        self.assertIn("整体${periods.end}", source)


if __name__ == "__main__":
    unittest.main()
