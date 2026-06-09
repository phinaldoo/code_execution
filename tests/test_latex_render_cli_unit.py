import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "sandbox" / "render_latex.py"


def load_module():
    spec = importlib.util.spec_from_file_location("render_latex", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LatexRenderCliTests(unittest.TestCase):
    def test_parse_request_preserves_common_asset_names(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            request_path = tmp_path / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "tex": "\\documentclass{article}\\begin{document}Hello\\end{document}",
                        "input_files": [
                            {"file_name": "chart 1.png", "base64_content": "aGVsbG8="},
                            {"file_name": "figure(2).jpg", "base64_content": "aGVsbG8="},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            _, _, input_files = module.parse_request(request_path)

            self.assertEqual([item.name for item in input_files], ["chart 1.png", "figure(2).jpg"])

    def test_render_from_file_builds_pdf_archive(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            def fake_run_pdflatex(work_dir, tex_path):
                self.assertEqual(tex_path.name, "main.tex")
                (work_dir / "main.pdf").write_bytes(b"%PDF-1.4\nmock pdf\n")
                (work_dir / "main.log").write_text("mock compile log", encoding="utf-8")
                return "stdout log", 0.12

            request_path = tmp_path / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "tex": "\\documentclass{article}\\begin{document}Hello\\end{document}",
                        "job_name": "hello-report",
                        "input_files": [],
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(module, "run_pdflatex", fake_run_pdflatex):
                result = module.render_from_file(request_path, tmp_path / "out")

            self.assertIsNone(result["error"])
            self.assertEqual(result["media_type"], "application/zip")
            self.assertEqual(result["pdf_file_name"], "hello-report.pdf")
            archive_path = Path(result["output_path"])
            self.assertTrue(archive_path.exists())

            import zipfile

            with zipfile.ZipFile(archive_path, "r") as archive:
                self.assertIn("hello-report.pdf", archive.namelist())
                self.assertTrue(archive.read("hello-report.pdf").startswith(b"%PDF"))
                self.assertTrue(archive.read("source/main.tex").decode("utf-8").startswith("\\documentclass"))
                self.assertEqual(archive.read("logs/pdflatex.log").decode("utf-8"), "mock compile log")

    def test_render_from_file_writes_input_assets_before_compilation(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            asset_bytes = b"fake-image-bytes"
            request_path = tmp_path / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "tex": "\\documentclass{article}\\usepackage{graphicx}\\begin{document}\\includegraphics{chart 1.png}\\end{document}",
                        "job_name": "asset-check",
                        "input_files": [
                            {
                                "file_name": "chart 1.png",
                                "base64_content": "ZmFrZS1pbWFnZS1ieXRlcw==",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def fake_run_pdflatex(work_dir, tex_path):
                self.assertEqual(tex_path.name, "main.tex")
                self.assertEqual((work_dir / "chart 1.png").read_bytes(), asset_bytes)
                (work_dir / "main.pdf").write_bytes(b"%PDF-1.4\nmock pdf\n")
                (work_dir / "main.log").write_text("mock compile log", encoding="utf-8")
                return "stdout log", 0.12

            with mock.patch.object(module, "run_pdflatex", fake_run_pdflatex):
                result = module.render_from_file(request_path, tmp_path / "out")

            self.assertEqual(result["pdf_file_name"], "asset-check.pdf")
            self.assertTrue(Path(result["output_path"]).exists())

    def test_render_from_file_preserves_compile_error_log(self):
        module = load_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            def fake_run_pdflatex(work_dir, tex_path):
                raise module.LatexExecutionError("pdflatex failed", "missing \\end{document}")

            request_path = tmp_path / "request.json"
            request_path.write_text(
                json.dumps({"tex": "\\documentclass{article}\\begin{document}", "job_name": "broken"}),
                encoding="utf-8",
            )

            with mock.patch.object(module, "run_pdflatex", fake_run_pdflatex):
                with self.assertRaises(module.LatexExecutionError) as ctx:
                    module.render_from_file(request_path, tmp_path / "out")

            self.assertIn("pdflatex failed", str(ctx.exception))
            self.assertIn("missing", ctx.exception.log_text)


if __name__ == "__main__":
    unittest.main()
