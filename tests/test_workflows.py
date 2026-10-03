from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowSyntaxTests(unittest.TestCase):
    def test_all_workflows_are_valid_yaml_mappings(self):
        workflow_dir = ROOT / ".github" / "workflows"
        for path in workflow_dir.glob("*.yml"):
            with self.subTest(path=path.name):
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(parsed, dict)
                self.assertIn("jobs", parsed)


class SigninWorkflowTests(unittest.TestCase):
    """手动触发时的 force 开关：用于忽略“今日已签到”标记重跑一次。"""

    def setUp(self):
        path = ROOT / ".github" / "workflows" / "quark_signin.yml"
        self.workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        # YAML 1.1 会把裸键 on 解析成布尔值 True
        self.conditions = {
            step.get("name"): step.get("if", "")
            for step in self.workflow["jobs"]["sign-in"]["steps"]
        }

    def test_force_input_is_declared_as_boolean(self):
        dispatch = self.workflow[True]["workflow_dispatch"]
        force = dispatch["inputs"]["force"]
        self.assertEqual(force["type"], "boolean")
        self.assertEqual(force["default"], False)

    def test_force_bypasses_the_skip_branch(self):
        condition = self.conditions["今日已完成签到"]
        self.assertIn("github.event.inputs.force != 'true'", condition)

    def test_force_runs_every_signin_step(self):
        for name in (
            "检出代码",
            "随机延迟",
            "设置 Python 环境",
            "安装依赖",
            "执行签到脚本",
        ):
            with self.subTest(step=name):
                self.assertIn(
                    "github.event.inputs.force == 'true'", self.conditions[name]
                )

    def test_marking_steps_still_require_a_fresh_cache(self):
        """标记保存必须保持原条件，否则强制重跑会因缓存键已存在而失败。"""

        for name in ("生成今日成功标记", "保存今日成功标记"):
            with self.subTest(step=name):
                self.assertIn("cache-hit != 'true'", self.conditions[name])
                self.assertNotIn("inputs.force", self.conditions[name])


if __name__ == "__main__":
    unittest.main()
