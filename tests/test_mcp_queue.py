import json
import unittest

from scripts import mcp_queue


class StreamableHttpParsingTests(unittest.TestCase):
    def test_parse_streamable_http_event(self):
        body = (
            "event: message\n"
            'data: {"result":{"content":[{"type":"text","text":"{\\"ok\\": true}"}]},"jsonrpc":"2.0","id":2}\n'
        )

        parsed = mcp_queue._parse_streamable_http(body)

        self.assertEqual(True, parsed["result"]["content"][0]["text"].startswith("{"))

    def test_coerce_content_text_parses_json(self):
        result = {
            "content": [
                {"type": "text", "text": json.dumps({"status": "review", "owner_id": "claude"})}
            ]
        }

        parsed = mcp_queue._coerce_content_text(result)

        self.assertEqual({"status": "review", "owner_id": "claude"}, parsed)

    def test_artifact_spec_requires_type_and_path(self):
        self.assertEqual(("test_result", "/tmp/out.log"), mcp_queue._artifact_spec("test_result:/tmp/out.log"))
        with self.assertRaises(Exception):
            mcp_queue._artifact_spec("missing-delimiter")


if __name__ == "__main__":
    unittest.main()
