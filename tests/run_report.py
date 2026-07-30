"""测试执行器 — 运行全部测试并生成报告

用法:
    python tests/run_report.py          # 运行全部并生成 HTML 报告
    python tests/run_report.py --smoke  # 仅冒烟测试
    python tests/run_report.py --unit   # 仅单元测试
    python tests/run_report.py --int    # 仅集成测试
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent
REPORT_DIR = TESTS_DIR / "reports"


def setup_env():
    """设置 SQLite 测试环境变量"""
    os.environ.setdefault("DB_DRIVER", "sqlite")
    os.environ.setdefault("DB_HOST", "")
    os.environ.setdefault("DB_NAME", "")
    os.environ.setdefault("DB_USER", "")
    os.environ.setdefault("DB_PASS", "")
    os.environ.setdefault("SECRET_KEY", "test-key-for-report-run")
    os.environ.setdefault("HARBOR_REGISTRY", "test.local")
    os.environ.setdefault("HARBOR_USER", "admin")
    os.environ.setdefault("HARBOR_PASSWORD", "test")
    os.environ.setdefault("MONITORING_ENABLED", "true")
    os.environ.setdefault("CI_API_URL", "")


def run_smoke():
    """运行冒烟测试"""
    print("\n" + "=" * 70)
    print("  阶段 1/2: 冒烟测试")
    print("=" * 70)
    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / "smoke.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print("[stderr]", result.stderr)
    return result.returncode == 0


def run_pytest(target: str, label: str):
    """运行 pytest"""
    print("\n" + "=" * 70)
    print(f"  阶段 2/2: {label}")
    print("=" * 70)
    cmd = [
        sys.executable, "-m", "pytest",
        str(TESTS_DIR / target),
        "-v", "--tb=short", "--color=yes",
        "--junitxml", str(REPORT_DIR / f"pytest_{target.replace('/', '_')}.xml"),
    ]
    result = subprocess.run(cmd, cwd=str(ROOT))
    return result.returncode == 0


def collect_results() -> dict:
    """汇总所有测试结果"""
    import xml.etree.ElementTree as ET

    results = {
        "timestamp": datetime.now().isoformat(),
        "title": "CD Service v1.2.2 测试报告",
        "suites": [],
        "summary": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
    }

    for xml_file in REPORT_DIR.glob("pytest_*.xml"):
        tree = ET.parse(xml_file)
        root = tree.getroot()
        for suite in root.findall("testsuite"):
            suite_data = {
                "name": suite.get("name", "unknown"),
                "tests": int(suite.get("tests", 0)),
                "failures": int(suite.get("failures", 0)),
                "errors": int(suite.get("errors", 0)),
                "skipped": int(suite.get("skipped", 0)),
                "time": float(suite.get("time", 0)),
                "cases": [],
            }
            results["suites"].append(suite_data)

            summary = results["summary"]
            summary["total"] += suite_data["tests"]
            summary["failed"] += suite_data["failures"]
            summary["errors"] += suite_data["errors"]
            summary["skipped"] += suite_data["skipped"]

            for case in suite.findall("testcase"):
                case_data = {
                    "name": case.get("name", ""),
                    "classname": case.get("classname", ""),
                    "time": float(case.get("time", 0)),
                    "status": "passed",
                    "message": "",
                }
                failure = case.find("failure")
                error = case.find("error")
                skipped = case.find("skipped")
                if failure is not None:
                    case_data["status"] = "failed"
                    case_data["message"] = failure.get("message", "")
                elif error is not None:
                    case_data["status"] = "error"
                    case_data["message"] = error.get("message", "")
                elif skipped is not None:
                    case_data["status"] = "skipped"
                    case_data["message"] = skipped.get("message", "")
                suite_data["cases"].append(case_data)

    summary = results["summary"]
    summary["passed"] = summary["total"] - summary["failed"] - summary["errors"] - summary["skipped"]

    # 读取冒烟测试结果
    smoke_report = TESTS_DIR / "smoke_report.json"
    if smoke_report.exists():
        with open(smoke_report, "r", encoding="utf-8") as f:
            smoke_data = json.load(f)
        results["smoke"] = smoke_data

    return results


def generate_html(results: dict) -> str:
    """生成 HTML 测试报告"""
    s = results["summary"]
    smoke = results.get("smoke", {})
    total_all = s["total"] + smoke.get("total", 0)
    passed_all = s["passed"] + smoke.get("passed", 0)
    failed_all = s["failed"] + smoke.get("failed", 0)
    pass_rate = f"{passed_all / total_all * 100:.1f}" if total_all > 0 else "N/A"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>CD Service v1.2.2 测试报告</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; padding: 20px; }}
  .container {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 24px; margin-bottom: 8px; }}
  .subtitle {{ color: #666; margin-bottom: 24px; }}
  .summary {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .card {{ background: #fff; border-radius: 8px; padding: 20px; min-width: 120px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); flex: 1; }}
  .card .num {{ font-size: 36px; font-weight: bold; }}
  .card .label {{ color: #666; font-size: 13px; margin-top: 4px; }}
  .card.total .num {{ color: #333; }}
  .card.pass .num {{ color: #22c55e; }}
  .card.fail .num {{ color: #ef4444; }}
  .card.skip .num {{ color: #f59e0b; }}
  .card.rate .num {{ color: #3b82f6; }}
  table {{ width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; }}
  th, td {{ padding: 10px 14px; text-align: left; font-size: 13px; }}
  th {{ background: #f8fafc; font-weight: 600; color: #475569; border-bottom: 1px solid #e2e8f0; }}
  td {{ border-bottom: 1px solid #f1f5f9; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
  .badge.pass {{ background: #dcfce7; color: #16a34a; }}
  .badge.fail {{ background: #fee2e2; color: #dc2626; }}
  .badge.skip {{ background: #fef3c7; color: #d97706; }}
  .badge.error {{ background: #fce7f3; color: #be185d; }}
  h2 {{ font-size: 18px; margin: 24px 0 12px; }}
  .time {{ color: #999; font-size: 12px; }}
  .detail {{ margin-top: 6px; padding: 8px 12px; background: #fef2f2; border-radius: 4px; font-size: 12px; font-family: monospace; white-space: pre-wrap; max-height: 120px; overflow: auto; }}
</style>
</head>
<body>
<div class="container">
  <h1>{results["title"]}</h1>
  <p class="subtitle">生成时间: {results["timestamp"]} · 分支: v1.2.2</p>

  <h2>总览</h2>
  <div class="summary">
    <div class="card total"><div class="num">{total_all}</div><div class="label">总计</div></div>
    <div class="card pass"><div class="num">{passed_all}</div><div class="label">通过</div></div>
    <div class="card fail"><div class="num">{failed_all}</div><div class="label">失败</div></div>
    <div class="card skip"><div class="num">{s.get("skipped", 0)}</div><div class="label">跳过</div></div>
    <div class="card rate"><div class="num">{pass_rate}%</div><div class="label">通过率</div></div>
  </div>

  <h2>冒烟测试 ({smoke.get("passed", 0)}/{smoke.get("total", 0)})</h2>
  <table>
    <tr><th>测试项</th><th>状态</th><th>详情</th></tr>
"""
    for item in smoke.get("results", []):
        cls = "pass" if item["status"] == "PASS" else "fail"
        detail = item.get("detail", "")
        html += f'    <tr><td>{item["name"]}</td><td><span class="badge {cls}">{item["status"]}</span></td><td class="time">{detail}</td></tr>\n'

    html += """  </table>

  <h2>单元测试 + 集成测试</h2>
"""
    for suite in results.get("suites", []):
        failed_cases = [c for c in suite["cases"] if c["status"] != "passed"]
        html += f"""  <h2 style="font-size:14px">{suite["name"]} ({suite["tests"]} tests, {suite["failures"]} failed, {suite["errors"]} errors, <span class="time">{suite["time"]:.2f}s</span>)</h2>
  <table>
    <tr><th>用例</th><th>类</th><th>状态</th><th>耗时</th></tr>
"""
        for c in suite["cases"]:
            status_cls = c["status"]
            html += f'    <tr><td>{c["name"]}</td><td class="time">{c["classname"]}</td><td><span class="badge {status_cls}">{c["status"]}</span></td><td class="time">{c["time"]:.3f}s</td></tr>\n'
            if c["message"]:
                html += f'    <tr><td colspan="4"><div class="detail">{c["message"]}</div></td></tr>\n'
        html += "  </table>\n"

    html += """
  <p class="subtitle" style="margin-top:32px">tests/ 目录不纳入版本控制</p>
</div>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="CD Service 测试执行器")
    parser.add_argument("--smoke", action="store_true", help="仅冒烟测试")
    parser.add_argument("--unit", action="store_true", help="仅单元测试")
    parser.add_argument("--int", action="store_true", help="仅集成测试")
    args = parser.parse_args()

    REPORT_DIR.mkdir(exist_ok=True)
    setup_env()

    start = time.time()
    smoke_ok = True
    unit_ok = True
    int_ok = True

    if args.unit:
        unit_ok = run_pytest("unit", "单元测试")
    elif args.int:
        int_ok = run_pytest("integration", "集成测试")
    elif args.smoke:
        smoke_ok = run_smoke()
    else:
        # 全部运行
        smoke_ok = run_smoke()
        unit_ok = run_pytest("unit", "单元测试 (exceptions, responses, models, database)")
        int_ok = run_pytest("integration", "集成测试 (API 路由, App, Auth)")

    elapsed = time.time() - start

    # 生成报告
    results = collect_results()
    html = generate_html(results)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"report_{timestamp}.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n{'='*70}")
    print(f"  测试完成 ({elapsed:.1f}s)")
    print(f"  报告: {report_path}")
    print(f"{'='*70}")

    # 退出码
    failed = not smoke_ok or not unit_ok or not int_ok
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
