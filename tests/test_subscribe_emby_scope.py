import ast
import unittest
from pathlib import Path


class SubscribeEmbyScopeTests(unittest.TestCase):
    def test_run_matching_does_not_shadow_emby_client(self):
        src = (Path(__file__).resolve().parents[1] / "app/routers/subscribe.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_matching"
        )
        local_emby_imports = []
        for child in ast.walk(fn):
            if isinstance(child, ast.ImportFrom) and child.module == "app.services.emby":
                names = [alias.name for alias in (child.names or [])]
                if "EmbyClient" in names:
                    local_emby_imports.append(child.lineno)
        self.assertEqual([], local_emby_imports)
        self.assertIn("emby_verifier = EmbyClient()", src)


if __name__ == "__main__":
    unittest.main()
