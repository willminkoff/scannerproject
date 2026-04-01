import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENABLE_SCRIPT = ROOT / "scripts" / "enable-tailscale-https.sh"


class TailscaleHttpsScriptTests(unittest.TestCase):
    def test_enable_script_has_portable_timeout_wrapper(self):
        text = ENABLE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("run_with_timeout()", text)
        self.assertIn("command -v gtimeout", text)
        self.assertNotIn("require_cmd timeout", text)

    def test_enable_script_runs_without_timeout_binary_when_tailscale_and_curl_succeed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bindir = Path(tmpdir) / "bin"
            bindir.mkdir()

            tailscale_stub = bindir / "tailscale"
            tailscale_stub.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "${1:-}" == "status" && "${2:-}" == "--json" ]]; then
                      printf '%s' '{"Self":{"DNSName":"scanner.tail.example.ts.net."}}'
                      exit 0
                    fi
                    if [[ "${1:-}" == "status" ]]; then
                      exit 0
                    fi
                    if [[ "${1:-}" == "serve" && "${2:-}" == "status" ]]; then
                      echo "https://scanner.tail.example.ts.net (tailnet only)"
                      exit 0
                    fi
                    if [[ "${1:-}" == "serve" ]]; then
                      echo "serve configured"
                      exit 0
                    fi
                    echo "unexpected tailscale args: $*" >&2
                    exit 1
                    """
                ),
                encoding="utf-8",
            )
            tailscale_stub.chmod(stat.S_IRWXU)

            curl_stub = bindir / "curl"
            curl_stub.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    outfile=""
                    url=""
                    while [[ "$#" -gt 0 ]]; do
                      case "$1" in
                        -o)
                          outfile="$2"
                          shift 2
                          ;;
                        -w|-m)
                          shift 2
                          ;;
                        -s|-sS|-L)
                          shift
                          ;;
                        *)
                          url="$1"
                          shift
                          ;;
                      esac
                    done
                    if [[ -n "$outfile" ]]; then
                      if [[ "$url" == *"/api/status" ]]; then
                        printf '%s' '{"rtl_active":true}' > "$outfile"
                      else
                        : > "$outfile"
                      fi
                    fi
                    printf '200'
                    """
                ),
                encoding="utf-8",
            )
            curl_stub.chmod(stat.S_IRWXU)

            env = os.environ.copy()
            env["PATH"] = f"{bindir}:{env['PATH']}"

            result = subprocess.run(
                ["bash", str(ENABLE_SCRIPT)],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
            self.assertIn("PASS: HTTPS enabled at https://scanner.tail.example.ts.net", result.stdout)


if __name__ == "__main__":
    unittest.main()
