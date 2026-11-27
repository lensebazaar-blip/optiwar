import ast
import os
import subprocess
import sys
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module_file(module):
    module_path = os.path.join(REPO, *module.split("."))
    source_path = module_path + ".py"
    if os.path.isfile(source_path):
        return os.path.relpath(source_path, REPO)
    package_path = os.path.join(module_path, "__init__.py")
    if os.path.isfile(package_path):
        return os.path.relpath(package_path, REPO)
    return None


def _models_relative_module_files():
    models_path = os.path.join(REPO, "models.py")
    with open(models_path, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), filename=models_path)

    modules = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            module_file = _module_file(node.module)
            if module_file:
                modules.add(module_file)
            continue
        for imported in node.names:
            module_file = _module_file(imported.name)
            if module_file:
                modules.add(module_file)
    return modules


def _deployment_lists():
    deploy_path = os.path.join(REPO, "deploy", "deploy.py")
    with open(deploy_path, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), filename=deploy_path)

    lists = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in (
                    "DEPLOY_SET", "NEW_IN_RELEASE"):
                lists[target.id] = set(ast.literal_eval(node.value))
    return lists


class DeployListsTest(unittest.TestCase):
    # These omissions are already present on main and must stay out of the
    # manifests pending an owner decision. mail.py's production copy must never
    # be overwritten; embed_helper.py is ahead in production; the other three
    # are pre-existing omissions.
    DEPLOY_SET_OMISSIONS = {
        "mail.py",
        "embed_helper.py",
        "db.py",
        "cl_range_model.py",
        "country_iso.py",
    }

    # NEW_IN_RELEASE is per-release. These additional modules are also
    # imported by models.py but are not missing from the current production
    # release; do not add them without an owner decision.
    NEW_IN_RELEASE_OMISSIONS = DEPLOY_SET_OMISSIONS | {
        "acr.py",
        "ai_client.py",
        "cart_persist.py",
        "models.py",
        "payments.py",
        "pricing.py",
    }

    def test_models_relative_import_modules_are_in_shipping_deploy_lists(self):
        imports = _models_relative_module_files()
        lists = _deployment_lists()
        covered_lists = [
            list_name for list_name, paths in lists.items()
            if "shipping_options.py" in paths
        ]
        self.assertIn("DEPLOY_SET", covered_lists)
        for list_name in covered_lists:
            paths = lists[list_name]
            self.assertIn("india_offer.py", paths)
            self.assertIn("india_states.py", paths)
            missing = sorted(imports - lists[list_name])
            if list_name == "DEPLOY_SET":
                allowed = self.DEPLOY_SET_OMISSIONS
            elif list_name == "NEW_IN_RELEASE":
                allowed = self.NEW_IN_RELEASE_OMISSIONS
            else:
                allowed = set()
            with self.subTest(deploy_list=list_name):
                self.assertEqual(
                    set(missing),
                    allowed,
                    "%s omits models.py relative-import module files: %s"
                    % (list_name, ", ".join(missing)),
                )

    def test_mail_is_not_in_any_deploy_list(self):
        for list_name, paths in _deployment_lists().items():
            with self.subTest(deploy_list=list_name):
                self.assertNotIn("mail.py", paths)

    def test_shipping_options_deploy_loader_works_from_tmp_without_repo_path(self):
        deploy_path = os.path.join(REPO, "deploy", "deploy.py")
        code = (
            "import importlib.util, os, sys; "
            f"repo = {REPO!r}; "
            "sys.path = [path for path in sys.path "
            "if os.path.realpath(path or os.getcwd()) != repo]; "
            "assert repo not in [os.path.realpath(path or os.getcwd()) "
            "for path in sys.path]; "
            f"spec = importlib.util.spec_from_file_location('deploy_probe', {deploy_path!r}); "
            "deploy = importlib.util.module_from_spec(spec); "
            "sys.modules[spec.name] = deploy; "
            "spec.loader.exec_module(deploy); "
            "print(deploy.shipping_options_module().TABLES)"
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("order_shipping", result.stdout)


if __name__ == "__main__":
    unittest.main()
